"""AuthMiddleware public-path rules: only the badge SVG bypasses auth, not any *.svg."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from mira.dashboard.auth import SESSION_COOKIE, AuthMiddleware
from mira.dashboard.db import AppDatabase


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    app = FastAPI()
    app.add_middleware(AuthMiddleware, db=AppDatabase(url="", admin_password="pw"))

    @app.get("/{full_path:path}")
    def catch_all(full_path: str) -> dict:
        return {"ok": True}

    return TestClient(app)


def test_badge_svg_is_public(client: TestClient) -> None:
    assert client.get("/api/repos/owner/repo/blast-radius.svg").status_code == 200


def test_other_svg_paths_require_auth(client: TestClient) -> None:
    for path in (
        "/api/users.svg",
        "/api/admin/settings.svg",
        "/api/repos/owner/blast-radius.svg",
        "/api/repos/owner/repo/extra/blast-radius.svg",
    ):
        assert client.get(path).status_code == 401, path


def test_api_requires_auth_without_cookie(client: TestClient) -> None:
    assert client.get("/api/events").status_code == 401


def test_non_api_paths_are_public(client: TestClient) -> None:
    assert client.get("/login").status_code == 200


def test_badge_identifies_a_signed_in_viewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The badge stays public, but a valid session cookie still sets
    request.state.user so the handler can show a signed-in viewer more."""
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    db = AppDatabase(url="", admin_password="pw")
    app = FastAPI()
    app.add_middleware(AuthMiddleware, db=db)

    @app.get("/{full_path:path}")
    def catch_all(full_path: str, request: Request) -> dict:
        user = getattr(request.state, "user", None)
        return {"user": user.username if user else None}

    client = TestClient(app)
    badge = "/api/repos/owner/repo/blast-radius.svg"
    assert client.get(badge).json() == {"user": None}
    assert client.get(badge, cookies={SESSION_COOKIE: "bogus"}).json() == {"user": None}
    token = db.create_session(db.authenticate("admin", "pw").id)
    assert client.get(badge, cookies={SESSION_COOKIE: token}).json() == {"user": "admin"}
