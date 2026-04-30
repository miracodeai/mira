"""Webhook event handlers for the GitHub App."""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from mira.config import load_config
from mira.core.engine import ReviewEngine
from mira.github_app.auth import GitHubAppAuth
from mira.index.store import IndexStore
from mira.llm.prompts.conversation import build_conversation_prompt
from mira.llm.provider import LLMProvider
from mira.providers import create_provider

logger = logging.getLogger(__name__)


def _open_store(owner: str, repo: str) -> IndexStore:
    """Open an IndexStore for the given owner/repo."""
    return IndexStore.open(owner, repo)


_REVIEW_KEYWORDS = {"review", "review this", "review this pr"}
_REJECT_KEYWORDS = {"reject", "dismiss", "resolve", "ignore"}
_REVIEW_REST_KEYWORDS = {"review-rest", "review rest", "rest", "continue"}

PAUSE_LABEL = "mira-paused"
_PAUSE_KEYWORDS = {"pause"}
_RESUME_KEYWORDS = {"resume"}


async def handle_pull_request(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
) -> None:
    """Handle a pull_request event by running a full review."""
    installation_id: int = payload.get("installation", {}).get("id", 0)
    try:
        token = await app_auth.get_installation_token(installation_id)

        pr = payload["pull_request"]
        owner = payload["repository"]["owner"]["login"]
        repo = payload["repository"]["name"]
        number = pr["number"]
        pr_url = f"https://github.com/{owner}/{repo}/pull/{number}"

        config = load_config()
        from mira.dashboard.models_config import llm_config_for

        llm = LLMProvider(llm_config_for("review", config.llm))
        provider = create_provider("github", token)
        engine = ReviewEngine(config=config, llm=llm, provider=provider, bot_name=bot_name)

        # Check if repo is indexed
        from mira.dashboard.api import _app_db

        repo_record = _app_db.get_repo(owner, repo)
        is_indexed = bool(repo_record and repo_record.status == "ready")

        logger.info("Reviewing PR %s (indexed=%s)", pr_url, is_indexed)
        await engine.review_pr(pr_url)

        # Post a friendly note if the repo isn't indexed yet
        if not is_indexed:
            try:
                from mira.models import PRInfo

                pr_info = PRInfo(
                    title=pr["title"],
                    description=pr.get("body") or "",
                    base_branch=pr["base"]["ref"],
                    head_branch=pr["head"]["ref"],
                    url=pr_url,
                    number=number,
                    owner=owner,
                    repo=repo,
                )
                dashboard_url = os.environ.get("MIRA_DASHBOARD_URL", "http://localhost:5173")
                note = (
                    f"> **Note:** Mira hasn't indexed `{owner}/{repo}` yet, so this review uses only the diff. "
                    f"Cross-repo context and blast radius are unavailable until indexing completes.\n\n"
                    f"[Set up indexing →]({dashboard_url}/repos/{owner}/{repo})"
                )
                await provider.post_comment(pr_info, note)
            except Exception as exc:
                logger.warning("Failed to post unindexed note: %s", exc)

        logger.info("Review complete for PR %s", pr_url)
    except Exception:
        logger.exception("Error handling pull_request event")


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
        question = comment_body.replace(f"@{bot_name}", "").strip()

        owner = payload["repository"]["owner"]["login"]
        repo = payload["repository"]["name"]
        number = payload["issue"]["number"]
        pr_url = f"https://github.com/{owner}/{repo}/pull/{number}"

        config = load_config()
        from mira.dashboard.models_config import llm_config_for

        llm = LLMProvider(llm_config_for("review", config.llm))
        provider = create_provider("github", token)

        normalized = question.lower().strip()
        is_review = normalized in _REVIEW_KEYWORDS
        is_review_rest = normalized in _REVIEW_REST_KEYWORDS

        if is_review_rest:
            # Pull the unreviewed paths from progress and run review scoped to them.
            from mira.dashboard.api import _app_db

            progress = _app_db.get_pr_review_progress(owner, repo, number)
            if not progress or not progress.skipped_paths:
                pr_info_for_reply = await provider.get_pr_info(pr_url)
                await provider.post_comment(
                    pr_info_for_reply,
                    f"> @{comment_user}: nothing left to review — every file in this "
                    "PR has already been covered. 🎉",
                )
                return
            engine = ReviewEngine(config=config, llm=llm, provider=provider, bot_name=bot_name)
            engine._review_only_paths = set(progress.skipped_paths)  # type: ignore[attr-defined]
            logger.info(
                "review-rest triggered for PR %s by @%s — %d remaining file(s)",
                pr_url,
                comment_user,
                len(progress.skipped_paths),
            )
            await engine.review_pr(pr_url)
            logger.info("review-rest complete for PR %s", pr_url)
        elif is_review:
            engine = ReviewEngine(config=config, llm=llm, provider=provider, bot_name=bot_name)
            logger.info("Re-review triggered for PR %s by @%s", pr_url, comment_user)
            await engine.review_pr(pr_url)
            logger.info("Re-review complete for PR %s", pr_url)
        else:
            pr_info = await provider.get_pr_info(pr_url)
            diff_text = await provider.get_pr_diff(pr_info)

            messages = build_conversation_prompt(
                question=question,
                diff_text=diff_text,
                pr_title=pr_info.title,
                pr_description=pr_info.description,
            )
            response = await llm.complete(messages, json_mode=False)

            reply = f"> @{comment_user} asked: {question}\n\n{response}"
            await provider.post_comment(pr_info, reply)
            logger.info("Replied to comment on PR %s", pr_url)

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

        # Extract the first word after @bot_name
        match = re.search(rf"@{re.escape(bot_name)}\s+(\w+)", comment_body, re.IGNORECASE)
        if not match:
            return
        command = match.group(1).lower()
        if command not in _REJECT_KEYWORDS:
            return

        owner = payload["repository"]["owner"]["login"]
        repo = payload["repository"]["name"]
        number = payload["pull_request"]["number"]

        provider = create_provider("github", token)
        thread_id = await provider.get_thread_id_for_comment(comment_node_id)
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
        )

        from mira.providers.github import parse_bot_comment_metadata

        store = _open_store(owner, repo)
        accepted = 0
        human_recorded = 0
        deterministic_rules = 0
        llm_rules = 0
        try:
            existing = store.list_feedback(limit=2000)
            if any(
                e.signal in ("accepted", "human_review") and e.pr_number == number for e in existing
            ):
                logger.info("PR %s already processed for merge-time learning", pr_url)
                return
            rejected_locations = {
                (e.comment_path, e.comment_line)
                for e in existing
                if e.signal == "rejected" and e.pr_number == number
            }

            try:
                bot_threads = await provider.get_all_bot_threads(pr_info)
            except Exception as exc:
                logger.warning("Failed to fetch bot threads for %s: %s", pr_url, exc)
                bot_threads = []

            bot_events: list[dict] = []
            merged_by = (pr.get("merged_by") or {}).get("login", "")
            for thread in bot_threads:
                if (thread.path, thread.line) in rejected_locations:
                    continue
                meta = parse_bot_comment_metadata(thread.body)
                if not meta["category"]:
                    continue
                bot_events.append(
                    {
                        "pr_number": number,
                        "pr_url": pr_url,
                        "comment_path": thread.path,
                        "comment_line": thread.line,
                        "comment_category": meta["category"],
                        "comment_severity": meta["severity"],
                        "comment_title": meta["title"],
                        "signal": "accepted",
                        "actor": merged_by,
                    }
                )

            try:
                human_comments = await provider.get_human_review_comments(pr_info, bot_name)
            except Exception as exc:
                logger.warning("Failed to fetch human review comments for %s: %s", pr_url, exc)
                human_comments = []

            human_events: list[dict] = []
            for hc in human_comments:
                body = (hc.body or "").strip()
                if not body:
                    continue
                human_events.append(
                    {
                        "pr_number": number,
                        "pr_url": pr_url,
                        "comment_path": hc.path,
                        "comment_line": hc.line,
                        "comment_category": "human_review",
                        "comment_severity": "",
                        # Body stored in comment_title column to avoid schema change.
                        "comment_title": body[:2000],
                        "signal": "human_review",
                        "actor": hc.author,
                    }
                )

            if bot_events:
                store.record_bulk_feedback(bot_events)
                accepted = len(bot_events)
            if human_events:
                store.record_bulk_feedback(human_events)
                human_recorded = len(human_events)

            from mira.analysis.feedback import (
                synthesize_from_human_reviews,
                synthesize_rules,
            )

            deterministic_rules = synthesize_rules(store)

            if human_recorded > 0:
                try:
                    config = load_config()
                    from mira.dashboard.models_config import llm_config_for

                    indexing_llm = LLMProvider(llm_config_for("indexing", config.llm))
                    llm_rules = await synthesize_from_human_reviews(store, indexing_llm)
                except Exception as exc:
                    logger.warning("LLM rule synthesis failed for %s: %s", pr_url, exc)
        finally:
            store.close()

        logger.info(
            "PR merged %s: recorded %d accepted + %d human review events; "
            "upserted %d deterministic + %d LLM rules",
            pr_url,
            accepted,
            human_recorded,
            deterministic_rules,
            llm_rules,
        )

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
