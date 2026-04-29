"""OpenRouter API provider with retry/fallback and tool calling support."""

from __future__ import annotations

import logging
import os

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from mira.config import LLMConfig
from mira.exceptions import LLMError

logger = logging.getLogger(__name__)

_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# ---------------------------------------------------------------------------
# Tool schemas for structured output via function/tool calling
# ---------------------------------------------------------------------------

SUBMIT_REVIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_review",
        "description": "Submit your code review findings including comments, key issues, and a summary.",
        "parameters": {
            "type": "object",
            "properties": {
                "comments": {
                    "type": "array",
                    "description": "List of review comments on specific lines of code.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Relative file path."},
                            "line": {
                                "type": "integer",
                                "description": "Line number in the target file.",
                            },
                            "end_line": {
                                "type": ["integer", "null"],
                                "description": "End line for multi-line comments, or null.",
                            },
                            "severity": {
                                "type": "string",
                                "enum": ["blocker", "warning", "suggestion", "nitpick"],
                            },
                            "category": {
                                "type": "string",
                                "enum": [
                                    "bug",
                                    "security",
                                    "performance",
                                    "error-handling",
                                    "race-condition",
                                    "resource-leak",
                                    "maintainability",
                                    "clarity",
                                    "configuration",
                                    "other",
                                ],
                            },
                            "title": {"type": "string", "description": "Short title (<80 chars)."},
                            "body": {
                                "type": "string",
                                "description": "Detailed explanation of the issue. Use single backticks for inline code references. Do NOT use triple-backtick code blocks.",
                            },
                            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                            "existing_code": {
                                "type": "string",
                                "description": "Verbatim copy of the code from the diff that this comment targets. Must be an exact substring.",
                            },
                            "suggestion": {
                                "type": ["string", "null"],
                                "description": "Optional replacement code to fix the issue. Raw code only — do NOT wrap in backticks or markdown fences.",
                            },
                            "agent_prompt": {
                                "type": ["string", "null"],
                                "description": "Concise imperative instruction for AI coding agents.",
                            },
                        },
                        "required": [
                            "path",
                            "line",
                            "severity",
                            "category",
                            "title",
                            "body",
                            "confidence",
                            "existing_code",
                        ],
                    },
                },
                "key_issues": {
                    "type": "array",
                    "description": "1-3 most critical findings a human reviewer MUST examine.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "issue": {"type": "string"},
                            "path": {"type": "string"},
                            "line": {"type": "integer"},
                        },
                        "required": ["issue", "path", "line"],
                    },
                },
                "summary": {
                    "type": "string",
                    "description": "Brief overall summary of the review.",
                },
                "metadata": {
                    "type": "object",
                    "properties": {
                        "reviewed_files": {"type": "integer"},
                        "skipped_reason": {"type": ["string", "null"]},
                    },
                },
            },
            "required": ["comments", "summary"],
        },
    },
}

SUBMIT_WALKTHROUGH_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_walkthrough",
        "description": "Submit a high-level walkthrough summary of the pull request.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Brief overall summary of the PR."},
                "confidence_score": {
                    "type": "object",
                    "properties": {
                        "score": {"type": "integer", "minimum": 1, "maximum": 5},
                        "label": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["score", "label", "reason"],
                },
                "change_groups": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "files": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "path": {"type": "string"},
                                        "change_type": {
                                            "type": "string",
                                            "enum": ["added", "modified", "deleted", "renamed"],
                                        },
                                        "description": {"type": "string"},
                                    },
                                    "required": ["path", "change_type", "description"],
                                },
                            },
                        },
                        "required": ["label", "files"],
                    },
                },
                "effort": {
                    "type": "object",
                    "properties": {
                        "level": {"type": "integer", "minimum": 1, "maximum": 5},
                        "label": {"type": "string"},
                        "minutes": {"type": "integer"},
                    },
                    "required": ["level", "label", "minutes"],
                },
                "sequence_diagram": {
                    "type": ["string", "null"],
                    "description": "Mermaid sequence diagram or null.",
                },
            },
            "required": ["summary", "change_groups"],
        },
    },
}


def _get_api_key() -> str:
    """Resolve the OpenRouter API key from environment."""
    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise LLMError(
            "No API key found. Set OPENROUTER_API_KEY or OPENAI_API_KEY environment variable."
        )
    return key


def _strip_model_prefix(model: str) -> str:
    """Strip 'openrouter/' prefix if present — OpenRouter API wants bare model IDs."""
    if model.startswith("openrouter/"):
        return model[len("openrouter/") :]
    return model


