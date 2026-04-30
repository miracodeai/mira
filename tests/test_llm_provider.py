"""Tests for LLM provider wrapper (OpenRouter via httpx)."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.config import LLMConfig
from mira.exceptions import LLMError
from mira.llm.provider import LLMProvider

# Set a dummy API key for tests so _get_api_key() doesn't fail
os.environ.setdefault("OPENROUTER_API_KEY", "test-key-for-unit-tests")


def _make_response_json(content: str = "response", usage: dict | None = None) -> dict:
    """Create a mock OpenRouter API response dict."""
    resp = {
        "choices": [
            {
                "message": {
                    "content": content,
                },
            }
        ],
    }
    if usage is not None:
        resp["usage"] = usage
    return resp


def _make_tool_response_json(arguments: str, usage: dict | None = None) -> dict:
    """Create a mock OpenRouter API response with a tool call."""
    resp = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "function": {
                                "name": "submit_review",
                                "arguments": arguments,
                            }
                        }
                    ],
                },
            }
        ],
    }
    if usage is not None:
        resp["usage"] = usage
    return resp


def _mock_httpx_response(data: dict, status_code: int = 200):
    """Create a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    resp.text = json.dumps(data)
    return resp


class TestLLMProviderInit:
    def test_default_config(self):
        config = LLMConfig()
        provider = LLMProvider(config)
        assert provider.config.model == "anthropic/claude-sonnet-4-6"
        assert provider.total_prompt_tokens == 0
        assert provider.total_completion_tokens == 0


class TestComplete:
    @pytest.mark.asyncio
    async def test_successful_completion(self):
        config = LLMConfig(model="test-model")
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response(
            _make_response_json("hello", {"prompt_tokens": 10, "completion_tokens": 5})
        )

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await provider.complete([{"role": "user", "content": "hi"}])

        assert result == "hello"
        assert provider.total_prompt_tokens == 10
        assert provider.total_completion_tokens == 5

    @pytest.mark.asyncio
    async def test_json_mode_passes_response_format(self):
        config = LLMConfig(model="test-model")
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response(_make_response_json("{}"))

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await provider.complete([{"role": "user", "content": "hi"}], json_mode=True)

            call_kwargs = mock_client.post.call_args
            body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            assert body["response_format"] == {"type": "json_object"}

    @pytest.mark.asyncio
    async def test_non_json_mode_no_response_format(self):
        config = LLMConfig(model="test-model")
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response(_make_response_json("text"))

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await provider.complete([{"role": "user", "content": "hi"}], json_mode=False)

            call_kwargs = mock_client.post.call_args
            body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            assert "response_format" not in body

    @pytest.mark.asyncio
    async def test_no_usage_tracked_when_missing(self):
        config = LLMConfig(model="test-model")
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response(_make_response_json("ok"))

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await provider.complete([{"role": "user", "content": "hi"}])

        assert provider.total_prompt_tokens == 0
        assert provider.total_completion_tokens == 0

    @pytest.mark.asyncio
    async def test_primary_failure_with_fallback(self):
        config = LLMConfig(model="primary", fallback_model="fallback")
        provider = LLMProvider(config)

        call_count = 0

        async def _side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            body = kwargs.get("json", {})
            if body.get("model") == "primary":
                return _mock_httpx_response({}, status_code=500)
            return _mock_httpx_response(_make_response_json("fallback ok"))

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=_side_effect)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await provider.complete([{"role": "user", "content": "hi"}])

        assert result == "fallback ok"

    @pytest.mark.asyncio
    async def test_primary_failure_no_fallback_raises(self):
        config = LLMConfig(model="primary", fallback_model=None)
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response({}, status_code=500)

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(LLMError, match="LLM completion failed"):
                await provider.complete([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_both_models_fail_raises(self):
        config = LLMConfig(model="primary", fallback_model="fallback")
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response({}, status_code=500)

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(LLMError, match="Both primary.*and fallback.*failed"):
                await provider.complete([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_empty_content_returns_empty_string(self):
        config = LLMConfig(model="test-model")
        provider = LLMProvider(config)

        resp_data = {"choices": [{"message": {"content": None}}]}
        mock_resp = _mock_httpx_response(resp_data)

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await provider.complete([{"role": "user", "content": "hi"}])

        assert result == ""


class TestCountTokens:
    def test_heuristic_count(self):
        config = LLMConfig(model="test-model")
        provider = LLMProvider(config)
        count = provider.count_tokens("hello world test")
        # ~4 chars per token heuristic
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
