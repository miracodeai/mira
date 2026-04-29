"""SQLite-backed storage for file summaries. One DB per repo."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_INDEX_DIR = os.environ.get("MIRA_INDEX_DIR", "/data/indexes")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    language TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS symbols (
    file_path TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'function',
    signature TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (file_path, name),
    FOREIGN KEY (file_path) REFERENCES files(path) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS imports (
    source_path TEXT NOT NULL,
    target_path TEXT NOT NULL,
    PRIMARY KEY (source_path, target_path),
    FOREIGN KEY (source_path) REFERENCES files(path) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS symbol_refs (
    source_path TEXT NOT NULL,
    source_symbol TEXT NOT NULL,
    target_path TEXT NOT NULL,
    target_symbol TEXT NOT NULL,
    PRIMARY KEY (source_path, source_symbol, target_path, target_symbol),
    FOREIGN KEY (source_path) REFERENCES files(path) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS directories (
    path TEXT PRIMARY KEY,
    summary TEXT NOT NULL DEFAULT '',
    file_count INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS external_refs (
    file_path TEXT NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (file_path, kind, target),
    FOREIGN KEY (file_path) REFERENCES files(path) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS review_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_number INTEGER NOT NULL DEFAULT 0,
    pr_title TEXT NOT NULL DEFAULT '',
    pr_url TEXT NOT NULL DEFAULT '',
    comments_posted INTEGER NOT NULL DEFAULT 0,
    blockers INTEGER NOT NULL DEFAULT 0,
    warnings INTEGER NOT NULL DEFAULT 0,
    suggestions INTEGER NOT NULL DEFAULT 0,
    files_reviewed INTEGER NOT NULL DEFAULT 0,
    lines_changed INTEGER NOT NULL DEFAULT 0,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    categories TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS review_context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS feedback_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_number INTEGER NOT NULL DEFAULT 0,
    pr_url TEXT NOT NULL DEFAULT '',
    comment_path TEXT NOT NULL DEFAULT '',
    comment_line INTEGER NOT NULL DEFAULT 0,
    comment_category TEXT NOT NULL DEFAULT '',
    comment_severity TEXT NOT NULL DEFAULT '',
    comment_title TEXT NOT NULL DEFAULT '',
    signal TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS learned_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_text TEXT NOT NULL DEFAULT '',
    source_signal TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    path_pattern TEXT NOT NULL DEFAULT '',
    sample_count INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS package_manifests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL DEFAULT '',
    is_dev INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    UNIQUE(name, kind, file_path)
);

CREATE INDEX IF NOT EXISTS idx_pkg_manifest_name ON package_manifests(name);

CREATE TABLE IF NOT EXISTS vulnerabilities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_name TEXT NOT NULL,
    ecosystem TEXT NOT NULL,
    package_version TEXT NOT NULL DEFAULT '',
    cve_id TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'unknown',
    advisory_url TEXT NOT NULL DEFAULT '',
    fixed_in TEXT NOT NULL DEFAULT '',
    first_seen_at REAL NOT NULL DEFAULT 0,
    last_seen_at REAL NOT NULL DEFAULT 0,
    UNIQUE(package_name, ecosystem, package_version, cve_id)
);

CREATE INDEX IF NOT EXISTS idx_vuln_package
    ON vulnerabilities(package_name, ecosystem, package_version);
CREATE INDEX IF NOT EXISTS idx_vuln_severity ON vulnerabilities(severity);
"""


@dataclass
class SymbolInfo:
    name: str
    kind: str  # "function", "class", "method", "constant"
    signature: str  # e.g. "def authenticate(token: str) -> Session"
    description: str  # one-line description


@dataclass
class FileSummary:
    path: str
    language: str
    summary: str
    symbols: list[SymbolInfo] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    # (source_symbol, target_path, target_symbol)
    symbol_refs: list[tuple[str, str, str]] = field(default_factory=list)
    external_refs: list[ExternalRef] = field(default_factory=list)
    content_hash: str = ""
    updated_at: float = 0.0


@dataclass
class DirectorySummary:
    path: str
    summary: str
    file_count: int
    updated_at: float = 0.0


