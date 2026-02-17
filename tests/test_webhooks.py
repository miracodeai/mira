"""Tests for the FastAPI webhook server."""

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
def app(app_auth: GitHubAppAuth):  # noqa: ANN201
    return create_app(app_auth=app_auth, webhook_secret=WEBHOOK_SECRET, bot_name=BOT_NAME)


@pytest.fixture
async def client(app) -> AsyncClient:  # noqa: ANN001
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _sign(payload_bytes: bytes) -> str:
    """Compute the X-Hub-Signature-256 for a payload."""
    sig = hmac.new(WEBHOOK_SECRET.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def _pr_opened_payload() -> dict:
    return {
        "action": "opened",
        "installation": {"id": 1},
        "pull_request": {"number": 42},
        "repository": {
            "owner": {"login": "testowner"},
            "name": "testrepo",
        },
    }


def _comment_payload(body: str, is_pr: bool = True) -> dict:
    issue: dict = {"number": 7}
    if is_pr:
        issue["pull_request"] = {"url": "https://api.github.com/repos/o/r/pulls/7"}
    return {
        "action": "created",
        "installation": {"id": 1},
        "comment": {"body": body, "user": {"login": "alice"}},
        "issue": issue,
        "repository": {
            "owner": {"login": "testowner"},
            "name": "testrepo",
        },
    }


async def test_health(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_invalid_signature(client: AsyncClient) -> None:
    payload = json.dumps({"action": "opened"}).encode()
    resp = await client.post(
        "/webhook",
        content=payload,
        headers={
            "X-Hub-Signature-256": "sha256=invalid",
            "X-GitHub-Event": "pull_request",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401


@patch("mira.github_app.webhooks.handle_pull_request", new_callable=AsyncMock)
async def test_pr_opened_triggers_handler(mock_handler: AsyncMock, client: AsyncClient) -> None:
    payload_bytes = json.dumps(_pr_opened_payload()).encode()
    resp = await client.post(
        "/webhook",
        content=payload_bytes,
        headers={
            "X-Hub-Signature-256": _sign(payload_bytes),
            "X-GitHub-Event": "pull_request",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "processing"
    # BackgroundTasks runs synchronously in test, so handler should have been called
    mock_handler.assert_awaited_once()


async def test_pr_closed_ignored(client: AsyncClient) -> None:
    payload = {"action": "closed", "installation": {"id": 1}}
    payload_bytes = json.dumps(payload).encode()
    resp = await client.post(
        "/webhook",
        content=payload_bytes,
        headers={
            "X-Hub-Signature-256": _sign(payload_bytes),
            "X-GitHub-Event": "pull_request",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


@patch("mira.github_app.webhooks.handle_comment", new_callable=AsyncMock)
async def test_comment_with_mention_triggers_handler(
    mock_handler: AsyncMock, client: AsyncClient
) -> None:
    payload = _comment_payload(f"@{BOT_NAME} why is this slow?")
    payload_bytes = json.dumps(payload).encode()
    resp = await client.post(
        "/webhook",
        content=payload_bytes,
        headers={
            "X-Hub-Signature-256": _sign(payload_bytes),
            "X-GitHub-Event": "issue_comment",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "processing"
    mock_handler.assert_awaited_once()


async def test_comment_without_mention_ignored(client: AsyncClient) -> None:
    payload = _comment_payload("Just a regular comment")
    payload_bytes = json.dumps(payload).encode()
    resp = await client.post(
        "/webhook",
        content=payload_bytes,
        headers={
            "X-Hub-Signature-256": _sign(payload_bytes),
            "X-GitHub-Event": "issue_comment",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


async def test_comment_on_issue_not_pr_ignored(client: AsyncClient) -> None:
    payload = _comment_payload(f"@{BOT_NAME} help", is_pr=False)
    payload_bytes = json.dumps(payload).encode()
    resp = await client.post(
        "/webhook",
        content=payload_bytes,
        headers={
            "X-Hub-Signature-256": _sign(payload_bytes),
            "X-GitHub-Event": "issue_comment",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


# ── pull_request_review_comment tests ────────────────────────────────────────


def _review_comment_payload(body: str, user: str = "alice") -> dict:
    return {
        "action": "created",
        "installation": {"id": 1},
        "comment": {
            "body": body,
            "node_id": "MDI0Ol_abc",
            "user": {"login": user},
        },
        "pull_request": {"number": 42},
        "repository": {
            "owner": {"login": "testowner"},
            "name": "testrepo",
        },
    }


@patch("mira.github_app.webhooks.handle_thread_reject", new_callable=AsyncMock)
async def test_review_comment_reject_triggers_handler(
    mock_handler: AsyncMock, client: AsyncClient
) -> None:
    payload = _review_comment_payload(f"@{BOT_NAME} reject")
    payload_bytes = json.dumps(payload).encode()
    resp = await client.post(
        "/webhook",
        content=payload_bytes,
        headers={
            "X-Hub-Signature-256": _sign(payload_bytes),
            "X-GitHub-Event": "pull_request_review_comment",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "processing"
    mock_handler.assert_awaited_once()


async def test_review_comment_without_mention_ignored(client: AsyncClient) -> None:
    payload = _review_comment_payload("Just a regular reply")
    payload_bytes = json.dumps(payload).encode()
    resp = await client.post(
        "/webhook",
        content=payload_bytes,
        headers={
            "X-Hub-Signature-256": _sign(payload_bytes),
            "X-GitHub-Event": "pull_request_review_comment",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


async def test_review_comment_from_bot_self_ignored(client: AsyncClient) -> None:
    payload = _review_comment_payload(f"@{BOT_NAME} reject", user=f"{BOT_NAME}[bot]")
    payload_bytes = json.dumps(payload).encode()
    resp = await client.post(
        "/webhook",
        content=payload_bytes,
        headers={
            "X-Hub-Signature-256": _sign(payload_bytes),
            "X-GitHub-Event": "pull_request_review_comment",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"
