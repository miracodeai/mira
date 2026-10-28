"""Bitbucket Cloud provider using the REST 2.0 API (``api.bitbucket.org/2.0``).

Mirrors GitHubProvider but speaks Bitbucket Cloud: pull requests live under
``/repositories/{workspace}/{repo_slug}/pullrequests/{id}``, review threads are
a root comment plus ``parent``-linked replies, inline comments anchor on
``inline.to`` (a line in the new file), and resolution is a per-comment
``/resolve`` call. There is no batch-review endpoint, so findings post one
comment each. Bitbucket PRs have no labels, so the pause label lives in the app
database (``mira.platforms.bitbucket.state``).

``owner`` is the workspace slug, ``repo`` the repository slug.

PR URL shape: ``https://bitbucket.org/workspace/repo/pull-requests/123``.
"""

from __future__ import annotations

import asyncio
import logging
import os
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
from mira.platforms.bitbucket import state
from mira.platforms.bitbucket.auth import (
    bitbucket_auth_headers,
    learned_bot_user,
    remember_bot_user,
    user_handle,
    user_identifiers,
)
from mira.providers.base import BaseProvider
from mira.providers.formatting import format_comment_body, format_key_issues

logger = logging.getLogger(__name__)

# https://bitbucket.org/workspace/repo/pull-requests/123[/diff|/overview|…]
_PR_URL_PATTERN = re.compile(
    r"(?:https?://[^/]+/)?(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)/pull-requests/(?P<number>\d+)"
)


def parse_pr_url(pr_url: str) -> tuple[str, str, int]:
    """Parse a PR URL into (workspace, repo_slug, id)."""
    match = _PR_URL_PATTERN.match(pr_url.strip())
    if not match:
        raise ProviderError(
            f"Cannot parse PR URL: {pr_url}. Expected "
            "https://bitbucket.org/workspace/repo/pull-requests/123"
        )
    return match.group("owner"), match.group("repo"), int(match.group("number"))


def _quote_path(path: str) -> str:
    return quote(path, safe="/")


# Bitbucket's markdown escapes raw HTML, so Mira's GitHub-flavoured bodies would
# show `<!-- marker -->` and `<details>` tags as text. Outgoing bodies are
# rewritten (outside fenced code): HTML comments become link-reference
# definitions, which render as nothing; <details>/<summary>/<b> become markdown.
# Incoming bodies map the definitions back, so marker lookups are unchanged.
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_HTML_COMMENT = re.compile(r"<!--\s*(.*?)\s*-->", re.DOTALL)
_HIDDEN_REF = re.compile(r"^\[//\]: # \((.*)\)$", re.MULTILINE)
_SUMMARY = re.compile(r"<summary>(.*?)</summary>", re.DOTALL | re.IGNORECASE)
_BOLD = re.compile(r"<(b|strong)>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
_DETAILS = re.compile(r"</?details>", re.IGNORECASE)
_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)


def _outside_fences(text: str, transform: Any) -> str:
    """Apply ``transform`` to the prose segments of ``text``, not fenced code."""
    out: list[str] = []
    prose: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        m = _FENCE.match(line)
        if fence is None and m:
            out.append(transform("\n".join(prose)))
            prose = []
            fence = m.group(1)[0] * len(m.group(1))
            out.append(line)
        elif fence is not None:
            out.append(line)
            if line.strip().startswith(fence) and line.strip().strip(fence[0]) == "":
                fence = None
        else:
            prose.append(line)
    out.append(transform("\n".join(prose)))
    return "\n".join(out)


