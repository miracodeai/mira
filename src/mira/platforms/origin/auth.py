"""Cursor Origin authentication.

Origin apps authenticate with an Ed25519-signed app JWT (``alg: EdDSA``), then
mint short-lived installation access tokens (``oit_…``) — the same shape as a
GitHub App, but with EdDSA instead of RS256.

Operators who already hold a bearer token (installation token or user access
token from the Origin CLI) can use :class:`OriginTokenAuth` for CLI review and
dashboard sync without minting.
"""

from __future__ import annotations

import contextlib
import logging
import time
from datetime import datetime

import httpx
import jwt

from mira.exceptions import WebhookError
from mira.platforms import profiles

logger = logging.getLogger(__name__)

# Origin installation tokens last at most 15 minutes (and never past the app
# JWT that requested them). Refresh when less than 60s remain.
_TOKEN_MIN_REMAINING = 60


def _resolve_private_key(value: str) -> str:
    """Accept either raw PEM text or ``@path/to/key.pem`` and return PEM text."""
    if value.startswith("@"):
        with open(value[1:]) as f:
            return f.read()
    return value


def _api_base() -> str:
    return (profiles.resolve("origin").get("api_url") or "https://api.cursor.com/v1/origin").rstrip(
        "/"
    )


class OriginTokenAuth:
    """A static Origin Bearer token. No minting, no expiry handling."""

    def __init__(self, token: str, base_url: str | None = None) -> None:
        if not token:
            raise WebhookError("Origin token is required")
        self._token = token
        self._base_url = (base_url or _api_base()).rstrip("/")
        self._identity_fetched = False
        self._identity: str | None = None

    async def get_token(self, scope: str | int | None = None) -> str:
        return self._token

    async def get_bot_identity(self) -> str | None:
        """Best-effort display name for the token's actor (cached).

        Installation tokens (``oit_…``) have no ``/user`` endpoint; try the
        authenticated-app metadata when the token is an app JWT, otherwise
        return ``None`` so callers fall back to the configured bot name.
        """
        if self._identity_fetched:
            return self._identity
        self._identity_fetched = True
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{self._base_url}/app", headers=headers, timeout=10.0)
                if resp.status_code == 200:
                    data = resp.json() or {}
                    name = data.get("displayName") or data.get("namespaceSlug") or data.get("id")
                    self._identity = name if isinstance(name, str) and name else None
        except (httpx.HTTPError, ValueError):
            self._identity = None
        return self._identity


class OriginAppAuth:
    """Origin App JWT + per-installation token minting (EdDSA / Ed25519)."""

    def __init__(self, app_id: str, private_key: str, base_url: str | None = None) -> None:
        if not app_id:
            raise WebhookError("Origin app id is required")
        self._app_id = app_id
        self._private_key = _resolve_private_key(private_key)
        self._base_url = (base_url or _api_base()).rstrip("/")
        self._token_cache: dict[str, tuple[str, float]] = {}
        self._identity_fetched = False
        self._identity: str | None = None

    async def get_token(self, scope: str | int | None = None) -> str:
        """PlatformAuth — mint a per-installation token. ``scope`` is the installation id."""
        if scope is None:
            raise WebhookError("Origin requires an installation id to mint a token")
        return await self.get_installation_token(str(scope))

    async def get_bot_identity(self) -> str | None:
        if not self._identity_fetched:
            try:
                self._identity = await self.get_app_display_name()
            except Exception as exc:
                logger.warning("Failed to resolve Origin bot identity: %s", exc)
                self._identity = None
            self._identity_fetched = True
        return self._identity

    def _generate_jwt(self, lifetime_s: int = 300) -> str:
        """EdDSA-signed app JWT. ``iss`` and ``kid`` are the Origin app id."""
        now = int(time.time())
        headers = {"alg": "EdDSA", "kid": self._app_id, "typ": "JWT"}
        payload = {
            "iss": self._app_id,
            "aud": "origin-apps",
            "iat": now,
            "exp": now + lifetime_s,
        }
        return jwt.encode(payload, self._private_key, algorithm="EdDSA", headers=headers)

    async def get_installation_token(self, installation_id: str) -> str:
        """Mint (or reuse a cached) installation access token for ``installation_id``."""
        cached = self._token_cache.get(installation_id)
        if cached:
            token, expires_at = cached
            if expires_at - time.time() > _TOKEN_MIN_REMAINING:
                return token

        app_jwt = self._generate_jwt()
        url = f"{self._base_url}/app/installations/{installation_id}/access_tokens"
        headers = {"Authorization": f"Bearer {app_jwt}"}

        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, json={})
            if resp.status_code not in (200, 201):
                raise WebhookError(
                    f"Failed to get Origin installation token "
                    f"(HTTP {resp.status_code}): {resp.text[:300]}"
                )
            data = resp.json()

        new_token: str = data.get("token") or data.get("accessToken") or ""
        if not new_token:
            raise WebhookError("Origin installation token response missing token")

        expires_at = time.time() + 4 * 60  # conservative default (~4 min)
        expires_raw = data.get("expiresAt") or data.get("expires_at")
        if isinstance(expires_raw, str) and expires_raw:
            with contextlib.suppress(ValueError):
                expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00")).timestamp()

        self._token_cache[installation_id] = (new_token, expires_at)
        logger.debug("Cached Origin installation token for %s", installation_id)
        return new_token

    async def get_app_display_name(self) -> str | None:
        """Fetch this Origin App's display name for @mention matching."""
        app_jwt = self._generate_jwt()
        headers = {"Authorization": f"Bearer {app_jwt}"}
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self._base_url}/app", headers=headers, timeout=10.0)
            if resp.status_code != 200:
                return None
            data = resp.json() or {}
            name = data.get("displayName") or data.get("namespaceSlug")
            return name if isinstance(name, str) and name else None
