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
from mira.dashboard.api import ModelsUpdate
from mira.dashboard.db import AppDatabase
from mira.dashboard.models_config import llm_config_for
from mira.dashboard.routers.admin import get_models, set_models


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
    def test_saved_empty_lists_override_mira_yaml(self, in_memory_db: AppDatabase):
        from mira.dashboard.models_config import ensemble_configs

        cfg = LLMConfig(fallback_models=["a/b"], ensemble_models=["c/d"])
        assert llm_config_for("review", cfg).fallback_models == ["a/b"]
        assert [c.model for c in ensemble_configs(cfg)] == ["c/d"]
        in_memory_db.set_settings({"review_fallback_models": "", "ensemble_models": ""})
        assert llm_config_for("review", cfg).fallback_models == []
        assert ensemble_configs(cfg) == []

    def test_empty_value_clears_override(self, in_memory_db: AppDatabase):
        in_memory_db.set_setting("review_model", "anthropic/claude-sonnet-4-6")
        body = ModelsUpdate(indexing_model="", review_model="")
        assert set_models(body, _admin_req()) == {"ok": True}
        assert in_memory_db.get_setting("review_model") == ""
        # Cleared override → config is authoritative again.
        cfg = LLMConfig(review_model="openai/gpt-5.1")
        assert llm_config_for("review", cfg).model == "openai/gpt-5.1"

    def test_non_registry_model_accepted(self, in_memory_db: AppDatabase):
        body = ModelsUpdate(
            indexing_model="local/llama-3.3-70b",
            review_model="openai/gpt-5.1-codex-mini",
        )
        assert set_models(body, _admin_req()) == {"ok": True}
        assert in_memory_db.get_setting("review_model") == "openai/gpt-5.1-codex-mini"
        assert llm_config_for("review", LLMConfig()).model == "openai/gpt-5.1-codex-mini"

    def test_save_is_one_write(self, in_memory_db: AppDatabase, monkeypatch: pytest.MonkeyPatch):
        """A failed save must not leave new models next to stale fallbacks."""
        in_memory_db.set_setting("review_model", "old/model")
        calls: list[dict[str, str]] = []

        def boom(values: dict[str, str]) -> None:
            calls.append(dict(values))
            raise RuntimeError("disk full")

        body = ModelsUpdate(indexing_model="", review_model="new/model", review_fallbacks=["x/y"])
        with monkeypatch.context() as m, pytest.raises(RuntimeError):
            m.setattr(in_memory_db, "set_settings", boom)
            set_models(body, _admin_req())
        assert len(calls) == 1
        assert calls[0]["review_model"] == "new/model"
        assert calls[0]["review_fallback_models"] == "x/y"
        assert in_memory_db.get_setting("review_model") == "old/model"
        assert in_memory_db.get_setting("review_fallback_models") is None

        in_memory_db.set_settings({"review_model": "new/model", "review_fallback_models": "x/y"})
        assert in_memory_db.get_setting("review_model") == "new/model"
        assert in_memory_db.get_setting("review_fallback_models") == "x/y"


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


