"""Cursor Origin provider using the Origin REST API (`/v1/origin`).

Mirrors GitHubProvider / ForgejoProvider but speaks Cursor Origin: camelCase
JSON, string-encoded PR numbers and IDs, Bearer auth, and PR URLs shaped like
``https://cursor.com/codebase/owner/repo/pull/123`` (or the git host
``https://origin.cursor.com/owner/repo``).

Docs: https://cursor.com/docs/api/origin
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
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

# https://cursor.com/codebase/owner/repo/pull/123
# https://origin.cursor.com/owner/repo/pull/123
_PR_URL_PATTERN = re.compile(
    r"https?://(?:cursor\.com/codebase|origin\.cursor\.com)/"
    r"(?P<owner>[^/\s#]+)/(?P<repo>[^/\s#]+?)/pull/(?P<number>\d+)",
    re.IGNORECASE,
)


def parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    """Parse an Origin PR URL into (owner, repo, number)."""
    match = _PR_URL_PATTERN.search(pr_url.strip())
    if not match:
        raise ProviderError(
            f"Cannot parse Origin PR URL: {pr_url}. Expected "
            "https://cursor.com/codebase/owner/repo/pull/123"
        )
    return match.group("owner"), match.group("repo"), int(match.group("number"))


def _actor_handle(actor: dict[str, Any] | None) -> str:
    """Extract a display handle from an Origin actor object."""
    if not actor:
        return ""
    user = actor.get("user") or {}
    if user.get("handle"):
        return str(user["handle"])
    if user.get("displayName"):
        return str(user["displayName"])
    app = actor.get("app") or {}
    if app.get("displayName"):
        return str(app["displayName"])
    if app.get("id"):
        return str(app["id"])
    sa = actor.get("serviceAccount") or {}
    if sa.get("displayName"):
        return str(sa["displayName"])
    return ""


def _ref_branch(ref: str) -> str:
    """Strip ``refs/heads/`` so callers get a short branch name when possible."""
    if ref.startswith("refs/heads/"):
        return ref[len("refs/heads/") :]
    return ref


def _patches_to_diff(files: list[dict[str, Any]]) -> str:
    """Rebuild a unified diff from Origin per-file ``patch`` fields."""
    chunks: list[str] = []
    for f in files:
        filename = f.get("filename") or ""
        previous = f.get("previousFilename") or ""
        status = f.get("status") or "modified"
        patch = f.get("patch") or ""
        if not filename:
            continue
        if status == "added":
            chunks.append(f"diff --git a/{filename} b/{filename}")
            chunks.append("new file mode 100644")
            chunks.append("--- /dev/null")
            chunks.append(f"+++ b/{filename}")
        elif status == "removed":
            chunks.append(f"diff --git a/{filename} b/{filename}")
            chunks.append("deleted file mode 100644")
            chunks.append(f"--- a/{filename}")
            chunks.append("+++ /dev/null")
        elif status in ("renamed", "copied") and previous:
            chunks.append(f"diff --git a/{previous} b/{filename}")
            chunks.append(f"--- a/{previous}")
            chunks.append(f"+++ b/{filename}")
        else:
            chunks.append(f"diff --git a/{filename} b/{filename}")
            chunks.append(f"--- a/{filename}")
            chunks.append(f"+++ b/{filename}")
        if patch:
            chunks.append(patch if patch.endswith("\n") else patch + "\n")
        else:
            chunks.append("")
    return "\n".join(chunks).rstrip() + ("\n" if chunks else "")


class OriginProvider(BaseProvider):
    """Cursor Origin code hosting provider."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ProviderError("Origin token is required")
        # create_provider may pass a callable; BaseProvider typing says str,
        # but GitHub accepts callables too. Normalize to str for Origin.
        if callable(token):
            raise ProviderError(
                "OriginProvider requires a resolved bearer token string "
                "(call the token factory before create_provider)"
            )
        self._token = token
        profile = profiles.resolve("origin")
        self._api = (profile.get("api_url") or "https://api.cursor.com/v1/origin").rstrip("/")
        self._identity: str | None = None
        # Origin IDs are opaque strings; BaseProvider uses int comment ids.
        # Intern string↔int for round-trips within this provider instance.
        self._id_to_int: dict[str, int] = {}
        self._int_to_id: dict[int, str] = {}
        self._next_id = 1

    def _intern(self, sid: str) -> int:
        if sid in self._id_to_int:
            return self._id_to_int[sid]
        i = self._next_id
        self._next_id += 1
        self._id_to_int[sid] = i
        self._int_to_id[i] = sid
        return i

    def _resolve_id(self, comment_id: int | str) -> str:
        if isinstance(comment_id, str):
            return comment_id
        sid = self._int_to_id.get(comment_id)
        if sid is None:
            raise ProviderError(f"Unknown Origin comment id mapping: {comment_id}")
        return sid

    def _repo(self, owner: str, repo: str) -> str:
        return f"{self._api}/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"

    def _pr(self, pr_info: PRInfo) -> str:
        return f"{self._repo(pr_info.owner, pr_info.repo)}/pulls/{pr_info.number}"

    def _headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", **extra}

    async def _request(
        self, method: str, url: str, *, ok: tuple[int, ...] = (200, 201), **kw: Any
    ) -> httpx.Response:
        headers = self._headers(**kw.pop("headers", {}))
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.request(method, url, headers=headers, **kw)
        if resp.status_code not in ok:
            err = ProviderError(f"Origin {method} {url} → {resp.status_code}: {resp.text[:300]}")
            err.status_code = resp.status_code  # type: ignore[attr-defined]
            raise err
        return resp

    async def _paginate(self, url: str, list_key: str) -> list[dict[str, Any]]:
        """Paginate an Origin collection that returns ``{list_key: [...], nextPageToken}``."""
        out: list[dict[str, Any]] = []
        page_token: str | None = None
        async with httpx.AsyncClient(timeout=60) as client:
            while True:
                params: dict[str, Any] = {"pageSize": 100}
                if page_token:
                    params["pageToken"] = page_token
                resp = await client.get(url, headers=self._headers(), params=params)
                if resp.status_code != 200:
                    raise ProviderError(
                        f"Origin GET {url} → {resp.status_code}: {resp.text[:300]}"
                    )
                data = resp.json() or {}
                items = data.get(list_key) or []
                out.extend(items)
                page_token = data.get("nextPageToken") or data.get("next_page_token") or None
                if not page_token or not items:
                    break
        return out

    async def _self_identity(self) -> str:
        if self._identity is None:
            try:
                resp = await self._request("GET", f"{self._api}/app", ok=(200, 401, 403, 404))
                if resp.status_code == 200:
                    data = resp.json() or {}
                    self._identity = (
                        data.get("displayName") or data.get("namespaceSlug") or data.get("id") or ""
                    )
                else:
                    self._identity = ""
            except Exception as exc:
                logger.warning("Failed to resolve Origin bot identity: %s", exc)
                self._identity = ""
        return self._identity or ""

    # ── PR read ─────────────────────────────────────────────────────

    async def get_pr_info(self, pr_url: str) -> PRInfo:
        owner, repo, number = parse_pr_url(pr_url)
        try:
            resp = await self._request("GET", f"{self._repo(owner, repo)}/pulls/{number}")
            pr = resp.json()
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to fetch Origin PR info: {e}") from e

        head = pr.get("head") or {}
        base = pr.get("base") or {}
        web_url = pr.get("webUrl") or pr_url
        return PRInfo(
            title=pr.get("title") or "",
            description=pr.get("body") or "",
            base_branch=_ref_branch(base.get("ref") or ""),
            head_branch=_ref_branch(head.get("ref") or ""),
            url=web_url,
            number=int(pr.get("number") or number),
            owner=owner,
            repo=repo,
            head_sha=head.get("sha") or "",
            platform="origin",
        )

    async def get_pr_diff(self, pr_info: PRInfo) -> str:
        """Assemble a unified diff from List Pull Request Files patches."""
        try:
            files = await self._paginate(f"{self._pr(pr_info)}/files", "files")
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to fetch Origin PR diff: {e}") from e
        return _patches_to_diff(files)

    async def get_compare_diff(self, pr_info: PRInfo, base_sha: str, head_sha: str) -> str:
        if base_sha == head_sha or not base_sha or not head_sha:
            return ""
        basehead = f"{quote(base_sha, safe='')}...{quote(head_sha, safe='')}"
        url = f"{self._repo(pr_info.owner, pr_info.repo)}/compare/{basehead}/files"
        try:
            files = await self._paginate(url, "files")
        except ProviderError as exc:
            logger.warning("Origin compare diff failed: %s", exc)
            return ""
        except Exception as e:
            raise ProviderError(f"Failed to fetch Origin compare diff: {e}") from e
        return _patches_to_diff(files)

    async def get_file_content(self, pr_info: PRInfo, path: str, ref: str) -> str:
        url = f"{self._repo(pr_info.owner, pr_info.repo)}/contents"
        try:
            resp = await self._request(
                "GET",
                url,
                params={"path": path, "ref": ref},
                ok=(200, 404),
            )
        except ProviderError as exc:
            logger.warning("Failed to fetch %s@%s: %s", path, ref, exc)
            return ""
        if resp.status_code == 404:
            return ""
        data = resp.json() or {}
        if data.get("type") != "file":
            return ""
        raw = data.get("content") or ""
        try:
            return base64.b64decode(raw).decode("utf-8", errors="replace")
        except Exception:
            return ""

    async def get_repo_tree(self, pr_info: PRInfo, ref: str) -> list[str]:
        url = f"{self._repo(pr_info.owner, pr_info.repo)}/git/trees/{quote(ref, safe='')}"
        try:
            resp = await self._request("GET", url, params={"recursive": "true"})
            data = resp.json() or {}
        except Exception as exc:
            logger.debug("Failed to fetch Origin repo tree: %s", exc)
            return []
        paths: list[str] = []
        for entry in data.get("tree") or []:
            if entry.get("type") == "blob" and entry.get("path"):
                paths.append(entry["path"])
        if data.get("truncated"):
            logger.warning("Origin repo tree for %s/%s@%s was truncated", pr_info.owner, pr_info.repo, ref)
        return paths

    async def get_file_history(
        self, pr_info: PRInfo, paths: list[str], max_per_file: int = 5
    ) -> dict[str, list[FileHistoryEntry]]:
        if not paths:
            return {}
        # Origin List Commits supports a single path filter; fan out per path.
        sem = asyncio.Semaphore(8)
        base = f"{self._repo(pr_info.owner, pr_info.repo)}/commits"

        async def _fetch_one(
            client: httpx.AsyncClient, path: str
        ) -> tuple[str, list[FileHistoryEntry]]:
            async with sem:
                try:
                    resp = await client.get(
                        base,
                        headers=self._headers(),
                        params={
                            "path": path,
                            "sha": pr_info.head_sha or pr_info.head_branch,
                            "pageSize": max_per_file,
                        },
                    )
                    if resp.status_code != 200:
                        return path, []
                    data = resp.json() or {}
                    commits = data.get("commits") or data.get("items") or []
                except Exception as exc:
                    logger.debug("Origin file history failed for %s: %s", path, exc)
                    return path, []

            entries: list[FileHistoryEntry] = []
            for item in commits[:max_per_file]:
                commit = item.get("commit") or item
                message = (commit.get("message") or "") if isinstance(commit, dict) else ""
                author_obj = (commit.get("author") or {}) if isinstance(commit, dict) else {}
                author = author_obj.get("name") or ""
                date = author_obj.get("date") or ""
                sha = str(item.get("sha") or "")[:8]
                entries.append(
                    FileHistoryEntry(
                        sha=sha,
                        message=message.split("\n\n", 1)[0][:300],
                        author=author,
                        date=date,
                    )
                )
            return path, entries

        async with httpx.AsyncClient(timeout=60) as client:
            results = await asyncio.gather(*[_fetch_one(client, p) for p in paths])
        return {path: hist for path, hist in results if hist}

    # ── posting ─────────────────────────────────────────────────────

    async def post_review(
        self, pr_info: PRInfo, result: ReviewResult, bot_name: str = "miracodeai"
    ) -> list[int]:
        if not result.comments:
            return []

        summary_text = ""
        if result.summary:
            summary_text = f"**Mira Review Summary**\n\n{result.summary}"
        if result.key_issues:
            summary_text += format_key_issues(result.key_issues)

        review_body: dict[str, Any] = {
            "verdict": "comment",
            "body": summary_text or "",
            "comments": [
                {
                    "body": format_comment_body(comment, bot_name=bot_name),
                    "inline": {
                        "path": comment.path,
                        "side": "right",
                        "startLine": comment.line,
                        **(
                            {"endLine": comment.end_line}
                            if comment.end_line and comment.end_line > comment.line
                            else {}
                        ),
                    },
                }
                for comment in result.comments
            ],
        }

        try:
            resp = await self._request("POST", f"{self._pr(pr_info)}/reviews", json=review_body)
            data = resp.json() or {}
            # Prefer per-comment ids when present; otherwise return empty ids.
            ids: list[int] = []
            for c in data.get("comments") or []:
                cid = c.get("id")
                ids.append(self._intern(str(cid)) if cid else 0)
            while len(ids) < len(result.comments):
                ids.append(0)
            return ids[: len(result.comments)]
        except ProviderError as exc:
            status = getattr(exc, "status_code", None)
            if status in (400, 422):
                logger.warning("Origin inline review failed (%s); posting as individual comments", exc)
                if summary_text:
                    try:
                        await self.post_comment(pr_info, summary_text)
                    except ProviderError:
                        logger.warning("Failed to post Origin PR summary comment (fallback)")
                ids = []
                for comment in result.comments:
                    body = format_comment_body(comment, bot_name=bot_name)
                    try:
                        cid = await self._post_inline_comment(pr_info, comment.path, comment.line, body)
                        ids.append(cid)
                    except ProviderError:
                        logger.warning(
                            "Origin comment fallback failed for %s:%s",
                            comment.path,
                            comment.line,
                        )
                        ids.append(0)
                return ids
            raise

    async def _post_inline_comment(
        self, pr_info: PRInfo, path: str, line: int, body: str
    ) -> int:
        resp = await self._request(
            "POST",
            f"{self._pr(pr_info)}/comments",
            json={
                "body": body,
                "inline": {"path": path, "side": "right", "startLine": line},
            },
        )
        cid = (resp.json() or {}).get("id")
        return self._intern(str(cid)) if cid else 0

    async def post_comment(self, pr_info: PRInfo, body: str) -> None:
        await self._request(
            "POST",
            f"{self._pr(pr_info)}/comments",
            json={"body": body},
        )

    async def find_bot_comment(self, pr_info: PRInfo, marker: str) -> int | None:
        try:
            comments = await self._paginate(f"{self._pr(pr_info)}/comments", "comments")
        except Exception as e:
            raise ProviderError(f"Failed to list Origin PR comments: {e}") from e
        me = await self._self_identity()
        for comment in comments:
            if marker not in (comment.get("body") or ""):
                continue
            author = _actor_handle(comment.get("author"))
            # Prefer our own comments when we know our identity.
            if me and author and author != me:
                continue
            cid = comment.get("id")
            if cid:
                return self._intern(str(cid))
        return None

    async def update_comment(self, pr_info: PRInfo, comment_id: int, body: str) -> None:
        sid = self._resolve_id(comment_id)
        await self._request(
            "PATCH",
            f"{self._pr(pr_info)}/comments/{quote(sid, safe='')}",
            json={"body": body},
        )

    async def get_comment_body(self, pr_info: PRInfo, comment_id: int) -> str:
        try:
            sid = self._resolve_id(comment_id) if isinstance(comment_id, int) else str(comment_id)
        except ProviderError:
            sid = str(comment_id)
        try:
            resp = await self._request(
                "GET", f"{self._pr(pr_info)}/comments/{quote(sid, safe='')}"
            )
            return ((resp.json() or {}).get("body") or "")[:1500]
        except Exception:
            return ""

    async def reply_to_review_comment(self, pr_info: PRInfo, comment_id: int, body: str) -> None:
        try:
            sid = self._resolve_id(comment_id) if isinstance(comment_id, int) else str(comment_id)
        except ProviderError:
            sid = str(comment_id)
        # Prefer threading via threadId when comment_id is actually a thread id;
        # Origin Create Comment accepts threadId for replies.
        await self._request(
            "POST",
            f"{self._pr(pr_info)}/comments",
            json={"body": body, "threadId": sid},
        )

    # ── reviews / threads ──────────────────────────────────────────

    async def get_all_bot_threads(
        self, pr_info: PRInfo, bot_login: str | None = None
    ) -> list[BotThreadRecord]:
        bot_identities = {n for n in (await self._self_identity(), bot_login) if n}
        records: list[BotThreadRecord] = []
        try:
            comments = await self._paginate(f"{self._pr(pr_info)}/comments", "comments")
        except Exception as e:
            raise ProviderError(f"Failed to fetch Origin PR comments: {e}") from e

        # Group by thread; keep the root (first) bot-authored comment as the record.
        seen_threads: set[str] = set()
        for comment in comments:
            thread = comment.get("thread") or {}
            thread_id = str(thread.get("id") or comment.get("id") or "")
            if not thread_id or thread_id in seen_threads:
                continue
            author = _actor_handle(comment.get("author"))
            if bot_identities and author and author not in bot_identities:
                continue
            seen_threads.add(thread_id)
            records.append(
                BotThreadRecord(
                    thread_id=thread_id,
                    path=thread.get("path") or "",
                    line=int(thread.get("startLine") or thread.get("endLine") or 0),
                    body=comment.get("body") or "",
                    is_resolved=bool(thread.get("resolvedAt")),
                    is_outdated=False,
                )
            )
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
        resolved = 0
        for tid in thread_ids:
            try:
                await self._request(
                    "PATCH",
                    f"{self._pr(pr_info)}/threads/{quote(tid, safe='')}",
                    json={"resolved": True},
                )
                resolved += 1
            except ProviderError as exc:
                logger.debug("Failed to resolve Origin thread %s: %s", tid, exc)
        return resolved

    async def resolve_outdated_review_threads(self, pr_info: PRInfo) -> int:
        # Origin threads don't expose an "outdated" flag the way GitHub does.
        logger.debug("resolve_outdated_review_threads is a no-op for Origin")
        return 0

    async def get_thread_id_for_comment(self, comment_node_id: str, pr_info: PRInfo) -> str | None:
        try:
            resp = await self._request(
                "GET",
                f"{self._pr(pr_info)}/comments/{quote(comment_node_id, safe='')}",
                ok=(200, 404),
            )
            if resp.status_code == 404:
                return None
            thread = (resp.json() or {}).get("thread") or {}
            tid = thread.get("id")
            return str(tid) if tid else None
        except Exception:
            return None

    async def get_human_review_comments(
        self, pr_info: PRInfo, bot_login: str
    ) -> list[HumanReviewComment]:
        bot_identities = {n for n in (await self._self_identity(), bot_login) if n}
        out: list[HumanReviewComment] = []
        try:
            comments = await self._paginate(f"{self._pr(pr_info)}/comments", "comments")
        except Exception as e:
            raise ProviderError(f"Failed to fetch Origin comments: {e}") from e
        for comment in comments:
            author = _actor_handle(comment.get("author"))
            if author in bot_identities:
                continue
            thread = comment.get("thread") or {}
            path = thread.get("path") or ""
            if not path:
                continue  # skip general discussion for merge-time learning
            out.append(
                HumanReviewComment(
                    path=path,
                    line=int(thread.get("startLine") or thread.get("endLine") or 0),
                    body=comment.get("body") or "",
                    author=author,
                )
            )
        return out

    # ── labels / description ────────────────────────────────────────

    async def get_pr_description(self, pr_info: PRInfo) -> str:
        resp = await self._request("GET", self._pr(pr_info))
        return (resp.json() or {}).get("body") or ""

    async def update_pr_description(self, pr_info: PRInfo, body: str) -> None:
        await self._request("PATCH", self._pr(pr_info), json={"body": body})

    async def add_label(self, pr_info: PRInfo, label: str) -> None:
        await self._request(
            "POST",
            f"{self._pr(pr_info)}/labels",
            json={"labels": [label]},
        )

    async def remove_label(self, pr_info: PRInfo, label: str) -> None:
        await self._request(
            "DELETE",
            f"{self._pr(pr_info)}/labels/{quote(label, safe='')}",
        )

    async def get_discussion_root_body(self, pr_info: PRInfo, discussion_id: str) -> str:
        try:
            comments = await self._paginate(f"{self._pr(pr_info)}/comments", "comments")
        except Exception:
            return ""
        for comment in comments:
            thread = comment.get("thread") or {}
            if str(thread.get("id") or "") == discussion_id or str(comment.get("id") or "") == discussion_id:
                return (comment.get("body") or "")[:1500]
        return ""
