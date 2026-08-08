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

from mira.config import LLMConfig
from mira.exceptions import LLMError
from mira.llm.base import OpenAICompatibleProvider, _strip_model_prefix

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
                items.append({"type": "message", "role": "assistant", "content": content})
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
    f = chat_tool.get("function", {})
    out: dict = {"type": "function", "name": f.get("name", "")}
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


class ResponsesProvider(OpenAICompatibleProvider):
    """OpenAI Responses API provider ({base_url}/responses).

    Same OpenAI-compatible endpoint/auth/model ids as the chat provider, but
    speaks the /responses protocol. Inherits protocol-agnostic infrastructure
    from :class:`OpenAICompatibleProvider`.
    """

    supports_json_mode: ClassVar[bool] = True
    supports_tool_calling: ClassVar[bool] = True

    def __init__(self, config: LLMConfig) -> None:
        super().__init__(config)
        self._url = f"{config.base_url.rstrip('/')}/responses"

    # ── Overrides ────────────────────────────────────────────────────

    def _account_usage(self, data: dict) -> None:
        """Accumulate token counts from Responses API usage.

        The Responses API uses ``input_tokens`` / ``output_tokens`` keys,
        mapping to our ``total_prompt_tokens`` / ``total_completion_tokens``.
        """
        usage = data.get("usage")
        if usage:
            self.total_prompt_tokens += usage.get("input_tokens", 0)
            self.total_completion_tokens += usage.get("output_tokens", 0)

    # ── Internal LLM calls (retry-decorated by base class) ──────────

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
            self._handle_error(resp)
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
        if not tools:
            raise LLMError("tools list must not be empty")
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
                logger.info("Model %s rejected forced tool_choice; retrying with auto", api_model)
                self._no_forced_tool_choice.add(api_model)
                body["tool_choice"] = "auto"
                resp = await client.post(self._url, headers=self._build_headers(), json=body)
            if resp.status_code == 400 and "reasoning" in body and "reasoning" in resp.text.lower():
                logger.info("Model %s rejected reasoning effort; retrying without it", api_model)
                self._no_reasoning.add(api_model)
                body.pop("reasoning", None)
                body["temperature"] = (
                    temperature if temperature is not None else self.config.temperature
                )
                resp = await client.post(self._url, headers=self._build_headers(), json=body)
            self._handle_error(resp)
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
            logger.warning("Model returned content instead of tool call, using content as fallback")
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
        if not tools:
            raise LLMError("tools list must not be empty")
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
                logger.info("Model %s rejected reasoning effort; retrying without it", api_model)
                self._no_reasoning.add(api_model)
                body.pop("reasoning", None)
                body["temperature"] = (
                    temperature if temperature is not None else self.config.temperature
                )
                resp = await client.post(self._url, headers=self._build_headers(), json=body)
            self._handle_error(resp)
            data = resp.json()

        self._account_usage(data)
        return _response_message(data)
