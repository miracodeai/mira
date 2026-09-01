"""Tests for the Forgejo webhook route + author filter dispatch."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from mira.config import FilterConfig, MiraConfig
from mira.platforms.forgejo.auth import ForgejoTokenAuth
from mira.platforms.server import create_app

FJ_SECRET = "fj-secret"
BOT = "mira-bot"


@pytest.fixture
def forgejo_auth():  # noqa: ANN201
    auth = ForgejoTokenAuth("tok")
    auth.get_bot_identity = AsyncMock(return_value="mira-bot")  # type: ignore[method-assign]
    return auth


@pytest.fixture
def app(forgejo_auth):  # noqa: ANN001, ANN201
    return create_app(
        app_auth=None,
        webhook_secret=None,
        bot_name=BOT,
        forgejo_auth=forgejo_auth,
        forgejo_webhook_secret=FJ_SECRET,
    )


@pytest.fixture
async def client(app) -> AsyncClient:  # noqa: ANN001
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _sign(payload_bytes: bytes) -> str:
    return hmac.new(FJ_SECRET.encode(), payload_bytes, hashlib.sha256).hexdigest()


def _pr_payload(action: str, login: str):
    return {
        "action": action,
        "pull_request": {"number": 7},
        "repository": {"full_name": "o/r", "private": False},
        "sender": {"login": login},
    }


def _comment_payload(body: str, login: str):
    return {
        "action": "created",
        "is_pull": True,
        "comment": {"body": body},
        "sender": {"login": login},
    }


@pytest.mark.asyncio
async def test_pr_opened_blocked_author_filtered(client):
    """Blocked author with [bot] suffix is filtered for PR opened."""
    with (
        patch("mira.platforms.forgejo.webhook.handle_forgejo_pr", new=AsyncMock()) as h,
        patch(
            "mira.platforms.forgejo.webhook.load_config",
            return_value=MiraConfig(filter=FilterConfig(blocked_authors=["dependabot"])),
        ),
    ):
        payload = _pr_payload("opened", "dependabot[bot]")
        body = json.dumps(payload).encode()
        resp = await client.post(
            "/forgejo/webhook",
            content=body,
            headers={
                "X-Forgejo-Event": "pull_request",
                "X-Forgejo-Signature": _sign(body),
            },
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    h.assert_not_called()


@pytest.mark.asyncio
async def test_pr_opened_allowed_author_not_filtered(client):
    """Non-blocked author passes through the filter."""
    with (
        patch("mira.platforms.forgejo.webhook.handle_forgejo_pr", new=AsyncMock()) as h,
        patch(
            "mira.platforms.forgejo.webhook.load_config",
            return_value=MiraConfig(filter=FilterConfig(blocked_authors=["dependabot"])),
        ),
    ):
        payload = _pr_payload("opened", "alice")
        body = json.dumps(payload).encode()
        resp = await client.post(
            "/forgejo/webhook",
            content=body,
            headers={
                "X-Forgejo-Event": "pull_request",
                "X-Forgejo-Signature": _sign(body),
            },
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "processing"
    h.assert_awaited_once()


@pytest.mark.asyncio
async def test_pr_opened_allowlist_filters_off_list(client):
    """Allowlist set but author not on it → filtered."""
    with (
        patch("mira.platforms.forgejo.webhook.handle_forgejo_pr", new=AsyncMock()) as h,
        patch(
            "mira.platforms.forgejo.webhook.load_config",
            return_value=MiraConfig(filter=FilterConfig(allowed_authors=["alice"])),
        ),
    ):
        payload = _pr_payload("opened", "bob")
        body = json.dumps(payload).encode()
        resp = await client.post(
            "/forgejo/webhook",
            content=body,
            headers={
                "X-Forgejo-Event": "pull_request",
                "X-Forgejo-Signature": _sign(body),
            },
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    h.assert_not_called()


@pytest.mark.asyncio
async def test_comment_review_bypass(client):
    """Manual @mira-bot review comment bypasses the author filter."""
    with (
        patch("mira.platforms.forgejo.webhook.handle_forgejo_note", new=AsyncMock()) as h,
        patch(
            "mira.platforms.forgejo.webhook.load_config",
            return_value=MiraConfig(filter=FilterConfig(blocked_authors=["dependabot"])),
        ),
    ):
        payload = _comment_payload("@mira-bot review", "dependabot[bot]")
        body = json.dumps(payload).encode()
        resp = await client.post(
            "/forgejo/webhook",
            content=body,
            headers={
                "X-Forgejo-Event": "issue_comment",
                "X-Forgejo-Signature": _sign(body),
            },
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "processing"
    h.assert_awaited_once()


@pytest.mark.asyncio
async def test_comment_non_review_no_bypass(client):
    """Non-review command (pause) by blocked author does NOT bypass filter."""
    with (
        patch("mira.platforms.forgejo.webhook.handle_forgejo_note", new=AsyncMock()) as h,
        patch(
            "mira.platforms.forgejo.webhook.load_config",
            return_value=MiraConfig(filter=FilterConfig(blocked_authors=["alice"])),
        ),
    ):
        payload = _comment_payload("@mira-bot pause", "alice")
        body = json.dumps(payload).encode()
        resp = await client.post(
            "/forgejo/webhook",
            content=body,
            headers={
                "X-Forgejo-Event": "issue_comment",
                "X-Forgejo-Signature": _sign(body),
            },
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
    h.assert_not_called()
