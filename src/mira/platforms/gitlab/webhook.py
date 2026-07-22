"""GitLab webhook handling: token verification, event normalization, handlers.

GitLab's webhook shape differs from GitHub's (``X-Gitlab-Token`` shared-secret
instead of an HMAC signature, ``object_kind`` events, merge requests, project
``path_with_namespace``). These thin handlers translate that into the same
platform-neutral review/index cores the GitHub handlers use.
"""

from __future__ import annotations

import hmac
import logging
import re
from typing import Any

import httpx

from mira.config import load_config
from mira.platforms import profiles
from mira.platforms.auth import PlatformAuth
from mira.platforms.fetch import _next_link, make_fetcher
from mira.platforms.mentions import (
    author_is_filtered,
    command_after_mention,
    has_mention,
    mention_names,
    strip_mentions,
)
from mira.providers import create_provider

logger = logging.getLogger(__name__)


async def list_gitlab_projects(token: str, base_url: str) -> list[dict[str, Any]]:
    """Every project the token can access (paginated). Scope follows the token:
    a project token → that project; a group token → the group's projects; a PAT
    → everything the user is a member of."""
    out: list[dict[str, Any]] = []
    url: str | None = f"{base_url.rstrip('/')}/projects?membership=true&simple=true&per_page=100"
    async with httpx.AsyncClient(timeout=30) as client:
        while url:
            resp = await client.get(url, headers={"PRIVATE-TOKEN": token})
            if resp.status_code != 200:
                logger.warning(
                    "GitLab project list failed: %d %s", resp.status_code, resp.text[:200]
                )
                break
            out.extend(resp.json())
            url = _next_link(resp.headers.get("link", ""))
    return out


async def backfill_gitlab_projects(auth: PlatformAuth) -> int:
    """Register every accessible GitLab project so they show in the dashboard
    ready to index — without waiting for a webhook. Returns the count."""
    from mira.platforms.index_handlers import _get_app_db

    token = await auth.get_token()
    base_url = profiles.resolve("gitlab")["api_url"]
    projects = await list_gitlab_projects(token, base_url)
    db = _get_app_db()
    n = 0
    for p in projects:
        path = p.get("path_with_namespace", "")
        if "/" not in path:
            continue
        owner, repo = path.rsplit("/", 1)
        db.register_repo(owner, repo, platform="gitlab")
        db.set_repo_visibility(
            owner, repo, p.get("visibility", "private") != "public", platform="gitlab"
        )
        n += 1
    logger.info("GitLab: discovered + registered %d accessible project(s)", n)
    return n


def verify_gitlab_token(header_value: str, secret: str) -> bool:
    """GitLab sends the configured secret verbatim in X-Gitlab-Token."""
    return hmac.compare_digest(header_value or "", secret or "")


def _split_project_path(path_with_namespace: str) -> tuple[str, str]:
    """'group/sub/proj' → ('group/sub', 'proj'). Owner may be a nested group."""
    owner, _, repo = path_with_namespace.rpartition("/")
    return owner, repo


async def handle_merge_request(payload: dict[str, Any], auth: PlatformAuth, bot_name: str) -> None:
    """Review a merge request (open / reopen / new commits)."""
    from mira.platforms.handlers import PAUSE_LABEL, run_pr_review
    from mira.platforms.index_handlers import _get_app_db

    attrs = payload.get("object_attributes", {})
    project = payload.get("project", {})
    owner, repo = _split_project_path(project.get("path_with_namespace", ""))
    iid = attrs.get("iid")
    mr_url = attrs.get("url") or f"{project.get('web_url', '')}/-/merge_requests/{iid}"
    is_private = project.get("visibility", "private") != "public"

    # Same opt-outs as GitHub: `@mira ignore` in the description and the
    # mira-paused label both skip auto-review.
    names = mention_names(bot_name, await auth.get_bot_identity())
    description = attrs.get("description", "") or ""
    if any(re.search(rf"@{re.escape(n)}[ \t]+ignore\b", description, re.IGNORECASE) for n in names):
        logger.info("MR %s/%s!%s ignored via @%s ignore in description", owner, repo, iid, bot_name)
        return
    labels = payload.get("labels") or attrs.get("labels") or []
    if any((lbl.get("title") or lbl.get("name")) == PAUSE_LABEL for lbl in labels):
        logger.info("MR %s/%s!%s paused via %s label", owner, repo, iid, PAUSE_LABEL)
        return

    try:
        # GitLab has no install event, so a repo first becomes known to Mira
        # when we see an MR for it. Register it (idempotent; preserves status)
        # so it shows up in the dashboard, ready to index.
        _get_app_db().register_repo(owner, repo, platform="gitlab")
        token = await auth.get_token()
        provider = create_provider("gitlab", token)
        await run_pr_review(
            provider, owner, repo, iid, mr_url, is_private, bot_name, platform="gitlab"
        )
    except Exception:
        logger.exception("Error handling GitLab merge_request event for %s/%s!%s", owner, repo, iid)


