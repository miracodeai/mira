"""Tests for model override visibility and clearing (issue #124).

Covers:
- `llm_config_for` logging the effective model and its source.
- `set_models` accepting "" (inherit from config) and free-form model ids.
- `get_models` reporting the override source and the config-resolved models.
"""

from __future__ import annotations

import json
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


@pytest.fixture
def models_http_app(in_memory_db):
    from fastapi import FastAPI

    from mira.dashboard.api import router

    app = FastAPI()

    @app.middleware("http")
    async def admin_user(request, call_next):
        request.state.user = _admin_req().state.user
        return await call_next(request)

    app.include_router(router)
    return app


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


class TestIndexingTemperatureSettings:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [0, 0.4, "omit", None])
    async def test_http_policy_round_trip(
        self, in_memory_db, no_catalog_fetch, models_http_app, value
    ):
        import httpx

        in_memory_db.set_setting("indexing_temperature", '"omit"')
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=models_http_app), base_url="http://test.invalid"
        ) as client:
            response = await client.put(
                "/api/settings/models",
                json={"indexing_model": "", "review_model": "", "indexing_temperature": value},
            )
            assert response.status_code == 200
            resolved = (await client.get("/api/settings/models")).json()
        assert resolved["indexing_temperature"] == (0.0 if value is None else value)
        assert resolved["indexing_temperature_source"] == (
            "config" if value is None else "dashboard"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "literal",
        [
            "1e400",
            "-1e400",
            "NaN",
            "Infinity",
            "-Infinity",
            '{"nested":1e400}',
            "[1e400]",
            "-1",
            "true",
            '"0.5"',
        ],
    )
    async def test_http_validation_returns_serializable_errors_without_writes(
        self, in_memory_db, models_http_app, literal
    ):
        import httpx

        in_memory_db.set_setting("indexing_model", "original")
        in_memory_db.set_setting("indexing_temperature", '"omit"')
        body = (
            '{"indexing_model":"changed","review_model":"","indexing_temperature":' + literal + "}"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=models_http_app, raise_app_exceptions=False),
            base_url="http://test.invalid",
        ) as client:
            response = await client.put(
                "/api/settings/models", content=body, headers={"Content-Type": "application/json"}
            )
        assert response.status_code == 422
        assert response.json()["detail"]
        assert in_memory_db.get_setting("indexing_model") == "original"
        assert in_memory_db.get_setting("indexing_temperature") == '"omit"'

    @pytest.mark.parametrize("stored", ["bad-json", "true", "-1", "NaN", '"0.5"', "null", "{}"])
    def test_invalid_saved_policy_uses_config_without_logging_value(self, stored, caplog):
        from mira.dashboard.models_config import resolve_indexing_temperature

        with caplog.at_level(logging.WARNING):
            value, source = resolve_indexing_temperature(
                LLMConfig(indexing_temperature=0.6), stored
            )
        assert (value, source) == (0.6, "config")
        assert caplog.messages == [
            "Invalid saved indexing_temperature. Using deployment config. "
            "Clear the override in Models settings."
        ]

    def test_non_admin_cannot_change_policy(self, in_memory_db):
        from fastapi import HTTPException

        request = _admin_req()
        request.state.user.is_admin = False
        with pytest.raises(HTTPException) as exc:
            set_models(
                ModelsUpdate(indexing_model="", review_model="", indexing_temperature="omit"),
                request,
            )
        assert exc.value.status_code == 403
        assert in_memory_db.get_setting("indexing_temperature") is None

    @pytest.mark.parametrize("value", [0, 0.4, "omit"])
    def test_saved_policy_overrides_config_only_for_indexing(self, in_memory_db, value):
        body = ModelsUpdate(
            indexing_model="",
            review_model="",
            indexing_temperature=value,
            review_thinking_mode="low",
        )
        set_models(body, _admin_req())
        saved = in_memory_db.get_setting("indexing_temperature")
        assert saved is not None
        assert json.loads(saved) == value
        config = LLMConfig(indexing_temperature=0.8, review_reasoning_effort="low")
        indexing = llm_config_for("indexing", config)
        assert indexing.indexing_temperature == value
        assert indexing.temperature == 0.2
        assert indexing.reasoning_effort is None
        review = llm_config_for("review", config)
        assert review.temperature == 0.2
        assert review.reasoning_effort == "low"
        assert config.indexing_temperature == 0.8

    def test_missing_field_preserves_saved_policy(self, in_memory_db):
        in_memory_db.set_setting("indexing_temperature", '"omit"')
        set_models(ModelsUpdate(indexing_model="", review_model=""), _admin_req())
        assert in_memory_db.get_setting("indexing_temperature") == '"omit"'

    def test_explicit_null_clears_override(self, in_memory_db):
        in_memory_db.set_setting("indexing_temperature", '"omit"')
        set_models(
            ModelsUpdate(indexing_model="", review_model="", indexing_temperature=None),
            _admin_req(),
        )
        assert in_memory_db.get_setting("indexing_temperature") == ""
        assert (
            llm_config_for("indexing", LLMConfig(indexing_temperature=0.6)).indexing_temperature
            == 0.6
        )

    @pytest.mark.parametrize("value", [-1, True, "0.5", "default", float("nan"), float("inf")])
    def test_invalid_policy_rejected_before_any_writes(self, in_memory_db, value):
        in_memory_db.set_setting("indexing_model", "original")
        with pytest.raises(ValueError):
            set_models(
                ModelsUpdate(indexing_model="changed", review_model="", indexing_temperature=value),
                _admin_req(),
            )
        assert in_memory_db.get_setting("indexing_model") == "original"

    @pytest.mark.asyncio
    async def test_get_reports_effective_config_and_source(self, in_memory_db, no_catalog_fetch):
        in_memory_db.set_setting("indexing_temperature", '"omit"')
        response = (await get_models()).model_dump()
        assert response.get("indexing_temperature") == "omit"
        assert response.get("config_indexing_temperature") == 0.0
        assert response.get("indexing_temperature_source") == "dashboard"


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
