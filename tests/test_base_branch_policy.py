"""Auto-review destination-branch policy (shared across platforms)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from mira.config import FilterConfig, MiraConfig
from mira.platforms.base_branch_policy import (
    MODE_DISABLE,
    MODE_INHERIT,
    MODE_OVERRIDE,
    base_branch_is_filtered,
    parse_repo_policy,
    resolve_patterns,
    validate_patterns,
)


def _cfg(
    default: list[str] | None = None, overrides: dict[str, list[str]] | None = None
) -> MiraConfig:
    return MiraConfig(
        filter=FilterConfig(
            allowed_base_branches=default or [],
            allowed_base_branch_overrides=overrides or {},
        )
    )


class TestUnconfigured:
    def test_no_policy_reviews_everything(self):
        cfg = MiraConfig()
        assert base_branch_is_filtered("o/r", "main", cfg) is False
        assert base_branch_is_filtered("o/r", "anything", cfg) is False
        assert base_branch_is_filtered("o/r", "", cfg) is False

    def test_empty_lists_mean_unrestricted(self):
        cfg = _cfg(default=[])
        assert base_branch_is_filtered("o/r", "dev", cfg) is False


class TestDefaultPatterns:
    @pytest.mark.parametrize("source", ["global", "repo", "stored"])
    def test_padded_patterns_are_normalized(self, source):
        patterns = [" main", "release/*\t "]
        cfg = _cfg(
            default=patterns if source == "global" else [],
            overrides={"o/r": patterns} if source == "repo" else {},
        )
        raw = json.dumps({"mode": "override", "branches": patterns}) if source == "stored" else None
        assert base_branch_is_filtered("o/r", "main", cfg, raw) is False
        assert base_branch_is_filtered("o/r", "release/1.2", cfg, raw) is False
        assert base_branch_is_filtered("o/r", "develop", cfg, raw) is True

    def test_whitelist_matches(self):
        cfg = _cfg(default=["main", "release/*"])
        assert base_branch_is_filtered("o/r", "main", cfg) is False
        assert base_branch_is_filtered("o/r", "release/1.2", cfg) is False
        assert base_branch_is_filtered("o/r", "release/", cfg) is False

    def test_whitelist_filters_other_branches(self):
        cfg = _cfg(default=["main", "release/*"])
        assert base_branch_is_filtered("o/r", "develop", cfg) is True
        assert base_branch_is_filtered("o/r", "dev", cfg) is True
        assert base_branch_is_filtered("o/r", "Main", cfg) is True  # case-sensitive

    def test_star_permits_all(self):
        cfg = _cfg(default=["*"])
        assert base_branch_is_filtered("o/r", "anything", cfg) is False

    def test_missing_base_branch_is_filtered_when_configured(self):
        # A webhook event that carries no resolvable base ref must not
        # bypass the gate.
        cfg = _cfg(default=["main"])
        assert base_branch_is_filtered("o/r", "", cfg) is True


class TestRepoOverrides:
    def test_override_replaces_default(self):
        cfg = _cfg(default=["dev*"], overrides={"o/r": ["main"]})
        assert base_branch_is_filtered("o/r", "main", cfg) is False
        assert base_branch_is_filtered("o/r", "dev", cfg) is True

    def test_other_repos_keep_default(self):
        cfg = _cfg(default=["dev*"], overrides={"o/r": ["main"]})
        assert base_branch_is_filtered("o/other", "dev", cfg) is False
        assert base_branch_is_filtered("o/other", "main", cfg) is True

    def test_keys_case_insensitive(self):
        cfg = _cfg(default=["dev*"], overrides={"O/R": ["main"]})
        assert base_branch_is_filtered("o/r", "main", cfg) is False
        assert base_branch_is_filtered("o/r", "dev", cfg) is True

    def test_empty_override_disables_repo(self):
        cfg = _cfg(default=["dev*"], overrides={"o/r": []})
        assert base_branch_is_filtered("o/r", "dev", cfg) is True
        assert base_branch_is_filtered("o/other", "dev", cfg) is False


class TestDashboardPolicy:
    def _repo_policy(self, mode: str, branches: list[str] | None = None) -> str:
        return json.dumps({"mode": mode, "branches": branches or []})

    def test_disable_mode(self):
        cfg = MiraConfig()
        assert (
            base_branch_is_filtered(
                "o/r", "main", cfg, repo_policy_raw=self._repo_policy(MODE_DISABLE)
            )
            is True
        )

    def test_override_mode(self):
        cfg = _cfg(default=["dev*"])
        raw = self._repo_policy(MODE_OVERRIDE, ["main"])
        assert base_branch_is_filtered("o/r", "main", cfg, repo_policy_raw=raw) is False
        assert base_branch_is_filtered("o/r", "dev", cfg, repo_policy_raw=raw) is True

    def test_inherit_mode_uses_config(self):
        cfg = _cfg(default=["dev*"])
        raw = self._repo_policy(MODE_INHERIT)
        assert base_branch_is_filtered("o/r", "dev", cfg, repo_policy_raw=raw) is False
        assert base_branch_is_filtered("o/r", "main", cfg, repo_policy_raw=raw) is True

    def test_dashboard_policy_beats_config_override(self):
        cfg = _cfg(default=["dev*"], overrides={"o/r": ["feature/*"]})
        raw = self._repo_policy(MODE_OVERRIDE, ["main"])
        assert base_branch_is_filtered("o/r", "main", cfg, repo_policy_raw=raw) is False
        assert base_branch_is_filtered("o/r", "feature/x", cfg, repo_policy_raw=raw) is True

    def test_malformed_json_falls_back_to_config(self):
        cfg = _cfg(default=["dev*"])
        assert base_branch_is_filtered("o/r", "dev", cfg, repo_policy_raw="{bad json") is False
        assert base_branch_is_filtered("o/r", "main", cfg, repo_policy_raw="{bad json") is True

    def test_unknown_mode_falls_back_to_config(self):
        cfg = _cfg(default=["dev*"])
        raw = json.dumps({"mode": "yolo"})
        assert base_branch_is_filtered("o/r", "dev", cfg, repo_policy_raw=raw) is False
        assert base_branch_is_filtered("o/r", "main", cfg, repo_policy_raw=raw) is True

    def test_override_with_missing_or_bad_branches_falls_back(self):
        # Regression: these used to raise ValueError inside the dispatcher.
        cfg = _cfg(default=["dev*"])
        bad_overrides = [
            json.dumps({"mode": "override"}),  # no branches key
            json.dumps({"mode": "override", "branches": "main"}),  # not a list
            json.dumps({"mode": "override", "branches": [""]}),  # blank pattern
            json.dumps({"mode": "override", "branches": ["main", 42]}),  # non-str
        ]
        for raw in bad_overrides:
            # Falls back to the deployment default instead of crashing.
            assert base_branch_is_filtered("o/r", "dev", cfg, repo_policy_raw=raw) is False
            assert base_branch_is_filtered("o/r", "main", cfg, repo_policy_raw=raw) is True

    def test_inherit_drops_stale_branches(self):
        raw = json.dumps({"mode": "inherit", "branches": ["stale"]})
        parsed = parse_repo_policy(raw)
        assert parsed == {"mode": "inherit", "branches": []}


class TestConfigValidation:
    """Bad patterns must fail at config load, not at webhook time."""

    def test_rejects_blank_pattern(self):
        with pytest.raises(ValidationError):
            MiraConfig(filter=FilterConfig(allowed_base_branches=[""]))

    def test_rejects_string_not_list(self):
        with pytest.raises(ValidationError):
            MiraConfig(filter=FilterConfig(allowed_base_branches="main"))

    def test_rejects_non_string_entry(self):
        with pytest.raises(ValidationError):
            MiraConfig(filter=FilterConfig(allowed_base_branches=[42]))

    def test_rejects_bad_override_patterns(self):
        with pytest.raises(ValidationError):
            MiraConfig(filter=FilterConfig(allowed_base_branch_overrides={"o/r": [""]}))

    def test_rejects_bad_override_key(self):
        for bad_key in [
            "owneronly",  # no slash
            "owner/",  # empty repo segment
            "/repo",  # empty owner segment
            "owner//repo",  # empty middle segment
            "owner/ repo",  # inner whitespace
            " owner/repo",  # outer whitespace
            "own er/repo",  # whitespace inside a segment
            "owner/re\tpo",  # tab inside a segment
        ]:
            with pytest.raises(ValidationError):
                MiraConfig(filter=FilterConfig(allowed_base_branch_overrides={bad_key: ["main"]}))

    def test_accepts_nested_namespace_override_key(self):
        # GitLab namespaces like group/subgroup/repo must keep working.
        cfg = MiraConfig(
            filter=FilterConfig(allowed_base_branch_overrides={"group/subgroup/repo": ["main"]})
        )
        assert cfg.filter.allowed_base_branch_overrides == {"group/subgroup/repo": ["main"]}

    def test_empty_lists_are_valid(self):
        # Empty default = review everything; empty override = disable that repo.
        cfg = MiraConfig(filter=FilterConfig(allowed_base_branches=[]))
        assert cfg.filter.allowed_base_branches == []
        cfg = MiraConfig(filter=FilterConfig(allowed_base_branch_overrides={"o/r": []}))
        assert cfg.filter.allowed_base_branch_overrides == {"o/r": []}


class TestValidatePatterns:
    def test_rejects_non_list(self):
        with pytest.raises(ValueError, match="list of non-empty"):
            validate_patterns("dev*")

    def test_rejects_empty_pattern(self):
        with pytest.raises(ValueError, match="non-empty"):
            validate_patterns(["dev*", ""])

    def test_rejects_non_string(self):
        with pytest.raises(ValueError, match="non-empty"):
            validate_patterns(["dev*", 42])

    def test_accepts_good_list(self):
        assert validate_patterns(["main", "dev*"]) == ["main", "dev*"]


class TestResolvePatterns:
    def test_none_means_unrestricted(self):
        assert resolve_patterns("o/r", default_patterns=[], overrides={}, repo_policy=None) is None

    def test_disable_yields_empty_list(self):
        assert (
            resolve_patterns(
                "o/r",
                default_patterns=["main"],
                overrides={},
                repo_policy={"mode": MODE_DISABLE},
            )
            == []
        )
