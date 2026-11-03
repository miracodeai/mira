"""Read-only Linear import into the registry (HAI-11606).

Linear stays authoritative during the dual-write. This module only reads
Linear: the client refuses any GraphQL mutation. It has three parts:

- ``LinearClient``: GraphQL over HTTP with ``LINEAR_SYNC_API_KEY``, which is the
  sync's own key, so its calls are never counted as agent Linear calls. It
  retries 429, 5xx, and transport errors.
- ``inventory()``: counts what exists in Linear (teams, issues, comments, and
  documents) and names what the importer will not carry yet.
- ``LinearImporter``: resumable walks over issues and comments. Each page's
  cursor is saved after the page is imported, so a restart resumes where it
  stopped and a replayed page is a no-op. Issues still waiting on a link target
  are re-read at the end of each issue walk.

Run with ``python -m mira.registry.linear inventory|import``. It covers every team
in the workspace and every state, archived and trashed included; ``--team KEY``
narrows it to the named teams. ``import`` is a dry run unless ``--apply`` is given.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import datetime

import httpx

from mira.registry.model import Actor
from mira.registry.service import Registry

logger = logging.getLogger(__name__)

SOURCE_SYSTEM = "linear"
DEFAULT_ENDPOINT = "https://api.linear.app/graphql"
IMPORT_ACTOR = Actor("human", "linear-import")
PAGE_SIZE = 50
# A new walk starts this far before the last completed walk began, so an edit
# saved while that walk was paging is read again (idempotently) next time.
WATERMARK_OVERLAP = 300.0
MAX_ATTEMPTS = 5
# Relations Linear stores once, on the issue that holds them. Types not listed
# here ("similar") are reported by the inventory and not imported.
RELATION_LINKS = {"blocks": "blocks", "related": "relates", "duplicate": "relates"}
STATE_DELIVERY = {
    "triage": "planned",
    "backlog": "planned",
    "unstarted": "planned",
    "started": "active",
    "completed": "shipped",
    "canceled": "canceled",
    "duplicate": "canceled",
}
RELATIONS_PAGE = 50  # the relations(first:) in ISSUES_QUERY
PENDING_BATCH = 50  # ids per ``id: {in: [...]}`` re-read of pending issues

# Field names follow hickory-ai's production Linear poller
# (hickory-ui/backend/app/services/engineering_metrics/linear_poller.py).
ISSUES_QUERY = """
query RegistryIssues($filter: IssueFilter!, $after: String, $first: Int!) {
  issues(filter: $filter, after: $after, first: $first, orderBy: updatedAt, includeArchived: true) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id identifier title description url updatedAt archivedAt trashed
      team { key }
      state { type }
      assignee { displayName email }
      project { id }
      parent { id }
      relations(first: 50) {
        pageInfo { hasNextPage }
        nodes { type relatedIssue { id } }
      }
    }
  }
}
"""

COMMENTS_QUERY = """
query RegistryComments($filter: CommentFilter!, $after: String, $first: Int!) {
  comments(filter: $filter, after: $after, first: $first, orderBy: updatedAt, includeArchived: true) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id body url createdAt updatedAt archivedAt
      user { displayName email }
      issue { id team { key } }
    }
  }
}
"""

TEAMS_QUERY = """
query RegistryTeams($filter: TeamFilter!, $after: String, $first: Int!) {
  teams(filter: $filter, after: $after, first: $first, includeArchived: true) {
    pageInfo { hasNextPage endCursor }
    nodes { id key name archivedAt }
  }
}
"""

DOCUMENTS_QUERY = """
query RegistryDocuments($filter: DocumentFilter!, $after: String, $first: Int!) {
  documents(filter: $filter, after: $after, first: $first, orderBy: updatedAt, includeArchived: true) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id title url updatedAt archivedAt
      issue { id identifier team { key } }
      project { id }
      initiative { id }
    }
  }
}
"""

# Every GraphQL document this module sends; tests validate each against Linear's schema.
QUERIES = (ISSUES_QUERY, COMMENTS_QUERY, TEAMS_QUERY, DOCUMENTS_QUERY)


class LinearError(RuntimeError):
    """Linear refused or failed a request after retries."""


class LinearGraphQLError(LinearError):
    """Linear answered with GraphQL errors. These are not retried."""


class LinearCursorRejected(LinearError):
    """Linear refused a saved cursor: a GraphQL error on a resumed walk's first page."""


