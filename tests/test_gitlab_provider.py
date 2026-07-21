"""Tests for GitLabProvider (mira.providers.gitlab)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from mira.exceptions import ProviderError
from mira.models import PRInfo, ReviewComment, ReviewResult, Severity
from mira.providers import create_provider
from mira.providers.gitlab import GitLabProvider, _build_unified_diff, parse_mr_url


class _FakeResp:
    def __init__(self, status=200, json_data=None, text="", headers=None):
        self.status_code = status
        self._json = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, handler):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, **kw):
        return self._handler(method, url, **kw)

    async def get(self, url, **kw):
        return self._handler("GET", url, **kw)


def _patch(handler):
    return patch("mira.providers.gitlab.httpx.AsyncClient", lambda *a, **k: _FakeClient(handler))


_PR = PRInfo(
    title="t",
    description="d",
    base_branch="main",
    head_branch="feat",
    url="u",
    number=7,
    owner="group/sub",
    repo="proj",
    head_sha="abc",
)


class TestParseMrUrl:
    def test_full_url_nested_group(self):
        owner, repo, iid = parse_mr_url("https://gitlab.com/group/sub/proj/-/merge_requests/42")
        assert owner == "group/sub"
        assert repo == "proj"
        assert iid == 42

    def test_shorthand(self):
        assert parse_mr_url("group/proj!13") == ("group", "proj", 13)

    def test_invalid(self):
        with pytest.raises(ProviderError):
            parse_mr_url("not-a-url")


class TestBuildUnifiedDiff:
    def test_headers_prepended(self):
        changes = [{"old_path": "a.py", "new_path": "a.py", "diff": "@@ -1 +1 @@\n-x\n+y\n"}]
        out = _build_unified_diff(changes)
        assert "--- a/a.py" in out
        assert "+++ b/a.py" in out
        assert "@@ -1 +1 @@" in out

    def test_added_file_uses_devnull(self):
        changes = [
            {
                "old_path": "n.py",
                "new_path": "n.py",
                "new_file": True,
                "diff": "@@ -0,0 +1 @@\n+x\n",
            }
        ]
        out = _build_unified_diff(changes)
        assert "--- /dev/null" in out
        assert "+++ b/n.py" in out
        assert "new file mode" in out

    def test_added_file_parses_as_single_file(self):
        # Regression: a `diff --git` header + /dev/null without a file-mode line
        # makes unidiff emit the file twice (the empty entry then clobbers the
        # real line ranges, so every finding is dropped). The mode line fixes it.
        from mira.core.diff_parser import parse_diff

        changes = [
            {
                "old_path": "n.py",
                "new_path": "n.py",
                "new_file": True,
                "diff": "@@ -0,0 +1,2 @@\n+import os\n+x = 1\n",
            }
        ]
        files = parse_diff(_build_unified_diff(changes)).files
        assert len(files) == 1
        assert files[0].path == "n.py"
        assert len(files[0].hunks) == 1


class TestGetPrInfo:
    @pytest.mark.asyncio
    async def test_maps_iid_and_branches(self):
        def handler(method, url, **kw):
            return _FakeResp(
                json_data={
                    "title": "Add feature",
                    "description": "body",
                    "target_branch": "main",
                    "source_branch": "feat",
                    "web_url": "https://gitlab.com/g/p/-/merge_requests/7",
                    "sha": "deadbeef",
                }
            )

        with _patch(handler):
            info = await GitLabProvider("tok").get_pr_info(
                "https://gitlab.com/g/p/-/merge_requests/7"
            )
        assert info.number == 7
        assert info.base_branch == "main"
        assert info.head_branch == "feat"
        assert info.head_sha == "deadbeef"


class TestPostReview:
    @pytest.mark.asyncio
    async def test_inline_position_payload(self):
        calls = []

        def handler(method, url, **kw):
            calls.append((method, url, kw.get("data")))
            if url.endswith("/changes"):
                return _FakeResp(
                    json_data={
                        "diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "h"},
                        "changes": [],
                    }
                )
            return _FakeResp(status=201, json_data={"id": 1})

        result = ReviewResult(
            comments=[
                ReviewComment(
                    path="a.py",
                    line=10,
                    end_line=None,
                    severity=Severity.WARNING,
                    category="bug",
                    title="x",
                    body="y",
                    confidence=0.9,
                )
            ],
            summary="",
            key_issues=[],
        )
        with _patch(handler):
            await GitLabProvider("tok").post_review(_PR, result)

        disc = next(c for c in calls if c[1].endswith("/discussions"))
        data = disc[2]
        assert data["position[new_line]"] == "10"
        assert data["position[base_sha]"] == "b"
        assert data["position[new_path]"] == "a.py"

    @pytest.mark.asyncio
    async def test_falls_back_to_note_on_400(self):
        calls = []

        def handler(method, url, **kw):
            calls.append((method, url, kw.get("data")))
            if url.endswith("/changes"):
                return _FakeResp(json_data={"diff_refs": {}, "changes": []})
            if url.endswith("/discussions"):
                return _FakeResp(status=400, text="line not in diff")
            return _FakeResp(status=201, json_data={"id": 1})  # /notes

        result = ReviewResult(
            comments=[
                ReviewComment(
                    path="a.py",
                    line=10,
                    end_line=None,
                    severity=Severity.WARNING,
                    category="bug",
                    title="x",
                    body="y",
                    confidence=0.9,
                )
            ],
            summary="",
            key_issues=[],
        )
        with _patch(handler):
            await GitLabProvider("tok").post_review(_PR, result)

        # The inline 400 should have produced a plain note containing the path.
        notes = [c for c in calls if c[1].endswith("/notes")]
        assert any("a.py:10" in (c[2] or {}).get("body", "") for c in notes)


class TestCompareDiff:
    @pytest.mark.asyncio
    async def test_builds_diff_from_compare(self):
        def handler(method, url, **kw):
            assert "from=base1" in url and "to=head2" in url
            return _FakeResp(
                json_data={
                    "diffs": [
                        {"old_path": "a.py", "new_path": "a.py", "diff": "@@ -1 +1 @@\n-x\n+y\n"}
                    ]
                }
            )

        with _patch(handler):
            out = await GitLabProvider("tok").get_compare_diff(_PR, "base1", "head2")
        assert "--- a/a.py" in out and "@@ -1 +1 @@" in out

    @pytest.mark.asyncio
    async def test_identical_shas_returns_empty(self):
        out = await GitLabProvider("tok").get_compare_diff(_PR, "x", "x")
        assert out == ""


class TestResolveThreads:
    @pytest.mark.asyncio
    async def test_puts_resolved_true(self):
        calls = []

        def handler(method, url, **kw):
            calls.append((method, url, kw.get("data")))
            return _FakeResp(status=200, json_data={})

        with _patch(handler):
            n = await GitLabProvider("tok").resolve_threads(_PR, ["d1", "d2"])
        assert n == 2
        assert all(c[2] == {"resolved": "true"} for c in calls)
        assert all("/discussions/" in c[1] for c in calls)


class TestDiscussionRootBody:
    @pytest.mark.asyncio
    async def test_returns_first_note(self):
        def handler(method, url, **kw):
            assert "/discussions/abc" in url
            return _FakeResp(
                json_data={"notes": [{"body": "original suggestion"}, {"body": "reply"}]}
            )

        with _patch(handler):
            body = await GitLabProvider("tok").get_discussion_root_body(_PR, "abc")
        assert body == "original suggestion"

    @pytest.mark.asyncio
    async def test_empty_on_error(self):
        def handler(method, url, **kw):
            return _FakeResp(status=404, text="nope")

        with _patch(handler):
            assert await GitLabProvider("tok").get_discussion_root_body(_PR, "abc") == ""


class TestBotThreadIdentity:
    @pytest.mark.asyncio
    async def test_matches_token_user_not_configured_name(self):
        # The bot posts as the access-token user (project_99_bot_x), which is
        # unrelated to the configured display name "mira". Dedup/resolve must
        # still recognise its own threads.
        discussions = [
            {
                "id": "d1",
                "notes": [
                    {
                        "author": {"username": "project_99_bot_x"},
                        "position": {"new_path": "a.py", "new_line": 5},
                        "body": "bot finding",
                        "resolved": False,
                    }
                ],
            },
            {
                "id": "d2",
                "notes": [
                    {
                        "author": {"username": "alice"},
                        "position": {"new_path": "a.py", "new_line": 9},
                        "body": "human note",
                        "resolved": False,
                    }
                ],
            },
        ]

        def handler(method, url, **kw):
            if url.endswith("/user"):
                return _FakeResp(json_data={"username": "project_99_bot_x"})
            return _FakeResp(json_data=discussions)

        with _patch(handler):
            threads = await GitLabProvider("tok").get_unresolved_bot_threads(_PR, "mira")
        assert [t.thread_id for t in threads] == ["d1"]


class TestGetFileContent:
    @pytest.mark.asyncio
    async def test_returns_raw_body(self):
        def handler(method, url, **kw):
            assert "/repository/files/" in url and "/raw?ref=" in url
            return _FakeResp(text="x = 1\n")

        with _patch(handler):
            body = await GitLabProvider("tok").get_file_content(_PR, "a.py", "feat")
        assert body == "x = 1\n"

    @pytest.mark.asyncio
    async def test_missing_file_returns_empty(self):
        def handler(method, url, **kw):
            return _FakeResp(status=404, text="not found")

        with _patch(handler):
            assert await GitLabProvider("tok").get_file_content(_PR, "gone.py", "feat") == ""


class TestGetRepoTree:
    @pytest.mark.asyncio
    async def test_lists_blob_paths_only(self):
        def handler(method, url, **kw):
            assert "/repository/tree" in url
            return _FakeResp(
                json_data=[
                    {"type": "blob", "path": "a.py"},
                    {"type": "tree", "path": "pkg"},
                    {"type": "blob", "path": "pkg/b.py"},
                ]
            )

        with _patch(handler):
            paths = await GitLabProvider("tok").get_repo_tree(_PR, "main")
        assert paths == ["a.py", "pkg/b.py"]


class TestGetFileHistory:
    @pytest.mark.asyncio
    async def test_maps_commits(self):
        def handler(method, url, **kw):
            assert "/repository/commits" in url
            return _FakeResp(
                json_data=[
                    {
                        "short_id": "abc1234",
                        "message": "fix thing\n\ndetails",
                        "author_name": "Alice",
                        "authored_date": "2026-01-01",
                    }
                ]
            )

        with _patch(handler):
            hist = await GitLabProvider("tok").get_file_history(_PR, ["a.py"], max_per_file=5)
        entry = hist["a.py"][0]
        assert entry.sha == "abc1234"
        assert entry.author == "Alice"
        assert entry.message == "fix thing"

    @pytest.mark.asyncio
    async def test_empty_paths_short_circuits(self):
        with _patch(lambda *a, **k: _FakeResp(json_data=[])):
            assert await GitLabProvider("tok").get_file_history(_PR, []) == {}


class TestRegistry:
    def test_create_provider_gitlab(self):
        assert isinstance(create_provider("gitlab", "tok"), GitLabProvider)
