"""Read-side helpers behind the dashboard's list, activity and stats routes.

The dashboard's ``/api/*`` routes answer for every repository in the
registry. The aggregation lives here, taking the repositories as an explicit
argument, so any other caller can build the same response models over a
subset of them.
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mira.dashboard import api as _api
from mira.dashboard.db import RepoRecord
from mira.index.store import IndexStore

if TYPE_CHECKING:
    from mira.index.store import ReviewEvent

logger = logging.getLogger(__name__)

# Search scans history until this many matches per repo; an empty search keeps
# the recent feed at the same size.
ACTIVITY_SCAN_LIMIT = 500


def repo_list_item(r: RepoRecord) -> _api.RepoListItem:
    """Registry row → the dashboard's repo list entry."""
    return _api.RepoListItem(
        owner=r.owner,
        repo=r.repo,
        platform=r.platform,
        status=r.status,
        index_mode=r.index_mode,
        file_count=r.files_indexed,
        file_count_estimate=r.file_count_estimate,
        installation_id=r.installation_id,
        error=r.error,
        last_indexed=datetime.fromtimestamp(r.last_indexed_at, tz=UTC).isoformat()
        if r.last_indexed_at
        else None,
    )


def review_event_model(e: ReviewEvent) -> _api.ReviewEventModel:
    """Store row → the per-repo review event response."""
    return _api.ReviewEventModel(
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
    )


def list_repo_reviews(repo_record: RepoRecord, limit: int = 50) -> list[_api.ReviewEventModel]:
    """Recent review events for one registered repository."""
    store = IndexStore.open(repo_record.owner, repo_record.repo, platform=repo_record.platform)
    try:
        return [review_event_model(e) for e in store.list_review_events(limit=limit)]
    finally:
        store.close()


def collect_activity(
    repos: list[RepoRecord], *, limit: int = 200, repo: str = "", q: str = ""
) -> _api.ActivityResponse:
    """Feed of review events across *repos*, newest first.

    Flattens per-repo review_events into a single list sorted by created_at
    (newest first), attaching owner/repo to each. Supports an optional repo
    filter ("owner/repo") and a case-insensitive search ``q`` matched across
    the PR title, PR number, repo slug, and categories (multi-word queries
    AND). Search scans history until ``ACTIVITY_SCAN_LIMIT`` matches per
    repo; an empty search keeps the recent feed. Matching timestamp ties use
    ascending event IDs.
    """
    terms = [t for t in q.lower().split() if t]
    repo_slugs = sorted(f"{r.owner}/{r.repo}" for r in repos)

    events: list[_api.ActivityEventModel] = []
    for repo_record in repos:
        slug = f"{repo_record.owner}/{repo_record.repo}"
        if repo and slug != repo:
            continue
        try:
            store = IndexStore.open(
                repo_record.owner, repo_record.repo, platform=repo_record.platform
            )
            try:
                source = (
                    store.iter_review_events()
                    if terms
                    else nullcontext(store.list_review_events(limit=ACTIVITY_SCAN_LIMIT))
                )
                with source as review_events:
                    matched = 0
                    for e in review_events:
                        if terms:
                            haystack = f"{e.pr_title} #{e.pr_number} {slug} {e.categories}".lower()
                            if not all(t in haystack for t in terms):
                                continue
                        events.append(
                            _api.ActivityEventModel(
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
                                author_username=e.author,
                                author_avatar_url=e.author_avatar_url,
                            )
                        )
                        matched += 1
                        if terms and matched >= ACTIVITY_SCAN_LIMIT:
                            break
            finally:
                store.close()
        except Exception:
            logger.warning("Failed to read activity for %s", slug, exc_info=True)

    events.sort(key=lambda ev: ev.created_at, reverse=True)
    return _api.ActivityResponse(events=events[:limit], repos=repo_slugs)


def aggregate_org_stats(
    repos: list[RepoRecord],
    since: float | None = None,
    *,
    scoped: bool = False,
) -> _api.OrgStatsModel:
    """Aggregate review stats across *repos*, optionally after *since*.

    Relationship counts (edges, groups) come from the cross-repo relationship
    store. With ``scoped=True`` they are restricted to edges and groups whose
    repositories are all in *repos*; the dashboard passes ``False`` and gets
    the deployment-wide counts it always showed.
    """
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
            store = IndexStore.open(
                repo_record.owner, repo_record.repo, platform=repo_record.platform
            )
            try:
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
            finally:
                store.close()
        except Exception:
            logger.warning(
                "Failed to read stats for %s/%s", repo_record.owner, repo_record.repo, exc_info=True
            )

    agg_stats["avg_duration_ms"] = int(duration_sum / review_count) if review_count > 0 else 0
    agg_stats["avg_comments_per_pr"] = (
        round(agg_stats["total_comments"] / review_count, 1) if review_count > 0 else 0.0
    )

    total_edges = 0
    total_groups = 0
    try:
        with _api._open_relationships() as rs:
            edges = rs.resolve_edges()
            names = rs.repos
            if scoped:
                allowed = {f"{r.owner}/{r.repo}" for r in repos}
                edges = [e for e in edges if e.source_repo in allowed and e.target_repo in allowed]
                names = [n for n in names if n in allowed]
            total_edges = len(edges)
            total_groups = len(rs.group_repos(names))
    except Exception:
        pass

    return _api.OrgStatsModel(
        total_repos=total_repos,
        total_files=total_files,
        total_edges=total_edges,
        total_groups=total_groups,
        review_stats=_api.ReviewStatsModel(**agg_stats),
    )
