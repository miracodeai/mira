"""GitLab authentication — a static group/project access token."""

from __future__ import annotations

import httpx


class GitLabTokenAuth:
    """A static group/project access token. No minting, no expiry handling."""

    def __init__(self, token: str, base_url: str = "https://gitlab.com/api/v4") -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._username_fetched = False
        self._username: str | None = None

    async def get_token(self, scope: str | int | None = None) -> str:
        return self._token

    async def get_bot_identity(self) -> str | None:
        """The token user's username, via ``GET /user`` (cached)."""
        if self._username_fetched:
            return self._username
        self._username_fetched = True
        headers = {"PRIVATE-TOKEN": self._token}
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self._base_url}/user", headers=headers, timeout=10.0)
                if resp.status_code == 200:
                    username = resp.json().get("username")
                    self._username = username if isinstance(username, str) and username else None
        except (httpx.HTTPError, ValueError):
            self._username = None
        return self._username
