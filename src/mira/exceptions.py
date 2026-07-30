"""Custom exception hierarchy for Mira."""

from __future__ import annotations

# Import directly from the module, not the mira.llm package, to avoid a
# circular import: mira.llm.__init__ imports mira.config which imports
# mira.exceptions.
from mira.error_messages import get_error_message


class MiraError(Exception):
    """Base exception for all Mira errors."""

    @property
    def safe_message(self) -> str:
        """User-safe error message without sensitive details."""
        return str(self)


class ConfigError(MiraError):
    """Error loading or validating configuration."""


class DiffParseError(MiraError):
    """Error parsing a diff/patch."""


class LLMError(MiraError):
    """Error communicating with an LLM provider.

    Constructed from the centralized error catalog in
    ``mira.error_messages`` — never from an ad-hoc f-string. The catalog
    holds both the full template (with model names, internal errors) and a
    safe template (user-facing). ``safe_message`` returns the safe variant.

    Use the ``code`` parameter to select the error type, and pass template
    variables as keyword args::

        raise LLMError("tool_call_failed", model="glm-5.2", error=primary_err)
    """

    def __init__(self, code: str, **kwargs: object) -> None:
        self._code = code
        self._full, self._safe = get_error_message(code, **kwargs)
        super().__init__(self._full)

    @property
    def safe_message(self) -> str:
        """User-safe error message without model names or underlying errors."""
        return self._safe

    @property
    def code(self) -> str:
        """The error code from the catalog."""
        return self._code


class NonRetriableLLMError(LLMError):
    """LLM client error (4xx) that should not be retried."""


class ResponseParseError(MiraError):
    """Error parsing or validating LLM response."""


class ProviderError(MiraError):
    """Error communicating with a code hosting provider (GitHub, etc.)."""


class WebhookError(MiraError):
    """Error processing a webhook event."""
