"""Tests for the model registry, incl. the runtime MIRA_MODELS_JSON_PATH override."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mira.llm import registry

_CUSTOM = {
    "deepseek/deepseek-chat": {
        "label": "DeepSeek Chat",
        "provider": "openai",
        "max_input_tokens": 64000,
        "max_output_tokens": 8000,
        "input_cost_per_1m": 0.27,
        "output_cost_per_1m": 1.10,
        "supports_json_mode": True,
        "purposes": ["indexing", "review"],
        "recommended_for": [],
    },
    "_comment": "doc key — must be dropped",
}


@pytest.fixture(autouse=True)
def _clear_registry_cache():
    # The registry caches per-process; clear around each test so env changes
    # take effect and don't leak.
    registry._load.cache_clear()
    yield
    registry._load.cache_clear()


def test_bundled_models_load() -> None:
    models = registry.all_models()
    assert models  # non-empty
    assert all(not k.startswith("_") for k in models)  # doc keys dropped


def test_override_adds_and_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundled = set(registry.all_models())
    existing = next(iter(bundled))  # an arbitrary bundled model to override

    custom = dict(_CUSTOM)
    custom[existing] = {
        "label": "OVERRIDDEN",
        "provider": "x",
        "max_input_tokens": 1,
        "max_output_tokens": 1,
        "input_cost_per_1m": 0,
        "output_cost_per_1m": 0,
        "supports_json_mode": False,
        "purposes": ["review"],
    }
    f = tmp_path / "models.json"
    f.write_text(json.dumps(custom))
    monkeypatch.setenv("MIRA_MODELS_JSON_PATH", str(f))
    # Required (not redundant with the fixture): all_models() was already called
    # above to read `bundled`, which populated the lru_cache before the env var
    # was set. Clear it so the call below re-reads with the override in place.
    registry._load.cache_clear()

    models = registry.all_models()
    # New model is added, supported, and shows up in the dropdown.
    assert "deepseek/deepseek-chat" in models
    assert registry.is_supported("deepseek/deepseek-chat", purpose="review")
    assert any(
        m["value"] == "deepseek/deepseek-chat" for m in registry.models_for_purpose("review")
    )
    # Bundled defaults are preserved (merge, not replace).
    assert bundled <= set(models)
    # The overlay replaced the existing entry, and doc keys are dropped.
    assert models[existing]["label"] == "OVERRIDDEN"
    assert "_comment" not in models


def test_custom_entry_without_cost_fields_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A minimal custom entry (no cost fields) must not break pricing/registry load.
    f = tmp_path / "models.json"
    f.write_text(json.dumps({"local/llama": {"label": "Local", "purposes": ["review"]}}))
    monkeypatch.setenv("MIRA_MODELS_JSON_PATH", str(f))

    assert registry.is_supported("local/llama", purpose="review")
    # Falls back to default pricing rather than raising KeyError.
    assert registry.pricing("local/llama") == (3.00, 15.00)


def test_missing_override_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MIRA_MODELS_JSON_PATH", str(tmp_path / "nope.json"))
    # No crash; bundled models still load.
    assert registry.all_models()


def test_invalid_override_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tmp_path / "models.json"
    bad.write_text("{ not valid json ")
    monkeypatch.setenv("MIRA_MODELS_JSON_PATH", str(bad))
    assert registry.all_models()
