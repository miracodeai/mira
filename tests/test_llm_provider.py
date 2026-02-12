"""Tests for LLM provider wrapper."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.config import LLMConfig
from mira.exceptions import LLMError
from mira.llm.provider import LLMProvider


def _make_completion_response(content: str = "response", usage: dict | None = None):
    """Create a mock LiteLLM completion response."""
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    resp = SimpleNamespace(choices=[choice])
    if usage is not None:
        resp.usage = SimpleNamespace(**usage)
    else:
        resp.usage = None
    return resp


class TestLLMProviderInit:
    def test_default_config(self):
        config = LLMConfig()
        provider = LLMProvider(config)
        assert provider.config.model == "openai/gpt-4o"
        assert provider.total_prompt_tokens == 0
        assert provider.total_completion_tokens == 0


class TestComplete:
    @pytest.mark.asyncio
    async def test_successful_completion(self):
        config = LLMConfig(model="test-model")
        provider = LLMProvider(config)

        mock_response = _make_completion_response(
            "hello", {"prompt_tokens": 10, "completion_tokens": 5}
        )

        with patch("mira.llm.provider.litellm") as mock_litellm:
            mock_litellm.acompletion = AsyncMock(return_value=mock_response)
            result = await provider.complete([{"role": "user", "content": "hi"}])

        assert result == "hello"
        assert provider.total_prompt_tokens == 10
        assert provider.total_completion_tokens == 5

    @pytest.mark.asyncio
    async def test_json_mode_passes_response_format(self):
        config = LLMConfig(model="test-model")
        provider = LLMProvider(config)

        mock_response = _make_completion_response("{}")

        with patch("mira.llm.provider.litellm") as mock_litellm:
            mock_litellm.acompletion = AsyncMock(return_value=mock_response)
            await provider.complete([{"role": "user", "content": "hi"}], json_mode=True)

            call_kwargs = mock_litellm.acompletion.call_args[1]
            assert call_kwargs["response_format"] == {"type": "json_object"}

    @pytest.mark.asyncio
    async def test_non_json_mode_no_response_format(self):
        config = LLMConfig(model="test-model")
        provider = LLMProvider(config)

        mock_response = _make_completion_response("text")

        with patch("mira.llm.provider.litellm") as mock_litellm:
            mock_litellm.acompletion = AsyncMock(return_value=mock_response)
            await provider.complete([{"role": "user", "content": "hi"}], json_mode=False)

            call_kwargs = mock_litellm.acompletion.call_args[1]
            assert "response_format" not in call_kwargs

    @pytest.mark.asyncio
    async def test_no_usage_tracked_when_none(self):
        config = LLMConfig(model="test-model")
        provider = LLMProvider(config)

        mock_response = _make_completion_response("ok")
        mock_response.usage = None

        with patch("mira.llm.provider.litellm") as mock_litellm:
            mock_litellm.acompletion = AsyncMock(return_value=mock_response)
            await provider.complete([{"role": "user", "content": "hi"}])

        assert provider.total_prompt_tokens == 0
        assert provider.total_completion_tokens == 0

    @pytest.mark.asyncio
    async def test_primary_failure_with_fallback(self):
        config = LLMConfig(model="primary", fallback_model="fallback")
        provider = LLMProvider(config)

        mock_response = _make_completion_response("fallback ok")

        call_count = 0

        async def _side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if kwargs["model"] == "primary":
                raise RuntimeError("primary down")
            return mock_response

        with patch("mira.llm.provider.litellm") as mock_litellm:
            mock_litellm.acompletion = AsyncMock(side_effect=_side_effect)
            result = await provider.complete([{"role": "user", "content": "hi"}])

        assert result == "fallback ok"

    @pytest.mark.asyncio
    async def test_primary_failure_no_fallback_raises(self):
        config = LLMConfig(model="primary", fallback_model=None)
        provider = LLMProvider(config)

        with patch("mira.llm.provider.litellm") as mock_litellm:
            mock_litellm.acompletion = AsyncMock(side_effect=RuntimeError("boom"))

            with pytest.raises(LLMError, match="LLM completion failed"):
                await provider.complete([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_both_models_fail_raises(self):
        config = LLMConfig(model="primary", fallback_model="fallback")
        provider = LLMProvider(config)

        with patch("mira.llm.provider.litellm") as mock_litellm:
            mock_litellm.acompletion = AsyncMock(side_effect=RuntimeError("all down"))

            with pytest.raises(LLMError, match="Both primary.*and fallback.*failed"):
                await provider.complete([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_empty_content_returns_empty_string(self):
        config = LLMConfig(model="test-model")
        provider = LLMProvider(config)

        message = SimpleNamespace(content=None)
        choice = SimpleNamespace(message=message)
        mock_response = SimpleNamespace(choices=[choice], usage=None)

        with patch("mira.llm.provider.litellm") as mock_litellm:
            mock_litellm.acompletion = AsyncMock(return_value=mock_response)
            result = await provider.complete([{"role": "user", "content": "hi"}])

        assert result == ""


class TestCountTokens:
    def test_successful_count(self):
        config = LLMConfig(model="test-model")
        provider = LLMProvider(config)

        with patch("mira.llm.provider.litellm") as mock_litellm:
            mock_litellm.token_counter = MagicMock(return_value=42)
            count = provider.count_tokens("hello world")

        assert count == 42

    def test_fallback_on_error(self):
        config = LLMConfig(model="test-model")
        provider = LLMProvider(config)

        with patch("mira.llm.provider.litellm") as mock_litellm:
            mock_litellm.token_counter = MagicMock(side_effect=Exception("no tokenizer"))
            count = provider.count_tokens("hello world test")

        # Fallback: len("hello world test") // 4 = 4
        assert count == len("hello world test") // 4


class TestUsageProperty:
    def test_usage_aggregation(self):
        config = LLMConfig(model="test-model")
        provider = LLMProvider(config)
        provider.total_prompt_tokens = 100
        provider.total_completion_tokens = 50

        usage = provider.usage
        assert usage["prompt_tokens"] == 100
        assert usage["completion_tokens"] == 50
        assert usage["total_tokens"] == 150
