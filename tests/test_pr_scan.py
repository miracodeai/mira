"""Tests for review-time OSV scan (src/mira/security/pr_scan.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.models import FileChangeType, FileDiff, HunkInfo, Severity
from mira.security.osv import VulnEntry
from mira.security.pr_scan import (
    _added_line_hits,
    scan_manifest_changes,
)

# ---------------------------------------------------------------------------
# Helpers
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


# ---------------------------------------------------------------------------
# _added_line_hits unit test
# ---------------------------------------------------------------------------


class TestAddedLineHits:
    """_added_line_hits correctly maps @@ header to target_start."""

    def test_first_added_line_maps_to_target_start(self):
        """Off-by-one guard: @@ header does NOT increment line_no."""
        hunk = _hunk(
            target_start=5,
            content=("@@ -1,3 +1,4 @@\n-old\n+lodash\n+new\n"),
        )
        diff = _file_diff("x.txt", [hunk])

        hits = _added_line_hits(diff, "lodash")
        assert len(hits) == 1
        line_no, line_text = hits[0]
        assert line_no == 5, "first body added line must be target_start"
        assert "lodash" in line_text

    def test_second_added_line_advances(self):
        hunk = _hunk(
            target_start=5,
            content=("@@ -1,3 +1,4 @@\n-old\n+lodash\n+new\n"),
        )
        diff = _file_diff("x.txt", [hunk])

        hits = _added_line_hits(diff, "new")
        assert len(hits) == 1
        assert hits[0] == (6, "new")

    def test_context_and_removed_lines_advance_counter(self):
        hunk = _hunk(
            target_start=1,
            content=("@@ -1,5 +1,6 @@\nctx\n-old\nctx2\n+lodash\n+sec\n"),
        )
        diff = _file_diff("x.txt", [hunk])

        hits = _added_line_hits(diff, "lodash")
        assert hits == [(3, "lodash")]

        hits2 = _added_line_hits(diff, "sec")
        assert hits2 == [(4, "sec")]

    def test_case_insensitive(self):
        hunk = _hunk(
            target_start=1,
            content="@@ -1,1 +1,2 @@\n+LODASH\n",
        )
        diff = _file_diff("x.txt", [hunk])

        hits = _added_line_hits(diff, "lodash")
        assert len(hits) == 1


# ---------------------------------------------------------------------------
# scan_manifest_changes integration tests
# ---------------------------------------------------------------------------


class TestScanManifestChanges:
    @pytest.mark.asyncio
    async def test_added_vulnerable_dep(self):
        """Added vulnerable dep → one correctly-anchored blocker comment."""
        vuln = VulnEntry(
            cve_id="CVE-2021-23337",
            summary="Command Injection",
            severity="critical",
            advisory_url="https://nvd.nist.gov/vuln/detail/CVE-2021-23337",
            fixed_in="4.17.21",
        )
        fake_qb = AsyncMock(
            return_value={
                ("npm", "lodash", "4.17.20"): [vuln],
            }
        )

        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=('{"dependencies": {"lodash": "4.17.20"}}'))

        diff = _file_diff(
            "package.json",
            [
                _hunk(
                    target_start=2,
                    content='@@ -1,2 +1,3 @@\n+    "lodash": "4.17.20"\n',
                )
            ],
        )

        with patch("mira.security.pr_scan.query_batch", fake_qb):
            comments = await scan_manifest_changes([diff], fetcher)

        assert len(comments) == 1
        c = comments[0]
        assert c.line == 2
        assert c.severity == Severity.BLOCKER
        assert c.category == "security"
        assert c.source_pass == "osv"
        assert "lodash" in c.existing_code

    @pytest.mark.asyncio
    async def test_pre_existing_dep_excluded(self):
        """Package present in head manifest but NOT in added lines → skipped."""
        fake_qb = AsyncMock(return_value={})

        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=('{"dependencies": {"lodash": "4.17.20"}}'))

        # Hunk adds a line that does NOT contain "lodash"
        diff = _file_diff(
            "package.json",
            [
                _hunk(
                    target_start=1,
                    content='@@ -1,2 +1,3 @@\n+    "express": "4.0.0"\n',
                )
            ],
        )

        with patch("mira.security.pr_scan.query_batch", fake_qb):
            comments = await scan_manifest_changes([diff], fetcher)

        assert comments == []
        # query_batch should NOT have been called (no matching queries)
        fake_qb.assert_not_called()

    @pytest.mark.asyncio
    async def test_query_batch_raises_fails_open(self):
        """query_batch raising → returns [] without error."""
        vuln = VulnEntry(
            cve_id="CVE-2021-23337",
            summary="Command Injection",
            severity="critical",
            advisory_url="https://nvd.nist.gov/vuln/detail/CVE-2021-23337",
            fixed_in="4.17.21",
        )
        fake_qb = AsyncMock(side_effect=Exception("network fail"))

        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=('{"dependencies": {"lodash": "4.17.20"}}'))

        diff = _file_diff(
            "package.json",
            [
                _hunk(
                    target_start=1,
                    content='@@ -1,2 +1,3 @@\n+    "lodash": "4.17.20"\n',
                )
            ],
        )

        with patch("mira.security.pr_scan.query_batch", fake_qb):
            comments = await scan_manifest_changes([diff], fetcher)

        assert comments == []

    @pytest.mark.asyncio
    async def test_fetcher_returns_none_skips_file(self):
        """Fetcher returning None → file skipped, no queries."""
        fake_qb = AsyncMock()

        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=None)

        diff = _file_diff("package.json", [_hunk(target_start=1, content="+x\n")])

        with patch("mira.security.pr_scan.query_batch", fake_qb):
            comments = await scan_manifest_changes([diff], fetcher)

        assert comments == []
        fake_qb.assert_not_called()

    @pytest.mark.asyncio
    async def test_dockerfile_only_returns_empty(self):
        """Dockerfile-only change → osv_ecosystem('docker') is None → []."""
        fake_qb = AsyncMock()

        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=("FROM node:18\nRUN npm install lodash\n"))

        diff = _file_diff(
            "Dockerfile",
            [
                _hunk(
                    target_start=1,
                    content="@@ -1 +2 @@\n+FROM node:18\n",
                )
            ],
        )

        with patch("mira.security.pr_scan.query_batch", fake_qb):
            comments = await scan_manifest_changes([diff], fetcher)

        assert comments == []

    @pytest.mark.asyncio
    async def test_empty_input(self):
        """Empty manifest_files list returns immediately."""
        fetcher = MagicMock()
        comments = await scan_manifest_changes([], fetcher)
        assert comments == []
