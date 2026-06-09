"""Tests for changing/resetting user passwords."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse

from mira.dashboard.auth import (
    ChangePasswordRequest,
    ResetPasswordRequest,
    create_auth_router,
)
from mira.dashboard.db import AppDatabase


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


def _req(user) -> SimpleNamespace:  # noqa: ANN001
    return SimpleNamespace(state=SimpleNamespace(user=user))


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
