"""Subscription OAuth accounts (ChatGPT, Claude) — sign-in, storage, refresh, model lists.

Mirrors OpenCodex's flows: ChatGPT uses OpenAI's device-code grant over HTTP, Claude uses
PKCE against claude.ai with a localhost:54545 callback (or a pasted redirect URL / code).
Credentials live in one owner-only JSON file under MIRA_INDEX_DIR, never in API responses.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from mira.exceptions import LLMError, NonRetriableLLMError
from mira.llm import credential_store

logger = logging.getLogger(__name__)

PROVIDERS = ("chatgpt", "anthropic")

# ── Anthropic (Claude Pro/Max) ──
ANTHROPIC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
ANTHROPIC_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
ANTHROPIC_SCOPES = "org:create_api_key user:profile user:inference"
ANTHROPIC_CALLBACK_PORT = 54545
ANTHROPIC_REDIRECT_URI = f"http://localhost:{ANTHROPIC_CALLBACK_PORT}/callback"
ANTHROPIC_API = "https://api.anthropic.com"
ANTHROPIC_OAUTH_BETA = "claude-code-20250219,oauth-2025-04-20"
CLAUDE_CODE_IDENTITY = "You are a Claude agent, built on Anthropic's Claude Agent SDK."

# ── ChatGPT (Plus/Pro/Business via Codex) ──
CHATGPT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CHATGPT_TOKEN_URL = "https://auth.openai.com/oauth/token"
CHATGPT_USERCODE_URL = "https://auth.openai.com/api/accounts/deviceauth/usercode"
CHATGPT_DEVICE_TOKEN_URL = "https://auth.openai.com/api/accounts/deviceauth/token"
CHATGPT_DEVICE_REDIRECT_URI = "https://auth.openai.com/deviceauth/callback"
CHATGPT_VERIFICATION_URL = "https://auth.openai.com/codex/device"
CHATGPT_API = "https://chatgpt.com/backend-api/codex"
CHATGPT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_CLIENT_VERSION = "0.155.0"

_DEVICE_TTL = 15 * 60
_logins: dict[str, dict[str, Any]] = {}
_callback_server: asyncio.Server | None = None
# Account id -> time until which it is skipped after a 429 (in memory only).
_rate_limited_until: dict[str, float] = {}


# ── Storage ──────────────────────────────────────────────────────────

_STORE = "accounts.json"


def _load() -> dict[str, list[dict]]:
    data = credential_store.read(_STORE)
    return {p: list(data.get(p, [])) for p in PROVIDERS}


def _save(data: dict) -> None:
    credential_store.save(_STORE, data)


def _active(accounts: list[dict]) -> dict | None:
    return next((a for a in accounts if a.get("active")), accounts[0] if accounts else None)


def mask_email(email: str | None) -> str:
    if not email or "@" not in email:
        return email or ""
    name, domain = email.split("@", 1)
    return f"{name[:1]}***{name[-1:]}@{domain}" if len(name) > 1 else f"{name}***@{domain}"


def public_accounts() -> dict[str, list[dict]]:
    """Account list safe to return to the browser (no tokens, masked emails)."""
    out = {}
    now = time.time()
    for provider, accounts in _load().items():
        active = _active(accounts)
        out[provider] = [
            {
                "id": a["id"],
                "email": mask_email(a.get("email")),
                "active": a is active,
                "rate_limited": _rate_limited_until.get(a["id"], 0) > now,
            }
            for a in accounts
        ]
    return out


def has_account(provider: str) -> bool:
    return bool(_load()[provider])


def is_quota_error(resp: httpx.Response) -> bool:
    # The Codex backend reports an exhausted window as 502 usage_limit_reached, not only 429.
    return resp.status_code == 429 or "usage_limit" in resp.text


def mark_rate_limited(account: dict, resp: httpx.Response) -> None:
    """Park a quota-limited account so the next attempt uses another signed-in account."""
    try:
        wait = float(resp.headers.get("retry-after", ""))
    except ValueError:
        wait = 300
    _rate_limited_until[account["id"]] = time.time() + wait


async def _add_account(provider: str, creds: dict) -> None:
    async with credential_store.lock:
        data = _load()
        accounts = [
            a
            for a in data[provider]
            if not (creds.get("email") and a.get("email") == creds["email"])
        ]
        for a in accounts:
            a["active"] = False
        accounts.append({"id": uuid.uuid4().hex[:12], "active": True, **creds})
        data[provider] = accounts
        _save(data)


async def activate(provider: str, account_id: str) -> None:
    async with credential_store.lock:
        data = _load()
        if not any(a["id"] == account_id for a in data[provider]):
            raise KeyError(account_id)
        for a in data[provider]:
            a["active"] = a["id"] == account_id
        _save(data)


async def remove(provider: str, account_id: str | None = None) -> None:
    """Remove one account, or every account for the provider when account_id is None."""
    async with credential_store.lock:
        data = _load()
        data[provider] = [a for a in data[provider] if account_id and a["id"] != account_id]
        _save(data)


# ── Token helpers ────────────────────────────────────────────────────


def _jwt_claims(token: str | None) -> dict:
    try:
        payload = (token or "").split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:
        return {}


def _chatgpt_creds(data: dict, refresh_fallback: str = "") -> dict:
    if not data.get("access_token"):
        raise LLMError(
            "oauth_failed", provider="ChatGPT", detail="token response had no access token"
        )
    claims = _jwt_claims(data.get("id_token")) or _jwt_claims(data["access_token"])
    auth_ns = claims.get("https://api.openai.com/auth") or {}
    return {
        "access": data["access_token"],
        "refresh": data.get("refresh_token") or refresh_fallback,
        "expires": time.time() + float(data.get("expires_in") or 3600),
        "account_id": claims.get("chatgpt_account_id") or auth_ns.get("chatgpt_account_id"),
        "email": (claims.get("email") or "").lower() or None,
    }


def _anthropic_creds(data: dict, refresh_fallback: str = "") -> dict:
    if not data.get("access_token"):
        raise LLMError(
            "oauth_failed", provider="Claude", detail="token response had no access token"
        )
    account = data.get("account") or {}
    return {
        "access": data["access_token"],
        "refresh": data.get("refresh_token") or refresh_fallback,
        # Refresh 5 minutes early, like OpenCodex.
        "expires": time.time() + float(data.get("expires_in") or 3600) - 300,
        "account_id": account.get("uuid"),
        "email": account.get("email_address"),
    }


async def _post_token(provider: str, payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        if provider == "anthropic":
            resp = await client.post(ANTHROPIC_TOKEN_URL, json=payload)
        else:
            resp = await client.post(CHATGPT_TOKEN_URL, data=payload)
    if resp.status_code != 200:
        name = "Claude" if provider == "anthropic" else "ChatGPT"
        raise LLMError(
            "oauth_failed", provider=name, detail=f"HTTP {resp.status_code}: {resp.text[:300]}"
        )
    return resp.json()


async def _refresh(provider: str, account: dict) -> dict:
    payload = {"grant_type": "refresh_token", "refresh_token": account["refresh"]}
    if provider == "anthropic":
        payload["client_id"] = ANTHROPIC_CLIENT_ID
        fresh = _anthropic_creds(await _post_token(provider, payload), account["refresh"])
    else:
        payload["client_id"] = CHATGPT_CLIENT_ID
        fresh = _chatgpt_creds(await _post_token(provider, payload), account["refresh"])
    # Refresh responses may omit identity; keep what sign-in recorded.
    fresh["account_id"] = fresh["account_id"] or account.get("account_id")
    fresh["email"] = fresh["email"] or account.get("email")
    return fresh


async def access_token(provider: str, account_id: str | None = None) -> dict:
    """Return an account with a fresh token: account_id if given, else the active account
    (or the next signed-in one while the active account is rate limited)."""
    async with credential_store.lock:
        data = _load()
        active = _active(data[provider])
        if active is None:
            name = "Claude" if provider == "anthropic" else "ChatGPT"
            raise NonRetriableLLMError("oauth_not_signed_in", provider=name)
        if account_id:
            account = next((a for a in data[provider] if a["id"] == account_id), None)
            if account is None:
                raise KeyError(account_id)
        else:
            now = time.time()
            account = next(
                (
                    a
                    for a in [active, *data[provider]]
                    if _rate_limited_until.get(a["id"], 0) <= now
                ),
                active,
            )
        if account.get("expires", 0) < time.time() + 60:
            account.update(await _refresh(provider, account))
            _save(data)
        return dict(account)


# ── Request headers ──────────────────────────────────────────────────


def anthropic_headers(access: str) -> dict[str, str]:
    """Claude Code request fingerprint used by OpenCodex for OAuth tokens."""
    h = hashlib.sha256(f"claude-code-session:{access}".encode()).hexdigest()
    return {
        "Authorization": f"Bearer {access}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": ANTHROPIC_OAUTH_BETA,
        "Content-Type": "application/json",
        "User-Agent": "claude-cli/2.1.280 (external, cli)",
        "X-App": "cli",
        "X-Claude-Code-Session-Id": f"{h[:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:32]}",
        "x-client-request-id": str(uuid.uuid4()),
    }


def chatgpt_headers(account: dict) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {account['access']}",
        "originator": "codex_cli_rs",
        "Content-Type": "application/json",
    }
    if account.get("account_id"):
        headers["chatgpt-account-id"] = account["account_id"]
    return headers


# ── Model lists ──────────────────────────────────────────────────────


async def list_models(provider: str) -> list[dict]:
    """Live [{value, label}] from the signed-in account."""
    account = await access_token(provider)
    async with httpx.AsyncClient(timeout=15) as client:
        if provider == "anthropic":
            resp = await client.get(
                f"{ANTHROPIC_API}/v1/models?limit=100", headers=anthropic_headers(account["access"])
            )
            resp.raise_for_status()
            return [
                {"value": m["id"], "label": m.get("display_name") or m["id"]}
                for m in resp.json().get("data", [])
            ]
        resp = await client.get(
            f"{CHATGPT_API}/models?client_version={CODEX_CLIENT_VERSION}",
            headers=chatgpt_headers(account),
        )
        resp.raise_for_status()
        return [
            {"value": m["slug"], "label": m.get("display_name") or m["slug"]}
            for m in resp.json().get("models", [])
            if m.get("visibility", "list") == "list"
        ]


# ── Sign-in flows ────────────────────────────────────────────────────


def login_status(login_id: str) -> dict:
    login = _logins.get(login_id)
    if login is None:
        return {"status": "unknown"}
    return {"status": login["status"], "error": login.get("error")}


async def usage() -> dict[str, dict[str, list[dict]]]:
    """Subscription usage windows per provider and account id:
    [{label, percent (0-100), resets_at (ISO 8601)}]. An account that can't be read gets []."""
    out: dict[str, dict[str, list[dict]]] = {}
    async with httpx.AsyncClient(timeout=15) as client:
        for provider, accounts in _load().items():
            out[provider] = {}
            for a in accounts:
                try:
                    out[provider][a["id"]] = await _usage_windows(
                        client, provider, await access_token(provider, a["id"])
                    )
                except Exception as exc:
                    logger.warning(
                        "Usage read failed for %s account %s: %s", provider, a["id"], exc
                    )
                    out[provider][a["id"]] = []
    return out


