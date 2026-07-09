"""Tests for the platform-profile registry (mira.platforms.profiles)."""

from __future__ import annotations

import json

from mira.platforms import profiles


class TestResolve:
    def test_github(self):
        p = profiles.resolve("github")
        assert p["name"] == "github"
        assert p["api_url"] == "https://api.github.com"
        assert p["auth_model"] == "app"
        assert p["signature"]["scheme"] == "hmac-sha256"
        assert p["supports"]["installations"] is True

    def test_gitlab(self):
        p = profiles.resolve("gitlab")
        assert p["name"] == "gitlab"
        assert p["api_url"] == "https://gitlab.com/api/v4"
        assert p["auth_model"] == "token"
        assert p["signature"]["scheme"] == "shared-token"
        assert p["signature"]["header"] == "X-Gitlab-Token"
        assert p["supports"]["installations"] is False
        assert p["terminology"]["pr_short"] == "MR"

    def test_unknown_name_returns_default_with_name(self):
        p = profiles.resolve("bitbucket")
        assert p["name"] == "bitbucket"
        assert p["api_url"] == ""
        assert p["signature"] == {}


class TestGet:
    def test_injects_name(self):
        assert profiles.get("gitlab")["name"] == "gitlab"

    def test_missing_returns_none(self):
        assert profiles.get("bitbucket") is None


class TestEnvSeeding:
    def test_github_enterprise_url_flows_in(self, monkeypatch):
        monkeypatch.setenv("MIRA_GITHUB_API_URL", "https://gh.acme.test/api/v3")
        profiles._load.cache_clear()
        try:
            p = profiles.resolve("github")
            assert p["api_url"] == "https://gh.acme.test/api/v3"
            assert p["graphql_url"] == "https://gh.acme.test/api/v3/graphql"
        finally:
            profiles._load.cache_clear()

    def test_self_managed_gitlab_url_flows_in(self, monkeypatch):
        monkeypatch.setenv("MIRA_GITLAB_API_URL", "https://gitlab.acme.test/api/v4")
        profiles._load.cache_clear()
        try:
            assert profiles.resolve("gitlab")["api_url"] == "https://gitlab.acme.test/api/v4"
        finally:
            profiles._load.cache_clear()


class TestRuntimeOverride:
    def test_override_file_adds_platform(self, tmp_path, monkeypatch):
        custom = tmp_path / "platforms.json"
        custom.write_text(
            json.dumps(
                {
                    "gitlab_self": {
                        "api_url": "https://git.acme.test/api/v4",
                        "webhook_route": "/gitlab/webhook",
                        "auth_model": "token",
                        "signature": {"scheme": "shared-token", "header": "X-Gitlab-Token"},
                    }
                }
            )
        )
        monkeypatch.setenv("MIRA_PLATFORMS_JSON_PATH", str(custom))
        profiles._load.cache_clear()
        try:
            p = profiles.resolve("gitlab_self")
            assert p["api_url"] == "https://git.acme.test/api/v4"
            assert p["auth_model"] == "token"
            # bundled platforms still resolve alongside the override
            assert profiles.resolve("github")["name"] == "github"
        finally:
            profiles._load.cache_clear()
