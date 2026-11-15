"""Tests for review-time OSV scan (src/mira/security/pr_scan.py)."""

from __future__ import annotations

import json
from dataclasses import replace
from difflib import unified_diff
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from mira.core.diff_parser import parse_diff
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


@pytest.fixture
def osv_requests():
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "POST" and request.url.path == "/v1/querybatch":
            queries = json.loads(request.content)["queries"]
            return httpx.Response(
                200, json={"results": [{"vulns": [{"id": "SYNTH-OSV-1"}]} for _ in queries]}
            )
        if request.method == "GET" and request.url.path == "/v1/vulns/SYNTH-OSV-1":
            return httpx.Response(
                200,
                json={
                    "id": "SYNTH-OSV-1",
                    "summary": "Synthetic advisory",
                    "database_specific": {"severity": "HIGH"},
                    "references": [{"type": "ADVISORY", "url": "https://example.invalid/advisory"}],
                },
            )
        pytest.fail(f"Unexpected OSV request: {request.method} {request.url.path}")

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    with patch(
        "mira.security.osv.httpx.AsyncClient",
        side_effect=lambda **kwargs: client_type(transport=transport, **kwargs),
    ):
        yield requests


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


def _json_file_diff(path: str, before: str, after: str) -> FileDiff:
    diff = "".join(
        unified_diff(
            StringIO(before).readlines(),
            StringIO(after).readlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    return parse_diff(diff).files[0]


class TestExactNpmOrigins:
    @pytest.mark.asyncio
    async def test_added_lock_version_cannot_anchor_an_unchanged_version(self, osv_requests):
        before = (
            json.dumps(
                {
                    "lockfileVersion": 3,
                    "packages": {"node_modules/demo-package": {"version": "1.0.0"}},
                },
                indent=2,
            )
            + "\n"
        )
        data = json.loads(before)
        added_path = "node_modules/parent/node_modules/demo-package"
        data["packages"][added_path] = {"version": "2.0.0"}
        after = json.dumps(data, indent=2) + "\n"
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=after)

        comments = await scan_manifest_changes(
            [_json_file_diff("package-lock.json", before, after)], fetcher
        )

        posts = [request for request in osv_requests if request.method == "POST"]
        assert len(posts) == 1
        assert json.loads(posts[0].content) == {
            "queries": [
                {"package": {"ecosystem": "npm", "name": "demo-package"}, "version": "2.0.0"}
            ]
        }
        line, text = next(
            (line, text) for line, text in enumerate(after.splitlines(), 1) if added_path in text
        )
        assert [(c.line, c.existing_code, c.title) for c in comments] == [
            (line, text, "Known vulnerabilities in demo-package@2.0.0")
        ]

    @pytest.mark.asyncio
    async def test_package_mention_in_description_does_not_query_unchanged_dependency(
        self, osv_requests
    ):
        data = {"description": "old", "dependencies": {"demo-package": "1.0.0"}}
        before = json.dumps(data, indent=2) + "\n"
        data["description"] = "demo-package 1.0.0 is mentioned here"
        after = json.dumps(data, indent=2) + "\n"
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=after)

        assert (
            await scan_manifest_changes([_json_file_diff("package.json", before, after)], fetcher)
            == []
        )
        assert osv_requests == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("version", ["^1.0.0", ">=1.0.0", "1.x", "file:../local"])
    async def test_nonconcrete_npm_declaration_is_not_queried(self, osv_requests, version):
        after = json.dumps({"dependencies": {"demo-package": version}}) + "\n"
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=after)

        assert (
            await scan_manifest_changes([_json_file_diff("package.json", "", after)], fetcher) == []
        )
        assert osv_requests == []

    @pytest.mark.asyncio
    async def test_nested_legacy_version_under_versionless_parent_is_anchored(self, osv_requests):
        before = (
            json.dumps(
                {"lockfileVersion": 1, "dependencies": {"demo-package": {"version": "1.0.0"}}},
                indent=2,
            )
            + "\n"
        )
        data = json.loads(before)
        data["dependencies"]["parent"] = {"dependencies": {"demo-package": {"version": "2.0.0"}}}
        after = json.dumps(data, indent=2) + "\n"
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=after)

        comments = await scan_manifest_changes(
            [_json_file_diff("package-lock.json", before, after)], fetcher
        )

        posts = [request for request in osv_requests if request.method == "POST"]
        assert len(posts) == 1
        assert json.loads(posts[0].content) == {
            "queries": [
                {"package": {"ecosystem": "npm", "name": "demo-package"}, "version": "2.0.0"}
            ]
        }
        assert len(comments) == 1
        assert comments[0].line == next(
            i for i, text in enumerate(after.splitlines(), 1) if '"demo-package"' in text and i > 5
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("legacy", [False, True], ids=["modern", "legacy"])
    async def test_version_only_lockfile_bump_anchors_the_version(self, osv_requests, legacy):
        key = "dependencies" if legacy else "packages"
        name = "demo-package" if legacy else "node_modules/demo-package"
        data = {key: {name: {"version": "1.0.0"}}}
        before = json.dumps(data, indent=2) + "\n"
        data[key][name]["version"] = "2.0.0"
        after = json.dumps(data, indent=2) + "\n"
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=after)

        comments = await scan_manifest_changes(
            [_json_file_diff("package-lock.json", before, after)], fetcher
        )

        assert len(comments) == 1
        line, text = next(
            (i, text) for i, text in enumerate(after.splitlines(), 1) if '"2.0.0"' in text
        )
        assert (comments[0].line, comments[0].existing_code) == (line, text)
        posts = [r for r in osv_requests if r.method == "POST"]
        assert json.loads(posts[0].content)["queries"] == [
            {"package": {"ecosystem": "npm", "name": "demo-package"}, "version": "2.0.0"}
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "section", ["dependencies", "devDependencies", "peerDependencies", "optionalDependencies"]
    )
    async def test_scoped_declaration_version_on_next_line(self, osv_requests, section):
        before = json.dumps({section: {"@scope/demo-package": "1.0.0"}}, indent=2) + "\n"
        after = '{\n  "' + section + '": {\n    "@scope/demo-package":\n      "2.0.0"\n  }\n}\n'
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=after)

        comments = await scan_manifest_changes(
            [_json_file_diff("nested/package.json", before, after)], fetcher
        )

        assert len(comments) == 1
        # The added property line belongs to this exact version even when its value wraps.
        assert (comments[0].path, comments[0].line) == ("nested/package.json", 3)
        assert comments[0].title == "Known vulnerabilities in @scope/demo-package@2.0.0"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "before_data,after_data,path",
        [
            (
                {"dependencies": {"demo-package": "1.0.0", "other": "2.0.0"}},
                {"dependencies": {"other": "2.0.0", "demo-package": "1.0.0"}},
                "package.json",
            ),
            (
                {"dependencies": {"demo-package": "1.0.0"}},
                {"devDependencies": {"demo-package": "1.0.0"}},
                "package.json",
            ),
            (
                {"packages": {"node_modules/demo-package": {"version": "1.0.0"}}},
                {
                    "packages": {
                        "node_modules/parent/node_modules/demo-package": {"version": "1.0.0"}
                    }
                },
                "package-lock.json",
            ),
            (
                {"dependencies": {"demo-package": "1.0.0"}},
                {"description": "demo-package 1.0.0 removed", "dependencies": {}},
                "package.json",
            ),
        ],
        ids=["reordered", "moved-to-dev", "moved-installation", "removed-only"],
    )
    async def test_existing_or_removed_tuple_is_not_introduced(
        self, osv_requests, before_data, after_data, path
    ):
        before = json.dumps(before_data, indent=2) + "\n"
        after = json.dumps(after_data, indent=2) + "\n"
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=after)

        assert await scan_manifest_changes([_json_file_diff(path, before, after)], fetcher) == []
        assert osv_requests == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content",
        [
            '{"dependencies":{"demo-package":"1.0.0","demo-package":"2.0.0"}}',
            '{"dependencies":{"demo-package":"1.0.0","demo\\u002dpackage":"2.0.0"}}',
            '{"packages":{"node_modules/demo-package":{"version":"1.0.0","version":"2.0.0"}}}',
        ],
    )
    async def test_duplicate_json_keys_are_ambiguous(self, osv_requests, content):
        path = "package-lock.json" if '"packages"' in content else "package.json"
        after = content + "\n"
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=after)

        assert await scan_manifest_changes([_json_file_diff(path, "", after)], fetcher) == []
        assert osv_requests == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", ["source-mismatch", "truncated-hunk", "invalid-before"])
    async def test_unproven_diff_source_relationship_is_omitted(self, osv_requests, failure):
        before = json.dumps({"dependencies": {"demo-package": "1.0.0"}}, indent=2) + "\n"
        after = before.replace("1.0.0", "2.0.0")
        if failure == "invalid-before":
            before = before.replace('"1.0.0"', '"1.0.0",')
        diff = _json_file_diff("package.json", before, after)
        if failure == "truncated-hunk":
            hunk = diff.hunks[0]
            diff = replace(diff, hunks=[replace(hunk, content=hunk.content.split("+    ")[0])])
        content = after.replace("2.0.0", "3.0.0") if failure == "source-mismatch" else after
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=content)

        assert await scan_manifest_changes([diff], fetcher) == []
        assert osv_requests == []

    @pytest.mark.asyncio
    async def test_duplicate_tuple_keeps_the_eligible_installation_origin(self, osv_requests):
        location = "node_modules/parent/node_modules/demo-package"
        after = (
            json.dumps(
                {
                    "packages": {
                        "node_modules/demo-package": {"version": "1.0.0", "link": True},
                        location: {"version": "1.0.0"},
                    }
                },
                indent=2,
            )
            + "\n"
        )
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=after)

        comments = await scan_manifest_changes(
            [_json_file_diff("package-lock.json", "", after)], fetcher
        )

        assert len(comments) == 1
        assert location in comments[0].existing_code
        posts = [r for r in osv_requests if r.method == "POST"]
        assert len(json.loads(posts[0].content)["queries"]) == 1

    @pytest.mark.asyncio
    async def test_unrelated_first_manifest_cannot_capture_second_manifest_origin(
        self, osv_requests
    ):
        before = '{"description":"old","dependencies":{"demo-package":"1.0.0"}}\n'
        first = before.replace('"old"', '"demo-package 1.0.0"')
        second = '{"dependencies":{"demo-package":"1.0.0"}}\n'
        contents = {"first/package.json": first, "second/package.json": second}
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(side_effect=contents.get)

        comments = await scan_manifest_changes(
            [
                _json_file_diff("first/package.json", before, first),
                _json_file_diff("second/package.json", "", second),
            ],
            fetcher,
        )

        assert len(comments) == 1
        assert comments[0].path == "second/package.json"

    @pytest.mark.asyncio
    async def test_unicode_separator_in_metadata_does_not_shift_the_diff_anchor(self, osv_requests):
        after = (
            json.dumps(
                {"description": "one\u2028two", "dependencies": {"demo-package": "1.0.0"}},
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=after)

        comments = await scan_manifest_changes(
            [_json_file_diff("package.json", "", after)], fetcher
        )

        assert len(comments) == 1
        assert comments[0].line == next(
            i for i, line in enumerate(after.split("\n"), 1) if '"demo-package"' in line
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("legacy", [False, True], ids=["modern", "legacy"])
    @pytest.mark.parametrize(
        "metadata", [{"inBundle": True}, {"link": True}, {"resolved": "file:../local"}]
    )
    async def test_eligibility_metadata_change_does_not_introduce_an_existing_version(
        self, osv_requests, legacy, metadata
    ):
        key = "dependencies" if legacy else "packages"
        name = "demo-package" if legacy else "node_modules/demo-package"
        before = json.dumps({key: {name: {"version": "1.0.0", **metadata}}}) + "\n"
        after = json.dumps({key: {name: {"version": "1.0.0"}}}) + "\n"
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=after)

        assert (
            await scan_manifest_changes(
                [_json_file_diff("package-lock.json", before, after)], fetcher
            )
            == []
        )
        assert osv_requests == []


# ---------------------------------------------------------------------------
# scan_manifest_changes integration tests
# ---------------------------------------------------------------------------


class TestScanManifestChanges:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path,content,ecosystem,name,version,line",
        [
            pytest.param(
                "requirements.txt",
                "demo-package==1.0.0\n",
                "PyPI",
                "demo-package",
                "1.0.0",
                1,
                id="pip",
            ),
            pytest.param(
                "package.json",
                '{"dependencies":{"demo-package":"1.0.0"}}',
                "npm",
                "demo-package",
                "1.0.0",
                1,
                id="npm",
            ),
            pytest.param(
                "go.mod",
                "module example.invalid/app\n\nrequire example.invalid/demo v1.0.0\n",
                "Go",
                "example.invalid/demo",
                "v1.0.0",
                3,
                id="go",
            ),
            pytest.param(
                "composer.lock",
                '{"packages":[{"name":"example/demo-package","version":"1.0.0"}]}',
                "Packagist",
                "example/demo-package",
                "1.0.0",
                1,
                id="composer",
            ),
        ],
    )
    async def test_manifest_ecosystem_reaches_osv(
        self, osv_requests, path, content, ecosystem, name, version, line
    ):
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=content)
        diff = _file_diff(
            path,
            [
                HunkInfo(
                    source_start=line - 1,
                    source_length=0,
                    target_start=line,
                    target_length=1,
                    content=f"@@ -{line - 1},0 +{line} @@\n+{content.splitlines()[line - 1]}\n",
                )
            ],
        )
        comments = await scan_manifest_changes([diff], fetcher)

        posts = [request for request in osv_requests if request.method == "POST"]
        assert len(posts) == 1
        assert json.loads(posts[0].content) == {
            "queries": [{"package": {"ecosystem": ecosystem, "name": name}, "version": version}]
        }
        assert len(comments) == 1
        assert comments[0].path == path
        assert comments[0].line == line
        assert comments[0].severity == Severity.BLOCKER
        assert comments[0].title == f"Known vulnerabilities in {name}@{version}"
        assert "SYNTH-OSV-1" in comments[0].body

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path,content,added",
        [
            ("Dockerfile", "FROM example.invalid/demo:1.0.0\n", "FROM example.invalid/demo:1.0.0"),
            ("requirements.txt", "demo-package==1.0.0\n", "# unrelated comment"),
        ],
    )
    async def test_ineligible_dependency_makes_no_osv_request(
        self, osv_requests, path, content, added
    ):
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=content)
        diff = _file_diff(path, [_hunk(target_start=1, content=f"@@ -0,0 +1 @@\n+{added}\n")])
        assert await scan_manifest_changes([diff], fetcher) == []
        assert osv_requests == []

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
        content = '{"dependencies": {\n    "lodash": "4.17.20"\n}}\n'
        fetcher.fetch = AsyncMock(return_value=content)
        diff = _json_file_diff("package.json", '{"dependencies": {\n}}\n', content)

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
        content = '{"dependencies": {"lodash": "4.17.20"}}\n'
        fetcher.fetch = AsyncMock(return_value=content)
        diff = _json_file_diff("package.json", "", content)

        with patch("mira.security.pr_scan.query_batch", fake_qb):
            comments = await scan_manifest_changes([diff], fetcher)

        assert comments == []
        fake_qb.assert_awaited_once()

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


