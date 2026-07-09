"""Tests for the provider-profile registry (mira.llm.provider_profiles)."""

from __future__ import annotations

import json

from mira.llm import provider_profiles as profiles


class TestResolve:
    def test_matches_openrouter_by_base_url(self):
        p = profiles.resolve("https://openrouter.ai/api/v1")
        assert p["name"] == "openrouter"
        assert p["model_prefix"] == "keep"
        assert p["extra_headers"]["X-Title"] == "Mira Code Reviewer"
        assert p["reasoning_effort_map"] == {"max": "xhigh"}

    def test_trailing_slash_insensitive(self):
        assert profiles.resolve("https://openrouter.ai/api/v1/")["name"] == "openrouter"

    def test_unknown_url_returns_portable_default(self):
        p = profiles.resolve("https://some-new-llm.example/v1")
        assert p["name"] == ""
        assert p["model_prefix"] == "strip"
        assert p["extra_headers"] == {}
        assert p["reasoning_effort_map"] == {}

    def test_sparse_profile_fills_from_default(self, tmp_path, monkeypatch):
        # A profile with only base_url + api_key_env still resolves with every
        # field, filled in from DEFAULT_PROFILE.
        custom = tmp_path / "providers.json"
        custom.write_text(
            json.dumps({"sparse": {"base_url": "https://sparse.test/v1", "api_key_env": "K"}})
        )
        monkeypatch.setenv("MIRA_PROVIDERS_JSON_PATH", str(custom))
        profiles._load.cache_clear()
        try:
            p = profiles.resolve("https://sparse.test/v1")
            assert p["name"] == "sparse"
            assert p["model_prefix"] == "strip"
            assert p["extra_headers"] == {}
            assert p["reasoning_effort_map"] == {}
        finally:
            profiles._load.cache_clear()


class TestGet:
    def test_injects_name(self):
        assert profiles.get("openrouter")["name"] == "openrouter"

    def test_missing_returns_none(self):
        assert profiles.get("not-a-provider") is None


class TestRuntimeOverride:
    """A user can add or override a provider at runtime, no code change."""

    def test_override_file_adds_provider(self, tmp_path, monkeypatch):
        custom = tmp_path / "providers.json"
        custom.write_text(
            json.dumps(
                {
                    "acme": {
                        "base_url": "https://llm.acme.test/v1",
                        "api_key_env": "ACME_API_KEY",
                        "model_prefix": "keep",
                        "extra_headers": {"X-Acme": "1"},
                    }
                }
            )
        )
        monkeypatch.setenv("MIRA_PROVIDERS_JSON_PATH", str(custom))
        profiles._load.cache_clear()
        try:
            p = profiles.resolve("https://llm.acme.test/v1")
            assert p["name"] == "acme"
            assert p["model_prefix"] == "keep"
            assert p["extra_headers"] == {"X-Acme": "1"}
            # Bundled profiles still resolve alongside the override.
            assert profiles.resolve("https://openrouter.ai/api/v1")["name"] == "openrouter"
        finally:
            profiles._load.cache_clear()

    def test_missing_override_file_falls_back(self, monkeypatch):
        monkeypatch.setenv("MIRA_PROVIDERS_JSON_PATH", "/no/such/providers.json")
        profiles._load.cache_clear()
        try:
            assert profiles.resolve("https://openrouter.ai/api/v1")["name"] == "openrouter"
        finally:
            profiles._load.cache_clear()
