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

    def __init__(self, is_admin: bool, username: str = "u"):
        self.state = type("S", (), {"user": User(id=1, username=username, is_admin=is_admin)})()


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


def test_non_admin_create_is_pending(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    created = api.create_learned_rule(
        "acme",
        "web",
        api.LearnedRuleInput(rule_text="Be nice", category="style"),
        _Req(is_admin=False, username="junior"),
    )
    assert created.status == "pending"
    assert created.created_by == "junior"


def test_admin_create_is_approved(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    created = api.create_learned_rule(
        "acme",
        "web",
        api.LearnedRuleInput(rule_text="Be safe", category="security"),
        _Req(is_admin=True, username="boss"),
    )
    assert created.status == "approved"


def test_creator_can_edit_own_pending(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    created = api.create_learned_rule(
        "acme",
        "web",
        api.LearnedRuleInput(rule_text="original", category="style"),
        _Req(is_admin=False, username="junior"),
    )
    api.update_learned_rule(
        "acme",
        "web",
        created.id,
        api.LearnedRuleInput(rule_text="edited", category="style"),
        _Req(is_admin=False, username="junior"),
    )
    store = IndexStore.open("acme", "web")
    assert store.get_learned_rule(created.id).rule_text == "edited"
    store.close()


def test_other_non_admin_cannot_edit(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    created = api.create_learned_rule(
        "acme",
        "web",
        api.LearnedRuleInput(rule_text="original", category="style"),
        _Req(is_admin=False, username="junior"),
    )
    with pytest.raises(HTTPException) as exc:
        api.update_learned_rule(
            "acme",
            "web",
            created.id,
            api.LearnedRuleInput(rule_text="hijacked", category="style"),
            _Req(is_admin=False, username="someone-else"),
        )
    assert exc.value.status_code == 403


def test_creator_cannot_edit_once_approved(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    created = api.create_learned_rule(
        "acme",
        "web",
        api.LearnedRuleInput(rule_text="original", category="style"),
        _Req(is_admin=False, username="junior"),
    )
    api.approve_learned_rule("acme", "web", created.id, _Req(is_admin=True))
    with pytest.raises(HTTPException) as exc:
        api.update_learned_rule(
            "acme",
            "web",
            created.id,
            api.LearnedRuleInput(rule_text="edited", category="style"),
            _Req(is_admin=False, username="junior"),
        )
    assert exc.value.status_code == 403


def test_other_non_admin_cannot_read_pending(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    created = api.create_learned_rule(
        "acme",
        "web",
        api.LearnedRuleInput(rule_text="original", category="style"),
        _Req(is_admin=False, username="junior"),
    )
    with pytest.raises(HTTPException) as exc:
        api.get_learned_rule_detail(
            "acme", "web", created.id, _Req(is_admin=False, username="someone-else")
        )
    assert exc.value.status_code == 403
    # Creator and admin can still read it.
    assert (
        api.get_learned_rule_detail(
            "acme", "web", created.id, _Req(is_admin=False, username="junior")
        ).id
        == created.id
    )
    assert (
        api.get_learned_rule_detail(
            "acme", "web", created.id, _Req(is_admin=True, username="boss")
        ).id
        == created.id
    )
    # Once approved, anyone authenticated can read it.
    api.approve_learned_rule("acme", "web", created.id, _Req(is_admin=True))
    assert (
        api.get_learned_rule_detail(
            "acme", "web", created.id, _Req(is_admin=False, username="someone-else")
        ).status
        == "approved"
    )


def test_admin_can_edit_anyones_rule(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    created = api.create_learned_rule(
        "acme",
        "web",
        api.LearnedRuleInput(rule_text="original", category="style"),
        _Req(is_admin=False, username="junior"),
    )
    api.update_learned_rule(
        "acme",
        "web",
        created.id,
        api.LearnedRuleInput(rule_text="admin edited", category="style"),
        _Req(is_admin=True, username="boss"),
    )
    store = IndexStore.open("acme", "web")
    assert store.get_learned_rule(created.id).rule_text == "admin edited"
    store.close()
