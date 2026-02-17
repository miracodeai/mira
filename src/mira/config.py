"""Configuration loading and validation for Mira."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from mira.exceptions import ConfigError

_DEFAULT_CONFIG_FILENAME = ".mira.yml"


class LLMConfig(BaseModel):
    model: str = "openai/gpt-4o"
    fallback_model: str | None = None
    temperature: float = 0.2
    max_tokens: int = 4096
    max_context_tokens: int = 120_000


class FilterConfig(BaseModel):
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    max_comments: int = Field(default=5, ge=1)
    min_severity: str = "nitpick"
    exclude_patterns: list[str] = Field(
        default_factory=lambda: [
            "*.lock",
            "*.lockb",
            "package-lock.json",
            "yarn.lock",
            "pnpm-lock.yaml",
            "Pipfile.lock",
            "poetry.lock",
            "go.sum",
            "*.min.js",
            "*.min.css",
            "*.map",
            "*.svg",
            "*.png",
            "*.jpg",
            "*.jpeg",
            "*.gif",
            "*.ico",
            "*.woff",
            "*.woff2",
            "*.ttf",
            "*.eot",
            "*.pdf",
            "*.zip",
            "*.tar.gz",
        ]
    )
    exclude_deleted: bool = True
    max_files: int = 50


class ReviewConfig(BaseModel):
    context_lines: int = Field(default=3, ge=0)
    max_diff_size: int = 50_000
    include_summary: bool = True
    focus_only_on_problems: bool = False
    walkthrough: bool = True
    walkthrough_sequence_diagram: bool = True


class ProviderConfig(BaseModel):
    type: str = "github"


class MiraConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    filter: FilterConfig = Field(default_factory=FilterConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)


def find_config_file(start_dir: Path | None = None) -> Path | None:
    """Walk up from start_dir looking for .mira.yml."""
    current = start_dir or Path.cwd()
    for directory in [current, *current.parents]:
        candidate = directory / _DEFAULT_CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def _load_yaml(path: Path) -> dict[str, Any]:
    """Read and parse a YAML config file, returning the top-level dict."""
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(raw)
        if parsed and isinstance(parsed, dict):
            return dict(parsed)
        return {}
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from e


def load_config(
    config_path: Path | str | None = None,
    overrides: dict[str, Any] | None = None,
) -> MiraConfig:
    """Load config from YAML file, merge with defaults, apply overrides."""
    data: dict[str, Any] = {}

    if config_path is not None:
        path = Path(config_path)
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")
        data = _load_yaml(path)
    else:
        found = find_config_file()
        if found:
            data = _load_yaml(found)

    if overrides:
        for key, value in overrides.items():
            _set_nested(data, key.split("."), value)

    # Respect MIRA_MODEL env var as a fallback when not set via file or overrides
    env_model = os.environ.get("MIRA_MODEL")
    if env_model and "llm" not in data:
        data["llm"] = {"model": env_model}
    elif env_model and "model" not in data.get("llm", {}):
        data.setdefault("llm", {})["model"] = env_model

    try:
        return MiraConfig.model_validate(data)
    except Exception as e:
        raise ConfigError(f"Invalid configuration: {e}") from e


def _set_nested(d: dict[str, Any], keys: list[str], value: Any) -> None:
    """Set a value in a nested dict using a list of keys."""
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value
