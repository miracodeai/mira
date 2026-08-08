"""OpenAI Responses API provider ({base_url}/responses).

Speaks the Responses protocol: ``input`` items, ``text.format`` for JSON
mode, ``max_output_tokens``, and ``output`` items (``function_call`` /
``output_text``). Presents the same chat-shaped conversation contract to
callers, converting to/from Responses input/output items internally.
"""

from __future__ import annotations

import json
import logging
from typing import ClassVar

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from mira.config import LLMConfig
from mira.exceptions import LLMError, NonRetriableLLMError
from mira.llm import provider_profiles as profiles
from mira.llm.provider import _get_api_key, _retriable, _strip_model_prefix
from mira.llm.tool_schemas import SUBMIT_REVIEW_TOOL, SUBMIT_WALKTHROUGH_TOOL

logger = logging.getLogger(__name__)


# ── Message conversion helpers ──────────────────────────────────────


def _responses_input(messages: list[dict]) -> list[dict]:
    """Convert chat-shaped messages to Responses API input items.

    system/user -> {"role": role, "content": <str>} (simple form).
    assistant   -> output-text message item, PLUS one {"type": "function_call",
                  "id"/"call_id" = call id, "name", "arguments"} item per tool call.
    tool        -> {"type": "function_call_output", "call_id": msg["tool_call_id"]
                  or "call_0", "output": <str>}.
    Assistant items with empty content AND no tool_calls are skipped.
    ``arguments`` may arrive as a dict or JSON string: json.dumps dicts.
    """
    items: list[dict] = []
    for msg in messages:
        role = msg.get("role", "user")

        if role == "system":
            items.append({"role": "system", "content": msg.get("content", "")})
            continue

        if role == "user":
            items.append({"role": "user", "content": msg.get("content", "")})
            continue

        if role == "assistant":
            content = msg.get("content")
            tool_calls = msg.get("tool_calls") or []
            if not content and not tool_calls:
                continue  # skip empty assistant
            if content:
                items.append(
                    {"type": "message", "role": "assistant", "content": content}
                )
            for tc in tool_calls:
                args = tc.get("function", {}).get("arguments", "{}")
                if isinstance(args, dict):
                    args = json.dumps(args)
                items.append(
                    {
                        "type": "function_call",
                        "id": tc.get("id", ""),
                        "call_id": tc.get("id", ""),
                        "name": tc.get("function", {}).get("name", ""),
                        "arguments": args,
                    }
                )
            continue

        if role == "tool":
            call_id = msg.get("tool_call_id", "call_0")
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": msg.get("content", ""),
                }
            )
            continue

    return items


def _responses_tool(chat_tool: dict) -> dict:
    """Flatten {"type":"function","function":{...}} to the Responses shape
    {"type":"function","name","description","parameters"}."""
    f = chat_tool["function"]
    out: dict = {"type": "function", "name": f["name"]}
    if f.get("description"):
        out["description"] = f["description"]
    if f.get("parameters"):
        out["parameters"] = f["parameters"]
    return out


def _output_text(data: dict) -> str:
    """Concatenated assistant text: scan data["output"] for message items whose
    content parts are type "output_text" or "text" (accept both for compat),
    falling back to data.get("output_text", "") when no message item exists."""
    output = data.get("output", [])
    for item in output:
        if item.get("type") == "message" and item.get("role") == "assistant":
            content = item.get("content", [])
            parts = []
            if isinstance(content, list):
                for part in content:
                    if part.get("type") in ("output_text", "text"):
                        parts.append(part.get("text", ""))
            if parts:
                return "".join(parts)
    # Fallback: some implementations put output_text at the top level
    return data.get("output_text", "")


def _response_message(data: dict) -> dict:
    """Normalize a Responses response to the chat-shaped assistant dict the
    agentic loop reads: {"role":"assistant","content": <str or None>,
    "tool_calls": [{"id","type":"function","function":{"name","arguments"}}]}.
    content None when only tool calls are present (match chat shape).
    tool_calls empty list when none."""
    output = data.get("output", [])
    text_parts: list[str] = []
    tool_calls: list[dict] = []

    for item in output:
        itype = item.get("type", "")
        if itype == "message" and item.get("role") == "assistant":
            content = item.get("content", [])
            if isinstance(content, list):
                for part in content:
                    if part.get("type") in ("output_text", "text"):
                        text_parts.append(part.get("text", ""))
        elif itype == "function_call":
            # In Responses API, name/arguments are direct on the item
            name = item.get("name", "")
            arguments = item.get("arguments", "{}")
            call_id = item.get("call_id") or item.get("id", "")
            tool_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            )

    content = "".join(text_parts) if text_parts else (None if tool_calls else "")
    return {"role": "assistant", "content": content, "tool_calls": tool_calls}


# ── Provider class ──────────────────────────────────────────────────