def to_epoch(iso: str | None) -> float:
    if not iso:
        return 0.0
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def to_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat()


def version(iso: str | None) -> int:
    """Linear's ``updatedAt`` in milliseconds: the registry ``source_version``."""
    return round(to_epoch(iso) * 1000)


class LinearClient:
    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise LinearError("LINEAR_SYNC_API_KEY is not set")
        self._http = http or httpx.Client(timeout=30.0)
        self._endpoint = endpoint
        self._headers = {
            "Authorization": api_key,
            "Content-Type": "application/json",
            "User-Agent": "kory-source-linear-sync",
        }
        self._sleep = sleep

    @classmethod
    def from_env(cls) -> LinearClient:
        return cls(
            os.environ.get("LINEAR_SYNC_API_KEY", ""),
            endpoint=os.environ.get("LINEAR_SYNC_ENDPOINT", DEFAULT_ENDPOINT),
        )

    def query(self, document: str, variables: dict) -> dict:
        if document.lstrip().lower().startswith("mutation"):
            raise LinearError("the Linear import client is read-only")
        last = ""
        for attempt in range(MAX_ATTEMPTS):
            try:
                resp = self._http.post(
                    self._endpoint,
                    headers=self._headers,
                    json={"query": document, "variables": variables},
                )
            except httpx.TransportError as exc:
                last = f"transport error: {exc}"
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    last = f"HTTP {resp.status_code}"
                    retry_after = resp.headers.get("retry-after", "")
                    if retry_after.isdigit():
                        self._sleep(float(retry_after))
                        continue
                elif resp.status_code != 200:
                    raise LinearError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                else:
                    body = resp.json()
                    if body.get("errors"):
                        messages = "; ".join(e.get("message", "") for e in body["errors"])
                        raise LinearGraphQLError(f"GraphQL error: {messages}")
                    return body["data"]
            if attempt < MAX_ATTEMPTS - 1:
                self._sleep(min(2.0**attempt, 30.0))
        raise LinearError(f"Linear request failed after {MAX_ATTEMPTS} attempts: {last}")

    def pages(
        self, document: str, connection: str, filt: dict, after: str | None = None
    ) -> Iterator[tuple[list[dict], str | None]]:
        """Yield ``(nodes, end_cursor)`` per page. ``end_cursor`` is None on the last page.

        A GraphQL error on the first page of a resumed walk means Linear rejected
        the saved cursor, so it raises ``LinearCursorRejected`` instead.
        """
        resumed = after is not None
        first = True
        while True:
            try:
                data = self.query(document, {"filter": filt, "after": after, "first": PAGE_SIZE})
            except LinearGraphQLError as exc:
                if resumed and first:
                    raise LinearCursorRejected(str(exc)) from exc
                raise
            first = False
            conn = data[connection]
            info = conn.get("pageInfo") or {}
            after = info.get("endCursor") if info.get("hasNextPage") else None
            yield conn.get("nodes") or [], after
            if after is None:
                return


# ── mapping ─────────────────────────────────────────────────────────────


def issue_item(node: dict) -> dict:
    """One registry ``work`` import item for a Linear issue."""
    links: list[dict] = []
    if node.get("parent"):
        # Stored parent -> child; the child owns the link so re-parenting replaces it.
        links.append(
            {"type": "parent_of", "target_source_id": node["parent"]["id"], "reverse": True}
        )
    for rel in ((node.get("relations") or {}).get("nodes")) or []:
        link_type = RELATION_LINKS.get(rel.get("type") or "")
        related = rel.get("relatedIssue") or {}
        if link_type and related.get("id"):
            links.append({"type": link_type, "target_source_id": related["id"]})
    assignee = node.get("assignee") or {}
    trashed = bool(node.get("trashed"))
    delivery = (
        "archived"
        if node.get("archivedAt")
        else STATE_DELIVERY.get((node.get("state") or {}).get("type") or "", "planned")
    )
    return {
        "source_id": node["id"],
        "source_version": version(node.get("updatedAt")),
        "kind": "work",
        "title": node.get("title") or "",
        "body": node.get("description") or "",
        "ticket": node.get("identifier") or "",
        "source_url": node.get("url") or "",
        "owner": assignee.get("email") or assignee.get("displayName") or "",
        "delivery_state": delivery,
        # A trashed ticket retires its work; restoring it from the trash restores the work.
        "status": "retired" if trashed else "approved",
        "status_reason": "source_deleted" if trashed else "",
        "links": links,
    }


