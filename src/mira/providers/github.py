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
    PRInfo,
    ReviewComment,
    ReviewResult,
    UnresolvedThread,
)
from mira.providers.base import BaseProvider

# Shared comment-formatting helpers (re-exported for back-compat — callers and
# tests import these names from this module).
from mira.providers.formatting import (  # noqa: F401
    _CATEGORY_DISPLAY,
    parse_bot_comment_metadata,
)
from mira.providers.formatting import (
    format_comment_body as _format_comment_body,
)
from mira.providers.formatting import (
    format_key_issues as _format_key_issues,
)

# Transient errors worth retrying — network issues and GitHub server errors.
_RETRYABLE = (ConnectionError, TimeoutError, httpx.TransportError, GithubException)

logger = logging.getLogger(__name__)

_retry_transient = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type(_RETRYABLE),
    reraise=True,
)

# GitHub Enterprise: set MIRA_GITHUB_API_URL (and MIRA_GITHUB_GRAPHQL_URL if non-default).
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
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          comments(first: 100) {
            nodes { id }
          }
        }
      }
    }
  }
}
"""

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
            user = pr.user
            return PRInfo(
                title=pr.title or "",
                description=pr.body or "",
                base_branch=pr.base.ref,
                head_branch=pr.head.ref,
                url=pr.html_url,
                number=pr.number,
                owner=owner,
                repo=repo,
                head_sha=pr.head.sha or "",
                author=(user.login or "") if user else "",
                author_avatar_url=(user.avatar_url or "") if user else "",
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

    async def get_compare_diff(
        self,
        pr_info: PRInfo,
        base_sha: str,
        head_sha: str,
    ) -> str:
        """Fetch a unified diff between two commits via GitHub's compare API.

        Used by round 2+ reviews so we only review what's been pushed since
        the last review (``last_reviewed_sha``..``current_head_sha``) rather
        than re-flagging every file in the PR.

        Returns an empty string if the two SHAs are identical (nothing new
        to review).
        """
        if base_sha == head_sha or not base_sha or not head_sha:
            return ""
        url = (
            f"{_GITHUB_API_URL}/repos/{pr_info.owner}/{pr_info.repo}"
            f"/compare/{base_sha}...{head_sha}"
        )
        headers = {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github.v3.diff",
        }

        @_retry_transient
        async def _fetch() -> str:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers=headers, follow_redirects=True)
                resp.raise_for_status()
                return resp.text

        try:
            return await _fetch()
        except Exception as e:
            raise ProviderError(f"Failed to fetch compare diff: {e}") from e

    async def post_review(
        self,
        pr_info: PRInfo,
        result: ReviewResult,
        bot_name: str = "miracodeai",
    ) -> list[int]:
        if not result.comments:
            return []

        # The line GitHub anchors a comment to (the end line for multi-line).
        def _anchor(c: ReviewComment) -> int:
            return c.end_line if (c.end_line and c.end_line > c.line) else c.line

        review_comments: list[dict[str, str | int]] = []
        for comment in result.comments:
            body = _format_comment_body(comment, bot_name=bot_name)
            rc: dict[str, str | int] = {
                "path": comment.path,
                "body": body,
            }
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
        def _post() -> list[int]:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            pr = gh_repo.get_pull(pr_info.number)

            commits = list(pr.get_commits())
            if not commits:
                raise ProviderError("PR has no commits")
            latest_commit = commits[-1]

            # GitHub comment IDs aligned to result.comments (0 = unknown).
            ids = [0] * len(result.comments)

            try:
                review = pr.create_review(
                    commit=latest_commit,
                    body=review_body,
                    event="COMMENT",
                    comments=review_comments,  # type: ignore[arg-type]
                )
                # Map the posted comments back to ours by (path, anchored line)
                # so human replies can later link to the exact comment.
                try:
                    by_loc: dict[tuple[str, int], int] = {}
                    for posted_c in review.get_comments():
                        ln = posted_c.line
                        if ln is None:
                            ln = getattr(posted_c, "original_line", 0) or 0
                        by_loc[(posted_c.path, ln)] = posted_c.id
                    for i, c in enumerate(result.comments):
                        ids[i] = by_loc.get((c.path, _anchor(c)), 0)
                except Exception:
                    logger.debug("Could not map review comment IDs", exc_info=True)
                return ids
            except GithubException as exc:
                if exc.status != 422:
                    raise
                logger.warning(
                    "Batch review failed (422: %s), falling back to individual comments",
                    exc.data,
                )

            # Per-comment fallback via /comments — looser 422 validation
            # than /reviews. Inlines aren't grouped under a review object,
            # but they still show up on the PR.
            posted = 0
            for i, rc in enumerate(review_comments):
                try:
                    kwargs: dict = {
                        "body": rc["body"],
                        "commit": latest_commit,
                        "path": rc["path"],
                    }
                    if "line" in rc:
                        kwargs["line"] = rc["line"]
                    if "start_line" in rc:
                        kwargs["start_line"] = rc["start_line"]
                    logger.info(
                        "POST /comments commit=%s path=%s line=%s body_len=%d",
                        getattr(latest_commit, "sha", "?")[:8],
                        kwargs.get("path"),
                        kwargs.get("line"),
                        len(kwargs.get("body", "")),
                    )
                    created = pr.create_review_comment(**kwargs)
                    ids[i] = getattr(created, "id", 0) or 0
                    posted += 1
                except GithubException as exc:
                    if exc.status == 422:
                        logger.warning(
                            "Skipping comment on %s:%s — 422 from GitHub: %s",
                            rc.get("path"),
                            rc.get("line"),
                            exc.data,
                        )
                    else:
                        raise

            # If every inline failed, post the summary alone so the review still shows up.
            if posted == 0 and review_body:
                try:
                    pr.create_review(
                        commit=latest_commit,
                        body=review_body,
                        event="COMMENT",
                        comments=[],
                    )
                except GithubException as exc:
                    logger.warning(
                        "Summary-only fallback also failed (%s): %s",
                        exc.status,
                        exc.data,
                    )

            logger.info("Individual fallback: posted %d/%d comments", posted, len(review_comments))
            return ids

        try:
            return await asyncio.to_thread(_post)
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

    async def reply_to_review_comment(self, pr_info: PRInfo, comment_id: int, body: str) -> None:
        """Post a reply to an existing review (line) comment, threading it.

        Issue (PR-level) comments use ``post_comment``. Review comments are
        line-anchored and threaded; replying needs the PR-comments-replies
        REST endpoint, not ``create_comment``.
        """

        @_retry_transient
        def _reply() -> None:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            pr = gh_repo.get_pull(pr_info.number)
            pr.create_review_comment_reply(comment_id, body)

        try:
            await asyncio.to_thread(_reply)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to reply to review comment: {e}") from e

    async def get_comment_body(self, pr_info: PRInfo, comment_id: int) -> str:
        """Fetch a review (line) comment's body by id. Best-effort."""

        def _fetch() -> str:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            pr = gh_repo.get_pull(pr_info.number)
            return (pr.get_review_comment(comment_id).body or "")[:1500]

        try:
            return await asyncio.to_thread(_fetch)
        except Exception:
            return ""

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

    async def get_repo_tree(self, pr_info: PRInfo, ref: str) -> list[str]:
        """List every blob (file) path in the repo at a given ref.

        Used by JIT cross-file context: we fetch the tree once, then
        check which import-resolution candidates actually exist before
        spending API calls fetching their contents. One API call → up to
        thousands of paths in response.
        """
        url = f"{_GITHUB_API_URL}/repos/{pr_info.owner}/{pr_info.repo}/git/trees/{ref}?recursive=1"
        headers = {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github+json",
        }

        @_retry_transient
        async def _fetch() -> list[str]:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers=headers, follow_redirects=True)
                resp.raise_for_status()
                data = resp.json()
            return [item["path"] for item in data.get("tree", []) if item.get("type") == "blob"]

        try:
            return await _fetch()
        except Exception as exc:
            logger.debug("Failed to fetch repo tree: %s", exc)
            return []

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
            except Exception as exc:
                logger.warning(
                    "Failed to resolve thread %s on PR %s: %s",
                    tid,
                    pr_info.url,
                    exc,
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

    async def get_thread_id_for_comment(
        self,
        comment_node_id: str,
        pr_info: PRInfo,
    ) -> str | None:
        """Look up the review thread containing ``comment_node_id``.

        GitHub's GraphQL schema doesn't expose ``pullRequestReviewThread``
        directly on a ``PullRequestReviewComment``, so we paginate the PR's
        ``reviewThreads`` connection and match by comment node ID. Returns
        the thread's GraphQL ID (suitable for ``resolveReviewThread``), or
        ``None`` if no matching thread is found or it's already resolved.
        """
        cursor: str | None = None
        while True:
            try:
                data = await self._graphql_request(
                    _COMMENT_THREAD_QUERY,
                    {
                        "owner": pr_info.owner,
                        "repo": pr_info.repo,
                        "number": pr_info.number,
                        "cursor": cursor,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "Failed to look up thread for comment %s: %s",
                    comment_node_id,
                    exc,
                )
                return None

            pr_data = (data.get("repository") or {}).get("pullRequest") or {}
            connection = pr_data.get("reviewThreads") or {
                "nodes": [],
                "pageInfo": {"hasNextPage": False},
            }
            for thread in connection.get("nodes") or []:
                comment_ids = {c["id"] for c in thread.get("comments", {}).get("nodes", [])}
                if comment_node_id in comment_ids:
                    if thread.get("isResolved"):
                        return None
                    thread_id: str = thread["id"]
                    return thread_id

            page = connection.get("pageInfo", {})
            if not page.get("hasNextPage"):
                return None
            cursor = page.get("endCursor")
            if cursor is None:
                logger.warning(
                    "hasNextPage=True but endCursor is None for comment %s; stopping pagination",
                    comment_node_id,
                )
                return None

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

    async def get_review_inline_comments(self, pr_info: PRInfo, review_id: int) -> list[str]:
        """Return the bodies of the inline comments that belong to one review
        (matched via ``pull_request_review_id``). Used to classify whether an
        approval was a substantive review or a rubber-stamp."""

        @_retry_transient
        def _fetch() -> list[str]:
            gh_repo = self._github.get_repo(f"{pr_info.owner}/{pr_info.repo}")
            pr = gh_repo.get_pull(pr_info.number)
            return [
                c.body or ""
                for c in pr.get_review_comments()
                if getattr(c, "pull_request_review_id", None) == review_id
            ]

        try:
            return await asyncio.to_thread(_fetch)
        except Exception as e:
            raise ProviderError(f"Failed to fetch review inline comments: {e}") from e
