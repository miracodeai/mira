"""API-key and local providers (OpenAI-compatible) added from the dashboard catalog.

A connected provider's models are offered as ``@<id>/<model>`` and run through the
standard chat-completions client with that provider's base URL and key. Presets mirror
OpenCodex's provider catalog; anything else can be added as a custom provider.
"""

from __future__ import annotations

import re

import httpx

from mira.llm import credential_store

_STORE = "api_providers.json"
_LOCAL = "Use localhost instead of host.docker.internal when Mira runs outside Docker."


def _p(
    pid: str,
    label: str,
    tier: str,
    base_url: str,
    dashboard: str = "",
    note: str = "",
    key: str = "required",
) -> dict:
    return {
        "id": pid,
        "label": label,
        "tier": tier,
        "base_url": base_url,
        "dashboard": dashboard,
        "note": note,
        "key": key,
    }


# tier: "free" (free tier or free models) | "paid" | "local"; key: "required" | "optional" | "none"
PRESETS: list[dict] = [
    _p(
        "nvidia",
        "NVIDIA NIM",
        "free",
        "https://integrate.api.nvidia.com/v1",
        "https://build.nvidia.com",
        "Free tier; API key still required (get one at build.nvidia.com).",
    ),
    _p(
        "groq",
        "Groq",
        "free",
        "https://api.groq.com/openai/v1",
        "https://console.groq.com/keys",
        "Free tier with rate limits.",
    ),
    _p(
        "cerebras",
        "Cerebras",
        "free",
        "https://api.cerebras.ai/v1",
        "https://cloud.cerebras.ai/platform/apikeys",
        "Free tier with daily limits.",
    ),
    _p(
        "gemini",
        "Google Gemini (AI Studio)",
        "free",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "https://aistudio.google.com/apikey",
        "Free tier on AI Studio keys.",
    ),
    _p(
        "mistral",
        "Mistral",
        "free",
        "https://api.mistral.ai/v1",
        "https://console.mistral.ai/api-keys",
        "Free experiment plan.",
    ),
    _p(
        "openrouter",
        "OpenRouter",
        "free",
        "https://openrouter.ai/api/v1",
        "https://openrouter.ai/keys",
        "Hundreds of models; ids ending in :free cost nothing.",
    ),
    _p(
        "sambanova",
        "SambaNova Cloud",
        "free",
        "https://api.sambanova.ai/v1",
        "https://cloud.sambanova.ai/apis",
        "Free tier with rate limits.",
    ),
    _p(
        "huggingface",
        "Hugging Face",
        "free",
        "https://router.huggingface.co/v1",
        "https://huggingface.co/settings/tokens",
        "Monthly free credits.",
    ),
    _p(
        "cloudflare",
        "Cloudflare Workers AI",
        "free",
        "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
        "https://dash.cloudflare.com/?to=/:account/ai/workers-ai",
        "Free daily allowance. Put your account ID in the base URL.",
    ),
    _p(
        "ollama-cloud",
        "Ollama Cloud",
        "free",
        "https://ollama.com/v1",
        "https://ollama.com/settings/keys",
        "Free tier with hourly and weekly limits.",
    ),
    _p(
        "cohere",
        "Cohere",
        "free",
        "https://api.cohere.com/compatibility/v1",
        "https://dashboard.cohere.com/api-keys",
        "Free trial keys.",
    ),
    _p(
        "opencode-free",
        "OpenCode Zen (free models)",
        "free",
        "https://opencode.ai/zen/v1",
        "https://opencode.ai/auth",
        "Zen's free models; a free Zen API key is still required.",
    ),
    _p(
        "openai",
        "OpenAI API",
        "paid",
        "https://api.openai.com/v1",
        "https://platform.openai.com/api-keys",
    ),
    _p(
        "anthropic",
        "Anthropic API",
        "paid",
        "https://api.anthropic.com/v1",
        "https://console.anthropic.com/settings/keys",
    ),
    _p("xai", "xAI Grok", "paid", "https://api.x.ai/v1", "https://console.x.ai"),
    _p(
        "deepseek",
        "DeepSeek",
        "paid",
        "https://api.deepseek.com",
        "https://platform.deepseek.com/api_keys",
    ),
    _p(
        "moonshot",
        "Moonshot (Kimi)",
        "paid",
        "https://api.moonshot.ai/v1",
        "https://platform.moonshot.ai/console/api-keys",
    ),
    _p(
        "zai",
        "Z.ai (GLM)",
        "paid",
        "https://api.z.ai/api/paas/v4",
        "https://z.ai/manage-apikey/apikey-list",
    ),
    _p(
        "together",
        "Together AI",
        "paid",
        "https://api.together.xyz/v1",
        "https://api.together.ai/settings/api-keys",
    ),
    _p(
        "fireworks",
        "Fireworks AI",
        "paid",
        "https://api.fireworks.ai/inference/v1",
        "https://fireworks.ai/account/api-keys",
    ),
    _p(
        "deepinfra",
        "DeepInfra",
        "paid",
        "https://api.deepinfra.com/v1/openai",
        "https://deepinfra.com/dash/api_keys",
    ),
    _p(
        "novita",
        "Novita AI",
        "paid",
        "https://api.novita.ai/openai/v1",
        "https://novita.ai/settings/key-management",
    ),
    _p(
        "hyperbolic",
        "Hyperbolic",
        "paid",
        "https://api.hyperbolic.xyz/v1",
        "https://app.hyperbolic.xyz/settings",
    ),
    _p(
        "nebius",
        "Nebius Token Factory",
        "paid",
        "https://api.tokenfactory.nebius.com/v1",
        "https://tokenfactory.nebius.com",
    ),
    _p(
        "baseten",
        "Baseten Model APIs",
        "paid",
        "https://inference.baseten.co/v1",
        "https://app.baseten.co/settings/api_keys",
    ),
    _p(
        "opencode-zen",
        "OpenCode Zen",
        "paid",
        "https://opencode.ai/zen/v1",
        "https://opencode.ai/auth",
        "Pay as you go. Only chat-completions models are listed.",
    ),
    _p(
        "opencode-go",
        "OpenCode Go",
        "paid",
        "https://opencode.ai/zen/go/v1",
        "https://opencode.ai/auth",
        "Subscription. Only chat-completions models are listed.",
    ),
    _p(
        "ollama", "Ollama", "local", "http://host.docker.internal:11434/v1", note=_LOCAL, key="none"
    ),
    _p(
        "lm-studio",
        "LM Studio",
        "local",
        "http://host.docker.internal:1234/v1",
        note=_LOCAL,
        key="none",
    ),
    _p(
        "llama-cpp",
        "llama.cpp server",
        "local",
        "http://host.docker.internal:8080/v1",
        note=_LOCAL,
        key="none",
    ),
    _p("vllm", "vLLM", "local", "http://host.docker.internal:8000/v1", note=_LOCAL, key="optional"),
    _p(
        "litellm",
        "LiteLLM proxy",
        "local",
        "http://host.docker.internal:4000/v1",
        note=_LOCAL,
        key="optional",
    ),
]

