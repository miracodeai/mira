"""GitHub webhook: signature check, event dispatch, and GitHub-specific
handlers (PRs, comments, installations, push indexing)."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from datetime import datetime
from typing import Any

from fastapi import BackgroundTasks

from mira.config import load_config
from mira.index.indexer import _should_index
from mira.index.store import IndexStore
from mira.models import PRInfo
from mira.platforms.fetch import make_fetcher
from mira.platforms.github.auth import GitHubAppAuth
from mira.platforms.handlers import (
    _PAUSE_KEYWORDS,
    _REJECT_KEYWORDS,
    _RESUME_KEYWORDS,
    PAUSE_LABEL,
    _open_store,
    run_pr_command,
    run_pr_merged_learning,
    run_pr_review,
    run_thread_reply,
)
from mira.platforms.index_handlers import _get_app_db, run_incremental_index
from mira.platforms.mentions import (
    command_after_mention,
    has_mention,
    mention_names,
    strip_mentions,
)
from mira.providers import create_provider

logger = logging.getLogger(__name__)

_PR_ACTIONS = {"opened", "synchronize", "reopened"}
_PR_MERGE_ACTIONS = {"closed"}


def _parse_iso(ts: str) -> float:
    """GitHub ISO-8601 timestamp ('...Z') → epoch seconds. Falls back to now."""
    if not ts:
        return time.time()
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def _is_bot_user(user: dict[str, Any]) -> bool:
    """Whether a GitHub user object is a bot account."""
    login = user.get("login") or ""
    return user.get("type") == "Bot" or login.endswith("[bot]")


def _record_pr_contribution(payload: dict[str, Any], kind: str) -> None:
    """Record a pr_opened / pr_merged contribution from a pull_request payload.

    Idempotent (keyed on pr/prm:<number>) so a `synchronize` re-fire or a
    backfill that overlaps this webhook never double-counts. Best-effort —
    swallows errors so analytics never breaks the review path.
    """
    try:
        pr = payload.get("pull_request", {})
        user = pr.get("user") or {}
        login = user.get("login") or ""
        if not login:
            return
        owner = payload["repository"]["owner"]["login"]
        repo = payload["repository"]["name"]
        number = pr["number"]
        if kind == "pr_merged":
            external_key = f"prm:{number}"
            event_at = _parse_iso(pr.get("merged_at") or "")
            merged = True
        else:
            external_key = f"pr:{number}"
            event_at = _parse_iso(pr.get("created_at") or "")
            merged = False

        _get_app_db().record_contribution_for_login(
            "github",
            login,
            owner,
            repo,
            kind,
            external_key,
            event_at=event_at,
            external_id=int(user.get("id") or 0),
            avatar_url=user.get("avatar_url") or "",
            is_bot=_is_bot_user(user),
            pr_number=number,
            title=pr.get("title") or "",
            additions=int(pr.get("additions") or 0),
            deletions=int(pr.get("deletions") or 0),
            changed_files=int(pr.get("changed_files") or 0),
            merged=merged,
        )
    except Exception as exc:  # never let analytics break the webhook
        logger.debug("Failed to record %s contribution: %s", kind, exc)


def _pr_state(pr: dict[str, Any]) -> str:
    if pr.get("merged"):
        return "merged"
    if pr.get("state") == "closed":
        return "closed"
    return "open"


def _record_pr_lifecycle(payload: dict[str, Any]) -> None:
    """Upsert a PR's review-insights lifecycle row from any pull_request-shaped
    payload (pull_request, pull_request_review). Best-effort."""
    try:
        pr = payload.get("pull_request", {})
        repo = payload.get("repository", {})
        owner = repo.get("owner", {}).get("login", "")
        name = repo.get("name", "")
        number = pr.get("number")
        if not owner or not name or not number:
            return
        _get_app_db().upsert_pull_request(
            owner,
            name,
            number,
            author=(pr.get("user") or {}).get("login", ""),
            title=pr.get("title") or "",
            url=pr.get("html_url") or f"https://github.com/{owner}/{name}/pull/{number}",
            state=_pr_state(pr),
            draft=bool(pr.get("draft")),
            created_at=_parse_iso(pr.get("created_at") or ""),
            updated_at=_parse_iso(pr.get("updated_at") or "") or time.time(),
            merged_at=_parse_iso(pr["merged_at"]) if pr.get("merged_at") else 0.0,
            closed_at=_parse_iso(pr["closed_at"]) if pr.get("closed_at") else 0.0,
        )
    except Exception as exc:
        logger.debug("PR lifecycle record failed: %s", exc)


async def handle_pr_review_meta(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
) -> None:
    """Track PR review lifecycle for any pull_request event: keep the PR row
    current and record review_requested / review_request_removed."""
    try:
        _record_pr_lifecycle(payload)
        action = payload.get("action", "")
        if action not in ("review_requested", "review_request_removed"):
            return
        pr = payload.get("pull_request", {})
        repo = payload.get("repository", {})
        owner = repo.get("owner", {}).get("login", "")
        name = repo.get("name", "")
        number = pr.get("number")
        reviewer = (payload.get("requested_reviewer") or {}).get("login", "")
        if not reviewer or not number:
            return  # team requests (requested_team) have no single login — skip
        app_db = _get_app_db()
        if action == "review_requested":
            app_db.upsert_pr_reviewer(owner, name, number, reviewer, requested_at=time.time())
        else:
            app_db.remove_pr_reviewer(owner, name, number, reviewer)
    except Exception:
        logger.exception("Error handling PR review meta")


async def _classify_bare_approval(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    owner: str,
    name: str,
    number: int,
    review: dict[str, Any],
) -> int:
    """1 if this approval is a rubber-stamp (no substantive body or inline
    comments), else 0. Best-effort — any failure returns 0 and the next backfill
    reclassifies."""
    from mira.platforms.github.review_signals import is_bare_approval, is_substantive

    body = review.get("body") or ""
    # A substantive summary already disqualifies it — no API call needed.
    if is_substantive(body):
        return 0
    try:
        installation_id = payload.get("installation", {}).get("id", 0)
        token = await app_auth.get_installation_token(installation_id)
        provider = create_provider("github", token)
        pr_info = PRInfo(
            title="",
            description="",
            base_branch="",
            head_branch="",
            url=f"https://github.com/{owner}/{name}/pull/{number}",
            number=number,
            owner=owner,
            repo=name,
        )
        comments = await provider.get_review_inline_comments(pr_info, int(review.get("id") or 0))
    except Exception as exc:
        logger.debug("Could not fetch review comments for bare-approval check: %s", exc)
        comments = []
    return int(is_bare_approval("approved", body, comments))


async def handle_pull_request_review(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
) -> None:
    """Handle a submitted human review: record responsiveness + the review
    contribution, and stamp the PR's first-review time."""
    try:
        if payload.get("action") != "submitted":
            return
        review = payload.get("review", {})
        user = review.get("user") or {}
        reviewer = user.get("login", "")
        if not reviewer or _is_bot_user(user):
            return  # ignore Mira's own and other bot reviews
        pr = payload.get("pull_request", {})
        author = (pr.get("user") or {}).get("login", "")
        if reviewer == author:
            return  # a self-review isn't a review of someone else's work
        repo = payload.get("repository", {})
        owner = repo.get("owner", {}).get("login", "")
        name = repo.get("name", "")
        number = pr.get("number")
        if not number:
            return
        submitted = _parse_iso(review.get("submitted_at") or "")
        state = (review.get("state") or "").lower()

        # Classify rubber-stamp approvals (best-effort; backfill is the source of
        # truth). Only approvals can be bare, and a substantive body short-circuits
        # before we spend an API call fetching inline comments.
        bare = 0
        if state == "approved":
            bare = await _classify_bare_approval(payload, app_auth, owner, name, number, review)

        _record_pr_lifecycle(payload)
        app_db = _get_app_db()
        app_db.upsert_pr_reviewer(
            owner, name, number, reviewer, responded_at=submitted, state=state, bare_approval=bare
        )
        app_db.set_pr_first_review(owner, name, number, submitted)
        # Also count it as a review contribution for the heatmap.
        app_db.record_contribution_for_login(
            "github",
            reviewer,
            owner,
            name,
            "review",
            f"review:{review.get('id')}",
            event_at=submitted,
            external_id=int(user.get("id") or 0),
            avatar_url=user.get("avatar_url") or "",
            pr_number=number,
            title=pr.get("title") or "",
        )
    except Exception:
        logger.exception("Error handling pull_request_review")


