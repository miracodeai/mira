"""GitHub provider using PyGithub."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from typing import Any

import httpx
from github import Github, GithubException
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from mira.exceptions import ProviderError
from mira.models import OutdatedThread, PRInfo, ReviewComment, ReviewResult, Severity
from mira.providers.base import BaseProvider

# Transient errors worth retrying — network issues and GitHub server errors.
_RETRYABLE = (ConnectionError, TimeoutError, httpx.TransportError, GithubException)

logger = logging.getLogger(__name__)

_GRAPHQL_URL = "https://api.github.com/graphql"

_REVIEW_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  viewer { login }
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo {
          hasNextPage
          endCursor
        }
        nodes {
          id
          isResolved
          isOutdated
          comments(first: 1) {
            nodes {
              author { login }
              body
              path
              line
            }
          }
        }
      }
    }
  }
}
"""

_RESOLVE_THREAD_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""

_CATEGORY_DISPLAY: dict[str, tuple[str, str]] = {
    "bug": ("\U0001f41b", "Bug"),
    "security": ("\U0001f512", "Security issue"),
    "performance": ("\u26a1", "Performance"),
    "maintainability": ("\U0001f527", "Refactor suggestion"),
    "style": ("\U0001f3a8", "Style"),
    "clarity": ("\U0001f4dd", "Clarity"),
    "configuration": ("\u2699\ufe0f", "Configuration"),
    "other": ("\U0001f4cc", "Note"),
}

_SEVERITY_BADGE: dict[Severity, str] = {
    Severity.BLOCKER: "\U0001f6d1 Blocker \u2014 must fix before merge",
    Severity.WARNING: "\u26a0\ufe0f Warning",
    Severity.SUGGESTION: "\U0001f4a1 Suggestion",
    Severity.NITPICK: "\U0001f4ac Nitpick",
}

# Matches: https://github.com/owner/repo/pull/123 or owner/repo#123
_PR_URL_PATTERN = re.compile(
    r"(?:https?://github\.com/)?(?P<owner>[^/\s]+)/(?P<repo>[^/\s#]+)(?:/pull/|#)(?P<number>\d+)"
)


def parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    """Parse a PR URL or shorthand into (owner, repo, number)."""
    match = _PR_URL_PATTERN.match(pr_url.strip())
    if not match:
        raise ProviderError(
            f"Cannot parse PR URL: {pr_url}. "
            "Expected format: https://github.com/owner/repo/pull/123 or owner/repo#123"
        )
    return match.group("owner"), match.group("repo"), int(match.group("number"))


