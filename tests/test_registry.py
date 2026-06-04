"""Tests for the model registry including MiniMax-2.7."""

from __future__ import annotations

from mira.llm import registry


class TestMiniMaxInRegistry:
    """MiniMax-M2.7 must be registered and usable for both indexing and review."""

    def test_minimax_in_all_models(self):
        assert "minimax/MiniMax-M2.7" in registry.all_models()

    def test_minimax_entry_structure(self):
        info = registry.get("minimax/MiniMax-M2.7")
        assert info["label"] == "MiniMax M2.7"
        assert info["provider"] == "minimax"
        assert info["max_input_tokens"] == 1000000
        assert info["max_output_tokens"] == 131072
        assert info["supports_json_mode"] is True

    def test_minimax_supports_indexing(self):
        assert registry.is_supported("minimax/MiniMax-M2.7", purpose="indexing")

    def test_minimax_supports_review(self):
        assert registry.is_supported("minimax/MiniMax-M2.7", purpose="review")

    def test_minimax_in_indexing_models_list(self):
        indexing = registry.models_for_purpose("indexing")
        values = [m["value"] for m in indexing]
        assert "minimax/MiniMax-M2.7" in values

    def test_minimax_in_review_models_list(self):
        review = registry.models_for_purpose("review")
        values = [m["value"] for m in review]
        assert "minimax/MiniMax-M2.7" in values

    def test_minimax_pricing(self):
        inp, out = registry.pricing("minimax/MiniMax-M2.7")
        assert inp == 0.30
        assert out == 2.50

    def test_minimax_max_output_tokens(self):
        assert registry.max_output_tokens("minimax/MiniMax-M2.7") == 131072


class TestRegistryRegression:
    """Existing models must not be affected by the MiniMax addition."""

    def test_existing_models_still_present(self):
        assert "anthropic/claude-sonnet-4-6" in registry.all_models()
        assert "google/gemini-2.5-flash" in registry.all_models()

    def test_existing_indexing_models_still_work(self):
        indexing = registry.models_for_purpose("indexing")
        values = [m["value"] for m in indexing]
        assert "google/gemini-2.5-flash" in values

    def test_existing_review_models_still_work(self):
        review = registry.models_for_purpose("review")
        values = [m["value"] for m in review]
        assert "anthropic/claude-sonnet-4-6" in values