def comment_item(node: dict) -> dict:
    user = node.get("user") or {}
    return {
        "source_id": node["id"],
        "parent_source_id": (node.get("issue") or {}).get("id") or "",
        "source_version": version(node.get("updatedAt")),
        "author": user.get("email") or user.get("displayName") or "",
        "body": node.get("body") or "",
        "source_url": node.get("url") or "",
        "source_created_at": to_epoch(node.get("createdAt")),
        "archived": bool(node.get("archivedAt")),
    }


def _team_filter(teams: list[str] | None) -> dict:
    """An ``IssueFilter`` for ``teams``. ``None`` is every team: no filter."""
    if not teams:
        return {}
    if len(teams) == 1:
        return {"team": {"key": {"eq": teams[0]}}}
    return {"or": [{"team": {"key": {"eq": t}}} for t in teams]}


def _and(*filters: dict) -> dict:
    kept = [f for f in filters if f]
    if len(kept) == 1:
        return kept[0]
    return {"and": kept} if kept else {}


def _on_issue(teams: list[str] | None) -> dict:
    """A comment or document filter: attached to an issue of ``teams``."""
    return {"issue": _team_filter(teams)} if teams else {}


def _comment_team(node: dict) -> str:
    return (((node.get("issue") or {}).get("team")) or {}).get("key") or ""


# ── inventory ───────────────────────────────────────────────────────────


def inventory(client: LinearClient, teams: list[str] | None = None) -> dict:
    """Count what Linear holds for ``teams`` (``None``: all) and name what the importer skips."""
    team_rows = [
        n
        for nodes, _ in client.pages(TEAMS_QUERY, "teams", {})
        for n in nodes
        if not teams or n.get("key") in teams
    ]
    issues: Counter[str] = Counter()
    states: Counter[str] = Counter()
    relation_types: Counter[str] = Counter()
    projects: set[str] = set()
    truncated: list[str] = []
    for nodes, _ in client.pages(ISSUES_QUERY, "issues", _team_filter(teams)):
        for node in nodes:
            issues["total"] += 1
            issues[f"team:{(node.get('team') or {}).get('key', '')}"] += 1
            issues["archived"] += bool(node.get("archivedAt"))
            issues["trashed"] += bool(node.get("trashed"))
            states[(node.get("state") or {}).get("type") or "unknown"] += 1
            if node.get("project"):
                projects.add(node["project"]["id"])
            rels = node.get("relations") or {}
            for rel in rels.get("nodes") or []:
                relation_types[rel.get("type") or "unknown"] += 1
            if (rels.get("pageInfo") or {}).get("hasNextPage"):
                truncated.append(node.get("identifier") or node["id"])
    comments: Counter[str] = Counter()
    for nodes, _ in client.pages(COMMENTS_QUERY, "comments", _on_issue(teams)):
        for node in nodes:
            if not node.get("issue"):
                comments["not_on_issue"] += 1
            elif not teams or _comment_team(node) in teams:
                comments["on_issues"] += 1
                comments["archived"] += bool(node.get("archivedAt"))
    documents: Counter[str] = Counter()
    linked: list[str] = []
    for nodes, _ in client.pages(DOCUMENTS_QUERY, "documents", _on_issue(teams)):
        for node in nodes:
            documents["total"] += 1
            documents["archived"] += bool(node.get("archivedAt"))
            issue = node.get("issue") or {}
            for parent in ("issue", "project", "initiative"):
                documents[f"on_{parent}"] += bool(node.get(parent))
            if issue:
                linked.append(f"{issue.get('identifier') or issue.get('id')}: {node.get('title')}")
    unmapped = {t: n for t, n in relation_types.items() if t not in RELATION_LINKS}
    exceptions = []
    if projects:
        exceptions.append(
            f"projects: {len(projects)} referenced; not imported yet, so issues carry no "
            "project link"
        )
    for rel_type, count in sorted(unmapped.items()):
        exceptions.append(f"relations: {count} of type {rel_type!r}, not imported")
    if truncated:
        exceptions.append(
            f"relations: {len(truncated)} issues hold more than {RELATIONS_PAGE}; only the "
            f"first {RELATIONS_PAGE} import ({', '.join(truncated[:20])})"
        )
    if documents["total"]:
        exceptions.append(
            f"documents: {documents['total']} ({documents['on_issue']} linked to an issue); "
            "counted here, not imported yet"
        )
    if comments["not_on_issue"]:
        exceptions.append(
            f"comments: {comments['not_on_issue']} on documents, projects or updates; not imported"
        )
    if teams:
        exceptions.append(
            "team filter: documents and comments not attached to an issue of the named teams "
            "are not counted; run without --team to count the whole workspace"
        )
    return {
        "teams": [
            {"key": t.get("key"), "name": t.get("name"), "archived": bool(t.get("archivedAt"))}
            for t in team_rows
        ],
        "issues": dict(issues),
        "state_types": dict(states),
        "relation_types": dict(relation_types),
        "comments": dict(comments),
        "documents": dict(documents),
        "documents_linked_to_issues": linked,
        "projects_referenced": len(projects),
        "exceptions": exceptions,
    }


