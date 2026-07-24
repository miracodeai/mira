"""OpenAI-compatible API provider with retry/fallback and tool calling support.

Per-provider quirks (attribution headers, model-prefix policy, reasoning
remapping) come from the profile registry in ``mira.llm.provider_profiles``, matched
to the configured ``base_url``. OpenRouter is the one profile with quirks; any
other OpenAI-compatible endpoint works off the portable default, no entry needed.
"""

from __future__ import annotations

import logging
import os
from typing import ClassVar

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from mira.config import LLMConfig
from mira.exceptions import LLMError
from mira.llm import provider_profiles as profiles
from mira.llm.tool_schemas import SUBMIT_REVIEW_TOOL, SUBMIT_WALKTHROUGH_TOOL

logger = logging.getLogger(__name__)


def _get_api_key(config: LLMConfig, profile: dict | None = None) -> str:
    """Resolve the API key for the configured endpoint.

    Reads `config.api_key_env` first, then the matched provider profile's
    `api_key_env`, then the legacy `OPENROUTER_API_KEY` / `OPENAI_API_KEY`
    lookup for backward compatibility. If `api_key_env` is explicitly "" the
    empty string is returned without error — useful for local endpoints
    (Ollama, llama.cpp server) that don't require auth.
    """
    if config.api_key_env == "":
        return ""
    key = os.environ.get(config.api_key_env, "")
    if not key and profile and profile.get("api_key_env"):
        key = os.environ.get(profile["api_key_env"], "")
    if not key:
        # Back-compat with pre-`api_key_env` setups.
        key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise LLMError(
            f"No API key found. Set {config.api_key_env} (or OPENROUTER_API_KEY / "
            f'OPENAI_API_KEY) in the environment, or set llm.api_key_env: "" in '
            f"your config for a local endpoint that needs no auth."
        )
    return key


def _strip_model_prefix(model: str, base_url: str) -> str:
    """Apply the endpoint's model-prefix policy from its provider profile.

    'keep' (OpenRouter) routes on the full `vendor/model` string and only sheds
    a redundant self-prefix (`openrouter/…`). 'strip' (the default for other
    endpoints) sends the bare model name (e.g. 'minimax/MiniMax-M2.7' →
    'MiniMax-M2.7').
    """
    profile = profiles.resolve(base_url)
    if profile.get("model_prefix") == "keep":
        self_prefix = f"{profile['name']}/"
        return model[len(self_prefix) :] if model.startswith(self_prefix) else model
    return model.split("/", 1)[1] if "/" in model else model


