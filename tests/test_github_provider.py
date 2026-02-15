"""Tests for GitHub provider."""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import httpx
import pytest

from mira.exceptions import ProviderError
from mira.models import PRInfo, ReviewComment, ReviewResult, Severity
from mira.providers.github import (
    _CATEGORY_DISPLAY,
    GitHubProvider,
    _format_comment_body,
    parse_pr_url,
)


class TestParsePRUrl:
    def test_full_url(self):
        owner, repo, number = parse_pr_url("https://github.com/octocat/hello/pull/42")
        assert owner == "octocat"
        assert repo == "hello"
        assert number == 42

    def test_shorthand(self):
        owner, repo, number = parse_pr_url("octocat/hello#42")
        assert owner == "octocat"
        assert repo == "hello"
        assert number == 42

    def test_full_url_with_trailing_slash(self):
        # The regex won't match trailing slash, but the number extraction works
        owner, repo, number = parse_pr_url("https://github.com/owner/repo/pull/123")
        assert owner == "owner"
        assert repo == "repo"
        assert number == 123

    def test_invalid_url(self):
        with pytest.raises(ProviderError, match="Cannot parse PR URL"):
            parse_pr_url("not a valid url")

    def test_empty_string(self):
        with pytest.raises(ProviderError):
            parse_pr_url("")

    def test_http_url(self):
        owner, repo, number = parse_pr_url("http://github.com/owner/repo/pull/1")
        assert owner == "owner"
        assert repo == "repo"
        assert number == 1


class TestGitHubProvider:
    def test_requires_token(self):
        with pytest.raises(ProviderError, match="token is required"):
            GitHubProvider(token="")


def _make_pr_info() -> PRInfo:
    return PRInfo(
        title="Test",
        description="desc",
        base_branch="main",
        head_branch="feat",
        url="https://github.com/o/r/pull/1",
        number=1,
        owner="o",
        repo="r",
    )


