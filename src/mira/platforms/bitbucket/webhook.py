"""Bitbucket Cloud webhook handling: HMAC-SHA256 signature verification, event
normalization, handlers.

Bitbucket names the event in ``X-Event-Key`` (``pullrequest:created``,
``pullrequest:updated``, ``pullrequest:fulfilled``,
``pullrequest:comment_created``, ``repo:push``). When the webhook has a secret,
the body's HMAC-SHA256 arrives in ``X-Hub-Signature`` as ``sha256=<hex>``.

Two Bitbucket quirks shape the handlers:

- ``pullrequest:updated`` fires for title/description edits too, so a review
  only runs when the source commit changed (tracked in ``bitbucket.state``);
- ``repo:push`` carries no file lists, so the changed paths for incremental
  indexing come from the diffstat API.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from typing import Any
from urllib.parse import quote

import httpx

from mira.config import load_config
from mira.platforms import profiles
from mira.platforms.auth import PlatformAuth
from mira.platforms.bitbucket import state
from mira.platforms.bitbucket.auth import (
    bitbucket_auth_headers,
    normalize_mentions,
    user_handle,
    user_identifiers,
)
from mira.platforms.command_policy import is_command_allowed
from mira.platforms.fetch import make_fetcher
from mira.platforms.mentions import (
    author_is_filtered,
    has_mention,
    mention_names,
    strip_mentions,
)
from mira.providers import create_provider

logger = logging.getLogger(__name__)

PR_REVIEW_EVENTS = ("pullrequest:created", "pullrequest:updated")


def _api() -> str:
    return profiles.resolve("bitbucket")["api_url"] or "https://api.bitbucket.org/2.0"


async def list_bitbucket_repos(token: str, base_url: str, workspace: str = "") -> list[dict]:
    """Every repo the credential can access, following ``next`` links.

    With ``workspace`` set (``MIRA_BITBUCKET_WORKSPACE``) only that workspace is
    listed, which is also what workspace/project access tokens can read;
    otherwise every repo the credential's user is a member of.
    """
    base = base_url.rstrip("/")
    url: str | None = (
        f"{base}/repositories/{quote(workspace, safe='')}?pagelen=100"
        if workspace
        else f"{base}/repositories?role=member&pagelen=100"
    )
    out: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        while url:
            resp = await client.get(url, headers=bitbucket_auth_headers(token))
            if resp.status_code != 200:
                logger.warning(
                    "Bitbucket repo list failed: %d %s", resp.status_code, resp.text[:200]
                )
                break
            data = resp.json()
            out.extend(data.get("values") or [])
            url = data.get("next")
    return out


async def backfill_bitbucket_repos(auth: PlatformAuth) -> int:
    """Register every accessible Bitbucket repo so they show in the dashboard
    ready to index — without waiting for a webhook. Returns the count."""
    from mira.platforms.index_handlers import _get_app_db

    token = await auth.get_token()
    workspace = os.environ.get("MIRA_BITBUCKET_WORKSPACE", "")
    repos = await list_bitbucket_repos(token, _api(), workspace)
    db = _get_app_db()
    n = 0
    for r in repos:
        try:
            owner, repo = _split_repo_path(r.get("full_name", ""))
        except ValueError:
            continue
        db.register_repo(owner, repo, platform="bitbucket")
        db.set_repo_visibility(owner, repo, r.get("is_private", True), platform="bitbucket")
        n += 1
    logger.info("Bitbucket: discovered + registered %d accessible repo(s)", n)
    return n


def verify_bitbucket_signature(signature: str, body: bytes, secret: str) -> bool:
    """Check ``X-Hub-Signature: sha256=<hex>`` against the body's HMAC-SHA256.

    An empty secret never verifies: an unsigned webhook would let anyone
    trigger reviews.
    """
    if not secret or not signature:
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _split_repo_path(full_name: str) -> tuple[str, str]:
    """'workspace/repo_slug' → ('workspace', 'repo_slug')."""
    parts = full_name.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid Bitbucket repo full_name: {full_name!r}")
    return parts[0], parts[1]


def _pr_url(payload: dict[str, Any]) -> str:
    pr = payload.get("pullrequest", {})
    href = ((pr.get("links") or {}).get("html") or {}).get("href")
    if href:
        return str(href)
    repo_href = ((payload.get("repository", {}).get("links") or {}).get("html") or {}).get(
        "href", ""
    )
    return f"{repo_href}/pull-requests/{pr.get('id')}"


async def _bot_ids(auth: PlatformAuth) -> set[str]:
    get_ids = getattr(auth, "get_bot_ids", None)
    if get_ids is not None:
        return set(await get_ids())
    identity = await auth.get_bot_identity()
    return {identity} if identity else set()


def _comment_text(comment: dict[str, Any], bot_ids: set[str], bot_handle: str) -> str:
    """The comment's raw markdown with the bot's ``@{id}`` mentions made ``@handle``."""
    raw = (comment.get("content") or {}).get("raw") or ""
    if not bot_handle:
        return str(raw)
    return normalize_mentions(str(raw), dict.fromkeys(bot_ids, bot_handle))


async def handle_bitbucket_pr(payload: dict[str, Any], auth: PlatformAuth, bot_name: str) -> None:
    """Review a pull request (created / new commits pushed)."""
    from mira.platforms.handlers import PAUSE_LABEL, run_pr_review
    from mira.platforms.index_handlers import _get_app_db

    pr = payload.get("pullrequest", {})
    repo = payload.get("repository", {})
    try:
        owner, repo_name = _split_repo_path(repo.get("full_name", ""))
    except ValueError:
        return
    number = pr.get("id")
    if number is None or pr.get("state", "OPEN") != "OPEN":
        return

    names = mention_names(bot_name, await auth.get_bot_identity())
    description = pr.get("description", "") or ""
    if any(re.search(rf"@{re.escape(n)}[ \t]+ignore\b", description, re.IGNORECASE) for n in names):
        logger.info(
            "PR %s/%s#%d ignored via @%s ignore in description", owner, repo_name, number, bot_name
        )
        return
    if PAUSE_LABEL in state.get_labels(owner, repo_name, number):
        logger.info("PR %s/%s#%d paused via %s", owner, repo_name, number, PAUSE_LABEL)
        return

    try:
        _get_app_db().register_repo(owner, repo_name, platform="bitbucket")
        token = await auth.get_token()
        provider = create_provider("bitbucket", token)
        await run_pr_review(
            provider,
            owner,
            repo_name,
            number,
            _pr_url(payload),
            repo.get("is_private", True),
            bot_name,
            platform="bitbucket",
            pr_title=pr.get("title", "") or "",
        )
    except Exception as exc:
        logger.exception("Error handling Bitbucket PR event for %s/%s#%d", owner, repo_name, number)
        from mira.outbound_webhooks import REVIEW_FAILED, dispatch_event

        await dispatch_event(
            REVIEW_FAILED,
            {"repo": f"{owner}/{repo_name}", "pr_url": _pr_url(payload), "error": str(exc)},
        )


async def handle_bitbucket_merge(
    payload: dict[str, Any], auth: PlatformAuth, bot_name: str
) -> None:
    """Merge-time learning when a PR is merged (``pullrequest:fulfilled``)."""
    from mira.platforms.handlers import run_pr_merged_learning

    pr = payload.get("pullrequest", {})
    merged_by = user_handle(payload.get("actor"))
    try:
        token = await auth.get_token()
        provider = create_provider("bitbucket", token)
        pr_info = await provider.get_pr_info(_pr_url(payload))
        await run_pr_merged_learning(provider, pr_info, bot_name, merged_by, platform="bitbucket")
    except Exception:
        logger.exception("Error handling Bitbucket merge for PR #%s", pr.get("id"))


async def _changed_paths(
    token: str, owner: str, repo: str, new_hash: str, old_hash: str
) -> tuple[set[str], set[str]]:
    """(changed, removed) paths between two commits, via the diffstat API."""
    spec = f"{quote(new_hash, safe='')}..{quote(old_hash, safe='')}"
    url: str | None = (
        f"{_api()}/repositories/{quote(owner, safe='')}/{quote(repo, safe='')}"
        f"/diffstat/{spec}?topic=false&pagelen=500"
    )
    changed: set[str] = set()
    removed: set[str] = set()
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        while url:
            resp = await client.get(url, headers=bitbucket_auth_headers(token))
            resp.raise_for_status()
            data = resp.json()
            for entry in data.get("values") or []:
                old_path = (entry.get("old") or {}).get("path")
                new_path = (entry.get("new") or {}).get("path")
                if entry.get("status") == "removed" or not new_path:
                    if old_path:
                        removed.add(old_path)
                    continue
                changed.add(new_path)
                if old_path and old_path != new_path:
                    removed.add(old_path)
            url = data.get("next")
    return changed - removed, removed


async def handle_bitbucket_push(payload: dict[str, Any], auth: PlatformAuth, bot_name: str) -> None:
    """Incrementally index a push to the repo's main branch."""
    from mira.platforms.index_handlers import _get_app_db, run_incremental_index

    repo_data = payload.get("repository", {})
    try:
        owner, repo_name = _split_repo_path(repo_data.get("full_name", ""))
    except ValueError:
        return

    repo_record = _get_app_db().get_repo(owner, repo_name, platform="bitbucket")
    if not repo_record or repo_record.status not in ("ready", "indexing"):
        logger.debug("Bitbucket push to %s/%s skipped — not indexed", owner, repo_name)
        return

    try:
        token = await auth.get_token()
        fetcher = make_fetcher("bitbucket", token)
        main_branch = (repo_data.get("mainbranch") or {}).get("name") or (
            await fetcher.default_branch(owner, repo_name)
        )
        changed: set[str] = set()
        removed: set[str] = set()
        for change in (payload.get("push") or {}).get("changes") or []:
            new = change.get("new") or {}
            old = change.get("old") or {}
            if new.get("type") != "branch" or new.get("name") != main_branch:
                continue
            new_hash = (new.get("target") or {}).get("hash", "")
            old_hash = (old.get("target") or {}).get("hash", "")
            if not new_hash or not old_hash:
                # Branch creation has no "old" side; a full re-index covers it.
                continue
            c, r = await _changed_paths(token, owner, repo_name, new_hash, old_hash)
            changed |= c
            removed |= r
        changed -= removed
        if not changed and not removed:
            return
        await run_incremental_index(
            owner,
            repo_name,
            fetcher,
            list(changed),
            list(removed),
            main_branch,
            platform="bitbucket",
        )
    except Exception:
        logger.exception("Error handling Bitbucket push for %s/%s", owner, repo_name)


