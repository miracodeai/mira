"""GitHub provider using PyGithub."""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from typing import Any

import httpx
from github import Github, GithubException
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from mira.exceptions import ProviderError
from mira.models import (
    BotThreadRecord,
    FileHistoryEntry,
    HumanReviewComment,
    KeyIssue,
    PRInfo,
    ReviewComment,
    ReviewResult,
    Severity,
    UnresolvedThread,
)
from mira.providers.base import BaseProvider

# Transient errors worth retrying — network issues and GitHub server errors.
_RETRYABLE = (ConnectionError, TimeoutError, httpx.TransportError, GithubException)

logger = logging.getLogger(__name__)

_retry_transient = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(_RETRYABLE),
    reraise=True,
)

# GitHub Enterprise Server support: override via MIRA_GITHUB_API_URL.
# Examples: "https://github.acme-corp.com/api/v3" (REST root), with the
# corresponding GraphQL endpoint derived by appending "/graphql" if not set
# explicitly via MIRA_GITHUB_GRAPHQL_URL.
_GITHUB_API_URL = os.environ.get(
    "MIRA_GITHUB_API_URL",
    "https://api.github.com",
).rstrip("/")
_GRAPHQL_URL = os.environ.get(
    "MIRA_GITHUB_GRAPHQL_URL",
    f"{_GITHUB_API_URL}/graphql",
)


def _normalize_login(login: str) -> str:
    """Normalize a GitHub login for comparison.

    GitHub Apps have a quirk: ``viewer.login`` returns ``app[bot]`` while
    review-comment authors are stored as just ``app``.  Strip the ``[bot]``
    suffix and lower-case so both forms match reliably.
    """
    return login.removesuffix("[bot]").lower()


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
              originalLine
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

_COMMENT_THREAD_QUERY = """
query($nodeId: ID!) {
  node(id: $nodeId) {
    ... on PullRequestReviewComment {
      pullRequestReviewThread { id isResolved }
    }
  }
}
"""

