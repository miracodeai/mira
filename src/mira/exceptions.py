"""Custom exception hierarchy for Mira."""


class MiraError(Exception):
    """Base exception for all Mira errors."""


class ConfigError(MiraError):
    """Error loading or validating configuration."""


class DiffParseError(MiraError):
    """Error parsing a diff/patch."""


class LLMError(MiraError):
    """Error communicating with an LLM provider."""


class ResponseParseError(MiraError):
    """Error parsing or validating LLM response."""


class ProviderError(MiraError):
    """Error communicating with a code hosting provider (GitHub, etc.)."""
