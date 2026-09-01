"""The session cookie gets the Secure flag when served over HTTPS."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mira.dashboard.auth import create_auth_router
from mira.dashboard.db import AppDatabase


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(create_auth_router(AppDatabase(url="", admin_password="admin")))
    return app


def _login_cookie_header(app: FastAPI, base_url: str) -> str:
    client = TestClient(app, base_url=base_url)
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert resp.status_code == 200
    header = resp.headers.get("set-cookie", "")
    assert "mira_session=" in header
    return header


def test_cookie_not_secure_over_http(app: FastAPI) -> None:
    header = _login_cookie_header(app, "http://testserver")
    assert "Secure" not in header


def test_cookie_secure_over_https(app: FastAPI) -> None:
    header = _login_cookie_header(app, "https://testserver")
    assert "Secure" in header
    assert "HttpOnly" in header