async def _usage_windows(client: httpx.AsyncClient, provider: str, account: dict) -> list[dict]:
    if provider == "anthropic":
        resp = await client.get(
            f"{ANTHROPIC_API}/api/oauth/usage", headers=anthropic_headers(account["access"])
        )
        resp.raise_for_status()
        body = resp.json()
        buckets = [("5-hour", body.get("five_hour")), ("Weekly", body.get("seven_day"))]
        return [
            {"label": label, "percent": float(b["utilization"]), "resets_at": b.get("resets_at")}
            for label, b in buckets
            if b and b.get("utilization") is not None
        ]
    resp = await client.get(CHATGPT_USAGE_URL, headers=chatgpt_headers(account))
    resp.raise_for_status()
    limits = resp.json().get("rate_limit") or {}
    windows = []
    for w in (limits.get("primary_window"), limits.get("secondary_window")):
        if not w or w.get("used_percent") is None:
            continue
        seconds = w.get("limit_window_seconds") or 0
        label = {18_000: "5-hour", 604_800: "Weekly"}.get(seconds, f"{round(seconds / 3600)}-hour")
        reset = w.get("reset_at")
        windows.append(
            {
                "label": label,
                "percent": float(w["used_percent"]),
                "resets_at": datetime.fromtimestamp(reset, UTC).isoformat() if reset else None,
            }
        )
    return windows


