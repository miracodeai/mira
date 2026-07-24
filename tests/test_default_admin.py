"""Tests for initial admin creation — no hard-coded default password (CWE-1188)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mira.dashboard.db import AppDatabase


@pytest.fixture(autouse=True)
def _index_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))


def test_no_password_generates_random_admin(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        db = AppDatabase(url="")

    # admin/admin must not work.
    assert db.authenticate("admin", "admin") is None

    # The generated password is logged once and does work.
    msg = next(r.getMessage() for r in caplog.records if "generated password" in r.getMessage())
    password = msg.split("generated password: ")[1].split(" ")[0]
    assert db.authenticate("admin", password) is not None


def test_configured_password_is_used(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        db = AppDatabase(url="", admin_password="s3cret-value")

    assert db.authenticate("admin", "s3cret-value") is not None
    assert db.authenticate("admin", "admin") is None
    assert not any("generated password" in r.getMessage() for r in caplog.records)


def test_existing_default_password_warns_on_startup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Simulate an old deployment that shipped with admin/admin.
    AppDatabase(url="", admin_password="admin")

    with caplog.at_level("WARNING"):
        db = AppDatabase(url="")

    assert any("well-known default password" in r.getMessage() for r in caplog.records)
    # No second admin was created; the old one is untouched.
    assert db.authenticate("admin", "admin") is not None


def test_no_warning_for_strong_existing_password(
    caplog: pytest.LogCaptureFixture,
) -> None:
    AppDatabase(url="", admin_password="a-strong-password")

    with caplog.at_level("WARNING"):
        AppDatabase(url="")

    assert not any("well-known default" in r.getMessage() for r in caplog.records)
