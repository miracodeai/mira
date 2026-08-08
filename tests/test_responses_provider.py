"""Tests for the OpenAI Responses API provider."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.config import LLMConfig
from mira.exceptions import LLMError, NonRetriableLLMError
from mira.llm import create_llm
from mira.llm.responses import ResponsesProvider

# Set a dummy API key for tests so _get_api_key() doesn't fail
os.environ.setdefault("OPENROUTER_API_KEY", "test-key-for-unit-tests")


def _make_resp_text(text: str, usage: dict | None = None) -> dict:
    """Create a mock Responses API response with text content."""
    resp = {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ]
    }
    if usage is not None:
        resp["usage"] = usage
    return resp


def _make_resp_usage(prompt: int, completion: int) -> dict:
    return {"input_tokens": prompt, "output_tokens": completion}


def _make_resp_tool(name: str, args: str, call_id: str = "call_1") -> dict:
    return {
        "output": [
            {
                "type": "function_call",
                "id": call_id,
                "call_id": call_id,
                "name": name,
                "arguments": args,
            }
        ],
        "usage": _make_resp_usage(50, 30),
    }


def _make_resp_text_and_tool(
    text: str, name: str, args: str, call_id: str = "call_1"
) -> dict:
    return {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            },
            {
                "type": "function_call",
                "id": call_id,
                "call_id": call_id,
                "name": name,
                "arguments": args,
            },
        ],
        "usage": _make_resp_usage(50, 30),
    }


def _mock_httpx_response(data: dict, status_code: int = 200):
    """Create a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    resp.text = json.dumps(data)
    return resp


