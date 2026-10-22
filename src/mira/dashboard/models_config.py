"""Model resolution — reads from DB settings first, falls back to config.

Model lists, pricing, and capabilities all come from
``src/mira/llm/models.json`` via ``mira.llm.registry``. Add or remove a
model there; this file picks it up automatically.
"""

from __future__ import annotations

import logging

from mira.config import LLMConfig
from mira.llm import registry

logger = logging.getLogger(__name__)

# Thinking-mode options for each model slot. "off" disables extended thinking
# (today's behavior); low/medium/high/xhigh map to the provider's unified
# ``reasoning.effort``; "max" is a top level remapped per provider (OpenRouter
# sends it as "xhigh"). Single source for the dashboard dropdown and validation.
THINKING_MODES: list[dict[str, str]] = [
    {"value": "off", "label": "Off"},
    {"value": "low", "label": "Low"},
    {"value": "medium", "label": "Medium"},
    {"value": "high", "label": "High"},
    {"value": "xhigh", "label": "XHigh"},
    # Top "max" level (sent as "xhigh" on OpenRouter, which rejects "max").
    # Sits above "xhigh". Not every provider supports it.
    {"value": "max", "label": "Max"},
]
THINKING_MODE_VALUES = {m["value"] for m in THINKING_MODES}

# API-protocol options for the Models page. Single source for the dropdown
# and validation, mirroring THINKING_MODES.
API_STYLES: list[dict[str, str]] = [
    {"value": "chat", "label": "Chat Completions"},
    {"value": "responses", "label": "Responses API"},
]
API_STYLE_VALUES = {m["value"] for m in API_STYLES}


def resolve_api_style(config: LLMConfig, db_value: str | None = None) -> str:
    """Resolve the API protocol: DB → config.api_style → "chat"."""
    if db_value and db_value in API_STYLE_VALUES:
        return db_value
    return config.api_style if config.api_style in API_STYLE_VALUES else "chat"


