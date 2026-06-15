"""AWS Bedrock provider using the Converse API."""

from __future__ import annotations

import json
import logging
from typing import ClassVar

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from mira.config import LLMConfig
from mira.exceptions import LLMError
from mira.llm.provider import SUBMIT_REVIEW_TOOL, SUBMIT_WALKTHROUGH_TOOL

logger = logging.getLogger(__name__)

# Generic tool used to force structured JSON output from models that lack
# native json_mode. The model "calls" this tool with arbitrary JSON as input.
_JSON_RESPONSE_TOOL = {
    "toolSpec": {
        "name": "submit_json_response",
        "description": "Return your response as structured JSON.",
        "inputSchema": {"json": {"type": "object", "additionalProperties": True}},
    }
}


class _BedrockThrottlingError(Exception):
    """Wrapper for Bedrock throttling/availability errors to target retries."""


def _openai_tool_to_bedrock(tool: dict) -> dict:
    """Convert OpenAI function-calling tool schema to Bedrock toolSpec format."""
    func = tool["function"]
    return {
        "toolSpec": {
            "name": func["name"],
            "description": func.get("description", ""),
            "inputSchema": {"json": func["parameters"]},
        }
    }


def _messages_to_bedrock(messages: list[dict]) -> tuple[list[dict] | None, list[dict]]:
    """Convert OpenAI-style messages to Bedrock Converse format.

    Returns (system_prompts, conversation_messages).
    """
    system: list[dict] = []
    conversation: list[dict] = []

    for msg in messages:
        role = msg["role"]
        content = msg.get("content", "")

        if role == "system":
            system.append({"text": content})
        elif role == "user":
            conversation.append({"role": "user", "content": [{"text": content}]})
        elif role == "assistant":
            # Reconstruct assistant messages — may have tool_calls from agentic loop
            blocks: list[dict] = []
            if content:
                blocks.append({"text": content})
            for tc in msg.get("tool_calls", []):
                blocks.append(
                    {
                        "toolUse": {
                            "toolUseId": tc["id"],
                            "name": tc["function"]["name"],
                            "input": json.loads(tc["function"]["arguments"]),
                        }
                    }
                )
            if blocks:
                conversation.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            # Tool results go back as user messages in Bedrock
            tool_use_id = msg.get("tool_call_id", "call_0")
            try:
                result_content = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                result_content = {"result": content}
            conversation.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": tool_use_id,
                                "content": [{"json": result_content}],
                            }
                        }
                    ],
                }
            )

    return system or None, conversation