def _record_push_commits(payload: dict[str, Any]) -> None:
    """Record per-commit contributions from a push to the default branch.

    Mirrors GitHub's contribution graph (default-branch commits). Keyed on
    commit:<sha> so the same commit arriving via push and later via backfill
    is counted once. Best-effort — never breaks the indexing path.
    """
    try:
        repository = payload.get("repository", {})
        default_branch = repository.get("default_branch", "main")
        if payload.get("ref", "") != f"refs/heads/{default_branch}":
            return
        owner = repository["owner"]["login"]
        repo = repository["name"]
        app_db = _get_app_db()
        for commit in payload.get("commits", []):
            sha = commit.get("id") or ""
            author = commit.get("author") or {}
            login = author.get("username") or ""
            if not sha or not login:
                # No GitHub login (only an email) → can't attribute; skip.
                continue
            ts = commit.get("timestamp") or ""
            try:
                event_at = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except ValueError:
                event_at = time.time()
            app_db.record_contribution_for_login(
                "github",
                login,
                owner,
                repo,
                "commit",
                f"commit:{sha}",
                event_at=event_at,
                title=(commit.get("message") or "").split("\n", 1)[0][:200],
            )
    except Exception as exc:
        logger.debug("Failed to record push commits: %s", exc)


def _schedule_contributor_backfill(
    app_auth: GitHubAppAuth, installation_id: int, repos: list[dict[str, Any]]
) -> None:
    """Kick off a one-time historical contributor backfill for newly added
    repos. Runs in the background, one repo at a time to spare the API budget."""
    import asyncio

    from mira.platforms.github.contributor_backfill import backfill_repo_contributions

    pairs: list[tuple[str, str]] = []
    for repo_info in repos:
        full_name = repo_info.get("full_name", "")
        if "/" in full_name:
            owner, repo = full_name.split("/", 1)
            pairs.append((owner, repo))
    if not pairs:
        return

    async def _run() -> None:
        for owner, repo in pairs:
            try:
                await backfill_repo_contributions(
                    owner, repo, app_auth, installation_id=installation_id
                )
            except Exception:
                logger.exception("Auto-backfill failed for %s/%s", owner, repo)

    asyncio.create_task(_run())