_CATEGORY_DISPLAY: dict[str, tuple[str, str]] = {
    "bug": ("\U0001f41b", "Bug"),
    "security": ("\U0001f512", "Security issue"),
    "performance": ("\u26a1", "Performance"),
    "error-handling": ("\u26a0\ufe0f", "Error Handling"),
    "race-condition": ("\U0001f3c1", "Race Condition"),
    "resource-leak": ("\U0001f4a7", "Resource Leak"),
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

        @_retry_transient
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
        diff_url = f"{_GITHUB_API_URL}/repos/{pr_info.owner}/{pr_info.repo}/pulls/{pr_info.number}"
        headers = {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github.v3.diff",
        }

        @_retry_transient
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
        bot_name: str = "miracodeai",
    ) -> None:
        if not result.comments:
            return

        # Build inline comments (no retry needed for local formatting)
        review_comments: list[dict[str, str | int]] = []
        for comment in result.comments:
            body = _format_comment_body(comment, bot_name=bot_name)
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
        if result.key_issues:
            review_body += _format_key_issues(result.key_issues)

        @_retry_transient
        def _post() -> None:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            pr = gh_repo.get_pull(pr_info.number)

            commits = list(pr.get_commits())
            if not commits:
                raise ProviderError("PR has no commits")
            latest_commit = commits[-1]

            # Try posting all comments as a single review first
            try:
                pr.create_review(
                    commit=latest_commit,
                    body=review_body,
                    event="COMMENT",
                    comments=review_comments,  # type: ignore[arg-type]
                )
                return
            except GithubException as exc:
                if exc.status != 422:
                    raise
                logger.warning(
                    "Batch review failed (422: %s), falling back to individual comments",
                    exc.data,
                )

            # Fallback: post each comment individually so one bad line
            # doesn't kill all comments.
            posted = 0
            for rc in review_comments:
                try:
                    pr.create_review(
                        commit=latest_commit,
                        body="",
                        event="COMMENT",
                        comments=[rc],  # type: ignore[arg-type]
                    )
                    posted += 1
                except GithubException as exc:
                    if exc.status == 422:
                        logger.warning(
                            "Skipping comment on %s:%s — line not in diff",
                            rc.get("path"),
                            rc.get("line"),
                        )
                    else:
                        raise

            if posted == 0 and review_body:
                # All inline comments failed but we still have a summary
                pr.create_review(
                    commit=latest_commit,
                    body=review_body,
                    event="COMMENT",
                    comments=[],
                )

            logger.info("Individual fallback: posted %d/%d comments", posted, len(review_comments))

        try:
            await asyncio.to_thread(_post)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to post review: {e}") from e

    async def post_comment(self, pr_info: PRInfo, body: str) -> None:
        @_retry_transient
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
        @_retry_transient
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
        @_retry_transient
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
        @_retry_transient
        async def _resolve() -> int:
            # Phase 1: Paginate through review threads and collect bot-authored
            # unresolved threads that GitHub has marked as outdated.
            bot_login: str | None = None
            thread_ids: list[str] = []
            total_unresolved = 0
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
                    if _normalize_login(author["login"]) == _normalize_login(bot_login):
                        total_unresolved += 1
                        if node["isOutdated"]:
                            thread_ids.append(node["id"])

                page_info = threads["pageInfo"]
                if not page_info["hasNextPage"]:
                    break
                cursor = page_info["endCursor"]

            logger.debug(
                "Brute-force resolver (viewer=%s): %d unresolved bot thread(s), "
                "%d outdated to resolve",
                bot_login,
                total_unresolved,
                len(thread_ids),
            )

            # Phase 2: Resolve each collected outdated thread
            for thread_id in thread_ids:
                await self._graphql_request(_RESOLVE_THREAD_MUTATION, {"threadId": thread_id})

            return len(thread_ids)

        try:
            return await _resolve()
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to resolve outdated review threads: {e}") from e

    async def get_unresolved_bot_threads(
        self, pr_info: PRInfo, bot_login: str | None = None
    ) -> list[UnresolvedThread]:
        """Fetch all unresolved review threads authored by the bot.

        If *bot_login* is ``None`` the authenticated user (viewer) is used,
        which is the reliable way to match the GitHub App's own comments.
        """
        threads: list[UnresolvedThread] = []
        viewer_login: str | None = None
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

            if viewer_login is None:
                viewer_login = data["viewer"]["login"]

            effective_login = bot_login or viewer_login

            rt = data["repository"]["pullRequest"]["reviewThreads"]
            total_nodes = len(rt["nodes"])
            skipped_resolved = 0
            skipped_no_comments = 0
            skipped_author = 0

            for node in rt["nodes"]:
                if node["isResolved"]:
                    skipped_resolved += 1
                    continue
                comments = node["comments"]["nodes"]
                if not comments:
                    skipped_no_comments += 1
                    continue
                first = comments[0]
                author = (first.get("author") or {}).get("login", "")
                if _normalize_login(author) != _normalize_login(effective_login):
                    skipped_author += 1
                    logger.info(
                        "Skipping thread %s: author %r != %r",
                        node["id"],
                        author,
                        effective_login,
                    )
                    continue
                threads.append(
                    UnresolvedThread(
                        thread_id=node["id"],
                        path=first.get("path", ""),
                        line=first.get("line") or first.get("originalLine") or 0,
                        body=first.get("body", ""),
                        is_outdated=bool(node["isOutdated"]),
                    )
                )

            logger.info(
                "Page: %d nodes, %d resolved, %d no comments, %d wrong author, %d matched",
                total_nodes,
                skipped_resolved,
                skipped_no_comments,
                skipped_author,
                total_nodes - skipped_resolved - skipped_no_comments - skipped_author,
            )

            if rt["pageInfo"]["hasNextPage"]:
                cursor = rt["pageInfo"]["endCursor"]
            else:
                break

        logger.info(
            "get_unresolved_bot_threads (viewer=%s, match=%s): "
            "found %d thread(s) for PR %s (%d outdated)",
            viewer_login,
            effective_login,
            len(threads),
            pr_info.url,
            sum(1 for t in threads if t.is_outdated),
        )
        return threads

    async def add_label(self, pr_info: PRInfo, label: str) -> None:
        @_retry_transient
        def _add() -> None:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            issue = gh_repo.get_issue(pr_info.number)
            issue.add_to_labels(label)

        try:
            await asyncio.to_thread(_add)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to add label: {e}") from e

    async def remove_label(self, pr_info: PRInfo, label: str) -> None:
        @_retry_transient
        def _remove() -> None:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            issue = gh_repo.get_issue(pr_info.number)
            try:
                issue.remove_from_labels(label)
            except GithubException as exc:
                if exc.status == 404:
                    return
                raise

        try:
            await asyncio.to_thread(_remove)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to remove label: {e}") from e

    async def get_file_content(self, pr_info: PRInfo, path: str, ref: str) -> str:
        """Fetch file content at a specific ref via the REST API."""
        url = f"{_GITHUB_API_URL}/repos/{pr_info.owner}/{pr_info.repo}/contents/{path}"
        headers = {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github.v3+json",
        }

        @_retry_transient
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
                logger.warning(
                    "Failed to resolve thread %s on PR %s",
                    tid,
                    pr_info.url,
                )
        if resolved < len(thread_ids):
            logger.warning(
                "Resolved %d/%d threads on PR %s (%d failed)",
                resolved,
                len(thread_ids),
                pr_info.url,
                len(thread_ids) - resolved,
            )
        return resolved

    async def get_thread_id_for_comment(self, comment_node_id: str) -> str | None:
        """Look up the review thread for a comment. Returns thread ID or None."""
        try:
            data = await self._graphql_request(_COMMENT_THREAD_QUERY, {"nodeId": comment_node_id})
        except Exception:
            logger.warning("Failed to look up thread for comment %s", comment_node_id)
            return None

        node = data.get("node")
        if not node:
            return None

        thread = node.get("pullRequestReviewThread")
        if not thread:
            return None

        if thread.get("isResolved"):
            return None

        thread_id: str = thread["id"]
        return thread_id

    async def get_all_bot_threads(
        self, pr_info: PRInfo, bot_login: str | None = None
    ) -> list[BotThreadRecord]:
        """Fetch all bot-authored review threads on a PR (resolved and unresolved)."""
        threads: list[BotThreadRecord] = []
        viewer_login: str | None = None
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

            if viewer_login is None:
                viewer_login = data["viewer"]["login"]

            effective_login = bot_login or viewer_login
            rt = data["repository"]["pullRequest"]["reviewThreads"]

            for node in rt["nodes"]:
                comments = node["comments"]["nodes"]
                if not comments:
                    continue
                first = comments[0]
                author = (first.get("author") or {}).get("login", "")
                if _normalize_login(author) != _normalize_login(effective_login):
                    continue
                threads.append(
                    BotThreadRecord(
                        thread_id=node["id"],
                        path=first.get("path", ""),
                        line=first.get("line") or first.get("originalLine") or 0,
                        body=first.get("body", ""),
                        is_resolved=bool(node["isResolved"]),
                        is_outdated=bool(node["isOutdated"]),
                    )
                )

            if rt["pageInfo"]["hasNextPage"]:
                cursor = rt["pageInfo"]["endCursor"]
            else:
                break

        logger.info(
            "get_all_bot_threads: %d thread(s) on PR %s (%d resolved)",
            len(threads),
            pr_info.url,
            sum(1 for t in threads if t.is_resolved),
        )
        return threads

    async def get_file_history(
        self,
        pr_info: PRInfo,
        paths: list[str],
        max_per_file: int = 5,
    ) -> dict[str, list[FileHistoryEntry]]:
        """Fetch recent commit history per file.

        Returns ``{path: [FileHistoryEntry, ...]}`` ordered most-recent first,
        capped at ``max_per_file`` per path. Used to give the review LLM
        context for "why does this code exist?" before it suggests deletion.

        Concurrency-bounded so a PR touching 50 files doesn't blow the rate
        limit; uses a small semaphore.
        """
        if not paths:
            return {}

        sem = asyncio.Semaphore(8)
        headers = {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github.v3+json",
        }
        base = f"{_GITHUB_API_URL}/repos/{pr_info.owner}/{pr_info.repo}/commits"

        async def _fetch_one(
            client: httpx.AsyncClient, path: str
        ) -> tuple[str, list[FileHistoryEntry]]:
            async with sem:
                try:
                    resp = await client.get(
                        base,
                        headers=headers,
                        params={"path": path, "per_page": max_per_file},
                    )
                    if resp.status_code != 200:
                        return path, []
                    data = resp.json()
                except Exception as exc:
                    logger.debug("File history fetch failed for %s: %s", path, exc)
                    return path, []

            entries: list[FileHistoryEntry] = []
            for item in data[:max_per_file]:
                commit = item.get("commit") or {}
                author = commit.get("author") or {}
                message = (commit.get("message") or "").strip()
                # Trim multi-line messages — first paragraph is typically the
                # imperative subject and is enough context for the LLM.
                short_message = message.split("\n\n", 1)[0][:300]
                entries.append(
                    FileHistoryEntry(
                        sha=str(item.get("sha", ""))[:8],
                        message=short_message,
                        author=str(author.get("name", "")),
                        date=str(author.get("date", "")),
                    )
                )
            return path, entries

        async with httpx.AsyncClient(timeout=30) as client:
            results = await asyncio.gather(
                *[_fetch_one(client, p) for p in paths],
                return_exceptions=False,
            )

        return {path: hist for path, hist in results if hist}

    async def get_human_review_comments(
        self, pr_info: PRInfo, bot_login: str
    ) -> list[HumanReviewComment]:
        """Fetch all non-bot review comments (line-level) on a PR."""
        bot_norm = _normalize_login(bot_login)

        @_retry_transient
        def _fetch() -> list[HumanReviewComment]:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            pr = gh_repo.get_pull(pr_info.number)
            results: list[HumanReviewComment] = []
            for c in pr.get_review_comments():
                author = c.user.login if c.user else ""
                if _normalize_login(author) == bot_norm:
                    continue
                results.append(
                    HumanReviewComment(
                        path=c.path or "",
                        line=(c.line or c.original_line or 0),
                        body=c.body or "",
                        author=author,
                    )
                )
            return results

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as e:
            raise ProviderError(f"Failed to fetch human review comments: {e}") from e


