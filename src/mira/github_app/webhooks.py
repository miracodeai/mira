"""FastAPI webhook server for the Mira GitHub App."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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

    # Both paths route to the same handler.
    # `/github/webhook` is the canonical path going forward — the namespace
    # leaves room for `/gitlab/webhook`, `/bitbucket/webhook`, etc. without
    # breaking GitHub installations.
    # `/webhook` stays as a deprecated alias so existing GitHub Apps that
    # were created before the rename keep working.
    @app.post("/github/webhook")
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

    # ── Dashboard API + UI ───────────────────────────────────────
    # Register all `/api/*` dashboard routes onto this same app, so a single
    # process serves webhooks, REST API, and the bundled UI.
    from mira.dashboard.api import register_dashboard

    register_dashboard(app)

    # Serve the built UI as static files with SPA fallback. The Dockerfile
    # builds the React app into `ui/mira/dist/` and copies it next to the
    # backend at /app/ui_dist; locally, fall back to ui/mira/dist relative to
    # the repo root. If neither exists, the API still works — only the UI
    # routes will 404.
    ui_dist_env = os.environ.get("MIRA_UI_DIST")
    candidates: list[Path] = []
    if ui_dist_env:
        candidates.append(Path(ui_dist_env))
    candidates.extend(
        [
            Path("/app/ui_dist"),
            Path(__file__).resolve().parents[3] / "ui" / "mira" / "dist",
        ]
    )
    ui_dist = next((p for p in candidates if p.is_dir()), None)

    if ui_dist is not None:
        # Mount static assets at /assets so the UI's bundled JS/CSS are served.
        assets_dir = ui_dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        index_html = ui_dist / "index.html"

        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str) -> Response:
            # Reserve API/webhook namespaces — return 404 instead of swallowing
            # them with the SPA shell. Webhook + dashboard routes are
            # registered above and take precedence by route ordering, but a
            # bad path like /api/nonexistent should 404 cleanly.
            if full_path.startswith("api/") or full_path in {"webhook", "health"}:
                raise HTTPException(status_code=404)
            file_path = ui_dist / full_path
            if file_path.is_file():
                return FileResponse(file_path)
            return FileResponse(index_html)

    return app