async def _handle_thread_freeform_reply(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
) -> None:
    """GitHub adapter for the free-form thread reply (see run_thread_reply)."""
    installation_id: int = payload.get("installation", {}).get("id", 0)
    try:
        token = await app_auth.get_installation_token(installation_id)
        provider = create_provider("github", token)

        comment = payload["comment"]
        comment_id: int = comment["id"]
        in_reply_to_id: int | None = comment.get("in_reply_to_id")

        owner = payload["repository"]["owner"]["login"]
        repo = payload["repository"]["name"]
        number = payload["pull_request"]["number"]
        pr_info = PRInfo(
            title="",
            description="",
            base_branch="",
            head_branch="",
            url=f"https://github.com/{owner}/{repo}/pull/{number}",
            number=number,
            owner=owner,
            repo=repo,
        )

        names = mention_names(bot_name, await app_auth.get_bot_identity())
        user_reply = strip_mentions(comment["body"], names)
        original_suggestion = (
            await provider.get_comment_body(pr_info, in_reply_to_id) if in_reply_to_id else ""
        )

        await run_thread_reply(
            provider,
            pr_info,
            user_reply,
            comment_id,
            original_suggestion=original_suggestion,
            comment_node_id=comment["node_id"],
            comment_path=comment.get("path", ""),
            comment_line=comment.get("original_line", 0) or comment.get("line", 0),
            actor=comment["user"]["login"],
            bot_name=bot_name,
        )
    except Exception:
        logger.exception("Error handling free-form thread reply")


def _verify_signature(payload_bytes: bytes, signature_header: str, secret: str) -> bool:
    """Verify the X-Hub-Signature-256 HMAC signature (timing-safe)."""
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature_header)


