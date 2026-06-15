"""Tests for outbound webhook notifications.

Covers the pure helpers (format detection, URL masking, payload rendering),
guarded delivery/dispatch with a mocked httpx client, and the admin CRUD/test
endpoints (mirroring the fixture pattern in test_admin_settings.py).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from mira import outbound_webhooks as nf
from mira.dashboard.api import (
    WebhookCreate,
    WebhookUpdate,
    create_webhook,
    delete_webhook,
    get_webhook,
    list_webhooks,
    update_webhook,
)
from mira.dashboard.api import test_webhook as run_webhook_test
from mira.dashboard.db import AppDatabase


@pytest.fixture
def in_memory_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    db = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr("mira.dashboard.api._app_db", db)
    return db


def _admin() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(is_admin=True)))


def _non_admin() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(is_admin=False)))


def _mock_httpx(status: int = 200, side_effect=None):
    """Return (factory, client) to patch `mira.outbound_webhooks.httpx.AsyncClient`."""
    client = MagicMock()
    if side_effect is not None:
        client.post = AsyncMock(side_effect=side_effect)
    else:
        client.post = AsyncMock(return_value=SimpleNamespace(status_code=status))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=cm), client


# ── Pure helpers ─────────────────────────────────────────────────────────────


class TestFormatDetection:
    def test_slack(self):
        assert nf.detect_format("https://hooks.slack.com/services/T/B/X") == "slack"

    def test_teams_connector(self):
        assert nf.detect_format("https://acme.webhook.office.com/webhookb2/x") == "teams"

    def test_teams_workflow(self):
        assert nf.detect_format("https://prod-1.westus.logic.azure.com/workflows/x") == "teams"

    def test_generic(self):
        assert nf.detect_format("https://example.com/hook") == "generic"

    def test_teams_lookalike_host_is_not_teams(self):
        # Subdomain-confusion guard: must not match without the leading dot.
        assert nf.detect_format("https://evilwebhook.office.com/x") == "generic"

    def test_mask_hides_middle(self):
        masked = nf.mask_url("https://hooks.slack.com/services/T0/B0/abcd1234")
        assert masked == "https://hooks.slack.com/…1234"
        assert "abcd" not in masked


class TestRender:
    def test_slack_payload_shape(self):
        body = nf.render(nf.REVIEW_COMPLETED, nf.sample_data(nf.REVIEW_COMPLETED), "slack")
        assert "text" in body and "blocks" in body
        assert body["blocks"][0]["text"]["type"] == "mrkdwn"

    def test_teams_payload_shape(self):
        body = nf.render(nf.REVIEW_HIGH_SEVERITY, nf.sample_data(nf.REVIEW_COMPLETED), "teams")
        assert body["@type"] == "MessageCard"
        assert "title" in body and "themeColor" in body

    def test_generic_envelope(self):
        data = nf.sample_data(nf.INDEXING_COMPLETED)
        body = nf.render(nf.INDEXING_COMPLETED, data, "generic")
        assert body["event"] == nf.INDEXING_COMPLETED
        assert body["data"] == data
        assert "timestamp" in body


# ── Delivery / dispatch ──────────────────────────────────────────────────────


class TestDeliverOne:
    async def test_success(self):
        factory, client = _mock_httpx(status=200)
        with patch("mira.outbound_webhooks.httpx.AsyncClient", factory):
            ok, detail = await nf.deliver_one(
                {"url": "https://example.com/h"}, nf.REVIEW_COMPLETED, {}
            )
        assert ok is True
        assert detail == "HTTP 200"
        assert client.post.call_count == 1

    async def test_client_error_no_retry(self):
        factory, client = _mock_httpx(status=404)
        with patch("mira.outbound_webhooks.httpx.AsyncClient", factory):
            ok, detail = await nf.deliver_one(
                {"url": "https://example.com/h"}, nf.REVIEW_COMPLETED, {}
            )
        assert ok is False
        assert detail == "HTTP 404"
        assert client.post.call_count == 1  # 4xx is not retried

    async def test_server_error_retries_once(self):
        factory, client = _mock_httpx(status=500)
        with patch("mira.outbound_webhooks.httpx.AsyncClient", factory):
            ok, _ = await nf.deliver_one({"url": "https://example.com/h"}, nf.REVIEW_COMPLETED, {})
        assert ok is False
        assert client.post.call_count == 2  # 5xx retried once

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8080/x",
            "http://127.0.0.1/x",
            "http://169.254.169.254/latest/meta-data",
            "http://10.0.0.5/x",
            "http://192.168.1.10/x",
            "http://[::1]/x",
        ],
    )
    async def test_internal_url_blocked_without_request(self, url):
        factory, client = _mock_httpx(status=200)
        with patch("mira.outbound_webhooks.httpx.AsyncClient", factory):
            ok, detail = await nf.deliver_one({"url": url}, nf.REVIEW_COMPLETED, {})
        assert ok is False
        assert "private or internal" in detail
        assert client.post.call_count == 0  # never even attempted

    def test_safe_url_helper(self):
        assert nf.is_safe_webhook_url("https://hooks.slack.com/x") is True
        # Named internal hosts are allowed (not resolved); only IP literals blocked.
        assert nf.is_safe_webhook_url("http://mattermost:8065/hooks/x") is True
        assert nf.is_safe_webhook_url("http://localhost/x") is False
        assert nf.is_safe_webhook_url("http://169.254.169.254/") is False

    async def test_network_error_swallowed(self):
        factory, _ = _mock_httpx(side_effect=httpx.ConnectError("boom"))
        with patch("mira.outbound_webhooks.httpx.AsyncClient", factory):
            ok, detail = await nf.deliver_one(
                {"url": "https://example.com/h"}, nf.REVIEW_COMPLETED, {}
            )
        assert ok is False
        assert "ConnectError" in detail


class TestDispatch:
    async def test_only_enabled_and_subscribed(self, in_memory_db: AppDatabase):
        in_memory_db.set_webhooks(
            [
                {
                    "id": "a",
                    "url": "https://a.com",
                    "events": [nf.REVIEW_COMPLETED],
                    "enabled": True,
                },
                {
                    "id": "b",
                    "url": "https://b.com",
                    "events": [nf.REVIEW_COMPLETED],
                    "enabled": False,
                },
                {"id": "c", "url": "https://c.com", "events": [nf.REVIEW_FAILED], "enabled": True},
            ]
        )
        factory, client = _mock_httpx(status=200)
        with patch("mira.outbound_webhooks.httpx.AsyncClient", factory):
            await nf.dispatch_event(nf.REVIEW_COMPLETED, {"repo": "x/y"})
        # Only webhook "a" qualifies (enabled + subscribed).
        assert client.post.call_count == 1

    async def test_never_raises_on_delivery_error(self, in_memory_db: AppDatabase):
        in_memory_db.set_webhooks(
            [{"id": "a", "url": "https://a.com", "events": [nf.REVIEW_COMPLETED], "enabled": True}]
        )
        factory, _ = _mock_httpx(side_effect=httpx.ConnectError("boom"))
        with patch("mira.outbound_webhooks.httpx.AsyncClient", factory):
            # Must not raise.
            await nf.dispatch_event(nf.REVIEW_COMPLETED, {"repo": "x/y"})

    async def test_no_db_is_noop(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("mira.dashboard.api._app_db", None)
        await nf.dispatch_event(nf.REVIEW_COMPLETED, {"repo": "x/y"})  # no raise


# ── Admin endpoints ──────────────────────────────────────────────────────────


class TestAdminEndpoints:
    def test_non_admin_forbidden(self, in_memory_db: AppDatabase):
        with pytest.raises(HTTPException) as exc:
            create_webhook(
                WebhookCreate(name="x", url="https://a.com", events=[nf.REVIEW_COMPLETED]),
                _non_admin(),
            )
        assert exc.value.status_code == 403

    def test_create_masks_and_lists(self, in_memory_db: AppDatabase):
        created = create_webhook(
            WebhookCreate(
                name="eng",
                url="https://hooks.slack.com/services/T/B/secret123",
                events=[nf.REVIEW_COMPLETED],
            ),
            _admin(),
        )
        assert created["id"]
        assert created["format"] == "slack"
        assert "secret123" not in created["url_masked"]

        listing = list_webhooks(_admin())
        assert len(listing["webhooks"]) == 1
        assert {e["value"] for e in listing["available_events"]} == {
            nf.REVIEW_COMPLETED,
            nf.REVIEW_HIGH_SEVERITY,
            nf.REVIEW_FAILED,
            nf.INDEXING_COMPLETED,
        }

    def test_get_by_id_returns_full_url(self, in_memory_db: AppDatabase):
        created = create_webhook(
            WebhookCreate(
                name="eng",
                url="https://hooks.slack.com/services/T/B/secret123",
                events=[nf.REVIEW_COMPLETED],
            ),
            _admin(),
        )
        full = get_webhook(created["id"], _admin())
        # The edit endpoint returns the real URL (the list masks it).
        assert full["url"] == "https://hooks.slack.com/services/T/B/secret123"
        assert full["format"] == "slack"

    def test_get_by_id_404(self, in_memory_db: AppDatabase):
        with pytest.raises(HTTPException) as exc:
            get_webhook("nope", _admin())
        assert exc.value.status_code == 404

    def test_get_by_id_non_admin_forbidden(self, in_memory_db: AppDatabase):
        with pytest.raises(HTTPException) as exc:
            get_webhook("any", _non_admin())
        assert exc.value.status_code == 403

    def test_create_rejects_bad_url(self, in_memory_db: AppDatabase):
        with pytest.raises(HTTPException) as exc:
            create_webhook(
                WebhookCreate(name="x", url="ftp://nope", events=[nf.REVIEW_COMPLETED]),
                _admin(),
            )
        assert exc.value.status_code == 400

    def test_update_blank_url_keeps_secret(self, in_memory_db: AppDatabase):
        created = create_webhook(
            WebhookCreate(name="eng", url="https://a.com/original", events=[nf.REVIEW_COMPLETED]),
            _admin(),
        )
        update_webhook(created["id"], WebhookUpdate(events=[nf.REVIEW_FAILED], url=""), _admin())
        stored = in_memory_db.get_webhooks()[0]
        assert stored["url"] == "https://a.com/original"  # unchanged
        assert stored["events"] == [nf.REVIEW_FAILED]

    def test_delete(self, in_memory_db: AppDatabase):
        created = create_webhook(
            WebhookCreate(name="eng", url="https://a.com", events=[nf.REVIEW_COMPLETED]),
            _admin(),
        )
        delete_webhook(created["id"], _admin())
        assert in_memory_db.get_webhooks() == []
        with pytest.raises(HTTPException) as exc:
            delete_webhook(created["id"], _admin())
        assert exc.value.status_code == 404

    async def test_test_endpoint_delivers(self, in_memory_db: AppDatabase):
        created = create_webhook(
            WebhookCreate(name="eng", url="https://a.com", events=[nf.REVIEW_COMPLETED]),
            _admin(),
        )
        factory, client = _mock_httpx(status=200)
        with patch("mira.outbound_webhooks.httpx.AsyncClient", factory):
            res = await run_webhook_test(created["id"], _admin())
        assert res["ok"] is True
        assert client.post.call_count == 1
