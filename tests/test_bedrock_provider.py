"""Tests for the Bedrock LLM provider."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from mira.config import LLMConfig
from mira.exceptions import LLMError

boto3 = pytest.importorskip(
    "boto3", reason="boto3 not installed (install with: pip install mira-reviewer[bedrock])"
)


def _bedrock_config(**kwargs) -> LLMConfig:
    return LLMConfig(
        provider="bedrock",
        model="us.anthropic.claude-sonnet-4-6-v1:0",
        region="us-east-1",
        **kwargs,
    )


def _mock_converse_response(text: str = "response", usage: dict | None = None) -> dict:
    """Mock Bedrock Converse API response with text content."""
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "usage": usage or {"inputTokens": 100, "outputTokens": 50},
        "stopReason": "end_turn",
    }


def _mock_tool_use_response(
    name: str = "submit_review", input_data: dict | None = None, usage: dict | None = None
) -> dict:
    """Mock Bedrock Converse API response with a tool use block."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": "call_abc123",
                            "name": name,
                            "input": input_data or {"comments": [], "summary": "LGTM"},
                        }
                    }
                ],
            }
        },
        "usage": usage or {"inputTokens": 200, "outputTokens": 100},
        "stopReason": "tool_use",
    }


class TestBedrockProviderInit:
    @patch("boto3.Session")
    def test_creates_client_with_region(self, mock_session_cls):
        from mira.llm.bedrock import BedrockProvider

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        BedrockProvider(_bedrock_config())

        mock_session_cls.assert_called_once_with(profile_name=None, region_name="us-east-1")
        mock_session.client.assert_called_once_with("bedrock-runtime")

    @patch("boto3.Session")
    def test_creates_client_with_profile(self, mock_session_cls):
        from mira.llm.bedrock import BedrockProvider

        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        BedrockProvider(_bedrock_config(aws_profile="my-profile"))

        mock_session_cls.assert_called_once_with(profile_name="my-profile", region_name="us-east-1")

    @patch("boto3.Session")
    def test_initial_token_counts(self, mock_session_cls):
        from mira.llm.bedrock import BedrockProvider

        mock_session_cls.return_value = MagicMock()
        provider = BedrockProvider(_bedrock_config())
        assert provider.total_prompt_tokens == 0
        assert provider.total_completion_tokens == 0

    def test_missing_boto3_raises(self):
        with (
            patch.dict("sys.modules", {"boto3": None}),
            pytest.raises(LLMError, match="boto3 is required"),
        ):
            import importlib

            from mira.llm import bedrock

            importlib.reload(bedrock)
            bedrock.BedrockProvider(_bedrock_config())


