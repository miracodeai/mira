"""Tests for the OpenAI Responses API provider."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.config import LLMConfig
from mira.exceptions import LLMError, NonRetriableLLMError
from mira.llm import create_llm
from mira.llm.responses import ResponsesProvider, _ensure_json_word

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


def _make_resp_text_and_tool(text: str, name: str, args: str, call_id: str = "call_1") -> dict:
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
    # api_key_env="OPENAI_API_KEY" is intentional: the module-level
    # ``os.environ.setdefault("OPENROUTER_API_KEY", ...)`` means the
    # fixture exercises ``_get_api_key``'s back-compat fallback chain
    # (config env var -> profile override -> OPENROUTER_API_KEY fallback).
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
            await provider.complete([{"role": "user", "content": "json"}], json_mode=True)

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
        mock_resp = _mock_httpx_response({"error": {"message": "bad model"}}, 400)
        mock_client = _mock_client(mock_resp)

        with (
            patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(NonRetriableLLMError),
        ):
            await provider.complete([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_retriable_error(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        mock_resp = _mock_httpx_response({"error": {"message": "rate limit"}}, 500)
        mock_client = _mock_client(mock_resp)

        with (
            patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client),
            pytest.raises(LLMError),
        ):
            await provider.complete([{"role": "user", "content": "test"}])


class TestJsonHintInjection:
    """Responses backends (Codex) require the word "json" in the input
    whenever text.format type is json_object."""

    @pytest.mark.asyncio
    async def test_json_hint_prepended_when_missing(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        mock_data = _make_resp_text('{"ok": true}', _make_resp_usage(10, 10))
        mock_resp = _mock_httpx_response(mock_data, 200)
        mock_client = _mock_client(mock_resp)

        with patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client):
            await provider.complete(
                [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "Summarize this PR."},
                ],
                json_mode=True,
            )

        body = mock_client.post.call_args[1]["json"]
        assert body["text"]["format"]["type"] == "json_object"
        first = body["input"][0]
        assert first["role"] == "system"
        assert "json" in first["content"].lower()
        # Original system message and user message preserved after the hint.
        assert body["input"][1] == {"role": "system", "content": "You are a helpful assistant."}
        assert body["input"][2] == {"role": "user", "content": "Summarize this PR."}

    @pytest.mark.asyncio
    async def test_no_hint_when_json_already_present(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        mock_data = _make_resp_text("{}", _make_resp_usage(10, 10))
        mock_resp = _mock_httpx_response(mock_data, 200)
        mock_client = _mock_client(mock_resp)

        with patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client):
            await provider.complete(
                [
                    {"role": "system", "content": "Respond with a JSON object."},
                    {"role": "user", "content": "Summarize this PR."},
                ],
                json_mode=True,
            )

        body = mock_client.post.call_args[1]["json"]
        assert len(body["input"]) == 2
        assert body["input"][0] == {
            "role": "system",
            "content": "Respond with a JSON object.",
        }

    @pytest.mark.asyncio
    async def test_no_hint_when_json_word_in_user_content(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        mock_data = _make_resp_text("{}", _make_resp_usage(10, 10))
        mock_resp = _mock_httpx_response(mock_data, 200)
        mock_client = _mock_client(mock_resp)

        with patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client):
            await provider.complete(
                [{"role": "user", "content": "Return the diff as JSON."}],
                json_mode=True,
            )

        body = mock_client.post.call_args[1]["json"]
        assert len(body["input"]) == 1
        assert body["input"][0]["content"] == "Return the diff as JSON."

    @pytest.mark.asyncio
    async def test_no_hint_when_json_in_tool_result(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        mock_data = _make_resp_text("{}", _make_resp_usage(10, 10))
        mock_resp = _mock_httpx_response(mock_data, 200)
        mock_client = _mock_client(mock_resp)

        convo = [
            {"role": "user", "content": "Run the check."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "config.json"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": '{"status": "ok"}'},
        ]
        with patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client):
            await provider.complete(convo, json_mode=True)

        body = mock_client.post.call_args[1]["json"]
        # "json" appears in the function_call arguments — no hint needed.
        assert body["input"][0] == {"role": "user", "content": "Run the check."}

    @pytest.mark.asyncio
    async def test_no_hint_when_json_mode_disabled(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        mock_data = _make_resp_text("plain text", _make_resp_usage(10, 10))
        mock_resp = _mock_httpx_response(mock_data, 200)
        mock_client = _mock_client(mock_resp)

        with patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client):
            await provider.complete([{"role": "user", "content": "say hello"}], json_mode=False)

        body = mock_client.post.call_args[1]["json"]
        assert "text" not in body
        assert len(body["input"]) == 1
        assert body["input"][0]["content"] == "say hello"


class TestJsonHintHelper:
    def test_returns_same_list_when_word_present(self):
        items = [{"role": "system", "content": "Respond in JSON."}]
        assert _ensure_json_word(items) is items

    def test_case_insensitive_match(self):
        items = [{"role": "user", "content": "give me the Json blob"}]
        assert _ensure_json_word(items) is items

    def test_prepends_hint_when_absent(self):
        items = [{"role": "user", "content": "hi"}]
        out = _ensure_json_word(items)
        assert len(out) == 2
        assert out[0]["role"] == "system"
        assert "json" in out[0]["content"].lower()
        assert out[1] == {"role": "user", "content": "hi"}

    def test_scans_non_role_values(self):
        # "json" inside function_call arguments must count.
        items = [
            {
                "type": "function_call",
                "id": "c1",
                "call_id": "c1",
                "name": "read_file",
                "arguments": '{"path": "json_schema.py"}',
            }
        ]
        assert _ensure_json_word(items) is items


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
        mock_data = _make_resp_tool("submit_review", "{}")
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
            await provider.complete_with_tools([{"role": "user", "content": "review"}], tools=tools)

        body = mock_client.post.call_args[1]["json"]
        assert body["tool_choice"] == {
            "type": "function",
            "name": "submit_review",
        }

    @pytest.mark.asyncio
    async def test_tool_choice_400_fallback(self, config: LLMConfig):
        provider = ResponsesProvider(config)

        err_resp = _mock_httpx_response({"error": "tool_choice not supported"}, 400)
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
        mock_data = _make_resp_text_and_tool("let me check", "submit_review", '{"comments":[]}')
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

        func_items = [i for i in input_items if i.get("type") == "function_call"]
        assert len(func_items) == 1
        func = func_items[0]
        assert func["name"] == "read_file"
        assert func["call_id"] == "call_1"

        output_items = [i for i in input_items if i.get("type") == "function_call_output"]
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

        provider = create_llm(LLMConfig(provider="bedrock", api_style="responses"))
        assert isinstance(provider, BedrockProvider)


class TestReviewAndWalkthrough:
    @pytest.mark.asyncio
    async def test_review_calls_complete_with_tools(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        mock_data = _make_resp_tool("submit_review", '{"comments":[]}')
        mock_resp = _mock_httpx_response(mock_data, 200)
        mock_client = _mock_client(mock_resp)

        with patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client):
            result = await provider.review([{"role": "user", "content": "review"}])

        assert result == '{"comments":[]}'

    @pytest.mark.asyncio
    async def test_walkthrough_calls_complete_with_tools(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        mock_data = _make_resp_tool("submit_walkthrough", '{"summary":"x","file_changes":[]}')
        mock_resp = _mock_httpx_response(mock_data, 200)
        mock_client = _mock_client(mock_resp)

        with patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client):
            result = await provider.walkthrough([{"role": "user", "content": "wt"}])

        assert result == '{"summary":"x","file_changes":[]}'


class TestCountTokens:
    def test_estimates_tokens(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        assert provider.count_tokens("hello world") == 2  # 11 chars // 4
        assert provider.count_tokens("") == 0
        assert provider.count_tokens("a" * 100) == 25


class TestUsage:
    def test_usage_before_calls(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        usage = provider.usage
        assert usage == {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

    @pytest.mark.asyncio
    async def test_usage_accumulates(self, config: LLMConfig):
        provider = ResponsesProvider(config)
        mock_data = _make_resp_text("result", _make_resp_usage(100, 50))
        mock_resp = _mock_httpx_response(mock_data, 200)
        mock_client = _mock_client(mock_resp)

        with patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client):
            await provider.complete([{"role": "user", "content": "test"}])

        usage = provider.usage
        assert usage["prompt_tokens"] == 100
        assert usage["completion_tokens"] == 50
        assert usage["total_tokens"] == 150


class TestApplyReasoning:
    @pytest.mark.asyncio
    async def test_reasoning_rejection_records_fallback(self, config: LLMConfig):
        """When a model rejects reasoning effort, ResponsesProvider records
        it so subsequent calls skip reasoning for that model."""
        config.reasoning_effort = "high"
        provider = ResponsesProvider(config)

        err_resp = _mock_httpx_response({"error": "reasoning not supported"}, 400)
        ok_data = _make_resp_text("done", _make_resp_usage(10, 10))
        ok_resp = _mock_httpx_response(ok_data, 200)

        mock_client = _mock_client(None, extra_posts=[err_resp, ok_resp])

        tools = [
            {
                "type": "function",
                "function": {"name": "do_thing", "parameters": {"type": "object"}},
            }
        ]
        with patch("mira.llm.responses.httpx.AsyncClient", return_value=mock_client):
            await provider.complete_agentic([{"role": "user", "content": "test"}], tools=tools)

        # Model should be recorded in _no_reasoning after rejection
        assert "gpt-4o" in provider._no_reasoning
        assert mock_client.post.call_count == 2

    @pytest.mark.asyncio
    async def test_apply_reasoning_adds_reasoning_field(self, config: LLMConfig):
        """_apply_reasoning adds a reasoning field to the body when effort is set."""
        config.reasoning_effort = "high"
        provider = ResponsesProvider(config)
        body = {"model": "gpt-4o"}
        provider._apply_reasoning(body)
        assert "reasoning" in body
        assert body["reasoning"] == {"effort": "high"}

    @pytest.mark.asyncio
    async def test_apply_reasoning_xhigh(self, config: LLMConfig):
        """xhigh passes through verbatim on non-OpenRouter OpenAI-compatible hosts."""
        config.reasoning_effort = "xhigh"
        provider = ResponsesProvider(config)
        body = {"model": "gpt-4o"}
        provider._apply_reasoning(body)
        assert body["reasoning"] == {"effort": "xhigh"}

    @pytest.mark.asyncio
    async def test_apply_reasoning_skips_when_off(self, config: LLMConfig):
        """_apply_reasoning is a no-op when reasoning effort is off or None."""
        provider = ResponsesProvider(config)
        body = {"model": "gpt-4o", "temperature": 0.1}
        provider._apply_reasoning(body)
        assert "reasoning" not in body
        # Temperature should NOT be popped when reasoning is off
        assert "temperature" in body


class TestResponseMessage:
    def test_tool_calls_only_yields_none_content(self):
        from mira.llm.responses import _response_message

        data = {
            "output": [
                {
                    "type": "function_call",
                    "id": "call_1",
                    "call_id": "call_1",
                    "name": "submit_review",
                    "arguments": "{}",
                }
            ]
        }
        result = _response_message(data)
        assert result["role"] == "assistant"
        assert result["content"] is None
        assert len(result["tool_calls"]) == 1


class TestOutputText:
    def test_fallback_to_output_text(self):
        from mira.llm.responses import _output_text

        # When no message item exists, fallback to top-level output_text
        data = {"output_text": "fallback text"}
        assert _output_text(data) == "fallback text"

    def test_empty_output_returns_empty_string(self):
        from mira.llm.responses import _output_text

        data = {"output": []}
        assert _output_text(data) == ""
