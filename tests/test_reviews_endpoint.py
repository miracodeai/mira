"""Tests for the org-wide reviews endpoint (/api/reviews)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from mira.core.review_status import ReviewTracker
from mira.dashboard import api
from mira.dashboard.api import app
from mira.dashboard.db import AppDatabase
from mira.index.store import IndexStore


@pytest.fixture
def patched_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    db = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr(api, "_app_db", db)
    client = TestClient(app)
    client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    return client


def test_returns_active_reviews(patched_app: TestClient):
    tracker = ReviewTracker()
    tracker.start("test/repo", 1, "Test PR", "https://github.com/test/repo/pull/1")
    with patch("mira.dashboard.api.tracker", tracker):
        resp = patched_app.get("/api/reviews")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    entry = body["items"][0]
    assert entry["repo"] == "test/repo"
    assert entry["pr_number"] == 1
    assert entry["status"] == "reviewing"
    assert entry["pr_title"] == "Test PR"


def test_includes_db_completed_reviews(tmp_path: Path, patched_app: TestClient):
    owner, repo = "testowner", "testrepo"
    store_dir = tmp_path / owner
    store_dir.mkdir(parents=True, exist_ok=True)
    store = IndexStore(str(store_dir / f"{repo}.db"))
    store.record_review(
        pr_number=5,
        pr_title="Completed PR",
        pr_url="https://github.com/testowner/testrepo/pull/5",
        comments_posted=3,
        blockers=0,
        warnings=1,
        suggestions=2,
        files_reviewed=2,
        lines_changed=100,
        tokens_used=500,
        duration_ms=15000,
        categories="bug,security",
    )
    store.close()

    api._app_db.register_repo(owner, repo)

    with patch("mira.dashboard.api.tracker", ReviewTracker()):
        resp = patched_app.get("/api/reviews")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    entry = next(
        (r for r in body["items"] if r["repo"] == f"{owner}/{repo}" and r["pr_number"] == 5),
        None,
    )
    assert entry is not None
    assert entry["status"] == "completed"


def test_empty_when_no_data(patched_app: TestClient):
    with patch("mira.dashboard.api.tracker", ReviewTracker()):
        resp = patched_app.get("/api/reviews")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_requires_auth():
    client = TestClient(app)
    resp = client.get("/api/reviews")
    assert resp.status_code == 401
