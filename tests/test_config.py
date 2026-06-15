"""Tests for config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

import mira.config as mira_config
from mira.config import MiraConfig, find_config_file, load_config, set_global_defaults
from mira.exceptions import ConfigError


@pytest.fixture(autouse=True)
def _reset_global_defaults(monkeypatch: pytest.MonkeyPatch):
    """Each test starts with empty global defaults AND no DB layer so cases
    don't interfere with each other or pick up admin overrides written by
    the running dev server's `_app.db`."""
    saved = mira_config._global_defaults
    mira_config._global_defaults = {}
    monkeypatch.setattr("mira.dashboard.api._app_db", None)
    try:
        yield
    finally:
        mira_config._global_defaults = saved


class TestLoadConfig:
    def test_default_config(self):
        config = load_config()
        assert config.llm.model == "anthropic/claude-sonnet-4-6"
        assert config.filter.confidence_threshold == 0.7
        assert config.filter.max_comments == 5
        assert config.review.focus_only_on_problems is False
        assert config.review.walkthrough is True
        assert config.review.walkthrough_sequence_diagram is True
        assert config.index.max_file_size == 1_048_576

    def test_focus_only_on_problems_override(self, sample_config_path: Path):
        config = load_config(
            sample_config_path,
            {"review.focus_only_on_problems": False},
        )
        assert config.review.focus_only_on_problems is False

    def test_load_from_file(self, sample_config_path: Path):
        config = load_config(sample_config_path)
        assert config.llm.model == "openai/gpt-4o-mini"
        assert config.llm.temperature == 0.1
        assert config.filter.confidence_threshold == 0.8
        assert config.filter.max_comments == 3

    def test_overrides(self, sample_config_path: Path):
        config = load_config(sample_config_path, {"llm.model": "anthropic/claude-3-haiku"})
        assert config.llm.model == "anthropic/claude-3-haiku"
        # Other values from file still apply
        assert config.filter.confidence_threshold == 0.8

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "nonexistent.yml")

    def test_invalid_yaml(self, tmp_path: Path):
        bad_file = tmp_path / ".mira.yaml"
        bad_file.write_text("{{invalid yaml")
        with pytest.raises(ConfigError, match="Invalid YAML"):
            load_config(bad_file)

    def test_empty_yaml(self, tmp_path: Path):
        empty_file = tmp_path / ".mira.yaml"
        empty_file.write_text("")
        config = load_config(empty_file)
        assert config == MiraConfig()


class TestFindConfigFile:
    def test_finds_config_in_current_dir(self, tmp_path: Path):
        config_file = tmp_path / ".mira.yaml"
        config_file.write_text("llm:\n  model: test")
        result = find_config_file(tmp_path)
        assert result == config_file

    def test_finds_config_in_parent(self, tmp_path: Path):
        config_file = tmp_path / ".mira.yaml"
        config_file.write_text("llm:\n  model: test")
        child = tmp_path / "subdir"
        child.mkdir()
        result = find_config_file(child)
        assert result == config_file

    def test_returns_none_when_not_found(self, tmp_path: Path):
        result = find_config_file(tmp_path)
        assert result is None


class TestWalkthroughConfig:
    def test_walkthrough_defaults(self):
        config = load_config()
        assert config.review.walkthrough is True
        assert config.review.walkthrough_sequence_diagram is True


class TestGlobalDefaults:
    """Layered config: deployment-wide global → per-repo `.mira.yaml` → overrides."""

    def test_global_defaults_applied(self, tmp_path: Path):
        global_file = tmp_path / "mira.yaml"
        global_file.write_text(
            "llm:\n"
            "  model: anthropic/claude-haiku-4-5\n"
            "filter:\n"
            "  confidence_threshold: 0.6\n"
            "  max_comments: 10\n"
        )
        set_global_defaults(global_file)

        config = load_config()
        assert config.llm.model == "anthropic/claude-haiku-4-5"
        assert config.filter.confidence_threshold == 0.6
        assert config.filter.max_comments == 10

    def test_per_repo_overrides_global(self, tmp_path: Path):
        # Global sets a baseline.
        global_file = tmp_path / "mira.yaml"
        global_file.write_text(
            "llm:\n  model: anthropic/claude-sonnet-4-6\n"
            "filter:\n  confidence_threshold: 0.7\n  max_comments: 5\n"
        )
        set_global_defaults(global_file)

        # Per-repo `.mira.yaml` overrides only the threshold; other keys
        # inherit from the global.
        repo_file = tmp_path / "repo" / ".mira.yaml"
        repo_file.parent.mkdir()
        repo_file.write_text("filter:\n  confidence_threshold: 0.4\n")

        config = load_config(repo_file)
        assert config.filter.confidence_threshold == 0.4  # repo wins
        assert config.filter.max_comments == 5  # inherited from global
        assert config.llm.model == "anthropic/claude-sonnet-4-6"  # inherited

    def test_set_global_defaults_missing_file(self, tmp_path: Path):
        with pytest.raises(ConfigError, match="not found"):
            set_global_defaults(tmp_path / "nope.yaml")

    def test_env_overrides_global(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        global_file = tmp_path / "mira.yaml"
        global_file.write_text("llm:\n  model: anthropic/claude-sonnet-4-6\n")
        set_global_defaults(global_file)

        # MIRA_MODEL env should NOT win against an explicit global value —
        # global config is more specific than env. We assert that explicit
        # global beats the env fallback.
        monkeypatch.setenv("MIRA_MODEL", "openai/gpt-4o")
        config = load_config()
        assert config.llm.model == "anthropic/claude-sonnet-4-6"

    def test_env_fills_when_global_silent(self, monkeypatch: pytest.MonkeyPatch):
        # No global, no per-repo, just env — env fallback applies.
        monkeypatch.setenv("MIRA_MODEL", "openai/gpt-4o-mini")
        config = load_config()
        assert config.llm.model == "openai/gpt-4o-mini"

    def test_walkthrough_overrides(self):
        config = load_config(
            overrides={
                "review.walkthrough": False,
                "review.walkthrough_sequence_diagram": True,
            }
        )
        assert config.review.walkthrough is False
        assert config.review.walkthrough_sequence_diagram is True
