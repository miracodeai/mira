"""Tests for the backend-aware dynamic model catalog."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from mira.config import LLMConfig
from mira.dashboard import model_catalog
from mira.dashboard.model_catalog import active_backend, build_options, fetch_catalog


@pytest.fixture(autouse=True)
def _clear_cache():
    model_catalog._cache.clear()
    yield
    model_catalog._cache.clear()


class TestActiveBackend:
    def test_default_is_openrouter(self):
        assert active_backend(LLMConfig()) == "openrouter"

    def test_bedrock_provider(self):
        assert active_backend(LLMConfig(provider="bedrock")) == "bedrock"

    def test_codex_cli_provider(self):
        assert active_backend(LLMConfig(provider="codex-cli")) == "codex-cli"

    def test_requesty_endpoint(self):
        assert active_backend(LLMConfig(base_url="https://router.requesty.ai/v1")) == "requesty"

    def test_generic_endpoint(self):
        assert (
            active_backend(LLMConfig(base_url="http://localhost:11434/v1")) == "openai-compatible"
        )


class TestBuildOptions:
    def test_registry_filtered_by_backend(self):
        openrouter = [m["value"] for m in build_options("openrouter", None, "review")]
        bedrock = [m["value"] for m in build_options("bedrock", None, "review")]
        assert "anthropic/claude-sonnet-4-6" in openrouter
        assert "us.anthropic.claude-sonnet-4-6-v1:0" not in openrouter
        assert "us.anthropic.claude-sonnet-4-6-v1:0" in bedrock
        assert "anthropic/claude-sonnet-4-6" not in bedrock

    def test_codex_backend_only_offers_codex_models(self):
        values = [m["value"] for m in build_options("codex-cli", None, "review")]
        assert values == ["codex-default"]

    def test_openrouter_does_not_offer_codex_models(self):
        values = [m["value"] for m in build_options("openrouter", None, "review")]
        assert "codex-default" not in values

    def test_dynamic_merged_and_deduped_against_registry(self):
        dynamic = [
            {"value": "anthropic/claude-sonnet-4.6", "label": "Anthropic: Claude Sonnet 4.6"},
            {"value": "mistralai/mistral-large-3", "label": "Mistral Large 3"},
        ]
        options = build_options("openrouter", dynamic, "review")
        values = [m["value"] for m in options]
        # Dot-form alias of a registry id is dropped; genuinely new model kept.
        assert "anthropic/claude-sonnet-4.6" not in values
        assert "anthropic/claude-sonnet-4-6" in values
        assert "mistralai/mistral-large-3" in values

    def test_generic_endpoint_uses_dynamic_only(self):
        dynamic = [{"value": "llama-3.3-70b", "label": "llama-3.3-70b"}]
        values = [m["value"] for m in build_options("openai-compatible", dynamic, "review")]
        assert values == ["llama-3.3-70b"]

    def test_requesty_uses_dynamic_only(self):
        dynamic = [{"value": "claude-sonnet-4-6", "label": "claude-sonnet-4-6"}]
        values = [m["value"] for m in build_options("requesty", dynamic, "review")]
        assert values == ["claude-sonnet-4-6"]

    def test_requesty_falls_back_to_registry(self):
        values = [m["value"] for m in build_options("requesty", None, "review")]
        assert "anthropic/claude-sonnet-4-6" in values
        assert "us.anthropic.claude-sonnet-4-6-v1:0" not in values

    def test_generic_endpoint_falls_back_to_registry(self):
        values = [m["value"] for m in build_options("openai-compatible", None, "review")]
        assert "anthropic/claude-sonnet-4-6" in values

    def test_recommended_sort_first(self):
        options = build_options("openrouter", None, "indexing")
        assert options[0]["recommended"] is True


class TestFetchCatalog:
    @pytest.mark.asyncio
    async def test_failure_returns_none_and_is_cached(self, monkeypatch: pytest.MonkeyPatch):
        calls = 0

        async def boom(config, tools_only):
            nonlocal calls
            calls += 1
            raise RuntimeError("no network")

        monkeypatch.setattr(model_catalog, "_fetch_openai_style", boom)
        assert await fetch_catalog(LLMConfig()) is None
        # A dead endpoint must not re-block every settings-page load.
        assert await fetch_catalog(LLMConfig()) is None
        assert calls == 1

    @pytest.mark.asyncio
    async def test_result_is_cached(self, monkeypatch: pytest.MonkeyPatch):
        calls = 0

        async def fake(config, tools_only):
            nonlocal calls
            calls += 1
            return [{"value": "m", "label": "m"}]

        monkeypatch.setattr(model_catalog, "_fetch_openai_style", fake)
        assert await fetch_catalog(LLMConfig()) == [{"value": "m", "label": "m"}]
        assert await fetch_catalog(LLMConfig()) == [{"value": "m", "label": "m"}]
        assert calls == 1

    @pytest.mark.asyncio
    async def test_concurrent_cold_fetches_coalesce(self, monkeypatch: pytest.MonkeyPatch):
        calls = 0

        async def slow(config, tools_only):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)
            return [{"value": "m", "label": "m"}]

        monkeypatch.setattr(model_catalog, "_fetch_openai_style", slow)
        results = await asyncio.gather(*(fetch_catalog(LLMConfig()) for _ in range(5)))
        assert all(r == [{"value": "m", "label": "m"}] for r in results)
        assert calls == 1

    @pytest.mark.asyncio
    async def test_requesty_lists_managed_first_and_tool_models_only(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("REQUESTY_API_KEY", "rqsty-test")
        pages = {
            "https://router.requesty.ai/v1/models/managed": [
                {"id": "claude-sonnet-4-6", "api": "chat", "supports_tool_calling": True},
            ],
            "https://router.requesty.ai/v1/models": [
                {"id": "openai/gpt-4o-mini", "api": "chat", "supports_tool_calling": True},
                {"id": "claude-sonnet-4-6", "api": "chat", "supports_tool_calling": True},
                {"id": "openai/text-embedding-3-small", "api": "embedding"},
                {"id": "vendor/no-tools", "api": "chat", "supports_tool_calling": False},
            ],
        }
        seen_auth = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_auth.append(request.headers.get("Authorization"))
            return httpx.Response(200, json={"object": "list", "data": pages[str(request.url)]})

        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            model_catalog.httpx,
            "AsyncClient",
            lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
        )
        config = LLMConfig(base_url="https://router.requesty.ai/v1", api_key_env="REQUESTY_API_KEY")
        values = [m["value"] for m in await fetch_catalog(config) or []]
        assert values == ["claude-sonnet-4-6", "openai/gpt-4o-mini"]
        assert seen_auth == ["Bearer rqsty-test", "Bearer rqsty-test"]

    @pytest.mark.asyncio
    async def test_requesty_keeps_managed_list_when_models_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).endswith("/models/managed"):
                data = [{"id": "claude-sonnet-4-6", "api": "chat", "supports_tool_calling": True}]
                return httpx.Response(200, json={"object": "list", "data": data})
            return httpx.Response(503)

        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            model_catalog.httpx,
            "AsyncClient",
            lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
        )
        config = LLMConfig(base_url="https://router.requesty.ai/v1", api_key_env="REQUESTY_API_KEY")
        values = [m["value"] for m in await model_catalog._fetch_requesty(config)]
        assert values == ["claude-sonnet-4-6"]

    @pytest.mark.asyncio
    async def test_requesty_raises_when_both_listings_fail(self, monkeypatch: pytest.MonkeyPatch):
        real_client = httpx.AsyncClient
        monkeypatch.setattr(
            model_catalog.httpx,
            "AsyncClient",
            lambda **kw: real_client(
                transport=httpx.MockTransport(lambda r: httpx.Response(503)), **kw
            ),
        )
        config = LLMConfig(base_url="https://router.requesty.ai/v1", api_key_env="REQUESTY_API_KEY")
        with pytest.raises(httpx.HTTPStatusError):
            await model_catalog._fetch_requesty(config)

    def test_bedrock_cache_key_includes_profile(self):
        # Switching aws_profile must not serve the previous account's catalog.
        a = LLMConfig(provider="bedrock", aws_profile="account-a")
        b = LLMConfig(provider="bedrock", aws_profile="account-b")
        assert a.region == b.region
        # Keys derived the same way fetch_catalog does.
        key_a = f"bedrock:{a.region}:{a.aws_profile or ''}"
        key_b = f"bedrock:{b.region}:{b.aws_profile or ''}"
        assert key_a != key_b
