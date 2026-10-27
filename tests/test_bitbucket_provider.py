"""Tests for BitbucketProvider, BitbucketRepoFetcher and Bitbucket auth helpers."""

from __future__ import annotations

import base64
from unittest.mock import patch

import httpx
import pytest

from mira.exceptions import ProviderError
from mira.models import PRInfo, ReviewComment, ReviewResult, Severity
from mira.platforms.bitbucket.auth import (
    BitbucketTokenAuth,
    bitbucket_auth_headers,
    normalize_mentions,
)
from mira.platforms.fetch import BitbucketRepoFetcher, EmptyRepoError, make_fetcher
from mira.providers import create_provider
from mira.providers.bitbucket import BitbucketProvider, parse_pr_url

API = "https://api.bitbucket.org/2.0"
REPO = f"{API}/repositories/acme/widgets"
BOT = {"nickname": "mira-bot", "account_id": "557058:bot", "uuid": "{bot-uuid}"}
ALICE = {"nickname": "alice", "account_id": "557058:alice", "uuid": "{alice-uuid}"}


class _FakeResp:
    def __init__(self, status=200, json_data=None, text="", url="https://x"):
        self.status_code = status
        self._json = json_data
        self.text = text
        self._url = url

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", self._url)
            raise httpx.HTTPStatusError(
                "error", request=request, response=httpx.Response(self.status_code)
            )


class _FakeClient:
    """Routes every call through ``handler(method, url, **kw)`` and records it."""

    def __init__(self, handler, calls):
        self._handler = handler
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, method, url, **kw):
        self._calls.append((method, url, kw))
        return self._handler(method, url, **kw)

    async def get(self, url, **kw):
        return await self.request("GET", url, **kw)


def _patch(handler, module="mira.providers.bitbucket"):
    calls: list = []
    ctx = patch(f"{module}.httpx.AsyncClient", lambda *a, **k: _FakeClient(handler, calls))
    return ctx, calls


def _pr_info(**kw) -> PRInfo:
    base = {
        "title": "t",
        "description": "d",
        "base_branch": "main",
        "head_branch": "feature",
        "url": "https://bitbucket.org/acme/widgets/pull-requests/7",
        "number": 7,
        "owner": "acme",
        "repo": "widgets",
        "head_sha": "abc123",
        "platform": "bitbucket",
    }
    base.update(kw)
    return PRInfo(**base)


@pytest.fixture(autouse=True)
def _no_bot_override(monkeypatch):
    monkeypatch.delenv("MIRA_BITBUCKET_BOT_USER", raising=False)


# ── auth helpers ────────────────────────────────────────────────────


class TestAuthHeaders:
    def test_access_token_is_bearer(self):
        assert bitbucket_auth_headers("ATCTT3x") == {"Authorization": "Bearer ATCTT3x"}

    def test_email_api_token_is_basic(self):
        header = bitbucket_auth_headers("bot@acme.test:ATATT3x")["Authorization"]
        assert header.startswith("Basic ")
        assert base64.b64decode(header[6:]).decode() == "bot@acme.test:ATATT3x"


class TestNormalizeMentions:
    def test_rewrites_known_account_id(self):
        text = "@{557058:bot} review please"
        assert normalize_mentions(text, {"557058:bot": "mira-bot"}) == "@mira-bot review please"

    def test_rewrites_braced_uuid(self):
        assert normalize_mentions("@{{bot-uuid}} hi", {"{bot-uuid}": "mira"}) == "@mira hi"

    def test_leaves_unknown_ids(self):
        assert normalize_mentions("@{557058:other} x", {"557058:bot": "m"}) == "@{557058:other} x"


class TestTokenAuth:
    @pytest.mark.asyncio
    async def test_identity_from_user_endpoint(self):
        def handler(method, url, **kw):
            assert url == f"{API}/user"
            return _FakeResp(json_data=BOT)

        ctx, _ = _patch(handler, "mira.platforms.bitbucket.auth")
        with ctx:
            auth = BitbucketTokenAuth("tok")
            assert await auth.get_bot_identity() == "mira-bot"
            assert await auth.get_bot_ids() == {"mira-bot", "557058:bot", "{bot-uuid}"}

    @pytest.mark.asyncio
    async def test_override_wins_when_user_endpoint_denied(self):
        def handler(method, url, **kw):
            return _FakeResp(status=403, json_data={})

        ctx, _ = _patch(handler, "mira.platforms.bitbucket.auth")
        with ctx:
            auth = BitbucketTokenAuth("tok", bot_identity="mira")
            assert await auth.get_bot_identity() == "mira"
            assert await auth.get_bot_ids() == {"mira"}