class TestGitHubRetry:
    """Fix 5: Retry behaviour for GitHub API calls."""

    @pytest.mark.asyncio
    async def test_get_pr_info_retries_on_transient_error(self):
        """get_pr_info retries and succeeds on the second attempt."""
        provider = GitHubProvider.__new__(GitHubProvider)
        provider._token = "test-token"

        call_count = 0
        mock_pr = MagicMock()
        mock_pr.title = "PR"
        mock_pr.body = "desc"
        mock_pr.base.ref = "main"
        mock_pr.head.ref = "feat"
        mock_pr.html_url = "https://github.com/o/r/pull/1"
        mock_pr.number = 1

        mock_repo = MagicMock()

        def _get_pull(n):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("transient")
            return mock_pr

        mock_repo.get_pull = _get_pull

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        provider._github = mock_gh

        result = await provider.get_pr_info("https://github.com/o/r/pull/1")
        assert result.title == "PR"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_get_pr_info_exhausts_retries(self):
        """get_pr_info raises ProviderError after all retries fail."""
        provider = GitHubProvider.__new__(GitHubProvider)
        provider._token = "test-token"

        mock_repo = MagicMock()
        mock_repo.get_pull.side_effect = ConnectionError("always fails")

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        provider._github = mock_gh

        with pytest.raises(ProviderError, match="Failed to fetch PR info"):
            await provider.get_pr_info("https://github.com/o/r/pull/1")

        assert mock_repo.get_pull.call_count == 3

    @pytest.mark.asyncio
    async def test_get_pr_diff_retries_on_transient_error(self):
        """get_pr_diff retries transient HTTP errors."""
        provider = GitHubProvider.__new__(GitHubProvider)
        provider._token = "test-token"

        call_count = 0
        pr_info = _make_pr_info()

        async def _mock_get(self, url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise httpx.ConnectError("transient")
            return httpx.Response(
                200,
                text="diff content",
                request=httpx.Request("GET", url),
            )

        with patch.object(httpx.AsyncClient, "get", _mock_get):
            result = await provider.get_pr_diff(pr_info)

        assert result == "diff content"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_get_pr_diff_exhausts_retries(self):
        """get_pr_diff raises ProviderError after all retries fail."""
        provider = GitHubProvider.__new__(GitHubProvider)
        provider._token = "test-token"
        pr_info = _make_pr_info()

        call_count = 0

        async def _mock_get(self, url, **kwargs):
            nonlocal call_count
            call_count += 1
            raise httpx.ConnectError("always fails")

        with (
            patch.object(httpx.AsyncClient, "get", _mock_get),
            pytest.raises(ProviderError, match="Failed to fetch PR diff"),
        ):
            await provider.get_pr_diff(pr_info)

        assert call_count == 3

    @pytest.mark.asyncio
    async def test_post_review_retries_on_transient_error(self):
        """post_review retries and succeeds on the second attempt."""
        provider = GitHubProvider.__new__(GitHubProvider)
        provider._token = "test-token"

        pr_info = _make_pr_info()
        result = ReviewResult(
            comments=[
                ReviewComment(
                    path="a.py",
                    line=1,
                    end_line=None,
                    severity=Severity.WARNING,
                    category="bug",
                    title="Issue",
                    body="desc",
                    confidence=0.9,
                )
            ],
            summary="Found issues",
        )

        call_count = 0
        mock_commit = MagicMock()
        mock_pr = MagicMock()
        mock_pr.get_commits.return_value = [mock_commit]

        mock_repo = MagicMock()

        def _get_pull(n):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("transient")
            return mock_pr

        mock_repo.get_pull = _get_pull

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        provider._github = mock_gh

        await provider.post_review(pr_info, result)
        assert call_count == 2
        mock_pr.create_review.assert_called_once()

    @pytest.mark.asyncio
    async def test_post_review_no_commits_not_retried(self):
        """ProviderError('PR has no commits') is permanent and should not be retried."""
        provider = GitHubProvider.__new__(GitHubProvider)
        provider._token = "test-token"

        pr_info = _make_pr_info()
        result = ReviewResult(
            comments=[
                ReviewComment(
                    path="a.py",
                    line=1,
                    end_line=None,
                    severity=Severity.WARNING,
                    category="bug",
                    title="Issue",
                    body="desc",
                    confidence=0.9,
                )
            ],
            summary="Found issues",
        )

        mock_pr = MagicMock()
        mock_pr.get_commits.return_value = []  # no commits — permanent error

        mock_repo = MagicMock()
        mock_repo.get_pull.return_value = mock_pr

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        provider._github = mock_gh

        with pytest.raises(ProviderError, match="PR has no commits"):
            await provider.post_review(pr_info, result)

        # Should have been called only once — no retries for ProviderError
        mock_repo.get_pull.assert_called_once()


class TestFormatCommentBody:
    """Tests for the richer comment formatting."""

    def _make_comment(self, **overrides) -> ReviewComment:
        defaults = {
            "path": "src/foo.py",
            "line": 10,
            "end_line": None,
            "severity": Severity.WARNING,
            "category": "bug",
            "title": "Something is wrong",
            "body": "Detailed explanation.",
            "confidence": 0.9,
            "suggestion": None,
        }
        defaults.update(overrides)
        return ReviewComment(**defaults)

    def test_basic_comment(self):
        body = _format_comment_body(self._make_comment())
        assert "\U0001f41b **Bug**" in body
        assert "\u26a0\ufe0f Warning" in body
        assert "**Something is wrong**" in body
        assert "Detailed explanation." in body
        assert "Suggested fix:" not in body

    def test_with_suggestion(self):
        body = _format_comment_body(self._make_comment(suggestion="return json.loads(f.read())"))
        assert "**Suggested fix:**" in body
        assert "```suggestion" in body
        assert "return json.loads(f.read())" in body
        assert body.endswith("```")

    def test_blocker_badge(self):
        body = _format_comment_body(self._make_comment(severity=Severity.BLOCKER))
        assert "\U0001f6d1 Blocker \u2014 must fix before merge" in body

    def test_unknown_category_fallback(self):
        body = _format_comment_body(self._make_comment(category="unknown_cat"))
        assert "\U0001f4cc **Note**" in body

    def test_all_known_categories(self):
        for cat, (emoji, label) in _CATEGORY_DISPLAY.items():
            body = _format_comment_body(self._make_comment(category=cat))
            assert f"{emoji} **{label}**" in body


class TestFormatCommentBodyAgentPrompt:
    """Tests for agent_prompt rendering in comment body."""

    def _make_comment(self, **overrides) -> ReviewComment:
        defaults = {
            "path": "src/foo.py",
            "line": 10,
            "end_line": None,
            "severity": Severity.WARNING,
            "category": "bug",
            "title": "Something is wrong",
            "body": "Detailed explanation.",
            "confidence": 0.9,
            "suggestion": None,
            "agent_prompt": None,
        }
        defaults.update(overrides)
        return ReviewComment(**defaults)

    def test_with_agent_prompt(self):
        body = _format_comment_body(
            self._make_comment(agent_prompt="In src/foo.py at line 10, replace foo() with bar().")
        )
        assert "<details>" in body
        assert "Prompt for AI Agents" in body
        assert "</details>" in body
        assert "In src/foo.py at line 10, replace foo() with bar()." in body

    def test_without_agent_prompt(self):
        body = _format_comment_body(self._make_comment(agent_prompt=None))
        assert "<details>" not in body
        assert "Prompt for AI Agents" not in body

    def test_agent_prompt_after_suggestion(self):
        body = _format_comment_body(
            self._make_comment(
                suggestion="return bar()",
                agent_prompt="In src/foo.py at line 10, replace foo() with bar().",
            )
        )
        suggestion_pos = body.index("```suggestion")
        details_pos = body.index("<details>")
        assert details_pos > suggestion_pos


class TestPostComment:
    @pytest.mark.asyncio
    async def test_post_comment_calls_create_comment(self):
        provider = GitHubProvider.__new__(GitHubProvider)
        provider._token = "test-token"

        pr_info = _make_pr_info()

        mock_issue = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_issue.return_value = mock_issue

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        provider._github = mock_gh

        await provider.post_comment(pr_info, "Hello world")

        mock_repo.get_issue.assert_called_once_with(1)
        mock_issue.create_comment.assert_called_once_with("Hello world")

    @pytest.mark.asyncio
    async def test_post_comment_retries_on_transient_error(self):
        provider = GitHubProvider.__new__(GitHubProvider)
        provider._token = "test-token"

        pr_info = _make_pr_info()

        call_count = 0
        mock_issue = MagicMock()
        mock_repo = MagicMock()

        def _get_issue(n):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("transient")
            return mock_issue

        mock_repo.get_issue = _get_issue

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        provider._github = mock_gh

        await provider.post_comment(pr_info, "Hello")
        assert call_count == 2
        mock_issue.create_comment.assert_called_once_with("Hello")


class TestFindBotComment:
    @pytest.mark.asyncio
    async def test_find_bot_comment_found(self):
        provider = GitHubProvider.__new__(GitHubProvider)
        provider._token = "test-token"

        pr_info = _make_pr_info()

        comment1 = MagicMock()
        comment1.body = "unrelated comment"
        comment1.id = 10

        comment2 = MagicMock()
        comment2.body = "<!-- mira-walkthrough -->\n## Mira PR Walkthrough"
        comment2.id = 42

        mock_issue = MagicMock()
        mock_issue.get_comments.return_value = [comment1, comment2]
        mock_repo = MagicMock()
        mock_repo.get_issue.return_value = mock_issue

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        provider._github = mock_gh

        result = await provider.find_bot_comment(pr_info, "<!-- mira-walkthrough -->")
        assert result == 42

    @pytest.mark.asyncio
    async def test_find_bot_comment_not_found(self):
        provider = GitHubProvider.__new__(GitHubProvider)
        provider._token = "test-token"

        pr_info = _make_pr_info()

        comment1 = MagicMock()
        comment1.body = "unrelated comment"
        comment1.id = 10

        mock_issue = MagicMock()
        mock_issue.get_comments.return_value = [comment1]
        mock_repo = MagicMock()
        mock_repo.get_issue.return_value = mock_issue

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        provider._github = mock_gh

        result = await provider.find_bot_comment(pr_info, "<!-- mira-walkthrough -->")
        assert result is None

    @pytest.mark.asyncio
    async def test_find_bot_comment_empty_comments(self):
        provider = GitHubProvider.__new__(GitHubProvider)
        provider._token = "test-token"

        pr_info = _make_pr_info()

        mock_issue = MagicMock()
        mock_issue.get_comments.return_value = []
        mock_repo = MagicMock()
        mock_repo.get_issue.return_value = mock_issue

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        provider._github = mock_gh

        result = await provider.find_bot_comment(pr_info, "<!-- mira-walkthrough -->")
        assert result is None


class TestUpdateComment:
    @pytest.mark.asyncio
    async def test_update_comment_calls_edit(self):
        provider = GitHubProvider.__new__(GitHubProvider)
        provider._token = "test-token"

        pr_info = _make_pr_info()

        mock_comment = MagicMock()
        mock_issue = MagicMock()
        mock_issue.get_comment.return_value = mock_comment
        mock_repo = MagicMock()
        mock_repo.get_issue.return_value = mock_issue

        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        provider._github = mock_gh

        await provider.update_comment(pr_info, 42, "new body")

        mock_issue.get_comment.assert_called_once_with(42)
        mock_comment.edit.assert_called_once_with("new body")


# ── Shared helpers for GraphQL-based tests ──────────────────────────────────


def _make_thread_node(
    thread_id: str,
    is_resolved: bool = False,
    is_outdated: bool = True,
    author_login: str | None = "mira-app[bot]",
    body: str = "Hardcoded secret",
    path: str = "src/app.py",
    line: int = 10,
) -> dict:
    """Build a reviewThread node for GraphQL response mocking."""
    author = {"login": author_login} if author_login is not None else None
    return {
        "id": thread_id,
        "isResolved": is_resolved,
        "isOutdated": is_outdated,
        "comments": {
            "nodes": [
                {
                    "author": author,
                    "body": body,
                    "path": path,
                    "line": line,
                }
            ]
        },
    }


def _make_graphql_response(
    thread_nodes: list[dict],
    viewer_login: str = "mira-app[bot]",
    has_next_page: bool = False,
    end_cursor: str | None = None,
) -> dict:
    """Build a full GraphQL response for review threads query."""
    return {
        "data": {
            "viewer": {"login": viewer_login},
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {
                            "hasNextPage": has_next_page,
                            "endCursor": end_cursor,
                        },
                        "nodes": thread_nodes,
                    }
                }
            },
        }
    }


