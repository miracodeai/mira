"""Tests for the deterministic secrets scanner (src/mira/security/secrets_scan.py)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from mira.models import FileChangeType, FileDiff, HunkInfo, Severity
from mira.security import secrets_scan
from mira.security.secrets_scan import scan_secrets, shannon_entropy

# ---------------------------------------------------------------------------
# Helpers (cloned from tests/test_pr_scan.py)
# ---------------------------------------------------------------------------


def _hunk(target_start: int, content: str) -> HunkInfo:
    return HunkInfo(
        source_start=1,
        source_length=0,
        target_start=target_start,
        target_length=0,
        content=content,
    )


def _file_diff(path: str, hunks: list[HunkInfo]) -> FileDiff:
    return FileDiff(
        path=path,
        change_type=FileChangeType.MODIFIED,
        language="",
        added_lines=0,
        deleted_lines=0,
        hunks=hunks,
    )


# The key-format patterns are anchored with \b, so the key must not sit
# immediately inside quotes (the opening quote breaks the boundary) — mirror
# the plan's unquoted example form.
AWS_KEY_LINE = "aws_access_key_id = AKIAABCDEFGHIJKLMNOP"
GITHUB_TOKEN = "ghp_" + "a1B2c3D4e5F6g7H8i9J0kLmNoPqRsTuVwXyZ"  # 40 chars after ghp_

# ---------------------------------------------------------------------------
# shannon_entropy
# ---------------------------------------------------------------------------


class TestShannonEntropy:
    def test_empty_is_zero(self):
        assert shannon_entropy("") == 0.0

    def test_single_char_is_zero(self):
        assert shannon_entropy("aaaaaaaaaaaaaaaaaaaa") == 0.0

    def test_mixed_is_high(self):
        assert shannon_entropy("aB3$x9Zk2mQp7wLt") > 3.5


# ---------------------------------------------------------------------------
# Pattern matches
# ---------------------------------------------------------------------------


class TestSecretMatches:
    @pytest.mark.asyncio
    async def test_aws_key(self):
        """One added line with an AWS key → exactly one correctly-anchored comment."""
        diff = _file_diff(
            "config.py",
            [_hunk(target_start=7, content=f"@@ -1,1 +1,2 @@\n+{AWS_KEY_LINE}\n")],
        )

        comments = await scan_secrets([diff])
        assert len(comments) == 1
        c = comments[0]
        assert c.path == "config.py"
        assert c.line == 7
        assert c.severity == Severity.BLOCKER
        assert c.category == "security"
        assert c.source_pass == "secrets"
        assert "AKIA" in c.existing_code
        assert c.confidence == 0.9
        assert c.title == "Hardcoded AWS access key in diff"

    @pytest.mark.asyncio
    async def test_github_token(self):
        diff = _file_diff(
            "ci.sh",
            [_hunk(target_start=3, content=f"@@ -1,1 +1,2 @@\n+GITHUB_TOKEN={GITHUB_TOKEN}\n")],
        )

        comments = await scan_secrets([diff])
        assert len(comments) == 1
        assert "GitHub token" in comments[0].title
        assert comments[0].line == 3

    @pytest.mark.asyncio
    async def test_high_entropy_assignment(self):
        line = "password = 'aB3$x9Zk2mQp7wLt'"
        diff = _file_diff(
            "setup.py",
            [_hunk(target_start=2, content=f"@@ -1,1 +1,2 @@\n+{line}\n")],
        )

        comments = await scan_secrets([diff])
        assert len(comments) == 1
        assert "credential assignment" in comments[0].title

    @pytest.mark.asyncio
    async def test_placeholder_assignment_not_matched(self):
        """'changeme' is on the placeholder reject list → no comment."""
        line = 'api_key = "changeme"'
        diff = _file_diff(
            "setup.py",
            [_hunk(target_start=2, content=f"@@ -1,1 +1,2 @@\n+{line}\n")],
        )

        assert await scan_secrets([diff]) == []

    @pytest.mark.asyncio
    async def test_low_entropy_assignment_not_matched(self):
        """All-same-char literal has entropy 0 → no comment."""
        line = 'api_key = "aaaaaaaaaaaaaaaaaaaa"'
        diff = _file_diff(
            "setup.py",
            [_hunk(target_start=2, content=f"@@ -1,1 +1,2 @@\n+{line}\n")],
        )

        assert await scan_secrets([diff]) == []


# ---------------------------------------------------------------------------
# Dedup and cap
# ---------------------------------------------------------------------------


class TestDedupAndCap:
    @pytest.mark.asyncio
    async def test_same_pattern_twice_one_comment(self):
        """Same AWS key on two added lines in the same file → exactly one comment."""
        diff = _file_diff(
            "config.py",
            [
                _hunk(
                    target_start=1,
                    content=f"@@ -1,1 +1,3 @@\n+{AWS_KEY_LINE}\n+other\n+{AWS_KEY_LINE}\n",
                )
            ],
        )

        comments = await scan_secrets([diff])
        assert len(comments) == 1
        assert comments[0].line == 1, "first added line wins"

    @pytest.mark.asyncio
    async def test_per_file_cap(self):
        """Six distinct secret patterns in one file → capped at 5 comments."""
        lines = [
            AWS_KEY_LINE,
            f"token={GITHUB_TOKEN}",
            "glpat-AbCdEfGhIjKlMnOpQrSt",
            "AIza" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r",  # AIza + 35 chars
            "xoxb-1234567890abcdef",
            "sk_live_" + "AbCdEfGhIjKlMnOpQrStUvWx",
        ]
        content = "@@ -1,1 +1,7 @@\n" + "".join(f"+{line}\n" for line in lines)
        diff = _file_diff("creds.env", [_hunk(target_start=1, content=content)])

        comments = await scan_secrets([diff])
        assert len(comments) == 5
        assert {c.title for c in comments} == {
            "Hardcoded AWS access key in diff",
            "Hardcoded GitHub token in diff",
            "Hardcoded GitLab personal access token in diff",
            "Hardcoded Google API key in diff",
            "Hardcoded Slack token in diff",
        }


# ---------------------------------------------------------------------------
# Edge cases and fail-open
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_files(self):
        assert await scan_secrets([]) == []

    @pytest.mark.asyncio
    async def test_no_secrets(self):
        diff = _file_diff(
            "README.md",
            [_hunk(target_start=1, content="@@ -1,1 +1,2 @@\n+just some prose\n")],
        )
        assert await scan_secrets([diff]) == []

    @pytest.mark.asyncio
    async def test_fail_open(self):
        """Any exception inside the scan → warn + return [], never raise."""
        diff = _file_diff("x.py", [_hunk(target_start=1, content="@@ -1,1 +1,2 @@\n+pass\n")])
        with patch.object(secrets_scan, "_added_line_hits", side_effect=RuntimeError("boom")):
            assert await scan_secrets([diff]) == []
