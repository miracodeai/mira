"""Dynamic model catalog — lists models from the configured backend.

The static registry (llm/models.json) uses OpenRouter-style ids, so a
deployment on Bedrock or a generic OpenAI-compatible endpoint would be
offered ids its backend can't serve. Detect the active backend, fetch its
live model list (cached for an hour; failures for a minute), and fall back
to the backend-filtered registry when the fetch fails.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable

import httpx

from mira.config import LLMConfig
from mira.llm import provider_profiles as profiles
from mira.llm import registry
from mira.llm.base import _get_api_key

logger = logging.getLogger(__name__)

_CATALOG_TTL = 3600.0
_FAILURE_TTL = 60.0
_cache: dict[str, tuple[float, list[dict] | None]] = {}
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def active_backend(config: LLMConfig) -> str:
    """Return the model-catalog backend for the deployment."""
    if config.provider == "bedrock":
        return "bedrock"
    if config.provider in {"codex-cli", "codex_cli", "codex"}:
        return "codex-cli"
    profile = profiles.resolve(config.base_url)
    return "openrouter" if profile.get("name") == "openrouter" else "openai-compatible"


def missing_api_key(config: LLMConfig) -> str:
    """Env var name when the API endpoint needs a key that isn't set, else ""."""
    if active_backend(config) in {"bedrock", "codex-cli"}:
        return ""
    try:
        _get_api_key(config, profiles.resolve(config.base_url))
    except Exception:
        return config.api_key_env
    return ""


def _norm(model_id: str) -> str:
    # OpenRouter serves dash and dot forms of the same id as aliases
    # (anthropic/claude-haiku-4-5 == anthropic/claude-haiku-4.5).
    return model_id.lower().replace(".", "-")


async def _fetch_openai_style(config: LLMConfig, tools_only: bool) -> list[dict]:
    """GET {base_url}/models. With tools_only (OpenRouter), keep only
    tool-calling models — Mira's review pass needs tool calling."""
    headers = {}
    try:
        key = _get_api_key(config)
    except Exception as exc:
        logger.warning("Could not retrieve API key for model catalog fetch: %s", exc)
        key = ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{config.base_url.rstrip('/')}/models", headers=headers)
    resp.raise_for_status()
    out = []
    effort_map = profiles.resolve(config.base_url).get("reasoning_effort_map", {})
    for m in resp.json().get("data", []):
        if tools_only and "tools" not in (m.get("supported_parameters") or []):
            continue
        out.append(
            {
                "value": m["id"],
                "label": m.get("name") or m["id"],
                "reasoning_levels": registry.reasoning_levels(m, effort_map),
            }
        )
    return out


def _fetch_bedrock_sync(config: LLMConfig) -> list[dict]:
    import boto3
    from botocore.config import Config as BotoConfig

    session = boto3.Session(profile_name=config.aws_profile, region_name=config.region)
    client = session.client(
        "bedrock",
        config=BotoConfig(connect_timeout=5, read_timeout=15, retries={"max_attempts": 1}),
    )
    out = []
    for p in client.list_inference_profiles().get("inferenceProfileSummaries", []):
        out.append(
            {
                "value": p["inferenceProfileId"],
                "label": p.get("inferenceProfileName") or p["inferenceProfileId"],
            }
        )
    models = client.list_foundation_models(byOutputModality="TEXT", byInferenceType="ON_DEMAND")
    for m in models.get("modelSummaries", []):
        out.append({"value": m["modelId"], "label": m.get("modelName") or m["modelId"]})
    return out


async def fetch_catalog(config: LLMConfig) -> list[dict] | None:
    """Live ``[{value, label}]`` list for the active backend, or None if
    unavailable (no network, no boto3, no credentials, ...).

    Successes are cached for an hour, failures for a minute — the settings
    page must not re-block on a dead endpoint on every load. A per-key lock
    coalesces concurrent cold-cache fetches (two tabs, the setup modal poll).
    """
    backend = active_backend(config)
    if backend == "bedrock":
        cache_key = f"bedrock:{config.region}:{config.aws_profile or ''}"
    elif backend == "codex-cli":
        return None
    else:
        cache_key = config.base_url

    async def fetch() -> list[dict]:
        if backend == "bedrock":
            return await asyncio.to_thread(_fetch_bedrock_sync, config)
        return await _fetch_openai_style(config, tools_only=backend == "openrouter")

    return await _cached(cache_key, fetch, backend)