async def handle_gitlab_push(payload: dict[str, Any], auth: PlatformAuth, bot_name: str) -> None:
    """Incrementally index a push to the default branch."""
    from mira.platforms.index_handlers import _get_app_db, run_incremental_index

    project = payload.get("project", {})
    owner, repo = _split_project_path(project.get("path_with_namespace", ""))
    default_branch = project.get("default_branch", "main")

    repo_record = _get_app_db().get_repo(owner, repo, platform="gitlab")
    if not repo_record or repo_record.status not in ("ready", "indexing"):
        logger.debug("GitLab push to %s/%s skipped — not indexed", owner, repo)
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
            repo,
            make_fetcher("gitlab", token),
            list(changed),
            list(removed),
            default_branch,
            platform="gitlab",
        )
    except Exception:
        logger.exception("Error handling GitLab push for %s/%s", owner, repo)


async def handle_gitlab_merge(payload: dict[str, Any], auth: PlatformAuth, bot_name: str) -> None:
    """Merge-time learning when an MR is merged."""
    from mira.platforms.handlers import run_pr_merged_learning

    attrs = payload.get("object_attributes", {})
    project = payload.get("project", {})
    owner, repo = _split_project_path(project.get("path_with_namespace", ""))
    iid = attrs.get("iid")
    mr_url = attrs.get("url") or f"{project.get('web_url', '')}/-/merge_requests/{iid}"
    merged_by = payload.get("user", {}).get("username", "")
    try:
        token = await auth.get_token()
        provider = create_provider("gitlab", token)
        pr_info = await provider.get_pr_info(mr_url)
        await run_pr_merged_learning(provider, pr_info, bot_name, merged_by, platform="gitlab")
    except Exception:
        logger.exception("Error handling GitLab merge for %s/%s!%s", owner, repo, iid)