async def dispatch_github_event(
    event: str,
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
    background_tasks: BackgroundTasks,
) -> str:
    """Route a verified GitHub webhook to a handler. Returns a status string.

    Mirrors ``dispatch_gitlab_event`` — self-authored events are ignored to
    avoid review loops.
    """
    action = payload.get("action", "")

    # Track PR review lifecycle for every pull_request event (open/close/
    # ready/review_requested/...), independent of the review-trigger logic
    # below — so review-insights data stays current even for paused/ignored
    # PRs. Doesn't return; falls through to the review-trigger branches.
    if event == "pull_request" and payload.get("sender", {}).get("login", "") != f"{bot_name}[bot]":
        background_tasks.add_task(handle_pr_review_meta, payload, app_auth, bot_name)

    # A human submitted a review — capture responsiveness + the review event.
    if event == "pull_request_review" and action == "submitted":
        if payload.get("sender", {}).get("login", "") != f"{bot_name}[bot]":
            background_tasks.add_task(handle_pull_request_review, payload, app_auth, bot_name)
        return "processing"

    if (
        event == "pull_request"
        and action in _PR_MERGE_ACTIONS
        and payload.get("pull_request", {}).get("merged")
    ):
        if payload.get("sender", {}).get("login", "") == f"{bot_name}[bot]":
            return "ignored"
        background_tasks.add_task(handle_pr_merged, payload, app_auth, bot_name)
        return "processing"

    if event == "pull_request" and action in _PR_ACTIONS:
        sender = payload.get("sender", {}).get("login", "")
        if sender == f"{bot_name}[bot]":
            logger.debug("Ignoring pull_request event from self (%s)", sender)
            return "ignored"
        names = mention_names(bot_name, await app_auth.get_bot_identity())
        pr_body = payload.get("pull_request", {}).get("body", "") or ""
        if any(re.search(rf"@{re.escape(n)}[ \t]+ignore\b", pr_body, re.IGNORECASE) for n in names):
            logger.info("PR ignored via @%s ignore in description", bot_name)
            return "ignored"
        pr_labels = payload.get("pull_request", {}).get("labels", [])
        if any(lbl.get("name") == PAUSE_LABEL for lbl in pr_labels):
            logger.info("PR paused via %s label", PAUSE_LABEL)
            return "paused"
        background_tasks.add_task(handle_pull_request, payload, app_auth, bot_name)
        return "processing"

    if event == "issue_comment" and action == "created":
        comment_body = payload.get("comment", {}).get("body", "")
        comment_user = payload.get("comment", {}).get("user", {}).get("login", "")
        comment_user_type = payload.get("comment", {}).get("user", {}).get("type", "")
        if comment_user_type == "Bot" or comment_user == f"{bot_name}[bot]":
            logger.debug("Ignoring comment from bot (%s)", comment_user)
            return "ignored"
        names = mention_names(bot_name, await app_auth.get_bot_identity())
        if "pull_request" in payload.get("issue", {}) and has_mention(comment_body, names):
            cmd_word = command_after_mention(comment_body, names)
            if cmd_word in _PAUSE_KEYWORDS | _RESUME_KEYWORDS:
                background_tasks.add_task(
                    handle_pause_resume, payload, app_auth, bot_name, cmd_word
                )
                return "processing"
            background_tasks.add_task(handle_comment, payload, app_auth, bot_name)
            return "processing"

    if event == "pull_request_review_comment" and action == "created":
        rc_body = payload.get("comment", {}).get("body", "")
        rc_user = payload.get("comment", {}).get("user", {}).get("login", "")
        rc_user_type = payload.get("comment", {}).get("user", {}).get("type", "")
        if rc_user_type == "Bot" or rc_user == f"{bot_name}[bot]":
            logger.debug("Ignoring review comment from bot (%s)", rc_user)
            return "ignored"
        names = mention_names(bot_name, await app_auth.get_bot_identity())
        if has_mention(rc_body, names):
            background_tasks.add_task(handle_thread_reject, payload, app_auth, bot_name)
            return "processing"

    if event == "installation" and action == "created":
        background_tasks.add_task(handle_installation, payload, app_auth, bot_name)
        return "processing"
    if event == "installation" and action == "deleted":
        background_tasks.add_task(handle_installation_deleted, payload, app_auth, bot_name)
        return "processing"
    if event == "installation_repositories" and action == "added":
        background_tasks.add_task(handle_repos_added, payload, app_auth, bot_name)
        return "processing"
    if event == "installation_repositories" and action == "removed":
        background_tasks.add_task(handle_repos_removed, payload, app_auth, bot_name)
        return "processing"

    if event == "push":
        ref = payload.get("ref", "")
        default_branch = payload.get("repository", {}).get("default_branch", "main")
        if ref == f"refs/heads/{default_branch}":
            background_tasks.add_task(handle_push_index, payload, app_auth, bot_name)
            return "processing"

    return "ignored"


async def handle_pull_request(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
) -> None:
    """Handle a pull_request event by running a full review."""
    installation_id: int = payload.get("installation", {}).get("id", 0)
    pr_url = ""
    repo_full = ""
    try:
        token = await app_auth.get_installation_token(installation_id)

        pr = payload["pull_request"]
        owner = payload["repository"]["owner"]["login"]
        repo = payload["repository"]["name"]
        number = pr["number"]
        pr_url = f"https://github.com/{owner}/{repo}/pull/{number}"
        repo_full = f"{owner}/{repo}"

        provider = create_provider("github", token)
        is_private = bool(payload["repository"].get("private", False))

        # Record the authoring contribution before review so it lands even if
        # the review itself fails. Idempotent on every synchronize re-fire.
        _record_pr_contribution(payload, "pr_opened")

        await run_pr_review(provider, owner, repo, number, pr_url, is_private, bot_name)
    except Exception as exc:
        logger.exception("Error handling pull_request event")
        if pr_url:
            from mira.outbound_webhooks import REVIEW_FAILED, dispatch_event

            await dispatch_event(
                REVIEW_FAILED,
                {"repo": repo_full, "pr_url": pr_url, "error": str(exc)},
            )