def _origin_manifest(path, package, *, padding=0, changed=True, version="1.0.0"):
    data = {"dependencies": {package: version}}
    if not changed:
        data = {"description": "unrelated update", **data}
    content = "\n" * padding + json.dumps(data, indent=2) + "\n"
    needle = json.dumps(package) if changed else '"description"'
    line, text = next((i, text) for i, text in enumerate(content.splitlines(), 1) if needle in text)
    hunk = HunkInfo(
        source_start=line - 1,
        source_length=0,
        target_start=line,
        target_length=1,
        content=f"@@ -{line - 1},0 +{line} @@\n+{text}\n",
    )
    diff = FileDiff(path=path, change_type=FileChangeType.MODIFIED, hunks=[hunk], added_lines=1)
    return diff, content, line, text


@pytest.fixture
def origin_osv_http():
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "POST" and request.url.path == "/v1/querybatch":
            queries = json.loads(request.content)["queries"]
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"vulns": [{"id": "SYNTH-" + q["package"]["name"]}]}
                        if q["package"]["name"].startswith("vuln-")
                        else {}
                        for q in queries
                    ]
                },
            )
        if request.method == "GET" and request.url.path.startswith("/v1/vulns/SYNTH-vuln-"):
            return httpx.Response(
                200,
                json={
                    "id": request.url.path.rsplit("/", 1)[-1],
                    "summary": "Synthetic advisory",
                    "database_specific": {"severity": "HIGH"},
                    "references": [{"type": "ADVISORY", "url": "https://example.invalid/advisory"}],
                },
            )
        pytest.fail(f"Unexpected OSV request: {request.method} {request.url.path}")

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    with patch(
        "mira.security.osv.httpx.AsyncClient",
        side_effect=lambda **kwargs: client_type(transport=transport, **kwargs),
    ):
        yield requests


