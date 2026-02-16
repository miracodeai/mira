"""Webhook event handlers for the GitHub App."""

from __future__ import annotations

import logging
import time
from typing import Any

from mira.config import load_config
from mira.core.engine import ReviewEngine
from mira.github_app.auth import GitHubAppAuth
from mira.github_app.metrics import Metrics
from mira.llm.prompts.conversation import build_conversation_prompt
from mira.llm.provider import LLMProvider
from mira.providers import create_provider

logger = logging.getLogger(__name__)

_REVIEW_KEYWORDS = {"review", "review this", "review this pr"}


async def handle_pull_request(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
    metrics: Metrics | None = None,
) -> None:
    """Handle a pull_request event by running a full review."""
    installation_id: int = payload.get("installation", {}).get("id", 0)
    start = time.monotonic()
    try:
        token = await app_auth.get_installation_token(installation_id)

        pr = payload["pull_request"]
        owner = payload["repository"]["owner"]["login"]
        repo = payload["repository"]["name"]
        number = pr["number"]
        pr_url = f"https://github.com/{owner}/{repo}/pull/{number}"

        config = load_config()
        llm = LLMProvider(config.llm)
        provider = create_provider("github", token)
        engine = ReviewEngine(config=config, llm=llm, provider=provider, bot_name=bot_name)

        logger.info("Reviewing PR %s", pr_url)
        result = await engine.review_pr(pr_url)

        if metrics:
            metrics.track(
                "pr_review_completed",
                installation_id=installation_id,
                properties={
                    "duration_s": round(time.monotonic() - start, 2),
                    "comments_count": len(result.comments),
                },
            )
        logger.info("Review complete for PR %s", pr_url)
    except Exception as e:
        if metrics:
            metrics.track(
                "pr_review_failed",
                installation_id=installation_id,
                properties={"error_type": type(e).__name__},
            )
        logger.exception("Error handling pull_request event")


async def handle_comment(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
    metrics: Metrics | None = None,
) -> None:
    """Handle an issue_comment event mentioning the bot."""
    installation_id: int = payload.get("installation", {}).get("id", 0)
    start = time.monotonic()
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
        llm = LLMProvider(config.llm)
        provider = create_provider("github", token)

        is_review = question.lower() in _REVIEW_KEYWORDS
        if is_review:
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

        if metrics:
            metrics.track(
                "comment_reply_completed",
                installation_id=installation_id,
                properties={
                    "duration_s": round(time.monotonic() - start, 2),
                    "is_review_trigger": is_review,
                },
            )
    except Exception as e:
        if metrics:
            metrics.track(
                "comment_reply_failed",
                installation_id=installation_id,
                properties={"error_type": type(e).__name__},
            )
        logger.exception("Error handling comment event")
