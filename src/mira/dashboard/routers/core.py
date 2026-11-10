"""Dashboard core routes"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import Request
from fastapi.responses import StreamingResponse

from mira.dashboard import api as _api
from mira.dashboard import queries as _queries
from mira.dashboard.api import (
    ActivityResponse,
    CostEstimate,
    IndexStatusModel,
    OrgStatsModel,
    TimeSeriesPoint,
    _period_to_since,
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
    Search scans history until 500 matches per repo; an empty search keeps
    the recent-500 feed. Matching timestamp ties use ascending event IDs.
    """
    return _queries.collect_activity(_api._app_db.list_repos(), limit=limit, repo=repo, q=q)


@router.get("/api/stats", response_model=OrgStatsModel)
def get_org_stats(period: str = "") -> OrgStatsModel:
    """Aggregate stats across all repos, optionally filtered by period."""
    since = _period_to_since(period) if period else None
    return _queries.aggregate_org_stats(_api._app_db.list_repos(), since)


@router.get("/api/stats/timeseries", response_model=list[TimeSeriesPoint])
def get_timeseries(period: str = "day") -> list[TimeSeriesPoint]:
    """Aggregate review metrics over time. Period: day, week, month."""
    from collections import defaultdict
    from contextlib import closing

    def iter_events():
        for repo_record in _api._app_db.list_repos():
            try:
                store = IndexStore.open(
                    repo_record.owner, repo_record.repo, platform=repo_record.platform
                )
                try:
                    with store.iter_review_events() as events:
                        yield from events
                finally:
                    store.close()
            except Exception:
                pass

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

    with closing(iter_events()) as events:
        for ev in events:
            dt = datetime.fromtimestamp(ev.created_at, tz=UTC)
            if period == "month":
                key = dt.strftime("%Y-%m")
            elif period == "week":
                iso_year, iso_week, _ = dt.isocalendar()
                key = f"{iso_year}-W{iso_week:02d}"
            else:
                key = dt.strftime("%Y-%m-%d")

            b = buckets[key]
            b["reviews"] += 1
            b["comments"] += ev.comments_posted
            b["blockers"] += ev.blockers
            b["warnings"] += ev.warnings
            b["suggestions"] += ev.suggestions
            b["lines_changed"] += ev.lines_changed
            b["tokens_used"] += ev.tokens_used
            for c in (ev.categories or "").split(","):
                c = c.strip()
                if c:
                    b["categories"][c] = b["categories"].get(c, 0) + 1

    return [TimeSeriesPoint(date=k, **v) for k, v in sorted(buckets.items())]
