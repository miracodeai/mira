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


class TestBaseUrlValidation:
    """base_url is trusted deployment input, but obvious misconfigurations
    (non-http schemes, plain http to a public host) fail loudly at load."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://openrouter.ai/api/v1",
            "https://api.together.xyz/v1",
            "http://localhost:11434/v1",
            "http://127.0.0.1:8000/v1",
            "http://ollama:11434/v1",
            "http://10.0.0.5:8000/v1",
        ],
    )
    def test_accepted(self, url):
        from mira.config import LLMConfig

        assert LLMConfig(base_url=url).base_url == url

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://openrouter.ai/api/v1",
            "file:///etc/passwd",
            "openrouter.ai/api/v1",
            "",
            "http://api.example.com/v1",
        ],
    )
    def test_rejected(self, url):
        from mira.config import LLMConfig

        with pytest.raises(ValueError):
            LLMConfig(base_url=url)


class TestCodexConfigValidation:
    def test_rejects_non_read_only_sandbox(self):
        from mira.config import LLMConfig

        with pytest.raises(ValueError):
            LLMConfig(provider="codex-cli", codex_sandbox="danger-full-access")

    def test_rejects_non_positive_timeout(self):
        from mira.config import LLMConfig

        with pytest.raises(ValueError):
            LLMConfig(provider="codex-cli", codex_timeout_seconds=0)


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

    def test_repo_file_cannot_enable_or_repoint_cli_provider(self, tmp_path: Path):
        repo_file = tmp_path / ".mira.yaml"
        repo_file.write_text(
            "llm:\n"
            "  provider: codex-cli\n"
            "  codex_command: ./repo-controlled-codex\n"
            "  codex_home: ./repo-auth\n"
        )

        config = load_config(repo_file)

        assert config.llm.provider == "openai"
        assert config.llm.codex_command == "codex"
        assert config.llm.codex_home is None

    def test_explicitly_trusted_cli_config_can_enable_codex(self, tmp_path: Path):
        config_file = tmp_path / "deployment.yaml"
        config_file.write_text(
            "llm:\n"
            "  provider: codex-cli\n"
            "  codex_command: /trusted/bin/codex\n"
            "  codex_home: /trusted/codex-home\n"
        )

        config = load_config(config_file, trust_execution_settings=True)

        assert config.llm.provider == "codex-cli"
        assert config.llm.codex_command == "/trusted/bin/codex"
        assert config.llm.codex_home == "/trusted/codex-home"

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

    def test_repo_config_cannot_override_codex_execution_settings(self, tmp_path: Path):
        global_file = tmp_path / "mira.yaml"
        global_file.write_text(
            "llm:\n"
            "  provider: codex-cli\n"
            "  codex_command: /trusted/bin/codex\n"
            "  codex_home: /trusted/codex-home\n"
            "  codex_sandbox: read-only\n"
            "  codex_timeout_seconds: 900\n"
        )
        set_global_defaults(global_file)
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        repo_file = repo_dir / ".mira.yaml"
        repo_file.write_text(
            "llm:\n"
            "  provider: openai\n"
            "  codex_command: ./repo-controlled-codex\n"
            "  codex_home: ./repo-auth\n"
            "  codex_sandbox: danger-full-access\n"
            "  codex_timeout_seconds: 1\n"
        )

        config = load_config(repo_file)

        assert config.llm.provider == "codex-cli"
        assert config.llm.codex_command == "/trusted/bin/codex"
        assert config.llm.codex_home == "/trusted/codex-home"
        assert config.llm.codex_sandbox == "read-only"
        assert config.llm.codex_timeout_seconds == 900

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
