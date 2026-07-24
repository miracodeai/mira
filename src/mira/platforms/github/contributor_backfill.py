"""Backfill historical contributor activity from the GitHub API.

Pulls a repo's pull requests (opened + merged), the reviews people gave on
them, and (optionally) its commits, recording each via
``AppDatabase.record_contribution_for_login``. Every event is keyed
idempotently (``pr:``/``prm:``/``review:``/``commit:``), so a backfill can be
re-run or overlap the live webhooks without double-counting — see
``AppDatabase.record_contribution``.

This is the heavy, rate-limited path; commits are the most expensive phase and
can be skipped (``include_commits=False``) for a fast first pass that still
populates a meaningful heatmap from PR/merge/review activity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from github import Github, GithubException

from mira.platforms.github.auth import GitHubAppAuth

logger = logging.getLogger(__name__)

# Pause when the installation's hourly API budget drops below this, so a big
# repo's backfill yields rather than erroring out mid-run.
_RATE_LIMIT_FLOOR = 200

# How often (in processed items) to refresh the persisted progress blob and
# re-check the rate limit. Keeps settings writes and rate-limit pings bounded.
_PROGRESS_EVERY = 25

_STATUS_PREFIX = "contrib_backfill:"


def _status_key(owner: str, repo: str) -> str:
    return f"{_STATUS_PREFIX}{owner}/{repo}"


def get_backfill_status(db: Any, owner: str, repo: str) -> dict[str, Any]:
    """Read the persisted progress blob for a repo's backfill, or {}."""
    raw = db.get_setting(_status_key(owner, repo))
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def _set_status(db: Any, owner: str, repo: str, **fields: Any) -> None:
    """Merge ``fields`` into the repo's progress blob (survives restarts)."""
    data = get_backfill_status(db, owner, repo)
    data.update(fields)
    data["updated_at"] = time.time()
    db.set_setting(_status_key(owner, repo), json.dumps(data))


