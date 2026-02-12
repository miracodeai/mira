"""GitHub provider using PyGithub."""

from __future__ import annotations

import asyncio
import logging
import re

import httpx
from github import Github, GithubException
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from mira.exceptions import ProviderError
from mira.models import PRInfo, ReviewComment, ReviewResult
from mira.providers.base import BaseProvider

# Transient errors worth retrying — network issues and GitHub server errors.
_RETRYABLE = (ConnectionError, TimeoutError, httpx.TransportError, GithubException)

logger = logging.getLogger(__name__)

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
        review_comments = []
        for comment in result.comments:
            body = _format_comment_body(comment)
            rc = {
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
                comments=review_comments,
            )

        try:
            await asyncio.to_thread(_post)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to post review: {e}") from e


def _format_comment_body(comment: ReviewComment) -> str:
    """Format a review comment body with severity emoji and suggestion block."""
    parts = [f"{comment.severity.emoji} **{comment.severity.name}** — {comment.title}"]
    parts.append("")
    parts.append(comment.body)

    if comment.suggestion:
        parts.append("")
        parts.append("```suggestion")
        parts.append(comment.suggestion)
        parts.append("```")

    return "\n".join(parts)
