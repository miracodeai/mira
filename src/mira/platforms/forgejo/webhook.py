"""Forgejo webhook handling: HMAC-SHA256 signature verification, event
normalization, handlers.

Forgejo sends GitHub-shaped payloads with ``X-Forgejo-Event`` (the event name
matches GitHub's event types: ``pull_request``, ``push``, ``issue_comment``).
Signature verification uses HMAC-SHA256 of the request body in
``X-Forgejo-Signature`` (matching Forgejo/Gitea webhook behavior).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from typing import Any

import httpx

from mira.config import load_config
from mira.platforms import profiles
from mira.platforms.auth import PlatformAuth
from mira.platforms.fetch import make_fetcher
from mira.platforms.mentions import (
    author_is_filtered,
    command_after_mention,
    has_mention,
    mention_names,
    strip_mentions,
)
from mira.providers import create_provider

logger = logging.getLogger(__name__)


async def list_forgejo_repos(token: str, base_url: str) -> list[dict]:
    """Every repo the token can access (paginated via page/limit)."""
    out: list[dict] = []
    page = 1
    limit = 50
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            url = f"{base_url.rstrip('/')}/user/repos?page={page}&limit={limit}"
            resp = await client.get(url, headers={"Authorization": f"token {token}"})
            if resp.status_code != 200:
                logger.warning("Forgejo repo list failed: %d %s", resp.status_code, resp.text[:200])
                break
            repos = resp.json()
            if not repos:
                break
            out.extend(repos)
            if len(repos) < limit:
                break
            page += 1
    return out


async def backfill_forgejo_repos(auth: PlatformAuth) -> int:
    """Register every accessible Forgejo repo so they show in the dashboard
    ready to index — without waiting for a webhook. Returns the count."""
    from mira.config import load_config
    from mira.platforms.index_handlers import _get_app_db

    token = await auth.get_token()
    base_url = profiles.resolve("forgejo")["api_url"]
    repos = await list_forgejo_repos(token, base_url)
    db = _get_app_db()
    exclude_patterns = load_config().filter.exclude_patterns
    fetcher = make_fetcher("forgejo", token)
    n = 0
    for r in repos:
        full_name = r.get("full_name", "")
        if "/" not in full_name:
            continue
        owner, repo = full_name.split("/", 1)
        db.register_repo(owner, repo, platform="forgejo")
        db.set_repo_visibility(owner, repo, r.get("private", True), platform="forgejo")
        try:
            from mira.index.indexer import _should_index

            branch = await fetcher.default_branch(owner, repo)
            tree_paths = await fetcher.repo_tree(owner, repo, branch)
            indexable = [p for p in tree_paths if _should_index(p, exclude_patterns)]
            db.set_repo_file_count(owner, repo, len(indexable), platform="forgejo")
            logger.info("Counted %d indexable files in %s", len(indexable), full_name)
        except Exception as exc:
            logger.warning("Failed to count files for %s: %s", full_name, exc)
        n += 1
    logger.info("Forgejo: discovered + registered %d accessible repo(s)", n)
    return n


def verify_forgejo_signature(signature: str, body: bytes, secret: str) -> bool:
    """Forgejo signs the request body with HMAC-SHA256 and sends the hex
    digest in ``X-Forgejo-Signature``. The secret is never sent in plaintext."""
    expected = hmac.new((secret or "").encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature or "", expected)


def _split_repo_path(full_name: str) -> tuple[str, str]:
    """'owner/repo' → ('owner', 'repo'). Forgejo full_name is always two parts."""
    parts = full_name.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid Forgejo repo full_name: {full_name!r}")
    return parts[0], parts[1]


async def handle_forgejo_pr(payload: dict[str, Any], auth: PlatformAuth, bot_name: str) -> None:
    """Review a pull request (open / reopen / new commits)."""
    from mira.platforms.handlers import PAUSE_LABEL, run_pr_review
    from mira.platforms.index_handlers import _get_app_db

    action = payload.get("action", "")
    if action not in ("opened", "synchronized", "reopened"):
        return

    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})
    full_name = repo.get("full_name", "")

    try:
        owner, repo_name = _split_repo_path(full_name)
    except ValueError:
        return

    number = pr.get("number")
    if number is None:
        return
    pr_url = pr.get("html_url", "")
    is_private = repo.get("private", True)

    # Same opt-outs as GitHub: `@mira ignore` in the description and the
    # mira-paused label both skip auto-review.
    names = mention_names(bot_name, await auth.get_bot_identity())
    description = pr.get("body", "") or ""
    if any(re.search(rf"@{re.escape(n)}[ \t]+ignore\b", description, re.IGNORECASE) for n in names):
        logger.info(
            "PR %s/%s#%d ignored via @%s ignore in description", owner, repo_name, number, bot_name
        )
        return
    labels = pr.get("labels") or []
    if any((lbl.get("name") or lbl.get("title")) == PAUSE_LABEL for lbl in labels):
        logger.info("PR %s/%s#%d paused via %s label", owner, repo_name, number, PAUSE_LABEL)
        return

    try:
        _get_app_db().register_repo(owner, repo_name, platform="forgejo")
        token = await auth.get_token()
        provider = create_provider("forgejo", token)
        await run_pr_review(
            provider, owner, repo_name, number, pr_url, is_private, bot_name, platform="forgejo"
        )
    except Exception:
        logger.exception(
            "Error handling Forgejo pull_request event for %s/%s#%d", owner, repo_name, number
        )


async def handle_forgejo_push(payload: dict[str, Any], auth: PlatformAuth, bot_name: str) -> None:
    """Incrementally index a push to the default branch."""
    from mira.platforms.index_handlers import _get_app_db, run_incremental_index

    repo_data = payload.get("repository", {})
    full_name = repo_data.get("full_name", "")
    default_branch = repo_data.get("default_branch", "main")
    ref = payload.get("ref", "")

    try:
        owner, repo_name = _split_repo_path(full_name)
    except ValueError:
        return

    if not ref.startswith("refs/heads/"):
        return

    branch = ref[len("refs/heads/") :]
    if branch != default_branch:
        logger.debug(
            "Forgejo push to %s/%s on %s (not default %s), skipping index",
            owner,
            repo_name,
            branch,
            default_branch,
        )
        return

    repo_record = _get_app_db().get_repo(owner, repo_name, platform="forgejo")
    if not repo_record or repo_record.status not in ("ready", "indexing"):
        logger.debug("Forgejo push to %s/%s skipped — not indexed", owner, repo_name)
        return

    changed: set[str] = set()
    removed: set[str] = set()
    for commit in payload.get("commits", []):
        changed.update(commit.get("added", []))
        changed.update(commit.get("modified", []))
        removed.update(commit.get("removed", []))
    changed -= removed
    if not changed and not removed:
        return

    try:
        token = await auth.get_token()
        await run_incremental_index(
            owner,
            repo_name,
            make_fetcher("forgejo", token),
            list(changed),
            list(removed),
            default_branch,
            platform="forgejo",
        )
    except Exception:
        logger.exception("Error handling Forgejo push for %s/%s", owner, repo_name)


async def handle_forgejo_note(payload: dict[str, Any], auth: PlatformAuth, bot_name: str) -> None:
    """An @-mention in a PR comment: command, pause/resume, or thread reject."""
    from mira.platforms.handlers import (
        _PAUSE_KEYWORDS,
        _REJECT_KEYWORDS,
        _RESUME_KEYWORDS,
        PAUSE_LABEL,
        _open_store,
        run_pr_command,
        run_thread_reply,
    )

    action = payload.get("action", "")
    if action != "created":
        return

    if payload.get("is_pull") is not True:
        return

    comment = payload.get("comment", {})
    comment_body = comment.get("body", "") or ""
    repo = payload.get("repository", {})
    full_name = repo.get("full_name", "")
    issue = payload.get("issue", {})
    number = issue.get("number")

    if number is None:
        return

    try:
        owner, repo_name = _split_repo_path(full_name)
    except ValueError:
        return

    # Build PR URL from repo html_url (like GitHub does)
    pr_url = f"{repo.get('html_url', '')}/pulls/{number}"

    # Actor is the comment author
    commenter = comment.get("user", {})
    actor = commenter.get("username", "") or commenter.get("login", "")

    try:
        token = await auth.get_token()
        provider = create_provider("forgejo", token)
        pr_info = await provider.get_pr_info(pr_url)

        # Accept a mention of either the configured name or the real bot user.
        names = mention_names(bot_name, await auth.get_bot_identity())
        question = strip_mentions(comment_body, names)
        first_word = question.split()[0].lower() if question.split() else ""

        if first_word in _PAUSE_KEYWORDS:
            await provider.add_label(pr_info, PAUSE_LABEL)
            await provider.post_comment(
                pr_info,
                f"Automatic reviews paused. Request a manual review with `@{bot_name} review`.",
            )
            return
        if first_word in _RESUME_KEYWORDS:
            await provider.remove_label(pr_info, PAUSE_LABEL)
            await provider.post_comment(pr_info, "Automatic reviews resumed.")
            return

        # Forgejo inline review comments carry path/line on the comment itself
        comment_path = comment.get("path", "")
        comment_line = comment.get("line", 0) or 0

        # Forgejo has no discussion threading, so thread_id is the comment id
        thread_id = str(comment.get("id", ""))

        # Explicit reject on an inline (diff) comment → record feedback.
        if first_word in _REJECT_KEYWORDS and comment_path:
            try:
                store = _open_store(owner, repo_name, "forgejo")
                store.record_feedback(
                    pr_number=number,
                    pr_url=pr_url,
                    comment_path=comment_path,
                    comment_line=comment_line,
                    comment_category="",
                    comment_severity="",
                    comment_title="",
                    signal="rejected",
                    actor=actor,
                )
            except Exception as exc:
                logger.debug("Failed to record Forgejo reject feedback: %s", exc)
            finally:
                if "store" in locals():
                    store.close()
            return

        # Free-form @-mention on an inline comment → LLM intent classification.
        if comment_path:
            original = await provider.get_comment_body(pr_info, int(thread_id))
            await run_thread_reply(
                provider,
                pr_info,
                question,
                comment.get("id"),
                original_suggestion=original,
                thread_id=thread_id,
                comment_path=comment_path,
                comment_line=comment_line,
                actor=actor,
                bot_name=bot_name,
                platform="forgejo",
            )
            return

        # General PR comment → review / help / Q&A.
        await run_pr_command(
            provider,
            owner,
            repo_name,
            number,
            pr_url,
            question,
            actor,
            bot_name,
            platform="forgejo",
        )
    except Exception:
        logger.exception(
            "Error handling Forgejo issue_comment on %s/%s#%d", owner, repo_name, number
        )


async def dispatch_forgejo_event(
    event: str,
    payload: dict[str, Any],
    auth: PlatformAuth,
    bot_name: str,
    background_tasks: Any,
) -> str:
    """Route a verified Forgejo webhook to a handler. Returns a status string.

    Self-authored events (the bot's own comments) are ignored to avoid loops.
    """
    sender = payload.get("sender", {})
    actor = sender.get("login", "")
    bot_identity = await auth.get_bot_identity()
    if actor and bot_identity and actor == bot_identity:
        return "ignored"
    cfg = load_config()

    if event == "pull_request":
        action = payload.get("action", "")
        if action in ("opened", "synchronized", "reopened"):
            full_name = payload.get("repository", {}).get("full_name", "")
            number = payload.get("pull_request", {}).get("number", 0)
            try:
                owner, repo_name = _split_repo_path(full_name)
            except ValueError:
                owner, repo_name = "?", "?"

            if author_is_filtered(actor, cfg.filter.allowed_authors, cfg.filter.blocked_authors):
                logger.debug(
                    "PR %s/%s#%s skipped — author %s filtered by author filter",
                    owner,
                    repo_name,
                    number,
                    actor,
                )
                return "ignored"
            background_tasks.add_task(handle_forgejo_pr, payload, auth, bot_name)
            return "processing"
        # Ignore other PR actions (closed, edited, labeled, merged, etc.)
        return "ignored"

    if event == "push":
        if author_is_filtered(actor, cfg.filter.allowed_authors, cfg.filter.blocked_authors):
            logger.debug("push ignored — author %s filtered", actor)
            return "ignored"
        background_tasks.add_task(handle_forgejo_push, payload, auth, bot_name)
        return "processing"

    if event == "issue_comment":
        if payload.get("is_pull") is not True:
            return "ignored"
        names = mention_names(bot_name, bot_identity)
        comment_body = payload.get("comment", {}).get("body", "") or ""
        if has_mention(comment_body, names):
            cmd_word = command_after_mention(comment_body, names)
            if cmd_word == "review":
                pass  # review command bypasses author filter
            elif author_is_filtered(actor, cfg.filter.allowed_authors, cfg.filter.blocked_authors):
                logger.debug("issue_comment skipped — author %s filtered", actor)
                return "ignored"
            background_tasks.add_task(handle_forgejo_note, payload, auth, bot_name)
            return "processing"
        return "ignored"

    return "ignored"