async def handle_bitbucket_comment(
    payload: dict[str, Any], auth: PlatformAuth, bot_name: str
) -> None:
    """An @-mention in a PR comment: command, pause/resume, or thread reply."""
    from mira.platforms.handlers import (
        _PAUSE_KEYWORDS,
        _REJECT_KEYWORDS,
        _RESUME_KEYWORDS,
        PAUSE_LABEL,
        _open_store,
        run_pr_command,
        run_thread_reply,
    )

    comment = payload.get("comment", {})
    pr = payload.get("pullrequest", {})
    number = pr.get("id")
    if number is None:
        return
    try:
        owner, repo_name = _split_repo_path(payload.get("repository", {}).get("full_name", ""))
    except ValueError:
        return

    pr_url = _pr_url(payload)
    actor = user_handle(comment.get("user") or payload.get("actor"))
    bot_identity = await auth.get_bot_identity()
    bot_ids = await _bot_ids(auth)
    names = mention_names(bot_name, bot_identity)
    body = _comment_text(comment, bot_ids, bot_identity or bot_name)

    try:
        token = await auth.get_token()
        provider = create_provider("bitbucket", token)
        pr_info = await provider.get_pr_info(pr_url)
        config = load_config()

        question = strip_mentions(body, names)
        first_word = question.split()[0].lower() if question.split() else ""

        if first_word in _PAUSE_KEYWORDS:
            if not await is_command_allowed(
                config, "pause", actor, provider=provider, owner=owner, repo=repo_name
            ):
                return
            await provider.add_label(pr_info, PAUSE_LABEL)
            await provider.post_comment(
                pr_info,
                f"Automatic reviews paused. Request a manual review with `@{bot_name} review`.",
            )
            return
        if first_word in _RESUME_KEYWORDS:
            if not await is_command_allowed(
                config, "resume", actor, provider=provider, owner=owner, repo=repo_name
            ):
                return
            await provider.remove_label(pr_info, PAUSE_LABEL)
            await provider.post_comment(pr_info, "Automatic reviews resumed.")
            return

        inline = comment.get("inline") or {}
        comment_path = inline.get("path", "") or ""
        comment_line = int(inline.get("to") or inline.get("from") or 0)
        comment_id = comment.get("id")
        thread_id = str((comment.get("parent") or {}).get("id") or comment_id or "")
        if comment.get("parent"):
            thread_id = (
                await provider.get_thread_id_for_comment(str(comment_id), pr_info) or thread_id
            )

        # A reply in a thread whose root is an inline comment is a thread reply,
        # even though the reply itself may not carry the inline anchor.
        root_body = ""
        if comment.get("parent"):
            root_body = await provider.get_discussion_root_body(pr_info, thread_id)
            if not comment_path:
                threads = await provider.get_all_bot_threads(pr_info)
                root = next((t for t in threads if t.thread_id == thread_id), None)
                if root is not None:
                    comment_path, comment_line = root.path, root.line

        if first_word in _REJECT_KEYWORDS and comment_path:
            if not await is_command_allowed(
                config, "reject", actor, provider=provider, owner=owner, repo=repo_name
            ):
                return
            store = None
            try:
                store = _open_store(owner, repo_name, "bitbucket")
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
                logger.debug("Failed to record Bitbucket reject feedback: %s", exc)
            finally:
                if store is not None:
                    store.close()
            return

        if comment_path:
            await run_thread_reply(
                provider,
                pr_info,
                question,
                comment_id,
                original_suggestion=root_body,
                thread_id=thread_id,
                comment_path=comment_path,
                comment_line=comment_line,
                actor=actor,
                bot_name=bot_name,
                platform="bitbucket",
            )
            return

        await run_pr_command(
            provider,
            owner,
            repo_name,
            number,
            pr_url,
            question,
            actor,
            bot_name,
            platform="bitbucket",
        )
    except Exception:
        logger.exception("Error handling Bitbucket comment on %s/%s#%d", owner, repo_name, number)