@dataclass
class ExternalRef:
    file_path: str
    kind: str  # terraform_module, docker_image, api_endpoint, go_import, git_url, npm_package, pip_package
    target: str
    description: str = ""


@dataclass
class ReviewEvent:
    id: int
    pr_number: int
    pr_title: str
    pr_url: str
    comments_posted: int
    blockers: int
    warnings: int
    suggestions: int
    files_reviewed: int
    lines_changed: int
    tokens_used: int
    duration_ms: int
    categories: str  # comma-separated: "bug,security,performance"
    created_at: float = 0.0


@dataclass
class ReviewContext:
    id: int
    title: str
    content: str
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class FeedbackEventRow:
    id: int
    pr_number: int
    pr_url: str
    comment_path: str
    comment_line: int
    comment_category: str
    comment_severity: str
    comment_title: str
    signal: str
    actor: str
    created_at: float = 0.0


@dataclass
class LearnedRuleRow:
    id: int
    rule_text: str
    source_signal: str
    category: str
    path_pattern: str
    sample_count: int
    active: bool = True
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class PackageManifestRow:
    id: int
    name: str
    kind: str  # "npm" | "pip" | "docker" | "go" | "rust"
    version: str
    file_path: str
    is_dev: bool = False
    updated_at: float = 0.0


@dataclass
class VulnerabilityRow:
    id: int
    package_name: str
    ecosystem: str  # Mira's internal kind ("npm" | "pip" | "go" | "rust")
    package_version: str
    cve_id: str
    summary: str
    severity: str  # "critical" | "high" | "moderate" | "low" | "unknown"
    advisory_url: str
    fixed_in: str
    first_seen_at: float = 0.0
    last_seen_at: float = 0.0


@dataclass
class BlastRadiusEntry:
    path: str
    summary: str
    affected_symbols: list[str]
    depth: int