class TestBedrockComplete:
    @pytest.mark.asyncio
    @patch("boto3.Session")
    async def test_successful_completion_json_mode(self, mock_session_cls):
        from mira.llm.bedrock import BedrockProvider

        mock_client = MagicMock()
        # json_mode uses tool calling, so response is a tool use
        mock_client.converse.return_value = _mock_tool_use_response(
            "submit_json_response", {"result": "ok"}
        )
        mock_session = MagicMock()
        mock_session.client.return_value = mock_client
        mock_session_cls.return_value = mock_session

        provider = BedrockProvider(_bedrock_config())
        result = await provider.complete([{"role": "user", "content": "hello"}])

        parsed = json.loads(result)
        assert parsed["result"] == "ok"
        assert provider.total_prompt_tokens == 200
        assert provider.total_completion_tokens == 100

    @pytest.mark.asyncio
    @patch("boto3.Session")
    async def test_completion_no_json_mode(self, mock_session_cls):
        from mira.llm.bedrock import BedrockProvider

        mock_client = MagicMock()
        mock_client.converse.return_value = _mock_converse_response("plain text response")
        mock_session = MagicMock()
        mock_session.client.return_value = mock_client
        mock_session_cls.return_value = mock_session

        provider = BedrockProvider(_bedrock_config())
        result = await provider.complete([{"role": "user", "content": "hello"}], json_mode=False)

        assert result == "plain text response"
        # Should NOT have toolConfig in the request
        call_kwargs = mock_client.converse.call_args[1]
        assert "toolConfig" not in call_kwargs

    @pytest.mark.asyncio
    @patch("boto3.Session")
    async def test_reasoning_effort_enables_thinking(self, mock_session_cls):
        from mira.llm.bedrock import BedrockProvider

        mock_client = MagicMock()
        mock_client.converse.return_value = _mock_converse_response("ok")
        mock_session = MagicMock()
        mock_session.client.return_value = mock_client
        mock_session_cls.return_value = mock_session

        # High max_tokens so the budget reflects the effort level, not the
        # `max_out - 1024` cap.
        provider = BedrockProvider(_bedrock_config(reasoning_effort="medium", max_tokens=40000))
        await provider.complete([{"role": "user", "content": "hi"}], json_mode=False)

        call_kwargs = mock_client.converse.call_args[1]
        # Bedrock Claude expects the `thinking` field with an explicit budget —
        # not `reasoning_config`, which silently disables extended thinking.
        thinking = call_kwargs["additionalModelRequestFields"]["thinking"]
        assert thinking == {"type": "enabled", "budget_tokens": 8192}
        # Thinking requires temperature unset.
        assert "temperature" not in call_kwargs["inferenceConfig"]

    @pytest.mark.asyncio
    @patch("boto3.Session")
    async def test_max_effort_uses_largest_budget(self, mock_session_cls):
        from mira.llm.bedrock import BedrockProvider

        mock_client = MagicMock()
        mock_client.converse.return_value = _mock_converse_response("ok")
        mock_session = MagicMock()
        mock_session.client.return_value = mock_client
        mock_session_cls.return_value = mock_session

        provider = BedrockProvider(_bedrock_config(reasoning_effort="max", max_tokens=40000))
        await provider.complete([{"role": "user", "content": "hi"}], json_mode=False)

        thinking = mock_client.converse.call_args[1]["additionalModelRequestFields"]["thinking"]
        assert thinking == {"type": "enabled", "budget_tokens": 32768}

    @pytest.mark.asyncio
    @patch("boto3.Session")
    async def test_reasoning_off_leaves_request_unchanged(self, mock_session_cls):
        from mira.llm.bedrock import BedrockProvider

        mock_client = MagicMock()
        mock_client.converse.return_value = _mock_converse_response("ok")
        mock_session = MagicMock()
        mock_session.client.return_value = mock_client
        mock_session_cls.return_value = mock_session

        provider = BedrockProvider(_bedrock_config(reasoning_effort="off"))
        await provider.complete([{"role": "user", "content": "hi"}], json_mode=False)

        call_kwargs = mock_client.converse.call_args[1]
        assert "additionalModelRequestFields" not in call_kwargs
        assert "temperature" in call_kwargs["inferenceConfig"]

    @pytest.mark.asyncio
    @patch("boto3.Session")
    async def test_json_mode_uses_tool_forcing(self, mock_session_cls):
        from mira.llm.bedrock import BedrockProvider

        mock_client = MagicMock()
        mock_client.converse.return_value = _mock_tool_use_response("submit_json_response", {})
        mock_session = MagicMock()
        mock_session.client.return_value = mock_client
        mock_session_cls.return_value = mock_session

        provider = BedrockProvider(_bedrock_config())
        await provider.complete([{"role": "user", "content": "hello"}], json_mode=True)

        call_kwargs = mock_client.converse.call_args[1]
        tool_config = call_kwargs["toolConfig"]
        assert tool_config["toolChoice"] == {"tool": {"name": "submit_json_response"}}

    @pytest.mark.asyncio
    @patch("boto3.Session")
    async def test_fallback_on_primary_failure(self, mock_session_cls):
        from mira.llm.bedrock import BedrockProvider

        mock_client = MagicMock()
        # Primary fails with non-throttle error (exhausts retries), fallback succeeds
        mock_client.converse.side_effect = [
            Exception("model overloaded"),
            _mock_converse_response("fallback response"),
        ]
        mock_session = MagicMock()
        mock_session.client.return_value = mock_client
        mock_session_cls.return_value = mock_session

        config = _bedrock_config(fallback_model="us.anthropic.claude-haiku-4-5-v1:0")
        provider = BedrockProvider(config)
        result = await provider.complete([{"role": "user", "content": "hello"}], json_mode=False)

        assert result == "fallback response"

    @pytest.mark.asyncio
    @patch("boto3.Session")
    async def test_no_fallback_raises(self, mock_session_cls):
        from mira.llm.bedrock import BedrockProvider

        mock_client = MagicMock()
        mock_client.converse.side_effect = Exception("broken")
        mock_session = MagicMock()
        mock_session.client.return_value = mock_client
        mock_session_cls.return_value = mock_session

        provider = BedrockProvider(_bedrock_config())
        with pytest.raises(LLMError, match="Bedrock call failed"):
            await provider.complete([{"role": "user", "content": "hello"}], json_mode=False)


