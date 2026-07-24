"""Default model resolution for the dashboard model pickers."""

from __future__ import annotations

from mira.config import LLMConfig
from mira.dashboard.models_config import get_indexing_model, get_review_model


def test_indexing_falls_back_to_config_model():
    # No override anywhere → the configured model is honored as-is, even if
    # it's review-tier; the combobox accepts free-form ids so nothing needs
    # to match a registry entry.
    cfg = LLMConfig()
    assert get_indexing_model(cfg) == cfg.model


def test_review_default_unchanged_when_model_is_review_capable():
    assert get_review_model(LLMConfig()) == "anthropic/claude-sonnet-4-6"


def test_explicit_choices_are_respected():
    # DB value wins outright.
    assert get_indexing_model(LLMConfig(), db_value="openai/gpt-4o-mini") == "openai/gpt-4o-mini"
    # config.indexing_model wins over the generic fallback, even if custom.
    assert get_indexing_model(LLMConfig(indexing_model="custom/local")) == "custom/local"


def test_indexing_capable_model_passes_through():
    # If config.model is itself indexing-capable, keep it.
    cfg = LLMConfig(model="anthropic/claude-haiku-4-5")
    assert get_indexing_model(cfg) == "anthropic/claude-haiku-4-5"