async def handle_comment(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
) -> None:
    """Handle an issue_comment event mentioning the bot."""
    installation_id: int = payload.get("installation", {}).get("id", 0)
    try:
        token = await app_auth.get_installation_token(installation_id)

        comment_body: str = payload["comment"]["body"]
        comment_user: str = payload["comment"]["user"]["login"]
        names = mention_names(bot_name, await app_auth.get_bot_identity())
        question = strip_mentions(comment_body, names)

        owner = payload["repository"]["owner"]["login"]
        repo = payload["repository"]["name"]
        number = payload["issue"]["number"]
        pr_url = f"https://github.com/{owner}/{repo}/pull/{number}"

        provider = create_provider("github", token)
        await run_pr_command(
            provider, owner, repo, number, pr_url, question, comment_user, bot_name
        )
    except Exception:
        logger.exception("Error handling comment event")


async def handle_thread_reject(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
) -> None:
    """Handle a pull_request_review_comment that rejects a review thread."""
    installation_id: int = payload.get("installation", {}).get("id", 0)
    try:
        token = await app_auth.get_installation_token(installation_id)

        comment_body: str = payload["comment"]["body"]
        comment_node_id: str = payload["comment"]["node_id"]

        # The first word after the mention (either the configured name or the
        # bot's real login), e.g. "@mira reject" → "reject".
        names = mention_names(bot_name, await app_auth.get_bot_identity())
        command = command_after_mention(comment_body, names)

        # No explicit reject/dismiss keyword → fall through to the
        # free-form LLM reply path. The bot reads the human's message,
        # classifies intent, and either acknowledges + resolves (if the
        # human refuted) or just replies (questions, agreements).
        if command not in _REJECT_KEYWORDS:
            await _handle_thread_freeform_reply(payload, app_auth, bot_name)
            return

        owner = payload["repository"]["owner"]["login"]
        repo = payload["repository"]["name"]
        number = payload["pull_request"]["number"]

        provider = create_provider("github", token)
        from mira.models import PRInfo as _PRInfo

        _pr_info_for_lookup = _PRInfo(
            title="",
            description="",
            base_branch="",
            head_branch="",
            url=f"https://github.com/{owner}/{repo}/pull/{number}",
            number=number,
            owner=owner,
            repo=repo,
        )
        thread_id = await provider.get_thread_id_for_comment(
            comment_node_id,
            _pr_info_for_lookup,
        )
        if not thread_id:
            logger.info(
                "Thread not found or already resolved for comment %s on PR %s/%s#%d",
                comment_node_id,
                owner,
                repo,
                number,
            )
            return

        from mira.models import PRInfo

        pr_info = PRInfo(
            title="",
            description="",
            base_branch="",
            head_branch="",
            url=f"https://github.com/{owner}/{repo}/pull/{number}",
            number=number,
            owner=owner,
            repo=repo,
        )
        try:
            resolved = await provider.resolve_threads(pr_info, [thread_id])
        except Exception as resolve_err:
            logger.warning(
                "Failed to resolve thread %s on PR %s/%s#%d: %s",
                thread_id,
                owner,
                repo,
                number,
                resolve_err,
            )
            try:
                await provider.post_comment(
                    pr_info,
                    "Sorry, I couldn't dismiss this suggestion. "
                    "Please try again or resolve the thread manually.",
                )
            except Exception:
                logger.warning(
                    "Failed to post reject failure reply on PR %s/%s#%d", owner, repo, number
                )
            return

        logger.info(
            "Reject command '%s': resolved %d thread(s) on PR %s/%s#%d",
            command,
            resolved,
            owner,
            repo,
            number,
        )

        # Record feedback for learning
        try:
            store = _open_store(owner, repo)
            try:
                store.record_feedback(
                    pr_number=number,
                    pr_url=f"https://github.com/{owner}/{repo}/pull/{number}",
                    comment_path=payload["comment"].get("path", ""),
                    comment_line=payload["comment"].get("original_line", 0)
                    or payload["comment"].get("line", 0),
                    comment_category="",
                    comment_severity="",
                    comment_title="",
                    signal="rejected",
                    actor=payload["comment"]["user"]["login"],
                )
            finally:
                store.close()
        except Exception as fb_err:
            logger.debug("Failed to record feedback: %s", fb_err)

    except Exception:
        logger.exception("Error handling thread reject event")


