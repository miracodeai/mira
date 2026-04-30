"""Model resolution — reads from DB settings first, falls back to config.

Model lists, pricing, and capabilities all come from
``src/mira/llm/models.json`` via ``mira.llm.registry``. Add or remove a
model there; this file picks it up automatically.
"""

from __future__ import annotations

from mira.config import LLMConfig
from mira.llm import registry

# ── Backwards-compatible accessors ──
# Older imports of MODEL_PRICING / INDEXING_MODELS / REVIEW_MODELS still
# work; they now derive from the registry.

MODEL_PRICING: dict[str, tuple[float, float]] = {
    model_id: registry.pricing(model_id) for model_id in registry.all_models()
}

INDEXING_MODELS = registry.models_for_purpose("indexing")
REVIEW_MODELS = registry.models_for_purpose("review")


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


def llm_config_for(purpose: str, base: LLMConfig) -> LLMConfig:
    """Return an LLMConfig with the appropriate model set for the given purpose.

    Reads the DB setting first (via _app_db), falls back to config fields.
    """
    try:
        from mira.dashboard.api import _app_db

        if purpose == "indexing":
            db_val = _app_db.get_setting("indexing_model")
            resolved = get_indexing_model(base, db_val)
        elif purpose == "review":
            db_val = _app_db.get_setting("review_model")
            resolved = get_review_model(base, db_val)
        else:
            resolved = base.model
    except Exception:
        # DB not available — fall back to config fields
        if purpose == "indexing":
            resolved = base.indexing_model or base.model
        elif purpose == "review":
            resolved = base.review_model or base.model
        else:
            resolved = base.model

    return base.model_copy(update={"model": resolved})