class GitHubProvider(BaseProvider):
    """GitHub code hosting provider."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ProviderError("GitHub token is required")
        self._github = Github(token)
        self._token = token

    async def get_pr_info(self, pr_url: str) -> PRInfo:
        owner, repo, number = parse_pr_url(pr_url)

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            retry=retry_if_exception_type(_RETRYABLE),
            reraise=True,
        )
        def _fetch() -> PRInfo:
            gh_repo = self._github.get_repo(f"{owner}/{repo}")
            pr = gh_repo.get_pull(number)
            return PRInfo(
                title=pr.title or "",
                description=pr.body or "",
                base_branch=pr.base.ref,
                head_branch=pr.head.ref,
                url=pr.html_url,
                number=pr.number,
                owner=owner,
                repo=repo,
            )

        try:
            return await asyncio.to_thread(_fetch)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to fetch PR info: {e}") from e

    async def get_pr_diff(self, pr_info: PRInfo) -> str:
        diff_url = (
            f"https://api.github.com/repos/{pr_info.owner}/{pr_info.repo}/pulls/{pr_info.number}"
        )
        headers = {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github.v3.diff",
        }

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            retry=retry_if_exception_type(_RETRYABLE),
            reraise=True,
        )
        async def _fetch_diff() -> str:
            async with httpx.AsyncClient() as client:
                resp = await client.get(diff_url, headers=headers, follow_redirects=True)
                resp.raise_for_status()
                return resp.text

        try:
            return await _fetch_diff()
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to fetch PR diff: {e}") from e

    async def post_review(
        self,
        pr_info: PRInfo,
        result: ReviewResult,
    ) -> None:
        if not result.comments:
            return

        # Build inline comments (no retry needed for local formatting)
        review_comments: list[dict[str, str | int]] = []
        for comment in result.comments:
            body = _format_comment_body(comment)
            rc: dict[str, str | int] = {
                "path": comment.path,
                "body": body,
            }
            # PyGithub uses 'line' for single-line, 'start_line'+'line' for multi-line
            if comment.end_line and comment.end_line > comment.line:
                rc["start_line"] = comment.line
                rc["line"] = comment.end_line
            else:
                rc["line"] = comment.line

            review_comments.append(rc)

        review_body = ""
        if result.summary:
            review_body = f"**Mira Review Summary**\n\n{result.summary}"

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            retry=retry_if_exception_type(_RETRYABLE),
            reraise=True,
        )
        def _post() -> None:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            pr = gh_repo.get_pull(pr_info.number)

            commits = list(pr.get_commits())
            if not commits:
                raise ProviderError("PR has no commits")
            latest_commit = commits[-1]

            pr.create_review(
                commit=latest_commit,
                body=review_body,
                event="COMMENT",
                comments=review_comments,  # type: ignore[arg-type]
            )

        try:
            await asyncio.to_thread(_post)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to post review: {e}") from e

    async def post_comment(self, pr_info: PRInfo, body: str) -> None:
        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            retry=retry_if_exception_type(_RETRYABLE),
            reraise=True,
        )
        def _post_comment() -> None:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            issue = gh_repo.get_issue(pr_info.number)
            issue.create_comment(body)

        try:
            await asyncio.to_thread(_post_comment)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to post comment: {e}") from e

    async def find_bot_comment(self, pr_info: PRInfo, marker: str) -> int | None:
        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            retry=retry_if_exception_type(_RETRYABLE),
            reraise=True,
        )
        def _find() -> int | None:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            issue = gh_repo.get_issue(pr_info.number)
            for comment in issue.get_comments():
                if marker in comment.body:
                    return comment.id
            return None

        try:
            return await asyncio.to_thread(_find)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to find bot comment: {e}") from e

    async def update_comment(self, pr_info: PRInfo, comment_id: int, body: str) -> None:
        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            retry=retry_if_exception_type(_RETRYABLE),
            reraise=True,
        )
        def _update() -> None:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            issue = gh_repo.get_issue(pr_info.number)
            comment = issue.get_comment(comment_id)
            comment.edit(body)

        try:
            await asyncio.to_thread(_update)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to update comment: {e}") from e

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((httpx.TransportError, ConnectionError, TimeoutError)),
        reraise=True,
    )
    async def _graphql_request(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Execute a GraphQL request against the GitHub API."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                _GRAPHQL_URL,
                json={"query": query, "variables": variables},
                headers={
                    "Authorization": f"bearer {self._token}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                raise ProviderError(f"GraphQL errors: {data['errors']}")
            result: dict[str, Any] = data["data"]
            return result

    async def resolve_outdated_review_threads(self, pr_info: PRInfo) -> int:
        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            retry=retry_if_exception_type(_RETRYABLE),
            reraise=True,
        )
        async def _resolve() -> int:
            # Phase 1: Paginate through review threads and collect bot-authored unresolved ones
            bot_login: str | None = None
            thread_ids: list[str] = []
            cursor: str | None = None

            while True:
                variables: dict[str, Any] = {
                    "owner": pr_info.owner,
                    "repo": pr_info.repo,
                    "number": pr_info.number,
                    "cursor": cursor,
                }
                data = await self._graphql_request(_REVIEW_THREADS_QUERY, variables)

                if bot_login is None:
                    bot_login = data["viewer"]["login"]

                threads = data["repository"]["pullRequest"]["reviewThreads"]
                for node in threads["nodes"]:
                    if node["isResolved"]:
                        continue
                    comments = node["comments"]["nodes"]
                    if not comments:
                        continue
                    author = comments[0].get("author")
                    if author is None:
                        continue
                    if author["login"] == bot_login:
                        thread_ids.append(node["id"])

                page_info = threads["pageInfo"]
                if not page_info["hasNextPage"]:
                    break
                cursor = page_info["endCursor"]

            # Phase 2: Resolve each collected thread
            for thread_id in thread_ids:
                await self._graphql_request(_RESOLVE_THREAD_MUTATION, {"threadId": thread_id})

            return len(thread_ids)

        try:
            return await _resolve()
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to resolve outdated review threads: {e}") from e

    async def get_outdated_bot_threads(
        self, pr_info: PRInfo, bot_login: str
    ) -> list[OutdatedThread]:
        """Fetch unresolved, outdated review threads authored by the bot."""
        threads: list[OutdatedThread] = []
        cursor: str | None = None

        while True:
            variables: dict[str, Any] = {
                "owner": pr_info.owner,
                "repo": pr_info.repo,
                "number": pr_info.number,
                "cursor": cursor,
            }
            try:
                data = await self._graphql_request(_REVIEW_THREADS_QUERY, variables)
            except ProviderError:
                raise
            except Exception as e:
                raise ProviderError(f"Failed to fetch review threads: {e}") from e

            rt = data["repository"]["pullRequest"]["reviewThreads"]
            for node in rt["nodes"]:
                if node["isResolved"] or not node["isOutdated"]:
                    continue
                comments = node["comments"]["nodes"]
                if not comments:
                    continue
                first = comments[0]
                author = (first.get("author") or {}).get("login", "")
                if author != bot_login:
                    continue
                threads.append(
                    OutdatedThread(
                        thread_id=node["id"],
                        path=first.get("path", ""),
                        line=first.get("line") or 0,
                        body=first.get("body", ""),
                    )
                )

            if rt["pageInfo"]["hasNextPage"]:
                cursor = rt["pageInfo"]["endCursor"]
            else:
                break

        return threads

    async def get_file_content(self, pr_info: PRInfo, path: str, ref: str) -> str:
        """Fetch file content at a specific ref via the REST API."""
        url = f"https://api.github.com/repos/{pr_info.owner}/{pr_info.repo}/contents/{path}"
        headers = {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github.v3+json",
        }

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=30),
            retry=retry_if_exception_type(_RETRYABLE),
            reraise=True,
        )
        async def _fetch() -> str:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    url, headers=headers, params={"ref": ref}, follow_redirects=True
                )
                resp.raise_for_status()
                data = resp.json()
                content = data.get("content", "")
                return base64.b64decode(content).decode("utf-8")

        try:
            return await _fetch()
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to fetch file content: {e}") from e

    async def resolve_threads(self, pr_info: PRInfo, thread_ids: list[str]) -> int:
        """Resolve review threads by ID. Returns count of successfully resolved."""
        resolved = 0
        for tid in thread_ids:
            try:
                await self._graphql_request(_RESOLVE_THREAD_MUTATION, {"threadId": tid})
                resolved += 1
            except Exception:
                logger.warning("Failed to resolve thread %s, skipping", tid)
        return resolved


def _format_comment_body(comment: ReviewComment) -> str:
    """Format a review comment body with category badge, severity, and suggestion block."""
    emoji, label = _CATEGORY_DISPLAY.get(comment.category, ("\U0001f4cc", "Note"))
    badge = _SEVERITY_BADGE.get(comment.severity, "")

    parts = [f"{emoji} **{label}**"]
    if badge:
        parts.append(badge)
    parts.append("")
    parts.append(f"**{comment.title}**")
    parts.append("")
    parts.append(comment.body)

    if comment.suggestion:
        parts.append("")
        parts.append("**Suggested fix:**")
        parts.append("```suggestion")
        parts.append(comment.suggestion)
        parts.append("```")

    if comment.agent_prompt:
        parts.append("")
        parts.append("<details>")
        parts.append("<summary>🤖 Prompt for AI Agents</summary>")
        parts.append("")
        parts.append("```text")
        parts.append(comment.agent_prompt)
        parts.append("```")
        parts.append("")
        parts.append("</details>")

    return "\n".join(parts)
