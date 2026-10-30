"""Unit tests for Origin webhook envelope + signature helpers."""

from __future__ import annotations

import base64
import hashlib
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mira.platforms.origin import webhook as wh


def test_unwrap_envelope() -> None:
    event_type, installation_id, payload = wh.unwrap_envelope(
        {
            "deliveryId": "whd_1",
            "appId": "app_1",
            "installationId": "i_99",
            "event": {
                "id": "evt_1",
                "type": "pull_request.created",
                "eventTime": "2026-01-01T00:00:00Z",
                "payload": {"pullRequest": {"number": "1"}},
            },
        }
    )
    assert event_type == "pull_request.created"
    assert installation_id == "i_99"
    assert payload["pullRequest"]["number"] == "1"


@pytest.mark.asyncio
async def test_verify_origin_signature_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    private = Ed25519PrivateKey.generate()
    public = private.public_key()

    async def fake_jwks(force: bool = False):
        return [public]

    monkeypatch.setattr(wh, "_load_jwks", fake_jwks)

    body = b'{"event":{"type":"ping"}}'
    webhook_id = "whd_test"
    ts = str(int(time.time()))
    digest = hashlib.sha256(f"{webhook_id}.{ts}.".encode() + body).hexdigest().encode()
    signature = base64.b64encode(private.sign(digest)).decode()
    header = f"v1ed,{signature}"

    assert await wh.verify_origin_signature(body, webhook_id, ts, header) is True
    assert await wh.verify_origin_signature(body, webhook_id, ts, "v1ed,AAAA") is False
    assert await wh.verify_origin_signature(body, "other", ts, header) is False