async def handle_pr_merged(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
) -> None:
    """Learn from a merged PR by extracting accept/reject + human-review signals."""
    installation_id: int = payload.get("installation", {}).get("id", 0)
    pr_url = ""
    try:
        pr = payload.get("pull_request", {})
        if not pr.get("merged"):
            return

        owner = payload["repository"]["owner"]["login"]
        repo = payload["repository"]["name"]
        number = pr["number"]
        pr_url = f"https://github.com/{owner}/{repo}/pull/{number}"

        # Record the merge contribution (idempotent on prm:<number>).
        _record_pr_contribution(payload, "pr_merged")

        token = await app_auth.get_installation_token(installation_id)
        provider = create_provider("github", token)

        from mira.models import PRInfo

        pr_info = PRInfo(
            title=pr.get("title", ""),
            description=pr.get("body") or "",
            base_branch=pr["base"]["ref"],
            head_branch=pr["head"]["ref"],
            url=pr_url,
            number=number,
            owner=owner,
            repo=repo,
            head_sha=pr["head"].get("sha") or "",
            author=(pr.get("user") or {}).get("login", ""),
        )
        merged_by = (pr.get("merged_by") or {}).get("login", "")
        await run_pr_merged_learning(provider, pr_info, bot_name, merged_by)
    except Exception:
        logger.exception("Error handling pr_merged event on %s", pr_url)


async def handle_pause_resume(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
    command: str,
) -> None:
    """Handle a pause or resume command from an issue comment."""
    installation_id: int = payload.get("installation", {}).get("id", 0)
    try:
        token = await app_auth.get_installation_token(installation_id)

        owner = payload["repository"]["owner"]["login"]
        repo = payload["repository"]["name"]
        number = payload["issue"]["number"]

        from mira.models import PRInfo

        pr_info = PRInfo(
            title="",
            description="",
            base_branch="",
            head_branch="",
            url=f"https://github.com/{owner}/{repo}/pull/{number}",
            number=number,
            owner=owner,
            repo=repo,
        )

        provider = create_provider("github", token)

        if command in _PAUSE_KEYWORDS:
            await provider.add_label(pr_info, PAUSE_LABEL)
            await provider.post_comment(
                pr_info,
                f"Automatic reviews paused. You can still request a manual review "
                f"by commenting `@{bot_name} review`.",
            )
            logger.info("Paused automatic reviews on PR %s/%s#%d", owner, repo, number)
        elif command in _RESUME_KEYWORDS:
            await provider.remove_label(pr_info, PAUSE_LABEL)
            await provider.post_comment(
                pr_info,
                "Automatic reviews resumed.",
            )
            logger.info("Resumed automatic reviews on PR %s/%s#%d", owner, repo, number)

    except Exception:
        logger.exception("Error handling pause/resume event")


async def _count_files_for_repos(
    app_auth: GitHubAppAuth,
    installation_id: int,
    repos: list[dict],
) -> None:
    """Count indexable files per repo via GitHub API and cache to DB."""
    try:
        token = await app_auth.get_installation_token(installation_id)
    except Exception as exc:
        logger.warning(
            "Cannot count files for installation %s — token fetch failed (%s). "
            "This usually means MIRA_GITHUB_APP_ID/MIRA_GITHUB_PRIVATE_KEY don't "
            "match the installed App, or the installation has been revoked.",
            installation_id,
            exc,
        )
        return

    app_db = _get_app_db()
    exclude_patterns = load_config().filter.exclude_patterns
    fetcher = make_fetcher("github", token)
    for repo_info in repos:
        full_name = repo_info.get("full_name", "")
        if "/" not in full_name:
            continue
        owner, repo = full_name.split("/", 1)
        try:
            # Resolve the repo's actual default branch — hardcoding "main"
            # 404s on repos whose default is "master" (or anything else).
            branch = await fetcher.default_branch(owner, repo)
            tree_paths = await fetcher.repo_tree(owner, repo, branch)
            indexable = [p for p in tree_paths if _should_index(p, exclude_patterns)]
            app_db.set_repo_file_count(owner, repo, len(indexable))
            logger.info("Counted %d indexable files in %s", len(indexable), full_name)
        except Exception as exc:
            logger.warning("Failed to count files for %s: %s", full_name, exc)


