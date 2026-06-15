"""Configuration loading and validation for Mira."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from mira.exceptions import ConfigError

logger = logging.getLogger(__name__)

# `.mira.yaml` is the canonical per-repo override filename. `.mira.yaml`
# is accepted for backward compat with repos that committed it before the
# 0.1.1 standardization on the .yaml extension.
_DEFAULT_CONFIG_FILENAMES = (".mira.yaml", ".mira.yml")


class LLMConfig(BaseModel):
    model: str = "anthropic/claude-sonnet-4-6"
    fallback_model: str | None = None
    # Optional per-purpose overrides. Fall back to `model` if not set.
    indexing_model: str | None = None
    review_model: str | None = None
    # Extended-thinking effort for reviews ("low"/"medium"/"high"; None/"off" =
    # no reasoning). `review_reasoning_effort` is the mira.yaml-level override;
    # `reasoning_effort` is the resolved value the provider reads (set by
    # `llm_config_for`, the same way `model` is resolved from `review_model`).
    review_reasoning_effort: str | None = None
    reasoning_effort: str | None = None
    temperature: float = 0.2
    max_tokens: int = 4096
    max_context_tokens: int = 120_000
    # Provider selection. "openai" uses any OpenAI-compatible endpoint (default).
    # "bedrock" uses AWS Bedrock Converse API directly (requires boto3).
    provider: str = "openai"
    # Endpoint configuration. Defaults to OpenRouter but any OpenAI-compatible
    # chat-completions endpoint works — vLLM, Ollama, LiteLLM proxy, LocalAI,
    # llama.cpp server, Together, Fireworks, Groq, etc. Set api_key_env to ""
    # for local endpoints that don't require auth.
    base_url: str = "https://openrouter.ai/api/v1"
    api_key_env: str = "OPENROUTER_API_KEY"
    # AWS Bedrock settings. Auth uses the standard AWS credential chain
    # (env vars, instance profile, ECS task role, SSO).
    region: str = "us-east-1"
    aws_profile: str | None = None


class FilterConfig(BaseModel):
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    # Per-category floors layered over confidence_threshold (the higher wins).
    # Lets noisy categories (e.g. "security" from the cheap-model pass) be
    # held to a stricter bar without raising the global floor.
    category_confidence_thresholds: dict[str, float] = Field(default_factory=dict)
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
    # Total diff size cap. Above this, the diff is *not* truncated arbitrarily —
    # files are ranked by priority and the lowest-priority files are skipped
    # until the diff fits. Skipped files are listed in the walkthrough so the
    # user can invoke `@mira-bot review-rest` to review them.
    max_diff_size: int = 250_000
    # Per-file size cap. A single huge file (lockfile, generated SDK, etc.)
    # gets skipped before chunking even starts.
    max_file_size: int = 50_000
    # Hard ceiling on chunks per single review pass. If the diff would split
    # into more chunks, only the top-priority N are reviewed; the rest are
    # listed as skipped.
    max_chunks_per_review: int = Field(default=5, ge=1, le=20)
    include_summary: bool = True
    focus_only_on_problems: bool = False
    walkthrough: bool = True
    walkthrough_sequence_diagram: bool = True
    code_context: bool = True
    context_token_budget: int = 8_000
    max_concurrent_chunks: int = Field(default=5, ge=1, le=20)
    # Review each chunk N times and keep only majority-vote findings.
    # 1 = off (single pass, exact current behavior). 3 is the sweet spot:
    # variance FPs flicker across runs, real findings recur. Runs fire in
    # parallel so wall clock stays ~flat, but token cost multiplies by N —
    # this is the opt-in "thorough" tier, not the default.
    ensemble_runs: int = Field(default=1, ge=1, le=5)
    # Sampling temperature for the extra ensemble runs (the first run keeps
    # the configured llm.temperature). Mild diversity makes the vote useful.
    ensemble_temperature: float = Field(default=0.3, ge=0.0, le=1.0)

    # Run a second-pass LLM critique on each draft comment before posting.
    # The critic asks "is this analysis actually correct? Cite specific
    # lines that prove it." Comments that fail the critique are dropped.
    # Disable for faster reviews where the extra wall-clock time matters
    # more than catching confident-but-wrong findings.
    self_critique: bool = True

    # Run a dedicated security review pass in parallel with the main review.
    # Uses the *indexing* tier LLM with a security-focused prompt (XSS,
    # injection, auth bypass, CSRF, SSRF, origin validation, deserialization,
    # crypto). The main pass on the review tier still catches deeper
    # chained-inference security bugs — this pass is the cheap pattern-
    # matching sweep on top. Set ``llm.indexing_model`` to the same model
    # as ``llm.review_model`` if you want the heavy model on every pass.
    # Findings are merged into the main review's comments list and go
    # through the same noise filter (dedup against overlapping main-pass
    # findings).
    security_pass: bool = True

    # When the repo is not indexed, give the reviewer LLM tools (`read_file`,
    # `grep_repo`) it can call to fetch cross-file context on demand. Closes
    # the gap on Java/Go cross-file findings that JIT pre-fetch can't reach
    # (those languages need build-system parsing to resolve imports).
    # No effect when the repo is indexed.
    agentic_tools: bool = True

    # Whether the JIT cross-file resolver should attempt Java + Go imports.
    # Resolution for those languages is heuristic (we can't see the build
    # system), and a wrong-file pick pollutes the prompt with off-topic
    # symbols. Toggle off when measuring whether Java/Go JIT is helping vs
    # hurting on a given codebase. The agentic loop still covers cross-file
    # needs for Java/Go on the unindexed path when this is False.
    jit_java_go: bool = True

    # Render the cross-repo "Blast Radius" section in the walkthrough comment.
    # Lists dependent repos that import code touched by this PR. Disable to
    # skip the relationship-store lookup and trim the walkthrough.
    blast_radius: bool = True


class IndexConfig(BaseModel):
    # Skip indexing any file larger than this (bytes). Generated SDKs, vendored
    # bundles and large test fixtures burn indexing tokens for little value.
    # Defaults to the previous hard-coded tarball cap (1 MB) so it's a no-op
    # until lowered; 0 disables the limit. In bytes, matching review.max_file_size.
    max_file_size: int = Field(default=1024 * 1024, ge=0)


class ProviderConfig(BaseModel):
    type: str = "github"


class DatabaseConfig(BaseModel):
    url: str = ""  # empty = SQLite fallback. "postgresql://user:pass@host:5432/mira"
    admin_password: str = "admin"  # default admin password, change in production


class MiraConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    filter: FilterConfig = Field(default_factory=FilterConfig)
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    index: IndexConfig = Field(default_factory=IndexConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)


def find_config_file(start_dir: Path | None = None) -> Path | None:
    """Walk up from start_dir looking for `.mira.yaml` (or legacy `.mira.yaml`)."""
    current = start_dir or Path.cwd()
    for directory in [current, *current.parents]:
        for name in _DEFAULT_CONFIG_FILENAMES:
            candidate = directory / name
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


_global_defaults: dict[str, Any] = {}


def set_global_defaults(config_path: Path | str) -> MiraConfig:
    """Load a deployment-wide config file once at server startup.

    Subsequent `load_config()` calls deep-merge per-repo `.mira.yaml` (and
    env-var fallbacks) over these defaults.
    """
    global _global_defaults
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    _global_defaults = _load_yaml(path)
    # Validate eagerly so a malformed file fails server boot, not first review.
    return load_config()


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Right-biased deep merge — overlay wins; nested dicts recurse."""
    out: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(
    config_path: Path | str | None = None,
    overrides: dict[str, Any] | None = None,
) -> MiraConfig:
    """Load config, layering global defaults → per-repo `.mira.yaml` → overrides.

    Sources, lowest priority first:
      1. Built-in pydantic defaults (`MiraConfig()`).
      2. Deployment-wide defaults loaded via `set_global_defaults(...)`.
      3. Admin-editable runtime overrides stored in the dashboard DB
         (Settings page). Optional — falls through cleanly if no DB is
         available (CLI usage, tests, etc.).
      4. Per-repo `.mira.yaml` (auto-discovered by walking up from cwd, OR
         the explicit `config_path` if passed).
      5. Caller-supplied `overrides` dict.
      6. `DATABASE_URL` / `MIRA_MODEL` env-var fallbacks.
    """
    data: dict[str, Any] = _deep_merge({}, _global_defaults)

    # Lazy import + broad except: this function runs in CLI / test contexts
    # that have no DB attached. A DB error must never block a review.
    try:
        from mira.dashboard.api import _app_db

        if _app_db is not None:
            db_overrides = _app_db.get_global_review_overrides()
            if db_overrides:
                data = _deep_merge(data, db_overrides)
    except Exception as _db_exc:  # noqa: BLE001
        logger.debug("load_config: skipping DB overrides (%s)", _db_exc)

    if config_path is not None:
        path = Path(config_path)
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")
        data = _deep_merge(data, _load_yaml(path))
    else:
        found = find_config_file()
        if found:
            data = _deep_merge(data, _load_yaml(found))

    if overrides:
        for key, value in overrides.items():
            _set_nested(data, key.split("."), value)

    # Respect DATABASE_URL env var
    env_db_url = os.environ.get("DATABASE_URL")
    if env_db_url and "database" not in data:
        data["database"] = {"url": env_db_url}
    elif env_db_url and "url" not in data.get("database", {}):
        data.setdefault("database", {})["url"] = env_db_url

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
