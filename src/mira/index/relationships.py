"""Org-level cross-repo relationship detection.

Resolves external references from indexed repos into explicit edges,
and groups repos using content-aware heuristics: mutual edges, shared
dependencies, summary keyword overlap, and naming conventions.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field

from mira.index.store import ExternalRef, IndexStore

logger = logging.getLogger(__name__)


@dataclass
class RepoEdge:
    """A directed edge between two repos."""

    source_repo: str  # "owner/repo"
    target_repo: str  # "owner/repo"
    kind: str  # e.g. "docker_image", "go_import", etc.
    refs: list[ExternalRef] = field(default_factory=list)


@dataclass
class RepoGroup:
    """A cluster of related repos."""

    name: str
    repos: list[str] = field(default_factory=list)
    confidence: float = 0.0  # 0-1, how confident we are in this grouping
    evidence: list[str] = field(default_factory=list)  # reasons for grouping

    @property
    def reason(self) -> str:
        return "; ".join(self.evidence)


# Patterns for matching external refs to repos
_GITHUB_URL = re.compile(r"github\.com/([^/]+/[^/\s#?]+)")
_GITHUB_SSH = re.compile(r"git@github\.com:([^/]+/[^/\s.]+)")
_GO_IMPORT = re.compile(r"github\.com/([^/]+/[^/\s]+)")
_DOCKER_IMAGE = re.compile(r"(?:ghcr\.io|docker\.io)/([^/]+/[^/:\s]+)")
_TERRAFORM_SOURCE = re.compile(r"github\.com/([^/]+/[^/\s?]+)(?://|$)")

# Words to ignore when comparing summaries for domain overlap
_STOP_WORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "for",
    "of",
    "in",
    "to",
    "with",
    "on",
    "at",
    "by",
    "from",
    "that",
    "this",
    "it",
    "its",
    "as",
    "not",
    "but",
    "if",
    "has",
    "have",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "can",
    "could",
    "should",
    "may",
    "might",
    "file",
    "files",
    "code",
    "function",
    "functions",
    "class",
    "classes",
    "module",
    "modules",
    "package",
    "import",
    "imports",
    "export",
    "exports",
    "main",
    "entry",
    "point",
    "configuration",
    "config",
    "settings",
    "helper",
    "helpers",
    "utility",
    "utilities",
    "utils",
    "common",
    "shared",
    "handler",
    "handlers",
    "implements",
    "implementation",
    "defines",
    "provides",
    "manages",
    "handles",
    "contains",
    "includes",
    "using",
    "used",
}

# Component suffixes that suggest a repo is part of a larger system
_COMPONENT_SUFFIXES = {
    "service",
    "worker",
    "api",
    "web",
    "app",
    "frontend",
    "backend",
    "server",
    "client",
    "admin",
    "gateway",
    "proxy",
    "ingest",
    "consumer",
    "producer",
    "scheduler",
    "monitor",
    "dashboard",
    "cli",
    "sdk",
    "core",
    "lib",
    "common",
    "shared",
    "infra",
}


_OVERRIDES_SCHEMA = """
CREATE TABLE IF NOT EXISTS relationship_overrides (
    source_repo TEXT NOT NULL,
    target_repo TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed',
    created_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (source_repo, target_repo)
);

