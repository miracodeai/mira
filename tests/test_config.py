"""Tests for config loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from mira.config import MiraConfig, find_config_file, load_config
from mira.exceptions import ConfigError


class TestLoadConfig:
    def test_default_config(self):
        config = load_config()
        assert config.llm.model == "openai/gpt-4o"
        assert config.filter.confidence_threshold == 0.7
        assert config.filter.max_comments == 5
        assert config.review.focus_only_on_problems is False
        assert config.review.walkthrough is True
        assert config.review.walkthrough_sequence_diagram is True

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
        bad_file = tmp_path / ".mira.yml"
        bad_file.write_text("{{invalid yaml")
        with pytest.raises(ConfigError, match="Invalid YAML"):
            load_config(bad_file)

    def test_empty_yaml(self, tmp_path: Path):
        empty_file = tmp_path / ".mira.yml"
        empty_file.write_text("")
        config = load_config(empty_file)
        assert config == MiraConfig()


class TestFindConfigFile:
    def test_finds_config_in_current_dir(self, tmp_path: Path):
        config_file = tmp_path / ".mira.yml"
        config_file.write_text("llm:\n  model: test")
        result = find_config_file(tmp_path)
        assert result == config_file

    def test_finds_config_in_parent(self, tmp_path: Path):
        config_file = tmp_path / ".mira.yml"
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

    def test_walkthrough_overrides(self):
        config = load_config(
            overrides={
                "review.walkthrough": False,
                "review.walkthrough_sequence_diagram": True,
            }
        )
        assert config.review.walkthrough is False
        assert config.review.walkthrough_sequence_diagram is True
