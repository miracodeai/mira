"""Export merged PRs and their Greptile comments into a Hickory manifest.

Produces the same shape as ``fixtures/hickory/manifest.json`` so a nightly
run (HAI-11608) can score a fresh window. Greptile inline comments become
``findings``; the summary comment stays provenance only.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

GREPTILE_LOGIN = "greptile-apps[bot]"
_API = "https://api.github.com"
_BADGE = re.compile(r'alt="(P[0-3])"')
_SEVERITY = {"P0": "blocker", "P1": "blocker", "P2": "warning", "P3": "warning"}

_THREADS_QUERY = """
query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { isResolved isOutdated comments(first: 1) { nodes { databaseId } } }
      }
    }
  }
}
"""


class GitHubReader:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient(timeout=60)
        self._headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}

    async def get(self, path: str, **params: Any) -> Any:
        resp = await self._client.get(f"{_API}{path}", params=params, headers=self._headers)
        resp.raise_for_status()
        return resp.json()

    async def paged(self, path: str, **params: Any) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            batch = await self.get(path, per_page=100, page=page, **params)
            items = batch.get("items", []) if isinstance(batch, dict) else batch
            out.extend(items)
            if len(items) < 100:
                return out
            page += 1

    async def graphql(self, query: str, variables: dict) -> dict:
        resp = await self._client.post(
            f"{_API}/graphql", json={"query": query, "variables": variables}, headers=self._headers
        )
        resp.raise_for_status()
        return resp.json()


async def _thread_state(
    gh: GitHubReader, owner: str, repo: str, number: int
) -> dict[int, tuple[bool, bool]]:
    state: dict[int, tuple[bool, bool]] = {}
    cursor = None
    while True:
        data = await gh.graphql(
            _THREADS_QUERY, {"owner": owner, "repo": repo, "number": number, "cursor": cursor}
        )
        threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        for node in threads["nodes"]:
            for c in node["comments"]["nodes"]:
                state[int(c["databaseId"])] = (bool(node["isResolved"]), bool(node["isOutdated"]))
        if not threads["pageInfo"]["hasNextPage"]:
            return state
        cursor = threads["pageInfo"]["endCursor"]


async def export_pr(gh: GitHubReader, repo: str, number: int, split: str) -> dict:
    owner, name = repo.split("/", 1)
    pr = await gh.get(f"/repos/{repo}/pulls/{number}")
    issue_comments = await gh.paged(f"/repos/{repo}/issues/{number}/comments")
    review_comments = await gh.paged(f"/repos/{repo}/pulls/{number}/comments")
    threads = await _thread_state(gh, owner, name, number)

    comments: list[dict] = []
    for c in issue_comments:
        if (c.get("user") or {}).get("login") == GREPTILE_LOGIN:
            comments.append(
                {
                    "kind": "summary",
                    "url": c["html_url"],
                    "created_at": c["created_at"],
                    "updated_at": c["updated_at"],
                    "body": c["body"],
                }
            )
    findings: list[dict] = []
    inline = [
        c
        for c in review_comments
        if (c.get("user") or {}).get("login") == GREPTILE_LOGIN and not c.get("in_reply_to_id")
    ]
    inline.sort(key=lambda c: (c["created_at"], c["id"]))
    for c in inline:
        resolved, outdated = threads.get(int(c["id"]), (False, False))
        comments.append(
            {
                "kind": "inline",
                "url": c["html_url"],
                "created_at": c["created_at"],
                "updated_at": c["updated_at"],
                "path": c["path"],
                "line": c.get("line"),
                "original_line": c.get("original_line"),
                "start_line": c.get("start_line"),
                "original_start_line": c.get("original_start_line"),
                "commit_id": c.get("commit_id"),
                "original_commit_id": c.get("original_commit_id"),
                "body": c["body"],
                "thread_resolved": resolved,
                "thread_outdated": outdated,
            }
        )
        badge = _BADGE.search(c["body"] or "")
        line = c.get("original_line") or c.get("line")
        findings.append(
            {
                "id": f"hickory-{number}-{len(findings) + 1}",
                "path": c["path"],
                "line_start": c.get("original_start_line") or line,
                "line_end": line,
                "description": c["body"],
                "category": "bug",
                "severity": _SEVERITY.get(badge.group(1) if badge else "", "warning"),
            }
        )
    return {
        "repo": repo,
        "pr_url": pr["html_url"],
        "pr_number": number,
        "pr_title": pr["title"],
        "merged_at": pr["merged_at"],
        "base_sha": pr["base"]["sha"],
        "head_sha": pr["head"]["sha"],
        "language": "mixed",
        "source": "hickory",
        "split": split,
        "greptile_comments": comments,
        "findings": findings,
    }


async def export_window(
    token: str,
    repo: str,
    since: str,
    until: str,
    limit: int,
    split: str = "nightly",
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Merged PRs in ``[since, until]`` (YYYY-MM-DD), newest first, at most ``limit``."""
    gh = GitHubReader(token, client)
    found = await gh.paged(
        "/search/issues", q=f"repo:{repo} is:pr is:merged merged:{since}..{until}", sort="updated"
    )
    found.sort(key=lambda i: (i.get("pull_request") or {}).get("merged_at") or "", reverse=True)
    if limit:
        found = found[:limit]
    return [await export_pr(gh, repo, int(item["number"]), split) for item in found]
