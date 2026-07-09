"""Dashboard core routes"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import Request
from fastapi.responses import StreamingResponse

from mira.dashboard import api as _api
from mira.dashboard.api import (
    ActivityEventModel,
    ActivityResponse,
    CostEstimate,
    IndexStatusModel,
    OrgStatsModel,
    ReviewStatsModel,
    TimeSeriesPoint,
    _open_relationships,
    _period_to_since,
    logger,
    router,
)
from mira.index.store import IndexStore


@router.get("/api/version")
def get_version() -> dict[str, str]:
    """Return the running Mira version and the bot's @mention handle. The
    dashboard renders the version next to the logo, and uses bot_name so help
    text shows the real handle instead of a hardcoded placeholder. bot_name is
    persisted by `mira serve` (env override, else the App's auto-detected slug);
    falls back to "miracodeai" before the server has recorded it."""
    from mira import __version__

    return {
        "version": __version__,
        "bot_name": _api._app_db.get_setting("bot_name") or "miracodeai",
    }


@router.get("/api/indexing/status", response_model=list[IndexStatusModel])
def get_indexing_status() -> list[IndexStatusModel]:
    """Get current indexing status for all repos."""
    from mira.index.status import tracker

    return [
        IndexStatusModel(
            repo=j.repo,
            status=j.status,
            files_total=j.files_total,
            files_done=j.files_done,
            started_at=j.started_at,
            finished_at=j.finished_at,
            error=j.error,
        )
        for j in tracker.get_all()
    ]


@router.get("/api/indexing/estimate", response_model=CostEstimate)
def estimate_cost() -> CostEstimate:
    """Estimate the cost of indexing all pending repos with the current model."""
    from mira.config import load_config
    from mira.dashboard.models_config import (
        estimate_indexing_cost,
        get_indexing_model,
    )

    config = load_config()
    model = get_indexing_model(config.llm, _api._app_db.get_setting("indexing_model"))

    # Sum file counts across all pending repos
    total_files = sum(
        r.file_count_estimate for r in _api._app_db.list_repos() if r.status == "pending"
    )

    est = estimate_indexing_cost(total_files, model)
    return CostEstimate(
        estimated_usd=est["estimated_usd"],
        input_tokens=est["input_tokens"],
        output_tokens=est["output_tokens"],
        model=model,
        file_count=total_files,
    )


@router.get("/api/events")
async def events_stream(request: Request) -> StreamingResponse:
    """Server-Sent Events stream for real-time dashboard updates."""
    from mira.dashboard.events import bus, format_sse

    async def generate():
        q = await bus.subscribe()
        try:
            # Send a heartbeat immediately
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield format_sse(event)
                except TimeoutError:
                    # Heartbeat to keep connection alive
                    yield ": heartbeat\n\n"
        finally:
            await bus.unsubscribe(q)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/activity", response_model=ActivityResponse)
def list_activity(limit: int = 200, repo: str = "", q: str = "") -> ActivityResponse:
    """Org-wide feed of review events across all repos.

    Flattens per-repo review_events into a single list sorted by created_at
    (newest first), attaching owner/repo to each. Supports an optional repo
    filter ("owner/repo") and a case-insensitive search `q` matched across the
    PR title, PR number, repo slug, and categories (multi-word queries AND).
    """
    terms = [t for t in q.lower().split() if t]

    repos = _api._app_db.list_repos()
    repo_slugs = sorted(f"{r.owner}/{r.repo}" for r in repos)

    events: list[ActivityEventModel] = []
    for repo_record in repos:
        slug = f"{repo_record.owner}/{repo_record.repo}"
        if repo and slug != repo:
            continue
        try:
            store = IndexStore.open(
                repo_record.owner, repo_record.repo, platform=repo_record.platform
            )
            try:
                for e in store.list_review_events(limit=500):
                    if terms:
                        haystack = f"{e.pr_title} #{e.pr_number} {slug} {e.categories}".lower()
                        if not all(t in haystack for t in terms):
                            continue
                    events.append(
                        ActivityEventModel(
                            id=e.id,
                            pr_number=e.pr_number,
                            pr_title=e.pr_title,
                            pr_url=e.pr_url,
                            comments_posted=e.comments_posted,
                            blockers=e.blockers,
                            warnings=e.warnings,
                            suggestions=e.suggestions,
                            files_reviewed=e.files_reviewed,
                            lines_changed=e.lines_changed,
                            tokens_used=e.tokens_used,
                            duration_ms=e.duration_ms,
                            categories=e.categories,
                            created_at=e.created_at,
                            owner=repo_record.owner,
                            repo=repo_record.repo,
                        )
                    )
            finally:
                store.close()
        except Exception:
            logger.warning("Failed to read activity for %s", slug, exc_info=True)

    events.sort(key=lambda ev: ev.created_at, reverse=True)
    return ActivityResponse(events=events[:limit], repos=repo_slugs)


@router.get("/api/stats", response_model=OrgStatsModel)
def get_org_stats(period: str = "") -> OrgStatsModel:
    """Aggregate stats across all repos, optionally filtered by period."""
    since = _period_to_since(period) if period else None

    repos = _api._app_db.list_repos()
    total_repos = len(repos)
    total_files = 0
    agg_stats: dict = {
        "total_reviews": 0,
        "total_comments": 0,
        "total_blockers": 0,
        "total_warnings": 0,
        "total_suggestions": 0,
        "total_files_reviewed": 0,
        "total_lines_changed": 0,
        "total_tokens": 0,
        "avg_duration_ms": 0,
        "categories": {},
        "avg_comments_per_pr": 0.0,
    }
    duration_sum = 0
    review_count = 0

    for repo_record in repos:
        try:
            store = IndexStore.open(repo_record.owner, repo_record.repo)
            total_files += len(store.all_paths())
            stats = store.get_review_stats(since=since)
            agg_stats["total_reviews"] += stats["total_reviews"]
            agg_stats["total_comments"] += stats["total_comments"]
            agg_stats["total_blockers"] += stats["total_blockers"]
            agg_stats["total_warnings"] += stats["total_warnings"]
            agg_stats["total_suggestions"] += stats["total_suggestions"]
            agg_stats["total_files_reviewed"] += stats["total_files_reviewed"]
            agg_stats["total_lines_changed"] += stats["total_lines_changed"]
            agg_stats["total_tokens"] += stats["total_tokens"]
            for cat, cnt in stats.get("categories", {}).items():
                agg_stats["categories"][cat] = agg_stats["categories"].get(cat, 0) + cnt
            if stats["total_reviews"] > 0:
                duration_sum += stats["avg_duration_ms"] * stats["total_reviews"]
                review_count += stats["total_reviews"]
            store.close()
        except Exception:
            logger.warning(
                "Failed to read stats for %s/%s", repo_record.owner, repo_record.repo, exc_info=True
            )

    agg_stats["avg_duration_ms"] = int(duration_sum / review_count) if review_count > 0 else 0
    agg_stats["avg_comments_per_pr"] = (
        round(agg_stats["total_comments"] / review_count, 1) if review_count > 0 else 0.0
    )

    # Get relationship counts
    total_edges = 0
    total_groups = 0
    try:
        with _open_relationships() as rs:
            total_edges = len(rs.resolve_edges())
            total_groups = len(rs.group_repos(rs.repos))
    except Exception:
        pass

    return OrgStatsModel(
        total_repos=total_repos,
        total_files=total_files,
        total_edges=total_edges,
        total_groups=total_groups,
        review_stats=ReviewStatsModel(**agg_stats),
    )


@router.get("/api/stats/timeseries", response_model=list[TimeSeriesPoint])
def get_timeseries(period: str = "day") -> list[TimeSeriesPoint]:
    """Aggregate review metrics over time. Period: day, week, month."""
    all_events: list[dict] = []

    for repo_record in _api._app_db.list_repos():
        try:
            store = IndexStore.open(repo_record.owner, repo_record.repo)
            for e in store.list_review_events(limit=500):
                all_events.append(
                    {
                        "created_at": e.created_at,
                        "comments": e.comments_posted,
                        "blockers": e.blockers,
                        "warnings": e.warnings,
                        "suggestions": e.suggestions,
                        "lines": e.lines_changed,
                        "tokens": e.tokens_used,
                        "categories": e.categories,
                    }
                )
            store.close()
        except Exception:
            pass

    if not all_events:
        return []

    # Bucket by period
    from collections import defaultdict

    buckets: dict[str, dict] = defaultdict(
        lambda: {
            "reviews": 0,
            "comments": 0,
            "blockers": 0,
            "warnings": 0,
            "suggestions": 0,
            "lines_changed": 0,
            "tokens_used": 0,
            "categories": {},
        }
    )

    for ev in all_events:
        dt = datetime.fromtimestamp(ev["created_at"], tz=UTC)
        if period == "month":
            key = dt.strftime("%Y-%m")
        elif period == "week":
            key = dt.strftime("%Y-W%W")
        else:
            key = dt.strftime("%Y-%m-%d")

        b = buckets[key]
        b["reviews"] += 1
        b["comments"] += ev["comments"]
        b["blockers"] += ev["blockers"]
        b["warnings"] += ev["warnings"]
        b["suggestions"] += ev["suggestions"]
        b["lines_changed"] += ev["lines"]
        b["tokens_used"] += ev["tokens"]
        for c in (ev["categories"] or "").split(","):
            c = c.strip()
            if c:
                b["categories"][c] = b["categories"].get(c, 0) + 1

    return [TimeSeriesPoint(date=k, **v) for k, v in sorted(buckets.items())]
