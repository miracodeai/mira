"""Claude via a Pro/Max subscription OAuth token — Anthropic Messages API, called directly.

Request shape follows OpenCodex's OAuth adapter: Claude Code identity as the first system
block, OAuth beta + client headers, and custom tool names under a "custom_" prefix.
"""

from __future__ import annotations

import json
import logging
from typing import ClassVar

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from mira.config import LLMConfig
from mira.exceptions import LLMError, NonRetriableLLMError
from mira.llm import oauth_accounts
from mira.llm.base import _retriable
from mira.llm.tool_schemas import SUBMIT_REVIEW_TOOL, SUBMIT_WALKTHROUGH_TOOL

logger = logging.getLogger(__name__)

_TOOL_PREFIX = "custom_"
_JSON_ONLY = "Respond with only one valid JSON object. No markdown fences or explanatory text."
# Models that rejected `temperature` (newer Claude models deprecate it); learned per process.
_NO_TEMPERATURE: set[str] = set()
# Models that rejected a forced tool_choice; they get "auto" plus an instruction instead.
_NO_FORCED_TOOL: set[str] = set()
# Models that rejected adaptive thinking (Haiku 4.5, Sonnet 4.x); they get a token budget.
_BUDGET_THINKING: set[str] = set()
_THINKING_BUDGET = {"low": 2048, "medium": 8192, "high": 16384, "xhigh": 24576, "max": 32000}


def _wire_name(name: str) -> str:
    return name if name.startswith(_TOOL_PREFIX) else _TOOL_PREFIX + name


def _strip_name(name: str) -> str:
    return name[len(_TOOL_PREFIX) :] if name.startswith(_TOOL_PREFIX) else name


def _to_anthropic(messages: list) -> tuple[str, list[dict]]:
    """Chat-shaped messages -> (system text, Anthropic messages)."""
    system: list[str] = []
    out: list[dict] = []

    def push(role: str, blocks: list[dict]) -> None:
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content") or ""
        if role == "system":
            system.append(content)
        elif role == "assistant":
            blocks = [{"type": "text", "text": content}] if content else []
            for tc in msg.get("tool_calls") or []:
                args = tc.get("function", {}).get("arguments") or "{}"
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id") or "call_0",
                        "name": _wire_name(tc.get("function", {}).get("name", "")),
                        "input": json.loads(args) if isinstance(args, str) else args,
                    }
                )
            if blocks:
                push("assistant", blocks)
        elif role == "tool":
            push(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id") or "call_0",
                        "content": content,
                    }
                ],
            )
        else:
            push("user", [{"type": "text", "text": content}])
    return "\n\n".join(s for s in system if s), out


def _tool(chat_tool: dict) -> dict:
    f = chat_tool.get("function", {})
    return {
        "name": _wire_name(f.get("name", "")),
        "description": f.get("description", ""),
        "input_schema": f.get("parameters") or {"type": "object"},
    }