class TestBedrockErrorHandling:
    @pytest.mark.asyncio
    @patch("boto3.Session")
    async def test_access_denied_error(self, mock_session_cls):
        from mira.llm.bedrock import BedrockProvider

        mock_client = MagicMock()
        err = Exception("AccessDeniedException: User is not authorized")
        mock_client.converse.side_effect = err
        mock_session = MagicMock()
        mock_session.client.return_value = mock_client
        mock_session_cls.return_value = mock_session

        provider = BedrockProvider(_bedrock_config())
        with pytest.raises(LLMError, match="access denied"):
            await provider.complete([{"role": "user", "content": "hello"}], json_mode=False)

    @pytest.mark.asyncio
    @patch("boto3.Session")
    async def test_resource_not_found_error(self, mock_session_cls):
        from mira.llm.bedrock import BedrockProvider

        mock_client = MagicMock()
        err = Exception("ResourceNotFoundException: Model not found")
        mock_client.converse.side_effect = err
        mock_session = MagicMock()
        mock_session.client.return_value = mock_client
        mock_session_cls.return_value = mock_session

        provider = BedrockProvider(_bedrock_config())
        with pytest.raises(LLMError, match="not found"):
            await provider.complete([{"role": "user", "content": "hello"}], json_mode=False)

    @pytest.mark.asyncio
    @patch("boto3.Session")
    async def test_throttling_retries_config_count(self, mock_session_cls):
        from mira.llm.bedrock import BedrockProvider

        mock_client = MagicMock()
        err = Exception("ThrottlingException: rate exceeded")
        mock_client.converse.side_effect = err
        mock_session = MagicMock()
        mock_session.client.return_value = mock_client
        mock_session_cls.return_value = mock_session

        provider = BedrockProvider(
            _bedrock_config(max_retries=5, retry_min_wait=0, retry_max_wait=0)
        )
        with pytest.raises(LLMError):
            await provider.complete([{"role": "user", "content": "hello"}], json_mode=False)

        assert mock_client.converse.call_count == 5