def _fresh(cache_key: str) -> tuple[float, list[dict] | None] | None:
    hit = _cache.get(cache_key)
    if hit is None:
        return None
    ttl = _CATALOG_TTL if hit[1] is not None else _FAILURE_TTL
    return hit if time.time() - hit[0] < ttl else None


async def _cached(
    cache_key: str, fetch: Callable[[], Awaitable[list[dict]]], name: str
) -> list[dict] | None:
    """Run fetch through the TTL cache; the per-key lock coalesces concurrent cold fetches."""
    if (hit := _fresh(cache_key)) is not None:
        return hit[1]
    async with _locks[cache_key]:
        if (hit := _fresh(cache_key)) is not None:
            return hit[1]
        try:
            models: list[dict] | None = await fetch()
        except Exception as exc:
            logger.warning("%s model list failed: %s", name, exc)
            models = None
        _cache[cache_key] = (time.time(), models)
        return models


def build_options(backend: str, dynamic: list[dict] | None, purpose: str) -> list[dict]:
    """Dropdown options for ``purpose``: registry entries matching the backend
    (carrying the recommended flags) merged with the dynamic catalog.

    Dynamic-only models have unknown capabilities, so they're offered for both
    purposes. On a generic endpoint only its own list is trustworthy — registry
    ids are OpenRouter-style — so the registry is used there only as fallback.
    """
    if backend == "openai-compatible" and dynamic is not None:
        options = [{**d, "recommended": False} for d in dynamic]
        options.sort(key=lambda m: m["label"].lower())
        return options

    options = []
    for model_id, info in registry.all_models().items():
        provider = info.get("provider")
        if backend == "bedrock" and provider != "bedrock":
            continue
        if backend == "codex-cli" and provider != "codex-cli":
            continue
        if backend not in {"bedrock", "codex-cli"} and provider in {"bedrock", "codex-cli"}:
            continue
        if purpose not in (info.get("purposes") or []):
            continue
        options.append(
            {
                "value": model_id,
                "label": info.get("label", model_id),
                "recommended": purpose in (info.get("recommended_for") or []),
                "reasoning_levels": info.get("reasoning_levels"),
            }
        )
    if dynamic is not None:
        by_id = {_norm(d["value"]): d for d in dynamic}
        for option in options:
            live = by_id.get(_norm(option["value"]), {})
            if live.get("reasoning_levels") is not None:
                option["reasoning_levels"] = live["reasoning_levels"]
        seen = {_norm(o["value"]) for o in options}
        options += [{**d, "recommended": False} for d in dynamic if _norm(d["value"]) not in seen]
    options.sort(key=lambda m: (not m["recommended"], m["label"].lower()))
    if backend == "bedrock":
        for option in options:
            if "anthropic." in option["value"]:
                option["reasoning_levels"] = ["off", "low", "medium", "high", "max"]
    return options


_SUBSCRIPTIONS = {"chatgpt": ("chatgpt/", "ChatGPT"), "anthropic": ("claude/", "Claude")}


async def subscription_options() -> list[dict]:
    """Models from every signed-in subscription account and connected API/local provider,
    prefixed so create_llm routes them."""
    from mira.llm import api_providers, oauth_accounts

    sources = [
        (account, prefix, name, lambda a=account: oauth_accounts.list_models(a))
        for account, (prefix, name) in _SUBSCRIPTIONS.items()
        if oauth_accounts.has_account(account)
    ]
    for p in api_providers.public():
        sources.append(
            (
                f"api:{p['id']}",
                f"@{p['id']}/",
                p["label"],
                lambda pid=p["id"]: api_providers.list_models(api_providers.get(pid) or {}, pid),
            )
        )
    results = await asyncio.gather(*(_cached(key, fetch, name) for key, _, name, fetch in sources))
    return [
        {**m, "value": prefix + m["value"], "label": f"{m['label']} ({name})", "recommended": False}
        for (_, prefix, name, _), models in zip(sources, results, strict=True)
        for m in models or []
    ]
