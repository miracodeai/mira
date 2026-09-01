"""Tests for LLM provider wrapper (OpenRouter via httpx)."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.config import LLMConfig
from mira.exceptions import LLMError, NonRetriableLLMError
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
    async def test_reasoning_effort_sets_reasoning_and_drops_temperature(self):
        config = LLMConfig(model="test-model", reasoning_effort="high")
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response(_make_response_json("ok"))

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await provider.complete([{"role": "user", "content": "hi"}])

            call_kwargs = mock_client.post.call_args
            body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
            assert body["reasoning"] == {"effort": "high"}
            # Anthropic rejects a custom temperature while thinking — we drop it.
            assert "temperature" not in body

    @pytest.mark.asyncio
    async def test_max_effort_maps_to_xhigh_on_openrouter(self):
        # OpenRouter rejects "max"; "xhigh" is its equivalent top level.
        config = LLMConfig(model="test-model", reasoning_effort="max")
        provider = LLMProvider(config)
        mock_resp = _mock_httpx_response(_make_response_json("ok"))

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await provider.complete([{"role": "user", "content": "hi"}])
            body = mock_client.post.call_args.kwargs["json"]
            assert body["reasoning"] == {"effort": "xhigh"}

    @pytest.mark.asyncio
    async def test_max_effort_passes_through_on_non_openrouter(self):
        # DeepSeek's native API accepts "max" verbatim.
        config = LLMConfig(
            model="deepseek-reasoner",
            reasoning_effort="max",
            base_url="https://api.deepseek.com/v1",
        )
        provider = LLMProvider(config)
        mock_resp = _mock_httpx_response(_make_response_json("ok"))

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await provider.complete([{"role": "user", "content": "hi"}])
            body = mock_client.post.call_args.kwargs["json"]
            assert body["reasoning"] == {"effort": "max"}

    @pytest.mark.asyncio
    async def test_reasoning_off_leaves_body_unchanged(self):
        for effort in (None, "off"):
            config = LLMConfig(model="test-model", reasoning_effort=effort)
            provider = LLMProvider(config)

            mock_resp = _mock_httpx_response(_make_response_json("ok"))

            with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=mock_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                await provider.complete([{"role": "user", "content": "hi"}])

                call_kwargs = mock_client.post.call_args
                body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
                assert "reasoning" not in body
                assert "temperature" in body

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

    @pytest.mark.asyncio
    async def test_config_timeout_passed_to_httpx(self):
        config = LLMConfig(model="test-model", request_timeout=300)
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response(_make_response_json("ok"))

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await provider.complete([{"role": "user", "content": "hi"}])

            assert mock_client_cls.call_args.kwargs["timeout"] == 300

    @pytest.mark.asyncio
    async def test_config_retries_count(self):
        config = LLMConfig(
            model="test-model",
            max_retries=5,
            retry_min_wait=0,
            retry_max_wait=0,
        )
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response({}, status_code=500)

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(LLMError):
                await provider.complete([{"role": "user", "content": "hi"}])

            assert mock_client.post.call_count == 5

    @pytest.mark.asyncio
    async def test_4xx_raises_non_retriable_error(self):
        """4xx errors (except 429) raise NonRetriableLLMError and skip retry."""
        config = LLMConfig(
            model="test-model",
            max_retries=5,
            retry_min_wait=0,
            retry_max_wait=0,
        )
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response({}, status_code=400)

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(NonRetriableLLMError):
                await provider.complete([{"role": "user", "content": "hi"}])

            # Non-retriable: only 1 attempt, no retries
            assert mock_client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_429_raises_retriable_error(self):
        """429 (rate limit) raises LLMError and retries."""
        config = LLMConfig(
            model="test-model",
            max_retries=3,
            retry_min_wait=0,
            retry_max_wait=0,
        )
        provider = LLMProvider(config)

        mock_resp = _mock_httpx_response({}, status_code=429)

        with patch("mira.llm.provider.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(LLMError):
                await provider.complete([{"role": "user", "content": "hi"}])

            # Retriable: 3 attempts
            assert mock_client.post.call_count == 3


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


class TestStripModelPrefix:
    """Tests for _strip_model_prefix — provider prefix stripping for non-OpenRouter endpoints."""

    def test_openrouter_url_strips_openrouter_prefix(self):
        from mira.llm.provider import _strip_model_prefix

        result = _strip_model_prefix("openrouter/deepseek-r1", "https://openrouter.ai/api/v1")
        assert result == "deepseek-r1"

    def test_openrouter_url_preserves_non_openrouter_prefix(self):
        from mira.llm.provider import _strip_model_prefix

        result = _strip_model_prefix("anthropic/claude-sonnet-4-6", "https://openrouter.ai/api/v1")
        assert result == "anthropic/claude-sonnet-4-6"

    def test_openrouter_url_preserves_model_without_prefix(self):
        from mira.llm.provider import _strip_model_prefix

        result = _strip_model_prefix("gpt-4o", "https://openrouter.ai/api/v1")
        assert result == "gpt-4o"

    def test_non_openrouter_url_strips_provider_prefix(self):
        from mira.llm.provider import _strip_model_prefix

        result = _strip_model_prefix("minimax/MiniMax-M2.7", "https://api.minimax.io/v1")
        assert result == "MiniMax-M2.7"

    def test_non_openrouter_url_strips_anthropic_prefix(self):
        from mira.llm.provider import _strip_model_prefix

        result = _strip_model_prefix("anthropic/claude-sonnet-4-6", "https://api.anthropic.com/v1")
        assert result == "claude-sonnet-4-6"

    def test_non_openrouter_url_preserves_model_without_prefix(self):
        from mira.llm.provider import _strip_model_prefix

        result = _strip_model_prefix("gpt-4o", "https://api.openai.com/v1")
        assert result == "gpt-4o"

    def test_non_openrouter_url_local_ollama(self):
        from mira.llm.provider import _strip_model_prefix

        result = _strip_model_prefix("llama3.1:latest", "http://localhost:11434/v1")
        assert result == "llama3.1:latest"


class TestProfileHeaders:
    """Provider-specific headers come from the matched profile, not hardcoding."""

    def test_openrouter_adds_ranking_headers(self):
        provider = LLMProvider(LLMConfig(model="m"))  # default base_url = openrouter
        headers = provider._build_headers()
        assert headers["HTTP-Referer"] == "https://github.com/miracodeai/mira"
        assert headers["X-Title"] == "Mira Code Reviewer"

    def test_other_endpoint_has_no_ranking_headers(self):
        provider = LLMProvider(LLMConfig(model="m", base_url="https://api.groq.com/openai/v1"))
        headers = provider._build_headers()
        assert "HTTP-Referer" not in headers
        assert "X-Title" not in headers
        assert headers["Authorization"].startswith("Bearer ")


class TestToolChoiceFallback:
    """#82: thinking models (deepseek) 400 on a forced tool_choice; retry
    with "auto" rather than failing the review."""

    _TOOL = {"type": "function", "function": {"name": "submit_review", "parameters": {}}}

    def _client(self, responses):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=responses)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        return mock_client

    @pytest.mark.asyncio
    async def test_retries_with_auto_on_tool_choice_400(self):
        provider = LLMProvider(LLMConfig(model="deepseek/deepseek-v4-pro"))
        rejected = _mock_httpx_response(
            {"error": {"message": "thinking mode does not support this tool_choice"}},
            status_code=400,
        )
        ok = _mock_httpx_response(_make_tool_response_json('{"comments": []}'))

        with patch("mira.llm.provider.httpx.AsyncClient") as cls:
            cls.return_value = self._client([rejected, ok])
            result = await provider.complete_with_tools(
                [{"role": "user", "content": "hi"}], tools=[self._TOOL]
            )
            n_posts = len(cls.return_value.post.call_args_list)

        assert result == '{"comments": []}'
        assert n_posts == 2  # forced 400'd, then the auto retry
        assert "deepseek/deepseek-v4-pro" in provider._no_forced_tool_choice

    @pytest.mark.asyncio
    async def test_remembered_model_skips_forced_attempt(self):
        provider = LLMProvider(LLMConfig(model="deepseek/deepseek-v4-pro"))
        provider._no_forced_tool_choice.add("deepseek/deepseek-v4-pro")
        ok = _mock_httpx_response(_make_tool_response_json('{"comments": []}'))

        with patch("mira.llm.provider.httpx.AsyncClient") as cls:
            cls.return_value = self._client([ok])
            await provider.complete_with_tools(
                [{"role": "user", "content": "hi"}], tools=[self._TOOL]
            )
            posts = cls.return_value.post.call_args_list

        assert len(posts) == 1  # straight to auto, no wasted forced attempt
        assert posts[0].kwargs["json"]["tool_choice"] == "auto"

    @pytest.mark.asyncio
    async def test_unrelated_400_does_not_trigger_auto_fallback(self):
        # A non-tool_choice 400 should error out, not flip the model to auto.
        provider = LLMProvider(LLMConfig(model="anthropic/claude-sonnet-4-6"))
        err = _mock_httpx_response(
            {"error": {"message": "context length exceeded"}}, status_code=400
        )
        client = AsyncMock()
        client.post = AsyncMock(return_value=err)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("mira.llm.provider.httpx.AsyncClient", return_value=client),
            pytest.raises(LLMError),
        ):
            await provider.complete_with_tools(
                [{"role": "user", "content": "hi"}], tools=[self._TOOL]
            )
        assert "anthropic/claude-sonnet-4-6" not in provider._no_forced_tool_choice


class TestReasoningFallback:
    """Thinking mode is opt-in and applied to whatever model is selected; a
    model/endpoint that rejects a reasoning effort must degrade to a normal
    review, not fail it."""

    _TOOL = {"type": "function", "function": {"name": "submit_review", "parameters": {}}}

    def _client(self, responses):
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=responses)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        return mock_client

    @pytest.mark.asyncio
    async def test_retries_without_reasoning_on_400(self):
        provider = LLMProvider(LLMConfig(model="some/model", reasoning_effort="high"))
        rejected = _mock_httpx_response(
            {"error": {"message": "reasoning_effort: Invalid option"}}, status_code=400
        )
        ok = _mock_httpx_response(_make_tool_response_json('{"comments": []}'))

        with patch("mira.llm.provider.httpx.AsyncClient") as cls:
            cls.return_value = self._client([rejected, ok])
            result = await provider.complete_with_tools(
                [{"role": "user", "content": "hi"}], tools=[self._TOOL]
            )
            posts = cls.return_value.post.call_args_list

        assert result == '{"comments": []}'
        assert len(posts) == 2  # reasoning 400'd, then retried without it
        assert "reasoning" not in posts[1].kwargs["json"]  # dropped on the retry
        assert "some/model" in provider._no_reasoning

    @pytest.mark.asyncio
    async def test_remembered_model_skips_reasoning(self):
        provider = LLMProvider(LLMConfig(model="some/model", reasoning_effort="high"))
        provider._no_reasoning.add("some/model")
        ok = _mock_httpx_response(_make_tool_response_json('{"comments": []}'))

        with patch("mira.llm.provider.httpx.AsyncClient") as cls:
            cls.return_value = self._client([ok])
            await provider.complete_with_tools(
                [{"role": "user", "content": "hi"}], tools=[self._TOOL]
            )
            posts = cls.return_value.post.call_args_list

        assert len(posts) == 1  # no wasted reasoning attempt
        assert "reasoning" not in posts[0].kwargs["json"]