class TestBedrockCompleteWithTools:
    @pytest.mark.asyncio
    @patch("boto3.Session")
    async def test_review_tool_calling(self, mock_session_cls):
        from mira.llm.bedrock import BedrockProvider

        review_data = {"comments": [], "summary": "Clean code", "key_issues": []}
        mock_client = MagicMock()
        mock_client.converse.return_value = _mock_tool_use_response("submit_review", review_data)
        mock_session = MagicMock()
        mock_session.client.return_value = mock_client
        mock_session_cls.return_value = mock_session

        provider = BedrockProvider(_bedrock_config())
        result = await provider.review([{"role": "user", "content": "review this"}])

        parsed = json.loads(result)
        assert parsed["summary"] == "Clean code"

    @pytest.mark.asyncio
    @patch("boto3.Session")
    async def test_tool_config_format(self, mock_session_cls):
        from mira.llm.bedrock import BedrockProvider

        mock_client = MagicMock()
        mock_client.converse.return_value = _mock_tool_use_response()
        mock_session = MagicMock()
        mock_session.client.return_value = mock_client
        mock_session_cls.return_value = mock_session

        provider = BedrockProvider(_bedrock_config())
        await provider.review([{"role": "user", "content": "review"}])

        call_kwargs = mock_client.converse.call_args[1]
        tool_config = call_kwargs["toolConfig"]
        assert "tools" in tool_config
        tool_spec = tool_config["tools"][0]["toolSpec"]
        assert tool_spec["name"] == "submit_review"
        assert "inputSchema" in tool_spec
        assert tool_config["toolChoice"] == {"tool": {"name": "submit_review"}}

    @pytest.mark.asyncio
    @patch("boto3.Session")
    async def test_text_fallback_when_no_tool_call(self, mock_session_cls):
        from mira.llm.bedrock import BedrockProvider

        # Model returns text instead of tool call
        mock_client = MagicMock()
        mock_client.converse.return_value = _mock_converse_response('{"comments": []}')
        mock_session = MagicMock()
        mock_session.client.return_value = mock_client
        mock_session_cls.return_value = mock_session

        provider = BedrockProvider(_bedrock_config())
        result = await provider.complete_with_tools(
            [{"role": "user", "content": "review"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "test",
                        "description": "",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        )

        assert result == '{"comments": []}'


class TestBedrockAgentic:
    @pytest.mark.asyncio
    @patch("boto3.Session")
    async def test_returns_openai_shaped_message(self, mock_session_cls):
        from mira.llm.bedrock import BedrockProvider

        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {"text": "Let me check that."},
                        {
                            "toolUse": {
                                "toolUseId": "call_xyz",
                                "name": "read_file",
                                "input": {"path": "src/main.py"},
                            }
                        },
                    ],
                }
            },
            "usage": {"inputTokens": 50, "outputTokens": 30},
            "stopReason": "tool_use",
        }
        mock_session = MagicMock()
        mock_session.client.return_value = mock_client
        mock_session_cls.return_value = mock_session

        provider = BedrockProvider(_bedrock_config())
        msg = await provider.complete_agentic(
            [{"role": "user", "content": "check the file"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a file",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                        },
                    },
                }
            ],
        )

        assert msg["role"] == "assistant"
        assert msg["content"] == "Let me check that."
        assert len(msg["tool_calls"]) == 1
        assert msg["tool_calls"][0]["function"]["name"] == "read_file"
        assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"path": "src/main.py"}

    @pytest.mark.asyncio
    @patch("boto3.Session")
    async def test_no_tool_calls_omits_key(self, mock_session_cls):
        from mira.llm.bedrock import BedrockProvider

        mock_client = MagicMock()
        mock_client.converse.return_value = _mock_converse_response("Done, no tools needed.")
        mock_session = MagicMock()
        mock_session.client.return_value = mock_client
        mock_session_cls.return_value = mock_session

        provider = BedrockProvider(_bedrock_config())
        msg = await provider.complete_agentic(
            [{"role": "user", "content": "summarize"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "noop",
                        "description": "",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        )

        assert msg["role"] == "assistant"
        assert msg["content"] == "Done, no tools needed."
        assert "tool_calls" not in msg


class TestCreateProvider:
    @patch("boto3.Session")
    def test_factory_returns_bedrock(self, mock_session_cls):
        from mira.llm import create_llm
        from mira.llm.bedrock import BedrockProvider

        mock_session_cls.return_value = MagicMock()
        provider = create_llm(_bedrock_config())
        assert isinstance(provider, BedrockProvider)

    def test_factory_returns_openai_by_default(self):
        from mira.llm import create_llm
        from mira.llm.provider import LLMProvider

        config = LLMConfig()
        provider = create_llm(config)
        assert isinstance(provider, LLMProvider)


class TestMessageConversion:
    def test_system_messages_extracted(self):
        from mira.llm.bedrock import _messages_to_bedrock

        messages = [
            {"role": "system", "content": "You are a reviewer."},
            {"role": "user", "content": "Review this code."},
        ]
        system, conversation = _messages_to_bedrock(messages)

        assert system == [{"text": "You are a reviewer."}]
        assert len(conversation) == 1
        assert conversation[0]["role"] == "user"

    def test_tool_results_converted(self):
        from mira.llm.bedrock import _messages_to_bedrock

        messages = [
            {"role": "user", "content": "check file"},
            {"role": "assistant", "content": "reading..."},
            {"role": "tool", "content": '{"data": "file contents"}', "tool_call_id": "call_1"},
        ]
        system, conversation = _messages_to_bedrock(messages)

        assert system is None
        assert len(conversation) == 3
        tool_result = conversation[2]["content"][0]["toolResult"]
        assert tool_result["toolUseId"] == "call_1"
        assert tool_result["content"] == [{"json": {"data": "file contents"}}]

    def test_tool_schema_conversion(self):
        from mira.llm.bedrock import _openai_tool_to_bedrock

        openai_tool = {
            "type": "function",
            "function": {
                "name": "test_tool",
                "description": "A test tool",
                "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
            },
        }
        bedrock_tool = _openai_tool_to_bedrock(openai_tool)

        assert bedrock_tool["toolSpec"]["name"] == "test_tool"
        assert bedrock_tool["toolSpec"]["description"] == "A test tool"
        assert (
            bedrock_tool["toolSpec"]["inputSchema"]["json"] == openai_tool["function"]["parameters"]
        )


class TestCapabilityAnnotations:
    @patch("boto3.Session")
    def test_bedrock_capabilities(self, mock_session_cls):
        from mira.llm.bedrock import BedrockProvider

        mock_session_cls.return_value = MagicMock()
        provider = BedrockProvider(_bedrock_config())
        assert provider.supports_json_mode is False
        assert provider.supports_tool_calling is True

    def test_openai_capabilities(self):
        from mira.llm.provider import LLMProvider

        provider = LLMProvider(LLMConfig())
        assert provider.supports_json_mode is True
        assert provider.supports_tool_calling is True


class TestUsage:
    @pytest.mark.asyncio
    @patch("boto3.Session")
    async def test_token_tracking(self, mock_session_cls):
        from mira.llm.bedrock import BedrockProvider

        mock_client = MagicMock()
        mock_client.converse.side_effect = [
            _mock_converse_response("r1", {"inputTokens": 100, "outputTokens": 50}),
            _mock_converse_response("r2", {"inputTokens": 200, "outputTokens": 80}),
        ]
        mock_session = MagicMock()
        mock_session.client.return_value = mock_client
        mock_session_cls.return_value = mock_session

        provider = BedrockProvider(_bedrock_config())
        await provider.complete([{"role": "user", "content": "a"}], json_mode=False)
        await provider.complete([{"role": "user", "content": "b"}], json_mode=False)

        assert provider.usage == {
            "prompt_tokens": 300,
            "completion_tokens": 130,
            "total_tokens": 430,
        }