async def start_chatgpt_login() -> dict:
    """Begin ChatGPT device-code sign-in; a background task polls until approved."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(CHATGPT_USERCODE_URL, json={"client_id": CHATGPT_CLIENT_ID})
    if resp.status_code != 200:
        raise LLMError(
            "oauth_failed", provider="ChatGPT", detail=f"device code HTTP {resp.status_code}"
        )
    payload = resp.json()
    device_id = payload.get("device_auth_id")
    user_code = payload.get("user_code") or payload.get("usercode")
    if not device_id or not user_code:
        raise LLMError("oauth_failed", provider="ChatGPT", detail="device code response incomplete")
    try:
        interval = min(max(float(payload.get("interval") or 5), 1.0), 30.0)
    except (TypeError, ValueError):
        interval = 5.0
    login_id = secrets.token_urlsafe(12)
    _logins[login_id] = {"status": "pending"}
    asyncio.create_task(_poll_chatgpt(login_id, device_id, user_code, interval))
    return {"login_id": login_id, "url": CHATGPT_VERIFICATION_URL, "code": user_code}


async def _poll_chatgpt(login_id: str, device_id: str, user_code: str, interval: float) -> None:
    login = _logins[login_id]
    deadline = time.time() + _DEVICE_TTL
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            while time.time() < deadline:
                resp = await client.post(
                    CHATGPT_DEVICE_TOKEN_URL,
                    json={"device_auth_id": device_id, "user_code": user_code},
                )
                if resp.status_code in (403, 404):  # still waiting for the user
                    await asyncio.sleep(interval)
                    continue
                if resp.status_code != 200:
                    raise LLMError(
                        "oauth_failed", provider="ChatGPT", detail=f"poll HTTP {resp.status_code}"
                    )
                grant = resp.json()
                tokens = await _post_token(
                    "chatgpt",
                    {
                        "grant_type": "authorization_code",
                        "client_id": CHATGPT_CLIENT_ID,
                        "code": grant["authorization_code"],
                        "code_verifier": grant["code_verifier"],
                        "redirect_uri": CHATGPT_DEVICE_REDIRECT_URI,
                    },
                )
                await _add_account("chatgpt", _chatgpt_creds(tokens))
                login["status"] = "done"
                return
        login.update(status="error", error="The code expired. Start sign-in again.")
    except Exception as exc:
        logger.warning("ChatGPT sign-in failed: %s", exc)
        login.update(status="error", error=str(exc))


async def start_anthropic_login() -> dict:
    """Begin Claude PKCE sign-in. Returns the claude.ai URL to open."""
    await _ensure_callback_server()
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    state = secrets.token_urlsafe(24)
    _logins[state] = {"status": "pending", "verifier": verifier}
    params = {
        "code": "true",
        "client_id": ANTHROPIC_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": ANTHROPIC_REDIRECT_URI,
        "scope": ANTHROPIC_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return {"login_id": state, "url": f"{ANTHROPIC_AUTHORIZE_URL}?{urlencode(params)}"}


def _parse_pasted_code(pasted: str) -> tuple[str, str | None]:
    """Accept a full redirect URL, code#state, or a bare code."""
    pasted = pasted.strip()
    if pasted.startswith("http"):
        query = parse_qs(urlparse(pasted).query)
        return query.get("code", [""])[0], query.get("state", [None])[0]
    code, _, state = pasted.partition("#")
    return code, state or None