def _prose_to_bitbucket(text: str) -> str:
    text = _HTML_COMMENT.sub(lambda m: f"\n\n[//]: # ({m.group(1).strip()})\n\n", text)
    text = _BOLD.sub(lambda m: f"**{m.group(2).strip()}**", text)
    text = _SUMMARY.sub(lambda m: f"\n{_bold(m.group(1).strip())}\n", text)
    text = _DETAILS.sub("", text)
    text = _BREAK.sub("\n", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def _bold(label: str) -> str:
    return label if label.startswith("**") else f"**{label}**"


def to_bitbucket_markdown(body: str) -> str:
    """Rewrite a Mira (GitHub-flavoured) body into markdown Bitbucket renders."""
    return _outside_fences(body, _prose_to_bitbucket).strip()


def from_bitbucket_markdown(raw: str) -> str:
    """Map hidden link-reference markers back to the HTML comments Mira expects."""
    return _HIDDEN_REF.sub(lambda m: f"<!-- {m.group(1)} -->", raw)


class BitbucketProvider(BaseProvider):
    """Bitbucket Cloud code hosting provider (REST 2.0)."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ProviderError("Bitbucket token is required")
        self._token = token
        self._api = profiles.resolve("bitbucket")["api_url"] or "https://api.bitbucket.org/2.0"
        self._bot_override = os.environ.get("MIRA_BITBUCKET_BOT_USER", "") or ""
        self._bot_ids: set[str] | None = None

    # ── low-level HTTP ──────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return bitbucket_auth_headers(self._token)

    def _repo_url(self, owner: str, repo: str) -> str:
        return f"{self._api}/repositories/{quote(owner, safe='')}/{quote(repo, safe='')}"

    def _repo(self, pr_info: PRInfo) -> str:
        return self._repo_url(pr_info.owner, pr_info.repo)

    def _pr(self, pr_info: PRInfo) -> str:
        return f"{self._repo(pr_info)}/pullrequests/{pr_info.number}"

    async def _request(
        self, method: str, url: str, *, ok: tuple[int, ...] = (200, 201), **kw: Any
    ) -> httpx.Response:
        headers = {**self._headers(), **kw.pop("headers", {})}
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.request(method, url, headers=headers, **kw)
        if resp.status_code not in ok:
            err = ProviderError(f"Bitbucket {method} {url} → {resp.status_code}: {resp.text[:300]}")
            err.status_code = resp.status_code  # type: ignore[attr-defined]
            raise err
        return resp

    async def _paginate(self, url: str) -> list[dict[str, Any]]:
        """Follow Bitbucket's ``next`` links, collecting every ``values`` entry."""
        out: list[dict[str, Any]] = []
        next_url: str | None = url + ("&" if "?" in url else "?") + "pagelen=100"
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            while next_url:
                resp = await client.get(next_url, headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
                out.extend(data.get("values") or [])
                next_url = data.get("next")
        return out

    async def _self_ids(self) -> set[str]:
        """Every identifier of the token's user — who comments are posted as.

        Access tokens get a 403 from ``GET /user``; their bot user is then the
        one learned from a previously posted comment.
        """
        if self._bot_ids is None:
            ids: set[str] = {self._bot_override} if self._bot_override else set()
            try:
                resp = await self._request("GET", f"{self._api}/user")
                ids |= user_identifiers(resp.json())
            except Exception as exc:
                logger.debug("GET /user unavailable for this credential: %s", exc)
            self._bot_ids = ids
        return self._bot_ids | user_identifiers(learned_bot_user())

    async def _bot_identities(self, bot_login: str | None) -> set[str]:
        ids = set(await self._self_ids())
        if bot_login:
            ids.add(bot_login)
        return ids

    @staticmethod
    def _is_by(comment: dict[str, Any], identities: set[str]) -> bool:
        return bool(identities & user_identifiers(comment.get("user")))

    # ── PR read ─────────────────────────────────────────────────────

    async def get_pr_info(self, pr_url: str) -> PRInfo:
        owner, repo, number = parse_pr_url(pr_url)
        try:
            resp = await self._request(
                "GET", f"{self._repo_url(owner, repo)}/pullrequests/{number}"
            )
            pr = resp.json()
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to fetch PR info: {e}") from e
        source = pr.get("source") or {}
        destination = pr.get("destination") or {}
        author = pr.get("author") or {}
        return PRInfo(
            title=pr.get("title") or "",
            description=from_bitbucket_markdown(
                pr.get("description") or (pr.get("summary") or {}).get("raw") or ""
            ),
            base_branch=(destination.get("branch") or {}).get("name") or "",
            head_branch=(source.get("branch") or {}).get("name") or "",
            url=((pr.get("links") or {}).get("html") or {}).get("href") or pr_url,
            number=pr.get("id") or number,
            owner=owner,
            repo=repo,
            head_sha=(source.get("commit") or {}).get("hash") or "",
            platform="bitbucket",
            author=user_handle(author),
            author_avatar_url=((author.get("links") or {}).get("avatar") or {}).get("href") or "",
        )

    async def get_repo_permission(
        self,
        owner: str,
        repo: str,
        username: str,
        *,
        user_id: int | None = None,
    ) -> str | None:
        """A user's effective repository permission, normalized for authz.

        Uses the workspace permissions API, which needs a credential with
        workspace admin rights. Without it, list the user in
        ``commands.allowed_users`` instead.
        """
        url = (
            f"{self._api}/workspaces/{quote(owner, safe='')}/permissions/repositories/"
            f"{quote(repo, safe='')}"
        )
        try:
            grants = await self._paginate(url)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                raise ProviderError(
                    f"Bitbucket denied the permission lookup for {username!r}. The "
                    "credential needs workspace admin rights to read repository "
                    "permissions; otherwise list the user in commands.allowed_users."
                ) from exc
            if exc.response.status_code == 404:
                return "none"
            raise ProviderError(f"Failed to fetch repo permission for {username}: {exc}") from exc
        except Exception as exc:
            raise ProviderError(f"Failed to fetch repo permission for {username}: {exc}") from exc

        wanted = username.lower()
        grant = next(
            (g for g in grants if wanted in {i.lower() for i in user_identifiers(g.get("user"))}),
            None,
        )
        if not grant:
            return "none"
        permission = str(grant.get("permission") or "").lower()
        if permission in ("admin", "write"):
            return "write"
        if permission == "read":
            return "read"
        if permission == "none":
            return "none"
        return None

    async def get_pr_diff(self, pr_info: PRInfo) -> str:
        """Raw unified diff for the PR (Bitbucket redirects to the diff endpoint)."""
        try:
            resp = await self._request("GET", f"{self._pr(pr_info)}/diff", ok=(200, 404))
            if resp.status_code == 404:
                logger.warning("PR diff returned 404 for %s", pr_info.url)
                return ""
            return resp.text
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Failed to fetch PR diff: {e}") from e

    async def get_compare_diff(self, pr_info: PRInfo, base_sha: str, head_sha: str) -> str:
        """Diff between two commits, for round 2+ incremental reviews.

        Bitbucket's spec is ``source..destination`` (changes in ``source``);
        ``topic=false`` asks for a plain two-dot diff rather than one against
        the merge base.
        """
        if base_sha == head_sha or not base_sha or not head_sha:
            return ""
        spec = f"{quote(head_sha, safe='')}..{quote(base_sha, safe='')}"
        url = f"{self._repo(pr_info)}/diff/{spec}?topic=false"
        try:
            resp = await self._request("GET", url, ok=(200, 404))
            return resp.text if resp.status_code == 200 else ""
        except ProviderError as exc:
            logger.warning("Compare diff failed: %s", exc)
            return ""
        except Exception as e:
            raise ProviderError(f"Failed to fetch compare diff: {e}") from e

    async def get_file_content(self, pr_info: PRInfo, path: str, ref: str) -> str:
        """Raw file content at a ref — used to verify a thread's fix landed."""
        url = f"{self._repo(pr_info)}/src/{quote(ref, safe='')}/{_quote_path(path)}"
        try:
            resp = await self._request("GET", url, ok=(200, 404))
        except ProviderError as exc:
            logger.warning("Failed to fetch %s@%s: %s", path, ref, exc)
            return ""
        return resp.text if resp.status_code == 200 else ""

    async def get_repo_tree(self, pr_info: PRInfo, ref: str) -> list[str]:
        """Every file path in the repo at a ref, for JIT cross-file context."""
        url = f"{self._repo(pr_info)}/src/{quote(ref, safe='')}/?max_depth=100"
        try:
            items = await self._paginate(url)
        except Exception as exc:
            logger.debug("Failed to fetch repo tree: %s", exc)
            return []
        return [it["path"] for it in items if it.get("type") == "commit_file" and it.get("path")]

    async def get_file_history(
        self, pr_info: PRInfo, paths: list[str], max_per_file: int = 5
    ) -> dict[str, list[FileHistoryEntry]]:
        """Recent commits per file (most-recent first), for decision archaeology."""
        if not paths:
            return {}

        sem = asyncio.Semaphore(8)
        ref = pr_info.head_sha or pr_info.head_branch
        # `?include=` resolves any commit; `/commits/{rev}` 404s on some heads.
        base = f"{self._repo(pr_info)}/commits"
        headers = self._headers()

        async def _fetch_one(
            client: httpx.AsyncClient, path: str
        ) -> tuple[str, list[FileHistoryEntry]]:
            async with sem:
                try:
                    resp = await client.get(
                        base,
                        headers=headers,
                        params={"include": ref, "path": path, "pagelen": max_per_file},
                    )
                    if resp.status_code != 200:
                        return path, []
                    data = resp.json().get("values") or []
                except Exception as exc:
                    logger.debug("File history fetch failed for %s: %s", path, exc)
                    return path, []

            entries: list[FileHistoryEntry] = []
            for item in data[:max_per_file]:
                message = (item.get("message") or "").strip()
                author = item.get("author") or {}
                name = user_handle(author.get("user")) or str(author.get("raw") or "")
                entries.append(
                    FileHistoryEntry(
                        sha=str(item.get("hash", ""))[:8],
                        message=message.split("\n\n", 1)[0][:300],
                        author=name,
                        date=str(item.get("date", "")),
                    )
                )
            return path, entries

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            results = await asyncio.gather(*[_fetch_one(client, p) for p in paths])
        return {path: hist for path, hist in results if hist}

    # ── posting ─────────────────────────────────────────────────────

    async def _post_pr_comment(self, pr_info: PRInfo, payload: dict[str, Any]) -> int:
        raw = (payload.get("content") or {}).get("raw")
        if raw is not None:
            payload = {**payload, "content": {"raw": to_bitbucket_markdown(raw)}}
        resp = await self._request("POST", f"{self._pr(pr_info)}/comments", json=payload)
        try:
            data = resp.json() or {}
            remember_bot_user(data.get("user"))
            return int(data.get("id") or 0)
        except (TypeError, ValueError):
            return 0

    async def post_review(
        self, pr_info: PRInfo, result: ReviewResult, bot_name: str = "miracodeai"
    ) -> list[int]:
        """Post each finding as an inline comment, then the summary as a PR comment.

        Bitbucket accepts any anchor line, even one outside the diff, so the
        engine's unanchorable-comment filter is what keeps findings on real
        lines. A comment Bitbucket does reject (e.g. a path not in the PR)
        falls back to a plain PR comment naming the file and line.
        """
        if not result.comments:
            return []

        ids: list[int] = []
        for comment in result.comments:
            body = format_comment_body(comment, bot_name=bot_name)
            line = (
                comment.end_line
                if comment.end_line and comment.end_line > comment.line
                else comment.line
            )
            try:
                ids.append(
                    await self._post_pr_comment(
                        pr_info,
                        {"content": {"raw": body}, "inline": {"path": comment.path, "to": line}},
                    )
                )
            except ProviderError as exc:
                logger.warning(
                    "Inline comment failed for %s:%s (%s); posting as a PR comment",
                    comment.path,
                    line,
                    exc,
                )
                note = f"**`{comment.path}:{line}`**\n\n{body}"
                try:
                    await self.post_comment(pr_info, note)
                except ProviderError:
                    logger.warning(
                        "Plain-comment fallback also failed for %s:%s", comment.path, line
                    )
                ids.append(0)

        summary = ""
        if result.summary:
            summary = f"**Mira Review Summary**\n\n{result.summary}"
        if result.key_issues:
            summary += format_key_issues(result.key_issues)
        if summary:
            try:
                await self.post_comment(pr_info, summary)
            except ProviderError as exc:
                logger.warning("Failed to post PR summary comment: %s", exc)

        logger.info(
            "Bitbucket: posted %d/%d inline comments",
            sum(1 for i in ids if i),
            len(result.comments),
        )
        return ids

    async def post_comment(self, pr_info: PRInfo, body: str) -> None:
        await self._post_pr_comment(pr_info, {"content": {"raw": body}})

    async def _comments(self, pr_info: PRInfo) -> list[dict[str, Any]]:
        # The list view returns a resolved comment's `resolution` as `{}` unless
        # its fields are requested explicitly.
        comments = await self._paginate(
            f"{self._pr(pr_info)}/comments?fields=%2Bvalues.resolution.type"
        )
        for c in comments:
            content = c.get("content")
            if isinstance(content, dict) and isinstance(content.get("raw"), str):
                content["raw"] = from_bitbucket_markdown(content["raw"])
        return [c for c in comments if not c.get("deleted")]

    async def find_bot_comment(self, pr_info: PRInfo, marker: str) -> int | None:
        try:
            comments = await self._comments(pr_info)
        except Exception as e:
            raise ProviderError(f"Failed to list PR comments: {e}") from e
        for comment in comments:
            if comment.get("inline") or comment.get("parent"):
                continue
            if marker in ((comment.get("content") or {}).get("raw") or ""):
                return int(comment["id"])
        return None

    async def update_comment(self, pr_info: PRInfo, comment_id: int, body: str) -> None:
        await self._request(
            "PUT",
            f"{self._pr(pr_info)}/comments/{comment_id}",
            json={"content": {"raw": to_bitbucket_markdown(body)}},
        )

    async def get_comment_body(self, pr_info: PRInfo, comment_id: int) -> str:
        """Fetch a comment's raw body by id. Best-effort."""
        try:
            resp = await self._request("GET", f"{self._pr(pr_info)}/comments/{comment_id}")
            raw = ((resp.json() or {}).get("content") or {}).get("raw") or ""
            return from_bitbucket_markdown(raw)[:1500]
        except Exception:
            return ""

    async def get_discussion_root_body(self, pr_info: PRInfo, discussion_id: str) -> str:
        """A thread's root comment body. A thread id *is* its root comment id."""
        try:
            return await self.get_comment_body(pr_info, int(discussion_id))
        except (TypeError, ValueError):
            return ""

    async def reply_to_review_comment(self, pr_info: PRInfo, comment_id: int, body: str) -> None:
        """Reply in the comment's thread (Bitbucket threads by ``parent``)."""
        await self._post_pr_comment(
            pr_info, {"content": {"raw": body}, "parent": {"id": int(comment_id)}}
        )

    # ── threads ─────────────────────────────────────────────────────

    @staticmethod
    def _inline_line(comment: dict[str, Any]) -> int:
        inline = comment.get("inline") or {}
        return int(inline.get("to") or inline.get("from") or 0)

    async def get_all_bot_threads(
        self, pr_info: PRInfo, bot_login: str | None = None
    ) -> list[BotThreadRecord]:
        try:
            comments = await self._comments(pr_info)
        except Exception as e:
            raise ProviderError(f"Failed to fetch PR comments: {e}") from e
        identities = await self._bot_identities(bot_login)
        records: list[BotThreadRecord] = []
        for c in comments:
            if c.get("parent") or not c.get("inline"):
                continue
            # Unknown identity matches nothing: never claim (or resolve) a human's thread.
            if not self._is_by(c, identities):
                continue
            inline = c.get("inline") or {}
            records.append(
                BotThreadRecord(
                    thread_id=str(c["id"]),
                    path=inline.get("path") or "",
                    line=self._inline_line(c),
                    body=(c.get("content") or {}).get("raw") or "",
                    is_resolved=c.get("resolution") is not None,
                    is_outdated=bool(inline.get("outdated")),
                )
            )
        return records

    async def get_unresolved_bot_threads(
        self, pr_info: PRInfo, bot_login: str | None = None
    ) -> list[UnresolvedThread]:
        threads = await self.get_all_bot_threads(pr_info, bot_login)
        return [
            UnresolvedThread(
                thread_id=t.thread_id,
                path=t.path,
                line=t.line,
                body=t.body,
                is_outdated=t.is_outdated,
            )
            for t in threads
            if not t.is_resolved
        ]

    async def resolve_threads(self, pr_info: PRInfo, thread_ids: list[str]) -> int:
        resolved = 0
        for tid in thread_ids:
            try:
                await self._request(
                    "POST", f"{self._pr(pr_info)}/comments/{tid}/resolve", ok=(200, 201, 204)
                )
                resolved += 1
            except ProviderError as exc:
                logger.warning("Failed to resolve comment thread %s: %s", tid, exc)
        return resolved

    async def resolve_outdated_review_threads(self, pr_info: PRInfo) -> int:
        """Resolve unresolved bot threads whose anchor Bitbucket marks outdated."""
        threads = await self.get_unresolved_bot_threads(pr_info)
        outdated = [t.thread_id for t in threads if t.is_outdated]
        return await self.resolve_threads(pr_info, outdated) if outdated else 0

    async def get_thread_id_for_comment(self, comment_node_id: str, pr_info: PRInfo) -> str | None:
        """The root comment id of the thread containing ``comment_node_id``."""
        try:
            comments = await self._comments(pr_info)
        except Exception:
            return None
        parents = {str(c.get("id")): str((c.get("parent") or {}).get("id") or "") for c in comments}
        current = str(comment_node_id)
        if current not in parents:
            return None
        seen: set[str] = set()
        while parents.get(current) and current not in seen:
            seen.add(current)
            current = parents[current]
        return current

    async def get_human_review_comments(
        self, pr_info: PRInfo, bot_login: str
    ) -> list[HumanReviewComment]:
        try:
            comments = await self._comments(pr_info)
        except Exception as e:
            raise ProviderError(f"Failed to fetch human review comments: {e}") from e
        identities = await self._bot_identities(bot_login)
        out: list[HumanReviewComment] = []
        for c in comments:
            inline = c.get("inline")
            if not inline or self._is_by(c, identities):
                continue
            out.append(
                HumanReviewComment(
                    path=inline.get("path") or "",
                    line=self._inline_line(c),
                    body=(c.get("content") or {}).get("raw") or "",
                    author=user_handle(c.get("user")),
                )
            )
        return out

    # ── description / labels ────────────────────────────────────────

    async def get_pr_description(self, pr_info: PRInfo) -> str:
        resp = await self._request("GET", self._pr(pr_info))
        return from_bitbucket_markdown((resp.json() or {}).get("description") or "")

    async def update_pr_description(self, pr_info: PRInfo, body: str) -> None:
        # Bitbucket's PUT validates the title, so send it alongside.
        resp = await self._request("GET", self._pr(pr_info))
        title = (resp.json() or {}).get("title") or pr_info.title
        await self._request(
            "PUT",
            self._pr(pr_info),
            json={"title": title, "description": to_bitbucket_markdown(body)},
        )

    async def add_label(self, pr_info: PRInfo, label: str) -> None:
        state.add_label(pr_info.owner, pr_info.repo, pr_info.number, label)

    async def remove_label(self, pr_info: PRInfo, label: str) -> None:
        state.remove_label(pr_info.owner, pr_info.repo, pr_info.number, label)
