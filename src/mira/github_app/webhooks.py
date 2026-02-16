"""FastAPI webhook server for the Mira GitHub App."""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request, Response

from mira.github_app.auth import GitHubAppAuth
from mira.github_app.handlers import handle_comment, handle_pull_request, handle_thread_reject
from mira.github_app.metrics import Metrics

logger = logging.getLogger(__name__)

_PR_ACTIONS = {"opened", "synchronize", "reopened"}


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
    metrics: Metrics | None = None,
) -> FastAPI:
    """Create and configure the FastAPI webhook application."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if metrics:
            metrics.track("server_started", installation_id=0)
        yield
        if metrics:
            metrics.shutdown()

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
        installation_id: int = payload.get("installation", {}).get("id", 0)

        if event == "pull_request" and action in _PR_ACTIONS:
            sender: str = payload.get("sender", {}).get("login", "")
            if sender == f"{bot_name}[bot]":
                logger.debug("Ignoring pull_request event from self (%s)", sender)
                return Response(
                    content='{"status": "ignored"}',
                    status_code=200,
                    media_type="application/json",
                )
            if metrics:
                metrics.track(
                    "webhook_received",
                    installation_id=installation_id,
                    properties={"event_type": event, "action": action, "status": "processing"},
                )
            background_tasks.add_task(handle_pull_request, payload, app_auth, bot_name, metrics)
            return Response(
                content='{"status": "processing"}',
                status_code=200,
                media_type="application/json",
            )

        if event == "issue_comment" and action == "created":
            comment_body: str = payload.get("comment", {}).get("body", "")
            comment_user: str = payload.get("comment", {}).get("user", {}).get("login", "")
            is_pr = "pull_request" in payload.get("issue", {})

            # Ignore comments authored by this bot to prevent self-triggering loops
            if comment_user == f"{bot_name}[bot]":
                logger.debug("Ignoring comment from self (%s)", comment_user)
                return Response(
                    content='{"status": "ignored"}',
                    status_code=200,
                    media_type="application/json",
                )

            if is_pr and f"@{bot_name}" in comment_body:
                if metrics:
                    metrics.track(
                        "webhook_received",
                        installation_id=installation_id,
                        properties={"event_type": event, "action": action, "status": "processing"},
                    )
                background_tasks.add_task(handle_comment, payload, app_auth, bot_name, metrics)
                return Response(
                    content='{"status": "processing"}',
                    status_code=200,
                    media_type="application/json",
                )

        if event == "pull_request_review_comment" and action == "created":
            rc_body: str = payload.get("comment", {}).get("body", "")
            rc_user: str = payload.get("comment", {}).get("user", {}).get("login", "")

            if rc_user == f"{bot_name}[bot]":
                logger.debug("Ignoring review comment from self (%s)", rc_user)
                return Response(
                    content='{"status": "ignored"}',
                    status_code=200,
                    media_type="application/json",
                )

            if f"@{bot_name}" in rc_body:
                if metrics:
                    metrics.track(
                        "webhook_received",
                        installation_id=installation_id,
                        properties={"event_type": event, "action": action, "status": "processing"},
                    )
                background_tasks.add_task(
                    handle_thread_reject, payload, app_auth, bot_name, metrics
                )
                return Response(
                    content='{"status": "processing"}',
                    status_code=200,
                    media_type="application/json",
                )

        if metrics:
            metrics.track(
                "webhook_received",
                installation_id=installation_id,
                properties={"event_type": event, "action": action, "status": "ignored"},
            )
        return Response(
            content='{"status": "ignored"}',
            status_code=200,
            media_type="application/json",
        )

    return app
