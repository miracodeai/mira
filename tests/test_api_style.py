"""Tests for api_style resolution and the Models endpoint.

Covers:
- ``resolve_api_style`` precedence: DB → config → default.
- ``llm_config_for`` setting ``api_style`` for all purposes.
- ``set_models`` validating and persisting the api_style.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from mira.config import LLMConfig
from mira.dashboard.api import ModelsUpdate
from mira.dashboard.db import AppDatabase
from mira.dashboard.models_config import (
    API_STYLE_VALUES,
    llm_config_for,
    resolve_api_style,
)
from mira.dashboard.routers.admin import set_models


def _admin_req():
    from types import SimpleNamespace

    user = SimpleNamespace(is_admin=True)
    return SimpleNamespace(state=SimpleNamespace(user=user))


@pytest.fixture
def in_memory_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    """Fresh per-test SQLite DB swapped in for the module-level ``_app_db``."""
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    db = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr("mira.dashboard.api._app_db", db)
    return db


class TestResolveApiStyle:
    def test_default_is_chat(self):
        assert resolve_api_style(LLMConfig(), None) == "chat"

    def test_db_wins_over_config(self):
        cfg = LLMConfig(api_style="chat")
        assert resolve_api_style(cfg, "responses") == "responses"

    def test_falls_back_to_config(self):
        cfg = LLMConfig(api_style="responses")
        assert resolve_api_style(cfg, None) == "responses"

    def test_unknown_db_falls_back_to_config(self):
        cfg = LLMConfig(api_style="responses")
        assert resolve_api_style(cfg, "garbage") == "responses"

    def test_unknown_config_defaults_to_chat(self):
        cfg = LLMConfig()
        cfg.api_style = "unknown_value"
        assert resolve_api_style(cfg, None) == "chat"

    def test_db_responses_wins_over_config_chat(self):
        cfg = LLMConfig(api_style="chat")
        assert resolve_api_style(cfg, "responses") == "responses"


class TestLLMConfigFor:
    def test_review_picks_up_api_style(self, in_memory_db: AppDatabase):
        in_memory_db.set_setting("api_style", "responses")
        resolved = llm_config_for("review", LLMConfig())
        assert resolved.api_style == "responses"

    def test_indexing_picks_up_api_style(self, in_memory_db: AppDatabase):
        in_memory_db.set_setting("api_style", "responses")
        resolved = llm_config_for("indexing", LLMConfig())
        assert resolved.api_style == "responses"

    def test_unknown_purpose_picks_up_api_style(self, in_memory_db: AppDatabase):
        in_memory_db.set_setting("api_style", "responses")
        resolved = llm_config_for("other", LLMConfig())
        assert resolved.api_style == "responses"

    def test_no_db_uses_config(self, in_memory_db: AppDatabase):
        # No api_style set in DB — config default is "chat"
        resolved = llm_config_for("review", LLMConfig())
        assert resolved.api_style == "chat"

    def test_config_responses_with_no_db(self, in_memory_db: AppDatabase):
        resolved = llm_config_for("review", LLMConfig(api_style="responses"))
        assert resolved.api_style == "responses"


class TestSetModelsApiStyle:
    def test_rejects_invalid_api_style(self, in_memory_db: AppDatabase):
        with pytest.raises(HTTPException) as exc_info:
            set_models(
                ModelsUpdate(
                    indexing_model="gpt-4o",
                    review_model="gpt-4o",
                    api_style="ultra",
                ),
                _admin_req(),
            )
        assert exc_info.value.status_code == 400

    def test_persists_responses(self, in_memory_db: AppDatabase):
        set_models(
            ModelsUpdate(
                indexing_model="gpt-4o",
                review_model="gpt-4o",
                api_style="responses",
            ),
            _admin_req(),
        )
        assert in_memory_db.get_setting("api_style") == "responses"

    def test_clears_on_chat(self, in_memory_db: AppDatabase):
        set_models(
            ModelsUpdate(
                indexing_model="gpt-4o",
                review_model="gpt-4o",
                api_style="chat",
            ),
            _admin_req(),
        )
        assert in_memory_db.get_setting("api_style") == ""

    def test_default_clears_and_does_not_shadow_config(
        self, in_memory_db: AppDatabase
    ):
        # Save "chat" (default) → should clear the DB entry
        set_models(
            ModelsUpdate(indexing_model="gpt-4o", review_model="gpt-4o"),
            _admin_req(),
        )
        assert in_memory_db.get_setting("api_style") == ""
        # Now a config-level responses should still resolve
        resolved = resolve_api_style(
            LLMConfig(api_style="responses"), in_memory_db.get_setting("api_style")
        )
        assert resolved == "responses"

    def test_existing_thinking_tests_still_work(self, in_memory_db: AppDatabase):
        """Ensure that api_style is independent of review_thinking_mode."""
        set_models(
            ModelsUpdate(
                indexing_model="gpt-4o",
                review_model="gpt-4o",
                review_thinking_mode="high",
                api_style="responses",
            ),
            _admin_req(),
        )
        assert in_memory_db.get_setting("api_style") == "responses"
        assert in_memory_db.get_setting("review_thinking_mode") == "high"

    def test_api_style_values_are_complete(self):
        assert "chat" in API_STYLE_VALUES
        assert "responses" in API_STYLE_VALUES
