"""Tests for the admin /api/admin/settings endpoint and its DB layer.

Covers:
- AppDatabase round-trip for the JSON-blobbed override settings.
- `load_config()` layers DB overrides between mira.yaml and per-repo files.
- Endpoint admin-gating, allowed-sections enforcement, validation rejects.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import mira.config as mira_config
from mira.config import load_config, set_global_defaults
from mira.dashboard.api import _ALLOWED_OVERRIDE_SECTIONS, GlobalSettingsUpdate
from mira.dashboard.db import AppDatabase
from mira.dashboard.routers.admin import get_global_settings, set_global_settings


@pytest.fixture
def in_memory_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    """Fresh per-test SQLite DB swapped in for the module-level `_app_db`.

    `AppDatabase()` with no url falls back to `${MIRA_INDEX_DIR}/_app.db`
    (default `./data/indexes/_app.db`) — a real shared file across tests.
    Pointing it at a tmp dir gives each test its own clean DB.
    """
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    db = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr("mira.dashboard.api._app_db", db)
    return db


@pytest.fixture(autouse=True)
def _reset_global_defaults():
    saved = mira_config._global_defaults
    mira_config._global_defaults = {}
    try:
        yield
    finally:
        mira_config._global_defaults = saved


def _admin_request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(is_admin=True)))


def _non_admin_request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(is_admin=False)))


class TestDBRoundTrip:
    def test_default_is_empty(self, in_memory_db: AppDatabase):
        assert in_memory_db.get_global_review_overrides() == {}

    def test_set_then_get(self, in_memory_db: AppDatabase):
        in_memory_db.set_global_review_overrides({"filter": {"max_comments": 8}})
        assert in_memory_db.get_global_review_overrides() == {"filter": {"max_comments": 8}}

    def test_set_empty_clears(self, in_memory_db: AppDatabase):
        in_memory_db.set_global_review_overrides({"filter": {"max_comments": 8}})
        in_memory_db.set_global_review_overrides({})
        assert in_memory_db.get_global_review_overrides() == {}

    def test_malformed_json_returns_empty(self, in_memory_db: AppDatabase):
        # Stuff a non-JSON string directly into the underlying setting and
        # confirm the typed accessor degrades to empty rather than raising.
        in_memory_db.set_setting("global_review_overrides", "{not json}")
        assert in_memory_db.get_global_review_overrides() == {}


class TestLoadConfigDbLayer:
    """`load_config()` lazy-imports `_app_db` and merges its overrides."""

    def test_db_overrides_layer_over_global_defaults(
        self, in_memory_db: AppDatabase, tmp_path: Path
    ):
        global_yaml = tmp_path / "mira.yaml"
        global_yaml.write_text("filter:\n  confidence_threshold: 0.7\n  max_comments: 5\n")
        set_global_defaults(global_yaml)

        in_memory_db.set_global_review_overrides({"filter": {"max_comments": 12}})

        cfg = load_config()
        assert cfg.filter.confidence_threshold == 0.7  # inherited from yaml
        assert cfg.filter.max_comments == 12  # DB override wins

    def test_per_repo_yaml_wins_over_db_overrides(self, in_memory_db: AppDatabase, tmp_path: Path):
        global_yaml = tmp_path / "mira.yaml"
        global_yaml.write_text("filter:\n  max_comments: 5\n")
        set_global_defaults(global_yaml)

        in_memory_db.set_global_review_overrides({"filter": {"max_comments": 12}})

        repo_yaml = tmp_path / "repo" / ".mira.yaml"
        repo_yaml.parent.mkdir()
        repo_yaml.write_text("filter:\n  max_comments: 99\n")

        cfg = load_config(repo_yaml)
        assert cfg.filter.max_comments == 99  # per-repo > DB > global


class TestEndpointAuthorization:
    def test_get_rejects_non_admin(self, in_memory_db: AppDatabase):
        with pytest.raises(HTTPException) as exc:
            get_global_settings(_non_admin_request())
        assert exc.value.status_code == 403

    def test_put_rejects_non_admin(self, in_memory_db: AppDatabase):
        with pytest.raises(HTTPException) as exc:
            set_global_settings(
                GlobalSettingsUpdate(overrides={"filter": {"max_comments": 8}}),
                _non_admin_request(),
            )
        assert exc.value.status_code == 403

    def test_get_returns_overrides_and_effective(self, in_memory_db: AppDatabase):
        in_memory_db.set_global_review_overrides({"filter": {"max_comments": 8}})
        resp = get_global_settings(_admin_request())
        assert resp.overrides == {"filter": {"max_comments": 8}}
        assert "filter" in resp.effective
        assert resp.effective["filter"]["max_comments"] == 8


class TestEndpointValidation:
    def test_rejects_disallowed_section(self, in_memory_db: AppDatabase):
        with pytest.raises(HTTPException) as exc:
            set_global_settings(
                GlobalSettingsUpdate(overrides={"llm": {"model": "x"}}),
                _admin_request(),
            )
        assert exc.value.status_code == 400
        assert "not allowed" in exc.value.detail

    def test_rejects_invalid_value(self, in_memory_db: AppDatabase):
        # confidence_threshold has ge=0.0, le=1.0 — 2.5 fails Pydantic validation.
        with pytest.raises(HTTPException) as exc:
            set_global_settings(
                GlobalSettingsUpdate(overrides={"filter": {"confidence_threshold": 2.5}}),
                _admin_request(),
            )
        assert exc.value.status_code == 400
        # Structured field-level error so the UI can render inline.
        assert exc.value.detail == {
            "field": "filter.confidence_threshold",
            "message": "must be ≤ 1.0",
        }

    def test_persists_valid_overrides(self, in_memory_db: AppDatabase):
        result = set_global_settings(
            GlobalSettingsUpdate(
                overrides={"filter": {"confidence_threshold": 0.4, "max_comments": 7}}
            ),
            _admin_request(),
        )
        assert result == {"ok": True}
        assert in_memory_db.get_global_review_overrides() == {
            "filter": {"confidence_threshold": 0.4, "max_comments": 7}
        }

    def test_persists_auto_resolve_conversations_override(self, in_memory_db: AppDatabase):
        result = set_global_settings(
            GlobalSettingsUpdate(overrides={"review": {"auto_resolve_conversations": False}}),
            _admin_request(),
        )
        assert result == {"ok": True}
        assert in_memory_db.get_global_review_overrides() == {
            "review": {"auto_resolve_conversations": False}
        }

    def test_allowed_sections_constant(self):
        assert {"filter", "review"} == _ALLOWED_OVERRIDE_SECTIONS


class TestVersionEndpoint:
    """The /api/version endpoint reads `mira.__version__` and returns it."""

    def test_returns_version(self):
        from mira.dashboard.routers.core import get_version

        result = get_version()
        assert "version" in result
        assert isinstance(result["version"], str)
        assert result["version"] != ""
