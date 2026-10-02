"""LLM provider package — factory entry point."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from mira.config import LLMConfig
from mira.exceptions import NonRetriableLLMError
from mira.llm.base import LLMProviderProtocol

logger = logging.getLogger(__name__)
T = TypeVar("T")

# Model-id prefixes routed to a signed-in subscription account instead of config.provider,
# so one deployment can mix API-key and ChatGPT/Claude subscription models per purpose.
SUBSCRIPTION_PREFIXES = {"chatgpt/": "chatgpt", "claude/": "anthropic"}

# A model that failed with a retriable error (quota, outage) is tried last for this long.
CHAIN_COOLDOWN_SECONDS = 300
# ponytail: per-process cooldown map; share via the DB if Mira ever runs multiple replicas.
_cooling_until: dict[str, float] = {}


class ModelChain:
    """Try each model in order until one answers. Models can use different providers."""

    def __init__(self, models: list[str], providers: list[LLMProviderProtocol]) -> None:
        self.models = models
        self.providers = providers
        self.config = providers[0].config
        self.supports_json_mode = providers[0].supports_json_mode
        self.supports_tool_calling = all(p.supports_tool_calling for p in providers)
        self.supports_temperature = getattr(providers[0], "supports_temperature", True)

    @property
    def total_prompt_tokens(self) -> int:
        return sum(p.total_prompt_tokens for p in self.providers)

    @property
    def total_completion_tokens(self) -> int:
        return sum(p.total_completion_tokens for p in self.providers)

    @property
    def usage(self) -> dict[str, int]:
        prompt, completion = self.total_prompt_tokens, self.total_completion_tokens
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }

    def count_tokens(self, text: str) -> int:
        return self.providers[0].count_tokens(text)

    async def _run(self, call: Callable[[LLMProviderProtocol], Awaitable[T]]) -> T:
        now = time.time()
        # Stable sort: healthy models keep their order, recently failed ones go last.
        order = sorted(
            range(len(self.models)), key=lambda i: _cooling_until.get(self.models[i], 0) > now
        )
        last: Exception | None = None
        for i in order:
            try:
                return await call(self.providers[i])
            except Exception as exc:
                last = exc
                if not isinstance(exc, NonRetriableLLMError):
                    _cooling_until[self.models[i]] = time.time() + CHAIN_COOLDOWN_SECONDS
                logger.warning("Model %s failed (%s); trying the next model", self.models[i], exc)
        assert last is not None
        raise last

    async def complete(
        self,
        messages: list[dict[str, str]],
        json_mode: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        return await self._run(lambda p: p.complete(messages, json_mode, temperature, max_tokens))

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict],
        temperature: float | None = None,
    ) -> str:
        return await self._run(lambda p: p.complete_with_tools(messages, tools, temperature))

    async def complete_agentic(
        self, messages: list, tools: list[dict], temperature: float | None = None
    ) -> dict:
        return await self._run(lambda p: p.complete_agentic(messages, tools, temperature))

    async def review(self, messages: list[dict[str, str]], temperature: float | None = None) -> str:
        return await self._run(lambda p: p.review(messages, temperature))

    async def walkthrough(self, messages: list[dict[str, str]]) -> str:
        return await self._run(lambda p: p.walkthrough(messages))


def create_llm(config: LLMConfig) -> LLMProviderProtocol:
    """Create the provider for config.model, wrapped in a ModelChain when fallbacks are set."""
    extra = [config.fallback_model] if config.fallback_model else []
    models = list(dict.fromkeys([config.model, *config.fallback_models, *extra]))
    if len(models) == 1:
        return _create_one(config)
    single: dict[str, Any] = {"fallback_model": None, "fallback_models": []}
    chain_models, providers = [], []
    for i, m in enumerate(models):
        model = m
        try:
            model, _ = parse_model_entry(m)
            providers.append(_create_one(config.model_copy(update={**single, "model": m})))
        except Exception as exc:
            if i == 0:
                raise
            # An unusable fallback (e.g. a disconnected provider) must not block the primary.
            logger.warning("Skipping fallback model %s: %s", model, exc)
            continue
        chain_models.append(model)
    return ModelChain(chain_models, providers) if len(providers) > 1 else providers[0]


def parse_model_entry(entry: str) -> tuple[str, str | None]:
    """Split an optional #effort without exposing it to providers or model lookups."""
    if "#" not in entry:
        return entry, None
    from mira.dashboard.models_config import THINKING_MODE_VALUES

    model, _, effort = entry.partition("#")
    if not model or effort not in THINKING_MODE_VALUES:
        raise ValueError(f"Invalid model reasoning entry: {entry!r}")
    return model, effort


def _create_one(config: LLMConfig) -> LLMProviderProtocol:
    model, effort = parse_model_entry(config.model)
    if effort is not None:
        config = config.model_copy(
            update={"model": model, "reasoning_effort": None if effort == "off" else effort}
        )
    for prefix, account in SUBSCRIPTION_PREFIXES.items():
        if config.model.startswith(prefix):
            sub = config.model_copy(update={"model": config.model.removeprefix(prefix)})
            if account == "anthropic":
                from mira.llm.anthropic_oauth import AnthropicOAuthProvider

                return AnthropicOAuthProvider(sub)
            from mira.llm.chatgpt_oauth import ChatGPTOAuthProvider
            from mira.llm.oauth_accounts import CHATGPT_API

            return ChatGPTOAuthProvider(sub.model_copy(update={"base_url": CHATGPT_API}))

    if config.model.startswith("@"):
        return _create_connected(config)

    if config.provider == "bedrock":
        from mira.llm.bedrock import BedrockProvider

        return BedrockProvider(config)

    if config.provider in {"codex-cli", "codex_cli", "codex"}:
        from mira.llm.codex_cli import CodexCLIProvider

        return CodexCLIProvider(config)

    if config.api_style == "responses":
        from mira.llm.responses import ResponsesProvider

        return ResponsesProvider(config)

    # Default: OpenAI-compatible endpoint (OpenRouter, vLLM, Ollama, etc.)
    from mira.llm.provider import LLMProvider

    return LLMProvider(config)


def _create_connected(config: LLMConfig) -> LLMProviderProtocol:
    """`@<id>/<model>` runs on a provider connected in Settings → Providers."""
    from mira.llm import api_providers
    from mira.llm import provider_profiles as profiles
    from mira.llm.provider import LLMProvider

    pid, _, model = config.model[1:].partition("/")
    provider = api_providers.get(pid)
    if provider is None:
        raise NonRetriableLLMError("provider_not_connected", provider=pid)
    key = provider.get("api_key") or None
    # The client strips the first "vendor/" segment unless the endpoint's profile keeps it
    # (OpenRouter); prefix with the id so the provider gets its own model id unchanged.
    if profiles.resolve(provider["base_url"]).get("model_prefix") != "keep":
        model = f"{pid}/{model}"
    return LLMProvider(
        config.model_copy(
            update={
                "model": model,
                "base_url": provider["base_url"],
                "api_key": key,
                "api_key_env": config.api_key_env if key else "",
                "api_style": "chat",
            }
        )
    )