def estimate_indexing_cost(file_count: int, model: str) -> dict:
    """Estimate cost of indexing N files with the given model.

    Based on actual indexer behavior:
    - Files batched 5-at-a-time
    - Each batch uses ~4K input tokens (prompt + 5 file contents ~500 lines avg)
    - Each batch outputs ~2K tokens (summaries + symbols JSON)
    - Plus a directory summarization pass at the end (~1 call per 10 files)
    """
    if file_count == 0:
        return {"estimated_usd": 0.0, "input_tokens": 0, "output_tokens": 0}

    input_price, output_price = registry.pricing(model)

    # File summarization batches
    batches = (file_count + 4) // 5  # ceil div
    # Estimate: 800 tokens per file input, 400 tokens per file output
    input_tokens = file_count * 800 + batches * 500  # +prompt overhead per batch
    output_tokens = file_count * 400

    # Directory summarization pass
    dir_batches = max(1, file_count // 10)
    input_tokens += dir_batches * 1500
    output_tokens += dir_batches * 300

    cost = (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price

    return {
        "estimated_usd": round(cost, 2),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def get_indexing_model(config: LLMConfig, db_value: str | None = None) -> str:
    """Resolve the indexing model: DB → config.indexing_model → config.model."""
    if db_value:
        return db_value
    if config.indexing_model:
        return config.indexing_model
    return config.model


def get_review_model(config: LLMConfig, db_value: str | None = None) -> str:
    """Resolve the review model: DB → config.review_model → config.model."""
    if db_value:
        return db_value
    if config.review_model:
        return config.review_model
    return config.model


def get_security_model(
    config: LLMConfig,
    db_value: str | None = None,
    db_review_model: str | None = None,
) -> str:
    """Resolve the security-pass model: DB → config.security_model → review tier.

    The review-tier fallback includes the dashboard's review_model setting
    (``db_review_model``) — without it, an instance whose review model lives
    only in the DB silently falls all the way back to ``config.model``.

    Never falls back to ``indexing_model`` — the security sweep is the
    highest-stakes cheap pass, and silently downgrading it to the indexing
    tier trades security recall for indexing cost savings.
    """
    if db_value:
        return db_value
    if config.security_model:
        return config.security_model
    return get_review_model(config, db_review_model)


def get_review_thinking_mode(config: LLMConfig, db_value: str | None = None) -> str | None:
    """Resolve the review thinking mode: DB → config.review_reasoning_effort → None.

    An empty DB value inherits mira.yaml; explicit "off" disables reasoning.
    """
    resolved = db_value or config.review_reasoning_effort
    if not resolved or resolved == "off":
        return None
    return resolved


def llm_config_for(purpose: str, base: LLMConfig) -> LLMConfig:
    """Return an LLMConfig with the appropriate model set for the given purpose.

    Reads the DB setting first (via _app_db), falls back to config fields.
    Logs the effective model and where it came from, so a dashboard override
    shadowing mira.yaml is visible instead of silent (issue #124).
    """
    db_model: str | None = None
    db_thinking: str | None = None
    db_review: str | None = None
    db_style: str | None = None
    db_fallbacks: str | None = None
    try:
        from mira.dashboard.api import _app_db

        if _app_db is not None:
            db_fallbacks = _app_db.get_setting(f"{purpose}_fallback_models")
            if purpose == "security" and db_fallbacks is None:
                db_fallbacks = _app_db.get_setting("review_fallback_models")
            if purpose == "indexing":
                db_model = _app_db.get_setting("indexing_model")
            elif purpose == "review":
                db_model = _app_db.get_setting("review_model")
                db_thinking = _app_db.get_setting("review_thinking_mode")
            elif purpose == "security":
                db_model = _app_db.get_setting("security_model")
                db_thinking = _app_db.get_setting("review_thinking_mode")
                db_review = _app_db.get_setting("review_model")
            db_style = _app_db.get_setting("api_style")
    except Exception:
        pass  # DB not available — resolve from config fields alone

    thinking_mode: str | None = None
    resolved_style = resolve_api_style(base, db_style)
    if purpose == "indexing":
        resolved = get_indexing_model(base, db_model)
        config_model = base.indexing_model
    elif purpose == "security":
        resolved = get_security_model(base, db_model, db_review)
        config_model = base.security_model or base.review_model
        thinking_mode = get_review_thinking_mode(base, db_thinking)
    elif purpose == "review":
        resolved = get_review_model(base, db_model)
        config_model = base.review_model
        thinking_mode = get_review_thinking_mode(base, db_thinking)
    else:
        return base.model_copy(update={"reasoning_effort": None, "api_style": resolved_style})

    if purpose != "review":
        effort = _db_setting(f"{purpose}_reasoning") or thinking_mode
        thinking_mode = None if effort == "off" else effort

    source = "dashboard setting" if db_model else ("mira.yaml" if config_model else "default")
    logger.info("%s model: %s (source: %s)", purpose.capitalize(), resolved, source)
    update: dict = {
        "model": resolved,
        "reasoning_effort": thinking_mode,
        "api_style": resolved_style,
    }
    # A saved "" is an explicit empty list; only a missing setting falls back to mira.yaml.
    if db_fallbacks is not None:
        update["fallback_models"] = parse_fallbacks(db_fallbacks)
    return base.model_copy(update=update)


def parse_fallbacks(value: str | None) -> list[str]:
    """Comma-separated fallback ids as stored in the settings table."""
    return [m.strip() for m in (value or "").split(",") if m.strip()]


def _db_setting(key: str) -> str | None:
    try:
        from mira.dashboard.api import _app_db

        return _app_db.get_setting(key) if _app_db is not None else None
    except Exception:
        return None


def critique_config(base: LLMConfig) -> LLMConfig | None:
    """Critic config, or None when the indexing provider already has the same effort."""
    stored = _db_setting("critique_model")
    model = stored if stored is not None else base.critique_model
    indexing = llm_config_for("indexing", base)
    effort = _db_setting("critique_reasoning") or None
    effort = None if effort == "off" else effort
    if not model and effort == indexing.reasoning_effort:
        return None
    return indexing.model_copy(
        update={
            "model": model or indexing.model,
            "reasoning_effort": effort,
            "fallback_model": None,
            "fallback_models": [],
        }
    )


def ensemble_configs(base: LLMConfig) -> list[LLMConfig]:
    """Review-tier configs for each second-opinion model (DB -> mira.yaml)."""
    stored = _db_setting("ensemble_models")
    models = parse_fallbacks(stored) if stored is not None else base.ensemble_models
    if not models:
        return []
    review = llm_config_for("review", base)
    return [
        review.model_copy(update={"model": m, "fallback_model": None, "fallback_models": []})
        for m in models
    ]