# ── provider ────────────────────────────────────────────────────────


class TestParsePrUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://bitbucket.org/acme/widgets/pull-requests/7",
            "https://bitbucket.org/acme/widgets/pull-requests/7/diff",
            "acme/widgets/pull-requests/7",
        ],
    )
    def test_parses(self, url):
        assert parse_pr_url(url) == ("acme", "widgets", 7)

    def test_rejects_github_shape(self):
        with pytest.raises(ProviderError):
            parse_pr_url("https://github.com/acme/widgets/pull/7")


def test_registered_and_requires_token():
    assert isinstance(create_provider("bitbucket", "tok"), BitbucketProvider)
    with pytest.raises(ProviderError):
        BitbucketProvider("")


class TestGetPrInfo:
    @pytest.mark.asyncio
    async def test_maps_fields(self):
        pr = {
            "id": 7,
            "title": "Add widget",
            "description": "Body",
            "source": {"branch": {"name": "feature"}, "commit": {"hash": "abc123"}},
            "destination": {"branch": {"name": "release"}, "commit": {"hash": "def456"}},
            "links": {"html": {"href": "https://bitbucket.org/acme/widgets/pull-requests/7"}},
            "author": {**ALICE, "links": {"avatar": {"href": "https://avatar"}}},
        }

        def handler(method, url, **kw):
            assert (method, url) == ("GET", f"{REPO}/pullrequests/7")
            assert kw["headers"]["Authorization"] == "Bearer tok"
            return _FakeResp(json_data=pr)

        ctx, _ = _patch(handler)
        with ctx:
            info = await BitbucketProvider("tok").get_pr_info(
                "https://bitbucket.org/acme/widgets/pull-requests/7"
            )
        assert info.title == "Add widget"
        assert info.base_branch == "release"
        assert info.head_branch == "feature"
        assert info.head_sha == "abc123"
        assert info.platform == "bitbucket"
        assert info.author == "alice"
        assert info.author_avatar_url == "https://avatar"


class TestDiffs:
    @pytest.mark.asyncio
    async def test_pr_diff(self):
        def handler(method, url, **kw):
            assert url == f"{REPO}/pullrequests/7/diff"
            return _FakeResp(text="diff --git a/x b/x\n")

        ctx, _ = _patch(handler)
        with ctx:
            assert await BitbucketProvider("tok").get_pr_diff(_pr_info()) == "diff --git a/x b/x\n"

    @pytest.mark.asyncio
    async def test_compare_diff_spec_is_head_then_base(self):
        def handler(method, url, **kw):
            assert url == f"{REPO}/diff/new222..old111?topic=false"
            return _FakeResp(text="D")

        ctx, _ = _patch(handler)
        with ctx:
            diff = await BitbucketProvider("tok").get_compare_diff(_pr_info(), "old111", "new222")
        assert diff == "D"

    @pytest.mark.asyncio
    async def test_compare_diff_same_sha_short_circuits(self):
        assert await BitbucketProvider("tok").get_compare_diff(_pr_info(), "a", "a") == ""


class TestPostReview:
    @staticmethod
    def _result() -> ReviewResult:
        return ReviewResult(
            comments=[
                ReviewComment(
                    path="src/a.py",
                    line=10,
                    end_line=12,
                    severity=Severity.WARNING,
                    category="bug",
                    title="Off by one",
                    body="Loop overruns",
                    confidence=0.9,
                ),
                ReviewComment(
                    path="src/b.py",
                    line=3,
                    end_line=None,
                    severity=Severity.SUGGESTION,
                    category="style",
                    title="Naming",
                    body="Rename",
                    confidence=0.8,
                ),
            ],
            summary="Two findings",
        )

    @pytest.mark.asyncio
    async def test_inline_comments_then_summary(self):
        posted: list[dict] = []

        def handler(method, url, **kw):
            assert (method, url) == ("POST", f"{REPO}/pullrequests/7/comments")
            posted.append(kw["json"])
            return _FakeResp(status=201, json_data={"id": 100 + len(posted)})

        ctx, _ = _patch(handler)
        with ctx:
            ids = await BitbucketProvider("tok").post_review(_pr_info(), self._result())
        assert ids == [101, 102]
        assert posted[0]["inline"] == {"path": "src/a.py", "to": 12}
        assert posted[1]["inline"] == {"path": "src/b.py", "to": 3}
        assert "inline" not in posted[2]
        assert "Two findings" in posted[2]["content"]["raw"]

    @pytest.mark.asyncio
    async def test_rejected_anchor_falls_back_to_plain_comment(self):
        posted: list[dict] = []

        def handler(method, url, **kw):
            body = kw["json"]
            if "inline" in body and body["inline"]["path"] == "src/b.py":
                return _FakeResp(status=400, json_data={"error": "bad anchor"})
            posted.append(body)
            return _FakeResp(status=201, json_data={"id": len(posted)})

        ctx, _ = _patch(handler)
        with ctx:
            ids = await BitbucketProvider("tok").post_review(_pr_info(), self._result())
        assert ids[1] == 0
        assert any("**`src/b.py:3`**" in p["content"]["raw"] for p in posted)


