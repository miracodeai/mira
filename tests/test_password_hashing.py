"""Tests for PBKDF2 password hashing and legacy SHA-256 migration."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mira.dashboard.db import AppDatabase, _hash_password, _verify_password


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    return AppDatabase(url="", admin_password="adminpw")


def _legacy_hash(password: str) -> str:
    return hashlib.sha256(f"mira_salt_v1:{password}".encode()).hexdigest()


def test_hashes_are_salted_per_user() -> None:
    a, b = _hash_password("same-password"), _hash_password("same-password")
    assert a != b
    assert a.startswith("pbkdf2$")
    assert _verify_password("same-password", a)
    assert _verify_password("same-password", b)
    assert not _verify_password("wrong", a)


def test_verify_accepts_legacy_hash() -> None:
    stored = _legacy_hash("oldpw")
    assert _verify_password("oldpw", stored)
    assert not _verify_password("wrong", stored)


def test_login_upgrades_legacy_hash(db: AppDatabase) -> None:
    bob = db.create_user("bob", "irrelevant", is_admin=False)
    db._sqlite_conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?", (_legacy_hash("oldpw"), bob.id)
    )
    db._sqlite_conn.commit()

    assert db.authenticate("bob", "wrong") is None
    assert db.authenticate("bob", "oldpw") is not None

    stored = db._sqlite_conn.execute(
        "SELECT password_hash FROM users WHERE id = ?", (bob.id,)
    ).fetchone()[0]
    assert stored.startswith("pbkdf2$")
    # Still logs in after the upgrade.
    assert db.authenticate("bob", "oldpw") is not None


def test_default_password_warning_covers_legacy_hashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    db = AppDatabase(url="", admin_password="x")
    db._sqlite_conn.execute(
        "UPDATE users SET password_hash = ? WHERE username = 'admin'",
        (_legacy_hash("admin"),),
    )
    db._sqlite_conn.commit()

    with caplog.at_level("WARNING"):
        AppDatabase(url="")

    assert any("well-known default password" in r.getMessage() for r in caplog.records)
