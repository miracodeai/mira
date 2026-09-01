"""Tests for model resolution logic (models_config)."""

from __future__ import annotations

from unittest.mock import patch

from mira.config import LLMConfig
from mira.dashboard.models_config import (
    get_security_model,
    llm_config_for,
)


class TestGetSecurityModel:
    """The security tier chain: DB → security_model → review_model → model."""

    def test_db_value_wins(self):
        config = LLMConfig(model="base", review_model="review")
        assert get_security_model(config, db_value="db-model") == "db-model"

    def test_security_model_set(self):
        config = LLMConfig(model="base", review_model="review", security_model="sec-model")
        assert get_security_model(config) == "sec-model"

    def test_falls_back_to_review_model(self):
        config = LLMConfig(model="base", review_model="review")
        assert get_security_model(config) == "review"

    def test_falls_back_to_model(self):
        config = LLMConfig(model="base")
        assert get_security_model(config) == "base"

    def test_never_returns_indexing_model(self):
        """Security tier must not silently downgrade to the indexing tier."""
        config = LLMConfig(model="base", indexing_model="cheap")
        result = get_security_model(config)
        assert result == "base"
        assert result != "cheap"

    def test_db_review_model_is_fallback(self):
        """A dashboard-set review model must win over config.model."""
        config = LLMConfig(model="base")
        assert get_security_model(config, db_review_model="db-review") == "db-review"

    def test_config_security_model_beats_db_review(self):
        config = LLMConfig(model="base", security_model="sec-model")
        assert get_security_model(config, db_review_model="db-review") == "sec-model"

    def test_db_value_beats_db_review(self):
        config = LLMConfig(model="base")
        assert (
            get_security_model(config, db_value="db-sec", db_review_model="db-review") == "db-sec"
        )


class TestLlmConfigForSecurityDbReview:
    """llm_config_for(\"security\") falls back to the dashboard review model."""

    def test_falls_back_to_db_review_model(self):
        class _FakeDB:
            def get_setting(self, key: str) -> str | None:
                return {"review_model": "db-review"}.get(key)

        base = LLMConfig(model="base")
        with patch("mira.dashboard.api._app_db", _FakeDB()):
            config = llm_config_for("security", base)

        assert config.model == "db-review"


class TestLlmConfigForSecurity:
    """llm_config_for(\"security\", base) resolves the security tier."""

    def test_resolves_security_model_from_config(self):
        base = LLMConfig(
            model="base",
            review_model="review",
            security_model="sec-model",
        )
        with patch("mira.dashboard.models_config._app_db", None, create=True):
            config = llm_config_for("security", base)

        assert config.model == "sec-model"

    def test_inherits_review_thinking_mode(self):
        """Security purpose should pick up the review thinking mode."""
        base = LLMConfig(
            model="base",
            review_reasoning_effort="medium",
        )
        with patch("mira.dashboard.models_config._app_db", None, create=True):
            config = llm_config_for("security", base)

        assert config.reasoning_effort == "medium"
