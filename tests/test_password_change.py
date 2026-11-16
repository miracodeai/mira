"""Tests for changing/resetting user passwords."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse

from mira.dashboard.auth import (
    SESSION_COOKIE,
    ChangePasswordRequest,
    ResetPasswordRequest,
    create_auth_router,
)
from mira.dashboard.db import AppDatabase, _hash_password


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    return AppDatabase(url="", admin_password="admin")


def _endpoint(db: AppDatabase, suffix: str, method: str = "POST"):
    """Pull a handler out of the auth router by path suffix + method."""
    router = create_auth_router(db)
    for route in router.routes:
        if route.path.endswith(suffix) and method in route.methods:
            return route.endpoint
    raise AssertionError(f"endpoint {method} *{suffix} not found")


def _req(user, token: str = "") -> SimpleNamespace:  # noqa: ANN001
    cookies = {SESSION_COOKIE: token} if token else {}
    return SimpleNamespace(state=SimpleNamespace(user=user), cookies=cookies)


def _status(result) -> int | None:  # noqa: ANN001
    return result.status_code if isinstance(result, JSONResponse) else None


def test_update_password_changes_login(db: AppDatabase) -> None:
    admin = db.authenticate("admin", "admin")
    assert admin is not None

    db.update_password(admin.id, "newpass123")

    # Old password no longer works; new one does.
    assert db.authenticate("admin", "admin") is None
    assert db.authenticate("admin", "newpass123") is not None


def test_update_password_only_affects_target_user(db: AppDatabase) -> None:
    admin = db.authenticate("admin", "admin")
    assert admin is not None
    other = db.create_user("bob", "bobpass", is_admin=False)

    db.update_password(other.id, "changed")

    # Bob's password changed; admin's is untouched.
    assert db.authenticate("bob", "bobpass") is None
    assert db.authenticate("bob", "changed") is not None
    assert db.authenticate("admin", "admin") is not None


# ── Admin reset endpoint: POST /api/auth/users/{id}/password ──


def test_admin_reset_changes_password(db: AppDatabase) -> None:
    admin = db.authenticate("admin", "admin")
    bob = db.create_user("bob", "bobpass", is_admin=False)
    reset = _endpoint(db, "/users/{user_id}/password")

    result = reset(bob.id, ResetPasswordRequest(new_password="reset123"), _req(admin))

    assert result == {"ok": True}
    assert db.authenticate("bob", "reset123") is not None


def test_admin_reset_requires_admin(db: AppDatabase) -> None:
    bob = db.create_user("bob", "bobpass", is_admin=False)
    reset = _endpoint(db, "/users/{user_id}/password")

    result = reset(bob.id, ResetPasswordRequest(new_password="x"), _req(bob))

    assert _status(result) == 403
    # Password unchanged.
    assert db.authenticate("bob", "bobpass") is not None


def test_admin_reset_rejects_empty_password(db: AppDatabase) -> None:
    admin = db.authenticate("admin", "admin")
    bob = db.create_user("bob", "bobpass", is_admin=False)
    reset = _endpoint(db, "/users/{user_id}/password")

    result = reset(bob.id, ResetPasswordRequest(new_password=""), _req(admin))

    assert _status(result) == 400
    assert db.authenticate("bob", "bobpass") is not None


# ── Self-service change endpoint: POST /api/auth/change-password ──


def test_change_password_with_correct_current(db: AppDatabase) -> None:
    admin = db.authenticate("admin", "admin")
    change = _endpoint(db, "/change-password")

    result = change(
        ChangePasswordRequest(current_password="admin", new_password="newpw123"),
        _req(admin),
    )

    assert result == {"ok": True}
    assert db.authenticate("admin", "newpw123") is not None


def test_change_password_wrong_current_rejected(db: AppDatabase) -> None:
    admin = db.authenticate("admin", "admin")
    change = _endpoint(db, "/change-password")

    result = change(
        ChangePasswordRequest(current_password="wrong", new_password="newpw123"),
        _req(admin),
    )

    assert _status(result) == 400
    # Password unchanged — the original still works.
    assert db.authenticate("admin", "admin") is not None


# ── Sessions must not outlive the password they were established with (CWE-613) ──


def test_admin_reset_revokes_target_sessions(db: AppDatabase) -> None:
    admin = db.authenticate("admin", "admin")
    bob = db.create_user("bob", "bobpass", is_admin=False)
    stolen, other = db.create_session(bob.id), db.create_session(bob.id)
    admins_own = db.create_session(admin.id)
    reset = _endpoint(db, "/users/{user_id}/password")

    assert reset(bob.id, ResetPasswordRequest(new_password="rotated"), _req(admin, admins_own)) == {
        "ok": True
    }

    # Every one of bob's sessions is gone; the admin's is untouched.
    assert db.validate_session(stolen) is None
    assert db.validate_session(other) is None
    assert db.validate_session(admins_own) is not None
    # The reset still works as a reset.
    assert db.authenticate("bob", "bobpass") is None
    assert db.authenticate("bob", "rotated") is not None


def test_admin_resetting_own_password_keeps_current_session(db: AppDatabase) -> None:
    admin = db.authenticate("admin", "admin")
    current, other = db.create_session(admin.id), db.create_session(admin.id)
    reset = _endpoint(db, "/users/{user_id}/password")

    reset(admin.id, ResetPasswordRequest(new_password="rotated"), _req(admin, current))

    assert db.validate_session(current) is not None
    assert db.validate_session(other) is None


def test_change_password_revokes_other_sessions_keeps_current(db: AppDatabase) -> None:
    admin = db.authenticate("admin", "admin")
    current, other = db.create_session(admin.id), db.create_session(admin.id)
    change = _endpoint(db, "/change-password")

    result = change(
        ChangePasswordRequest(current_password="admin", new_password="newpw123"),
        _req(admin, current),
    )

    assert result == {"ok": True}
    assert db.validate_session(current) is not None  # not signed out mid-flow
    assert db.validate_session(other) is None


def test_rejected_change_keeps_sessions(db: AppDatabase) -> None:
    admin = db.authenticate("admin", "admin")
    current, other = db.create_session(admin.id), db.create_session(admin.id)
    change = _endpoint(db, "/change-password")

    change(ChangePasswordRequest(current_password="wrong", new_password="x"), _req(admin, current))

    assert db.validate_session(current) is not None
    assert db.validate_session(other) is not None


def test_legacy_hash_upgrade_on_login_keeps_sessions(db: AppDatabase) -> None:
    # authenticate() rewrites a pre-0.7.1 SHA-256 hash via update_password();
    # that is a storage upgrade, not a password rotation, so sessions survive.
    import hashlib

    bob = db.create_user("bob", "irrelevant", is_admin=False)
    legacy = hashlib.sha256(b"mira_salt_v1:oldpw").hexdigest()
    assert not legacy.startswith("pbkdf2$") and _hash_password("x").startswith("pbkdf2$")
    db._sqlite_conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (legacy, bob.id))
    db._sqlite_conn.commit()
    session = db.create_session(bob.id)

    assert db.authenticate("bob", "oldpw") is not None

    assert db.validate_session(session) is not None


def test_delete_user_sessions_keep_token(db: AppDatabase) -> None:
    admin = db.authenticate("admin", "admin")
    bob = db.create_user("bob", "bobpass", is_admin=False)
    keep, drop = db.create_session(bob.id), db.create_session(bob.id)
    admins = db.create_session(admin.id)

    db.delete_user_sessions(bob.id, keep_token=keep)
    assert db.validate_session(keep) is not None
    assert db.validate_session(drop) is None
    assert db.validate_session(admins) is not None

    db.delete_user_sessions(bob.id)
    assert db.validate_session(keep) is None
