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

    def test_default_is_none(self):
        assert get_review_thinking_mode(LLMConfig(), None) is None

    @pytest.mark.parametrize("value", ["off", ""])
    def test_off_and_empty_normalize_to_none(self, value: str):
        assert get_review_thinking_mode(LLMConfig(), value) is None

    @pytest.mark.parametrize("db_value", ["off", "", None])
    def test_off_db_value_does_not_shadow_config(self, db_value: str | None):
        # Saving the models form always writes "off" by default; that must not
        # permanently disable a mira.yaml-level reasoning effort.
        cfg = LLMConfig(review_reasoning_effort="high")
        assert get_review_thinking_mode(cfg, db_value) == "high"


class TestLLMConfigFor:
    def test_review_picks_up_thinking_mode(self, in_memory_db: AppDatabase):
        in_memory_db.set_setting("review_thinking_mode", "high")
        resolved = llm_config_for("review", LLMConfig())
        assert resolved.reasoning_effort == "high"

    def test_indexing_never_sets_thinking_mode(self, in_memory_db: AppDatabase):
        in_memory_db.set_setting("review_thinking_mode", "high")
        resolved = llm_config_for("indexing", LLMConfig())
        assert resolved.reasoning_effort is None

    def test_review_default_off_is_none(self, in_memory_db: AppDatabase):
        resolved = llm_config_for("review", LLMConfig())
        assert resolved.reasoning_effort is None


class TestSetModelsThinkingValidation:
    def test_rejects_invalid_thinking_mode(self, in_memory_db: AppDatabase):
        body = ModelsUpdate(
            indexing_model="anthropic/claude-haiku-4-5",
            review_model="anthropic/claude-sonnet-4-6",
            review_thinking_mode="ultra",
        )
        with pytest.raises(HTTPException) as exc:
            set_models(body)
        assert exc.value.status_code == 400

    def test_persists_valid_thinking_mode(self, in_memory_db: AppDatabase):
        body = ModelsUpdate(
            indexing_model="anthropic/claude-haiku-4-5",
            review_model="anthropic/claude-sonnet-4-6",
            review_thinking_mode="medium",
        )
        assert set_models(body) == {"ok": True}
        assert in_memory_db.get_setting("review_thinking_mode") == "medium"

    def test_off_clears_setting_so_config_can_win(self, in_memory_db: AppDatabase):
        # "off" must not be persisted as a literal — it'd shadow a mira.yaml
        # override. It's stored as "" (the column is NOT NULL) and reads as unset.
        body = ModelsUpdate(
            indexing_model="anthropic/claude-haiku-4-5",
            review_model="anthropic/claude-sonnet-4-6",
            review_thinking_mode="off",
        )
        assert set_models(body) == {"ok": True}
        assert in_memory_db.get_setting("review_thinking_mode") == ""
        cfg = LLMConfig(review_reasoning_effort="high")
        assert (
            get_review_thinking_mode(cfg, in_memory_db.get_setting("review_thinking_mode"))
            == "high"
        )