class ResponsesProvider:
    """OpenAI Responses API provider ({base_url}/responses).

    Same OpenAI-compatible endpoint/auth/model ids as the chat provider, but
    speaks the /responses protocol. Presents the same chat-shaped conversation
    contract to callers.
    """

    supports_json_mode: ClassVar[bool] = True
    supports_tool_calling: ClassVar[bool] = True

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.profile = profiles.resolve(config.base_url)
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self._no_forced_tool_choice: set[str] = set()
        self._no_reasoning: set[str] = set()
        self._url = f"{config.base_url.rstrip('/')}/responses"

        self._retry = retry(
            stop=stop_after_attempt(config.max_retries),
            wait=wait_exponential(
                multiplier=1,
                min=config.retry_min_wait,
                max=config.retry_max_wait,
            ),
            retry=retry_if_exception(_retriable),
            reraise=True,
        )
        self._call_llm = self._retry(self._call_llm)
        self._call_llm_with_tools = self._retry(self._call_llm_with_tools)
        self._call_llm_agentic = self._retry(self._call_llm_agentic)

    def _build_headers(self) -> dict[str, str]:
        """Build request headers: Content-Type, optional Bearer auth, and any
        provider-specific extras from the profile."""
        if hasattr(self, "_cached_headers"):
            return dict(self._cached_headers)
        headers: dict[str, str] = {"Content-Type": "application/json"}
        key = _get_api_key(self.config, self.profile)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        headers.update(self.profile.get("extra_headers", {}))
        self._cached_headers = headers
        return dict(headers)

    def _apply_reasoning(self, body: dict) -> None:
        """Enable extended thinking when a reasoning effort is configured."""
        effort = self.config.reasoning_effort
        if not effort or effort == "off":
            return
        if body.get("model") in self._no_reasoning:
            return
        effort = self.profile.get("reasoning_effort_map", {}).get(effort, effort)
        body["reasoning"] = {"effort": effort}
        body.pop("temperature", None)

    def _account_usage(self, data: dict) -> None:
        """Accumulate token counts from Responses API usage.

        The Responses API uses ``input_tokens`` / ``output_tokens`` keys,
        mapping to our ``total_prompt_tokens`` / ``total_completion_tokens``.
        """
        usage = data.get("usage")
        if usage:
            self.total_prompt_tokens += usage.get("input_tokens", 0)
            self.total_completion_tokens += usage.get("output_tokens", 0)

    # ── Internal LLM calls (retry-decorated) ────────────────────────

    async def _call_llm(
        self,
        model: str,
        messages: list[dict[str, str]],
        json_mode: bool,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Make a single LLM call with retries against the /responses endpoint."""
        body: dict = {
            "model": _strip_model_prefix(model, self.config.base_url),
            "input": _responses_input(messages),
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_output_tokens": max_tokens if max_tokens is not None else self.config.max_tokens,
        }
        if json_mode:
            body["text"] = {"format": {"type": "json_object"}}
        self._apply_reasoning(body)

        async with httpx.AsyncClient(timeout=self.config.request_timeout) as client:
            resp = await client.post(
                self._url,
                headers=self._build_headers(),
                json=body,
            )
            if resp.status_code != 200:
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    raise NonRetriableLLMError(f"LLM API error {resp.status_code}: {resp.text}")
                raise LLMError(f"LLM API error {resp.status_code}: {resp.text}")
            data = resp.json()

        self._account_usage(data)
        return _output_text(data)

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
        api_model = _strip_model_prefix(model, self.config.base_url)
        forced_choice: dict | str = {
            "type": "function",
            "name": tools[0]["function"]["name"],
        }
        body: dict = {
            "model": api_model,
            "input": _responses_input(messages),
            "tools": [_responses_tool(t) for t in tools],
            "tool_choice": "auto" if api_model in self._no_forced_tool_choice else forced_choice,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_output_tokens": self.config.max_tokens,
        }
        self._apply_reasoning(body)

        async with httpx.AsyncClient(timeout=self.config.request_timeout) as client:
            resp = await client.post(
                self._url,
                headers=self._build_headers(),
                json=body,
            )
            if (
                resp.status_code == 400
                and body["tool_choice"] != "auto"
                and "tool_choice" in resp.text.lower()
            ):
                logger.info(
                    "Model %s rejected forced tool_choice; retrying with auto", api_model
                )
                self._no_forced_tool_choice.add(api_model)
                body["tool_choice"] = "auto"
                resp = await client.post(
                    self._url, headers=self._build_headers(), json=body
                )
            if resp.status_code == 400 and "reasoning" in body and "reasoning" in resp.text.lower():
                logger.info(
                    "Model %s rejected reasoning effort; retrying without it", api_model
                )
                self._no_reasoning.add(api_model)
                body.pop("reasoning", None)
                body["temperature"] = (
                    temperature if temperature is not None else self.config.temperature
                )
                resp = await client.post(
                    self._url, headers=self._build_headers(), json=body
                )
            if resp.status_code != 200:
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    raise NonRetriableLLMError(f"LLM API error {resp.status_code}: {resp.text}")
                raise LLMError(f"LLM API error {resp.status_code}: {resp.text}")
            data = resp.json()

        self._account_usage(data)

        # Check for tool calls in the response
        output = data.get("output", [])
        for item in output:
            if item.get("type") == "function_call":
                return item.get("arguments") or "{}"

        # Fallback: text content
        text = _output_text(data)
        if text:
            logger.warning(
                "Model returned content instead of tool call, using content as fallback"
            )
            return text

        raise LLMError("Model returned neither tool call nor content")

    async def _call_llm_agentic(
        self,
        model: str,
        messages: list,
        tools: list[dict],
        temperature: float | None = None,
    ) -> dict:
        """Make a tool-using LLM call without forcing a specific tool.

        Returns the full assistant message (with ``tool_calls`` and ``content``)
        so the caller can dispatch and continue the conversation.
        """
        body: dict = {
            "model": _strip_model_prefix(model, self.config.base_url),
            "input": _responses_input(messages),
            "tools": [_responses_tool(t) for t in tools],
            "tool_choice": "auto",
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_output_tokens": self.config.max_tokens,
        }
        self._apply_reasoning(body)

        async with httpx.AsyncClient(timeout=self.config.request_timeout) as client:
            resp = await client.post(
                self._url,
                headers=self._build_headers(),
                json=body,
            )
            if resp.status_code == 400 and "reasoning" in body and "reasoning" in resp.text.lower():
                api_model = _strip_model_prefix(model, self.config.base_url)
                logger.info(
                    "Model %s rejected reasoning effort; retrying without it", api_model
                )
                self._no_reasoning.add(api_model)
                body.pop("reasoning", None)
                body["temperature"] = (
                    temperature if temperature is not None else self.config.temperature
                )
                resp = await client.post(
                    self._url, headers=self._build_headers(), json=body
                )
            if resp.status_code != 200:
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    raise NonRetriableLLMError(f"LLM API error {resp.status_code}: {resp.text}")
                raise LLMError(f"LLM API error {resp.status_code}: {resp.text}")
            data = resp.json()

        self._account_usage(data)
        return _response_message(data)

    # ── Public API (matches LLMProviderProtocol) ─────────────────────

    async def complete(
        self,
        messages: list[dict[str, str]],
        json_mode: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Complete a prompt using JSON mode, with fallback model support."""
        try:
            return await self._call_llm(
                self.config.model,
                messages,
                json_mode,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except NonRetriableLLMError:
            raise
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
                        self.config.fallback_model,
                        messages,
                        json_mode,
                        temperature=temperature,
                        max_tokens=max_tokens,
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
        """Complete a prompt using tool calling for structured output."""
        try:
            return await self._call_llm_with_tools(
                self.config.model, messages, tools, temperature=temperature
            )
        except NonRetriableLLMError:
            raise
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
                        self.config.fallback_model,
                        messages,
                        tools,
                        temperature=temperature,
                    )
                except Exception as fallback_err:
                    raise LLMError(
                        f"Both primary ({self.config.model}) and fallback "
                        f"({self.config.fallback_model}) models failed: {fallback_err}"
                    ) from fallback_err
            raise LLMError(
                f"LLM tool-call failed with {self.config.model}: {primary_err}"
            ) from primary_err

    async def complete_agentic(
        self,
        messages: list,
        tools: list[dict],
        temperature: float | None = None,
    ) -> dict:
        """Single hop of an agentic loop. Returns the assistant message dict."""
        try:
            return await self._call_llm_agentic(
                self.config.model, messages, tools, temperature=temperature
            )
        except NonRetriableLLMError:
            raise
        except Exception as primary_err:
            if self.config.fallback_model:
                logger.warning(
                    "Primary model %s failed (%s), trying fallback %s",
                    self.config.model,
                    primary_err,
                    self.config.fallback_model,
                )
                try:
                    return await self._call_llm_agentic(
                        self.config.fallback_model,
                        messages,
                        tools,
                        temperature=temperature,
                    )
                except Exception as fallback_err:
                    raise LLMError(
                        f"Both primary ({self.config.model}) and fallback "
                        f"({self.config.fallback_model}) models failed: {fallback_err}"
                    ) from fallback_err
            raise LLMError(
                f"LLM agentic call failed with {self.config.model}: {primary_err}"
            ) from primary_err

    async def review(
        self, messages: list[dict[str, str]], temperature: float | None = None
    ) -> str:
        """Submit a review using tool calling."""
        return await self.complete_with_tools(
            messages, tools=[SUBMIT_REVIEW_TOOL], temperature=temperature
        )

    async def walkthrough(self, messages: list[dict[str, str]]) -> str:
        """Submit a walkthrough using tool calling."""
        return await self.complete_with_tools(
            messages, tools=[SUBMIT_WALKTHROUGH_TOOL]
        )

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
