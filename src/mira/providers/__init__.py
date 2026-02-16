"""Provider registry and factory."""

from __future__ import annotations

import threading

from mira.providers.base import BaseProvider

_REGISTRY: dict[str, type[BaseProvider]] = {}
_LOCK = threading.Lock()


def register_provider(name: str, cls: type[BaseProvider]) -> None:
    """Register a provider class under the given name."""
    with _LOCK:
        _REGISTRY[name] = cls


def get_available_providers() -> list[str]:
    """Return a sorted list of registered provider names."""
    return sorted(_REGISTRY)


def create_provider(name: str, token: str) -> BaseProvider:
    """Instantiate a registered provider by name."""
    with _LOCK:
        if name not in _REGISTRY:
            available = ", ".join(sorted(_REGISTRY)) or "(none)"
            raise ValueError(f"Unknown provider {name!r}. Available: {available}")
        return _REGISTRY[name](token)


# Register built-in providers
from mira.providers.github import GitHubProvider  # noqa: E402

register_provider("github", GitHubProvider)
