"""The registry service: objects, relationships, lifecycle, and reads.

Approval status is set by people (or by a clean merge of an agent proposal).
Realization is derived from branch and PR events. Confidence is recomputed on
every write and never accepted from a caller.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections.abc import Callable, Iterable
from typing import Any

from mira.registry.conflicts import MAX_CANDIDATES, Judge
from mira.registry.db import RegistryDB, is_unique_violation
from mira.registry.model import (
    CLAIM_KINDS,
    DELIVERY_STATES,
    EVIDENCE_RESULTS,
    EXPIRY_DAYS,
    KINDS,
    RELATIONSHIP_TYPES,
    WORK_LINKS,
    Actor,
    Forbidden,
    NotFound,
    RegistryError,
    compute_confidence,
)

logger = logging.getLogger(__name__)

COLUMNS = (
    "id",
    "kind",
    "title",
    "summary",
    "body",
    "scope",
    "repo",
    "status",
    "status_reason",
    "realization",
    "confidence",
    "author_kind",
    "author",
    "model",
    "decided_by",
    "decided_at",
    "ticket",
    "session",
    "branch",
    "pr",
    "merge_commit",
    "source_url",
    "basis",
    "rationale",
    "alternatives",
    "conflict_check",
    "goal",
    "work_scope",
    "plan",
    "owner",
    "delivery_state",
    "tested_revision",
    "method",
    "result",
    "artifact_url",
    "source_system",
    "source_id",
    "source_version",
    "sync_status",
    "synced_at",
    "created_at",
    "updated_at",
    "restored_at",
)
_COLS = ", ".join(COLUMNS)
_REL_COLS = (
    "id",
    "source_id",
    "target_id",
    "type",
    "status",
    "relation",
    "new_clause",
    "existing_clause",
    "origin",
    "created_by",
    "created_at",
    "resolved_by",
    "resolved_at",
)

# Fields a caller may set. Everything else is derived or comes from the actor.
_CALLER_FIELDS = {
    "title",
    "summary",
    "body",
    "scope",
    "repo",
    "ticket",
    "session",
    "branch",
    "source_url",
    "rationale",
    "alternatives",
    "basis",
    "goal",
    "work_scope",
    "plan",
    "owner",
    "delivery_state",
}
_INPUT_ONLY = {"kind", "concepts", "anchors"}
_CALLER_BASES = ("statement", "repository_analysis")
# Unresolved conflict checks: a pending, capped, or failed check blocks auto-approval.
_OPEN_CHECKS = ("pending", "partial", "failed")
_REALIZED = ("in_progress", "submitted", "merged", "merged_with_conflict")
# Approval queue: further-realized proposals first, because a merge already happened.
_REALIZATION_RANK = {
    "merged_with_conflict": 0,
    "merged": 1,
    "submitted": 2,
    "in_progress": 3,
    "stated": 4,
    "abandoned": 5,
}
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_DAY = 86400.0

AnchorLookup = Callable[[str, str], "dict | None"]

# Reasons an import may retire an approved object, and so may later undo.
_SOURCE_RETIRE_REASONS = ("source_retired", "source_deleted")
# Work fields the Linear importer writes. While Linear is authoritative an edit
# to them through the registry would be overwritten or drift (HAI-11606).
_MIRRORED_WORK_FIELDS = ("title", "owner", "delivery_state")
# Work links that mirror a Linear relation when both ends came from Linear.
_MIRRORED_WORK_LINKS = ("parent_of", "blocks", "relates")


class _ConcurrentInsert(Exception):
    """Another writer inserted the same source row first; retry the item."""


def new_id(prefix: str = "reg_") -> str:
    return prefix + secrets.token_hex(6)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]+", text.lower()) if len(w) > 3}


_TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
_REVIEW_EXCERPT = 6000


def _anchors_touched(anchors: list[dict], repo: str, hunks: dict[str, dict]) -> bool:
    """True when an anchor's symbol appears in the diff hunk for its file.

    A path-only anchor matches the file. A symbol anchor matches only when the
    qualified name, or its leaf, appears in that file's hunk text.
    """
    for anchor in anchors:
        if str(anchor.get("repo") or "").lower() != repo.lower():
            continue
        hunk = hunks.get(anchor.get("path") or "")
        if hunk is None:
            continue
        name = str(anchor.get("qualified_name") or "")
        if not name:
            return True
        leaf = name.rsplit(".", 1)[-1]
        text = hunk.get("text") or ""
        if name in text or (leaf and leaf in text):
            return True
    return False


def _hit_location(obj: dict, hunks: dict[str, dict], repo: str) -> tuple[str, int]:
    for anchor in obj.get("anchors") or []:
        if str(anchor.get("repo") or "").lower() != repo.lower():
            continue
        path = anchor.get("path") or ""
        hunk = hunks.get(path)
        if hunk is None:
            continue
        name = str(anchor.get("qualified_name") or "")
        leaf = name.rsplit(".", 1)[-1] if name else ""
        text = hunk.get("text") or ""
        if not name or name in text or (leaf and leaf in text):
            return path, int(hunk.get("line") or 1)
    path = next(iter(hunks), "")
    hunk = hunks.get(path) or {}
    return path, int(hunk.get("line") or 1)


def _pr_statement(title: str, body: str, hunks: dict[str, dict]) -> str:
    parts = [p for p in (title.strip(), body.strip()) if p]
    tickets = _TICKET_RE.findall(f"{title}\n{body}")
    if tickets:
        parts.append("Linked tickets: " + ", ".join(dict.fromkeys(tickets)))
    excerpt = "\n\n".join(f"# {path}\n{hunk.get('text') or ''}" for path, hunk in hunks.items())
    if len(excerpt) > _REVIEW_EXCERPT:
        excerpt = excerpt[:_REVIEW_EXCERPT]
    if excerpt:
        parts.append(excerpt)
    return "\n\n".join(parts)


class Registry:
    def __init__(
        self,
        db: RegistryDB,
        *,
        judge: Judge | None = None,
        anchor_lookup: AnchorLookup | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.judge = judge
        self.anchor_lookup = anchor_lookup
        self.clock = clock

    # ── rows ──────────────────────────────────────────────────────────

    def _row(self, row: tuple) -> dict:
        obj = dict(zip(COLUMNS, row, strict=True))
        try:
            obj["alternatives"] = json.loads(obj["alternatives"]) if obj["alternatives"] else []
        except ValueError:
            obj["alternatives"] = [obj["alternatives"]]
        obj["proposal"] = obj["status"] == "proposed"
        return obj

    def _rel(self, row: tuple) -> dict:
        return dict(zip(_REL_COLS, row, strict=True))

    def get(self, object_id: str) -> dict:
        rows = self.db.execute(f"SELECT {_COLS} FROM registry_objects WHERE id = ?", (object_id,))
        if not rows:
            raise NotFound(f"no registry object {object_id}")
        return self._row(rows[0])

    def _select(self, where: str = "1=1", params: Iterable = ()) -> list[dict]:
        rows = self.db.execute(f"SELECT {_COLS} FROM registry_objects WHERE {where}", tuple(params))
        return [self._row(r) for r in rows]

    def _write(self, obj: dict, *, insert: bool = False) -> dict:
        """Persist ``obj`` with a freshly computed confidence."""
        now = self.clock()
        obj["updated_at"] = now
        obj["confidence"] = compute_confidence(
            status=obj["status"],
            author_kind=obj["author_kind"],
            realization=obj["realization"],
            created_at=obj["created_at"],
            restored_at=obj["restored_at"],
            now=now,
        )
        values = [obj[c] for c in COLUMNS]
        values[COLUMNS.index("alternatives")] = (
            json.dumps(obj["alternatives"]) if obj["alternatives"] else ""
        )
        if insert:
            marks = ", ".join("?" for _ in COLUMNS)
            self.db.execute(f"INSERT INTO registry_objects ({_COLS}) VALUES ({marks})", values)
        else:
            sets = ", ".join(f"{c} = ?" for c in COLUMNS[1:])
            self.db.execute(
                f"UPDATE registry_objects SET {sets} WHERE id = ?", [*values[1:], obj["id"]]
            )
        obj["proposal"] = obj["status"] == "proposed"
        return obj

    def _blank(self, kind: str, actor: Actor) -> dict:
        now = self.clock()
        obj: dict[str, Any] = dict.fromkeys(COLUMNS, "")
        obj.update(
            id=new_id(),
            kind=kind,
            scope="repo",
            status="approved" if kind not in (*CLAIM_KINDS,) else "proposed",
            realization="stated",
            confidence=0.0,
            author_kind=actor.kind,
            author=actor.sub,
            model=actor.model,
            decided_by=actor.sub,
            decided_at=now,
            basis="statement",
            alternatives=[],
            conflict_check="none",
            source_version=0,
            synced_at=0.0,
            created_at=now,
            updated_at=now,
            restored_at=0.0,
        )
        if kind == "work":
            obj["delivery_state"] = "planned"
        return obj

    # ── relationships ─────────────────────────────────────────────────

    def relationships(
        self, object_id: str, *, direction: str = "both", type: str | None = None
    ) -> list[dict]:
        clauses, params = [], []
        if direction in ("out", "both"):
            clauses.append("source_id = ?")
            params.append(object_id)
        if direction in ("in", "both"):
            clauses.append("target_id = ?")
            params.append(object_id)
        where = "(" + " OR ".join(clauses) + ")"
        if type:
            where += " AND type = ?"
            params.append(type)
        rows = self.db.execute(
            f"SELECT {', '.join(_REL_COLS)} FROM registry_relationships WHERE {where} "
            "ORDER BY created_at, id",
            params,
        )
        return [self._rel(r) for r in rows]

    def _add_rel(
        self,
        source_id: str,
        target_id: str,
        type: str,
        actor: Actor,
        *,
        status: str = "active",
        origin: str = "",
        relation: str = "",
        new_clause: str = "",
        existing_clause: str = "",
    ) -> None:
        if type not in RELATIONSHIP_TYPES:
            raise RegistryError(f"unknown relationship type {type!r}")
        if source_id == target_id:
            raise RegistryError("an object cannot link to itself")
        self.db.execute(
            f"INSERT INTO registry_relationships ({', '.join(_REL_COLS)}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0) "
            "ON CONFLICT (source_id, target_id, type) DO UPDATE SET status = excluded.status, "
            "relation = excluded.relation, new_clause = excluded.new_clause, "
            "existing_clause = excluded.existing_clause",
            (
                new_id("rel_"),
                source_id,
                target_id,
                type,
                status,
                relation,
                new_clause,
                existing_clause,
                origin,
                actor.sub,
                self.clock(),
            ),
        )

    def _set_rel_status(self, source_id: str, target_id: str, type: str, status: str, by: str):
        self.db.execute(
            "UPDATE registry_relationships SET status = ?, resolved_by = ?, resolved_at = ? "
            "WHERE source_id = ? AND target_id = ? AND type = ?",
            (status, by, self.clock(), source_id, target_id, type),
        )

    def _linked(self, object_id: str, type: str, direction: str = "out") -> list[dict]:
        rels = self.relationships(object_id, direction=direction, type=type)
        key = "target_id" if direction == "out" else "source_id"
        out = []
        for rel in rels:
            try:
                out.append(self.get(rel[key]))
            except NotFound:
                continue
        return out

    # ── concepts and anchors ──────────────────────────────────────────

    def concept(self, name: str, actor: Actor) -> dict:
        name = name.strip()
        if not name:
            raise RegistryError("concept name is empty")
        found = self._select("kind = 'concept' AND LOWER(title) = ?", (name.lower(),))
        if found:
            return found[0]
        obj = self._blank("concept", actor)
        obj.update(title=name, scope="org")
        return self._write(obj, insert=True)

    def _attach_concepts(self, object_id: str, names: Iterable[str], actor: Actor) -> None:
        for name in names:
            if name and name.strip():
                self._add_rel(object_id, self.concept(name, actor)["id"], "belongs_to", actor)

    def link_anchor(self, object_id: str, anchor: dict, actor: Actor) -> dict:
        """Tie an object to a symbol. ``anchor_id`` is the HAI-11604 symbol anchor."""
        obj = self.get(object_id)
        repo = str(anchor.get("repo") or obj["repo"] or "").strip()
        path = str(anchor.get("path") or "").strip()
        qname = str(anchor.get("qualified_name") or "").strip()
        anchor_id = str(anchor.get("anchor_id") or "").strip()
        kind = str(anchor.get("symbol_kind") or "").strip()
        if anchor_id and self.anchor_lookup:
            found = self.anchor_lookup(repo, anchor_id)
            if found:
                path = path or found.get("path", "")
                qname = qname or found.get("qualified_name", "")
                kind = kind or found.get("symbol_kind", "")
        if not _REPO_RE.match(repo):
            raise RegistryError("anchor repo must be owner/name")
        if not path:
            raise RegistryError("anchor needs a path or a resolvable anchor_id")
        self.db.execute(
            "INSERT INTO registry_anchors "
            "(object_id, repo, path, qualified_name, symbol_kind, anchor_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (object_id, repo, path, qualified_name) DO UPDATE SET "
            "anchor_id = excluded.anchor_id, symbol_kind = excluded.symbol_kind",
            (object_id, repo, path, qname, kind, anchor_id, self.clock()),
        )
        return {
            "object_id": object_id,
            "repo": repo,
            "path": path,
            "qualified_name": qname,
            "symbol_kind": kind,
            "anchor_id": anchor_id,
        }

    def anchors(self, object_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT repo, path, qualified_name, symbol_kind, anchor_id FROM registry_anchors "
            "WHERE object_id = ? ORDER BY repo, path, qualified_name",
            (object_id,),
        )
        return [self._current_anchor(r) for r in rows]

    def _current_anchor(self, row: tuple) -> dict:
        """Snapshot row, with the path the index resolves today after a rename."""
        repo, path, qname, kind, anchor_id = row
        out = {
            "repo": repo,
            "path": path,
            "qualified_name": qname,
            "symbol_kind": kind,
            "anchor_id": anchor_id,
            "resolution_status": "",
        }
        if anchor_id and self.anchor_lookup:
            try:
                found = self.anchor_lookup(repo, anchor_id)
            except Exception:
                found = None
            if found:
                out["path"] = found.get("path") or path
                out["qualified_name"] = found.get("qualified_name") or qname
                out["resolution_status"] = found.get("resolution_status", "")
        return out

    def objects_on_paths(
        self, repo: str, paths: Iterable[str], symbols: Iterable[str] = ()
    ) -> list[str]:
        """Ids of objects anchored to any of ``paths`` or ``symbols`` in ``repo``."""
        wanted_paths, wanted_symbols = set(paths), set(symbols)
        rows = self.db.execute(
            "SELECT repo, path, qualified_name, symbol_kind, anchor_id, object_id "
            "FROM registry_anchors WHERE LOWER(repo) = ?",
            (repo.lower(),),
        )
        ids: list[str] = []
        for row in rows:
            anchor = self._current_anchor(row[:5])
            hit = anchor["path"] in wanted_paths or row[1] in wanted_paths
            hit = hit or (anchor["qualified_name"] and anchor["qualified_name"] in wanted_symbols)
            if hit and row[5] not in ids:
                ids.append(row[5])
        return ids

    def concept_members(self, concept: str) -> list[str]:
        rows = self.db.execute(
            "SELECT r.source_id FROM registry_relationships r JOIN registry_objects c "
            "ON c.id = r.target_id WHERE r.type = 'belongs_to' AND c.kind = 'concept' "
            "AND (c.id = ? OR LOWER(c.title) = ?)",
            (concept, concept.lower()),
        )
        return [r[0] for r in rows]

    # ── writes ────────────────────────────────────────────────────────

    async def put_object(self, fields: dict, actor: Actor) -> dict:
        """Record an object. Claims start ``proposed`` and run the conflict check."""
        kind = str(fields.get("kind") or "decision")
        if kind not in KINDS or kind == "verification_evidence":
            raise RegistryError(
                f"kind must be one of {', '.join(k for k in KINDS if k != 'verification_evidence')}"
            )
        ignored = sorted(k for k in fields if k not in _CALLER_FIELDS | _INPUT_ONLY)
        obj = self._blank(kind, actor)
        for key in _CALLER_FIELDS:
            if fields.get(key) not in (None, ""):
                obj[key] = fields[key]
        if isinstance(obj["alternatives"], str):
            obj["alternatives"] = [obj["alternatives"]]
        self._validate(obj)
        if kind in CLAIM_KINDS and not (obj["session"] and obj["branch"]):
            raise RegistryError(f"a {kind} needs both session and branch")
        if obj["basis"] not in _CALLER_BASES:
            raise RegistryError(f"basis must be one of {', '.join(_CALLER_BASES)}")
        if kind in CLAIM_KINDS:
            obj["conflict_check"] = "pending"
        self._write(obj, insert=True)
        self._attach_concepts(obj["id"], fields.get("concepts") or [], actor)
        for anchor in fields.get("anchors") or []:
            self.link_anchor(obj["id"], anchor, actor)

        judgments: list[dict] = []
        if kind in CLAIM_KINDS:
            judgments = await self.check_conflicts(obj["id"], actor)
        obj = self.get(obj["id"])
        return {
            "object": obj,
            "conflict_check": obj["conflict_check"],
            "conflicts": [j for j in judgments if j["relation"] != "none"],
            "judgments": judgments,
            "ignored_fields": ignored,
        }

    def _validate(self, obj: dict) -> None:
        if not str(obj["title"]).strip():
            raise RegistryError("title is required")
        if obj["scope"] not in ("org", "repo"):
            raise RegistryError("scope must be org or repo")
        if obj["repo"] and not _REPO_RE.match(obj["repo"]):
            raise RegistryError("repo must be owner/name")
        if obj["scope"] == "repo" and not obj["repo"] and obj["kind"] != "concept":
            raise RegistryError("a repo-scoped object needs repo (owner/name)")
        if obj["branch"] and not obj["repo"]:
            raise RegistryError("branch needs repo so PR events can be matched")
        if obj["kind"] == "work" and obj["delivery_state"] not in DELIVERY_STATES:
            raise RegistryError(f"delivery_state must be one of {', '.join(DELIVERY_STATES)}")

    async def put_work(self, fields: dict, actor: Actor) -> dict:
        return (await self.put_object({**fields, "kind": "work"}, actor))["object"]

    def update_work(self, work_id: str, fields: dict, actor: Actor) -> dict:
        obj = self.get(work_id)
        if obj["kind"] != "work":
            raise RegistryError(f"{work_id} is not a work object")
        if obj["source_system"] == "linear":
            changed = [
                k
                for k in _MIRRORED_WORK_FIELDS
                if fields.get(k) is not None and fields[k] != obj[k]
            ]
            if changed:
                raise RegistryError(
                    f"{work_id} mirrors Linear ticket {obj['ticket']}; Linear is authoritative "
                    f"for {', '.join(changed)}, so change them in Linear"
                )
        for key in ("title", "summary", "goal", "work_scope", "plan", "owner", "delivery_state"):
            if fields.get(key) is not None:
                obj[key] = fields[key]
        self._validate(obj)
        return self._write(obj)

    def link_work(self, work_id: str, target_id: str, type: str, actor: Actor) -> dict:
        """Link work to a requirement, decision, child work, or dependency."""
        if type not in WORK_LINKS:
            raise RegistryError(f"work link type must be one of {', '.join(WORK_LINKS)}")
        source_kind, target_kinds = WORK_LINKS[type]
        work, target = self.get(work_id), self.get(target_id)
        if work["kind"] != source_kind:
            raise RegistryError(f"{work_id} is a {work['kind']}, not work")
        if target["kind"] not in target_kinds:
            raise RegistryError(f"{type} links work to {' or '.join(target_kinds)}")
        if (
            type in _MIRRORED_WORK_LINKS
            and work["source_system"] == "linear"
            and target["source_system"] == "linear"
        ):
            raise RegistryError(
                f"{work['ticket']} and {target['ticket']} are Linear tickets; Linear is "
                f"authoritative for {type} between them, so link them in Linear"
            )
        self._check_work_link(work_id, target_id, type)
        self._add_rel(work_id, target_id, type, actor)
        return {"source_id": work_id, "target_id": target_id, "type": type}

    def _check_work_link(self, source_id: str, target_id: str, type: str) -> None:
        if type == "parent_of":
            parents = [
                r
                for r in self.relationships(target_id, direction="in", type="parent_of")
                if r["source_id"] != source_id
            ]
            if parents:
                raise RegistryError(f"{target_id} already has parent {parents[0]['source_id']}")
            if self._reaches(target_id, source_id, "parent_of"):
                raise RegistryError("that parent link would make a cycle")
        if type == "blocks" and self._reaches(target_id, source_id, "blocks"):
            raise RegistryError("that dependency would make a blocks cycle")

    def _reaches(self, start: str, goal: str, type: str) -> bool:
        seen, stack = set(), [start]
        while stack:
            node = stack.pop()
            if node == goal:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(
                r["target_id"] for r in self.relationships(node, direction="out", type=type)
            )
        return False

    def put_evidence(self, fields: dict, actor: Actor) -> dict:
        """Record a verification result for one or more requirements at a revision."""
        requirement_ids = list(fields.get("requirement_ids") or [])
        if not requirement_ids:
            raise RegistryError("evidence must name at least one requirement")
        for rid in requirement_ids:
            if self.get(rid)["kind"] != "requirement":
                raise RegistryError(f"{rid} is not a requirement")
        result = str(fields.get("result") or "")
        if result not in EVIDENCE_RESULTS:
            raise RegistryError(f"result must be one of {', '.join(EVIDENCE_RESULTS)}")
        for key in ("tested_revision", "method"):
            if not str(fields.get(key) or "").strip():
                raise RegistryError(f"evidence needs {key}")
        obj = self._blank("verification_evidence", actor)
        obj.update(
            title=fields.get("title") or f"{fields['method']} at {fields['tested_revision'][:12]}",
            summary=fields.get("summary") or "",
            repo=fields.get("repo") or "",
            scope="repo" if fields.get("repo") else "org",
            tested_revision=fields["tested_revision"],
            method=fields["method"],
            result=result,
            artifact_url=fields.get("artifact_url") or "",
            source_url=fields.get("artifact_url") or "",
            session=fields.get("session") or "",
            ticket=fields.get("ticket") or "",
        )
        self._validate(obj)
        self._write(obj, insert=True)
        for rid in requirement_ids:
            self._add_rel(obj["id"], rid, "verifies", actor)
        return obj

    # ── conflict check ────────────────────────────────────────────────

    def conflict_candidates(self, obj: dict) -> tuple[list[dict], int]:
        """Approved claims to judge against ``obj`` and how many were left out.

        Shared-anchor and shared-concept candidates come first and count toward
        the cap. Org-scope candidates only fill the remaining slots, ranked by
        word overlap, so a large org rulebook does not cap every check.
        """
        approved = (
            "status = 'approved' AND kind IN ('decision', 'rule', 'constraint', 'requirement')"
        )
        own = self.anchors(obj["id"])
        anchor_ids: list[str] = []
        for a in own:
            anchor_ids += self.objects_on_paths(
                a["repo"], [a["path"]], [a["qualified_name"]] if a["qualified_name"] else []
            )
        concept_ids: list[str] = []
        for c in self._linked(obj["id"], "belongs_to"):
            concept_ids += self.concept_members(c["id"])

        ranked: list[dict] = []
        seen = {obj["id"]}
        for group in (anchor_ids, concept_ids):
            batch = []
            for oid in group:
                if oid in seen:
                    continue
                seen.add(oid)
                cand = self._select(f"id = ? AND {approved}", (oid,))
                batch += cand
            ranked += sorted(batch, key=lambda c: -c["confidence"])
        remainder = max(0, len(ranked) - MAX_CANDIDATES)
        ranked = ranked[:MAX_CANDIDATES]
        if len(ranked) < MAX_CANDIDATES:
            mine = _words(f"{obj['title']} {obj['summary']} {obj['body']}")
            org = [c for c in self._select(f"scope = 'org' AND {approved}") if c["id"] not in seen]
            org.sort(
                key=lambda c: (
                    -len(mine & _words(f"{c['title']} {c['summary']} {c['body']}")),
                    -c["confidence"],
                )
            )
            org = [c for c in org if mine & _words(f"{c['title']} {c['summary']} {c['body']}")]
            ranked += org[: MAX_CANDIDATES - len(ranked)]
        return ranked, remainder

    async def check_conflicts(self, object_id: str, actor: Actor) -> list[dict]:
        obj = self.get(object_id)
        candidates, remainder = self.conflict_candidates(obj)
        if not candidates:
            obj["conflict_check"] = "partial" if remainder else "done"
            self._write(obj)
            return []
        if self.judge is None:
            obj["conflict_check"] = "failed"
            self._write(obj)
            return []

        async def _one(cand: dict) -> dict | None:
            self._record_prompt(str(obj.get("session") or ""), object_id, cand["id"])
            try:
                verdict = await self.judge(obj, cand)  # type: ignore[misc]
            except Exception as exc:
                logger.warning("Registry conflict judge failed on %s: %s", cand["id"], exc)
                return None
            return {"existing_id": cand["id"], "existing_title": cand["title"], **verdict}

        results = await asyncio.gather(*(_one(c) for c in candidates))
        judgments = [r for r in results if r is not None]
        for j in judgments:
            if j["relation"] != "none":
                self._add_rel(
                    object_id,
                    j["existing_id"],
                    "conflicts_with",
                    actor,
                    status="proposed",
                    relation=j["relation"],
                    new_clause=j["new_clause"],
                    existing_clause=j["existing_clause"],
                )
        failed = len(judgments) < len(candidates)
        obj = self.get(object_id)
        obj["conflict_check"] = "failed" if failed else ("partial" if remainder else "done")
        self._write(obj)
        return judgments

    def unresolved(self, obj: dict) -> list[str]:
        """Why this proposal may not auto-approve. Empty means it may."""
        reasons = []
        if obj["conflict_check"] in _OPEN_CHECKS:
            reasons.append(f"conflict check {obj['conflict_check']}")
        for rel in self.relationships(obj["id"], direction="out"):
            if rel["status"] != "proposed" or rel["type"] not in ("conflicts_with", "supersedes"):
                continue
            try:
                target = self.get(rel["target_id"])
            except NotFound:
                continue
            if target["status"] == "approved":
                reasons.append(f"{rel['type']} {target['id']}")
        return reasons

    def _record_prompt(self, session: str, object_id: str, candidate_id: str) -> None:
        self.db.execute(
            "INSERT INTO registry_conflict_prompts "
            "(id, session, object_id, candidate_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (new_id("prm_"), session, object_id, candidate_id, self.clock()),
        )

    def _record_answer(self, object_id: str, session: str, choice: str, actor: Actor) -> None:
        self.db.execute(
            "INSERT INTO registry_conflict_answers "
            "(object_id, session, choice, answered_by, answered_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (object_id, session) DO UPDATE SET choice = excluded.choice, "
            "answered_by = excluded.answered_by, answered_at = excluded.answered_at",
            (object_id, session, choice, actor.sub, self.clock()),
        )

    def _answer(self, object_id: str, session: str) -> dict | None:
        rows = self.db.execute(
            "SELECT choice, answered_by, answered_at FROM registry_conflict_answers "
            "WHERE object_id = ? AND session = ?",
            (object_id, session),
        )
        if not rows:
            return None
        return {"choice": rows[0][0], "answered_by": rows[0][1], "answered_at": rows[0][2]}

    def conflict_views(self, obj: dict) -> list[dict]:
        """Each ``conflicts_with`` link, the operator's answer, and the session that asked."""
        answer = self._answer(obj["id"], obj.get("session") or "")
        views = []
        for rel in self.relationships(obj["id"], direction="out", type="conflicts_with"):
            views.append(
                {
                    **rel,
                    "session": obj.get("session") or "",
                    "answer": None if answer is None else answer["choice"],
                    "answered_by": "" if answer is None else answer["answered_by"],
                    "answered_at": 0 if answer is None else answer["answered_at"],
                }
            )
        return views

    def session_conflicts(self, session: str) -> dict:
        """Whether ``session`` still blocks push on an open conflict.

        Keep and supersede clear this session. Defer leaves the conflict in
        the list with ``answer`` ``defer`` and ``unresolved`` true, so push
        and PR creation stay blocked while the session continues editing.
        Another session's answer does not clear this one.
        """
        session = str(session or "").strip()
        if not session:
            raise RegistryError("session is required")
        conflicts = []
        rows = self._select(
            "session = ? AND status = 'proposed' "
            "AND kind IN ('decision', 'rule', 'constraint', 'requirement')",
            (session,),
        )
        for obj in rows:
            answer = self._answer(obj["id"], session)
            if answer is not None and answer["choice"] in ("keep", "supersede"):
                continue
            for rel in self.relationships(obj["id"], direction="out", type="conflicts_with"):
                if rel["status"] != "proposed":
                    continue
                try:
                    if self.get(rel["target_id"])["status"] != "approved":
                        continue
                except NotFound:
                    continue
                conflicts.append(
                    {
                        "object_id": obj["id"],
                        "existing_id": rel["target_id"],
                        "session": session,
                        "relation": rel["relation"],
                        "new_clause": rel["new_clause"],
                        "existing_clause": rel["existing_clause"],
                        "answer": None if answer is None else answer["choice"],
                    }
                )
        return {"session": session, "unresolved": bool(conflicts), "conflicts": conflicts}

    def conflict_prompt_counts(self) -> dict:
        """Judge prompts per session, and the keep / supersede / defer split."""
        prompt_rows = self.db.execute(
            "SELECT session, COUNT(*) FROM registry_conflict_prompts GROUP BY session"
        )
        answer_rows = self.db.execute(
            "SELECT choice, COUNT(*) FROM registry_conflict_answers GROUP BY choice"
        )
        by_session = [{"session": r[0], "prompts": r[1]} for r in prompt_rows]
        answers = {"keep": 0, "supersede": 0, "defer": 0}
        for choice, count in answer_rows:
            if choice in answers:
                answers[choice] = count
        return {
            "prompts": sum(row["prompts"] for row in by_session),
            "answers": answers,
            "by_session": sorted(by_session, key=lambda row: row["session"]),
        }

    def _open_proposal(self, approved_id: str, repo: str, branch: str) -> str | None:
        """A proposed object that already conflicts with ``approved_id`` on this branch."""
        for rel in self.relationships(approved_id, direction="in", type="conflicts_with"):
            if rel["status"] != "proposed":
                continue
            try:
                src = self.get(rel["source_id"])
            except NotFound:
                continue
            if src["status"] != "proposed":
                continue
            if repo and src["repo"].lower() != repo.lower():
                continue
            if branch and src["branch"] and src["branch"] != branch:
                continue
            return src["id"]
        return None

    async def review_findings(
        self,
        *,
        repo: str,
        hunks: dict[str, dict],
        pr_title: str = "",
        pr_body: str = "",
        branch: str = "",
        pr_number: int = 0,
    ) -> list[dict]:
        """Judge a PR's text against approved decisions on the symbols it touches.

        Returns one hit per conflicting approved object. This does not write a
        registry object: the review comment is the whole output of this path.
        """
        if self.judge is None or not hunks:
            return []
        candidates = self._review_candidates(repo, hunks)
        if not candidates:
            return []
        session = f"review:{repo}#{pr_number}" if pr_number else f"review:{repo}"
        statement = _pr_statement(pr_title, pr_body, hunks)
        synthetic = {
            "id": "",
            "kind": "rule",
            "title": pr_title or "Pull request",
            "summary": "",
            "body": statement,
        }

        async def _one(cand: dict) -> dict | None:
            self._record_prompt(session, "", cand["id"])
            try:
                verdict = await self.judge(synthetic, cand)  # type: ignore[misc]
            except Exception as exc:
                logger.warning("Registry review judge failed on %s: %s", cand["id"], exc)
                return None
            if verdict["relation"] == "none":
                return None
            cand["anchors"] = self.anchors(cand["id"])
            path, line = _hit_location(cand, hunks, repo)
            return {
                "relation": verdict["relation"],
                "new_clause": verdict["new_clause"],
                "existing_clause": verdict["existing_clause"],
                "approved_id": cand["id"],
                "proposal_id": self._open_proposal(cand["id"], repo, branch) or "",
                "path": path,
                "line": line,
            }

        results = await asyncio.gather(*(_one(c) for c in candidates))
        return [r for r in results if r is not None]

    def _review_candidates(self, repo: str, hunks: dict[str, dict]) -> list[dict]:
        """Approved claims whose anchored symbol the diff actually touches."""
        approved = (
            "status = 'approved' AND kind IN ('decision', 'rule', 'constraint', 'requirement')"
        )
        ranked = []
        seen: set[str] = set()
        for oid in self.objects_on_paths(repo, list(hunks), []):
            if oid in seen:
                continue
            seen.add(oid)
            rows = self._select(f"id = ? AND {approved}", (oid,))
            if not rows:
                continue
            obj = rows[0]
            if _anchors_touched(self.anchors(obj["id"]), repo, hunks):
                ranked.append(obj)
        ranked.sort(key=lambda c: -c["confidence"])
        return ranked[:MAX_CANDIDATES]

    # ── approval lifecycle ────────────────────────────────────────────

    @staticmethod
    def _require_human(actor: Actor, action: str) -> None:
        if not actor.is_human:
            raise Forbidden(f"only a person can {action}")

    def approve(self, object_id: str, actor: Actor) -> dict:
        self._require_human(actor, "approve")
        obj = self.get(object_id)
        if obj["status"] != "proposed":
            raise RegistryError(f"{object_id} is {obj['status']}, not proposed")
        open_conflicts = [r for r in self.unresolved(obj) if r.startswith("conflicts_with")]
        if open_conflicts:
            raise RegistryError(f"resolve the conflict first: {', '.join(open_conflicts)}")
        obj.update(
            status="approved", status_reason="", decided_by=actor.sub, decided_at=self.clock()
        )
        self._write(obj)
        self._apply_supersedes(object_id, actor)
        return self.get(object_id)

    def reject(self, object_id: str, actor: Actor, reason: str = "") -> dict:
        self._require_human(actor, "reject")
        obj = self.get(object_id)
        if obj["status"] != "proposed":
            raise RegistryError(f"{object_id} is {obj['status']}, not proposed")
        obj.update(
            status="rejected",
            status_reason=reason or "rejected",
            decided_by=actor.sub,
            decided_at=self.clock(),
        )
        return self._write(obj)

    def retire(self, object_id: str, actor: Actor) -> dict:
        self._require_human(actor, "retire")
        obj = self.get(object_id)
        if obj["status"] != "approved":
            raise RegistryError(f"{object_id} is {obj['status']}, not approved")
        obj.update(
            status="retired", status_reason="retired", decided_by=actor.sub, decided_at=self.clock()
        )
        return self._write(obj)

    def restore(self, object_id: str, actor: Actor) -> dict:
        """Bring an expired proposal back. Its 14 days restart now."""
        self._require_human(actor, "restore")
        obj = self.get(object_id)
        if obj["status"] != "rejected" or obj["status_reason"] != "expired":
            raise RegistryError(f"{object_id} did not expire")
        obj.update(status="proposed", status_reason="", restored_at=self.clock())
        return self._write(obj)

    def supersede(self, new_id: str, old_id: str, actor: Actor) -> dict:
        """``new_id`` replaces ``old_id``.

        A proposal is replaced now: retired if it never reached code, else
        superseded. An approved object is superseded only when the new one is
        approved; until then the link is proposed and blocks auto-approval.
        """
        new, old = self.get(new_id), self.get(old_id)
        for o in (new, old):
            if o["kind"] not in CLAIM_KINDS:
                raise RegistryError(f"{o['id']} is a {o['kind']}; only claims supersede")
        if old["status"] not in ("proposed", "approved"):
            raise RegistryError(f"{old_id} is already {old['status']}")
        if new["status"] not in ("proposed", "approved"):
            raise RegistryError(f"{new_id} is {new['status']}")
        if self._reaches(old_id, new_id, "supersedes"):
            raise RegistryError("that supersession would make a cycle")
        applied = old["status"] == "proposed" or new["status"] == "approved"
        self._add_rel(
            new_id, old_id, "supersedes", actor, status="active" if applied else "proposed"
        )
        if applied:
            self._replace(old, actor)
        return {"new": self.get(new_id), "old": self.get(old_id), "applied": applied}

    def _replace(self, old: dict, actor: Actor) -> None:
        if old["status"] == "proposed" and old["realization"] == "stated":
            old.update(status="retired", status_reason="superseded_before_realization")
        else:
            old.update(status="superseded", status_reason="superseded")
        self._write(old)

    def _apply_supersedes(self, new_id: str, actor: Actor) -> None:
        for rel in self.relationships(new_id, direction="out", type="supersedes"):
            if rel["status"] != "proposed":
                continue
            old = self.get(rel["target_id"])
            if old["status"] in ("proposed", "approved"):
                self._replace(old, actor)
            self._set_rel_status(new_id, old["id"], "supersedes", "active", actor.sub)

    def resolve_conflict(self, object_id: str, choice: str, actor: Actor) -> dict:
        """A person answers keep, supersede, or defer for this proposal's session."""
        if not actor.is_human:
            raise Forbidden("resolve_conflict needs a person; an agent token cannot answer")
        obj = self.get(object_id)
        if actor.sub == obj["author"]:
            raise Forbidden("the author of a proposal cannot resolve its conflict")
        if choice not in ("keep", "supersede", "defer"):
            raise RegistryError("choice must be keep, supersede, or defer")
        if obj["status"] != "proposed":
            raise RegistryError(f"{object_id} is {obj['status']}, not proposed")
        conflicts = [
            r
            for r in self.relationships(object_id, direction="out", type="conflicts_with")
            if r["status"] == "proposed"
        ]
        if not conflicts:
            raise RegistryError("no proposed conflict to answer")
        now = self.clock()
        if choice == "defer":
            # The object stays proposed and the conflict stays open for merge.
            # The answer is recorded for this object's session only. Defer does
            # not clear session_conflicts, so push stays blocked.
            self._record_answer(object_id, obj["session"], "defer", actor)
            return self.get(object_id)
        self._record_answer(object_id, obj["session"], choice, actor)
        if choice == "keep":
            obj.update(
                status="rejected",
                status_reason="conflicts_with_approved",
                decided_by=actor.sub,
                decided_at=now,
            )
            self._write(obj)
            for r in conflicts:
                self._set_rel_status(
                    object_id, r["target_id"], "conflicts_with", "kept_existing", actor.sub
                )
            return self.get(object_id)

        obj.update(status="approved", status_reason="", decided_by=actor.sub, decided_at=now)
        if obj["realization"] == "merged_with_conflict":
            obj["realization"] = "merged"
        if obj["conflict_check"] in _OPEN_CHECKS:
            obj["conflict_check"] = "done"
        self._write(obj)
        for r in conflicts:
            old = self.get(r["target_id"])
            self._set_rel_status(
                object_id, old["id"], "conflicts_with", "superseded_existing", actor.sub
            )
            self._add_rel(object_id, old["id"], "supersedes", actor)
            if old["status"] in ("approved", "proposed"):
                old.update(status="superseded", status_reason="superseded")
                self._write(old)
        self._apply_supersedes(object_id, actor)
        return self.get(object_id)

    def expire(self) -> list[str]:
        """Reject agent proposals that reached no PR within 14 days of creation or restore."""
        cutoff = self.clock() - EXPIRY_DAYS * _DAY
        stale = self._select(
            "status = 'proposed' AND author_kind = 'agent' AND source_system = '' "
            "AND realization IN ('stated', 'in_progress') "
            "AND kind IN ('decision', 'rule', 'constraint', 'requirement') "
            "AND (CASE WHEN restored_at > created_at THEN restored_at ELSE created_at END) <= ?",
            (cutoff,),
        )
        for obj in stale:
            obj.update(status="rejected", status_reason="expired", decided_at=self.clock())
            self._write(obj)
        return [o["id"] for o in stale]

    def refresh_confidence(self) -> int:
        """Recompute age-dependent confidence for open proposals."""
        rows = self._select("status = 'proposed'")
        for obj in rows:
            self._write(obj)
        return len(rows)

    # ── realization ───────────────────────────────────────────────────

    def _on_branch(self, repo: str, branch: str) -> list[dict]:
        return self._select(
            "LOWER(repo) = ? AND branch = ? AND status IN ('proposed', 'approved') "
            "AND kind IN ('decision', 'rule', 'constraint', 'requirement')",
            (repo.lower(), branch),
        )

    def on_push(self, repo: str, branch: str) -> list[str]:
        changed = []
        for obj in self._on_branch(repo, branch):
            if obj["realization"] == "stated":
                obj["realization"] = "in_progress"
                self._write(obj)
                changed.append(obj["id"])
        return changed

    def on_pull_request(
        self,
        repo: str,
        branch: str,
        *,
        action: str,
        merged: bool = False,
        pr_url: str = "",
        merge_commit: str = "",
    ) -> list[str]:
        """Apply one PR event to every claim recorded on ``branch``."""
        changed = []
        for obj in self._on_branch(repo, branch):
            if obj["realization"] in ("merged", "merged_with_conflict"):
                continue
            before = (obj["realization"], obj["status"])
            if pr_url:
                obj["pr"] = pr_url
            if action == "closed" and merged:
                obj["merge_commit"] = merge_commit
                self._merge(obj)
            elif action == "closed":
                obj["realization"] = "abandoned"
            elif action in ("opened", "reopened", "ready_for_review", "synchronize"):
                if obj["realization"] in ("stated", "in_progress", "abandoned"):
                    obj["realization"] = "submitted"
            self._write(obj)
            if (obj["realization"], obj["status"]) != before:
                changed.append(obj["id"])
        return changed

    def _merge(self, obj: dict) -> None:
        if obj["status"] != "proposed":
            obj["realization"] = "merged"
            return
        if self.unresolved(obj):
            obj["realization"] = "merged_with_conflict"
            return
        obj["realization"] = "merged"
        if obj["author_kind"] == "agent":
            # The merge is the human act; decided_by stays the agent author.
            obj.update(status="approved", basis="merged_pr", decided_at=self.clock())
            self._write(obj)
            self._apply_supersedes(obj["id"], Actor("agent", obj["author"], obj["model"]))

    # ── reads ─────────────────────────────────────────────────────────

    def detail(self, object_id: str) -> dict:
        obj = self.get(object_id)
        rels = self.relationships(object_id)
        return {
            **obj,
            "anchors": self.anchors(object_id),
            "concepts": [c["title"] for c in self._linked(object_id, "belongs_to")],
            "relationships": rels,
            "conflicts": self.conflict_views(obj),
            "unresolved": self.unresolved(obj) if obj["status"] == "proposed" else [],
        }

    def search(
        self,
        query: str = "",
        *,
        kind: str | None = None,
        concept: str | None = None,
        repo: str | None = None,
        path: str | None = None,
        symbol: str | None = None,
        statuses: Iterable[str] | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Structured search, ranked by confidence. Concept and symbol never match prose."""
        where, params = ["1=1"], []
        if kind:
            where.append("kind = ?")
            params.append(kind)
        else:
            where.append("kind NOT IN ('concept', 'verification_evidence')")
        if statuses:
            st = list(statuses)
            where.append(f"status IN ({', '.join('?' for _ in st)})")
            params += st
        if repo and not (path or symbol):
            where.append("(LOWER(repo) = ? OR scope = 'org')")
            params.append(repo.lower())
        for word in query.lower().split():
            where.append("LOWER(title || ' ' || summary || ' ' || body) LIKE ?")
            params.append(f"%{word}%")
        ids: set[str] | None = None
        if concept:
            ids = set(self.concept_members(concept))
        if path or symbol:
            if not repo:
                raise RegistryError("path or symbol search needs repo")
            hits = set(
                self.objects_on_paths(repo, [path] if path else [], [symbol] if symbol else [])
            )
            ids = hits if ids is None else ids & hits
        if ids is not None:
            if not ids:
                return []
            where.append(f"id IN ({', '.join('?' for _ in ids)})")
            params += sorted(ids)
        rows = self._select(
            " AND ".join(where) + " ORDER BY confidence DESC, created_at DESC", params
        )
        return rows[: max(1, min(int(limit), 100))]

    def chain(self, object_id: str) -> list[dict]:
        """The supersession chain through ``object_id``, oldest first, retired included."""
        self.get(object_id)
        older, newer = [], []
        node = object_id
        seen = {object_id}
        while True:
            rels = [
                r
                for r in self.relationships(node, direction="out", type="supersedes")
                if r["target_id"] not in seen
            ]
            if not rels:
                break
            node = rels[0]["target_id"]
            seen.add(node)
            older.append(node)
        node = object_id
        while True:
            rels = [
                r
                for r in self.relationships(node, direction="in", type="supersedes")
                if r["source_id"] not in seen
            ]
            if not rels:
                break
            node = rels[0]["source_id"]
            seen.add(node)
            newer.append(node)
        nodes = [self.get(i) for i in [*reversed(older), object_id, *newer]]
        for node in nodes:
            node["conflicts"] = self.conflict_views(node)
        return nodes

    def context_for_paths(
        self,
        repo: str,
        paths: Iterable[str] = (),
        *,
        symbols: Iterable[str] = (),
        concepts: Iterable[str] = (),
        include_proposals: bool = True,
    ) -> list[dict]:
        """Claims on these files, symbols, or concepts, plus org-scope rules, by confidence."""
        statuses = ("approved", "proposed") if include_proposals else ("approved",)
        ids = list(self.objects_on_paths(repo, paths, symbols)) if repo else []
        for c in concepts:
            ids += self.concept_members(c)
        found: dict[str, dict] = {}
        for oid in ids:
            try:
                obj = self.get(oid)
            except NotFound:
                continue
            if obj["kind"] in CLAIM_KINDS and obj["status"] in statuses:
                found[oid] = obj
        for obj in self._select("scope = 'org' AND kind = 'rule' AND status = 'approved'"):
            found.setdefault(obj["id"], obj)
        out = sorted(found.values(), key=lambda o: (-o["confidence"], o["created_at"]))
        for obj in out:
            obj["anchors"] = self.anchors(obj["id"])
            obj["unresolved"] = self.unresolved(obj) if obj["status"] == "proposed" else []
        return out

    def work_context(self, work_id: str) -> dict:
        """Plan, requirements, decisions, children, dependencies, then evidence."""
        work = self.get(work_id)
        if work["kind"] != "work":
            raise RegistryError(f"{work_id} is not a work object")
        requirements = self._linked(work_id, "addresses")
        evidence: list[dict] = []
        for req in requirements:
            for ev in self._linked(req["id"], "verifies", direction="in"):
                ev["requirement_id"] = req["id"]
                evidence.append(ev)
        parent = self._linked(work_id, "parent_of", direction="in")
        return {
            "work": work,
            "requirements": requirements,
            "decisions": self._linked(work_id, "records"),
            "children": self._linked(work_id, "parent_of"),
            "dependencies": {
                "parent": parent[0] if parent else None,
                "blocks": self._linked(work_id, "blocks"),
                "blocked_by": self._linked(work_id, "blocks", direction="in"),
                "relates": self._linked(work_id, "relates"),
            },
            "evidence": evidence,
            "comments": self.comments_for(work),
        }

    def work_view(self, work_id: str) -> dict:
        """Implementation (derived from PR events), verification (evidence only), delivery."""
        ctx = self.work_context(work_id)
        records = [*ctx["requirements"], *ctx["decisions"]]
        counts: dict[str, int] = {}
        for rec in records:
            counts[rec["realization"]] = counts.get(rec["realization"], 0) + 1
        verification = []
        for req in ctx["requirements"]:
            evs = sorted(
                (e for e in ctx["evidence"] if e["requirement_id"] == req["id"]),
                key=lambda e: e["created_at"],
            )
            verification.append(
                {
                    "requirement": req,
                    "evidence": evs,
                    "latest_result": evs[-1]["result"] if evs else "unverified",
                    "latest_revision": evs[-1]["tested_revision"] if evs else "",
                }
            )
        return {
            **ctx,
            "delivery_state": ctx["work"]["delivery_state"],
            "implementation": {
                "realization_counts": counts,
                "prs": sorted({r["pr"] for r in records if r["pr"]}),
                "merge_commits": sorted({r["merge_commit"] for r in records if r["merge_commit"]}),
            },
            "verification": verification,
        }

    def queue(self) -> list[dict]:
        """Proposals for review: unresolved conflicts first, then realization, then age."""
        rows = self._select(
            "status = 'proposed' AND kind IN ('decision', 'rule', 'constraint', 'requirement')"
        )
        for obj in rows:
            obj["unresolved"] = self.unresolved(obj)
            obj["conflicts"] = [c for c in self.conflict_views(obj) if c["status"] == "proposed"]
        return sorted(
            rows,
            key=lambda o: (
                0 if o["unresolved"] or o["realization"] == "merged_with_conflict" else 1,
                _REALIZATION_RANK.get(o["realization"], 9),
                o["created_at"],
            ),
        )

    def expired(self) -> list[dict]:
        return self._select(
            "status = 'rejected' AND status_reason = 'expired' ORDER BY decided_at DESC"
        )

    def concepts(self) -> list[dict]:
        out = []
        for c in self._select("kind = 'concept' ORDER BY LOWER(title)"):
            c["member_count"] = len(self.concept_members(c["id"]))
            out.append(c)
        return out

    def anchor_index(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT a.repo, a.path, a.qualified_name, a.symbol_kind, a.anchor_id, a.object_id, "
            "o.title, o.status, o.kind FROM registry_anchors a JOIN registry_objects o "
            "ON o.id = a.object_id ORDER BY a.repo, a.path, a.qualified_name"
        )
        grouped: dict[tuple, dict] = {}
        for row in rows:
            anchor = self._current_anchor(row[:5])
            key = (anchor["repo"], anchor["path"], anchor["qualified_name"])
            entry = grouped.setdefault(key, {**anchor, "objects": []})
            entry["objects"].append(
                {"id": row[5], "title": row[6], "status": row[7], "kind": row[8]}
            )
        return list(grouped.values())

    def works(self) -> list[dict]:
        return self._select("kind = 'work' ORDER BY updated_at DESC")

    # ── import (HAI-11606 seam) ───────────────────────────────────────

    def import_items(
        self,
        source_system: str,
        items: list[dict],
        actor: Actor,
        *,
        status: str | None = None,
        basis: str = "migration",
    ) -> list[dict]:
        """Idempotent upsert keyed by ``(source_system, source_id, source_version)``.

        Same version is a no-op, an older one is ignored, a newer one updates the
        row and replaces its imported links. Links whose target has not arrived
        yet leave ``sync_status = pending_links`` and are retried on the next run.

        An item with links is written as ``pending_links`` and only marked
        ``synced`` once its links are in, so a crash between the two passes is
        retried by the next run instead of being skipped as unchanged.
        """
        if not source_system or ":" in source_system:
            raise RegistryError("source_system is required and may not contain ':'")
        results: list[dict] = []
        touched: list[tuple[dict, dict]] = []
        for item in items:
            try:
                try:
                    with self.db.transaction():
                        outcome, obj = self._upsert_import(
                            source_system, item, actor, status, basis
                        )
                except _ConcurrentInsert:
                    with self.db.transaction():
                        outcome, obj = self._upsert_import(
                            source_system, item, actor, status, basis
                        )
            except RegistryError as exc:
                results.append(
                    {"source_id": item.get("source_id"), "outcome": "error", "error": str(exc)}
                )
                continue
            results.append(
                {"source_id": item["source_id"], "object_id": obj["id"], "outcome": outcome}
            )
            if outcome in ("created", "updated") or obj["sync_status"] in (
                "pending_links",
                "error",
            ):
                touched.append((item, obj))
        for item, obj in touched:
            with self.db.transaction():
                sync, errors = self._import_links(source_system, item, obj, actor)
            for res in results:
                if res.get("object_id") == obj["id"]:
                    res["sync_status"] = sync
                    if errors:
                        res["link_errors"] = errors
        for res in results:
            if "object_id" in res and "sync_status" not in res:
                res["sync_status"] = self.get(res["object_id"])["sync_status"]
        return results

    def by_source(self, source_system: str, source_id: str) -> dict | None:
        rows = self._select("source_system = ? AND source_id = ?", (source_system, source_id))
        return rows[0] if rows else None

    def _upsert_import(
        self, system: str, item: dict, actor: Actor, status: str | None, basis: str
    ) -> tuple[str, dict]:
        source_id = str(item.get("source_id") or "")
        if not source_id:
            raise RegistryError("source_id is required")
        try:
            version = int(item.get("source_version") or 0)
        except (TypeError, ValueError):
            raise RegistryError("source_version must be an integer") from None
        kind = str(item.get("kind") or "work")
        if kind not in KINDS:
            raise RegistryError(f"unknown kind {kind!r}")
        existing = self.by_source(system, source_id)
        if existing is not None:
            if version == existing["source_version"]:
                return "unchanged", existing
            if version < existing["source_version"]:
                return "stale", existing
            obj = existing
            outcome = "updated"
        else:
            obj = self._blank(kind, actor)
            obj.update(source_system=system, source_id=source_id, scope="org", basis=basis)
            if status:
                obj["status"] = status
            outcome = "created"
        if obj["kind"] != kind:
            raise RegistryError(f"{source_id} changed kind from {obj['kind']} to {kind}")
        for key in _CALLER_FIELDS - {"basis"}:
            if key in item and item[key] is not None:
                obj[key] = item[key]
        for key in ("tested_revision", "method", "result", "artifact_url"):
            if key in item and item[key] is not None:
                obj[key] = item[key]
        if item.get("status") == "retired" and obj["status"] == "approved":
            # A source may withdraw what it holds approved; it never approves a proposal.
            reason = item.get("status_reason")
            reason = reason if reason in _SOURCE_RETIRE_REASONS else "source_retired"
            obj.update(status="retired", status_reason=reason)
        elif (
            item.get("status") == "approved"
            and obj["status"] == "retired"
            and obj["status_reason"] in _SOURCE_RETIRE_REASONS
        ):
            # The source restored what it had withdrawn (an untrashed ticket).
            obj.update(status="approved", status_reason="")
        if isinstance(obj["alternatives"], str):
            obj["alternatives"] = [obj["alternatives"]]
        if not obj["repo"]:
            obj["scope"] = "org"
        self._validate(obj)
        sync = "pending_links" if item.get("links") else "synced"
        obj.update(source_version=version, sync_status=sync, synced_at=self.clock())
        try:
            self._write(obj, insert=outcome == "created")
        except Exception as exc:
            if outcome != "created" or not is_unique_violation(exc):
                raise
            # Another importer inserted this source row between our read and
            # our insert. Its row stands. The transaction is rolled back and
            # the caller retries the item, which compares versions against it.
            raise _ConcurrentInsert(source_id) from exc
        if item.get("concepts"):
            self._attach_concepts(obj["id"], item["concepts"], actor)
        return outcome, obj

    def _import_links(
        self, system: str, item: dict, obj: dict, actor: Actor
    ) -> tuple[str, list[str]]:
        """Replace the links this item owns.

        A link with ``"reverse": true`` is stored target -> this object (a child
        naming its parent). Ownership is the per-item ``origin``, so re-parenting
        drops the old reversed link even though its ``source_id`` is the parent.
        """
        origin = f"import:{system}#{obj['source_id']}"
        self.db.execute("DELETE FROM registry_relationships WHERE origin = ?", (origin,))
        # Links written before per-item origins were tagged ``import:{system}``.
        self.db.execute(
            "DELETE FROM registry_relationships WHERE source_id = ? AND origin = ?",
            (obj["id"], f"import:{system}"),
        )
        missing: list[str] = []
        errors: list[str] = []
        for link in item.get("links") or []:
            target_source = str(link.get("target_source_id") or "")
            target = self.by_source(system, target_source)
            if target is None:
                missing.append(target_source)
                continue
            type = str(link.get("type") or "")
            src, dst = (
                (target["id"], obj["id"]) if link.get("reverse") else (obj["id"], target["id"])
            )
            try:
                if type == "conflicts_with":
                    raise RegistryError("imports cannot assert conflicts")
                if type in WORK_LINKS:
                    self._check_work_link(src, dst, type)
                self._add_rel(src, dst, type, actor, origin=origin)
            except RegistryError as exc:
                errors.append(f"{type} -> {target_source}: {exc}")
        obj = self.get(obj["id"])
        obj["sync_status"] = "error" if errors else ("pending_links" if missing else "synced")
        obj["synced_at"] = self.clock()
        self._write(obj)
        return obj["sync_status"], errors

    # ── mirrored comments (HAI-11606) ─────────────────────────────────

    def import_comments(self, source_system: str, comments: list[dict]) -> list[dict]:
        """Upsert comments keyed by ``(source_system, source_id)`` with the same
        version rules as objects. A comment whose parent is not imported yet is
        stored anyway and attaches when the parent arrives.
        """
        results: list[dict] = []
        for c in comments:
            source_id = str(c.get("source_id") or "")
            parent = str(c.get("parent_source_id") or "")
            if not source_id or not parent:
                results.append(
                    {"source_id": source_id, "outcome": "error", "error": "source ids required"}
                )
                continue
            version = int(c.get("source_version") or 0)
            values = (
                str(c.get("author") or ""),
                str(c.get("body") or ""),
                str(c.get("source_url") or ""),
                version,
                float(c.get("source_created_at") or 0),
                1 if c.get("archived") else 0,
                self.clock(),
            )
            with self.db.transaction():
                outcome = self._upsert_comment(source_system, source_id, parent, version, values)
            results.append({"source_id": source_id, "outcome": outcome})
        return results

    def _upsert_comment(
        self, source_system: str, source_id: str, parent: str, version: int, values: tuple
    ) -> str:
        select = (
            "SELECT source_version FROM registry_comments WHERE source_system = ? AND source_id = ?"
        )
        rows = self.db.execute(select, (source_system, source_id))
        if not rows:
            # ON CONFLICT DO NOTHING: when a concurrent importer inserted the row
            # first, nothing is returned and the outcome is read from its row.
            inserted = self.db.execute(
                "INSERT INTO registry_comments (id, source_system, source_id, "
                "parent_source_id, author, body, source_url, source_version, "
                "source_created_at, archived, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (source_system, source_id) DO NOTHING RETURNING id",
                (new_id("cmt_"), source_system, source_id, parent, *values),
            )
            if inserted:
                return "created"
            rows = self.db.execute(select, (source_system, source_id))
        if version == rows[0][0]:
            return "unchanged"
        if version < rows[0][0]:
            return "stale"
        self.db.execute(
            "UPDATE registry_comments SET parent_source_id = ?, author = ?, "
            "body = ?, source_url = ?, source_version = ?, source_created_at = ?, "
            "archived = ?, synced_at = ? WHERE source_system = ? AND source_id = ?",
            (parent, *values, source_system, source_id),
        )
        return "updated"

    def comments_for(self, obj: dict) -> list[dict]:
        """Mirrored comments on ``obj``, oldest first. Archived ones are left out."""
        if not obj.get("source_system"):
            return []
        rows = self.db.execute(
            "SELECT id, source_id, author, body, source_url, source_created_at "
            "FROM registry_comments WHERE source_system = ? AND parent_source_id = ? "
            "AND archived = 0 ORDER BY source_created_at, source_id",
            (obj["source_system"], obj["source_id"]),
        )
        keys = ("id", "source_id", "author", "body", "source_url", "created_at")
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def work_by_ticket(self, ticket: str) -> dict:
        """The work object mirrored for a ticket key such as ``HAI-123``."""
        rows = self._select("kind = 'work' AND ticket = ? AND source_system <> ''", (ticket,))
        if not rows:
            raise NotFound(f"no imported work for ticket {ticket}")
        return rows[0]

    def sync_status(self, source_system: str) -> dict:
        rows = self.db.execute(
            "SELECT sync_status, COUNT(*) FROM registry_objects WHERE source_system = ? "
            "GROUP BY sync_status",
            (source_system,),
        )
        return {r[0]: r[1] for r in rows}

    def pending_link_sources(self, source_system: str) -> list[str]:
        """Source ids of imported work still waiting on a link target."""
        rows = self.db.execute(
            "SELECT source_id FROM registry_objects WHERE source_system = ? AND kind = 'work' "
            "AND sync_status = 'pending_links' ORDER BY source_id",
            (source_system,),
        )
        return [r[0] for r in rows]