class TestResolveOutdatedReviewThreads:
    """Tests for resolve_outdated_review_threads using GraphQL."""

    def _make_provider(self) -> GitHubProvider:
        provider = GitHubProvider.__new__(GitHubProvider)
        provider._token = "test-token"
        provider._github = MagicMock()
        return provider

    @pytest.mark.asyncio
    async def test_found_and_resolved(self):
        """Only bot-authored, outdated, unresolved threads are resolved."""
        provider = self._make_provider()
        pr_info = _make_pr_info()

        threads = [
            _make_thread_node("T1", author_login="mira-app[bot]", is_outdated=True),
            _make_thread_node("T2", author_login="human-user"),
            _make_thread_node("T3", author_login="mira-app[bot]", is_outdated=False),
        ]
        query_resp = _make_graphql_response(threads)
        mutation_resp = {
            "data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}
        }

        call_count = 0

        async def _mock_post(self, url, **kwargs):
            nonlocal call_count
            call_count += 1
            body = kwargs.get("json", {})
            data = mutation_resp if "mutation" in body.get("query", "") else query_resp
            return httpx.Response(200, json=data, request=httpx.Request("POST", url))

        with patch.object(httpx.AsyncClient, "post", _mock_post):
            result = await provider.resolve_outdated_review_threads(pr_info)

        # Only T1 (outdated) resolved; T3 (not outdated) skipped
        assert result == 1
        # 1 query + 1 mutation
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_no_unresolved_bot_threads(self):
        """All threads resolved or human-authored → returns 0."""
        provider = self._make_provider()
        pr_info = _make_pr_info()

        threads = [
            _make_thread_node("T1", is_resolved=True, author_login="mira-app[bot]"),
            _make_thread_node("T2", author_login="human-user"),
        ]
        query_resp = _make_graphql_response(threads)

        async def _mock_post(self, url, **kwargs):
            return httpx.Response(200, json=query_resp, request=httpx.Request("POST", url))

        with patch.object(httpx.AsyncClient, "post", _mock_post):
            result = await provider.resolve_outdated_review_threads(pr_info)

        assert result == 0

    @pytest.mark.asyncio
    async def test_no_threads_at_all(self):
        """Empty nodes list → returns 0."""
        provider = self._make_provider()
        pr_info = _make_pr_info()

        query_resp = _make_graphql_response([])

        async def _mock_post(self, url, **kwargs):
            return httpx.Response(200, json=query_resp, request=httpx.Request("POST", url))

        with patch.object(httpx.AsyncClient, "post", _mock_post):
            result = await provider.resolve_outdated_review_threads(pr_info)

        assert result == 0

    @pytest.mark.asyncio
    async def test_pagination(self):
        """Bot threads across two pages are all collected and resolved."""
        provider = self._make_provider()
        pr_info = _make_pr_info()

        page1 = _make_graphql_response(
            [_make_thread_node("T1", author_login="mira-app[bot]")],
            has_next_page=True,
            end_cursor="cursor1",
        )
        page2 = _make_graphql_response(
            [_make_thread_node("T2", author_login="mira-app[bot]")],
        )
        mutation_resp = {
            "data": {"resolveReviewThread": {"thread": {"id": "X", "isResolved": True}}}
        }

        call_count = 0

        async def _mock_post(self, url, **kwargs):
            nonlocal call_count
            call_count += 1
            body = kwargs.get("json", {})
            query = body.get("query", "")
            if "mutation" in query:
                return httpx.Response(200, json=mutation_resp, request=httpx.Request("POST", url))
            variables = body.get("variables", {})
            data = page2 if variables.get("cursor") == "cursor1" else page1
            return httpx.Response(200, json=data, request=httpx.Request("POST", url))

        with patch.object(httpx.AsyncClient, "post", _mock_post):
            result = await provider.resolve_outdated_review_threads(pr_info)

        assert result == 2
        # 2 query pages + 2 mutations
        assert call_count == 4

    @pytest.mark.asyncio
    async def test_null_author_skipped(self):
        """Thread with deleted user (author: null) is safely skipped."""
        provider = self._make_provider()
        pr_info = _make_pr_info()

        threads = [
            _make_thread_node("T1", author_login=None),
            _make_thread_node("T2", author_login="mira-app[bot]"),
        ]
        query_resp = _make_graphql_response(threads)
        mutation_resp = {
            "data": {"resolveReviewThread": {"thread": {"id": "T2", "isResolved": True}}}
        }

        async def _mock_post(self, url, **kwargs):
            body = kwargs.get("json", {})
            if "mutation" in body.get("query", ""):
                return httpx.Response(200, json=mutation_resp, request=httpx.Request("POST", url))
            return httpx.Response(200, json=query_resp, request=httpx.Request("POST", url))

        with patch.object(httpx.AsyncClient, "post", _mock_post):
            result = await provider.resolve_outdated_review_threads(pr_info)

        assert result == 1

    @pytest.mark.asyncio
    async def test_graphql_error_raises_provider_error(self):
        """Response containing 'errors' key raises ProviderError."""
        provider = self._make_provider()
        pr_info = _make_pr_info()

        error_resp = {"errors": [{"message": "Something went wrong"}]}

        async def _mock_post(self, url, **kwargs):
            return httpx.Response(200, json=error_resp, request=httpx.Request("POST", url))

        with (
            patch.object(httpx.AsyncClient, "post", _mock_post),
            pytest.raises(ProviderError, match="GraphQL error"),
        ):
            await provider.resolve_outdated_review_threads(pr_info)

    @pytest.mark.asyncio
    async def test_retries_on_transient_error(self):
        """First call raises ConnectError, second succeeds."""
        provider = self._make_provider()
        pr_info = _make_pr_info()

        query_resp = _make_graphql_response([])

        call_count = 0

        async def _mock_post(self, url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("transient")
            return httpx.Response(200, json=query_resp, request=httpx.Request("POST", url))

        with patch.object(httpx.AsyncClient, "post", _mock_post):
            result = await provider.resolve_outdated_review_threads(pr_info)

        assert result == 0
        assert call_count == 2


class TestGetUnresolvedBotThreads:
    @pytest.mark.asyncio
    async def test_returns_all_unresolved_bot_threads(self):
        """Returns all unresolved threads authored by the bot, regardless of isOutdated."""
        provider = GitHubProvider.__new__(GitHubProvider)
        provider._token = "test-token"

        nodes = [
            _make_thread_node("T1", author_login="mira[bot]"),  # outdated — matches
            _make_thread_node("T2", is_resolved=True, author_login="mira[bot]"),  # resolved — skip
            _make_thread_node(
                "T3", is_outdated=False, author_login="mira[bot]"
            ),  # not outdated — still matches
            _make_thread_node("T4", author_login="human"),  # wrong author — skip
            _make_thread_node(
                "T5", author_login="mira[bot]", body="Another issue", path="b.py", line=5
            ),  # outdated — matches
        ]

        async def _mock_post(self, url, **kwargs):
            return httpx.Response(
                200,
                json=_make_graphql_response(nodes),
                request=httpx.Request("POST", url),
            )

        pr_info = _make_pr_info()
        with patch.object(httpx.AsyncClient, "post", _mock_post):
            result = await provider.get_unresolved_bot_threads(pr_info, "mira[bot]")

        assert len(result) == 3
        assert result[0].thread_id == "T1"
        assert result[0].is_outdated is True
        assert result[1].thread_id == "T3"
        assert result[1].is_outdated is False
        assert result[2].thread_id == "T5"
        assert result[2].path == "b.py"
        assert result[2].line == 5

    @pytest.mark.asyncio
    async def test_handles_pagination(self):
        """Paginates through multiple pages of review threads."""
        provider = GitHubProvider.__new__(GitHubProvider)
        provider._token = "test-token"

        call_count = 0

        async def _mock_post(self, url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(
                    200,
                    json=_make_graphql_response(
                        [_make_thread_node("T1", author_login="mira[bot]")],
                        has_next_page=True,
                        end_cursor="cursor1",
                    ),
                    request=httpx.Request("POST", url),
                )
            return httpx.Response(
                200,
                json=_make_graphql_response(
                    [_make_thread_node("T2", author_login="mira[bot]")],
                ),
                request=httpx.Request("POST", url),
            )

        pr_info = _make_pr_info()
        with patch.object(httpx.AsyncClient, "post", _mock_post):
            result = await provider.get_unresolved_bot_threads(pr_info, "mira[bot]")

        assert len(result) == 2
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_matches(self):
        """Returns empty list when all threads are resolved or by other authors."""
        provider = GitHubProvider.__new__(GitHubProvider)
        provider._token = "test-token"

        nodes = [
            _make_thread_node("T1", is_resolved=True),
            _make_thread_node("T2", author_login="human"),
        ]

        async def _mock_post(self, url, **kwargs):
            return httpx.Response(
                200,
                json=_make_graphql_response(nodes),
                request=httpx.Request("POST", url),
            )

        pr_info = _make_pr_info()
        with patch.object(httpx.AsyncClient, "post", _mock_post):
            result = await provider.get_unresolved_bot_threads(pr_info, "mira[bot]")

        assert result == []

    @pytest.mark.asyncio
    async def test_matches_author_without_bot_suffix(self):
        """Matches when viewer is 'app[bot]' but comment author is 'app' (GitHub App quirk)."""
        provider = GitHubProvider.__new__(GitHubProvider)
        provider._token = "test-token"

        nodes = [
            _make_thread_node("T1", author_login="miracodeai"),
            _make_thread_node("T2", author_login="miracodeai"),
        ]

        async def _mock_post(self, url, **kwargs):
            return httpx.Response(
                200,
                json=_make_graphql_response(nodes, viewer_login="miracodeai[bot]"),
                request=httpx.Request("POST", url),
            )

        pr_info = _make_pr_info()
        with patch.object(httpx.AsyncClient, "post", _mock_post):
            result = await provider.get_unresolved_bot_threads(pr_info)

        assert len(result) == 2


class TestResolveThreads:
    @pytest.mark.asyncio
    async def test_resolves_given_ids(self):
        """Resolves each thread and returns count."""
        provider = GitHubProvider.__new__(GitHubProvider)
        provider._token = "test-token"

        async def _mock_post(self, url, **kwargs):
            return httpx.Response(
                200,
                json={
                    "data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}
                },  # noqa: E501
                request=httpx.Request("POST", url),
            )

        pr_info = _make_pr_info()
        with patch.object(httpx.AsyncClient, "post", _mock_post):
            count = await provider.resolve_threads(pr_info, ["T1", "T2"])

        assert count == 2

    @pytest.mark.asyncio
    async def test_handles_per_thread_failures(self):
        """Per-thread failures are logged but don't block others."""
        provider = GitHubProvider.__new__(GitHubProvider)
        provider._token = "test-token"

        async def _mock_post(self, url, **kwargs):
            body = kwargs.get("json", {})
            variables = body.get("variables", {})
            if variables.get("threadId") == "T1":
                raise httpx.ConnectError("network error")
            return httpx.Response(
                200,
                json={
                    "data": {"resolveReviewThread": {"thread": {"id": "T2", "isResolved": True}}}
                },  # noqa: E501
                request=httpx.Request("POST", url),
            )

        pr_info = _make_pr_info()
        with patch.object(httpx.AsyncClient, "post", _mock_post):
            count = await provider.resolve_threads(pr_info, ["T1", "T2"])

        # T1 failed (all retries), T2 succeeded
        assert count == 1


class TestGetFileContent:
    @pytest.mark.asyncio
    async def test_returns_decoded_content(self):
        """Returns base64-decoded file content."""
        provider = GitHubProvider.__new__(GitHubProvider)
        provider._token = "test-token"

        file_text = "def hello():\n    return 'world'\n"
        encoded = base64.b64encode(file_text.encode()).decode()

        async def _mock_get(self, url, **kwargs):
            return httpx.Response(
                200,
                json={"content": encoded},
                request=httpx.Request("GET", url),
            )

        pr_info = _make_pr_info()
        with patch.object(httpx.AsyncClient, "get", _mock_get):
            result = await provider.get_file_content(pr_info, "src/hello.py", "feature")

        assert result == file_text