async def complete_anthropic_login(login_id: str, pasted: str) -> None:
    code, state = _parse_pasted_code(pasted)
    state = state or login_id
    login = _logins.get(state)
    if login is None or "verifier" not in login:
        raise LLMError("oauth_failed", provider="Claude", detail="sign-in expired; start again")
    if login["status"] == "done":
        return
    if not code:
        raise LLMError("oauth_failed", provider="Claude", detail="no authorization code found")
    try:
        tokens = await _post_token(
            "anthropic",
            {
                "grant_type": "authorization_code",
                "client_id": ANTHROPIC_CLIENT_ID,
                "code": code,
                "state": state,
                "redirect_uri": ANTHROPIC_REDIRECT_URI,
                "code_verifier": login["verifier"],
            },
        )
        await _add_account("anthropic", _anthropic_creds(tokens))
        login["status"] = "done"
    except Exception as exc:
        login.update(status="error", error=str(exc))
        raise


async def _ensure_callback_server() -> None:
    """Listen on :54545 for claude.ai's redirect. If the port is busy, paste still works."""
    global _callback_server
    if _callback_server is not None:
        return
    try:
        _callback_server = await asyncio.start_server(
            _handle_callback, "0.0.0.0", ANTHROPIC_CALLBACK_PORT
        )
    except OSError as exc:
        logger.warning(
            "Claude OAuth callback port %s unavailable (%s)", ANTHROPIC_CALLBACK_PORT, exc
        )


async def _handle_callback(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request_line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1")
        target = request_line.split(" ")[1] if " " in request_line else "/"
        query = parse_qs(urlparse(target).query)
        code, state = query.get("code", [""])[0], query.get("state", [""])[0]
        try:
            await complete_anthropic_login(state, f"{code}#{state}")
            body, status = (
                "Signed in to Claude. You can close this tab and return to Mira.",
                "200 OK",
            )
        except Exception as exc:
            body, status = f"Claude sign-in failed: {exc}", "400 Bad Request"
        page = f"<!doctype html><meta charset=utf-8><title>Mira</title><p>{html.escape(body)}</p>".encode()
        writer.write(
            f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(page)}\r\nConnection: close\r\n\r\n".encode()
            + page
        )
        await writer.drain()
    except Exception as exc:  # malformed request / client went away
        logger.debug("OAuth callback error: %s", exc)
    finally:
        writer.close()
