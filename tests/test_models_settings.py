"""Tests for model override visibility and clearing (issue #124).

Covers:
- `llm_config_for` logging the effective model and its source.
- `set_models` accepting "" (inherit from config) and free-form model ids.
- `get_models` reporting the override source and the config-resolved models.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from mira.config import LLMConfig
from mira.dashboard.api import ModelsUpdate, get_models, set_models
from mira.dashboard.db import AppDatabase
from mira.dashboard.models_config import llm_config_for


@pytest.fixture
def in_memory_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    """Fresh per-test SQLite DB swapped in for the module-level `_app_db`."""
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    db = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr("mira.dashboard.api._app_db", db)
    return db


class TestEffectiveModelLogging:
    def test_dashboard_override_logged_as_source(
        self, in_memory_db: AppDatabase, caplog: pytest.LogCaptureFixture
    ):
        in_memory_db.set_setting("review_model", "custom/model-x")
        with caplog.at_level(logging.INFO, logger="mira.dashboard.models_config"):
            resolved = llm_config_for("review", LLMConfig(review_model="openai/gpt-5.1"))
        assert resolved.model == "custom/model-x"
        assert "Review model: custom/model-x (source: dashboard setting)" in caplog.text

    def test_config_model_logged_as_mira_yaml(
        self, in_memory_db: AppDatabase, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.INFO, logger="mira.dashboard.models_config"):
            resolved = llm_config_for("review", LLMConfig(review_model="openai/gpt-5.1"))
        assert resolved.model == "openai/gpt-5.1"
        assert "Review model: openai/gpt-5.1 (source: mira.yaml)" in caplog.text

    def test_fallback_model_logged_as_default(
        self, in_memory_db: AppDatabase, caplog: pytest.LogCaptureFixture
    ):
        with caplog.at_level(logging.INFO, logger="mira.dashboard.models_config"):
            llm_config_for("indexing", LLMConfig(model="anthropic/claude-sonnet-4-6"))
        assert "Indexing model: anthropic/claude-sonnet-4-6 (source: default)" in caplog.text


class TestSetModelsInheritAndCustom:
    def test_empty_value_clears_override(self, in_memory_db: AppDatabase):
        in_memory_db.set_setting("review_model", "anthropic/claude-sonnet-4-6")
        body = ModelsUpdate(indexing_model="", review_model="")
        assert set_models(body) == {"ok": True}
        assert in_memory_db.get_setting("review_model") == ""
        # Cleared override → config is authoritative again.
        cfg = LLMConfig(review_model="openai/gpt-5.1")
        assert llm_config_for("review", cfg).model == "openai/gpt-5.1"

    def test_non_registry_model_accepted(self, in_memory_db: AppDatabase):
        body = ModelsUpdate(
            indexing_model="local/llama-3.3-70b",
            review_model="openai/gpt-5.1-codex-mini",
        )
        assert set_models(body) == {"ok": True}
        assert in_memory_db.get_setting("review_model") == "openai/gpt-5.1-codex-mini"
        assert llm_config_for("review", LLMConfig()).model == "openai/gpt-5.1-codex-mini"


@pytest.fixture
def no_catalog_fetch(monkeypatch: pytest.MonkeyPatch):
    """Keep get_models off the network and hermetic — default config,
    static registry options only (a developer's env/mira.yaml must not
    leak into assertions)."""
    from mira.config import MiraConfig

    async def _none(config):
        return None

    monkeypatch.setattr("mira.dashboard.model_catalog.fetch_catalog", _none)
    monkeypatch.setattr("mira.config.load_config", lambda *a, **kw: MiraConfig())


class TestGetModelsSource:
    @pytest.mark.asyncio
    async def test_reports_dashboard_source_and_config_target(
        self, in_memory_db: AppDatabase, no_catalog_fetch
    ):
        in_memory_db.set_setting("review_model", "custom/model-x")
        resp = await get_models()
        assert resp.review_source == "dashboard"
        assert resp.review_model == "custom/model-x"
        assert resp.indexing_source == "config"
        # The inherit target ignores the override.
        assert resp.config_review_model != "custom/model-x"

    @pytest.mark.asyncio
    async def test_reports_config_source_without_override(
        self, in_memory_db: AppDatabase, no_catalog_fetch
    ):
        resp = await get_models()
        assert resp.review_source == "config"
        assert resp.indexing_source == "config"
        assert resp.review_model == resp.config_review_model
        assert resp.backend == "openrouter"