def _json_object(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else text


class AnthropicOAuthProvider:
    supports_json_mode: ClassVar[bool] = False
    supports_tool_calling: ClassVar[bool] = True

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self._send = retry(  # type: ignore[method-assign]
            stop=stop_after_attempt(config.max_retries),
            wait=wait_exponential(
                multiplier=1, min=config.retry_min_wait, max=config.retry_max_wait
            ),
            retry=retry_if_exception(_retriable),
            reraise=True,
        )(self._send)

    @property
    def usage(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
        }

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    async def _send(self, messages: list, *, extra_system: str = "", **body: object) -> dict:
        if self.config.model in _NO_TEMPERATURE:
            body.pop("temperature", None)
        effort = self.config.reasoning_effort
        if effort and effort != "off":
            body.pop("temperature", None)  # thinking rejects custom temperatures
            # Thinking counts against max_tokens; at a small configured cap it can use the
            # whole budget and stop before the tool call or answer.
            budget = _THINKING_BUDGET.get(effort, 8192)
            floor = budget + 4096
            body["max_tokens"] = max(int(body.get("max_tokens") or self.config.max_tokens), floor)  # type: ignore[call-overload]
            if self.config.model in _BUDGET_THINKING:
                body.pop("output_config", None)
                body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            else:
                body["thinking"] = {"type": "adaptive"}
                body["output_config"] = {"effort": "low" if effort == "minimal" else effort}
        forced = body.get("tool_choice")
        if self.config.model in _NO_FORCED_TOOL and isinstance(forced, dict) and "name" in forced:
            body["tool_choice"] = {"type": "auto"}
            extra_system = (
                f"{extra_system}\n\nRespond by calling the {forced['name']} tool.".strip()
            )
        account = await oauth_accounts.access_token("anthropic")
        system_text, wire_messages = _to_anthropic(messages)
        system = [{"type": "text", "text": oauth_accounts.CLAUDE_CODE_IDENTITY}]
        for text in (system_text, extra_system):
            if text:
                system.append({"type": "text", "text": text})
        payload = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "system": system,
            "messages": wire_messages,
            **body,
        }
        async with httpx.AsyncClient(timeout=self.config.request_timeout) as client:
            resp = await client.post(
                f"{oauth_accounts.ANTHROPIC_API}/v1/messages",
                headers=oauth_accounts.anthropic_headers(account["access"]),
                json=payload,
            )
        if resp.status_code != 200:
            if resp.status_code == 400 and "temperature" in body and "temperature" in resp.text:
                _NO_TEMPERATURE.add(self.config.model)
                return await self._send(messages, extra_system=extra_system, **body)
            if (
                resp.status_code == 400
                and "tool_choice" in resp.text
                and isinstance(forced, dict)
                and "name" in forced
                and forced is body.get("tool_choice")
            ):
                _NO_FORCED_TOOL.add(self.config.model)
                return await self._send(messages, extra_system=extra_system, **body)
            if resp.status_code == 400 and "adaptive thinking" in resp.text:
                _BUDGET_THINKING.add(self.config.model)
                return await self._send(messages, extra_system=extra_system, **body)
            if oauth_accounts.is_quota_error(resp):
                oauth_accounts.mark_rate_limited(account, resp)
            error = (
                NonRetriableLLMError
                if 400 <= resp.status_code < 500 and resp.status_code != 429
                else LLMError
            )
            raise error("api_error", status=resp.status_code, body=resp.text[:2000])
        data = resp.json()
        usage = data.get("usage") or {}
        self.total_prompt_tokens += usage.get("input_tokens", 0) + usage.get(
            "cache_read_input_tokens", 0
        )
        self.total_completion_tokens += usage.get("output_tokens", 0)
        return data

    @staticmethod
    def _text(data: dict) -> str:
        return "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )

    async def complete(
        self,
        messages: list[dict[str, str]],
        json_mode: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        data = await self._send(
            messages,
            extra_system=_JSON_ONLY if json_mode else "",
            temperature=self.config.temperature if temperature is None else temperature,
            max_tokens=max_tokens or self.config.max_tokens,
        )
        text = self._text(data)
        return _json_object(text) if json_mode else text

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict],
        temperature: float | None = None,
    ) -> str:
        if not tools:
            raise LLMError("no_tools")
        wire_tools = [_tool(t) for t in tools]
        data = await self._send(
            messages,
            tools=wire_tools,
            tool_choice={"type": "tool", "name": wire_tools[0]["name"]},
            temperature=self.config.temperature if temperature is None else temperature,
        )
        for block in data.get("content", []):
            if block.get("type") == "tool_use":
                return json.dumps(block.get("input") or {})
        text = self._text(data)
        if text:
            logger.warning("Claude returned text instead of a tool call; using it as fallback")
            return _json_object(text)
        raise LLMError("no_tool_call")

    async def complete_agentic(
        self,
        messages: list,
        tools: list[dict],
        temperature: float | None = None,
    ) -> dict:
        if not tools:
            raise LLMError("no_tools")
        data = await self._send(
            messages,
            tools=[_tool(t) for t in tools],
            tool_choice={"type": "auto"},
            temperature=self.config.temperature if temperature is None else temperature,
        )
        tool_calls = [
            {
                "id": b.get("id"),
                "type": "function",
                "function": {
                    "name": _strip_name(b.get("name", "")),
                    "arguments": json.dumps(b.get("input") or {}),
                },
            }
            for b in data.get("content", [])
            if b.get("type") == "tool_use"
        ]
        text = self._text(data)
        return {
            "role": "assistant",
            "content": text or (None if tool_calls else ""),
            "tool_calls": tool_calls,
        }

    async def review(self, messages: list[dict[str, str]], temperature: float | None = None) -> str:
        return await self.complete_with_tools(
            messages, tools=[SUBMIT_REVIEW_TOOL], temperature=temperature
        )

    async def walkthrough(self, messages: list[dict[str, str]]) -> str:
        return await self.complete_with_tools(messages, tools=[SUBMIT_WALKTHROUGH_TOOL])