def _comments_page(*comments):
    return {"values": list(comments), "next": None}


class TestThreads:
    COMMENTS = (
        {
            "id": 1,
            "content": {"raw": "bot finding"},
            "user": BOT,
            "inline": {"path": "a.py", "to": 5},
        },
        {
            "id": 2,
            "content": {"raw": "resolved finding"},
            "user": BOT,
            "inline": {"path": "a.py", "to": 9, "outdated": True},
            "resolution": {"type": "resolved"},
        },
        {
            "id": 3,
            "content": {"raw": "human note"},
            "user": ALICE,
            "inline": {"path": "b.py", "from": 4},
        },
        {"id": 4, "content": {"raw": "reply"}, "user": ALICE, "parent": {"id": 1}},
        {"id": 5, "content": {"raw": "deeper"}, "user": BOT, "parent": {"id": 4}},
        {"id": 6, "content": {"raw": "gone"}, "user": BOT, "deleted": True, "inline": {}},
    )

    def _handler(self, method, url, **kw):
        if url == f"{API}/user":
            return _FakeResp(json_data=BOT)
        if url.startswith(f"{REPO}/pullrequests/7/comments?"):
            return _FakeResp(json_data=_comments_page(*self.COMMENTS))
        raise AssertionError(url)

    @pytest.mark.asyncio
    async def test_bot_threads_are_root_inline_comments_by_bot(self):
        ctx, _ = _patch(self._handler)
        with ctx:
            threads = await BitbucketProvider("tok").get_all_bot_threads(_pr_info())
        assert [(t.thread_id, t.line, t.is_resolved, t.is_outdated) for t in threads] == [
            ("1", 5, False, False),
            ("2", 9, True, True),
        ]

    @pytest.mark.asyncio
    async def test_unresolved_excludes_resolved(self):
        ctx, _ = _patch(self._handler)
        with ctx:
            threads = await BitbucketProvider("tok").get_unresolved_bot_threads(_pr_info())
        assert [t.thread_id for t in threads] == ["1"]

    @pytest.mark.asyncio
    async def test_thread_id_walks_to_root(self):
        ctx, _ = _patch(self._handler)
        with ctx:
            provider = BitbucketProvider("tok")
            assert await provider.get_thread_id_for_comment("5", _pr_info()) == "1"
            assert await provider.get_thread_id_for_comment("1", _pr_info()) == "1"
            assert await provider.get_thread_id_for_comment("999", _pr_info()) is None

    @pytest.mark.asyncio
    async def test_human_review_comments(self):
        ctx, _ = _patch(self._handler)
        with ctx:
            out = await BitbucketProvider("tok").get_human_review_comments(_pr_info(), "mira")
        assert [(c.path, c.line, c.author) for c in out] == [("b.py", 4, "alice")]

    @pytest.mark.asyncio
    async def test_find_bot_comment_skips_inline_and_replies(self):
        comments = (
            {"id": 1, "content": {"raw": "<!-- m -->"}, "inline": {"path": "a", "to": 1}},
            {"id": 2, "content": {"raw": "<!-- m -->"}, "parent": {"id": 1}},
            {"id": 3, "content": {"raw": "walkthrough <!-- m -->"}},
        )

        def handler(method, url, **kw):
            return _FakeResp(json_data=_comments_page(*comments))

        ctx, _ = _patch(handler)
        with ctx:
            assert await BitbucketProvider("tok").find_bot_comment(_pr_info(), "<!-- m -->") == 3

    @pytest.mark.asyncio
    async def test_resolve_and_reply(self):
        def handler(method, url, **kw):
            return _FakeResp(status=200, json_data={"id": 9})

        ctx, calls = _patch(handler)
        with ctx:
            provider = BitbucketProvider("tok")
            assert await provider.resolve_threads(_pr_info(), ["1", "2"]) == 2
            await provider.reply_to_review_comment(_pr_info(), 1, "thanks")
        assert calls[0][:2] == ("POST", f"{REPO}/pullrequests/7/comments/1/resolve")
        assert calls[2][2]["json"] == {"content": {"raw": "thanks"}, "parent": {"id": 1}}


