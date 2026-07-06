"""Learned-rules quarantine endpoints: list includes pending rules,
approve/decline, edit, and delete."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from fastapi import HTTPException

from mira.dashboard import api
from mira.index.store import IndexStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = IndexStore(str(tmp_path / "t.db"))

    @contextmanager
    def fake_open_store(owner, repo):
        yield s

    monkeypatch.setattr(api, "_open_store", fake_open_store)
    yield s
    s.close()


def _seed(store) -> int:
    row = store.upsert_learned_rule(
        rule_text="Avoid raw SQL in controllers.",
        source_signal="human_pattern",
        category="human_review",
        path_pattern="",
        sample_count=3,
    )
    return row.id


def test_list_includes_pending_with_id_and_status(store):
    rule_id = _seed(store)
    out = api.list_repo_learned_rules("acme", "web")
    assert len(out) == 1
    assert out[0].id == rule_id
    assert out[0].status == "pending"


def test_approve_and_decline(store):
    rule_id = _seed(store)
    out = api.set_learned_rule_status(
        "acme", "web", rule_id, api.LearnedRuleStatusUpdate(status="approved")
    )
    assert out.status == "approved"
    assert store.get_learned_rules_text() == ["Avoid raw SQL in controllers."]

    out = api.set_learned_rule_status(
        "acme", "web", rule_id, api.LearnedRuleStatusUpdate(status="declined")
    )
    assert out.status == "declined"
    assert store.get_learned_rules_text() == []


def test_status_rejects_invalid_value(store):
    rule_id = _seed(store)
    with pytest.raises(HTTPException) as exc:
        api.set_learned_rule_status(
            "acme", "web", rule_id, api.LearnedRuleStatusUpdate(status="pending")
        )
    assert exc.value.status_code == 400


def test_status_404_on_missing_rule(store):
    with pytest.raises(HTTPException) as exc:
        api.set_learned_rule_status(
            "acme", "web", 999, api.LearnedRuleStatusUpdate(status="approved")
        )
    assert exc.value.status_code == 404


def test_update_rule_text(store):
    rule_id = _seed(store)
    out = api.update_learned_rule(
        "acme", "web", rule_id, api.LearnedRuleTextUpdate(rule_text="  New wording  ")
    )
    assert out.rule_text == "New wording"
    assert out.status == "pending"  # editing never changes status


def test_update_rejects_empty_text(store):
    rule_id = _seed(store)
    with pytest.raises(HTTPException) as exc:
        api.update_learned_rule("acme", "web", rule_id, api.LearnedRuleTextUpdate(rule_text="   "))
    assert exc.value.status_code == 400


def test_update_404_on_missing_rule(store):
    with pytest.raises(HTTPException) as exc:
        api.update_learned_rule("acme", "web", 999, api.LearnedRuleTextUpdate(rule_text="x"))
    assert exc.value.status_code == 404


def test_delete(store):
    rule_id = _seed(store)
    assert api.delete_learned_rule("acme", "web", rule_id) == {"ok": True}
    assert store.list_learned_rules() == []