# Reverse maps for parsing bot comment metadata
_LABEL_TO_CATEGORY = {label: cat for cat, (_, label) in _CATEGORY_DISPLAY.items()}
_CATEGORY_EMOJI_TO_NAME = {emoji: cat for cat, (emoji, _) in _CATEGORY_DISPLAY.items()}
_SEVERITY_EMOJI_MAP: dict[str, str] = {
    "\U0001f6d1": "blocker",
    "⚠️": "warning",
    "⚠": "warning",
    "\U0001f4a1": "suggestion",
    "\U0001f4ac": "nitpick",
}

_CATEGORY_LINE_RE = re.compile(r"^(\S+)\s+\*\*([^*]+)\*\*\s*$")
_BOLD_LINE_RE = re.compile(r"^\*\*([^*\n]+)\*\*\s*$")


def parse_bot_comment_metadata(body: str) -> dict[str, str]:
    """Extract category, severity, and title from a bot review-comment body.

    The bot formats comments as:
        {category-emoji} **{Category Label}**
        {severity-emoji} {severity label}

        **{Title}**

        {body...}

    Returns a dict with keys 'category', 'severity', 'title'. Missing fields
    default to empty string. Safe on malformed input.
    """
    category = ""
    severity = ""
    title = ""

    for raw in body.split("\n"):
        line = raw.strip()
        if not line:
            continue

        if not category:
            m = _CATEGORY_LINE_RE.match(line)
            if m:
                emoji, label = m.group(1), m.group(2).strip()
                if label in _LABEL_TO_CATEGORY:
                    category = _LABEL_TO_CATEGORY[label]
                    continue
                if emoji in _CATEGORY_EMOJI_TO_NAME:
                    category = _CATEGORY_EMOJI_TO_NAME[emoji]
                    continue

        if not severity:
            matched = False
            for emoji, sev in _SEVERITY_EMOJI_MAP.items():
                if line.startswith(emoji):
                    severity = sev
                    matched = True
                    break
            if matched:
                continue

        if not title:
            m = _BOLD_LINE_RE.match(line)
            if m:
                title = m.group(1).strip()

        if category and severity and title:
            break

    return {"category": category, "severity": severity, "title": title}