async def handle_gitlab_note(payload: dict[str, Any], auth: PlatformAuth, bot_name: str) -> None:
    """An @-mention in an MR note: command, pause/resume, or thread reject."""
    from mira.platforms.handlers import (
        _PAUSE_KEYWORDS,
        _REJECT_KEYWORDS,
        _RESUME_KEYWORDS,
        PAUSE_LABEL,
        _open_store,
        run_pr_command,
        run_thread_reply,
    )

    attrs = payload.get("object_attributes", {})
    note_body = attrs.get("note", "") or ""
    project = payload.get("project", {})
    owner, repo = _split_project_path(project.get("path_with_namespace", ""))
    mr = payload.get("merge_request", {})
    iid = mr.get("iid")
    if iid is None:
        return
    mr_url = mr.get("url") or f"{project.get('web_url', '')}/-/merge_requests/{iid}"
    actor = payload.get("user", {}).get("username", "")

    try:
        token = await auth.get_token()
        provider = create_provider("gitlab", token)
        pr_info = await provider.get_pr_info(mr_url)

        # Accept a mention of either the configured name or the real bot user.
        names = mention_names(bot_name, await auth.get_bot_identity())
        question = strip_mentions(note_body, names)
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

        discussion_id = attrs.get("discussion_id")
        position = attrs.get("position")

        # Explicit reject on an inline (diff) note → resolve + record feedback.
        if first_word in _REJECT_KEYWORDS and discussion_id and position:
            await provider.resolve_threads(pr_info, [str(discussion_id)])
            try:
                store = _open_store(owner, repo, "gitlab")
                store.record_feedback(
                    pr_number=iid,
                    pr_url=mr_url,
                    comment_path=position.get("new_path", ""),
                    comment_line=position.get("new_line", 0) or 0,
                    comment_category="",
                    comment_severity="",
                    comment_title="",
                    signal="rejected",
                    actor=actor,
                )
            except Exception as exc:
                logger.debug("Failed to record GitLab reject feedback: %s", exc)
            finally:
                if "store" in locals():
                    store.close()
            return

        # Free-form @-mention on an inline thread → LLM intent classification.
        if discussion_id and position:
            original = await provider.get_discussion_root_body(pr_info, str(discussion_id))
            await run_thread_reply(
                provider,
                pr_info,
                question,
                attrs.get("id"),
                original_suggestion=original,
                thread_id=str(discussion_id),
                comment_path=position.get("new_path", ""),
                comment_line=position.get("new_line", 0) or 0,
                actor=actor,
                bot_name=bot_name,
                platform="gitlab",
            )
            return

        # General MR comment → review / help / Q&A.
        await run_pr_command(
            provider, owner, repo, iid, mr_url, question, actor, bot_name, platform="gitlab"
        )
    except Exception:
        logger.exception("Error handling GitLab note on %s/%s!%s", owner, repo, iid)


async def dispatch_gitlab_event(
    event: str,
    payload: dict[str, Any],
    auth: PlatformAuth,
    bot_name: str,
    background_tasks: Any,
) -> str:
    """Route a verified GitLab webhook to a handler. Returns a status string.

    Self-authored events (the bot's own notes/MRs) are ignored to avoid loops.
    """
    actor = payload.get("user", {}).get("username", "") or payload.get("user_username", "")
    bot_identity = await auth.get_bot_identity()
    if actor and bot_identity and actor == bot_identity:
        return "ignored"

    if event == "Merge Request Hook":
        attrs = payload.get("object_attributes", {})
        action = attrs.get("action", "")
        # 'update' fires for many reasons; only review when new commits landed.
        if action in ("open", "reopen") or (action == "update" and attrs.get("oldrev")):
            cfg = load_config()
            if author_is_filtered(actor, cfg.filter.allowed_authors, cfg.filter.blocked_authors):
                logger.debug("MR skipped — author %s filtered by author filter", actor)
                return "ignored"
            background_tasks.add_task(handle_merge_request, payload, auth, bot_name)
            return "processing"
        if action == "merge":
            background_tasks.add_task(handle_gitlab_merge, payload, auth, bot_name)
            return "processing"
        return "ignored"

    if event == "Note Hook":
        attrs = payload.get("object_attributes", {})
        names = mention_names(bot_name, bot_identity)
        if attrs.get("noteable_type") == "MergeRequest" and has_mention(
            attrs.get("note") or "", names
        ):
            cmd_word = command_after_mention(attrs.get("note") or "", names)
            if cmd_word != "review":
                cfg = load_config()
                if author_is_filtered(
                    actor, cfg.filter.allowed_authors, cfg.filter.blocked_authors
                ):
                    logger.debug("MR note skipped — author %s filtered", actor)
                    return "ignored"
            background_tasks.add_task(handle_gitlab_note, payload, auth, bot_name)
            return "processing"
        return "ignored"

    if event == "Push Hook":
        ref = payload.get("ref", "")
        default_branch = payload.get("project", {}).get("default_branch", "main")
        if ref == f"refs/heads/{default_branch}":
            cfg = load_config()
            if author_is_filtered(actor, cfg.filter.allowed_authors, cfg.filter.blocked_authors):
                logger.debug("push skipped — author %s filtered", actor)
                return "ignored"
            background_tasks.add_task(handle_gitlab_push, payload, auth, bot_name)
            return "processing"

    return "ignored"
