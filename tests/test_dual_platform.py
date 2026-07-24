"""Both platforms wired into one app.

The per-platform webhook tests each boot a single-platform app; this one boots
``create_app`` with GitHub *and* GitLab configured together and exercises both
routes — the wiring that the platform-layer split most directly affects.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from mira.platforms.gitlab.auth import GitLabTokenAuth
from mira.platforms.server import create_app

GH_SECRET = "gh-secret"
GL_SECRET = "gl-secret"
BOT = "mira-bot"


@pytest.fixture
def app():  # noqa: ANN201
    github_auth = AsyncMock()
    github_auth.get_bot_identity = AsyncMock(return_value=BOT)
    gitlab_auth = GitLabTokenAuth("tok")
    gitlab_auth.get_bot_identity = AsyncMock(return_value=BOT)  # type: ignore[method-assign]
    return create_app(
        app_auth=github_auth,
        webhook_secret=GH_SECRET,
        bot_name=BOT,
        gitlab_auth=gitlab_auth,
        gitlab_webhook_secret=GL_SECRET,
    )


@pytest.fixture
async def client(app) -> AsyncClient:  # noqa: ANN001
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _gh_sig(body: bytes) -> str:
    return "sha256=" + hmac.new(GH_SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_health(client):
    assert (await client.get("/health")).json()["status"] == "ok"


@pytest.mark.asyncio
async def test_github_route_rejects_bad_signature(client):
    resp = await client.post(
        "/github/webhook",
        content=b"{}",
        headers={"X-Hub-Signature-256": "sha256=bad", "X-GitHub-Event": "ping"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_github_pr_event_dispatches(client):
    payload = {
        "action": "opened",
        "installation": {"id": 1},
        "sender": {"login": "alice"},
        "pull_request": {"number": 1, "body": "", "labels": []},
        "repository": {"owner": {"login": "o"}, "name": "r"},
    }
    body = json.dumps(payload).encode()
    with patch("mira.platforms.github.webhook.handle_pull_request", new=AsyncMock()) as h:
        resp = await client.post(
            "/github/webhook",
            content=body,
            headers={"X-Hub-Signature-256": _gh_sig(body), "X-GitHub-Event": "pull_request"},
        )
    assert resp.json()["status"] == "processing"
    h.assert_awaited_once()


@pytest.mark.asyncio
async def test_gitlab_route_rejects_bad_token(client):
    resp = await client.post(
        "/gitlab/webhook",
        content=b"{}",
        headers={"X-Gitlab-Token": "wrong", "X-Gitlab-Event": "Merge Request Hook"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_gitlab_mr_event_dispatches(client):
    payload = {
        "object_kind": "merge_request",
        "object_attributes": {
            "iid": 7,
            "action": "open",
            "source_branch": "f",
            "target_branch": "main",
            "url": "https://gitlab.com/g/p/-/merge_requests/7",
        },
        "project": {"path_with_namespace": "g/p", "web_url": "https://gitlab.com/g/p"},
        "user": {"username": "alice"},
    }
    with patch("mira.platforms.gitlab.webhook.handle_merge_request", new=AsyncMock()) as h:
        resp = await client.post(
            "/gitlab/webhook",
            content=json.dumps(payload),
            headers={"X-Gitlab-Token": GL_SECRET, "X-Gitlab-Event": "Merge Request Hook"},
        )
    assert resp.json()["status"] == "processing"
    h.assert_awaited_once()
