"""Webhook event handlers for the GitHub App."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from mira.config import load_config
from mira.core.engine import ReviewEngine
from mira.dashboard.models_config import llm_config_for
from mira.github_app.auth import GitHubAppAuth
from mira.index.store import IndexStore
from mira.llm import create_llm
from mira.llm.prompts.review import build_conversation_prompt
from mira.llm.tool_schemas import SUBMIT_THREAD_REPLY_TOOL
from mira.llm.utils import strip_code_fences, strip_think_blocks
from mira.models import PRInfo
from mira.platforms.mentions import command_after_mention, mention_names, strip_mentions
from mira.providers import create_provider

logger = logging.getLogger(__name__)


def _open_store(owner: str, repo: str, platform: str = "github") -> IndexStore:
    """Open an IndexStore for the given owner/repo."""
    return IndexStore.open(owner, repo, platform=platform)


_REVIEW_KEYWORDS = {"review", "review this", "review this pr"}
_REJECT_KEYWORDS = {"reject", "dismiss", "resolve", "ignore"}
_REVIEW_REST_KEYWORDS = {"review-rest", "review rest", "rest", "continue"}
_HELP_KEYWORDS = {"help", "?", "commands"}


def _help_message(bot_name: str) -> str:
    """Markdown help comment listing every command Mira understands."""
    return (
        f"### Mira commands\n\n"
        f"Mention `@{bot_name}` in a PR comment followed by one of these verbs:\n\n"
        f"| Command | What it does |\n"
        f"|---|---|\n"
        f"| `@{bot_name} review` | Re-run the full review on this PR. Useful after force-pushes or when you want a fresh pass. |\n"
        f"| `@{bot_name} review-rest` | Review files that were skipped on the first pass because the PR was too large. Aliases: `rest`, `continue`. |\n"
        f"| `@{bot_name} pause` | Pause Mira on this PR. No more reviews until you resume. Adds a `mira-paused` label. |\n"
        f"| `@{bot_name} resume` | Resume Mira on a paused PR and re-review the latest diff. |\n"
        f"| `@{bot_name} help` | Show this message. Aliases: `?`, `commands`. |\n"
        f"| `@{bot_name} <anything else>` | Ask a free-form question about the PR. Mira will reply inline using the PR diff as context. |\n\n"
        f"On an inline review comment Mira posted, reply with `@{bot_name} reject` "
        f"(aliases: `dismiss`, `resolve`, `ignore`) to mark the thread resolved and "
        f"teach Mira not to make similar suggestions in the future.\n\n"
        f"To skip a PR entirely, include `@{bot_name} ignore` in the PR body.\n\n"
        f"Full docs: https://docs.miracode.ai/commands"
    )


_THREAD_REPLY_ENV = Environment(
    loader=FileSystemLoader(
        str(Path(__file__).resolve().parents[1] / "llm" / "prompts" / "templates")
    ),
    trim_blocks=True,
    lstrip_blocks=True,
)
_THREAD_REPLY_TEMPLATE = _THREAD_REPLY_ENV.get_template("thread_reply.jinja2")

PAUSE_LABEL = "mira-paused"
_PAUSE_KEYWORDS = {"pause"}
_RESUME_KEYWORDS = {"resume"}


async def run_pr_review(
    provider: Any,
    owner: str,
    repo: str,
    number: int,
    pr_url: str,
    is_private: bool,
    bot_name: str,
    platform: str = "github",
) -> None:
    """Platform-neutral review core: review a PR/MR and post the result.

    Shared by the GitHub and GitLab webhook handlers — everything here goes
    through the ``provider`` abstraction and the engine, so it's the same for
    every platform.
    """
    repo_full = f"{owner}/{repo}"
    config = load_config()
    from mira.dashboard.models_config import llm_config_for

    llm = create_llm(llm_config_for("review", config.llm))
    indexing_llm = create_llm(llm_config_for("indexing", config.llm))
    engine = ReviewEngine(
        config=config,
        llm=llm,
        provider=provider,
        bot_name=bot_name,
        indexing_llm=indexing_llm,
    )

    from mira.dashboard.api import _app_db

    # Keep visibility current — the blast-radius filter relies on it to avoid
    # naming private repos in a public repo's review.
    _app_db.set_repo_visibility(owner, repo, is_private, platform=platform)

    repo_record = _app_db.get_repo(owner, repo, platform=platform)
    is_indexed = bool(repo_record and repo_record.status == "ready")

    logger.info("Reviewing %s (indexed=%s)", pr_url, is_indexed)
    result = await engine.review_pr(pr_url)

    # The walkthrough comment already carries the "more accurate after indexing"
    # nudge for unindexed repos, so we don't post a separate note here — that
    # would repeat on every push.

    logger.info("Review complete for %s", pr_url)

    from mira.models import Severity, build_review_stats
    from mira.outbound_webhooks import (
        REVIEW_COMPLETED,
        REVIEW_HIGH_SEVERITY,
        dispatch_event,
    )

    stats = build_review_stats(result.comments)
    event_data = {
        "repo": repo_full,
        "pr_url": pr_url,
        "number": number,
        "comments": len(result.comments),
        "key_issues": len(result.key_issues),
        "severities": {sev.name.lower(): n for sev, n in stats.items()},
    }
    await dispatch_event(REVIEW_COMPLETED, event_data)
    if any(sev >= Severity.WARNING for sev in stats):
        await dispatch_event(REVIEW_HIGH_SEVERITY, event_data)


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
        await run_pr_review(provider, owner, repo, number, pr_url, is_private, bot_name)
    except Exception as exc:
        logger.exception("Error handling pull_request event")
        if pr_url:
            from mira.outbound_webhooks import REVIEW_FAILED, dispatch_event

            await dispatch_event(
                REVIEW_FAILED,
                {"repo": repo_full, "pr_url": pr_url, "error": str(exc)},
            )


async def run_pr_command(
    provider: Any,
    owner: str,
    repo: str,
    number: int,
    pr_url: str,
    question: str,
    actor: str,
    bot_name: str,
    platform: str = "github",
) -> None:
    """Platform-neutral handler for an @-mention command on a PR/MR.

    Dispatches help / review / review-rest / free-form Q&A through the provider
    and engine. Shared by the GitHub and GitLab comment handlers.
    """
    config = load_config()
    from mira.dashboard.models_config import llm_config_for

    llm = create_llm(llm_config_for("review", config.llm))
    indexing_llm = create_llm(llm_config_for("indexing", config.llm))

    normalized = question.lower().strip()
    is_review = normalized in _REVIEW_KEYWORDS
    is_review_rest = normalized in _REVIEW_REST_KEYWORDS
    is_help = normalized in _HELP_KEYWORDS

    if is_help:
        pr_info_for_help = await provider.get_pr_info(pr_url)
        await provider.post_comment(pr_info_for_help, _help_message(bot_name))
        logger.info("Help requested on %s by @%s", pr_url, actor)
        return

    if is_review_rest:
        from mira.dashboard.api import _app_db

        progress = _app_db.get_pr_review_progress(owner, repo, number, platform=platform)
        if not progress or not progress.skipped_paths:
            pr_info_for_reply = await provider.get_pr_info(pr_url)
            await provider.post_comment(
                pr_info_for_reply,
                f"> @{actor}: nothing left to review — every file in this "
                "PR has already been covered. 🎉",
            )
            return
        engine = ReviewEngine(
            config=config,
            llm=llm,
            provider=provider,
            bot_name=bot_name,
            indexing_llm=indexing_llm,
        )
        engine._review_only_paths = set(progress.skipped_paths)  # type: ignore[attr-defined]
        logger.info(
            "review-rest on %s by @%s — %d file(s)", pr_url, actor, len(progress.skipped_paths)
        )
        await engine.review_pr(pr_url)
    elif is_review:
        engine = ReviewEngine(
            config=config,
            llm=llm,
            provider=provider,
            bot_name=bot_name,
            indexing_llm=indexing_llm,
        )
        logger.info("Re-review triggered for %s by @%s", pr_url, actor)
        await engine.review_pr(pr_url)
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
        await provider.post_comment(pr_info, f"> @{actor} asked: {question}\n\n{response}")
        logger.info("Replied to comment on %s", pr_url)


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


async def run_thread_reply(
    provider: Any,
    pr_info: Any,
    human_reply: str,
    comment_id: int,
    *,
    original_suggestion: str = "",
    thread_id: str | None = None,
    comment_node_id: str | None = None,
    comment_path: str = "",
    comment_line: int = 0,
    actor: str = "",
    bot_name: str = "miracodeai",
    platform: str = "github",
) -> None:
    """Platform-neutral free-form thread reply with intent classification.

    The LLM classifies the human's message and we respond accordingly:
    ``disagreement`` → reply + resolve the thread + record a ``rejected``
    feedback signal (same learning signal as an explicit reject); ``question``
    → answer, leave open; ``agreement`` / ``other`` → acknowledge, leave open.
    """
    config = load_config()
    llm = create_llm(llm_config_for("indexing", config.llm))
    prompt = _THREAD_REPLY_TEMPLATE.render(
        user_reply=human_reply or "(empty)",
        original_suggestion=original_suggestion,
    )
    # Tool calling forces a schema-valid result — more reliable than parsing
    # free-form JSON. The provider's tenacity decorator retries transient fails.
    try:
        raw = await llm.complete_with_tools(
            messages=[{"role": "user", "content": prompt}],
            tools=[SUBMIT_THREAD_REPLY_TOOL],
            temperature=0.0,
        )
        data = json.loads(strip_think_blocks(strip_code_fences(raw))) if raw else {}
    except Exception as exc:
        logger.warning("Free-form thread reply LLM call failed: %s", exc)
        return

    intent = str(data.get("intent", "other")).lower()
    reply_text = str(data.get("reply", "")).strip()
    if not reply_text:
        logger.warning("Free-form thread reply: empty reply (intent=%s). Skipping.", intent)
        return

    try:
        await provider.reply_to_review_comment(pr_info, comment_id, reply_text)
    except Exception as exc:
        logger.warning("Failed to post thread reply: %s", exc)
        return

    if intent == "disagreement":
        try:
            tid = thread_id
            if tid is None and comment_node_id:
                tid = await provider.get_thread_id_for_comment(comment_node_id, pr_info)
            if tid:
                await provider.resolve_threads(pr_info, [tid])
        except Exception as exc:
            logger.warning("Failed to resolve disagreement thread: %s", exc)
        try:
            store = _open_store(pr_info.owner, pr_info.repo, platform)
            try:
                store.record_feedback(
                    pr_number=pr_info.number,
                    pr_url=pr_info.url,
                    comment_path=comment_path,
                    comment_line=comment_line,
                    comment_category="",
                    comment_severity="",
                    comment_title="",
                    signal="rejected",
                    actor=actor,
                )
            finally:
                store.close()
        except Exception as fb_err:
            logger.debug("Failed to record disagreement feedback: %s", fb_err)

    logger.info("Thread reply (%s) on %s: %s", intent, pr_info.url, reply_text[:80])


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


async def run_pr_merged_learning(
    provider: Any,
    pr_info: Any,
    bot_name: str,
    merged_by: str,
    platform: str = "github",
) -> None:
    """Platform-neutral merge-time learning: record accept/reject + human-review
    signals and synthesize rules. Shared by GitHub and GitLab."""
    from mira.providers.formatting import parse_bot_comment_metadata

    owner, repo, number, pr_url = pr_info.owner, pr_info.repo, pr_info.number, pr_info.url
    store = _open_store(owner, repo, platform)
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

        from mira.analysis.feedback import synthesize_from_human_reviews, synthesize_rules

        deterministic_rules = synthesize_rules(store)

        if human_recorded > 0:
            try:
                config = load_config()
                from mira.dashboard.models_config import llm_config_for

                indexing_llm = create_llm(llm_config_for("indexing", config.llm))
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
            head_sha=pr["head"].get("sha") or "",
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
