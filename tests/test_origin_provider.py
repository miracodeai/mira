"""Unit tests for the Cursor Origin provider helpers."""

from __future__ import annotations

import pytest

from mira.exceptions import ProviderError
from mira.providers.origin import _patches_to_diff, parse_pr_url


def test_parse_pr_url_codebase() -> None:
    owner, repo, number = parse_pr_url("https://cursor.com/codebase/acme/rocket/pull/17")
    assert (owner, repo, number) == ("acme", "rocket", 17)


def test_parse_pr_url_git_host() -> None:
    owner, repo, number = parse_pr_url("https://origin.cursor.com/acme/rocket/pull/17")
    assert (owner, repo, number) == ("acme", "rocket", 17)


def test_parse_pr_url_rejects_garbage() -> None:
    with pytest.raises(ProviderError):
        parse_pr_url("https://github.com/acme/rocket/pull/17")


def test_patches_to_diff_modified() -> None:
    diff = _patches_to_diff(
        [
            {
                "filename": "src/a.ts",
                "status": "modified",
                "patch": "@@ -1,2 +1,3 @@\n a\n+b\n c\n",
            }
        ]
    )
    assert "diff --git a/src/a.ts b/src/a.ts" in diff
    assert "+++ b/src/a.ts" in diff
    assert "+b" in diff
