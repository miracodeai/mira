"""Model resolution — reads from DB settings first, falls back to config.

Model lists, pricing, and capabilities all come from
``src/mira/llm/models.json`` via ``mira.llm.registry``. Add or remove a
model there; this file picks it up automatically.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from mira.config import IndexingTemperature, LLMConfig
from mira.llm import registry

logger = logging.getLogger(__name__)
_INDEXING_TEMPERATURE: TypeAdapter[IndexingTemperature] = TypeAdapter(IndexingTemperature)


def resolve_indexing_temperature(
    config: LLMConfig, db_value: str | None = None
) -> tuple[IndexingTemperature, Literal["dashboard", "config"]]:
    """Resolve a validated indexing temperature: DB → config → 0.0."""
    if db_value:
        try:
            return _INDEXING_TEMPERATURE.validate_json(db_value), "dashboard"
        except ValidationError:
            logger.warning(
                "Invalid saved indexing_temperature. Using deployment config. "
                "Clear the override in Models settings."
            )
    return config.indexing_temperature, "config"


MODEL_PRICING: dict[str, tuple[float, float]] = {
    model_id: registry.pricing(model_id) for model_id in registry.all_models()
}

# Thinking-mode options for the review model. "off" disables extended thinking
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

    input_price, output_price = MODEL_PRICING.get(model, (3.00, 15.00))

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


# Thinking effort used when neither the dashboard nor mira.yaml sets one. The
# published benchmark ran with it; "off" has to be chosen explicitly.
DEFAULT_REVIEW_REASONING_EFFORT = "medium"


def get_review_thinking_mode(config: LLMConfig, db_value: str | None = None) -> str | None:
    """Resolve the review thinking mode: DB → mira.yaml → the product default.

    A stored "" is unset and falls through; a stored "off" is the admin's
    explicit choice and wins over the config. "off" anywhere normalizes to
    None ("no reasoning"). The models form shows and round-trips the resolved
    mode, so it never writes an "off" the admin did not pick.
    """
    resolved = db_value or config.review_reasoning_effort or DEFAULT_REVIEW_REASONING_EFFORT
    if resolved == "off":
        return None
    return resolved


def llm_config_for(purpose: str, base: LLMConfig, *, use_dashboard: bool = True) -> LLMConfig:
    """Return an LLMConfig with the appropriate model set for the given purpose.

    Reads the DB setting first (via _app_db), falls back to config fields.
    Logs the effective model and where it came from, so a dashboard override
    shadowing mira.yaml is visible instead of silent (issue #124).
    """
    db_model: str | None = None
    db_thinking: str | None = None
    db_review: str | None = None
    db_style: str | None = None
    db_temperature: str | None = None
    if use_dashboard:
        try:
            from mira.dashboard.api import _app_db

            if _app_db is not None:
                if purpose == "indexing":
                    db_model = _app_db.get_setting("indexing_model")
                    db_temperature = _app_db.get_setting("indexing_temperature")
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

    # Thinking mode only applies to reviews; other purposes leave it off.
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

    source = "dashboard setting" if db_model else ("mira.yaml" if config_model else "default")
    logger.info("%s model: %s (source: %s)", purpose.capitalize(), resolved, source)
    return base.model_copy(
        update={
            "model": resolved,
            "reasoning_effort": thinking_mode,
            "api_style": resolved_style,
            "indexing_temperature": (
                resolve_indexing_temperature(base, db_temperature)[0]
                if purpose == "indexing"
                else base.indexing_temperature
            ),
        }
    )
