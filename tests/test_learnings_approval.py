"""Synthesized learnings land in a pending queue and only feed reviews once an
admin approves them. Admins can also CRUD rules directly."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from mira.dashboard import api
from mira.dashboard.db import AppDatabase, User
from mira.index.store import IndexStore


@pytest.fixture
def patched_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    db = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr(api, "_app_db", db)
    return db


class _Req:
    """Minimal stand-in for a Starlette Request carrying request.state.user."""

    def __init__(self, is_admin: bool):
        self.state = type("S", (), {"user": User(id=1, username="u", is_admin=is_admin)})()


def test_synthesized_rules_are_pending(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    store = IndexStore.open("acme", "web")
    # upsert is the synthesis path — should default to pending.
    rule = store.upsert_learned_rule(
        rule_text="Don't flag missing docstrings on helpers",
        source_signal="reject_pattern",
        category="style",
        path_pattern="",
        sample_count=3,
    )
    assert rule.status == "pending"
    # Pending rules must NOT feed reviews.
    assert store.list_active_learned_rules() == []
    store.close()


def test_approve_makes_rule_active(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    store = IndexStore.open("acme", "web")
    rule = store.upsert_learned_rule("r", "reject_pattern", "style", "", 3)
    store.close()

    api.approve_learned_rule("acme", "web", rule.id, _Req(is_admin=True))

    store = IndexStore.open("acme", "web")
    active = store.list_active_learned_rules()
    assert [r.id for r in active] == [rule.id]
    store.close()


def test_reject_keeps_rule_out(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    store = IndexStore.open("acme", "web")
    rule = store.upsert_learned_rule("r", "reject_pattern", "style", "", 3)
    store.close()

    api.reject_learned_rule("acme", "web", rule.id, _Req(is_admin=True))

    store = IndexStore.open("acme", "web")
    assert store.list_active_learned_rules() == []
    assert store.get_learned_rule(rule.id).status == "rejected"
    store.close()


def test_non_admin_cannot_approve(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    store = IndexStore.open("acme", "web")
    rule = store.upsert_learned_rule("r", "reject_pattern", "style", "", 3)
    store.close()
    with pytest.raises(HTTPException) as exc:
        api.approve_learned_rule("acme", "web", rule.id, _Req(is_admin=False))
    assert exc.value.status_code == 403


def test_admin_crud(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    # Create → approved + active immediately.
    created = api.create_learned_rule(
        "acme",
        "web",
        api.LearnedRuleInput(rule_text="No nits in tests", category="style", path_pattern="tests/"),
        _Req(is_admin=True),
    )
    assert created.status == "approved" and created.active

    store = IndexStore.open("acme", "web")
    assert any(r.rule_text == "No nits in tests" for r in store.list_active_learned_rules())
    store.close()

    # Update.
    api.update_learned_rule(
        "acme",
        "web",
        created.id,
        api.LearnedRuleInput(rule_text="Updated", category="style", path_pattern="tests/"),
        _Req(is_admin=True),
    )
    # Disable → drops out of active set.
    api.set_learned_rule_active(
        "acme", "web", created.id, api.LearnedRuleActiveInput(active=False), _Req(is_admin=True)
    )
    store = IndexStore.open("acme", "web")
    assert all(r.id != created.id for r in store.list_active_learned_rules())
    got = store.get_learned_rule(created.id)
    assert got.rule_text == "Updated"
    store.close()

    # Delete.
    api.delete_learned_rule("acme", "web", created.id, _Req(is_admin=True))
    store = IndexStore.open("acme", "web")
    assert store.get_learned_rule(created.id) is None
    store.close()