CREATE TABLE IF NOT EXISTS custom_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_repo TEXT NOT NULL,
    target_repo TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0
);
"""


@dataclass
class RelationshipOverride:
    source_repo: str
    target_repo: str
    status: str  # "confirmed" or "denied"
    created_at: float = 0.0


@dataclass
class CustomEdge:
    id: int
    source_repo: str
    target_repo: str
    reason: str
    created_at: float = 0.0


class RelationshipStore:
    """Manages cross-repo relationships across an org's indexed repos."""

    def __init__(self, index_dir: str | None = None) -> None:
        self._index_dir = index_dir or os.environ.get("MIRA_INDEX_DIR", "/data/indexes")
        self._stores: dict[str, IndexStore] = {}
        # Org-level overrides DB
        os.makedirs(self._index_dir, exist_ok=True)
        self._overrides_db = sqlite3.connect(os.path.join(self._index_dir, "_relationships.db"))
        self._overrides_db.execute("PRAGMA journal_mode=WAL")
        self._overrides_db.executescript(_OVERRIDES_SCHEMA)
        self._overrides_db.commit()
        self._scan_repos()

    def _scan_repos(self) -> None:
        """Discover all indexed repos (Postgres registry or SQLite files)."""
        # First try the Postgres repos table (via app DB)
        db_url = os.environ.get("DATABASE_URL", "")
        if db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
            try:
                from mira.dashboard.api import _app_db

                for repo_record in _app_db.list_repos():
                    if repo_record.status == "ready":
                        full_name = f"{repo_record.owner}/{repo_record.repo}"
                        try:
                            self._stores[full_name] = IndexStore.open(
                                repo_record.owner, repo_record.repo
                            )
                        except Exception as exc:
                            logger.warning("Failed to open index for %s: %s", full_name, exc)
                return
            except Exception as exc:
                logger.warning("Failed to scan repos from DB, falling back to filesystem: %s", exc)

        # Fallback: scan filesystem for SQLite .db files
        if not os.path.isdir(self._index_dir):
            return
        for owner_dir in os.listdir(self._index_dir):
            owner_path = os.path.join(self._index_dir, owner_dir)
            if not os.path.isdir(owner_path):
                continue
            for db_file in os.listdir(owner_path):
                if db_file.endswith(".db") and not db_file.startswith("_"):
                    repo_name = db_file[:-3]
                    full_name = f"{owner_dir}/{repo_name}"
                    db_path = os.path.join(owner_path, db_file)
                    try:
                        self._stores[full_name] = IndexStore(db_path)
                    except Exception as exc:
                        logger.warning("Failed to open index for %s: %s", full_name, exc)

    @property
    def repos(self) -> list[str]:
        """All known indexed repos."""
        return sorted(self._stores.keys())

    def resolve_edges(self) -> list[RepoEdge]:
        """Resolve external_refs into edges between known indexed repos.

        Respects overrides (denied edges are excluded) and includes custom edges.
        """
        known = set(self._stores.keys())
        short_to_full: dict[str, str] = {}
        for full_name in known:
            parts = full_name.split("/")
            if len(parts) == 2:
                short_to_full[parts[1]] = full_name

        # Load denied overrides
        denied = {
            (o.source_repo, o.target_repo) for o in self.list_overrides() if o.status == "denied"
        }

        edges: dict[tuple[str, str, str], RepoEdge] = {}

        for source_repo, store in self._stores.items():
            all_targets = store.get_all_external_targets()
            all_refs_by_target: dict[str, list[ExternalRef]] = {}
            for path in store.all_paths():
                for ref in store._load_external_refs(path):
                    all_refs_by_target.setdefault(ref.target, []).append(ref)

            for target_str in all_targets:
                matched_repo = self._match_target_to_repo(target_str, known, short_to_full)
                if matched_repo and matched_repo != source_repo:
                    # Skip denied edges
                    if (source_repo, matched_repo) in denied:
                        continue
                    key = (source_repo, matched_repo, "external_ref")
                    if key not in edges:
                        edges[key] = RepoEdge(
                            source_repo=source_repo,
                            target_repo=matched_repo,
                            kind="external_ref",
                        )
                    edges[key].refs.extend(all_refs_by_target.get(target_str, []))

        # Add custom edges
        for ce in self.list_custom_edges():
            key = (ce.source_repo, ce.target_repo, "custom")
            if key not in edges and (ce.source_repo, ce.target_repo) not in denied:
                edges[key] = RepoEdge(
                    source_repo=ce.source_repo,
                    target_repo=ce.target_repo,
                    kind="custom",
                )

        return sorted(edges.values(), key=lambda e: (e.source_repo, e.target_repo))

    def get_related_repos(self, owner: str, repo: str) -> list[tuple[str, str, list[RepoEdge]]]:
        """Get repos related to the given repo.

        Returns list of (repo_name, relationship_type, edges) tuples.
        relationship_type is "dependent", "dependency", or "same_group".
        """
        full_name = f"{owner}/{repo}"
        edges = self.resolve_edges()
        result: dict[str, tuple[str, list[RepoEdge]]] = {}

        for edge in edges:
            if edge.source_repo == full_name:
                key = edge.target_repo
                if key not in result:
                    result[key] = ("dependency", [])
                result[key][1].append(edge)
            elif edge.target_repo == full_name:
                key = edge.source_repo
                if key not in result:
                    result[key] = ("dependent", [])
                result[key][1].append(edge)

        # Also add same-group repos
        groups = self.group_repos(self.repos)
        for group in groups:
            if full_name in group.repos:
                for repo_name in group.repos:
                    if repo_name != full_name and repo_name not in result:
                        result[repo_name] = ("same_group", [])

        return [(k, v[0], v[1]) for k, v in sorted(result.items())]

    # ── Content-aware repo grouping ──────────────────────────────────

    def group_repos(self, repo_names: list[str]) -> list[RepoGroup]:
        """Group repos using multiple content-aware signals.

        Combines four signals to determine groups:
        1. Mutual edges — repos that reference each other directly
        2. Shared dependencies — repos importing the same internal libraries
        3. Content similarity — overlapping domain keywords in file summaries
        4. Naming convention — prefix/suffix patterns (weakest signal, needs confirmation)

        Each candidate group needs at least 2 signals or 1 strong signal
        (mutual edges) to be accepted.
        """
        edges = self.resolve_edges()

        # Detect utility repos first — they must not bridge unrelated groups
        repo_deps: dict[str, set[str]] = {}
        for e in edges:
            repo_deps.setdefault(e.source_repo, set()).add(e.target_repo)
        utility_repos = self._detect_utility_repos(repo_names, repo_deps)

        # Build per-pair evidence
        pair_evidence: dict[frozenset[str], list[str]] = {}
        pair_scores: dict[frozenset[str], float] = {}

        def _add(pair: frozenset[str], evidence: str, score: float) -> None:
            pair_evidence.setdefault(pair, []).append(evidence)
            pair_scores[pair] = pair_scores.get(pair, 0.0) + score

        # Signal 1: Mutual edges (strongest — 0.5)
        # Edges TO or FROM utility repos don't count for grouping —
        # everyone depends on shared-lib, that doesn't make them related.
        edge_pairs: dict[frozenset[str], list[str]] = {}
        for e in edges:
            if e.source_repo in utility_repos or e.target_repo in utility_repos:
                continue  # Skip utility repo edges for grouping
            pair = frozenset([e.source_repo, e.target_repo])
            ref_kinds = {r.kind for r in e.refs}
            edge_pairs.setdefault(pair, []).extend(ref_kinds)

        for pair, kinds in edge_pairs.items():
            repos = sorted(pair)
            kind_str = ", ".join(sorted(set(kinds)))
            _add(pair, f"direct dependency ({kind_str})", 0.4)

            # Mutual edges are even stronger
            has_forward = any(
                e.source_repo == repos[0] and e.target_repo == repos[1]
                for e in edges
                if e.source_repo not in utility_repos and e.target_repo not in utility_repos
            )
            has_reverse = any(
                e.source_repo == repos[1] and e.target_repo == repos[0]
                for e in edges
                if e.source_repo not in utility_repos and e.target_repo not in utility_repos
            )
            if has_forward and has_reverse:
                _add(pair, "mutual references (A↔B)", 0.2)

        # Signal 2: Shared internal dependencies (0.3)
        # Utility repos excluded from bridging — already detected above.
        repo_list = [r for r in repo_names if r in self._stores]
        for i, repo_a in enumerate(repo_list):
            for repo_b in repo_list[i + 1 :]:
                deps_a = repo_deps.get(repo_a, set())
                deps_b = repo_deps.get(repo_b, set())
                # Only count shared deps that aren't utility repos
                shared = (deps_a & deps_b) - {repo_a, repo_b} - utility_repos
                if shared:
                    pair = frozenset([repo_a, repo_b])
                    shared_names = ", ".join(s.split("/")[-1] for s in sorted(shared))
                    _add(pair, f"shared dependencies: {shared_names}", 0.3)

        # Signal 3: Content similarity via domain keyword overlap (0.3)
        repo_keywords = self._extract_repo_keywords()
        for i, repo_a in enumerate(repo_list):
            for repo_b in repo_list[i + 1 :]:
                kw_a = repo_keywords.get(repo_a, set())
                kw_b = repo_keywords.get(repo_b, set())
                if kw_a and kw_b:
                    overlap = kw_a & kw_b
                    union = kw_a | kw_b
                    if union:
                        jaccard = len(overlap) / len(union)
                        if jaccard >= 0.20 and len(overlap) >= 3:
                            pair = frozenset([repo_a, repo_b])
                            top_shared = sorted(overlap)[:5]
                            _add(
                                pair,
                                f"content overlap: {', '.join(top_shared)}",
                                min(jaccard * 1.5, 0.3),
                            )

        # Signal 4: Naming convention (weakest — 0.15)
        short_names: dict[str, str] = {}
        for full in repo_names:
            parts = full.split("/")
            short_names[parts[-1] if len(parts) >= 2 else full] = full

        name_groups: dict[str, list[str]] = {}
        for short, full in short_names.items():
            prefix = self._extract_group_prefix(short)
            if prefix:
                name_groups.setdefault(prefix, []).append(full)

        for prefix, members in name_groups.items():
            if len(members) >= 2:
                for i, a in enumerate(members):
                    for b in members[i + 1 :]:
                        pair = frozenset([a, b])
                        _add(pair, f"naming pattern: '{prefix}-*'", 0.15)

        # ── Merge pairs into groups using union-find ──
        # Only include pairs that meet the threshold
        MIN_SCORE = 0.3  # Need more than just a name match
        qualified_pairs = {pair for pair, score in pair_scores.items() if score >= MIN_SCORE}

        # Union-find to merge overlapping pairs into groups
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            while parent.get(x, x) != x:
                parent[x] = parent.get(parent[x], parent[x])
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for pair in qualified_pairs:
            repos = list(pair)
            if len(repos) == 2:
                union(repos[0], repos[1])

        # Collect groups
        clusters: dict[str, list[str]] = {}
        all_in_pairs = set()
        for pair in qualified_pairs:
            all_in_pairs.update(pair)
        for repo in all_in_pairs:
            root = find(repo)
            clusters.setdefault(root, []).append(repo)

        # Build final groups
        result: list[RepoGroup] = []
        for members in clusters.values():
            if len(members) < 2:
                continue

            members = sorted(members)

            # Collect all evidence and max score for this group
            all_evidence: list[str] = []
            total_score = 0.0
            for i, a in enumerate(members):
                for b in members[i + 1 :]:
                    pair = frozenset([a, b])
                    if pair in pair_evidence:
                        all_evidence.extend(pair_evidence[pair])
                    total_score = max(total_score, pair_scores.get(pair, 0.0))

            # Deduplicate evidence
            seen: set[str] = set()
            unique_evidence: list[str] = []
            for e in all_evidence:
                if e not in seen:
                    seen.add(e)
                    unique_evidence.append(e)

            # Derive group name from common keywords or naming prefix
            group_name = self._derive_group_name(members, repo_keywords)

            result.append(
                RepoGroup(
                    name=group_name,
                    repos=members,
                    confidence=min(total_score, 1.0),
                    evidence=unique_evidence,
                )
            )

        return sorted(result, key=lambda g: (-g.confidence, g.name))

    def _detect_utility_repos(
        self, repo_names: list[str], repo_deps: dict[str, set[str]]
    ) -> set[str]:
        """Identify repos that are general utilities/shared libraries.

        A utility repo is one that:
        - Has a name suggesting shared/common/lib/infra purpose, OR
        - Is depended on by many repos (>= 3) but depends on few itself
        """
        utility_names = {"shared", "common", "lib", "core", "infra", "utils", "tools", "platform"}
        utility_repos: set[str] = set()

        # Count how many repos depend on each repo
        dependents_count: dict[str, int] = {}
        for _source, targets in repo_deps.items():
            for t in targets:
                dependents_count[t] = dependents_count.get(t, 0) + 1

        for full_name in repo_names:
            short = full_name.split("/")[-1].lower()

            # Check name
            name_parts = re.split(r"[-._]", short)
            if any(part in utility_names for part in name_parts):
                utility_repos.add(full_name)
                continue

            # Check topology: many dependents, few dependencies
            n_dependents = dependents_count.get(full_name, 0)
            n_deps = len(repo_deps.get(full_name, set()))
            if n_dependents >= 3 and n_deps <= 1:
                utility_repos.add(full_name)

        return utility_repos

    def _extract_repo_keywords(self) -> dict[str, set[str]]:
        """Extract domain keywords from each repo's file summaries."""
        keywords: dict[str, set[str]] = {}

        for repo_name, store in self._stores.items():
            words: Counter[str] = Counter()
            for path in store.all_paths():
                summary = store.get_summary(path)
                if summary and summary.summary:
                    for word in re.findall(r"[a-z]{3,}", summary.summary.lower()):
                        if word not in _STOP_WORDS:
                            words[word] += 1
                    # Also include symbol names as domain signals
                    for sym in summary.symbols:
                        # Split camelCase/snake_case symbol names into words
                        for part in re.findall(
                            r"[a-z]{3,}", re.sub(r"([A-Z])", r" \1", sym.name).lower()
                        ):
                            if part not in _STOP_WORDS:
                                words[part] += 1

            # Keep top keywords (frequency > 1 or strongly domain-specific)
            keywords[repo_name] = {
                word for word, count in words.items() if count >= 1 and len(word) >= 4
            }

        return keywords

    def _derive_group_name(self, members: list[str], repo_keywords: dict[str, set[str]]) -> str:
        """Derive a meaningful group name from member repos.

        Preference order:
        1. Naming prefix (e.g. 'payments' from payments-service, payments-worker)
        2. Longest common prefix of repo names
        3. Shared domain keyword from summaries (filtered to avoid adjectives)
        """
        short_names = [m.split("/")[-1] for m in members]

        # 1. Try naming prefix via component suffixes
        prefixes: Counter[str] = Counter()
        for short in short_names:
            prefix = self._extract_group_prefix(short)
            if prefix:
                prefixes[prefix] += 1

        if prefixes:
            best_prefix, count = prefixes.most_common(1)[0]
            if count >= 2:
                return best_prefix

        # 2. Longest common substring of repo names
        if len(short_names) >= 2:
            common = _longest_common_prefix(short_names)
            common = common.rstrip("-._")
            if len(common) >= 3:
                return common

        # 3. Most common shared domain keyword — filter out adjectives/fluff
        _FILLER_WORDS = {
            "comprehensive",
            "modern",
            "simple",
            "advanced",
            "complete",
            "lightweight",
            "robust",
            "flexible",
            "powerful",
            "fast",
            "small",
            "tiny",
            "minimal",
            "clean",
            "elegant",
            "beautiful",
            "generic",
            "basic",
            "standard",
            "default",
            "custom",
            "provides",
            "includes",
            "offers",
            "contains",
            "supports",
            "application",
            "project",
            "repository",
            "codebase",
        }
        if members:
            shared = set.intersection(*(repo_keywords.get(m, set()) for m in members))
            shared = {w for w in shared if w not in _FILLER_WORDS and len(w) >= 4}
            if shared:
                ranked = sorted(shared, key=lambda w: (-len(w), w))
                return ranked[0]

        return "+".join(short_names[:2])

    @staticmethod
    def _extract_group_prefix(name: str) -> str | None:
        """Extract a group prefix from a repo name.

        Splits on '-', '.', '_' and returns the prefix if the remainder
        looks like a component suffix.
        """
        for sep in ("-", ".", "_"):
            parts = name.split(sep)
            if len(parts) >= 2:
                suffix = parts[-1].lower()
                if suffix in _COMPONENT_SUFFIXES:
                    return sep.join(parts[:-1])

        return None

    @staticmethod
    def _match_target_to_repo(
        target: str,
        known_repos: set[str],
        short_to_full: dict[str, str],
    ) -> str | None:
        """Try to match an external ref target to a known indexed repo."""
        for pattern in (_GITHUB_URL, _GITHUB_SSH, _GO_IMPORT, _TERRAFORM_SOURCE):
            match = pattern.search(target)
            if match:
                repo_ref = match.group(1).rstrip("/").removesuffix(".git")
                if repo_ref in known_repos:
                    return repo_ref
                parts = repo_ref.split("/")
                if len(parts) == 2 and parts[1] in short_to_full:
                    return short_to_full[parts[1]]

        docker_match = _DOCKER_IMAGE.search(target)
        if docker_match:
            image_ref = docker_match.group(1)
            parts = image_ref.split("/")
            repo_name = parts[-1] if parts else image_ref
            if repo_name in short_to_full:
                return short_to_full[repo_name]

        clean = target.lstrip("@").split("/")[-1] if "/" in target else target
        if clean in short_to_full:
            return short_to_full[clean]

        return None

    # ── Relationship overrides ──

    def set_override(self, source_repo: str, target_repo: str, status: str) -> RelationshipOverride:
        """Confirm or deny an edge. Status must be 'confirmed' or 'denied'."""
        now = time.time()
        self._overrides_db.execute(
            "INSERT INTO relationship_overrides (source_repo, target_repo, status, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(source_repo, target_repo) DO UPDATE SET status=?, created_at=?",
            (source_repo, target_repo, status, now, status, now),
        )
        self._overrides_db.commit()
        return RelationshipOverride(
            source_repo=source_repo, target_repo=target_repo, status=status, created_at=now
        )

    def delete_override(self, source_repo: str, target_repo: str) -> None:
        self._overrides_db.execute(
            "DELETE FROM relationship_overrides WHERE source_repo = ? AND target_repo = ?",
            (source_repo, target_repo),
        )
        self._overrides_db.commit()

    def list_overrides(self) -> list[RelationshipOverride]:
        rows = self._overrides_db.execute(
            "SELECT source_repo, target_repo, status, created_at FROM relationship_overrides"
        ).fetchall()
        return [
            RelationshipOverride(source_repo=r[0], target_repo=r[1], status=r[2], created_at=r[3])
            for r in rows
        ]

    # ── Custom edges ──

    def add_custom_edge(self, source_repo: str, target_repo: str, reason: str) -> CustomEdge:
        now = time.time()
        self._overrides_db.execute(
            "INSERT INTO custom_edges (source_repo, target_repo, reason, created_at) VALUES (?, ?, ?, ?)",
            (source_repo, target_repo, reason, now),
        )
        self._overrides_db.commit()
        row_id = self._overrides_db.execute("SELECT last_insert_rowid()").fetchone()[0]
        return CustomEdge(
            id=row_id,
            source_repo=source_repo,
            target_repo=target_repo,
            reason=reason,
            created_at=now,
        )

    def delete_custom_edge(self, edge_id: int) -> None:
        self._overrides_db.execute("DELETE FROM custom_edges WHERE id = ?", (edge_id,))
        self._overrides_db.commit()

    def list_custom_edges(self) -> list[CustomEdge]:
        rows = self._overrides_db.execute(
            "SELECT id, source_repo, target_repo, reason, created_at FROM custom_edges"
        ).fetchall()
        return [
            CustomEdge(id=r[0], source_repo=r[1], target_repo=r[2], reason=r[3], created_at=r[4])
            for r in rows
        ]

    def close(self) -> None:
        """Close all open stores."""
        for store in self._stores.values():
            store.close()
        self._stores.clear()
        self._overrides_db.close()


def _longest_common_prefix(strings: list[str]) -> str:
    """Find the longest common prefix among a list of strings."""
    if not strings:
        return ""
    prefix = strings[0]
    for s in strings[1:]:
        while not s.startswith(prefix):
            prefix = prefix[:-1]
            if not prefix:
                return ""
    return prefix
