"""Platform authentication abstraction.

GitHub uses an App-installation model that mints short-lived per-installation
tokens; GitLab uses a long-lived group/project access token. Both satisfy
``PlatformAuth`` so handlers can fetch a token and the bot's identity without
knowing which platform they're on. The concrete implementations live next to
each platform's webhook code (``platforms.github.auth`` / ``platforms.gitlab.auth``).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PlatformAuth(Protocol):
    """How a handler obtains a token and the bot's own identity."""

    async def get_token(self, scope: str | int | None = None) -> str:
        """Return an API token. ``scope`` is the installation id on GitHub,
        unused on token-based platforms."""
        ...

    async def get_bot_identity(self) -> str | None:
        """The bot's handle for self-author detection (GitHub App slug,
        GitLab token user's username). ``None`` if it can't be determined."""
        ...