class TestGetRepoPermission:
    URL = f"{API}/workspaces/acme/permissions/repositories/widgets"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raw", "normalized"),
        [("admin", "write"), ("write", "write"), ("read", "read"), ("none", "none")],
    )
    async def test_normalizes(self, raw, normalized):
        def handler(method, url, **kw):
            assert url.startswith(self.URL)
            return _FakeResp(json_data=_comments_page({"permission": raw, "user": ALICE}))

        ctx, _ = _patch(handler)
        with ctx:
            perm = await BitbucketProvider("tok").get_repo_permission("acme", "widgets", "Alice")
        assert perm == normalized

    @pytest.mark.asyncio
    async def test_user_without_grant_is_none(self):
        def handler(method, url, **kw):
            return _FakeResp(json_data=_comments_page({"permission": "write", "user": BOT}))

        ctx, _ = _patch(handler)
        with ctx:
            perm = await BitbucketProvider("tok").get_repo_permission("acme", "widgets", "alice")
        assert perm == "none"

    @pytest.mark.asyncio
    async def test_forbidden_raises_actionable_error(self):
        def handler(method, url, **kw):
            return _FakeResp(status=403, json_data={})

        ctx, _ = _patch(handler)
        with ctx, pytest.raises(ProviderError, match="allowed_users"):
            await BitbucketProvider("tok").get_repo_permission("acme", "widgets", "alice")


class TestDescriptionAndLabels:
    @pytest.mark.asyncio
    async def test_update_description_sends_title(self):
        def handler(method, url, **kw):
            if method == "GET":
                return _FakeResp(json_data={"title": "Current title", "description": "old"})
            return _FakeResp(json_data={})

        ctx, calls = _patch(handler)
        with ctx:
            await BitbucketProvider("tok").update_pr_description(_pr_info(), "new")
        assert calls[-1][0] == "PUT"
        assert calls[-1][2]["json"] == {"title": "Current title", "description": "new"}

    @pytest.mark.asyncio
    async def test_labels_live_in_app_state(self):
        store: dict[str, str] = {}

        class _DB:
            def get_setting(self, key):
                return store.get(key)

            def set_setting(self, key, value):
                store[key] = value

        from mira.platforms.bitbucket import state

        with patch.object(state, "_db", lambda: _DB()):
            provider = BitbucketProvider("tok")
            await provider.add_label(_pr_info(), "mira-paused")
            assert state.get_labels("acme", "widgets", 7) == ["mira-paused"]
            await provider.remove_label(_pr_info(), "mira-paused")
            assert state.get_labels("acme", "widgets", 7) == []
            assert state.swap_head("acme", "widgets", 7, "h1") == ""
            assert state.swap_head("acme", "widgets", 7, "h2") == "h1"


# ── fetcher ─────────────────────────────────────────────────────────


class TestFetcher:
    def test_make_fetcher(self):
        assert isinstance(make_fetcher("bitbucket", "tok"), BitbucketRepoFetcher)

    @pytest.mark.asyncio
    async def test_default_branch(self):
        def handler(method, url, **kw):
            return _FakeResp(json_data={"mainbranch": {"name": "develop"}})

        ctx, _ = _patch(handler, "mira.platforms.fetch")
        with ctx:
            assert await BitbucketRepoFetcher("tok").default_branch("acme", "widgets") == "develop"

    @pytest.mark.asyncio
    async def test_repo_tree_follows_next_and_keeps_files(self):
        page2 = f"{REPO}/src/main/?page=2"

        def handler(method, url, **kw):
            if url == page2:
                return _FakeResp(json_data={"values": [{"type": "commit_file", "path": "b/c.py"}]})
            assert url == f"{REPO}/src/main/?max_depth=100&pagelen=100"
            return _FakeResp(
                json_data={
                    "values": [
                        {"type": "commit_directory", "path": "b"},
                        {"type": "commit_file", "path": "a.py"},
                    ],
                    "next": page2,
                }
            )

        ctx, _ = _patch(handler, "mira.platforms.fetch")
        with ctx:
            paths = await BitbucketRepoFetcher("tok").repo_tree("acme", "widgets", "main")
        assert paths == ["a.py", "b/c.py"]

    @pytest.mark.asyncio
    async def test_empty_repo(self):
        def handler(method, url, **kw):
            return _FakeResp(status=404)

        ctx, _ = _patch(handler, "mira.platforms.fetch")
        with ctx, pytest.raises(EmptyRepoError):
            await BitbucketRepoFetcher("tok").repo_tree("acme", "widgets", "main")

    @pytest.mark.asyncio
    async def test_file_content_and_tarball_url(self):
        def handler(method, url, **kw):
            if "/src/" in url:
                assert url == f"{REPO}/src/main/dir/my%20file.py"
                return _FakeResp(text="print(1)")
            assert url == "https://bitbucket.org/acme/widgets/get/main.tar.gz"
            return _FakeResp(status=403)

        ctx, _ = _patch(handler, "mira.platforms.fetch")
        with ctx:
            fetcher = BitbucketRepoFetcher("tok")
            assert await fetcher.file_content("acme", "widgets", "dir/my file.py", "main") == (
                "print(1)"
            )
            assert await fetcher.repo_tarball("acme", "widgets", "main") is None


