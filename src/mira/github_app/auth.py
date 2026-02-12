"""GitHub App JWT authentication and installation token management."""

from __future__ import annotations

import logging
import time

import httpx
import jwt

from mira.exceptions import WebhookError

logger = logging.getLogger(__name__)

# Tokens last 60 min; refresh when less than 5 min remaining.
_TOKEN_TTL = 55 * 60  # 55 minutes
_TOKEN_MIN_REMAINING = 5 * 60  # 5 minutes


class GitHubAppAuth:
    """Handles GitHub App JWT generation and installation token caching."""

    def __init__(self, app_id: str, private_key: str) -> None:
        self._app_id = app_id
        self._private_key = private_key
        self._token_cache: dict[int, tuple[str, float]] = {}

    def _generate_jwt(self) -> str:
        """Generate an RS256-signed JWT for GitHub App authentication."""
        now = int(time.time())
        payload = {
            "iat": now - 60,  # issued-at with clock drift buffer
            "exp": now + 600,  # 10 minute expiry
            "iss": self._app_id,
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    async def get_installation_token(self, installation_id: int) -> str:
        """Get an installation access token, using cache when possible."""
        cached = self._token_cache.get(installation_id)
        if cached:
            token, expires_at = cached
            if expires_at - time.time() > _TOKEN_MIN_REMAINING:
                return token

        app_jwt = self._generate_jwt()
        url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers)
            if resp.status_code != 201:
                raise WebhookError(
                    f"Failed to get installation token (HTTP {resp.status_code}): {resp.text}"
                )
            data = resp.json()

        new_token: str = data["token"]
        new_expires_at = time.time() + _TOKEN_TTL
        self._token_cache[installation_id] = (new_token, new_expires_at)
        logger.debug("Cached installation token for %d", installation_id)
        return new_token
