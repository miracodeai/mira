from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

from mira.models import PRInfo

logger = logging.getLogger(__name__)

_API_URL = "https://api.linear.app/graphql"
_ISSUE_RE = re.compile(r"(?<![A-Z0-9])([A-Z][A-Z0-9]*-\d+)(?![A-Z0-9])", re.IGNORECASE)
_MAX_ISSUES = 3
_MAX_DESCRIPTION_CHARS = 10000
_MAX_COMMENT_CHARS = 1500
_MAX_CONTEXT_CHARS = 18000

_ISSUE_QUERY = """
query ReviewIssue($id: String!) {
  issue(id: $id) {
    identifier
    title
    description
    url
    priorityLabel
    state { name }
    assignee { name }
    project { name }
    labels { nodes { name } }
    comments(last: 20) {
      nodes {
        body
        user { name }
      }
    }
  }
}
"""


def extract_linear_issue_identifiers(pr_info: PRInfo) -> list[str]:
    sources = (pr_info.title, pr_info.description, pr_info.head_branch)
    identifiers: list[str] = []
    seen: set[str] = set()
    for source in sources:
        for match in _ISSUE_RE.finditer(source or ""):
            identifier = match.group(1).upper()
            if identifier not in seen:
                seen.add(identifier)
                identifiers.append(identifier)
            if len(identifiers) == _MAX_ISSUES:
                return identifiers
    return identifiers


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _format_issue(issue: dict[str, Any]) -> str:
    identifier = _clean_text(issue.get("identifier"), 100)
    title = _clean_text(issue.get("title"), 500)
    lines = [f"### {identifier}: {title}"]

    details: list[str] = []
    for label, value in (
        ("Status", (issue.get("state") or {}).get("name")),
        ("Priority", issue.get("priorityLabel")),
        ("Assignee", (issue.get("assignee") or {}).get("name")),
        ("Project", (issue.get("project") or {}).get("name")),
    ):
        cleaned = _clean_text(value, 300)
        if cleaned:
            details.append(f"- {label}: {cleaned}")

    labels = [
        _clean_text(node.get("name"), 100)
        for node in (issue.get("labels") or {}).get("nodes", [])
        if isinstance(node, dict)
    ]
    labels = [label for label in labels if label]
    if labels:
        details.append(f"- Labels: {', '.join(labels)}")

    url = _clean_text(issue.get("url"), 1000)
    if url:
        details.append(f"- URL: {url}")
    if details:
        lines.extend(details)

    description = _clean_text(issue.get("description"), _MAX_DESCRIPTION_CHARS)
    if description:
        lines.extend(("", "#### Description and acceptance criteria", description))

    comments: list[str] = []
    for node in (issue.get("comments") or {}).get("nodes", []):
        if not isinstance(node, dict):
            continue
        body = _clean_text(node.get("body"), _MAX_COMMENT_CHARS)
        if not body:
            continue
        author = _clean_text((node.get("user") or {}).get("name"), 200) or "Unknown"
        comments.append(f"- {author}: {body}")
    if comments:
        lines.extend(("", "#### Discussion", *comments))

    return "\n".join(lines)


async def fetch_linear_context(
    pr_info: PRInfo,
    api_key: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> str:
    key = api_key if api_key is not None else os.environ.get("MIRA_LINEAR_API_KEY", "")
    identifiers = extract_linear_issue_identifiers(pr_info)
    if not key or not identifiers:
        return ""

    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=10)
    issues: list[str] = []
    try:
        for identifier in identifiers:
            try:
                response = await active_client.post(
                    _API_URL,
                    headers={"Authorization": key, "Content-Type": "application/json"},
                    json={"query": _ISSUE_QUERY, "variables": {"id": identifier}},
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("errors"):
                    raise ValueError(payload["errors"][0].get("message", "GraphQL request failed"))
                issue = (payload.get("data") or {}).get("issue")
                if isinstance(issue, dict):
                    issues.append(_format_issue(issue))
            except Exception as exc:
                logger.warning("Could not load Linear issue %s: %s", identifier, exc)
    finally:
        if owns_client:
            await active_client.aclose()

    if not issues:
        return ""
    context = "## Linked Linear context\n\n" + "\n\n".join(issues)
    return context[:_MAX_CONTEXT_CHARS]