async def handle_installation(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
) -> None:
    """Handle installation.created — register all repos (no indexing until user confirms)."""
    installation_id: int = payload.get("installation", {}).get("id", 0)
    repos = payload.get("repositories", [])

    if not repos:
        try:
            repos_from_api = await app_auth.list_installation_repos(installation_id)
            repos = [
                {"full_name": r.get("full_name", ""), "private": r.get("private", False)}
                for r in repos_from_api
            ]
        except Exception as exc:
            logger.warning("Failed to list repos via API: %s", exc)

    logger.info("handle_installation: registering %d repos", len(repos))

    try:
        app_db = _get_app_db()
        for repo_info in repos:
            full_name = repo_info.get("full_name", "")
            if "/" not in full_name:
                continue
            owner, repo = full_name.split("/", 1)
            app_db.register_repo(owner, repo, installation_id)
            if "private" in repo_info:
                app_db.set_repo_visibility(owner, repo, bool(repo_info["private"]))
            logger.info("Registered repo %s (pending indexing)", full_name)

        # Count files in background — no LLM, just GitHub API
        import asyncio

        asyncio.create_task(_count_files_for_repos(app_auth, installation_id, repos))
        _schedule_contributor_backfill(app_auth, installation_id, repos)

        # Notify connected clients
        from mira.dashboard.events import bus

        bus.emit(
            "install_created",
            {
                "installation_id": installation_id,
                "repos": [r.get("full_name", "") for r in repos],
            },
        )

    except Exception:
        logger.exception("Error registering repos from installation")


async def handle_installation_deleted(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
) -> None:
    """Handle installation.deleted — queue pending uninstall (keep data until user decides)."""
    installation_id: int = payload.get("installation", {}).get("id", 0)
    account = payload.get("installation", {}).get("account", {})
    owner = str(account.get("login", "unknown"))
    logger.info(
        "handle_installation_deleted: queuing uninstall for installation %d (%s)",
        installation_id,
        owner,
    )

    try:
        app_db = _get_app_db()
        app_db.add_pending_uninstall(installation_id, owner)

        from mira.dashboard.events import bus

        bus.emit("uninstall_pending", {"installation_id": installation_id, "owner": owner})
    except Exception:
        logger.exception("Error handling installation.deleted")


async def handle_repos_removed(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
) -> None:
    """Handle installation_repositories.removed — remove specific repos."""
    repos = payload.get("repositories_removed", [])
    logger.info("handle_repos_removed: removing %d repos", len(repos))

    try:
        app_db = _get_app_db()
        for repo_info in repos:
            full_name = repo_info.get("full_name", "")
            if "/" not in full_name:
                continue
            owner, repo = full_name.split("/", 1)
            app_db.delete_repo(owner, repo)
            logger.info("Removed repo %s", full_name)
    except Exception:
        logger.exception("Error handling repos_removed")


async def handle_repos_added(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
) -> None:
    """Handle installation_repositories.added — register newly added repos."""
    installation_id: int = payload.get("installation", {}).get("id", 0)
    repos = payload.get("repositories_added", [])

    logger.info("handle_repos_added: registering %d repos", len(repos))

    try:
        app_db = _get_app_db()
        for repo_info in repos:
            full_name = repo_info.get("full_name", "")
            if "/" not in full_name:
                continue
            owner, repo = full_name.split("/", 1)
            app_db.register_repo(owner, repo, installation_id)
            if "private" in repo_info:
                app_db.set_repo_visibility(owner, repo, bool(repo_info["private"]))
            logger.info("Registered repo %s (pending indexing)", full_name)

        import asyncio

        asyncio.create_task(_count_files_for_repos(app_auth, installation_id, repos))
        _schedule_contributor_backfill(app_auth, installation_id, repos)

        from mira.dashboard.events import bus

        bus.emit(
            "repos_added",
            {
                "installation_id": installation_id,
                "repos": [r.get("full_name", "") for r in repos],
            },
        )
    except Exception:
        logger.exception("Error registering repos_added")