# OpenCode serves some families only on /responses or /messages; Mira calls /chat/completions.
# ponytail: family prefixes from opencode.ai/docs (zen, go); update when they add families.
_ZEN_CHAT = ("deepseek-", "glm-", "kimi-", "minimax-", "qwen3.8-max", "mimo-", "longcat-")
_ZEN_CHAT += ("ling-", "nemotron-", "space-bunny", "big-pickle")
_CHAT_ONLY = {
    "opencode-zen": _ZEN_CHAT,
    "opencode-free": _ZEN_CHAT,
    "opencode-go": ("deepseek-", "glm-", "kimi-", "mimo-", "longcat-", "hy", "space-bunny"),
}


def _callable(pid: str, model: str) -> bool:
    prefixes = _CHAT_ONLY.get(pid)
    if prefixes is None:
        return True
    if pid == "opencode-free" and not (model.endswith("-free") or model == "big-pickle"):
        return False
    return model.startswith(prefixes)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]


def _load() -> dict[str, dict]:
    return credential_store.read(_STORE)


def get(provider_id: str) -> dict | None:
    return _load().get(provider_id)


def public() -> list[dict]:
    """Connected providers, safe for the browser (keys reduced to a flag)."""
    return [
        {
            "id": pid,
            "label": p["label"],
            "base_url": p["base_url"],
            "has_key": bool(p.get("api_key")),
        }
        for pid, p in _load().items()
    ]


async def list_models(provider: dict, pid: str = "") -> list[dict]:
    """GET {base_url}/models -> [{value, label}]. Raises on HTTP or shape errors."""
    key = provider.get("api_key")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{provider['base_url'].rstrip('/')}/models", headers=headers)
    resp.raise_for_status()
    body = resp.json()
    # Most return {"data": [...]}; Together returns a bare list.
    rows = body if isinstance(body, list) else body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise ValueError("expected a model list or an object with a data list")
    from mira.llm import provider_profiles, registry

    effort_map = provider_profiles.resolve(provider["base_url"]).get("reasoning_effort_map", {})
    out = [
        {
            "value": r["id"],
            "label": r.get("name") or r.get("display_name") or r["id"],
            "reasoning_levels": registry.reasoning_levels(r, effort_map),
        }
        for r in rows
        if isinstance(r, dict) and r.get("id") and _callable(pid, r["id"])
    ]
    return sorted(out, key=lambda m: m["label"].lower())


async def connect(provider_id: str, label: str, base_url: str, api_key: str) -> int:
    """Validate by listing models, then save. Returns the model count."""
    provider = {
        "label": label,
        "base_url": base_url.strip().rstrip("/"),
        "api_key": api_key.strip(),
    }
    count = len(await list_models(provider, provider_id))
    async with credential_store.lock:
        data = _load()
        data[provider_id] = provider
        credential_store.save(_STORE, data)
    return count


async def disconnect(provider_id: str) -> None:
    async with credential_store.lock:
        data = _load()
        data.pop(provider_id, None)
        credential_store.save(_STORE, data)
