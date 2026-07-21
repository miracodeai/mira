"""Tests for the model registry contents (2026-07 refresh)."""

from __future__ import annotations

import pytest

from mira.llm import registry


class TestCurrentGenerationModels:
    """Current-generation entries — ids and pricing verified against the live
    OpenRouter catalog."""

    @pytest.mark.parametrize(
        ("model_id", "pricing", "purposes"),
        [
            ("anthropic/claude-sonnet-5", (2.00, 10.00), ["review"]),
            ("anthropic/claude-opus-4-8", (5.00, 25.00), ["review"]),
            ("anthropic/claude-fable-5", (10.00, 50.00), ["review"]),
            ("openai/gpt-5-nano", (0.05, 0.40), ["indexing"]),
            ("openai/gpt-5-mini", (0.25, 2.00), ["indexing", "review"]),
            ("openai/gpt-5.1-codex-mini", (0.25, 2.00), ["indexing", "review"]),
            ("openai/gpt-5.1-codex", (1.25, 10.00), ["review"]),
            ("openai/gpt-5.2", (1.75, 14.00), ["review"]),
            ("google/gemini-3-flash-preview", (0.50, 3.00), ["indexing", "review"]),
            ("google/gemini-3.1-flash-lite", (0.25, 1.50), ["indexing"]),
            ("google/gemini-3.1-pro-preview", (2.00, 12.00), ["review"]),
            ("deepseek/deepseek-v4-flash", (0.09, 0.18), ["indexing"]),
            ("deepseek/deepseek-v4-pro", (0.43, 0.87), ["review"]),
            ("minimax/minimax-m3", (0.30, 1.20), ["indexing", "review"]),
        ],
    )
    def test_registered_with_pricing_and_purposes(self, model_id, pricing, purposes):
        assert registry.pricing(model_id) == pricing
        for purpose in purposes:
            assert registry.is_supported(model_id, purpose=purpose)
            assert model_id in [m["value"] for m in registry.models_for_purpose(purpose)]

    def test_recommended_defaults_unchanged(self):
        # Recommended stays on the eval-validated pair until benchmarks say
        # otherwise (v10 baseline was measured on Sonnet 4.6 / Haiku 4.5).
        indexing = registry.models_for_purpose("indexing")
        review = registry.models_for_purpose("review")
        assert [m["value"] for m in indexing if m["recommended"]] == [
            "anthropic/claude-haiku-4-5",
            "us.anthropic.claude-haiku-4-5-v1:0",
        ]
        assert [m["value"] for m in review if m["recommended"]] == [
            "anthropic/claude-sonnet-4-6",
            "us.anthropic.claude-sonnet-4-6-v1:0",
        ]

    def test_superseded_models_removed(self):
        for model_id in (
            "openai/gpt-4o",
            "openai/gpt-4o-mini",
            "openai/gpt-4.1-mini",
            "google/gemini-2.5-flash",
            "google/gemini-2.5-pro",
            "minimax/MiniMax-M2.7",
            "anthropic/claude-opus-4-6",
        ):
            assert registry.get(model_id) is None
