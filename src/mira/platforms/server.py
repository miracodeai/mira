"""FastAPI webhook server — serves GitHub and/or GitLab plus the dashboard UI.

Each platform's events are verified and dispatched by its own module
(``platforms.github.webhook`` / ``platforms.gitlab.webhook``); this file is just
the thin wiring: two symmetric routes (verify → dispatch → response), the
background lifespan, and the dashboard + SPA mount.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from mira.platforms.github.auth import GitHubAppAuth
from mira.platforms.github.webhook import (
    _verify_signature,
    backfill_missing_indexes,
    dispatch_github_event,
)

logger = logging.getLogger(__name__)

_SAFE_BOT_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")


def _json_status(status: str) -> Response:
    return Response(
        content=f'{{"status": "{status}"}}', status_code=200, media_type="application/json"
    )


def create_app(
    app_auth: GitHubAppAuth | None = None,
    webhook_secret: str | None = None,
    bot_name: str = "miracodeai",
    *,
    gitlab_auth: Any = None,
    gitlab_webhook_secret: str | None = None,
    forgejo_auth: Any = None,
    forgejo_webhook_secret: str | None = None,
) -> FastAPI:
    """Create the FastAPI app. GitHub (``app_auth`` + ``webhook_secret``) and
    GitLab (``gitlab_auth`` + ``gitlab_webhook_secret``) routes each activate
    only when their creds are supplied, so a deployment can serve one or both.
    """
    if not _SAFE_BOT_NAME.match(bot_name):
        raise ValueError(f"Invalid bot_name {bot_name!r}: must match [a-zA-Z0-9_-]+")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Backfill is a GitHub-installation concept; skip it on GitLab-only.
        backfill_task = (
            asyncio.create_task(backfill_missing_indexes(app_auth))
            if app_auth is not None
            else None
        )
        if backfill_task is not None:
            backfill_task.add_done_callback(
                lambda t: (
                    logger.warning("Backfill failed: %s", t.exception()) if t.exception() else None
                )
            )

        # GitLab's equivalent: discover the projects the token can access and
        # register them so they're in the dashboard ready to index up front.
        if gitlab_auth is not None:
            from mira.platforms.gitlab.webhook import backfill_gitlab_projects

            gl_task = asyncio.create_task(backfill_gitlab_projects(gitlab_auth))
            gl_task.add_done_callback(
                lambda t: (
                    logger.warning("GitLab discovery failed: %s", t.exception())
                    if t.exception()
                    else None
                )
            )

        # Forgejo's equivalent: discover repos the token can access and
        # register them so they're in the dashboard ready to index up front.
        if forgejo_auth is not None:
            from mira.platforms.forgejo.webhook import backfill_forgejo_repos

            fj_task = asyncio.create_task(backfill_forgejo_repos(forgejo_auth))
            fj_task.add_done_callback(
                lambda t: (
                    logger.warning("Forgejo discovery failed: %s", t.exception())
                    if t.exception()
                    else None
                )
            )

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
        if backfill_task is not None and not backfill_task.done():
            backfill_task.cancel()
        if forgejo_auth is not None and not fj_task.done():
            fj_task.cancel()
        if not vuln_task.done():
            vuln_task.cancel()

    app = FastAPI(title="Mira", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        db_url = os.environ.get("DATABASE_URL", "")
        if db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
            from mira.db.postgres import connect

            conn = None
            try:
                conn = connect(db_url)
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
            except Exception:
                logger.exception("Health check: database unavailable")
                raise HTTPException(status_code=503, detail="database unavailable") from None
            finally:
                if conn is not None:
                    with suppress(Exception):
                        conn.close()
        return {"status": "ok"}

    # `/webhook` is a deprecated alias from before the `/github/webhook` rename.
    @app.post("/github/webhook")
    @app.post("/webhook")
    async def github_webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
        if app_auth is None or webhook_secret is None:
            raise HTTPException(status_code=404)
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
        status = await dispatch_github_event(event, payload, app_auth, bot_name, background_tasks)
        return _json_status(status)

    if gitlab_auth is not None and gitlab_webhook_secret is not None:
        from mira.platforms.gitlab.webhook import dispatch_gitlab_event, verify_gitlab_token

        @app.post("/gitlab/webhook")
        async def gitlab_webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
            token = request.headers.get("X-Gitlab-Token", "")
            if not verify_gitlab_token(token, gitlab_webhook_secret):
                return Response(
                    content='{"error": "invalid token"}',
                    status_code=401,
                    media_type="application/json",
                )
            event = request.headers.get("X-Gitlab-Event", "")
            payload = await request.json()
            status = await dispatch_gitlab_event(
                event, payload, gitlab_auth, bot_name, background_tasks
            )
            return _json_status(status)

    if forgejo_auth is not None and forgejo_webhook_secret is not None:
        from mira.platforms.forgejo.webhook import (
            dispatch_forgejo_event,
            verify_forgejo_signature,
        )

        @app.post("/forgejo/webhook")
        async def forgejo_webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
            body = await request.body()
            signature = request.headers.get("X-Forgejo-Signature", "")
            if not verify_forgejo_signature(signature, body, forgejo_webhook_secret):
                return Response(
                    content='{"error": "invalid signature"}',
                    status_code=401,
                    media_type="application/json",
                )
            event = request.headers.get("X-Forgejo-Event", "")
            payload = await request.json()
            status = await dispatch_forgejo_event(
                event, payload, forgejo_auth, bot_name, background_tasks
            )
            return _json_status(status)

    from mira.dashboard.api import register_dashboard

    register_dashboard(app)

    # UI dist resolution order: env override → Docker image path → repo-local.
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
        assets_dir = ui_dist / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

        index_html = ui_dist / "index.html"

        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str) -> Response:
            # Don't let the SPA shell swallow misspelled API/webhook paths.
            if full_path.startswith("api/") or full_path in {"webhook", "health"}:
                raise HTTPException(status_code=404)
            file_path = ui_dist / full_path
            if file_path.is_file():
                return FileResponse(file_path)
            return FileResponse(index_html)

    return app