class LLMProvider:
    """Direct OpenRouter API client for LLM completions."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def _call_llm(
        self,
        model: str,
        messages: list[dict[str, str]],
        json_mode: bool,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Make a single LLM call with retries via OpenRouter."""
        body: dict = {
            "model": _strip_model_prefix(model),
            "messages": messages,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": max_tokens if max_tokens is not None else self.config.max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {_get_api_key()}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/miracodeai/mira",
            "X-Title": "Mira Code Reviewer",
        }

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{_OPENROUTER_BASE_URL}/chat/completions",
                headers=headers,
                json=body,
            )
            if resp.status_code != 200:
                raise LLMError(f"OpenRouter API error {resp.status_code}: {resp.text}")
            data = resp.json()

        content = data["choices"][0]["message"].get("content") or ""

        # Track usage
        usage = data.get("usage")
        if usage:
            self.total_prompt_tokens += usage.get("prompt_tokens", 0)
            self.total_completion_tokens += usage.get("completion_tokens", 0)

        return content

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def _call_llm_with_tools(
        self,
        model: str,
        messages: list[dict[str, str]],
        tools: list[dict],
        temperature: float | None = None,
    ) -> str:
        """Make an LLM call with tool/function calling and retries.

        The LLM returns structured data by 'calling' a tool. We extract the
        tool arguments as the JSON response.
        """
        body: dict = {
            "model": _strip_model_prefix(model),
            "messages": messages,
            "tools": tools,
            "tool_choice": {"type": "function", "function": {"name": tools[0]["function"]["name"]}},
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }

        headers = {
            "Authorization": f"Bearer {_get_api_key()}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/miracodeai/mira",
            "X-Title": "Mira Code Reviewer",
        }

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{_OPENROUTER_BASE_URL}/chat/completions",
                headers=headers,
                json=body,
            )
            if resp.status_code != 200:
                raise LLMError(f"OpenRouter API error {resp.status_code}: {resp.text}")
            data = resp.json()

        # Track usage
        usage = data.get("usage")
        if usage:
            self.total_prompt_tokens += usage.get("prompt_tokens", 0)
            self.total_completion_tokens += usage.get("completion_tokens", 0)

        # Extract tool call arguments
        message = data["choices"][0]["message"]
        tool_calls = message.get("tool_calls")

        if tool_calls and len(tool_calls) > 0:
            return tool_calls[0]["function"]["arguments"]

        # Fallback: if the model returned content instead of a tool call,
        # return the content as-is (some models may not support tool calling)
        content = message.get("content") or ""
        if content:
            logger.warning("Model returned content instead of tool call, using content as fallback")
            return content

        raise LLMError("Model returned neither tool call nor content")

    async def complete(
        self,
        messages: list[dict[str, str]],
        json_mode: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Complete a prompt using JSON mode, with fallback model support.

        Args:
            temperature: Override the default temperature for this call.
                         Use ``0.0`` for deterministic tasks like verification.
            max_tokens: Override the default output token cap for this call.
                        Indexing summarization needs ~16k to avoid truncation
                        on large batches; the default 4096 cuts JSON off.
        """
        try:
            return await self._call_llm(
                self.config.model, messages, json_mode,
                temperature=temperature, max_tokens=max_tokens,
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
                    return await self._call_llm(
                        self.config.fallback_model, messages, json_mode,
                        temperature=temperature, max_tokens=max_tokens,
                    )
                except Exception as fallback_err:
                    raise LLMError(
                        f"Both primary ({self.config.model}) and fallback "
                        f"({self.config.fallback_model}) models failed: {fallback_err}"
                    ) from fallback_err
            raise LLMError(
                f"LLM completion failed with {self.config.model}: {primary_err}"
            ) from primary_err

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict],
        temperature: float | None = None,
    ) -> str:
        """Complete a prompt using tool calling for structured output.

        The LLM 'calls' a tool to return structured JSON data. Works reliably
        across all models available on OpenRouter.

        Args:
            messages: The prompt messages.
            tools: Tool schemas in OpenAI function-calling format.
            temperature: Override the default temperature.

        Returns:
            The JSON string from the tool call arguments.
        """
        try:
            return await self._call_llm_with_tools(
                self.config.model, messages, tools, temperature=temperature
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
                    return await self._call_llm_with_tools(
                        self.config.fallback_model, messages, tools, temperature=temperature
                    )
                except Exception as fallback_err:
                    raise LLMError(
                        f"Both primary ({self.config.model}) and fallback "
                        f"({self.config.fallback_model}) models failed: {fallback_err}"
                    ) from fallback_err
            raise LLMError(
                f"LLM tool-call failed with {self.config.model}: {primary_err}"
            ) from primary_err

    async def review(self, messages: list[dict[str, str]]) -> str:
        """Submit a review using tool calling.

        Returns the JSON string containing review comments, key issues, and summary.
        """
        return await self.complete_with_tools(messages, tools=[SUBMIT_REVIEW_TOOL])

    async def walkthrough(self, messages: list[dict[str, str]]) -> str:
        """Submit a walkthrough using tool calling.

        Returns the JSON string containing walkthrough summary and file changes.
        """
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