class BedrockProvider:
    """AWS Bedrock LLM provider using the Converse API.

    Auth uses the standard AWS credential chain (env vars, instance profile,
    ECS task role, SSO). Optionally accepts an explicit profile name.
    """

    supports_json_mode: ClassVar[bool] = False
    supports_tool_calling: ClassVar[bool] = True

    def __init__(self, config: LLMConfig) -> None:
        try:
            import boto3
        except ImportError as e:
            raise LLMError(
                "boto3 is required for the Bedrock provider. "
                "Install with: pip install mira-reviewer[bedrock]"
            ) from e

        self.config = config
        session = boto3.Session(
            profile_name=config.aws_profile,
            region_name=config.region,
        )
        self._client = session.client("bedrock-runtime")
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        logger.info(
            "Bedrock provider initialized: region=%s, model=%s",
            config.region,
            config.model,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=2, max=30, jitter=2),
        retry=retry_if_exception_type(_BedrockThrottlingError),
        reraise=True,
    )
    async def _call_converse(
        self,
        model: str,
        messages: list[dict],
        *,
        tool_config: dict | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        """Call Bedrock Converse API via thread executor."""
        import asyncio

        system, conversation = _messages_to_bedrock(messages)

        max_out = max_tokens if max_tokens is not None else self.config.max_tokens
        kwargs: dict = {
            "modelId": model,
            "messages": conversation,
            "inferenceConfig": {
                "temperature": temperature if temperature is not None else self.config.temperature,
                "maxTokens": max_out,
            },
        }
        # Extended thinking: map effort → a token budget (Bedrock Claude expects
        # an explicit budget, not an effort level). Thinking requires temperature
        # unset, so drop it when enabled. Budget must stay below maxTokens.
        effort = self.config.reasoning_effort
        if effort and effort != "off":
            budget = {"low": 2048, "medium": 8192, "high": 16384, "max": 32768}.get(effort, 8192)
            budget = min(budget, max(1024, max_out - 1024))
            kwargs["additionalModelRequestFields"] = {
                "thinking": {"type": "enabled", "budget_tokens": budget}
            }
            kwargs["inferenceConfig"].pop("temperature", None)
        if system:
            kwargs["system"] = system
        if tool_config:
            kwargs["toolConfig"] = tool_config

        logger.debug("Bedrock request: model=%s, messages=%d", model, len(conversation))

        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(None, lambda: self._client.converse(**kwargs))
        except Exception as e:
            self._handle_api_error(e, model)
            raise  # unreachable, but satisfies type checkers

        # Track usage
        usage = response.get("usage", {})
        input_tokens = usage.get("inputTokens", 0)
        output_tokens = usage.get("outputTokens", 0)
        self.total_prompt_tokens += input_tokens
        self.total_completion_tokens += output_tokens
        logger.info(
            "Bedrock response: model=%s, input_tokens=%d, output_tokens=%d, stop=%s",
            model,
            input_tokens,
            output_tokens,
            response.get("stopReason"),
        )

        return response

    def _handle_api_error(self, error: Exception, model: str) -> None:
        """Classify Bedrock errors and raise appropriate exceptions."""
        error_name = type(error).__name__
        error_msg = str(error)

        # Throttling — retryable
        if error_name in ("ThrottlingException", "ServiceUnavailableException"):
            logger.warning("Bedrock throttled: model=%s, error=%s", model, error_msg)
            raise _BedrockThrottlingError(error_msg) from error

        if "ThrottlingException" in error_msg or "Too many requests" in error_msg:
            logger.warning("Bedrock throttled: model=%s, error=%s", model, error_msg)
            raise _BedrockThrottlingError(error_msg) from error

        # Access denied — not retryable, clear message
        if "AccessDeniedException" in error_msg:
            raise LLMError(
                f"Bedrock access denied for model {model}. "
                "Ensure your IAM role/user has bedrock:InvokeModel permission "
                "and the model is enabled in your account."
            ) from error

        # Model not found
        if "ResourceNotFoundException" in error_msg:
            raise LLMError(
                f"Bedrock model not found: {model}. "
                "Check the model ID and ensure it's available in your region."
            ) from error

        # Validation error (bad request shape)
        if "ValidationException" in error_msg:
            raise LLMError(f"Bedrock validation error for {model}: {error_msg}") from error

        # Catch-all
        raise LLMError(f"Bedrock API error for {model}: {error_msg}") from error

    def _extract_text(self, response: dict) -> str:
        """Extract text content from a Bedrock Converse response."""
        output = response.get("output", {})
        message = output.get("message", {})
        for block in message.get("content", []):
            if "text" in block:
                return block["text"]
        return ""

    def _extract_tool_use(self, response: dict) -> str | None:
        """Extract tool use input JSON from a Bedrock Converse response."""
        output = response.get("output", {})
        message = output.get("message", {})
        for block in message.get("content", []):
            if "toolUse" in block:
                return json.dumps(block["toolUse"]["input"])
        return None

    async def _call_with_fallback(
        self,
        messages: list[dict],
        *,
        tool_config: dict | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        """Call primary model with fallback on failure."""
        try:
            return await self._call_converse(
                self.config.model,
                messages,
                tool_config=tool_config,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as primary_err:
            if self.config.fallback_model:
                logger.warning(
                    "Primary model %s failed (%s), trying fallback %s",
                    self.config.model,
                    primary_err,
                    self.config.fallback_model,
                )
                try:
                    return await self._call_converse(
                        self.config.fallback_model,
                        messages,
                        tool_config=tool_config,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                except Exception as fallback_err:
                    raise LLMError(
                        f"Both primary ({self.config.model}) and fallback "
                        f"({self.config.fallback_model}) models failed: {fallback_err}"
                    ) from fallback_err
            raise LLMError(
                f"Bedrock call failed with {self.config.model}: {primary_err}"
            ) from primary_err

    async def complete(
        self,
        messages: list[dict[str, str]],
        json_mode: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Complete a prompt. Uses tool calling to enforce JSON when json_mode=True."""
        if json_mode:
            # Force structured JSON output via tool use
            tool_config = {
                "tools": [_JSON_RESPONSE_TOOL],
                "toolChoice": {"tool": {"name": "submit_json_response"}},
            }
            response = await self._call_with_fallback(
                messages,
                tool_config=tool_config,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            result = self._extract_tool_use(response)
            if result:
                return result
            # Fallback to text if model didn't use the tool
            return self._extract_text(response)

        response = await self._call_with_fallback(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return self._extract_text(response)

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict],
        temperature: float | None = None,
    ) -> str:
        """Complete using tool calling for structured output."""
        bedrock_tools = [_openai_tool_to_bedrock(t) for t in tools]
        tool_config = {
            "tools": bedrock_tools,
            "toolChoice": {"tool": {"name": tools[0]["function"]["name"]}},
        }

        response = await self._call_with_fallback(
            messages,
            tool_config=tool_config,
            temperature=temperature,
        )

        tool_json = self._extract_tool_use(response)
        if tool_json:
            return tool_json

        # Fallback: some models may return text instead of tool call
        text = self._extract_text(response)
        if text:
            logger.warning("Bedrock model returned text instead of tool call, using as fallback")
            return text

        raise LLMError("Bedrock model returned neither tool call nor content")

    async def complete_agentic(
        self,
        messages: list,
        tools: list[dict],
        temperature: float | None = None,
    ) -> dict:
        """Single hop of an agentic loop. Returns OpenAI-shaped assistant message."""
        bedrock_tools = [_openai_tool_to_bedrock(t) for t in tools]
        tool_config = {"tools": bedrock_tools}

        response = await self._call_with_fallback(
            messages,
            tool_config=tool_config,
            temperature=temperature,
        )

        # Convert Bedrock response to OpenAI-compatible message format
        output = response.get("output", {})
        message = output.get("message", {})
        content_blocks = message.get("content", [])

        openai_msg: dict = {"role": "assistant", "content": None, "tool_calls": []}

        for block in content_blocks:
            if "text" in block:
                openai_msg["content"] = block["text"]
            elif "toolUse" in block:
                tool_use = block["toolUse"]
                openai_msg["tool_calls"].append(
                    {
                        "id": tool_use["toolUseId"],
                        "type": "function",
                        "function": {
                            "name": tool_use["name"],
                            "arguments": json.dumps(tool_use["input"]),
                        },
                    }
                )

        if not openai_msg["tool_calls"]:
            del openai_msg["tool_calls"]

        return openai_msg

    async def review(self, messages: list[dict[str, str]]) -> str:
        """Submit a review using tool calling."""
        return await self.complete_with_tools(messages, tools=[SUBMIT_REVIEW_TOOL])

    async def walkthrough(self, messages: list[dict[str, str]]) -> str:
        """Submit a walkthrough using tool calling."""
        return await self.complete_with_tools(messages, tools=[SUBMIT_WALKTHROUGH_TOOL])

    def count_tokens(self, text: str) -> int:
        """Estimate token count. Uses ~4 chars per token heuristic."""
        return len(text) // 4

    @property
    def usage(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
        }
