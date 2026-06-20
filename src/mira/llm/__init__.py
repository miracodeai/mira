"""LLM provider package — factory entry point."""

from __future__ import annotations

from mira.config import LLMConfig
from mira.llm.base import LLMProviderProtocol


def create_llm(config: LLMConfig) -> LLMProviderProtocol:
    """Create the appropriate LLM provider based on config.provider.

    Returns an instance satisfying LLMProviderProtocol.
    """
    if config.provider == "bedrock":
        from mira.llm.bedrock import BedrockProvider

        return BedrockProvider(config)

    if config.provider in {"codex-cli", "codex_cli", "codex"}:
        from mira.llm.codex_cli import CodexCLIProvider

        return CodexCLIProvider(config)

    # Default: OpenAI-compatible endpoint (OpenRouter, vLLM, Ollama, etc.)
    from mira.llm.provider import LLMProvider

    return LLMProvider(config)