def _dt_to_epoch(value: Any) -> float:
    """PyGithub datetime (naive or aware UTC) → epoch seconds."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        if getattr(value, "tzinfo", None) is None:
            value = value.replace(tzinfo=UTC)
        return value.timestamp()
    except Exception:
        return 0.0


def _is_bot(user: Any) -> bool:
    try:
        login = user.login or ""
        return getattr(user, "type", "") == "Bot" or login.endswith("[bot]")
    except Exception:
        return False


def _maybe_wait_for_rate_limit(gh: Github) -> None:
    """Sleep until reset if the core API budget is nearly exhausted.

    Rate-limit lookups don't count against the budget, so this is safe to call
    periodically.
    """
    try:
        core = gh.get_rate_limit().core
    except GithubException:
        return
    if core.remaining >= _RATE_LIMIT_FLOOR:
        return
    wait = max(0.0, _dt_to_epoch(core.reset) - time.time()) + 5
    wait = min(wait, 3600)
    logger.warning(
        "GitHub rate limit low (%s remaining); sleeping %.0fs until reset",
        core.remaining,
        wait,
    )
    time.sleep(wait)


def _record_pr(db: Any, owner: str, repo: str, pr: Any, counts: dict[str, int]) -> None:
    user = pr.user
    login = user.login if user else ""
    if not login:
        return
    common = {
        "external_id": (user.id or 0),
        "avatar_url": (user.avatar_url or ""),
        "is_bot": _is_bot(user),
        "pr_number": pr.number,
        "title": pr.title or "",
        "additions": pr.additions or 0,
        "deletions": pr.deletions or 0,
        "changed_files": pr.changed_files or 0,
    }
    db.record_contribution_for_login(
        "github",
        login,
        owner,
        repo,
        "pr_opened",
        f"pr:{pr.number}",
        event_at=_dt_to_epoch(pr.created_at),
        **common,
    )
    counts["prs"] += 1
    if pr.merged_at:
        db.record_contribution_for_login(
            "github",
            login,
            owner,
            repo,
            "pr_merged",
            f"prm:{pr.number}",
            event_at=_dt_to_epoch(pr.merged_at),
            merged=True,
            **common,
        )
        counts["merges"] += 1


def _pr_state(pr: Any) -> str:
    if pr.merged_at:
        return "merged"
    if pr.state == "closed":
        return "closed"
    return "open"


def _record_pr_insights(db: Any, owner: str, repo: str, pr: Any) -> None:
    """Upsert the PR lifecycle row + currently-requested reviewers for the
    review-insights views. No extra API calls (all fields are on `pr`)."""
    db.upsert_pull_request(
        owner,
        repo,
        pr.number,
        author=(pr.user.login if pr.user else ""),
        title=pr.title or "",
        url=pr.html_url or "",
        state=_pr_state(pr),
        draft=bool(pr.draft),
        created_at=_dt_to_epoch(pr.created_at),
        updated_at=_dt_to_epoch(pr.updated_at),
        merged_at=_dt_to_epoch(pr.merged_at) if pr.merged_at else 0.0,
        closed_at=_dt_to_epoch(pr.closed_at) if pr.closed_at else 0.0,
    )
    # Currently-pending reviewers. No timeline call, so approximate the request
    # time with PR creation (webhooks record the precise time going forward).
    try:
        for ru in pr.requested_reviewers or []:
            if ru and ru.login:
                db.upsert_pr_reviewer(
                    owner, repo, pr.number, ru.login, requested_at=_dt_to_epoch(pr.created_at)
                )
    except Exception as exc:
        logger.debug("requested_reviewers backfill failed for PR #%s: %s", pr.number, exc)


def _record_reviews(db: Any, owner: str, repo: str, pr: Any, counts: dict[str, int]) -> None:
    from mira.platforms.github.review_signals import is_bare_approval

    author_login = pr.user.login if pr.user else ""
    try:
        reviews = pr.get_reviews()
    except GithubException as exc:
        logger.debug("Review fetch failed for PR #%s: %s", pr.number, exc)
        return
    # Inline comments, grouped by the review they belong to — for rubber-stamp
    # classification. One extra call per PR; tolerate failure.
    comments_by_review: dict[int, list[str]] = {}
    try:
        for c in pr.get_review_comments():
            rid = getattr(c, "pull_request_review_id", None)
            if rid is not None:
                comments_by_review.setdefault(rid, []).append(c.body or "")
    except GithubException as exc:
        logger.debug("Review-comment fetch failed for PR #%s: %s", pr.number, exc)
    for review in reviews:
        r_user = review.user
        r_login = r_user.login if r_user else ""
        # Skip self-reviews, authorless events, and bots (e.g. Mira).
        if not r_login or r_login == author_login or _is_bot(r_user):
            continue
        submitted = _dt_to_epoch(review.submitted_at)
        state = (review.state or "").lower()
        db.record_contribution_for_login(
            "github",
            r_login,
            owner,
            repo,
            "review",
            f"review:{review.id}",
            event_at=submitted,
            external_id=(r_user.id or 0),
            avatar_url=(r_user.avatar_url or ""),
            is_bot=False,
            pr_number=pr.number,
            title=pr.title or "",
        )
        counts["reviews"] += 1
        # Review-insights: responsiveness, first-review timing, rubber-stamp flag.
        bare = is_bare_approval(state, review.body or "", comments_by_review.get(review.id, []))
        db.upsert_pr_reviewer(
            owner,
            repo,
            pr.number,
            r_login,
            responded_at=submitted,
            state=state,
            bare_approval=int(bare),
        )
        db.set_pr_first_review(owner, repo, pr.number, submitted)


def _backfill_commits(
    db: Any,
    gh: Github,
    gh_repo: Any,
    owner: str,
    repo: str,
    since: float | None,
    counts: dict[str, int],
) -> None:
    kwargs: dict[str, Any] = {}
    if since:
        kwargs["since"] = datetime.fromtimestamp(since, tz=UTC)
    try:
        commits = gh_repo.get_commits(**kwargs)
    except GithubException as exc:
        logger.warning("Commit backfill skipped for %s/%s: %s", owner, repo, exc)
        return
    n = 0
    for commit in commits:
        n += 1
        if n % _PROGRESS_EVERY == 0:
            _maybe_wait_for_rate_limit(gh)
            _set_status(db, owner, repo, commits=counts["commits"])
        author = commit.author  # GitHub-linked NamedUser, or None for email-only
        login = author.login if author else ""
        if not login:
            continue
        inner = commit.commit
        event_at = _dt_to_epoch(inner.author.date if inner and inner.author else None)
        title = (inner.message.split("\n", 1)[0][:200]) if inner and inner.message else ""
        db.record_contribution_for_login(
            "github",
            login,
            owner,
            repo,
            "commit",
            f"commit:{commit.sha}",
            event_at=event_at,
            external_id=(author.id or 0),
            avatar_url=(author.avatar_url or ""),
            is_bot=_is_bot(author),
            title=title,
        )
        counts["commits"] += 1


def _backfill_sync(
    db: Any,
    token: str,
    owner: str,
    repo: str,
    since: float | None,
    include_commits: bool,
    counts: dict[str, int],
    progress_cb: Callable[[int, int], None] | None,
) -> None:
    """Blocking GitHub work — call via ``asyncio.to_thread``."""
    gh = Github(token)
    gh_repo = gh.get_repo(f"{owner}/{repo}")
    pulls = gh_repo.get_pulls(state="all", sort="created", direction="asc")
    total = pulls.totalCount
    done = 0
    for pr in pulls:
        done += 1
        if done % _PROGRESS_EVERY == 0:
            _maybe_wait_for_rate_limit(gh)
            _set_status(db, owner, repo, prs_done=done, total=total, **counts)
        # Incremental top-up: skip PRs untouched since the watermark.
        if since and _dt_to_epoch(pr.updated_at) < since:
            continue
        _record_pr(db, owner, repo, pr, counts)
        _record_pr_insights(db, owner, repo, pr)
        _record_reviews(db, owner, repo, pr, counts)
        if progress_cb:
            progress_cb(done, total)

    if include_commits:
        _backfill_commits(db, gh, gh_repo, owner, repo, since, counts)


async def backfill_repo_contributions(
    owner: str,
    repo: str,
    app_auth: GitHubAppAuth,
    *,
    installation_id: int = 0,
    since: float | None = None,
    include_commits: bool = True,
    progress_cb: Callable[[int, int], None] | None = None,
) -> dict[str, int]:
    """Backfill one repo's contributor activity. Returns event counts.

    Idempotent: safe to re-run or to run while live webhooks are firing.
    """
    from mira.dashboard.api import _app_db

    db = _app_db
    if not installation_id:
        rec = db.get_repo(owner, repo)
        installation_id = rec.installation_id if rec else 0
    token = await app_auth.get_installation_token(installation_id)

    counts = {"prs": 0, "merges": 0, "reviews": 0, "commits": 0}
    _set_status(db, owner, repo, status="running", error="", prs_done=0, total=0)
    try:
        await asyncio.to_thread(
            _backfill_sync, db, token, owner, repo, since, include_commits, counts, progress_cb
        )
    except Exception as exc:
        logger.exception("Backfill failed for %s/%s", owner, repo)
        _set_status(db, owner, repo, status="failed", error=str(exc), **counts)
        raise
    _set_status(db, owner, repo, status="complete", **counts)
    logger.info("Backfilled %s/%s: %s", owner, repo, counts)
    return counts


async def backfill_all_repos(
    app_auth: GitHubAppAuth,
    *,
    since: float | None = None,
    include_commits: bool = True,
) -> dict[str, int]:
    """Backfill every registered repo, one at a time (avoids rate-limit bursts)."""
    from mira.dashboard.api import _app_db

    totals = {"prs": 0, "merges": 0, "reviews": 0, "commits": 0, "repos": 0}
    for rec in _app_db.list_repos():
        try:
            counts = await backfill_repo_contributions(
                rec.owner,
                rec.repo,
                app_auth,
                installation_id=rec.installation_id,
                since=since,
                include_commits=include_commits,
            )
            for key in ("prs", "merges", "reviews", "commits"):
                totals[key] += counts.get(key, 0)
            totals["repos"] += 1
        except Exception:
            logger.exception("Backfill failed for %s/%s, continuing", rec.owner, rec.repo)
    return totals