class TestManifestCommentOrigins:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("last_state", ["safe", "unchanged", "unreadable"])
    @pytest.mark.parametrize("reverse", [False, True])
    async def test_finding_keeps_its_source_manifest(self, origin_osv_http, last_state, reverse):
        first = _origin_manifest("first/package.json", "vuln-first")
        last = _origin_manifest(
            "last/package.json", "safe-dep", padding=4, changed=last_state != "unchanged"
        )
        contents = {
            first[0].path: first[1],
            last[0].path: None if last_state == "unreadable" else last[1],
        }
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(side_effect=contents.get)
        files = [last[0], first[0]] if reverse else [first[0], last[0]]

        comments = await scan_manifest_changes(files, fetcher)

        assert len(comments) == 1
        comment = comments[0]
        assert (comment.path, comment.line, comment.existing_code) == (
            first[0].path,
            first[2],
            first[3],
        )
        assert comment.agent_prompt.startswith(f"In {first[0].path},")

    @pytest.mark.asyncio
    async def test_distinct_findings_keep_distinct_locations(self, origin_osv_http):
        cases = [
            _origin_manifest("first/package.json", "vuln-first"),
            _origin_manifest("last/package.json", "vuln-last", padding=4),
        ]
        contents = {case[0].path: case[1] for case in cases}
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(side_effect=contents.get)

        comments = await scan_manifest_changes([case[0] for case in cases], fetcher)

        assert len(comments) == 2
        assert {(c.path, c.line, c.existing_code) for c in comments} == {
            (case[0].path, case[2], case[3]) for case in cases
        }
        assert all(c.agent_prompt.startswith(f"In {c.path},") for c in comments)

    @pytest.mark.asyncio
    async def test_repeated_package_uses_first_matching_location(self, origin_osv_http):
        first = _origin_manifest("first/package.json", "vuln-shared")
        last = _origin_manifest("last/package.json", "vuln-shared", padding=4)
        contents = {first[0].path: first[1], last[0].path: last[1]}
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(side_effect=contents.get)

        comments = await scan_manifest_changes([first[0], last[0]], fetcher)

        assert comments
        assert all(
            (c.path, c.line, c.existing_code) == (first[0].path, first[2], first[3])
            for c in comments
        )
        assert all(c.agent_prompt.startswith(f"In {first[0].path},") for c in comments)


