"""Bitbucket Cloud authentication — a static access token or API token.

Two credential shapes are accepted in ``MIRA_BITBUCKET_TOKEN``:

- a workspace / project / repository **access token**, sent as
  ``Authorization: Bearer <token>``. Comments post as the token's bot user;
- an Atlassian **API token** for a (bot) account, given as ``email:token`` and
  sent as HTTP Basic auth. Comments post as that account.

Bitbucket identifies users by ``account_id`` / ``uuid`` rather than a stable
username, and mentions in comment markdown are stored as ``@{account_id}``.
So besides the human-readable handle returned by ``get_bot_identity`` we keep
every identifier of the bot user, for self-author detection and for turning
``@{account_id}`` mentions back into ``@<handle>``.

Access tokens can't call ``GET /user`` (403). Their bot user is learned
instead from the author of the first comment Mira posts
(``remember_bot_user``) and persisted, so later self-detection works.
"""

from __future__ import annotations

import base64
import re

import httpx


def bitbucket_auth_headers(token: str) -> dict[str, str]:
    """``Authorization`` header for a Bitbucket credential (see module doc)."""
    if ":" in token:
        basic = base64.b64encode(token.encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {basic}"}
    return {"Authorization": f"Bearer {token}"}


def user_identifiers(user: dict | None) -> set[str]:
    """Every identifier a Bitbucket user object carries (non-empty only)."""
    if not user:
        return set()
    keys = ("nickname", "username", "account_id", "uuid", "display_name")
    return {str(user[k]) for k in keys if user.get(k)}


def user_account_id(user: dict | None) -> str:
    """The user's immutable id: ``account_id``, else ``uuid``. Use it for authorization."""
    if not user:
        return ""
    return str(user.get("account_id") or user.get("uuid") or "")


def user_handle(user: dict | None) -> str:
    """The human-readable handle for a user: nickname, else username, else display name."""
    if not user:
        return ""
    return str(user.get("nickname") or user.get("username") or user.get("display_name") or "")


# ``@{557058:…}`` (account id) or ``@{{…uuid…}}`` (a braced uuid).
_learned_user: dict | None = None


def remember_bot_user(user: dict | None) -> None:
    """Record the token's bot user, seen as the author of a comment Mira posted."""
    global _learned_user
    if not user or not user_identifiers(user) or user == _learned_user:
        return
    _learned_user = {
        k: user[k] for k in ("nickname", "account_id", "uuid", "display_name") if user.get(k)
    }
    from mira.platforms.bitbucket import state

    state.save_bot_user(_learned_user)


def learned_bot_user() -> dict | None:
    """The bot user learned from a posted comment (memory, then the app DB)."""
    global _learned_user
    if _learned_user is None:
        from mira.platforms.bitbucket import state

        _learned_user = state.load_bot_user()
    return _learned_user


_ID_MENTION = re.compile(r"@\{(\{[^}\s]+\}|[^}\s]+)\}")


def normalize_mentions(text: str, ids_to_handle: dict[str, str]) -> str:
    """Rewrite ``@{account_id}`` / ``@{uuid}`` mentions to ``@handle``.

    Bitbucket's editor stores an autocompleted mention as ``@{557058:…}``; the
    shared mention matcher looks for ``@<name>``. Unknown ids are left alone.
    """

    def _sub(m: re.Match[str]) -> str:
        raw = m.group(1)
        handle = ids_to_handle.get(raw) or ids_to_handle.get(f"{{{raw}}}")
        return f"@{handle}" if handle else m.group(0)

    return _ID_MENTION.sub(_sub, text)


class BitbucketTokenAuth:
    """A static Bitbucket Cloud credential. No minting, no expiry handling.

    ``bot_identity`` overrides the ``GET /user`` lookup. Repository and project
    access tokens can't always read ``/user``, so operators can set
    ``MIRA_BITBUCKET_BOT_USER`` to the bot's nickname (or account id).
    """

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.bitbucket.org/2.0",
        bot_identity: str | None = None,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._override = bot_identity or None
        self._fetched = False
        self._user: dict | None = None

    async def get_token(self, scope: str | int | None = None) -> str:
        return self._token

    async def _fetch_user(self) -> dict | None:
        if self._fetched:
            return self._user or learned_bot_user()
        self._fetched = True
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self._base_url}/user",
                    headers=bitbucket_auth_headers(self._token),
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    self._user = data if isinstance(data, dict) else None
        except (httpx.HTTPError, ValueError):
            self._user = None
        return self._user or learned_bot_user()

    async def get_bot_identity(self) -> str | None:
        """The bot's handle (nickname), via ``GET /user`` (cached)."""
        if self._override:
            return self._override
        return user_handle(await self._fetch_user()) or None

    async def get_bot_ids(self) -> set[str]:
        """All identifiers of the bot user (handle, account_id, uuid, …)."""
        ids = user_identifiers(await self._fetch_user())
        if self._override:
            ids.add(self._override)
        return ids