class LLMProvider:
    """OpenAI-compatible API client for LLM completions."""

    supports_json_mode: ClassVar[bool] = True
    supports_tool_calling: ClassVar[bool] = True

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        # Per-provider quirks (headers, model-prefix, reasoning remap), matched
        # to the endpoint by base_url. Unknown endpoints get the portable default.
        self.profile = profiles.resolve(config.base_url)
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        # Models that 400 on a forced tool_choice (deepseek thinking mode);
        # remembered so we send tool_choice="auto" instead.
        self._no_forced_tool_choice: set[str] = set()
        # Models that 400 on a reasoning effort (it's opt-in and applied to
        # whatever model is selected); remembered so we drop it and review
        # without thinking rather than failing.
        self._no_reasoning: set[str] = set()

    def _chat_url(self) -> str:
        return f"{self.config.base_url.rstrip('/')}/chat/completions"

    def _build_headers(self) -> dict[str, str]:
        """Build request headers: Content-Type, optional Bearer auth, and any
        provider-specific extras from the profile (e.g. OpenRouter's ranking
        headers). Authorization is omitted entirely if the endpoint needs no
        key (Ollama, llama.cpp, etc.)."""
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
        """Enable extended thinking when a reasoning effort is configured.

        The effort is passed via the unified ``reasoning.effort`` knob, after
        any per-provider remap from the profile (e.g. OpenRouter wants
        ``xhigh`` where DeepSeek's native top level is ``max``). Anthropic
        models reject a custom ``temperature`` while thinking is on, so we drop
        it. No-op when reasoning is off, keeping the request unchanged.
        """
        effort = self.config.reasoning_effort
        if not effort or effort == "off":
            return
        if body.get("model") in self._no_reasoning:
            return
        effort = self.profile.get("reasoning_effort_map", {}).get(effort, effort)
        body["reasoning"] = {"effort": effort}
        body.pop("temperature", None)

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
        """Make a single LLM call with retries against the configured endpoint."""
        body: dict = {
            "model": _strip_model_prefix(model, self.config.base_url),
            "messages": messages,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": max_tokens if max_tokens is not None else self.config.max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        self._apply_reasoning(body)

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                self._chat_url(),
                headers=self._build_headers(),
                json=body,
            )
            if resp.status_code != 200:
                raise LLMError(f"LLM API error {resp.status_code}: {resp.text}")
            data = resp.json()

        content = data["choices"][0]["message"].get("content") or ""

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
        api_model = _strip_model_prefix(model, self.config.base_url)
        forced_choice: dict | str = {
            "type": "function",
            "function": {"name": tools[0]["function"]["name"]},
        }
        body: dict = {
            "model": api_model,
            "messages": messages,
            "tools": tools,
            # Force the one tool for structured args; models that reject a
            # forced choice fall back to "auto" (handled on the 400 below).
            "tool_choice": "auto" if api_model in self._no_forced_tool_choice else forced_choice,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        self._apply_reasoning(body)

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                self._chat_url(),
                headers=self._build_headers(),
                json=body,
            )
            if (
                resp.status_code == 400
                and body["tool_choice"] != "auto"
                and "tool_choice" in resp.text.lower()
            ):
                # Forced choice unsupported — remember it and let the model pick.
                logger.info("Model %s rejected forced tool_choice; retrying with auto", api_model)
                self._no_forced_tool_choice.add(api_model)
                body["tool_choice"] = "auto"
                resp = await client.post(self._chat_url(), headers=self._build_headers(), json=body)
            if resp.status_code == 400 and "reasoning" in body and "reasoning" in resp.text.lower():
                # Reasoning effort unsupported on this model/endpoint — drop it
                # and review without thinking instead of failing the review.
                logger.info("Model %s rejected reasoning effort; retrying without it", api_model)
                self._no_reasoning.add(api_model)
                body.pop("reasoning", None)
                body["temperature"] = (
                    temperature if temperature is not None else self.config.temperature
                )
                resp = await client.post(self._chat_url(), headers=self._build_headers(), json=body)
            if resp.status_code != 200:
                raise LLMError(f"LLM API error {resp.status_code}: {resp.text}")
            data = resp.json()

        usage = data.get("usage")
        if usage:
            self.total_prompt_tokens += usage.get("prompt_tokens", 0)
            self.total_completion_tokens += usage.get("completion_tokens", 0)

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
                self.config.model,
                messages,
                json_mode,
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

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    async def _call_llm_agentic(
        self,
        model: str,
        messages: list,
        tools: list[dict],
        temperature: float | None = None,
    ) -> dict:
        """Make a tool-using LLM call without forcing a specific tool.

        Unlike `_call_llm_with_tools`, this returns the *full* assistant
        message (with `tool_calls` and `content`) so the caller can
        dispatch the calls and continue the conversation. This is what
        the agentic loop needs.
        """
        body: dict = {
            "model": _strip_model_prefix(model, self.config.base_url),
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        self._apply_reasoning(body)

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                self._chat_url(),
                headers=self._build_headers(),
                json=body,
            )
            if resp.status_code != 200:
                raise LLMError(f"LLM API error {resp.status_code}: {resp.text}")
            data = resp.json()

        usage = data.get("usage")
        if usage:
            self.total_prompt_tokens += usage.get("prompt_tokens", 0)
            self.total_completion_tokens += usage.get("completion_tokens", 0)

        return data["choices"][0]["message"]

    async def complete_agentic(
        self,
        messages: list,
        tools: list[dict],
        temperature: float | None = None,
    ) -> dict:
        """Single hop of an agentic loop. Returns the assistant message dict.

        The caller is responsible for the loop: append the message,
        dispatch any `tool_calls`, append the tool results as `tool`-role
        messages, and call again until the terminal tool fires.
        """
        try:
            return await self._call_llm_agentic(
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
                    return await self._call_llm_agentic(
                        self.config.fallback_model, messages, tools, temperature=temperature
                    )
                except Exception as fallback_err:
                    raise LLMError(
                        f"Both primary ({self.config.model}) and fallback "
                        f"({self.config.fallback_model}) models failed: {fallback_err}"
                    ) from fallback_err
            raise LLMError(
                f"LLM agentic call failed with {self.config.model}: {primary_err}"
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

    async def review(self, messages: list[dict[str, str]], temperature: float | None = None) -> str:
        """Submit a review using tool calling.

        Returns the JSON string containing review comments, key issues, and summary.
        """
        return await self.complete_with_tools(
            messages, tools=[SUBMIT_REVIEW_TOOL], temperature=temperature
        )

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
