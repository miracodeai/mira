"""FastAPI webhook server for the Mira GitHub App."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request, Response

from mira.github_app.auth import GitHubAppAuth
from mira.github_app.handlers import (
    _PAUSE_KEYWORDS,
    _RESUME_KEYWORDS,
    PAUSE_LABEL,
    handle_comment,
    handle_pause_resume,
    handle_pr_merged,
    handle_pull_request,
    handle_thread_reject,
)
from mira.github_app.index_handlers import (
    backfill_missing_indexes,
    handle_installation,
    handle_installation_deleted,
    handle_push_index,
    handle_repos_added,
    handle_repos_removed,
)

logger = logging.getLogger(__name__)

_PR_ACTIONS = {"opened", "synchronize", "reopened"}
_PR_MERGE_ACTIONS = {"closed"}
_SAFE_BOT_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")


def _verify_signature(payload_bytes: bytes, signature_header: str, secret: str) -> bool:
    """Verify the X-Hub-Signature-256 HMAC signature (timing-safe)."""
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature_header)


def create_app(
    app_auth: GitHubAppAuth,
    webhook_secret: str,
    bot_name: str,
) -> FastAPI:
    """Create and configure the FastAPI webhook application."""
    if not _SAFE_BOT_NAME.match(bot_name):
        raise ValueError(f"Invalid bot_name {bot_name!r}: must match [a-zA-Z0-9_-]+")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Fire-and-forget: index repos that don't have an index yet
        backfill_task = asyncio.create_task(backfill_missing_indexes(app_auth))
        backfill_task.add_done_callback(
            lambda t: (
                logger.warning("Backfill failed: %s", t.exception()) if t.exception() else None
            )
        )

        # OSV.dev vulnerability poller — runs hourly across the org's
        # package_manifests. No-ops on SQLite-only deployments.
        from mira.security.poller import run_forever as run_vuln_poller

        vuln_task = asyncio.create_task(run_vuln_poller())
        vuln_task.add_done_callback(
            lambda t: (
                logger.warning("Vuln poller crashed: %s", t.exception())
                if t.exception() and not t.cancelled()
                else None
            )
        )

        yield
        if not backfill_task.done():
            backfill_task.cancel()
        if not vuln_task.done():
            vuln_task.cancel()

    app = FastAPI(title="Mira GitHub App", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhook")
    async def webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
        payload_bytes = await request.body()
        signature = request.headers.get("X-Hub-Signature-256", "")

        if not _verify_signature(payload_bytes, signature, webhook_secret):
            return Response(
                content='{"error": "invalid signature"}',
                status_code=401,
                media_type="application/json",
            )

        event = request.headers.get("X-GitHub-Event", "")
        payload: dict[str, Any] = await request.json()
        action = payload.get("action", "")

        if (
            event == "pull_request"
            and action in _PR_MERGE_ACTIONS
            and payload.get("pull_request", {}).get("merged")
        ):
            sender: str = payload.get("sender", {}).get("login", "")
            if sender == f"{bot_name}[bot]":
                return Response(
                    content='{"status": "ignored"}',
                    status_code=200,
                    media_type="application/json",
                )
            background_tasks.add_task(handle_pr_merged, payload, app_auth, bot_name)
            return Response(
                content='{"status": "processing"}',
                status_code=200,
                media_type="application/json",
            )

        if event == "pull_request" and action in _PR_ACTIONS:
            sender: str = payload.get("sender", {}).get("login", "")
            if sender == f"{bot_name}[bot]":
                logger.debug("Ignoring pull_request event from self (%s)", sender)
                return Response(
                    content='{"status": "ignored"}',
                    status_code=200,
                    media_type="application/json",
                )

            pr_body: str = payload.get("pull_request", {}).get("body", "") or ""
            if re.search(rf"@{re.escape(bot_name)}[ \t]+ignore\b", pr_body, re.IGNORECASE):
                logger.info("PR ignored via @%s ignore in description", bot_name)
                return Response(
                    content='{"status": "ignored"}',
                    status_code=200,
                    media_type="application/json",
                )

            pr_labels = payload.get("pull_request", {}).get("labels", [])
            if any(lbl.get("name") == PAUSE_LABEL for lbl in pr_labels):
                logger.info("PR paused via %s label", PAUSE_LABEL)
                return Response(
                    content='{"status": "paused"}',
                    status_code=200,
                    media_type="application/json",
                )

            background_tasks.add_task(handle_pull_request, payload, app_auth, bot_name)
            return Response(
                content='{"status": "processing"}',
                status_code=200,
                media_type="application/json",
            )

        if event == "issue_comment" and action == "created":
            comment_body: str = payload.get("comment", {}).get("body", "")
            comment_user: str = payload.get("comment", {}).get("user", {}).get("login", "")
            comment_user_type: str = payload.get("comment", {}).get("user", {}).get("type", "")
            is_pr = "pull_request" in payload.get("issue", {})

            # Ignore ANY bot-authored comment. This prevents self-triggering
            # loops where our own PR walkthrough contains "@{bot_name}" in a
            # footer hint and the webhook mistakes it for a user mention. The
            # exact-login check below remains as a secondary safeguard in case
            # `user.type` is absent on some event payloads.
            if comment_user_type == "Bot" or comment_user == f"{bot_name}[bot]":
                logger.debug("Ignoring comment from bot (%s)", comment_user)
                return Response(
                    content='{"status": "ignored"}',
                    status_code=200,
                    media_type="application/json",
                )

            if is_pr and f"@{bot_name}" in comment_body:
                # Extract command word after @bot_name
                cmd_match = re.search(
                    rf"@{re.escape(bot_name)}\s+(\w+)", comment_body, re.IGNORECASE
                )
                cmd_word = cmd_match.group(1).lower() if cmd_match else ""

                if cmd_word in _PAUSE_KEYWORDS | _RESUME_KEYWORDS:
                    background_tasks.add_task(
                        handle_pause_resume,
                        payload,
                        app_auth,
                        bot_name,
                        cmd_word,
                    )
                    return Response(
                        content='{"status": "processing"}',
                        status_code=200,
                        media_type="application/json",
                    )

                background_tasks.add_task(handle_comment, payload, app_auth, bot_name)
                return Response(
                    content='{"status": "processing"}',
                    status_code=200,
                    media_type="application/json",
                )

        if event == "pull_request_review_comment" and action == "created":
            rc_body: str = payload.get("comment", {}).get("body", "")
            rc_user: str = payload.get("comment", {}).get("user", {}).get("login", "")
            rc_user_type: str = payload.get("comment", {}).get("user", {}).get("type", "")

            if rc_user_type == "Bot" or rc_user == f"{bot_name}[bot]":
                logger.debug("Ignoring review comment from bot (%s)", rc_user)
                return Response(
                    content='{"status": "ignored"}',
                    status_code=200,
                    media_type="application/json",
                )

            if f"@{bot_name}" in rc_body:
                background_tasks.add_task(handle_thread_reject, payload, app_auth, bot_name)
                return Response(
                    content='{"status": "processing"}',
                    status_code=200,
                    media_type="application/json",
                )

        # Indexing events: installation, repos added, push to default branch
        if event == "installation" and action == "created":
            background_tasks.add_task(handle_installation, payload, app_auth, bot_name)
            return Response(
                content='{"status": "processing"}',
                status_code=200,
                media_type="application/json",
            )

        if event == "installation" and action == "deleted":
            background_tasks.add_task(handle_installation_deleted, payload, app_auth, bot_name)
            return Response(
                content='{"status": "processing"}',
                status_code=200,
                media_type="application/json",
            )

        if event == "installation_repositories" and action == "added":
            background_tasks.add_task(handle_repos_added, payload, app_auth, bot_name)
            return Response(
                content='{"status": "processing"}',
                status_code=200,
                media_type="application/json",
            )

        if event == "installation_repositories" and action == "removed":
            background_tasks.add_task(handle_repos_removed, payload, app_auth, bot_name)
            return Response(
                content='{"status": "processing"}',
                status_code=200,
                media_type="application/json",
            )

        if event == "push":
            ref = payload.get("ref", "")
            default_branch = payload.get("repository", {}).get("default_branch", "main")
            if ref == f"refs/heads/{default_branch}":
                background_tasks.add_task(handle_push_index, payload, app_auth, bot_name)
                return Response(
                    content='{"status": "processing"}',
                    status_code=200,
                    media_type="application/json",
                )

        return Response(
            content='{"status": "ignored"}',
            status_code=200,
            media_type="application/json",
        )

    return app
