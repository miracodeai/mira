"""Tests for indexing webhook handlers routing."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from mira.github_app.auth import GitHubAppAuth
from mira.github_app.webhooks import create_app

WEBHOOK_SECRET = "test-secret-123"
BOT_NAME = "mira-bot"


@pytest.fixture
def app_auth() -> GitHubAppAuth:
    return GitHubAppAuth(app_id="12345", private_key="fake-key")


@pytest.fixture
def app(app_auth: GitHubAppAuth):
    return create_app(app_auth=app_auth, webhook_secret=WEBHOOK_SECRET, bot_name=BOT_NAME)


@pytest.fixture
async def client(app) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _sign(payload_bytes: bytes) -> str:
    sig = hmac.new(WEBHOOK_SECRET.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


@pytest.mark.asyncio
class TestInstallationWebhook:
    async def test_installation_created_triggers_indexing(self, client):
        payload = {
            "action": "created",
            "installation": {"id": 1},
            "repositories": [
                {"full_name": "testowner/testrepo"},
            ],
        }
        body = json.dumps(payload).encode()

        with patch("mira.github_app.webhooks.handle_installation") as mock_handler:
            resp = await client.post(
                "/webhook",
                content=body,
                headers={
                    "X-GitHub-Event": "installation",
                    "X-Hub-Signature-256": _sign(body),
                },
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "processing"
        mock_handler.assert_called_once()

    async def test_installation_deleted_ignored(self, client):
        payload = {
            "action": "deleted",
            "installation": {"id": 1},
        }
        body = json.dumps(payload).encode()

        resp = await client.post(
            "/webhook",
            content=body,
            headers={
                "X-GitHub-Event": "installation",
                "X-Hub-Signature-256": _sign(body),
            },
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
class TestReposAddedWebhook:
    async def test_repos_added_triggers_indexing(self, client):
        payload = {
            "action": "added",
            "installation": {"id": 1},
            "repositories_added": [
                {"full_name": "testowner/newrepo"},
            ],
        }
        body = json.dumps(payload).encode()

        with patch("mira.github_app.webhooks.handle_repos_added") as mock_handler:
            resp = await client.post(
                "/webhook",
                content=body,
                headers={
                    "X-GitHub-Event": "installation_repositories",
                    "X-Hub-Signature-256": _sign(body),
                },
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "processing"
        mock_handler.assert_called_once()


@pytest.mark.asyncio
class TestPushWebhook:
    async def test_push_to_default_branch_triggers_indexing(self, client):
        payload = {
            "ref": "refs/heads/main",
            "installation": {"id": 1},
            "repository": {
                "owner": {"login": "testowner"},
                "name": "testrepo",
                "default_branch": "main",
            },
            "commits": [
                {"added": ["new.py"], "modified": ["changed.py"], "removed": []},
            ],
        }
        body = json.dumps(payload).encode()

        with patch("mira.github_app.webhooks.handle_push_index") as mock_handler:
            resp = await client.post(
                "/webhook",
                content=body,
                headers={
                    "X-GitHub-Event": "push",
                    "X-Hub-Signature-256": _sign(body),
                },
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "processing"
        mock_handler.assert_called_once()

    async def test_push_to_feature_branch_ignored(self, client):
        payload = {
            "ref": "refs/heads/feature/something",
            "installation": {"id": 1},
            "repository": {
                "owner": {"login": "testowner"},
                "name": "testrepo",
                "default_branch": "main",
            },
            "commits": [],
        }
        body = json.dumps(payload).encode()

        resp = await client.post(
            "/webhook",
            content=body,
            headers={
                "X-GitHub-Event": "push",
                "X-Hub-Signature-256": _sign(body),
            },
        )

        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
