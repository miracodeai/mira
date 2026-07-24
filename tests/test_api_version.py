"""/api/version exposes bot_name so the dashboard can show the real @mention
(persisted by `mira serve`) instead of a hardcoded placeholder."""

from __future__ import annotations

from pathlib import Path

import pytest

from mira.dashboard import api
from mira.dashboard.db import AppDatabase
from mira.dashboard.routers import core


@pytest.fixture
def patched_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    db = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr(api, "_app_db", db)
    return db


def test_bot_name_defaults_to_miracodeai(patched_db: AppDatabase):
    out = core.get_version()
    assert out["bot_name"] == "miracodeai"
    assert out["version"]


def test_bot_name_reflects_persisted_setting(patched_db: AppDatabase):
    patched_db.set_setting("bot_name", "acme-reviewer")
    assert core.get_version()["bot_name"] == "acme-reviewer"