# ── import walks ────────────────────────────────────────────────────────


class LinearImporter:
    """Resumable incremental import of issues, then comments.

    A walk reads everything updated since the last completed walk began, less
    ``WATERMARK_OVERLAP``. After each page is imported its end cursor is saved;
    a restart resumes from that cursor with the same lower bound. If Linear
    rejects a saved cursor, the walk restarts from its lower bound, which only
    re-reads pages that are already imported. Any other failure, including
    retries running out, leaves the saved cursor for the next run.

    Linear does not resend an issue when a link target arrives, so before an
    issue walk completes it re-reads every issue still ``pending_links``.
    """

    def __init__(
        self,
        registry: Registry,
        client: LinearClient,
        teams: list[str] | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.registry = registry
        self.client = client
        self.teams = teams
        self.clock = clock

    def run(self, *, apply: bool = False) -> dict:
        return {
            "apply": apply,
            "issues": self._walk("issues", apply),
            "comments": self._walk("comments", apply),
            "sync_status": self.registry.sync_status(SOURCE_SYSTEM),
        }

    # state rows

    def state(self, stream: str) -> dict:
        rows = self.registry.db.execute(
            "SELECT watermark, walk_since, walk_started_at, after_cursor "
            "FROM registry_sync_state WHERE source_system = ? AND stream = ?",
            (SOURCE_SYSTEM, stream),
        )
        keys = ("watermark", "walk_since", "walk_started_at", "after_cursor")
        if not rows:
            return dict.fromkeys(keys[:3], 0.0) | {"after_cursor": ""}
        return dict(zip(keys, rows[0], strict=True))

    def _save(self, stream: str, st: dict) -> None:
        self.registry.db.execute(
            "INSERT INTO registry_sync_state (source_system, stream, watermark, walk_since, "
            "walk_started_at, after_cursor, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (source_system, stream) DO UPDATE SET watermark = excluded.watermark, "
            "walk_since = excluded.walk_since, walk_started_at = excluded.walk_started_at, "
            "after_cursor = excluded.after_cursor, updated_at = excluded.updated_at",
            (
                SOURCE_SYSTEM,
                stream,
                st["watermark"],
                st["walk_since"],
                st["walk_started_at"],
                st["after_cursor"],
                self.clock(),
            ),
        )

    # walking

    def _walk(self, stream: str, apply: bool) -> dict:
        st = self.state(stream)
        if not st["after_cursor"]:
            st["walk_since"] = max(st["watermark"] - WATERMARK_OVERLAP, 0.0)
            st["walk_started_at"] = self.clock()
        since = {"updatedAt": {"gte": to_iso(st["walk_since"])}}
        scope = _team_filter(self.teams) if stream == "issues" else _on_issue(self.teams)
        filt = _and(scope, since)
        document = ISSUES_QUERY if stream == "issues" else COMMENTS_QUERY
        totals: Counter[str] = Counter()
        errors: list[dict] = []
        after = st["after_cursor"] or None
        try:
            self._pages(stream, document, filt, after, st, apply, totals, errors)
        except LinearCursorRejected as exc:
            logger.warning("Linear %s cursor rejected (%s); restarting walk", stream, exc)
            totals["walk_restarted"] += 1
            self._pages(stream, document, filt, None, st, apply, totals, errors)
        retried: dict = {}
        if apply and stream == "issues":
            retried = self._retry_pending(errors)
        if apply:
            st.update(watermark=st["walk_started_at"], after_cursor="")
            self._save(stream, st)
        return {"totals": dict(totals), "errors": errors, "retried": retried}

    def _retry_pending(self, errors: list[dict]) -> dict:
        """Re-read the issues still waiting on a link target and import them again.

        An issue read before its parent or blocker is written without that link.
        Linear does not resend it when the target arrives, so the walk re-reads it.
        """
        counts: Counter[str] = Counter()
        pending = self.registry.pending_link_sources(SOURCE_SYSTEM)
        for start in range(0, len(pending), PENDING_BATCH):
            ids = pending[start : start + PENDING_BATCH]
            filt = _and(_team_filter(self.teams), {"id": {"in": ids}})
            for nodes, _ in self.client.pages(ISSUES_QUERY, "issues", filt):
                items = [issue_item(n) for n in nodes]
                results = self.registry.import_items(SOURCE_SYSTEM, items, IMPORT_ACTOR)
                counts["read"] += len(results)
                for res in results:
                    if res["outcome"] == "error" or res.get("link_errors"):
                        errors.append(res)
                    synced = res.get("sync_status") == "synced"
                    counts["resolved" if synced else "still_pending"] += 1
        return dict(counts)

    def _pages(
        self,
        stream: str,
        document: str,
        filt: dict,
        after: str | None,
        st: dict,
        apply: bool,
        totals: Counter[str],
        errors: list[dict],
    ) -> None:
        for nodes, end_cursor in self.client.pages(document, stream, filt, after):
            if stream == "issues":
                items = [issue_item(n) for n in nodes]
                totals["read"] += len(items)
                results = (
                    self.registry.import_items(SOURCE_SYSTEM, items, IMPORT_ACTOR) if apply else []
                )
            else:
                kept = [
                    n
                    for n in nodes
                    if n.get("issue") and (not self.teams or _comment_team(n) in self.teams)
                ]
                totals["read"] += len(kept)
                totals["skipped_other_team_or_not_on_issue"] += len(nodes) - len(kept)
                items = [comment_item(n) for n in kept]
                results = self.registry.import_comments(SOURCE_SYSTEM, items) if apply else []
            for res in results:
                totals[res["outcome"]] += 1
                if res["outcome"] == "error" or res.get("link_errors"):
                    errors.append(res)
                if res.get("sync_status") == "pending_links":
                    totals["pending_links"] += 1
            if apply and end_cursor:
                st["after_cursor"] = end_cursor
                self._save(stream, st)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mira.registry.linear")
    parser.add_argument("command", choices=("inventory", "import"))
    parser.add_argument(
        "--team", action="append", help="limit to this Linear team key (default: every team)"
    )
    parser.add_argument("--apply", action="store_true", help="write to the registry")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    client = LinearClient.from_env()
    if args.command == "inventory":
        out = inventory(client, args.team)
    else:
        from mira.registry import get_registry

        registry = get_registry()
        assert registry is not None
        out = LinearImporter(registry, client, args.team).run(apply=args.apply)
    json.dump(out, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
