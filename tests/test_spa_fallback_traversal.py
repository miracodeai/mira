"""Path-traversal regression for the SPA catch-all fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from mira.platforms.github.auth import GitHubAppAuth
from mira.platforms.server import create_app


async def _raw_get(app, path: str) -> tuple[int, bytes]:
    """Drive the ASGI app with a raw path, the way `curl --path-as-is` does."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
    }
    body = bytearray()
    status = {"code": 0}
    messages = [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive():
        return messages.pop(0)

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    await app(scope, receive, send)
    return status["code"], bytes(body)


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    ui_dist = tmp_path / "dist"
    ui_dist.mkdir()
    (ui_dist / "index.html").write_text("SPA INDEX")
    (ui_dist / "app.js").write_text("REAL ASSET")
    (tmp_path / "secret.env").write_text("TOP SECRET")

    monkeypatch.setenv("MIRA_UI_DIST", str(ui_dist))
    return create_app(
        app_auth=GitHubAppAuth(app_id="12345", private_key="fake-key"),
        webhook_secret="test-secret-123",
        bot_name="mira-bot",
    )


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.env",
        "/%2e%2e/secret.env",
        "/../../../../../../../../etc/passwd",
    ],
)
async def test_traversal_falls_back_to_index(app, path: str) -> None:
    status, body = await _raw_get(app, path)
    assert status == 200
    assert b"TOP SECRET" not in body
    assert b"root:" not in body
    assert body == b"SPA INDEX"


async def test_real_asset_still_served(app) -> None:
    status, body = await _raw_get(app, "/app.js")
    assert status == 200
    assert body == b"REAL ASSET"