async def handle_push_index(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
) -> None:
    """Handle push to default branch — incremental index of changed files.

    If the push touches more than _INCREMENTAL_FILE_CAP files (e.g. a large
    rebase), queues a full re-index instead of inline incremental updates.
    """
    installation_id: int = payload.get("installation", {}).get("id", 0)

    try:
        token = await app_auth.get_installation_token(installation_id)

        owner = payload["repository"]["owner"]["login"]
        repo_name = payload["repository"]["name"]
        default_branch = payload.get("repository", {}).get("default_branch", "main")

        # Record commit contributions first — independent of index status, so
        # contributor analytics covers repos that aren't indexed for review.
        _record_push_commits(payload)

        # Check repo status — only re-index repos that are already indexed
        app_db = _get_app_db()
        repo_record = app_db.get_repo(owner, repo_name)
        if not repo_record or repo_record.status not in ("ready", "indexing"):
            logger.debug(
                "Push to %s/%s skipped — repo status is %s",
                owner,
                repo_name,
                repo_record.status if repo_record else "not registered",
            )
            return

        # Extract changed and removed paths from commits
        changed_paths: set[str] = set()
        removed_paths: set[str] = set()
        for commit in payload.get("commits", []):
            changed_paths.update(commit.get("added", []))
            changed_paths.update(commit.get("modified", []))
            removed_paths.update(commit.get("removed", []))

        # Files that were removed shouldn't be re-indexed
        changed_paths -= removed_paths

        if not changed_paths and not removed_paths:
            logger.debug("Push to %s/%s had no file changes", owner, repo_name)
            return

        await run_incremental_index(
            owner,
            repo_name,
            make_fetcher("github", token),
            list(changed_paths),
            list(removed_paths),
            default_branch,
        )
    except Exception:
        logger.exception("Error handling push indexing")


def _index_is_populated(owner: str, repo: str) -> bool:
    """Check whether a repo has a non-empty index."""
    index_dir = os.environ.get("MIRA_INDEX_DIR", "/data/indexes")
    db_path = os.path.join(index_dir, owner, f"{repo}.db")
    if not os.path.isfile(db_path):
        return False
    # DB file exists but might be empty (created then interrupted)
    try:
        store = IndexStore(db_path)
        has_files = len(store.all_paths()) > 0
        store.close()
        return has_files
    except Exception:
        return False


def _reconcile_repo_statuses() -> None:
    """Heal any 'indexing' rows left over from a crashed/restarted indexing job.

    If the actual IndexStore has files for a repo whose row says 'indexing',
    promote the row to 'ready' with the real file count. If the store is
    empty, demote to 'pending' so the user can retry from the dashboard.
    """
    app_db = _get_app_db()
    for r in app_db.list_repos():
        if r.status != "indexing":
            continue
        try:
            store = IndexStore.open(r.owner, r.repo, platform=r.platform)
            count = len(store.all_paths())
            store.close()
        except Exception:
            count = 0
        if count > 0:
            app_db.set_repo_status(
                r.owner, r.repo, "ready", files_indexed=count, platform=r.platform
            )
            logger.info("Reconciled %s/%s: indexing → ready (%d files)", r.owner, r.repo, count)
        else:
            app_db.set_repo_status(r.owner, r.repo, "pending", platform=r.platform)
            logger.info("Reconciled %s/%s: indexing → pending (no files)", r.owner, r.repo)


async def backfill_missing_indexes(
    app_auth: GitHubAppAuth,
) -> None:
    """Register all repos from GitHub App installations.

    Called at server startup. Only registers repos — does not start indexing.
    Indexing is user-initiated via the setup page.
    """
    try:
        # Reconcile any stale 'indexing' rows left over from a previous run
        # that crashed or was restarted mid-flight.
        _reconcile_repo_statuses()

        installations = await app_auth.list_installations()
        logger.info("Startup: found %d installation(s)", len(installations))

        app_db = _get_app_db()
        registered = 0

        for inst in installations:
            raw_id = inst.get("id", 0)
            installation_id = int(raw_id) if isinstance(raw_id, (int, str)) else 0
            if not installation_id:
                continue

            try:
                repos = await app_auth.list_installation_repos(installation_id)
            except Exception as exc:
                logger.warning("Failed to list repos for installation %d: %s", installation_id, exc)
                continue

            for repo_info in repos:
                full_name = str(repo_info.get("full_name", ""))
                if "/" not in full_name:
                    continue
                owner, repo = full_name.split("/", 1)
                app_db.register_repo(owner, repo, installation_id)
                # Refresh visibility every startup so the blast-radius privacy
                # filter has current data — backfills existing rows after upgrade.
                app_db.set_repo_visibility(owner, repo, bool(repo_info.get("private", False)))
                registered += 1

            # Count files in background
            import asyncio

            asyncio.create_task(_count_files_for_repos(app_auth, installation_id, repos))

        logger.info("Startup: registered %d repo(s)", registered)
    except Exception:
        logger.exception("Error during startup repo registration")