async def dispatch_bitbucket_event(
    event: str,
    payload: dict[str, Any],
    auth: PlatformAuth,
    bot_name: str,
    background_tasks: Any,
) -> str:
    """Route a verified Bitbucket webhook to a handler. Returns a status string.

    Self-authored events (the bot's own comments) are ignored to avoid loops.
    """
    actor_user = payload.get("actor") or {}
    actor = user_handle(actor_user)
    bot_ids = await _bot_ids(auth)
    if bot_ids & user_identifiers(actor_user):
        return "ignored"
    cfg = load_config()

    if event in PR_REVIEW_EVENTS:
        pr = payload.get("pullrequest", {})
        if pr.get("state", "OPEN") != "OPEN":
            return "ignored"
        author = user_handle(pr.get("author")) or actor
        if author_is_filtered(author, cfg.filter.allowed_authors, cfg.filter.blocked_authors):
            logger.debug("PR #%s skipped — author %s filtered", pr.get("id"), author)
            return "ignored"
        try:
            owner, repo_name = _split_repo_path(payload.get("repository", {}).get("full_name", ""))
        except ValueError:
            return "ignored"
        head = ((pr.get("source") or {}).get("commit") or {}).get("hash", "")
        previous = state.swap_head(owner, repo_name, pr.get("id", 0), head)
        if event == "pullrequest:updated":
            # Title/description edits also fire `updated`; only new commits count.
            if not head or head == previous:
                return "ignored"
            if not cfg.review.review_on_synchronize:
                logger.info(
                    "Bitbucket PR %s/%s#%s push skipped — review.review_on_synchronize is off",
                    owner,
                    repo_name,
                    pr.get("id"),
                )
                return "ignored"
        background_tasks.add_task(handle_bitbucket_pr, payload, auth, bot_name)
        return "processing"

    if event == "pullrequest:fulfilled":
        background_tasks.add_task(handle_bitbucket_merge, payload, auth, bot_name)
        return "processing"

    if event == "pullrequest:comment_created":
        bot_identity = await auth.get_bot_identity()
        names = mention_names(bot_name, bot_identity)
        body = _comment_text(payload.get("comment", {}), bot_ids, bot_identity or bot_name)
        if has_mention(body, names):
            background_tasks.add_task(handle_bitbucket_comment, payload, auth, bot_name)
            return "processing"
        return "ignored"

    if event == "repo:push":
        if author_is_filtered(actor, cfg.filter.allowed_authors, cfg.filter.blocked_authors):
            logger.debug("push ignored — author %s filtered", actor)
            return "ignored"
        background_tasks.add_task(handle_bitbucket_push, payload, auth, bot_name)
        return "processing"

    return "ignored"
