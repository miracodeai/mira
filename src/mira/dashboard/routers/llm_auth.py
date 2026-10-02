"""Dashboard routes for subscription accounts (ChatGPT / Claude OAuth) and API-key providers."""

from __future__ import annotations

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel, ValidationError

from mira.dashboard.api import _require_admin, router
from mira.exceptions import LLMError
from mira.llm import api_providers, oauth_accounts


class AnthropicCode(BaseModel):
    login_id: str
    code: str


class ConnectProvider(BaseModel):
    preset_id: str = ""
    label: str = ""
    base_url: str
    api_key: str = ""


def _account_key(provider: str) -> str:
    if provider not in oauth_accounts.PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown provider")
    return provider


def _forget_catalog() -> None:
    from mira.dashboard import model_catalog

    for account in oauth_accounts.PROVIDERS:
        model_catalog._cache.pop(account, None)


@router.get("/api/llm-accounts")
def get_accounts(request: Request) -> dict:
    _require_admin(request)
    return {"accounts": oauth_accounts.public_accounts()}


@router.get("/api/llm-accounts/usage")
async def get_usage(request: Request) -> dict:
    """5-hour / weekly subscription usage per account, read live from each provider."""
    _require_admin(request)
    return {"usage": await oauth_accounts.usage()}


@router.post("/api/llm-accounts/{provider}/login")
async def start_login(provider: str, request: Request) -> dict:
    _require_admin(request)
    try:
        if _account_key(provider) == "chatgpt":
            return await oauth_accounts.start_chatgpt_login()
        return await oauth_accounts.start_anthropic_login()
    except (LLMError, httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/api/llm-accounts/logins/{login_id}")
def login_status(login_id: str, request: Request) -> dict:
    _require_admin(request)
    status = oauth_accounts.login_status(login_id)
    if status["status"] == "done":
        _forget_catalog()
    return status


@router.post("/api/llm-accounts/anthropic/complete")
async def complete_anthropic(body: AnthropicCode, request: Request) -> dict:
    """Finish Claude sign-in with a pasted redirect URL or code (when the callback can't reach Mira)."""
    _require_admin(request)
    try:
        await oauth_accounts.complete_anthropic_login(body.login_id, body.code)
    except LLMError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Claude sign-in failed: {exc}") from exc
    _forget_catalog()
    return {"ok": True}


@router.post("/api/llm-accounts/{provider}/{account_id}/activate")
async def activate_account(provider: str, account_id: str, request: Request) -> dict:
    _require_admin(request)
    try:
        await oauth_accounts.activate(_account_key(provider), account_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown account") from exc
    _forget_catalog()
    return {"ok": True}


@router.delete("/api/llm-accounts/{provider}/{account_id}")
async def remove_account(provider: str, account_id: str, request: Request) -> dict:
    _require_admin(request)
    await oauth_accounts.remove(_account_key(provider), account_id)
    _forget_catalog()
    return {"ok": True}


@router.delete("/api/llm-accounts/{provider}")
async def log_out(provider: str, request: Request) -> dict:
    _require_admin(request)
    await oauth_accounts.remove(_account_key(provider))
    _forget_catalog()
    return {"ok": True}


@router.get("/api/llm-providers")
def get_api_providers(request: Request) -> dict:
    _require_admin(request)
    return {"presets": api_providers.PRESETS, "connected": api_providers.public()}


@router.post("/api/llm-providers")
async def connect_api_provider(body: ConnectProvider, request: Request) -> dict:
    """Connect a preset or custom OpenAI-compatible provider after checking its /models."""
    _require_admin(request)
    from mira.config import LLMConfig
    from mira.dashboard import model_catalog

    preset = next((p for p in api_providers.PRESETS if p["id"] == body.preset_id), None)
    label = (preset or {}).get("label") or body.label.strip()
    pid = preset["id"] if preset else api_providers.slug(label)
    if not pid:
        raise HTTPException(status_code=400, detail="Name is required")
    if "{" in body.base_url:
        raise HTTPException(status_code=400, detail="Fill in the placeholder in the base URL")
    try:
        LLMConfig(base_url=body.base_url.strip())
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors()[0]["msg"]) from exc
    try:
        count = await api_providers.connect(pid, label, body.base_url, body.api_key)
    except httpx.HTTPStatusError as exc:
        detail = f"{body.base_url.rstrip('/')}/models returned HTTP {exc.response.status_code}"
        raise HTTPException(status_code=400, detail=detail) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not list models: {exc}") from exc
    model_catalog._cache.pop(f"api:{pid}", None)
    return {"ok": True, "id": pid, "models": count}


@router.delete("/api/llm-providers/{provider_id}")
async def disconnect_api_provider(provider_id: str, request: Request) -> dict:
    _require_admin(request)
    from mira.dashboard import model_catalog

    await api_providers.disconnect(provider_id)
    model_catalog._cache.pop(f"api:{provider_id}", None)
    return {"ok": True}