def _mock_client(resp, extra_posts=None):
    """Build a mock AsyncClient whose .post() returns the given response(s)."""
    mock_client = AsyncMock()
    if extra_posts:
        mock_client.post = AsyncMock(side_effect=extra_posts)
    else:
        mock_client.post = AsyncMock(return_value=resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


@pytest.fixture
def config() -> LLMConfig:
    return LLMConfig(
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        api_style="responses",
    )


class TestResponsesProviderInit:
    def test_default_init(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        assert provider.config is config
        assert provider.total_prompt_tokens == 0
        assert provider.total_completion_tokens == 0
        assert provider._url == "https://api.openai.com/v1/responses"

    def test_url_with_trailing_slash(self):
        cfg = LLMConfig(base_url="https://api.openai.com/v1/", api_style="responses")
        provider = ResponsesProvider(cfg)
        assert provider._url == "https://api.openai.com/v1/responses"


class TestComplete:
    @pytest.mark.asyncio
    async def test_posts_to_responses_endpoint(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        mock_data = _make_resp_text("hello world", _make_resp_usage(100, 50))

        mock_resp = _mock_httpx_response(mock_data, 200)
        mock_client = _mock_client(mock_resp)

        with patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client):
            result = await provider.complete(
                [{"role": "user", "content": "say hello"}], json_mode=False
            )

        assert result == "hello world"
        assert mock_client.post.call_count == 1
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "https://api.openai.com/v1/responses"
        body = call_args[1]["json"]
        assert "input" in body
        assert "messages" not in body
        assert body["input"][0]["role"] == "user"
        assert body["input"][0]["content"] == "say hello"

    @pytest.mark.asyncio
    async def test_json_mode_body(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        mock_data = _make_resp_text('{"key": "val"}', _make_resp_usage(10, 10))
        mock_resp = _mock_httpx_response(mock_data, 200)
        mock_client = _mock_client(mock_resp)

        with patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client):
            await provider.complete(
                [{"role": "user", "content": "json"}], json_mode=True
            )

        body = mock_client.post.call_args[1]["json"]
        assert "input" in body
        assert "response_format" not in body
        assert body["text"]["format"]["type"] == "json_object"
        assert "max_output_tokens" in body

    @pytest.mark.asyncio
    async def test_token_accounting(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        mock_data = _make_resp_text("result", _make_resp_usage(100, 50))
        mock_resp = _mock_httpx_response(mock_data, 200)
        mock_client = _mock_client(mock_resp)

        with patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client):
            await provider.complete([{"role": "user", "content": "test"}])

        assert provider.total_prompt_tokens == 100
        assert provider.total_completion_tokens == 50
        usage = provider.usage
        assert usage["prompt_tokens"] == 100
        assert usage["completion_tokens"] == 50
        assert usage["total_tokens"] == 150

    @pytest.mark.asyncio
    async def test_non_retriable_error(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        mock_resp = _mock_httpx_response(
            {"error": {"message": "bad model"}}, 400
        )
        mock_client = _mock_client(mock_resp)

        with patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(NonRetriableLLMError):
                await provider.complete([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_retriable_error(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        mock_resp = _mock_httpx_response(
            {"error": {"message": "rate limit"}}, 500
        )
        mock_client = _mock_client(mock_resp)

        with patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(LLMError):
                await provider.complete([{"role": "user", "content": "test"}])


class TestCompleteWithTools:
    @pytest.mark.asyncio
    async def test_flat_tools_format(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        mock_data = _make_resp_tool("submit_review", '{"comments":[]}')
        mock_resp = _mock_httpx_response(mock_data, 200)
        mock_client = _mock_client(mock_resp)

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "submit_review",
                    "description": "Submit review",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        with patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client):
            result = await provider.complete_with_tools(
                [{"role": "user", "content": "review"}], tools=tools
            )

        assert result == '{"comments":[]}'
        body = mock_client.post.call_args[1]["json"]
        # Tools should be flat — no nested "function" key
        assert len(body["tools"]) == 1
        tool = body["tools"][0]
        assert tool["type"] == "function"
        assert tool["name"] == "submit_review"
        assert "function" not in tool
        assert "arguments" not in tool

    @pytest.mark.asyncio
    async def test_forced_tool_choice(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        mock_data = _make_resp_tool("submit_review", '{}')
        mock_resp = _mock_httpx_response(mock_data, 200)
        mock_client = _mock_client(mock_resp)

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "submit_review",
                    "description": "Submit review",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        with patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client):
            await provider.complete_with_tools(
                [{"role": "user", "content": "review"}], tools=tools
            )

        body = mock_client.post.call_args[1]["json"]
        assert body["tool_choice"] == {
            "type": "function",
            "name": "submit_review",
        }

    @pytest.mark.asyncio
    async def test_tool_choice_400_fallback(self, config: LLMConfig):
        provider = ResponsesProvider(config)

        err_resp = _mock_httpx_response(
            {"error": "tool_choice not supported"}, 400
        )
        ok_data = _make_resp_tool("submit_review", '{"ok":true}')
        ok_resp = _mock_httpx_response(ok_data, 200)

        mock_client = _mock_client(None, extra_posts=[err_resp, ok_resp])

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "submit_review",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        with patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client):
            result = await provider.complete_with_tools(
                [{"role": "user", "content": "review"}], tools=tools
            )

        assert result == '{"ok":true}'
        assert mock_client.post.call_count == 2
        # Second call should have "auto" tool_choice
        second_body = mock_client.post.call_args_list[1][1]["json"]
        assert second_body["tool_choice"] == "auto"


class TestCompleteAgentic:
    @pytest.mark.asyncio
    async def test_returns_chat_shaped_dict(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        mock_data = _make_resp_text_and_tool(
            "let me check", "submit_review", '{"comments":[]}'
        )
        mock_resp = _mock_httpx_response(mock_data, 200)
        mock_client = _mock_client(mock_resp)

        with patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client):
            result = await provider.complete_agentic(
                [{"role": "user", "content": "review"}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "submit_review",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
            )

        assert result["role"] == "assistant"
        assert result["content"] == "let me check"
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["id"] == "call_1"
        assert result["tool_calls"][0]["type"] == "function"
        assert result["tool_calls"][0]["function"]["name"] == "submit_review"
        assert result["tool_calls"][0]["function"]["arguments"] == '{"comments":[]}'

    @pytest.mark.asyncio
    async def test_auto_tool_choice_always(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        mock_data = _make_resp_text("done", _make_resp_usage(10, 10))
        mock_resp = _mock_httpx_response(mock_data, 200)
        mock_client = _mock_client(mock_resp)

        with patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client):
            await provider.complete_agentic(
                [{"role": "user", "content": "test"}],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "submit_review",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
            )

        body = mock_client.post.call_args[1]["json"]
        assert body["tool_choice"] == "auto"

    @pytest.mark.asyncio
    async def test_roundtrip_assistant_tool_and_tool_result(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        mock_data = _make_resp_text("result", _make_resp_usage(10, 10))
        mock_resp = _mock_httpx_response(mock_data, 200)
        mock_client = _mock_client(mock_resp)

        messages = [
            {"role": "user", "content": "review this"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path": "foo.py"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "print('hello')",
            },
        ]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        with patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client):
            await provider.complete_agentic(messages, tools=tools)

        body = mock_client.post.call_args[1]["json"]
        input_items = body["input"]

        # Find function_call item
        func_items = [i for i in input_items if i.get("type") == "function_call"]
        assert len(func_items) == 1
        func = func_items[0]
        assert func["name"] == "read_file"
        assert func["call_id"] == "call_1"

        # Find function_call_output item
        output_items = [
            i for i in input_items if i.get("type") == "function_call_output"
        ]
        assert len(output_items) == 1
        assert output_items[0]["call_id"] == "call_1"
        assert output_items[0]["output"] == "print('hello')"


class TestCreateLlmDispatch:
    def test_responses_style(self):
        provider = create_llm(LLMConfig(api_style="responses"))
        assert isinstance(provider, ResponsesProvider)

    def test_chat_style(self):
        from mira.llm.provider import LLMProvider

        provider = create_llm(LLMConfig(api_style="chat"))
        assert isinstance(provider, LLMProvider)

    def test_bedrock_ignores_api_style(self):
        from mira.llm.bedrock import BedrockProvider

        provider = create_llm(
            LLMConfig(provider="bedrock", api_style="responses")
        )
        assert isinstance(provider, BedrockProvider)
