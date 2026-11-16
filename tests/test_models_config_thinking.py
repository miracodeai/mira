"""Tests for review thinking-mode resolution and the models endpoint.

Covers:
- `get_review_thinking_mode` precedence and off/empty normalization.
- `llm_config_for` setting `reasoning_effort` for reviews only, from the DB.
- `set_models` validating the thinking mode and persisting it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from mira.config import LLMConfig
from mira.dashboard.api import ModelsUpdate
from mira.dashboard.db import AppDatabase
from mira.dashboard.models_config import (
    get_review_thinking_mode,
    llm_config_for,
)
from mira.dashboard.routers.admin import set_models


def _admin_req():
    from types import SimpleNamespace

    user = SimpleNamespace(is_admin=True)
    return SimpleNamespace(state=SimpleNamespace(user=user))


@pytest.fixture
def in_memory_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    """Fresh per-test SQLite DB swapped in for the module-level `_app_db`."""
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    db = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr("mira.dashboard.api._app_db", db)
    return db


class TestGetReviewThinkingMode:
    def test_db_value_wins(self):
        cfg = LLMConfig(review_reasoning_effort="low")
        assert get_review_thinking_mode(cfg, "high") == "high"

    def test_falls_back_to_config(self):
        cfg = LLMConfig(review_reasoning_effort="medium")
        assert get_review_thinking_mode(cfg, None) == "medium"

    def test_default_is_medium(self):
        # The published benchmark's setting; "off" has to be chosen.
        assert get_review_thinking_mode(LLMConfig(), None) == "medium"

    @pytest.mark.parametrize("value", ["off", ""])
    def test_db_off_is_explicit_and_empty_falls_through(self, value: str):
        expected = None if value == "off" else "medium"
        assert get_review_thinking_mode(LLMConfig(), value) == expected

    def test_config_off_turns_thinking_off(self):
        assert get_review_thinking_mode(LLMConfig(review_reasoning_effort="off"), None) is None

    @pytest.mark.parametrize("db_value", ["", None])
    def test_unset_db_value_does_not_shadow_config(self, db_value: str | None):
        cfg = LLMConfig(review_reasoning_effort="high")
        assert get_review_thinking_mode(cfg, db_value) == "high"

    def test_db_off_wins_over_config(self):
        # The models form shows the resolved mode and round-trips it, so a
        # stored "off" is the admin's explicit choice.
        cfg = LLMConfig(review_reasoning_effort="high")
        assert get_review_thinking_mode(cfg, "off") is None


class TestLLMConfigFor:
    def test_review_picks_up_thinking_mode(self, in_memory_db: AppDatabase):
        in_memory_db.set_setting("review_thinking_mode", "high")
        resolved = llm_config_for("review", LLMConfig())
        assert resolved.reasoning_effort == "high"

    def test_indexing_never_sets_thinking_mode(self, in_memory_db: AppDatabase):
        in_memory_db.set_setting("review_thinking_mode", "high")
        resolved = llm_config_for("indexing", LLMConfig())
        assert resolved.reasoning_effort is None

    def test_review_default_is_medium(self, in_memory_db: AppDatabase):
        resolved = llm_config_for("review", LLMConfig())
        assert resolved.reasoning_effort == "medium"

    def test_security_pass_shares_the_review_thinking_mode(self, in_memory_db: AppDatabase):
        resolved = llm_config_for("security", LLMConfig())
        assert resolved.reasoning_effort == "medium"


class TestSetModelsThinkingValidation:
    def test_rejects_invalid_thinking_mode(self, in_memory_db: AppDatabase):
        body = ModelsUpdate(
            indexing_model="anthropic/claude-haiku-4-5",
            review_model="anthropic/claude-sonnet-4-6",
            review_thinking_mode="ultra",
        )
        with pytest.raises(HTTPException) as exc:
            set_models(body, _admin_req())
        assert exc.value.status_code == 400

    def test_persists_valid_thinking_mode(self, in_memory_db: AppDatabase):
        body = ModelsUpdate(
            indexing_model="anthropic/claude-haiku-4-5",
            review_model="anthropic/claude-sonnet-4-6",
            review_thinking_mode="medium",
        )
        assert set_models(body, _admin_req()) == {"ok": True}
        assert in_memory_db.get_setting("review_thinking_mode") == "medium"

    def test_persists_xhigh_thinking_mode(self, in_memory_db: AppDatabase):
        body = ModelsUpdate(
            indexing_model="anthropic/claude-haiku-4-5",
            review_model="anthropic/claude-sonnet-4-6",
            review_thinking_mode="xhigh",
        )
        assert set_models(body, _admin_req()) == {"ok": True}
        assert in_memory_db.get_setting("review_thinking_mode") == "xhigh"

    def test_off_is_persisted_and_wins_over_config(self, in_memory_db: AppDatabase):
        # The form shows the resolved mode, so "off" coming back is the admin's
        # choice: it is stored as-is and beats both the config and the default.
        body = ModelsUpdate(
            indexing_model="anthropic/claude-haiku-4-5",
            review_model="anthropic/claude-sonnet-4-6",
            review_thinking_mode="off",
        )
        assert set_models(body, _admin_req()) == {"ok": True}
        assert in_memory_db.get_setting("review_thinking_mode") == "off"
        cfg = LLMConfig(review_reasoning_effort="high")
        assert (
            get_review_thinking_mode(cfg, in_memory_db.get_setting("review_thinking_mode")) is None
        )
