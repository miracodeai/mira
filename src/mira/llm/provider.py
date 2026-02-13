"""LiteLLM wrapper with retry/fallback."""

from __future__ import annotations

import logging

import litellm
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

# Suppress LiteLLM's verbose internal logging which Railway displays as errors
litellm.suppress_debug_info = True
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

from mira.config import LLMConfig
from mira.exceptions import LLMError

# Suppress LiteLLM's verbose internal logging which Railway displays as errors
litellm.suppress_debug_info = True
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


class LLMProvider:
    """Wrapper around LiteLLM for LLM completions."""

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
    async def _call_llm(self, model: str, messages: list[dict[str, str]], json_mode: bool) -> str:
        """Make a single LLM call with retries."""
        kwargs: dict[str, object] = {
            "model": model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        response = await litellm.acompletion(**kwargs)
        content = response.choices[0].message.content or ""

        # Track usage
        usage = getattr(response, "usage", None)
        if usage:
            self.total_prompt_tokens += getattr(usage, "prompt_tokens", 0)
            self.total_completion_tokens += getattr(usage, "completion_tokens", 0)

        return content

    async def complete(
        self,
        messages: list[dict[str, str]],
        json_mode: bool = True,
    ) -> str:
        """Complete a prompt, with fallback model support."""
        try:
            return await self._call_llm(self.config.model, messages, json_mode)
        except Exception as primary_err:
            if self.config.fallback_model:
                logger.warning(
                    "Primary model %s failed (%s), trying fallback %s",
                    self.config.model,
                    primary_err,
                    self.config.fallback_model,
                )
                try:
                    return await self._call_llm(self.config.fallback_model, messages, json_mode)
                except Exception as fallback_err:
                    raise LLMError(
                        f"Both primary ({self.config.model}) and fallback "
                        f"({self.config.fallback_model}) models failed: {fallback_err}"
                    ) from fallback_err
            raise LLMError(
                f"LLM completion failed with {self.config.model}: {primary_err}"
            ) from primary_err

    def count_tokens(self, text: str) -> int:
        """Count tokens in text using litellm's token counter."""
        try:
            return int(litellm.token_counter(model=self.config.model, text=text))
        except Exception:
            return len(text) // 4

    @property
    def usage(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
        }