def _format_key_issues(key_issues: list[KeyIssue]) -> str:
    """Format key issues as a markdown table for the review body."""
    lines = [
        "",
        "",
        "### Key Issues",
        "",
        "| | Issue | Location |",
        "|---|---|---|",
    ]
    for ki in key_issues:
        lines.append(f"| :red_circle: | {ki.issue} | `{ki.path}:{ki.line}` |")
    return "\n".join(lines)


_FENCE_RE = re.compile(r"^(`{3,})")


def _strip_suggestion_fences(text: str) -> str:
    """Remove wrapping triple-backtick fences the LLM may add to suggestion code.

    The suggestion content is placed inside a ```suggestion``` fence by the
    caller, so any fences inside the content itself would break GitHub's
    rendering.  We strip:
    - A leading fence line (```, ```ts, ```python, etc.)
    - A trailing fence line (```)
    - Any remaining triple-backtick-only lines in the middle
    """
    lines = text.split("\n")
    # Strip leading fence
    if lines and _FENCE_RE.match(lines[0].strip()):
        lines = lines[1:]
    # Strip trailing fence
    if lines and _FENCE_RE.match(lines[-1].strip()):
        lines = lines[:-1]
    # Remove any remaining triple-backtick-only lines (shouldn't appear in
    # real code, but LLMs sometimes produce them)
    lines = [ln for ln in lines if not re.fullmatch(r"`{3,}\s*", ln.strip())]
    return "\n".join(lines)