class IndexStore:
    """SQLite-backed index for a single repository."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @classmethod
    def open(cls, owner: str, repo: str):  # type: ignore[no-untyped-def]
        """Open (or create) the index store for a repo.

        Returns a PgIndexStore if DATABASE_URL is set, otherwise an IndexStore
        backed by a per-repo SQLite file.
        """
        db_url = os.environ.get("DATABASE_URL", "")
        if db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
            try:
                from mira.index.pg_store import PgIndexStore

                return PgIndexStore(owner, repo, db_url)
            except Exception as exc:
                logger.warning("Postgres store unavailable (%s), falling back to SQLite", exc)

        index_dir = os.environ.get("MIRA_INDEX_DIR", _INDEX_DIR)
        repo_dir = os.path.join(index_dir, owner)
        os.makedirs(repo_dir, exist_ok=True)
        db_path = os.path.join(repo_dir, f"{repo}.db")
        return cls(db_path)

    def get_summary(self, path: str) -> FileSummary | None:
        """Get the summary for a single file."""
        row = self._conn.execute(
            "SELECT path, language, summary, content_hash, updated_at FROM files WHERE path = ?",
            (path,),
        ).fetchone()
        if row is None:
            return None
        fs = FileSummary(
            path=row[0],
            language=row[1],
            summary=row[2],
            content_hash=row[3],
            updated_at=row[4],
        )
        fs.symbols = self._load_symbols(path)
        fs.imports = self._load_imports(path)
        fs.symbol_refs = self._load_symbol_refs(path)
        fs.external_refs = self._load_external_refs(path)
        return fs

    def get_summaries(self, paths: list[str]) -> dict[str, FileSummary]:
        """Get summaries for multiple files."""
        result: dict[str, FileSummary] = {}
        for path in paths:
            s = self.get_summary(path)
            if s is not None:
                result[path] = s
        return result

    def get_dependents(self, path: str) -> list[str]:
        """Files that import this path."""
        rows = self._conn.execute(
            "SELECT source_path FROM imports WHERE target_path = ?", (path,)
        ).fetchall()
        return [r[0] for r in rows]

    def get_directory_summary(self, path: str) -> DirectorySummary | None:
        """Get summary for a single directory."""
        row = self._conn.execute(
            "SELECT path, summary, file_count, updated_at FROM directories WHERE path = ?",
            (path,),
        ).fetchone()
        if row is None:
            return None
        return DirectorySummary(path=row[0], summary=row[1], file_count=row[2], updated_at=row[3])

    def get_directory_summaries(self, paths: list[str]) -> dict[str, DirectorySummary]:
        """Get summaries for multiple directories."""
        result: dict[str, DirectorySummary] = {}
        for path in paths:
            ds = self.get_directory_summary(path)
            if ds is not None:
                result[path] = ds
        return result

    def upsert_summary(self, summary: FileSummary) -> None:
        """Insert or update a file summary and its related data."""
        now = time.time()
        self._conn.execute(
            """INSERT INTO files (path, language, summary, content_hash, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                 language=excluded.language,
                 summary=excluded.summary,
                 content_hash=excluded.content_hash,
                 updated_at=excluded.updated_at""",
            (summary.path, summary.language, summary.summary, summary.content_hash, now),
        )
        # Replace symbols
        self._conn.execute("DELETE FROM symbols WHERE file_path = ?", (summary.path,))
        for sym in summary.symbols:
            self._conn.execute(
                "INSERT INTO symbols (file_path, name, kind, signature, description) "
                "VALUES (?, ?, ?, ?, ?)",
                (summary.path, sym.name, sym.kind, sym.signature, sym.description),
            )
        # Replace imports (dedup)
        self._conn.execute("DELETE FROM imports WHERE source_path = ?", (summary.path,))
        for target in set(summary.imports):
            self._conn.execute(
                "INSERT OR IGNORE INTO imports (source_path, target_path) VALUES (?, ?)",
                (summary.path, target),
            )
        # Replace symbol refs (dedup)
        self._conn.execute("DELETE FROM symbol_refs WHERE source_path = ?", (summary.path,))
        for src_sym, tgt_path, tgt_sym in set(summary.symbol_refs):
            self._conn.execute(
                "INSERT OR IGNORE INTO symbol_refs "
                "(source_path, source_symbol, target_path, target_symbol) "
                "VALUES (?, ?, ?, ?)",
                (summary.path, src_sym, tgt_path, tgt_sym),
            )
        # Replace external refs (dedup by kind+target)
        self._conn.execute("DELETE FROM external_refs WHERE file_path = ?", (summary.path,))
        seen_refs: set[tuple[str, str]] = set()
        for ref in summary.external_refs:
            key = (ref.kind, ref.target)
            if key in seen_refs:
                continue
            seen_refs.add(key)
            self._conn.execute(
                "INSERT OR IGNORE INTO external_refs (file_path, kind, target, description) "
                "VALUES (?, ?, ?, ?)",
                (summary.path, ref.kind, ref.target, ref.description),
            )
        self._conn.commit()

    def upsert_batch(self, summaries: list[FileSummary]) -> None:
        """Insert or update multiple file summaries in a single transaction."""
        for s in summaries:
            self.upsert_summary(s)

    def upsert_directory(self, summary: DirectorySummary) -> None:
        """Insert or update a directory summary."""
        now = time.time()
        self._conn.execute(
            """INSERT INTO directories (path, summary, file_count, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET
                 summary=excluded.summary,
                 file_count=excluded.file_count,
                 updated_at=excluded.updated_at""",
            (summary.path, summary.summary, summary.file_count, now),
        )
        self._conn.commit()

    def remove_paths(self, paths: list[str]) -> None:
        """Remove files (and their symbols/imports via CASCADE) from the index."""
        for path in paths:
            self._conn.execute("DELETE FROM files WHERE path = ?", (path,))
        self._conn.commit()

    def all_paths(self) -> set[str]:
        """Return all indexed file paths."""
        rows = self._conn.execute("SELECT path FROM files").fetchall()
        return {r[0] for r in rows}

    def get_call_graph(self, path: str, symbol: str) -> list[tuple[str, str]]:
        """Who calls this symbol? Returns list of (file_path, calling_symbol)."""
        rows = self._conn.execute(
            "SELECT source_path, source_symbol FROM symbol_refs "
            "WHERE target_path = ? AND target_symbol = ?",
            (path, symbol),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def get_reverse_deps(self, path: str, max_depth: int = 3) -> list[str]:
        """All files that (transitively) depend on this file, up to max_depth."""
        visited: set[str] = set()
        frontier = {path}
        for _ in range(max_depth):
            next_frontier: set[str] = set()
            for p in frontier:
                if p in visited:
                    continue
                visited.add(p)
                for dep in self.get_dependents(p):
                    if dep not in visited:
                        next_frontier.add(dep)
            frontier = next_frontier
            if not frontier:
                break
        visited.discard(path)
        return sorted(visited)

    def get_inbound_edge_counts(self, paths: list[str]) -> dict[str, int]:
        """Count how many other files reference each path via symbol_refs or imports.

        Used to rank files by importance — files with more callers/importers
        should get priority in the context budget.
        """
        counts: dict[str, int] = {}
        for path in paths:
            # Count import dependents
            import_count = self._conn.execute(
                "SELECT COUNT(*) FROM imports WHERE target_path = ?", (path,)
            ).fetchone()[0]
            # Count symbol_ref callers
            ref_count = self._conn.execute(
                "SELECT COUNT(DISTINCT source_path) FROM symbol_refs WHERE target_path = ?",
                (path,),
            ).fetchone()[0]
            counts[path] = import_count + ref_count
        return counts

    def get_blast_radius(self, changed_paths: list[str]) -> list[BlastRadiusEntry]:
        """For changed files, compute which files + symbols are affected."""
        entries: dict[str, BlastRadiusEntry] = {}

        for changed_path in changed_paths:
            # Get all symbols in the changed file
            symbols = self._load_symbols(changed_path)
            for sym in symbols:
                callers = self.get_call_graph(changed_path, sym.name)
                for caller_path, caller_symbol in callers:
                    if caller_path in changed_paths:
                        continue
                    if caller_path not in entries:
                        # Fetch summary for the caller file
                        row = self._conn.execute(
                            "SELECT summary FROM files WHERE path = ?", (caller_path,)
                        ).fetchone()
                        summary = row[0] if row else ""
                        entries[caller_path] = BlastRadiusEntry(
                            path=caller_path, summary=summary, affected_symbols=[], depth=1
                        )
                    entry = entries[caller_path]
                    if caller_symbol not in entry.affected_symbols:
                        entry.affected_symbols.append(caller_symbol)

        # Depth 2: callers of callers
        depth1_paths = list(entries.keys())
        for d1_path in depth1_paths:
            d1_entry = entries[d1_path]
            for affected_sym in list(d1_entry.affected_symbols):
                callers = self.get_call_graph(d1_path, affected_sym)
                for caller_path, caller_symbol in callers:
                    if caller_path in changed_paths or caller_path in depth1_paths:
                        continue
                    if caller_path not in entries:
                        row = self._conn.execute(
                            "SELECT summary FROM files WHERE path = ?", (caller_path,)
                        ).fetchone()
                        summary = row[0] if row else ""
                        entries[caller_path] = BlastRadiusEntry(
                            path=caller_path, summary=summary, affected_symbols=[], depth=2
                        )
                    entry = entries[caller_path]
                    if caller_symbol not in entry.affected_symbols:
                        entry.affected_symbols.append(caller_symbol)

        return sorted(entries.values(), key=lambda e: (e.depth, e.path))

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    # ── Review events ──

    def record_review(
        self,
        pr_number: int,
        pr_title: str,
        pr_url: str,
        comments_posted: int,
        blockers: int,
        warnings: int,
        suggestions: int = 0,
        files_reviewed: int = 0,
        lines_changed: int = 0,
        tokens_used: int = 0,
        duration_ms: int = 0,
        categories: str = "",
        created_at: float | None = None,
    ) -> ReviewEvent:
        now = created_at if created_at is not None else time.time()
        self._conn.execute(
            "INSERT INTO review_events "
            "(pr_number, pr_title, pr_url, comments_posted, blockers, warnings, "
            "suggestions, files_reviewed, lines_changed, tokens_used, duration_ms, "
            "categories, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pr_number,
                pr_title,
                pr_url,
                comments_posted,
                blockers,
                warnings,
                suggestions,
                files_reviewed,
                lines_changed,
                tokens_used,
                duration_ms,
                categories,
                now,
            ),
        )
        self._conn.commit()
        row_id = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return ReviewEvent(
            id=row_id,
            pr_number=pr_number,
            pr_title=pr_title,
            pr_url=pr_url,
            comments_posted=comments_posted,
            blockers=blockers,
            warnings=warnings,
            suggestions=suggestions,
            files_reviewed=files_reviewed,
            lines_changed=lines_changed,
            tokens_used=tokens_used,
            duration_ms=duration_ms,
            categories=categories,
            created_at=now,
        )

    def list_review_events(self, limit: int = 100) -> list[ReviewEvent]:
        rows = self._conn.execute(
            "SELECT id, pr_number, pr_title, pr_url, comments_posted, blockers, warnings, "
            "suggestions, files_reviewed, lines_changed, tokens_used, duration_ms, "
            "categories, created_at "
            "FROM review_events ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            ReviewEvent(
                id=r[0],
                pr_number=r[1],
                pr_title=r[2],
                pr_url=r[3],
                comments_posted=r[4],
                blockers=r[5],
                warnings=r[6],
                suggestions=r[7],
                files_reviewed=r[8],
                lines_changed=r[9],
                tokens_used=r[10],
                duration_ms=r[11],
                categories=r[12],
                created_at=r[13],
            )
            for r in rows
        ]

    def get_review_stats(self, since: float | None = None) -> dict:
        """Aggregate review statistics, optionally filtered to events after *since* (epoch)."""
        where = " WHERE created_at >= ?" if since else ""
        params: tuple = (since,) if since else ()

        row = self._conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(comments_posted),0), COALESCE(SUM(blockers),0), "
            "COALESCE(SUM(warnings),0), COALESCE(SUM(suggestions),0), "
            "COALESCE(SUM(files_reviewed),0), COALESCE(SUM(lines_changed),0), "
            "COALESCE(SUM(tokens_used),0), COALESCE(AVG(duration_ms),0) "
            f"FROM review_events{where}",
            params,
        ).fetchone()

        # Aggregate categories
        cat_where = f" WHERE categories != ''{' AND created_at >= ?' if since else ''}"
        cat_params: tuple = (since,) if since else ()
        cat_rows = self._conn.execute(
            f"SELECT categories FROM review_events{cat_where}",
            cat_params,
        ).fetchall()
        cat_counts: dict[str, int] = {}
        for (cats,) in cat_rows:
            for c in cats.split(","):
                c = c.strip()
                if c:
                    cat_counts[c] = cat_counts.get(c, 0) + 1

        return {
            "total_reviews": row[0],
            "total_comments": row[1],
            "total_blockers": row[2],
            "total_warnings": row[3],
            "total_suggestions": row[4],
            "total_files_reviewed": row[5],
            "total_lines_changed": row[6],
            "total_tokens": row[7],
            "avg_duration_ms": int(row[8]),
            "categories": cat_counts,
        }

    # ── Review context CRUD ──

    def list_review_context(self) -> list[ReviewContext]:
        """List all review context entries."""
        rows = self._conn.execute(
            "SELECT id, title, content, created_at, updated_at FROM review_context ORDER BY updated_at DESC"
        ).fetchall()
        return [
            ReviewContext(id=r[0], title=r[1], content=r[2], created_at=r[3], updated_at=r[4])
            for r in rows
        ]

    def get_review_context(self, context_id: int) -> ReviewContext | None:
        row = self._conn.execute(
            "SELECT id, title, content, created_at, updated_at FROM review_context WHERE id = ?",
            (context_id,),
        ).fetchone()
        if row is None:
            return None
        return ReviewContext(
            id=row[0], title=row[1], content=row[2], created_at=row[3], updated_at=row[4]
        )

    def upsert_review_context(
        self, title: str, content: str, context_id: int | None = None
    ) -> ReviewContext:
        """Create or update a review context entry."""
        now = time.time()
        if context_id is not None:
            self._conn.execute(
                "UPDATE review_context SET title = ?, content = ?, updated_at = ? WHERE id = ?",
                (title, content, now, context_id),
            )
        else:
            self._conn.execute(
                "INSERT INTO review_context (title, content, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (title, content, now, now),
            )
            context_id = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self._conn.commit()
        return self.get_review_context(context_id)  # type: ignore[return-value]

    def delete_review_context(self, context_id: int) -> None:
        self._conn.execute("DELETE FROM review_context WHERE id = ?", (context_id,))
        self._conn.commit()

    def get_all_review_context_text(self) -> str:
        """Get all review context concatenated for injection into prompts."""
        entries = self.list_review_context()
        if not entries:
            return ""
        parts = ["## Repository Documentation Context\n"]
        for entry in entries:
            parts.append(f"### {entry.title}\n{entry.content}\n")
        return "\n".join(parts)

    # ── External refs ──

    def get_external_refs_for_paths(self, paths: list[str]) -> list[ExternalRef]:
        """Get all external refs for the given file paths."""
        result: list[ExternalRef] = []
        for path in paths:
            result.extend(self._load_external_refs(path))
        return result

    def get_files_referencing(self, target: str) -> list[ExternalRef]:
        """Find all external refs whose target contains the given string."""
        rows = self._conn.execute(
            "SELECT file_path, kind, target, description FROM external_refs WHERE target LIKE ?",
            (f"%{target}%",),
        ).fetchall()
        return [ExternalRef(file_path=r[0], kind=r[1], target=r[2], description=r[3]) for r in rows]

    def get_all_external_targets(self) -> list[str]:
        """Return all unique external ref targets."""
        rows = self._conn.execute("SELECT DISTINCT target FROM external_refs").fetchall()
        return [r[0] for r in rows]

    def _load_external_refs(self, path: str) -> list[ExternalRef]:
        rows = self._conn.execute(
            "SELECT file_path, kind, target, description FROM external_refs WHERE file_path = ?",
            (path,),
        ).fetchall()
        return [ExternalRef(file_path=r[0], kind=r[1], target=r[2], description=r[3]) for r in rows]

    def _load_symbols(self, path: str) -> list[SymbolInfo]:
        rows = self._conn.execute(
            "SELECT name, kind, signature, description FROM symbols WHERE file_path = ?",
            (path,),
        ).fetchall()
        return [SymbolInfo(name=r[0], kind=r[1], signature=r[2], description=r[3]) for r in rows]

    def _load_imports(self, path: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT target_path FROM imports WHERE source_path = ?", (path,)
        ).fetchall()
        return [r[0] for r in rows]

    def _load_symbol_refs(self, path: str) -> list[tuple[str, str, str]]:
        rows = self._conn.execute(
            "SELECT source_symbol, target_path, target_symbol "
            "FROM symbol_refs WHERE source_path = ?",
            (path,),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    # ── Feedback events ──

    def record_feedback(
        self,
        pr_number: int,
        pr_url: str,
        comment_path: str,
        comment_line: int,
        comment_category: str,
        comment_severity: str,
        comment_title: str,
        signal: str,
        actor: str,
    ) -> FeedbackEventRow:
        now = time.time()
        cur = self._conn.execute(
            "INSERT INTO feedback_events "
            "(pr_number, pr_url, comment_path, comment_line, comment_category, "
            "comment_severity, comment_title, signal, actor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pr_number,
                pr_url,
                comment_path,
                comment_line,
                comment_category,
                comment_severity,
                comment_title,
                signal,
                actor,
                now,
            ),
        )
        self._conn.commit()
        return FeedbackEventRow(
            id=cur.lastrowid or 0,
            pr_number=pr_number,
            pr_url=pr_url,
            comment_path=comment_path,
            comment_line=comment_line,
            comment_category=comment_category,
            comment_severity=comment_severity,
            comment_title=comment_title,
            signal=signal,
            actor=actor,
            created_at=now,
        )

    def record_bulk_feedback(self, events: list[dict]) -> int:
        """Insert multiple feedback events in a single transaction.

        Each dict must contain the same keys as record_feedback's parameters.
        Returns the number of rows inserted.
        """
        if not events:
            return 0
        now = time.time()
        rows = [
            (
                e["pr_number"],
                e["pr_url"],
                e["comment_path"],
                e["comment_line"],
                e["comment_category"],
                e["comment_severity"],
                e["comment_title"],
                e["signal"],
                e["actor"],
                now,
            )
            for e in events
        ]
        self._conn.executemany(
            "INSERT INTO feedback_events "
            "(pr_number, pr_url, comment_path, comment_line, comment_category, "
            "comment_severity, comment_title, signal, actor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()
        return len(rows)

    def list_feedback(self, limit: int = 500) -> list[FeedbackEventRow]:
        rows = self._conn.execute(
            "SELECT id, pr_number, pr_url, comment_path, comment_line, "
            "comment_category, comment_severity, comment_title, signal, actor, created_at "
            "FROM feedback_events ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            FeedbackEventRow(
                id=r[0],
                pr_number=r[1],
                pr_url=r[2],
                comment_path=r[3],
                comment_line=r[4],
                comment_category=r[5],
                comment_severity=r[6],
                comment_title=r[7],
                signal=r[8],
                actor=r[9],
                created_at=r[10],
            )
            for r in rows
        ]

    def get_feedback_stats(self) -> dict:
        """Aggregate feedback counts by signal, category, and path directory."""
        rows = self._conn.execute(
            "SELECT signal, comment_category, comment_path, COUNT(*) "
            "FROM feedback_events GROUP BY signal, comment_category, comment_path"
        ).fetchall()
        stats: dict[str, dict[str, int]] = {}
        for signal, category, _path, count in rows:
            key = f"{signal}:{category}"
            stats.setdefault(key, {"total": 0})
            stats[key]["total"] += count
        return stats

    # ── Learned rules ──

    def upsert_learned_rule(
        self,
        rule_text: str,
        source_signal: str,
        category: str,
        path_pattern: str,
        sample_count: int,
    ) -> LearnedRuleRow:
        now = time.time()
        existing = self._conn.execute(
            "SELECT id FROM learned_rules WHERE category = ? AND path_pattern = ?",
            (category, path_pattern),
        ).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE learned_rules SET rule_text = ?, source_signal = ?, "
                "sample_count = ?, updated_at = ? WHERE id = ?",
                (rule_text, source_signal, sample_count, now, existing[0]),
            )
            self._conn.commit()
            return LearnedRuleRow(
                id=existing[0],
                rule_text=rule_text,
                source_signal=source_signal,
                category=category,
                path_pattern=path_pattern,
                sample_count=sample_count,
                created_at=now,
                updated_at=now,
            )
        cur = self._conn.execute(
            "INSERT INTO learned_rules "
            "(rule_text, source_signal, category, path_pattern, sample_count, "
            "active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (rule_text, source_signal, category, path_pattern, sample_count, now, now),
        )
        self._conn.commit()
        return LearnedRuleRow(
            id=cur.lastrowid or 0,
            rule_text=rule_text,
            source_signal=source_signal,
            category=category,
            path_pattern=path_pattern,
            sample_count=sample_count,
            created_at=now,
            updated_at=now,
        )

    def list_active_learned_rules(self) -> list[LearnedRuleRow]:
        rows = self._conn.execute(
            "SELECT id, rule_text, source_signal, category, path_pattern, "
            "sample_count, active, created_at, updated_at "
            "FROM learned_rules WHERE active = 1 ORDER BY sample_count DESC"
        ).fetchall()
        return [
            LearnedRuleRow(
                id=r[0],
                rule_text=r[1],
                source_signal=r[2],
                category=r[3],
                path_pattern=r[4],
                sample_count=r[5],
                active=bool(r[6]),
                created_at=r[7],
                updated_at=r[8],
            )
            for r in rows
        ]

    def get_learned_rules_text(self) -> list[str]:
        """Get active learned rules as a list of rule strings for prompt injection."""
        rules = self.list_active_learned_rules()
        return [r.rule_text for r in rules[:10]]

    # ── Package manifests (deterministic from package.json/requirements.txt/etc) ──

    def replace_manifest_packages(
        self,
        file_path: str,
        packages: list[dict],
    ) -> int:
        """Replace all package entries for a manifest file atomically.

        Called after an indexing pass re-reads the manifest; we want the DB
        to exactly mirror the file. Returns the number of rows inserted.
        """
        now = time.time()
        self._conn.execute(
            "DELETE FROM package_manifests WHERE file_path = ?",
            (file_path,),
        )
        if packages:
            rows = [
                (
                    p["name"],
                    p["kind"],
                    p["version"],
                    p["file_path"],
                    1 if p.get("is_dev") else 0,
                    now,
                )
                for p in packages
            ]
            self._conn.executemany(
                "INSERT OR REPLACE INTO package_manifests "
                "(name, kind, version, file_path, is_dev, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        self._conn.commit()
        return len(packages)

    def list_manifest_packages(self) -> list[PackageManifestRow]:
        rows = self._conn.execute(
            "SELECT id, name, kind, version, file_path, is_dev, updated_at "
            "FROM package_manifests ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [
            PackageManifestRow(
                id=r[0],
                name=r[1],
                kind=r[2],
                version=r[3],
                file_path=r[4],
                is_dev=bool(r[5]),
                updated_at=r[6],
            )
            for r in rows
        ]

    def clear_manifest_packages_for_missing_files(self, live_paths: set[str]) -> int:
        """Drop entries for manifest files that no longer exist in the repo.

        Called during indexing when we've finished the manifest pass and know
        which manifest file paths are still present.
        """
        existing = {
            r[0]
            for r in self._conn.execute(
                "SELECT DISTINCT file_path FROM package_manifests"
            ).fetchall()
        }
        stale = existing - live_paths
        if not stale:
            return 0
        self._conn.executemany(
            "DELETE FROM package_manifests WHERE file_path = ?",
            [(p,) for p in stale],
        )
        self._conn.commit()
        return len(stale)

    # ── Vulnerabilities (from OSV.dev poller) ──

    def replace_vulnerabilities_for_package(
        self,
        package_name: str,
        ecosystem: str,
        package_version: str,
        vulns: list[dict],
    ) -> int:
        """Atomically replace the vulnerability rows for a single (package,
        ecosystem, version). Called after each OSV poll for that combination.

        Each dict must have keys cve_id, summary, severity, advisory_url, fixed_in.
        Empty list clears all vulns for that combination (i.e. package no
        longer affected).
        """
        now = time.time()
        existing = {
            r[0]: r[1]  # cve_id → first_seen_at
            for r in self._conn.execute(
                "SELECT cve_id, first_seen_at FROM vulnerabilities "
                "WHERE package_name = ? AND ecosystem = ? AND package_version = ?",
                (package_name, ecosystem, package_version),
            ).fetchall()
        }
        self._conn.execute(
            "DELETE FROM vulnerabilities "
            "WHERE package_name = ? AND ecosystem = ? AND package_version = ?",
            (package_name, ecosystem, package_version),
        )
        if vulns:
            rows = [
                (
                    package_name,
                    ecosystem,
                    package_version,
                    v["cve_id"],
                    v.get("summary", ""),
                    v.get("severity", "unknown"),
                    v.get("advisory_url", ""),
                    v.get("fixed_in", ""),
                    existing.get(v["cve_id"], now),  # preserve first_seen_at
                    now,
                )
                for v in vulns
            ]
            self._conn.executemany(
                "INSERT INTO vulnerabilities "
                "(package_name, ecosystem, package_version, cve_id, summary, "
                "severity, advisory_url, fixed_in, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        self._conn.commit()
        return len(vulns)

    def list_vulnerabilities(self) -> list[VulnerabilityRow]:
        rows = self._conn.execute(
            "SELECT id, package_name, ecosystem, package_version, cve_id, "
            "summary, severity, advisory_url, fixed_in, first_seen_at, last_seen_at "
            "FROM vulnerabilities "
            "ORDER BY CASE severity "
            "WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            "WHEN 'moderate' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, "
            "package_name COLLATE NOCASE"
        ).fetchall()
        return [
            VulnerabilityRow(
                id=r[0],
                package_name=r[1],
                ecosystem=r[2],
                package_version=r[3],
                cve_id=r[4],
                summary=r[5],
                severity=r[6],
                advisory_url=r[7],
                fixed_in=r[8],
                first_seen_at=r[9],
                last_seen_at=r[10],
            )
            for r in rows
        ]

    def count_vulnerabilities_by_severity(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT severity, COUNT(*) FROM vulnerabilities GROUP BY severity"
        ).fetchall()
        return {r[0]: r[1] for r in rows}