class TestManifestQueryDeduplication:
    @pytest.mark.asyncio
    async def test_repeated_modern_lockfile_tuple_has_one_query_and_comment(self, osv_requests):
        content = json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "synthetic-project", "version": "1.0.0"},
                    "node_modules/demo-package": {"version": "1.0.0"},
                    "node_modules/parent/node_modules/demo-package": {"version": "1.0.0"},
                },
            },
            indent=2,
        )
        path = "package-lock.json"
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=content)
        diff = _json_file_diff(path, "", content + "\n")

        comments = await scan_manifest_changes([diff], fetcher)

        posts = [request for request in osv_requests if request.method == "POST"]
        assert len(posts) == 1
        assert json.loads(posts[0].content) == {
            "queries": [
                {"package": {"ecosystem": "npm", "name": "demo-package"}, "version": "1.0.0"}
            ]
        }
        assert len(comments) == 1
        line, text = next(
            (i, text)
            for i, text in enumerate(content.splitlines(), 1)
            if '"node_modules/demo-package"' in text
        )
        comment = comments[0]
        assert (comment.path, comment.line, comment.existing_code) == (path, line, text)
        assert comment.agent_prompt.startswith(f"In {path},")
        assert "SYNTH-OSV-1" in comment.body

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reverse", [False, True])
    @pytest.mark.parametrize("first_version", ["1.0.0", "^1.0.0"])
    async def test_repeated_tuple_keeps_first_eligible_origin(
        self, osv_requests, reverse, first_version
    ):
        cases = [
            _origin_manifest("first/package.json", "demo-package", version=first_version),
            _origin_manifest("last/package.json", "demo-package", padding=4),
        ]
        if reverse:
            cases.reverse()
        contents = {case[0].path: case[1] for case in cases}
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(side_effect=contents.get)

        comments = await scan_manifest_changes([case[0] for case in cases], fetcher)

        posts = [request for request in osv_requests if request.method == "POST"]
        assert len(posts) == 1
        assert json.loads(posts[0].content) == {
            "queries": [
                {"package": {"ecosystem": "npm", "name": "demo-package"}, "version": "1.0.0"}
            ]
        }
        assert len(comments) == 1
        first = cases[1] if first_version == "^1.0.0" and not reverse else cases[0]
        comment = comments[0]
        assert (comment.path, comment.line, comment.existing_code) == (
            first[0].path,
            first[2],
            first[3],
        )
        assert comment.agent_prompt.startswith(f"In {first[0].path},")
        assert "demo-package from 1.0.0" in comment.agent_prompt

    @pytest.mark.asyncio
    async def test_unchanged_tuple_does_not_suppress_later_eligible_origin(self, osv_requests):
        first = _origin_manifest("first/package.json", "demo-package", changed=False)
        last = _origin_manifest("last/package.json", "demo-package", padding=4)
        contents = {first[0].path: first[1], last[0].path: last[1]}
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(side_effect=contents.get)

        comments = await scan_manifest_changes([first[0], last[0], last[0]], fetcher)

        posts = [request for request in osv_requests if request.method == "POST"]
        assert len(posts) == 1
        assert json.loads(posts[0].content) == {
            "queries": [
                {"package": {"ecosystem": "npm", "name": "demo-package"}, "version": "1.0.0"}
            ]
        }
        assert len(comments) == 1
        comment = comments[0]
        assert (comment.path, comment.line, comment.existing_code) == (
            last[0].path,
            last[2],
            last[3],
        )
        assert comment.agent_prompt.startswith(f"In {last[0].path},")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path,content,ecosystem,name,version",
        [
            (
                "last/package.json",
                '{"dependencies":{"demo-package":"2.0.0"}}',
                "npm",
                "demo-package",
                "2.0.0",
            ),
            (
                "last/package.json",
                '{"dependencies":{"other-package":"1.0.0"}}',
                "npm",
                "other-package",
                "1.0.0",
            ),
            ("requirements.txt", "demo-package==1.0.0", "PyPI", "demo-package", "1.0.0"),
        ],
        ids=["different-version", "different-name", "different-kind"],
    )
    async def test_distinct_tuples_keep_encounter_order_and_shared_advisory(
        self, osv_requests, path, content, ecosystem, name, version
    ):
        first = _origin_manifest("first/package.json", "demo-package")
        last = _json_file_diff(path, "\n" * 6, "\n" * 6 + content + "\n")
        contents = {first[0].path: first[1], path: "\n" * 6 + content + "\n"}
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(side_effect=contents.get)

        comments = await scan_manifest_changes([first[0], last, first[0]], fetcher)

        posts = [request for request in osv_requests if request.method == "POST"]
        assert len(posts) == 1
        assert json.loads(posts[0].content) == {
            "queries": [
                {"package": {"ecosystem": "npm", "name": "demo-package"}, "version": "1.0.0"},
                {"package": {"ecosystem": ecosystem, "name": name}, "version": version},
            ]
        }
        assert len(comments) == 2
        assert [(c.path, c.line, c.existing_code) for c in comments] == [
            (first[0].path, first[2], first[3]),
            (path, 7, content),
        ]
        assert [c.title for c in comments] == [
            "Known vulnerabilities in demo-package@1.0.0",
            f"Known vulnerabilities in {name}@{version}",
        ]
        assert all("SYNTH-OSV-1" in c.body for c in comments)
        assert all(c.agent_prompt.startswith(f"In {c.path},") for c in comments)

    @pytest.mark.asyncio
    async def test_repeated_tuple_without_advisories_has_no_comments(self, origin_osv_http):
        first = _origin_manifest("first/package.json", "safe-package")
        last = _origin_manifest("last/package.json", "safe-package", padding=4)
        contents = {first[0].path: first[1], last[0].path: last[1]}
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(side_effect=contents.get)

        assert await scan_manifest_changes([first[0], last[0]], fetcher) == []

        assert len(origin_osv_http) == 1
        request = origin_osv_http[0]
        assert request.method == "POST"
        assert json.loads(request.content) == {
            "queries": [
                {"package": {"ecosystem": "npm", "name": "safe-package"}, "version": "1.0.0"}
            ]
        }

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path,content",
        [
            ("Dockerfile", "FROM example.invalid/demo:1.0.0"),
            ("package.json", '{"dependencies":{"demo-package":""}}'),
            ("package.json", '{"dependencies":{"demo-package":"^"}}'),
        ],
        ids=["unsupported-kind", "empty-version", "empty-normalized-version"],
    )
    async def test_repeated_ineligible_tuple_makes_no_request(self, osv_requests, path, content):
        diff = _file_diff(path, [_hunk(1, f"@@ -0,0 +1 @@\n+{content}\n")])
        fetcher = MagicMock()
        fetcher.fetch = AsyncMock(return_value=content)

        assert await scan_manifest_changes([diff, diff], fetcher) == []
        assert osv_requests == []