class TestFileHistory:
    @pytest.mark.asyncio
    async def test_uses_include_param_and_maps_entries(self):
        def handler(method, url, **kw):
            assert url == f"{REPO}/commits"
            assert kw["params"] == {"include": "abc123", "path": "a.py", "pagelen": 2}
            return _FakeResp(
                json_data={
                    "values": [
                        {
                            "hash": "0123456789ab",
                            "message": "Fix bug\n\nlong body",
                            "date": "2026-10-01T00:00:00+00:00",
                            "author": {"raw": "Alice <a@x>", "user": ALICE},
                        },
                        {"hash": "fedcba987654", "message": "Init", "author": {"raw": "Bob <b@x>"}},
                    ]
                }
            )

        ctx, _ = _patch(handler)
        with ctx:
            hist = await BitbucketProvider("tok").get_file_history(_pr_info(), ["a.py"], 2)
        assert [(e.sha, e.message, e.author) for e in hist["a.py"]] == [
            ("01234567", "Fix bug", "alice"),
            ("fedcba98", "Init", "Bob <b@x>"),
        ]


class TestFetcherTreeCompleteness:
    """Mirrors the incomplete-tree guards on the other platforms' fetchers."""

    FIRST = f"{REPO}/src/main/?max_depth=100&pagelen=100"

    async def _tree(self, pages):
        def handler(method, url, **kw):
            return pages[url]

        ctx, _ = _patch(handler, "mira.platforms.fetch")
        with ctx:
            return await BitbucketRepoFetcher("tok").repo_tree("acme", "widgets", "main")

    @pytest.mark.asyncio
    async def test_submodule_links_are_not_files(self):
        page = {
            "values": [
                {"type": "commit_file", "path": "a.py"},
                {"type": "commit_link", "path": "vendor/lib"},
            ]
        }
        assert await self._tree({self.FIRST: _FakeResp(json_data=page)}) == ["a.py"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "page",
        [
            [],
            {"values": "nope"},
            {"values": [{"type": "commit_file"}]},
            {"values": [{"type": "mystery", "path": "x"}]},
            {
                "values": [
                    {"type": "commit_file", "path": "a"},
                    {"type": "commit_file", "path": "a"},
                ]
            },
        ],
    )
    async def test_malformed_page_rejected(self, page):
        from mira.platforms.fetch import IncompleteTreeError

        with pytest.raises(IncompleteTreeError):
            await self._tree({self.FIRST: _FakeResp(json_data=page)})

    @pytest.mark.asyncio
    async def test_cyclic_pagination_rejected(self):
        from mira.platforms.fetch import IncompleteTreeError

        page = {"values": [{"type": "commit_file", "path": "a"}], "next": self.FIRST}
        with pytest.raises(IncompleteTreeError, match="cyclic"):
            await self._tree({self.FIRST: _FakeResp(json_data=page)})

    @pytest.mark.asyncio
    async def test_foreign_next_link_rejected(self):
        from mira.platforms.fetch import IncompleteTreeError

        page = {
            "values": [{"type": "commit_file", "path": "a"}],
            "next": "https://evil.test/2.0/repositories/acme/widgets/src/main/?page=2",
        }
        with pytest.raises(IncompleteTreeError, match="target"):
            await self._tree({self.FIRST: _FakeResp(json_data=page)})

    @pytest.mark.asyncio
    async def test_empty_page_with_next_rejected(self):
        from mira.platforms.fetch import IncompleteTreeError

        page = {"values": [], "next": f"{REPO}/src/main/?page=2"}
        with pytest.raises(IncompleteTreeError, match="progress"):
            await self._tree({self.FIRST: _FakeResp(json_data=page)})
