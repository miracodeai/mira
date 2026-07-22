"""Forgejo provider using the Gitea-compatible REST API (`/api/v1`).

Mirrors GitHubProvider but speaks Forgejo (Gitea-compatible): pull requests
instead of PRs on GitHub, review comments instead of review threads, and
position-anchored inline review comments. Authentication is a token sent as
``Authorization: token <token>``.

PR URL shape: ``https://forgejo.example.com/owner/repo/pulls/123`` —
note ``/pulls/`` (plural, not GitHub's ``/pull/`` singular).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from collections.abc import AsyncGenerator
from typing import Any
from urllib.parse import quote

import httpx

from mira.exceptions import ProviderError
from mira.models import (
    BotThreadRecord,
    FileHistoryEntry,
    HumanReviewComment,
    PRInfo,
    ReviewResult,
    UnresolvedThread,
)
from mira.platforms import profiles
from mira.providers.base import BaseProvider
from mira.providers.formatting import format_comment_body, format_key_issues

logger = logging.getLogger(__name__)

# https://forgejo.example.com/owner/repo/pulls/123
_PR_URL_PATTERN = re.compile(
    r"(?:https?://[^/]+/)?(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)/pulls/(?P<number>\d+)"
)


def parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    """Parse a PR URL into (owner, repo, number).

    Forgejo ``owner`` is a single segment (no nested groups like GitLab).
    """
    match = _PR_URL_PATTERN.match(pr_url.strip())
    if not match:
        raise ProviderError(
            f"Cannot parse PR URL: {pr_url}. Expected "
            "https://forgejo.example.com/owner/repo/pulls/123"
        )
    return match.group("owner"), match.group("repo"), int(match.group("number"))


class ForgejoProvider(BaseProvider):
    """Forgejo code hosting provider (Gitea-compatible REST API)."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ProviderError("Forgejo token is required")
        self._token = token
        self._api = profiles.resolve("forgejo")["api_url"] or "https://codeberg.org/api/v1"
        self._username: str | None = None

    # ── low-level HTTP ──────────────────────────────────────────────

    def _repo(self, pr_info: PRInfo) -> str:
        return f"{self._api}/repos/{quote(pr_info.owner, safe='')}/{quote(pr_info.repo, safe='')}"

    def _pr(self, pr_info: PRInfo) -> str:
        return f"{self._repo(pr_info)}/pulls/{pr_info.number}"

    async def _request(
        self, method: str, url: str, *, ok: tuple[int, ...] = (200, 201), **kw: Any
    ) -> httpx.Response:
        headers = {"Authorization": f"token {self._token}", **kw.pop("headers", {})}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, headers=headers, **kw)
        if resp.status_code not in ok:
            err = ProviderError(f"Forgejo {method} {url} → {resp.status_code}: {resp.text[:300]}")
            err.status_code = resp.status_code  # type: ignore[attr-defined]
            raise err
        return resp

    async def _paginate(self, url: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        next_url: str | None = url + ("&" if "?" in url else "?") + "limit=100"
        async with httpx.AsyncClient(timeout=30) as client:
            while next_url:
                resp = await client.get(next_url, headers={"Authorization": f"token {self._token}"})
                resp.raise_for_status()
                out.extend(resp.json())
                next_url = _next_link(resp.headers.get("link", ""))
        return out

    async def _self_username(self) -> str:
        """The token user's own username — who comments are actually posted as."""
        if self._username is None:
            try:
                resp = await self._request("GET", f"{self._api}/user")
                self._username = (resp.json() or {}).get("username", "") or ""
            except Exception as exc:
                logger.warning("Failed to resolve Forgejo bot identity: %s", exc)
                self._username = ""
        return self._username

    # ── PR read ─────────────────────────────────────────────────────

    async def get_pr_info(self, pr_url: str) -> PRInfo:
        owner, repo, number = parse_pr_url(pr_url)
        try:
            resp = await self._request(
                "GET",
                f"{self._api}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls/{number}",
            )
            pr = resp.json()
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to fetch PR info: {e}") from e
        return PRInfo(
            title=pr.get("title") or "",
            description=pr.get("body") or "",
            base_branch=(pr.get("base") or {}).get("ref") or "",
            head_branch=(pr.get("head") or {}).get("ref") or "",
            url=pr.get("html_url") or pr_url,
            number=pr.get("number") or number,
            owner=owner,
            repo=repo,
            head_sha=(pr.get("head") or {}).get("sha") or "",
            platform="forgejo",
        )

    async def get_pr_diff(self, pr_info: PRInfo) -> str:
        """Fetch the raw unified diff for the PR."""
        try:
            resp = await self._request(
                "GET",
                f"{self._pr(pr_info)}.diff",
                headers={"Accept": "text/plain"},
                ok=(200, 404),
            )
            if resp.status_code == 404:
                logger.warning("PR diff returned 404 for %s", pr_info.url)
                return ""
            return resp.text
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to fetch PR diff: {e}") from e

    async def get_compare_diff(self, pr_info: PRInfo, base_sha: str, head_sha: str) -> str:
        """Diff between two commits, for round 2+ incremental reviews."""
        if base_sha == head_sha or not base_sha or not head_sha:
            return ""
        url = (
            f"{self._repo(pr_info)}/compare/{quote(base_sha, safe='')}"
            f"..."
            f"{quote(head_sha, safe='')}.diff"
        )
        try:
            resp = await self._request("GET", url, headers={"Accept": "text/plain"}, ok=(200, 404))
            return resp.text if resp.status_code == 200 else ""
        except ProviderError as exc:
            logger.warning("Compare diff failed: %s", exc)
            return ""
        except Exception as e:
            raise ProviderError(f"Failed to fetch compare diff: {e}") from e

    async def get_file_content(self, pr_info: PRInfo, path: str, ref: str) -> str:
        """Raw file content at a ref — used to verify a thread's fix landed."""
        url = f"{self._repo(pr_info)}/contents/{quote(path, safe='')}?ref={quote(ref, safe='')}"
        try:
            resp = await self._request("GET", url, ok=(200, 404))
        except ProviderError as exc:
            logger.warning("Failed to fetch %s@%s: %s", path, ref, exc)
            return ""
        if resp.status_code == 404:
            return ""
        data = resp.json()
        return base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")

    async def get_repo_tree(self, pr_info: PRInfo, ref: str) -> list[str]:
        """Every file path in the repo at a ref, for JIT cross-file context.

        Paginates the Gitea git-trees endpoint, which truncates at 1000
        entries per page (``truncated=true``).  Without pagination, large
        repos silently return an incomplete file list.
        """
        base = f"{self._repo(pr_info)}/git/trees/{quote(ref, safe='')}?recursive=true"
        paths: list[str] = []
        page = 1
        try:
            while True:
                url = f"{base}&page={page}" if page > 1 else base
                resp = await self._request("GET", url)
                data = resp.json()
                for entry in data.get("tree") or []:
                    if entry.get("type") == "blob":
                        paths.append(entry["path"])
                if not data.get("truncated", False):
                    break
                page += 1
        except Exception as exc:
            logger.debug("Failed to fetch repo tree: %s", exc)
            return paths
        return paths

    async def get_file_history(
        self, pr_info: PRInfo, paths: list[str], max_per_file: int = 5
    ) -> dict[str, list[FileHistoryEntry]]:
        """Recent commits per file (most-recent first), for decision archaeology."""
        if not paths:
            return {}

        sem = asyncio.Semaphore(8)
        base = f"{self._repo(pr_info)}/commits"
        headers = {"Authorization": f"token {self._token}"}

        async def _fetch_one(
            client: httpx.AsyncClient, path: str
        ) -> tuple[str, list[FileHistoryEntry]]:
            async with sem:
                try:
                    resp = await client.get(
                        base,
                        headers=headers,
                        params={
                            "path": path,
                            "sha": pr_info.head_branch,
                            "limit": max_per_file,
                        },
                    )
                    if resp.status_code != 200:
                        return path, []
                    data = resp.json()
                except Exception as exc:
                    logger.debug("File history fetch failed for %s: %s", path, exc)
                    return path, []

            entries: list[FileHistoryEntry] = []
            for item in data[:max_per_file]:
                message = (item.get("commit") or {}).get("message", "") or ""
                author = (item.get("author") or {}).get("name", "") or ""
                date = (item.get("author") or {}).get("date", "") or ""
                sha = str(item.get("sha", ""))[:8]
                entries.append(
                    FileHistoryEntry(
                        sha=sha,
                        message=message.split("\n\n", 1)[0][:300],
                        author=author,
                        date=date,
                    )
                )
            return path, entries

        async with httpx.AsyncClient(timeout=30) as client:
            results = await asyncio.gather(*[_fetch_one(client, p) for p in paths])
        return {path: hist for path, hist in results if hist}

    # ── posting ─────────────────────────────────────────────────────

    async def post_review(
        self, pr_info: PRInfo, result: ReviewResult, bot_name: str = "miracodeai"
    ) -> None:
        if not result.comments:
            return

        summary_text = ""
        if result.summary:
            summary_text = f"**Mira Review Summary**\n\n{result.summary}"
        if result.key_issues:
            summary_text += format_key_issues(result.key_issues)

        review_body = {
            "event": "COMMENT",
            "body": summary_text or None,
            "commit_id": pr_info.head_sha,
            "comments": [
                {
                    "path": comment.path,
                    "body": format_comment_body(comment, bot_name=bot_name),
                    "new_position": comment.line,
                }
                for comment in result.comments
            ],
        }

        try:
            await self._request("POST", f"{self._pr(pr_info)}/reviews", json=review_body)
        except ProviderError as exc:
            if getattr(exc, "status_code", None) == 422:
                logger.warning("Inline review failed (%s); posting as individual comments", exc)
                if summary_text:
                    try:
                        await self.post_comment(pr_info, summary_text)
                    except ProviderError:
                        logger.warning("Failed to post PR summary comment (fallback)")
                for comment in result.comments:
                    body = format_comment_body(comment, bot_name=bot_name)
                    note = f"**`{comment.path}:{comment.line}`**\n\n{body}"
                    try:
                        await self.post_comment(pr_info, note)
                    except ProviderError:
                        logger.warning(
                            "Plain-comment fallback also failed for %s:%s",
                            comment.path,
                            comment.line,
                        )
            else:
                raise

    async def post_comment(self, pr_info: PRInfo, body: str) -> None:
        await self._request(
            "POST",
            f"{self._repo(pr_info)}/issues/{pr_info.number}/comments",
            json={"body": body},
        )

    async def find_bot_comment(self, pr_info: PRInfo, marker: str) -> int | None:
        try:
            comments = await self._paginate(
                f"{self._repo(pr_info)}/issues/{pr_info.number}/comments"
            )
        except Exception as e:
            raise ProviderError(f"Failed to list issue comments: {e}") from e
        for comment in comments:
            if marker in (comment.get("body") or ""):
                return int(comment["id"])
        return None

    async def update_comment(self, pr_info: PRInfo, comment_id: int, body: str) -> None:
        await self._request(
            "PATCH",
            f"{self._repo(pr_info)}/issues/comments/{comment_id}",
            json={"body": body},
        )

    async def get_comment_body(self, pr_info: PRInfo, comment_id: int) -> str:
        """Fetch an issue comment's body by id. Best-effort."""
        try:
            resp = await self._request("GET", f"{self._repo(pr_info)}/issues/comments/{comment_id}")
            return (resp.json().get("body") or "")[:1500]
        except Exception:
            return ""

    async def reply_to_review_comment(self, pr_info: PRInfo, comment_id: int, body: str) -> None:
        """Reply to a review comment — Forgejo doesn't support threaded replies."""
        # Forgejo doesn't have a native threaded-reply-to-review-comment API,
        # so we fall back to posting a plain issue comment.
        await self.post_comment(pr_info, body)

    # ── reviews / threads ──────────────────────────────────────────

    async def _fetch_review_comments(self, pr_info: PRInfo, review_id: int) -> list[dict[str, Any]]:
        """Fetch review comments for a given review id."""
        resp = await self._request("GET", f"{self._pr(pr_info)}/reviews/{review_id}/comments")
        data = resp.json()
        if isinstance(data, list):
            return data
        return [data]

    async def _iter_review_comments(
        self, pr_info: PRInfo
    ) -> AsyncGenerator[tuple[dict[str, Any], dict[str, Any]], None]:
        """Yield ``(review, comment)`` tuples for every review comment in a PR.

        Handles pagination over reviews and fetching comments per review,
        swallowing per-review fetch errors so a single bad review doesn't
        abort the whole traversal.
        """
        reviews = await self._paginate(f"{self._pr(pr_info)}/reviews")

        for review in reviews:
            try:
                comments = await self._fetch_review_comments(pr_info, review["id"])
            except Exception:
                continue

            for comment in comments:
                yield (review, comment)

    async def get_all_bot_threads(
        self, pr_info: PRInfo, bot_login: str | None = None
    ) -> list[BotThreadRecord]:
        bot_identities = {n for n in (await self._self_username(), bot_login) if n}
        records: list[BotThreadRecord] = []

        try:
            async for review, comment in self._iter_review_comments(pr_info):
                review_user = (review.get("user") or {}).get("username", "")
                if bot_identities and review_user not in bot_identities:
                    continue

                comment_user = (comment.get("user") or {}).get("username", "")
                if bot_identities and comment_user not in bot_identities:
                    continue

                line = comment.get("new_position") or comment.get("line") or 0
                records.append(
                    BotThreadRecord(
                        thread_id=str(review["id"]),
                        path=comment.get("path") or "",
                        line=int(line),
                        body=comment.get("body") or "",
                        is_resolved=bool(comment.get("resolved")),
                        is_outdated=bool(review.get("stale")),
                    )
                )
        except Exception as e:
            raise ProviderError(f"Failed to fetch PR reviews: {e}") from e

        return records

    async def get_unresolved_bot_threads(
        self, pr_info: PRInfo, bot_login: str | None = None
    ) -> list[UnresolvedThread]:
        threads = await self.get_all_bot_threads(pr_info, bot_login)
        return [
            UnresolvedThread(thread_id=t.thread_id, path=t.path, line=t.line, body=t.body)
            for t in threads
            if not t.is_resolved
        ]

    async def resolve_threads(self, pr_info: PRInfo, thread_ids: list[str]) -> int:
        """Resolve review threads — no-op (Forgejo hasn't shipped comment resolution)."""
        logger.debug("resolve_threads is a no-op for Forgejo (%d threads)", len(thread_ids))
        return 0

    async def resolve_outdated_review_threads(self, pr_info: PRInfo) -> int:
        """Resolve outdated review threads — no-op for Forgejo."""
        logger.debug("resolve_outdated_review_threads is a no-op for Forgejo")
        return 0

    async def get_thread_id_for_comment(self, comment_node_id: str, pr_info: PRInfo) -> str | None:
        """Look up the review thread for a comment by iterating reviews' comments."""
        try:
            async for review, comment in self._iter_review_comments(pr_info):
                if str(comment.get("id")) == comment_node_id:
                    return str(review["id"])
        except Exception:
            pass

        return None

    async def get_human_review_comments(
        self, pr_info: PRInfo, bot_login: str
    ) -> list[HumanReviewComment]:
        bot_identities = {n for n in (await self._self_username(), bot_login) if n}
        out: list[HumanReviewComment] = []

        try:
            async for review, comment in self._iter_review_comments(pr_info):
                review_user = (review.get("user") or {}).get("username", "")
                if review_user in bot_identities:
                    continue

                comment_user = (comment.get("user") or {}).get("username", "")
                if comment_user in bot_identities:
                    continue

                line = comment.get("new_position") or comment.get("line") or 0
                out.append(
                    HumanReviewComment(
                        path=comment.get("path") or "",
                        line=int(line),
                        body=comment.get("body") or "",
                        author=comment_user,
                    )
                )
        except Exception as e:
            raise ProviderError(f"Failed to fetch reviews for human comments: {e}") from e

        return out

    # ── labels ──────────────────────────────────────────────────────

    async def add_label(self, pr_info: PRInfo, label: str) -> None:
        await self._request(
            "POST",
            f"{self._repo(pr_info)}/issues/{pr_info.number}/labels",
            json={"labels": [label]},
        )

    async def remove_label(self, pr_info: PRInfo, label: str) -> None:
        await self._request(
            "DELETE",
            f"{self._repo(pr_info)}/issues/{pr_info.number}/labels?name={quote(label, safe='')}",
        )

    async def get_discussion_root_body(self, pr_info: PRInfo, discussion_id: str) -> str:
        """The first comment of a thread/discussion. Best-effort.

        Forgejo has no discussion threading (replies are flat issue comments),
        so this is equivalent to ``get_comment_body``.
        """
        try:
            resp = await self._request(
                "GET",
                f"{self._repo(pr_info)}/issues/comments/{discussion_id}",
            )
            return (resp.json().get("body") or "")[:1500]
        except Exception:
            return ""


def _next_link(link_header: str) -> str | None:
    """Extract the rel="next" URL from a Link header."""
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip().strip("<>")
    return None