def _close_open_fences(parts: list[str]) -> None:
    """If the accumulated body has an unclosed code fence, close it.

    An odd number of triple-backtick fence lines means a fence is still open.
    Appending a closing fence prevents it from swallowing the suggestion block
    that follows.
    """
    open_fence = False
    for part in parts:
        for line in part.split("\n"):
            stripped = line.strip()
            if _FENCE_RE.match(stripped):
                open_fence = not open_fence
    if open_fence:
        parts.append("```")


def _format_comment_body(comment: ReviewComment, bot_name: str = "miracodeai") -> str:
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
        # Unescape HTML entities that LLMs sometimes produce in code suggestions
        import html

        clean_suggestion = html.unescape(comment.suggestion)
        # Strip triple-backtick fences the LLM may have wrapped around the code
        clean_suggestion = _strip_suggestion_fences(clean_suggestion)

        # Ensure the body has balanced code fences so an unclosed fence
        # doesn't swallow the suggestion block.
        _close_open_fences(parts)

        parts.append("")
        parts.append("```suggestion")
        parts.append(clean_suggestion)
        parts.append("```")

    if comment.agent_prompt:
        import html as _html_mod

        prompt_text = comment.agent_prompt
        if comment.suggestion:
            clean = _html_mod.unescape(comment.suggestion)
            prompt_text += f"\n\nApply this code change:\n\n{clean}"
        escaped_prompt = _html_mod.escape(prompt_text)
        parts.append("")
        parts.append("---")
        parts.append("")
        parts.append(
            "<details>\n"
            "<summary>Prompt for AI Agents</summary>\n"
            "\n"
            f"<pre>{escaped_prompt}</pre>\n"
            "\n"
            "</details>"
        )

    parts.append("")
    parts.append(f"> Not useful? Reply `@{bot_name} reject` to dismiss this suggestion.")

    return "\n".join(parts)
