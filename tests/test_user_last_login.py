"""Tests for tracking and surfacing users' last-login time."""

from __future__ import annotations

from pathlib import Path

import pytest

from mira.dashboard.db import AppDatabase


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    return AppDatabase(url="", admin_password="admin")


def test_last_login_starts_unset(db: AppDatabase) -> None:
    users = db.list_users()
    assert users  # the default admin exists
    assert all(u.last_login_at == 0 for u in users)


def test_record_login_stamps_time(db: AppDatabase) -> None:
    admin = db.authenticate("admin", "admin")
    assert admin is not None

    db.record_login(admin.id)

    user = next(u for u in db.list_users() if u.id == admin.id)
    assert user.last_login_at > 0


def test_record_login_updates_on_each_login(db: AppDatabase) -> None:
    admin = db.authenticate("admin", "admin")
    assert admin is not None

    db.record_login(admin.id)
    first = next(u for u in db.list_users() if u.id == admin.id).last_login_at
    db.record_login(admin.id)
    second = next(u for u in db.list_users() if u.id == admin.id).last_login_at

    assert second >= first