def test_slot_reasoning_and_list_suffixes(in_memory_db: AppDatabase):
    from mira.dashboard.models_config import critique_config, ensemble_configs
    from mira.llm import ModelChain, create_llm

    base = LLMConfig(
        model="claude/primary",
        indexing_model="claude/indexer",
        review_reasoning_effort="high",
        fallback_models=["claude/fallback#low", "claude/inherited", "claude/off#off"],
        ensemble_models=["claude/opinion#max", "claude/another"],
    )
    assert llm_config_for("indexing", base).reasoning_effort is None
    assert llm_config_for("security", base).reasoning_effort == "high"
    assert critique_config(base) is None
    in_memory_db.set_settings({"indexing_reasoning": "medium", "security_reasoning": "off"})
    assert llm_config_for("indexing", base).reasoning_effort == "medium"
    assert llm_config_for("security", base).reasoning_effort is None
    critic = create_llm(critique_config(base))
    assert critic.config.model == "indexer"
    assert critic.config.reasoning_effort is None
    in_memory_db.set_settings({"critique_reasoning": "low", "critique_model": "claude/critic"})
    assert create_llm(critique_config(base)).config.reasoning_effort == "low"

    review = create_llm(llm_config_for("review", base))
    assert isinstance(review, ModelChain)
    assert review.models == ["claude/primary", "claude/fallback", "claude/inherited", "claude/off"]
    assert [p.config.reasoning_effort for p in review.providers] == ["high", "low", "high", None]
    opinions = [create_llm(c) for c in ensemble_configs(base)]
    assert [(p.config.model, p.config.reasoning_effort) for p in opinions] == [
        ("opinion", "max"),
        ("another", "high"),
    ]
    assert base.fallback_models[0] == "claude/fallback#low"
    in_memory_db.set_settings({"security_reasoning": "", "review_thinking_mode": "medium"})
    assert llm_config_for("security", base).reasoning_effort == "medium"


async def test_reasoning_settings_round_trip(in_memory_db, no_catalog_fetch, monkeypatch):
    from fastapi import HTTPException

    from mira.config import MiraConfig

    config = MiraConfig(
        llm=LLMConfig(
            review_reasoning_effort="high",
            critique_model="claude/critic",
            fallback_models=["claude/backup#low"],
            ensemble_models=["claude/opinion#max"],
        )
    )
    monkeypatch.setattr("mira.config.load_config", lambda: config)
    initial = await get_models()
    assert (initial.review_thinking_mode, initial.security_reasoning) == ("high", "high")
    assert (initial.indexing_reasoning, initial.critique_reasoning) == ("off", "off")
    assert set(initial.reasoning_overrides.values()) == {""}
    assert initial.review_fallbacks == ["claude/backup#low"]
    assert initial.ensemble_models == ["claude/opinion#max"]
    body = ModelsUpdate(
        indexing_model="",
        review_model="claude/reviewer",
        review_thinking_mode="medium",
        indexing_reasoning="low",
        security_reasoning="off",
        critique_reasoning="high",
        review_fallbacks=["claude/backup#max", "claude/inherit"],
        security_fallbacks=[],
        ensemble_models=["claude/opinion#off"],
    )
    set_models(body, _admin_req())
    saved = await get_models()
    assert saved.security_inherits_review
    assert saved.config_security_model == saved.security_model == "claude/reviewer"
    assert (
        saved.indexing_reasoning,
        saved.review_thinking_mode,
        saved.security_reasoning,
        saved.critique_reasoning,
    ) == (
        "low",
        "medium",
        "off",
        "high",
    )
    assert saved.security_fallbacks == []
    assert saved.review_fallbacks == body.review_fallbacks
    assert saved.ensemble_models == body.ensemble_models
    assert in_memory_db.get_setting("review_fallback_models") == "claude/backup#max,claude/inherit"
    assert in_memory_db.get_setting("security_fallback_models") == ""
    for field, value in {
        "indexing_reasoning": "ultra",
        "security_reasoning": "no",
        "critique_reasoning": "all",
        "review_thinking_mode": "invalid",
        "review_fallbacks": ["claude/x#no"],
        "ensemble_models": ["claude/x#"],
    }.items():
        with pytest.raises(HTTPException) as exc:
            set_models(body.model_copy(update={field: value}), _admin_req())
        assert exc.value.status_code == 400
    assert (await get_models()).model_dump() == saved.model_dump()
    set_models(
        body.model_copy(update={"review_thinking_mode": "off", "security_reasoning": ""}),
        _admin_req(),
    )
    assert (await get_models()).review_thinking_mode == "off"
    assert llm_config_for("review", config.llm).reasoning_effort is None
    set_models(ModelsUpdate(indexing_model="", review_model=""), _admin_req())
    restored = await get_models()
    assert (restored.review_thinking_mode, restored.security_reasoning) == ("high", "high")
    assert restored.indexing_reasoning == "low"  # Setup saves leave the new slots untouched.
