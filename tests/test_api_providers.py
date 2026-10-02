"""Routing for providers connected in Settings → Providers (`@<id>/<model>`)."""

from __future__ import annotations

import pytest

from mira.config import LLMConfig
from mira.exceptions import NonRetriableLLMError
from mira.llm import api_providers, create_llm, credential_store
from mira.llm.base import _strip_model_prefix


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.delenv("MIRA_SECRET_KEY", raising=False)
    credential_store.save(
        "api_providers.json",
        {
            "nvidia": {
                "label": "NVIDIA NIM",
                "base_url": "https://integrate.api.nvidia.com/v1",
                "api_key": "nv-key",
            },
            "openrouter": {
                "label": "OpenRouter",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "or-key",
            },
            "ollama": {"label": "Ollama", "base_url": "http://localhost:11434/v1", "api_key": ""},
        },
    )


def _sent(model: str):
    llm = create_llm(LLMConfig(model=model, api_style="chat"))
    return llm, _strip_model_prefix(llm.config.model, llm.config.base_url), llm._build_headers()


def test_vendor_slash_model_id_reaches_provider_unchanged():
    llm, sent, headers = _sent("@nvidia/meta/llama-3.3-70b-instruct")
    assert llm.config.base_url == "https://integrate.api.nvidia.com/v1"
    assert sent == "meta/llama-3.3-70b-instruct"
    assert headers["Authorization"] == "Bearer nv-key"


def test_openrouter_keeps_vendor_prefix():
    _, sent, headers = _sent("@openrouter/anthropic/claude-sonnet-4.6")
    assert sent == "anthropic/claude-sonnet-4.6"
    assert headers["Authorization"] == "Bearer or-key"


def test_keyless_local_provider_sends_no_auth():
    _, sent, headers = _sent("@ollama/qwen3:8b")
    assert sent == "qwen3:8b"
    assert "Authorization" not in headers


def test_unknown_provider_fails_without_retry():
    with pytest.raises(NonRetriableLLMError):
        create_llm(LLMConfig(model="@nope/x"))


def test_disconnected_fallback_does_not_block_primary():
    llm = create_llm(LLMConfig(model="@nvidia/x", fallback_models=["@nope/y"]))
    assert llm.config.base_url == "https://integrate.api.nvidia.com/v1"


def test_key_never_serialized():
    llm = create_llm(LLMConfig(model="@nvidia/x"))
    assert "nv-key" not in llm.config.model_dump_json() and "nv-key" not in repr(llm.config)


def test_public_hides_keys():
    assert all("api_key" not in p for p in api_providers.public())


def test_opencode_lists_only_chat_completions_models():
    ids = ["glm-5.3", "big-pickle", "space-bunny-free", "gpt-6-sol", "claude-opus-5-5"]
    ids += ["qwen3.8-max", "qwen3.6-plus", "jev-1.13-free", "muse-spark-1.3-contributor-free"]
    keep = {
        pid: [m for m in ids if api_providers._callable(pid, m)] for pid in api_providers._CHAT_ONLY
    }
    assert keep["opencode-zen"] == ["glm-5.3", "big-pickle", "space-bunny-free", "qwen3.8-max"]
    assert keep["opencode-free"] == ["big-pickle", "space-bunny-free"]
    assert keep["opencode-go"] == ["glm-5.3", "space-bunny-free"]
    assert api_providers._callable("nvidia", "gpt-6-sol")
