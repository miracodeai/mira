"""PostgreSQL-backed index store — implements the same interface as IndexStore.

One shared database with `owner`/`repo` columns on every table for scoping.
"""

from __future__ import annotations

import logging
import threading
import time

from mira.index.store import (
    BlastRadiusEntry,
    DirectorySummary,
    ExternalRef,
    FeedbackEventRow,
    FileSummary,
    LearnedRuleRow,
    ReviewContext,
    ReviewEvent,
    SymbolInfo,
)

logger = logging.getLogger(__name__)

_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    updated_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (owner, repo, path)
);

CREATE TABLE IF NOT EXISTS symbols (
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    file_path TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'function',
    signature TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (owner, repo, file_path, name)
);

CREATE TABLE IF NOT EXISTS imports (
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    source_path TEXT NOT NULL,
    target_path TEXT NOT NULL,
    PRIMARY KEY (owner, repo, source_path, target_path)
);

CREATE TABLE IF NOT EXISTS symbol_refs (
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_symbol TEXT NOT NULL,
    target_path TEXT NOT NULL,
    target_symbol TEXT NOT NULL,
    PRIMARY KEY (owner, repo, source_path, source_symbol, target_path, target_symbol)
);

CREATE TABLE IF NOT EXISTS directories (
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    file_count INTEGER NOT NULL DEFAULT 0,
    updated_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    PRIMARY KEY (owner, repo, path)
);

CREATE TABLE IF NOT EXISTS external_refs (
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    file_path TEXT NOT NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (owner, repo, file_path, kind, target)
);

CREATE TABLE IF NOT EXISTS review_events (
    id SERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
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
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS review_context (
    id SERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_at DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS feedback_events (
    id SERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL DEFAULT 0,
    pr_url TEXT NOT NULL DEFAULT '',
    comment_path TEXT NOT NULL DEFAULT '',
    comment_line INTEGER NOT NULL DEFAULT 0,
    comment_category TEXT NOT NULL DEFAULT '',
    comment_severity TEXT NOT NULL DEFAULT '',
    comment_title TEXT NOT NULL DEFAULT '',
    signal TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS learned_rules (
    id SERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    rule_text TEXT NOT NULL DEFAULT '',
    source_signal TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    path_pattern TEXT NOT NULL DEFAULT '',
    sample_count INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_at DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS package_manifests (
    id SERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL DEFAULT '',
    is_dev INTEGER NOT NULL DEFAULT 0,
    updated_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    UNIQUE(owner, repo, name, kind, file_path)
);

CREATE INDEX IF NOT EXISTS idx_pg_pkg_manifest_name
    ON package_manifests(owner, repo, name);

CREATE TABLE IF NOT EXISTS vulnerabilities (
    id SERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    package_name TEXT NOT NULL,
    ecosystem TEXT NOT NULL,
    package_version TEXT NOT NULL DEFAULT '',
    cve_id TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'unknown',
    advisory_url TEXT NOT NULL DEFAULT '',
    fixed_in TEXT NOT NULL DEFAULT '',
    first_seen_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_seen_at DOUBLE PRECISION NOT NULL DEFAULT 0,
    UNIQUE(owner, repo, package_name, ecosystem, package_version, cve_id)
);

CREATE INDEX IF NOT EXISTS idx_pg_vuln_package
    ON vulnerabilities(owner, repo, package_name, ecosystem, package_version);
CREATE INDEX IF NOT EXISTS idx_pg_vuln_severity
    ON vulnerabilities(severity);
"""

# Module-level shared connection — initialized lazily
_pg_conn = None
_schema_initialized = False
_lock = threading.Lock()


def _get_conn(url: str):
    """Get the shared Postgres connection, initializing schema on first use."""
    global _pg_conn, _schema_initialized
    with _lock:
        if _pg_conn is None:
            import psycopg

            _pg_conn = psycopg.connect(url, autocommit=True)
        if not _schema_initialized:
            with _pg_conn.cursor() as cur:
                cur.execute(_PG_SCHEMA)
            _schema_initialized = True
        return _pg_conn


def list_learned_rules_org_wide(url: str, limit: int = 1000) -> list[dict]:
    """List active learned rules across every repo. Used by the org-wide
    learned-rules dashboard page so admins can see what Mira has synthesized
    from feedback signals across the org."""
    conn = _get_conn(url)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT owner, repo, rule_text, source_signal, category, "
            "path_pattern, sample_count, updated_at "
            "FROM learned_rules WHERE active = 1 "
            "ORDER BY sample_count DESC, updated_at DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
    return [
        {
            "owner": r[0],
            "repo": r[1],
            "rule_text": r[2],
            "source_signal": r[3],
            "category": r[4],
            "path_pattern": r[5],
            "sample_count": r[6],
            "updated_at": r[7],
        }
        for r in rows
    ]


def count_vulnerabilities_org_wide(url: str) -> dict[str, int]:
    """Count open vulnerabilities org-wide by severity, for the dashboard widget."""
    conn = _get_conn(url)
    with conn.cursor() as cur:
        cur.execute("SELECT severity, COUNT(*) FROM vulnerabilities GROUP BY severity")
        rows = cur.fetchall()
    return {r[0]: r[1] for r in rows}


def list_vulnerabilities_org_wide(url: str, limit: int = 1000) -> list[dict]:
    """List every open vulnerability across the org, ordered by severity."""
    conn = _get_conn(url)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT owner, repo, package_name, ecosystem, package_version, "
            "cve_id, summary, severity, advisory_url, fixed_in, last_seen_at "
            "FROM vulnerabilities "
            "ORDER BY CASE severity "
            "WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            "WHEN 'moderate' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, "
            "LOWER(package_name) "
            "LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
    return [
        {
            "owner": r[0],
            "repo": r[1],
            "package_name": r[2],
            "ecosystem": r[3],
            "package_version": r[4],
            "cve_id": r[5],
            "summary": r[6],
            "severity": r[7],
            "advisory_url": r[8],
            "fixed_in": r[9],
            "last_seen_at": r[10],
        }
        for r in rows
    ]


def list_packages_org_wide(url: str) -> list[dict]:
    """List every (owner, repo, ecosystem, name, version, file_path) tuple
    across the whole org. Used by the OSV poller to know what to query;
    file_path lets the poller prefer lockfile rows over manifest rows."""
    conn = _get_conn(url)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT owner, repo, kind, name, version, file_path "
            "FROM package_manifests "
            "WHERE kind IN ('npm', 'pip', 'go', 'rust') AND version <> ''"
        )
        rows = cur.fetchall()
    return [
        {
            "owner": r[0], "repo": r[1], "kind": r[2], "name": r[3],
            "version": r[4], "file_path": r[5],
        }
        for r in rows
    ]


def search_packages_org_wide(
    url: str,
    name: str | None = None,
    version: str | None = None,
    kind: str | None = None,
    is_dev: bool | None = None,
    limit: int = 500,
) -> list[dict]:
    """Search for packages across every repo in the org.

    Returns rows of {owner, repo, name, kind, version, file_path, is_dev}.
    Case-insensitive name and version match (ILIKE with implicit wildcards).
    Empty/None filters are ignored. Backs the org-wide package search page —
    used for incident response ("which repos use lodash@4.17.20 after this
    CVE?") and upgrade audits.
    """
    conn = _get_conn(url)
    clauses: list[str] = []
    params: list = []
    if name:
        clauses.append("name ILIKE %s")
        params.append(f"%{name}%")
    if version:
        clauses.append("version ILIKE %s")
        params.append(f"%{version}%")
    if kind:
        clauses.append("kind = %s")
        params.append(kind)
    if is_dev is not None:
        clauses.append("is_dev = %s")
        params.append(1 if is_dev else 0)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        "SELECT owner, repo, name, kind, version, file_path, is_dev "
        f"FROM package_manifests {where} "
        "ORDER BY LOWER(name), owner, repo "
        "LIMIT %s"
    )
    params.append(limit)

    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()

    return [
        {
            "owner": r[0],
            "repo": r[1],
            "name": r[2],
            "kind": r[3],
            "version": r[4],
            "file_path": r[5],
            "is_dev": bool(r[6]),
        }
        for r in rows
    ]


class PgIndexStore:
    """PostgreSQL-backed index store with owner/repo scoping.

    Implements the same public interface as IndexStore. Shares a single
    connection across all instances.
    """

    def __init__(self, owner: str, repo: str, url: str) -> None:
        self._owner = owner
        self._repo = repo
        self._conn = _get_conn(url)

    def _exec(self, sql: str, params: tuple = ()):
        """Execute a query and return the cursor."""
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return cur

    def _fetchone(self, sql: str, params: tuple = ()):
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def _fetchall(self, sql: str, params: tuple = ()):
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    # ── File summaries ──

    def get_summary(self, path: str) -> FileSummary | None:
        row = self._fetchone(
            "SELECT path, language, summary, content_hash, updated_at FROM files "
            "WHERE owner=%s AND repo=%s AND path=%s",
            (self._owner, self._repo, path),
        )
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
        result: dict[str, FileSummary] = {}
        for path in paths:
            s = self.get_summary(path)
            if s is not None:
                result[path] = s
        return result

    def get_dependents(self, path: str) -> list[str]:
        rows = self._fetchall(
            "SELECT source_path FROM imports WHERE owner=%s AND repo=%s AND target_path=%s",
            (self._owner, self._repo, path),
        )
        return [r[0] for r in rows]

    def upsert_summary(self, summary: FileSummary) -> None:
        now = time.time()
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO files (owner, repo, path, language, summary, content_hash, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (owner, repo, path) DO UPDATE SET "
                "language=EXCLUDED.language, summary=EXCLUDED.summary, "
                "content_hash=EXCLUDED.content_hash, updated_at=EXCLUDED.updated_at",
                (
                    self._owner,
                    self._repo,
                    summary.path,
                    summary.language,
                    summary.summary,
                    summary.content_hash,
                    now,
                ),
            )
            # Replace symbols
            cur.execute(
                "DELETE FROM symbols WHERE owner=%s AND repo=%s AND file_path=%s",
                (self._owner, self._repo, summary.path),
            )
            for sym in summary.symbols:
                cur.execute(
                    "INSERT INTO symbols (owner, repo, file_path, name, kind, signature, description) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        self._owner,
                        self._repo,
                        summary.path,
                        sym.name,
                        sym.kind,
                        sym.signature,
                        sym.description,
                    ),
                )
            # Replace imports (dedup)
            cur.execute(
                "DELETE FROM imports WHERE owner=%s AND repo=%s AND source_path=%s",
                (self._owner, self._repo, summary.path),
            )
            for target in set(summary.imports):
                cur.execute(
                    "INSERT INTO imports (owner, repo, source_path, target_path) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (self._owner, self._repo, summary.path, target),
                )
            # Replace symbol refs (dedup)
            cur.execute(
                "DELETE FROM symbol_refs WHERE owner=%s AND repo=%s AND source_path=%s",
                (self._owner, self._repo, summary.path),
            )
            for src_sym, tgt_path, tgt_sym in set(summary.symbol_refs):
                cur.execute(
                    "INSERT INTO symbol_refs (owner, repo, source_path, source_symbol, target_path, target_symbol) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (self._owner, self._repo, summary.path, src_sym, tgt_path, tgt_sym),
                )
            # Replace external refs (dedup by kind+target)
            cur.execute(
                "DELETE FROM external_refs WHERE owner=%s AND repo=%s AND file_path=%s",
                (self._owner, self._repo, summary.path),
            )
            seen_refs = set()
            for ref in summary.external_refs:
                key = (ref.kind, ref.target)
                if key in seen_refs:
                    continue
                seen_refs.add(key)
                cur.execute(
                    "INSERT INTO external_refs (owner, repo, file_path, kind, target, description) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (self._owner, self._repo, summary.path, ref.kind, ref.target, ref.description),
                )

    def upsert_batch(self, summaries: list[FileSummary]) -> None:
        for s in summaries:
            self.upsert_summary(s)

    def remove_paths(self, paths: list[str]) -> None:
        with self._conn.cursor() as cur:
            for path in paths:
                for table in ("files", "symbols", "imports", "symbol_refs", "external_refs"):
                    col = (
                        "file_path"
                        if table in ("symbols", "external_refs")
                        else ("source_path" if table in ("imports", "symbol_refs") else "path")
                    )
                    cur.execute(
                        f"DELETE FROM {table} WHERE owner=%s AND repo=%s AND {col}=%s",
                        (self._owner, self._repo, path),
                    )

    def all_paths(self) -> set[str]:
        rows = self._fetchall(
            "SELECT path FROM files WHERE owner=%s AND repo=%s",
            (self._owner, self._repo),
        )
        return {r[0] for r in rows}

    # ── Directories ──

    def get_directory_summary(self, path: str) -> DirectorySummary | None:
        row = self._fetchone(
            "SELECT path, summary, file_count, updated_at FROM directories "
            "WHERE owner=%s AND repo=%s AND path=%s",
            (self._owner, self._repo, path),
        )
        if row is None:
            return None
        return DirectorySummary(path=row[0], summary=row[1], file_count=row[2], updated_at=row[3])

    def get_directory_summaries(self, paths: list[str]) -> dict[str, DirectorySummary]:
        result: dict[str, DirectorySummary] = {}
        for path in paths:
            ds = self.get_directory_summary(path)
            if ds is not None:
                result[path] = ds
        return result

    def upsert_directory(self, summary: DirectorySummary) -> None:
        now = time.time()
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO directories (owner, repo, path, summary, file_count, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (owner, repo, path) DO UPDATE SET "
                "summary=EXCLUDED.summary, file_count=EXCLUDED.file_count, updated_at=EXCLUDED.updated_at",
                (self._owner, self._repo, summary.path, summary.summary, summary.file_count, now),
            )

    # ── Call graph / blast radius ──

    def get_call_graph(self, path: str, symbol: str) -> list[tuple[str, str]]:
        rows = self._fetchall(
            "SELECT source_path, source_symbol FROM symbol_refs "
            "WHERE owner=%s AND repo=%s AND target_path=%s AND target_symbol=%s",
            (self._owner, self._repo, path, symbol),
        )
        return [(r[0], r[1]) for r in rows]

    def get_reverse_deps(self, path: str, max_depth: int = 3) -> list[str]:
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
        """Count how many other files reference each path via symbol_refs or imports."""
        counts: dict[str, int] = {}
        with self._conn.cursor() as cur:
            for path in paths:
                cur.execute(
                    "SELECT COUNT(*) FROM imports WHERE owner=%s AND repo=%s AND target_path=%s",
                    (self._owner, self._repo, path),
                )
                import_count = cur.fetchone()[0]
                cur.execute(
                    "SELECT COUNT(DISTINCT source_path) FROM symbol_refs "
                    "WHERE owner=%s AND repo=%s AND target_path=%s",
                    (self._owner, self._repo, path),
                )
                ref_count = cur.fetchone()[0]
                counts[path] = import_count + ref_count
        return counts

    def get_blast_radius(self, changed_paths: list[str]) -> list[BlastRadiusEntry]:
        entries: dict[str, BlastRadiusEntry] = {}

        for changed_path in changed_paths:
            symbols = self._load_symbols(changed_path)
            for sym in symbols:
                callers = self.get_call_graph(changed_path, sym.name)
                for caller_path, caller_symbol in callers:
                    if caller_path in changed_paths:
                        continue
                    if caller_path not in entries:
                        row = self._fetchone(
                            "SELECT summary FROM files WHERE owner=%s AND repo=%s AND path=%s",
                            (self._owner, self._repo, caller_path),
                        )
                        summary = row[0] if row else ""
                        entries[caller_path] = BlastRadiusEntry(
                            path=caller_path,
                            summary=summary,
                            affected_symbols=[],
                            depth=1,
                        )
                    entry = entries[caller_path]
                    if caller_symbol not in entry.affected_symbols:
                        entry.affected_symbols.append(caller_symbol)

        depth1_paths = list(entries.keys())
        for d1_path in depth1_paths:
            d1_entry = entries[d1_path]
            for affected_sym in list(d1_entry.affected_symbols):
                callers = self.get_call_graph(d1_path, affected_sym)
                for caller_path, caller_symbol in callers:
                    if caller_path in changed_paths or caller_path in depth1_paths:
                        continue
                    if caller_path not in entries:
                        row = self._fetchone(
                            "SELECT summary FROM files WHERE owner=%s AND repo=%s AND path=%s",
                            (self._owner, self._repo, caller_path),
                        )
                        summary = row[0] if row else ""
                        entries[caller_path] = BlastRadiusEntry(
                            path=caller_path,
                            summary=summary,
                            affected_symbols=[],
                            depth=2,
                        )
                    entry = entries[caller_path]
                    if caller_symbol not in entry.affected_symbols:
                        entry.affected_symbols.append(caller_symbol)

        return sorted(entries.values(), key=lambda e: (e.depth, e.path))

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
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO review_events (owner, repo, pr_number, pr_title, pr_url, "
                "comments_posted, blockers, warnings, suggestions, files_reviewed, "
                "lines_changed, tokens_used, duration_ms, categories, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (
                    self._owner,
                    self._repo,
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
            row_id = cur.fetchone()[0]
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
        rows = self._fetchall(
            "SELECT id, pr_number, pr_title, pr_url, comments_posted, blockers, warnings, "
            "suggestions, files_reviewed, lines_changed, tokens_used, duration_ms, "
            "categories, created_at "
            "FROM review_events WHERE owner=%s AND repo=%s "
            "ORDER BY created_at DESC LIMIT %s",
            (self._owner, self._repo, limit),
        )
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
        params: list = [self._owner, self._repo]
        since_clause = ""
        if since is not None:
            since_clause = " AND created_at >= %s"
            params.append(since)

        row = self._fetchone(
            "SELECT COUNT(*), COALESCE(SUM(comments_posted),0), COALESCE(SUM(blockers),0), "
            "COALESCE(SUM(warnings),0), COALESCE(SUM(suggestions),0), "
            "COALESCE(SUM(files_reviewed),0), COALESCE(SUM(lines_changed),0), "
            "COALESCE(SUM(tokens_used),0), COALESCE(AVG(duration_ms),0) "
            f"FROM review_events WHERE owner=%s AND repo=%s{since_clause}",
            tuple(params),
        )

        cat_params: list = [self._owner, self._repo]
        if since is not None:
            cat_params.append(since)
        cat_rows = self._fetchall(
            "SELECT categories FROM review_events "
            f"WHERE owner=%s AND repo=%s AND categories != ''{since_clause}",
            tuple(cat_params),
        )
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

    # ── Review context ──

    def list_review_context(self) -> list[ReviewContext]:
        rows = self._fetchall(
            "SELECT id, title, content, created_at, updated_at FROM review_context "
            "WHERE owner=%s AND repo=%s ORDER BY updated_at DESC",
            (self._owner, self._repo),
        )
        return [
            ReviewContext(id=r[0], title=r[1], content=r[2], created_at=r[3], updated_at=r[4])
            for r in rows
        ]

    def get_review_context(self, context_id: int) -> ReviewContext | None:
        row = self._fetchone(
            "SELECT id, title, content, created_at, updated_at FROM review_context "
            "WHERE owner=%s AND repo=%s AND id=%s",
            (self._owner, self._repo, context_id),
        )
        if row is None:
            return None
        return ReviewContext(
            id=row[0], title=row[1], content=row[2], created_at=row[3], updated_at=row[4]
        )

    def upsert_review_context(
        self, title: str, content: str, context_id: int | None = None
    ) -> ReviewContext:
        now = time.time()
        with self._conn.cursor() as cur:
            if context_id is not None:
                cur.execute(
                    "UPDATE review_context SET title=%s, content=%s, updated_at=%s "
                    "WHERE owner=%s AND repo=%s AND id=%s",
                    (title, content, now, self._owner, self._repo, context_id),
                )
            else:
                cur.execute(
                    "INSERT INTO review_context (owner, repo, title, content, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (self._owner, self._repo, title, content, now, now),
                )
                context_id = cur.fetchone()[0]
        return self.get_review_context(context_id)  # type: ignore[return-value]

    def delete_review_context(self, context_id: int) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM review_context WHERE owner=%s AND repo=%s AND id=%s",
                (self._owner, self._repo, context_id),
            )

    def get_all_review_context_text(self) -> str:
        entries = self.list_review_context()
        if not entries:
            return ""
        parts = ["## Repository Documentation Context\n"]
        for entry in entries:
            parts.append(f"### {entry.title}\n{entry.content}\n")
        return "\n".join(parts)

    # ── External refs ──

    def get_external_refs_for_paths(self, paths: list[str]) -> list[ExternalRef]:
        result: list[ExternalRef] = []
        for path in paths:
            result.extend(self._load_external_refs(path))
        return result

    def get_files_referencing(self, target: str) -> list[ExternalRef]:
        rows = self._fetchall(
            "SELECT file_path, kind, target, description FROM external_refs "
            "WHERE owner=%s AND repo=%s AND target LIKE %s",
            (self._owner, self._repo, f"%{target}%"),
        )
        return [ExternalRef(file_path=r[0], kind=r[1], target=r[2], description=r[3]) for r in rows]

    def get_all_external_targets(self) -> list[str]:
        rows = self._fetchall(
            "SELECT DISTINCT target FROM external_refs WHERE owner=%s AND repo=%s",
            (self._owner, self._repo),
        )
        return [r[0] for r in rows]

    def _load_external_refs(self, path: str) -> list[ExternalRef]:
        rows = self._fetchall(
            "SELECT file_path, kind, target, description FROM external_refs "
            "WHERE owner=%s AND repo=%s AND file_path=%s",
            (self._owner, self._repo, path),
        )
        return [ExternalRef(file_path=r[0], kind=r[1], target=r[2], description=r[3]) for r in rows]

    def _load_symbols(self, path: str) -> list[SymbolInfo]:
        rows = self._fetchall(
            "SELECT name, kind, signature, description FROM symbols "
            "WHERE owner=%s AND repo=%s AND file_path=%s",
            (self._owner, self._repo, path),
        )
        return [SymbolInfo(name=r[0], kind=r[1], signature=r[2], description=r[3]) for r in rows]

    def _load_imports(self, path: str) -> list[str]:
        rows = self._fetchall(
            "SELECT target_path FROM imports WHERE owner=%s AND repo=%s AND source_path=%s",
            (self._owner, self._repo, path),
        )
        return [r[0] for r in rows]

    def _load_symbol_refs(self, path: str) -> list[tuple[str, str, str]]:
        rows = self._fetchall(
            "SELECT source_symbol, target_path, target_symbol FROM symbol_refs "
            "WHERE owner=%s AND repo=%s AND source_path=%s",
            (self._owner, self._repo, path),
        )
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
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO feedback_events "
                "(owner, repo, pr_number, pr_url, comment_path, comment_line, "
                "comment_category, comment_severity, comment_title, signal, actor, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (
                    self._owner,
                    self._repo,
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
            row_id = cur.fetchone()[0]
        self._conn.commit()
        return FeedbackEventRow(
            id=row_id,
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
                self._owner,
                self._repo,
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
        with self._conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO feedback_events "
                "(owner, repo, pr_number, pr_url, comment_path, comment_line, "
                "comment_category, comment_severity, comment_title, signal, actor, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                rows,
            )
        self._conn.commit()
        return len(rows)

    def list_feedback(self, limit: int = 500) -> list[FeedbackEventRow]:
        rows = self._fetchall(
            "SELECT id, pr_number, pr_url, comment_path, comment_line, "
            "comment_category, comment_severity, comment_title, signal, actor, created_at "
            "FROM feedback_events WHERE owner=%s AND repo=%s "
            "ORDER BY created_at DESC LIMIT %s",
            (self._owner, self._repo, limit),
        )
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
        rows = self._fetchall(
            "SELECT signal, comment_category, comment_path, COUNT(*) "
            "FROM feedback_events WHERE owner=%s AND repo=%s "
            "GROUP BY signal, comment_category, comment_path",
            (self._owner, self._repo),
        )
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
        existing = self._fetchone(
            "SELECT id FROM learned_rules WHERE owner=%s AND repo=%s "
            "AND category=%s AND path_pattern=%s",
            (self._owner, self._repo, category, path_pattern),
        )
        if existing:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE learned_rules SET rule_text=%s, source_signal=%s, "
                    "sample_count=%s, updated_at=%s WHERE id=%s",
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
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO learned_rules "
                "(owner, repo, rule_text, source_signal, category, path_pattern, "
                "sample_count, active, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s) RETURNING id",
                (
                    self._owner,
                    self._repo,
                    rule_text,
                    source_signal,
                    category,
                    path_pattern,
                    sample_count,
                    now,
                    now,
                ),
            )
            row_id = cur.fetchone()[0]
        self._conn.commit()
        return LearnedRuleRow(
            id=row_id,
            rule_text=rule_text,
            source_signal=source_signal,
            category=category,
            path_pattern=path_pattern,
            sample_count=sample_count,
            created_at=now,
            updated_at=now,
        )

    def list_active_learned_rules(self) -> list[LearnedRuleRow]:
        rows = self._fetchall(
            "SELECT id, rule_text, source_signal, category, path_pattern, "
            "sample_count, active, created_at, updated_at "
            "FROM learned_rules WHERE owner=%s AND repo=%s AND active=1 "
            "ORDER BY sample_count DESC",
            (self._owner, self._repo),
        )
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
        rules = self.list_active_learned_rules()
        return [r.rule_text for r in rules[:10]]

    # ── Package manifests ──

    def replace_manifest_packages(
        self,
        file_path: str,
        packages: list[dict],
    ) -> int:
        from mira.index.store import PackageManifestRow  # noqa: F401

        now = time.time()
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM package_manifests WHERE owner=%s AND repo=%s AND file_path=%s",
                (self._owner, self._repo, file_path),
            )
            if packages:
                rows = [
                    (
                        self._owner,
                        self._repo,
                        p["name"],
                        p["kind"],
                        p["version"],
                        p["file_path"],
                        1 if p.get("is_dev") else 0,
                        now,
                    )
                    for p in packages
                ]
                cur.executemany(
                    "INSERT INTO package_manifests "
                    "(owner, repo, name, kind, version, file_path, is_dev, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (owner, repo, name, kind, file_path) DO UPDATE SET "
                    "version=EXCLUDED.version, is_dev=EXCLUDED.is_dev, "
                    "updated_at=EXCLUDED.updated_at",
                    rows,
                )
        self._conn.commit()
        return len(packages)

    def list_manifest_packages(self):  # type: ignore[no-untyped-def]
        from mira.index.store import PackageManifestRow

        rows = self._fetchall(
            "SELECT id, name, kind, version, file_path, is_dev, updated_at "
            "FROM package_manifests WHERE owner=%s AND repo=%s "
            "ORDER BY LOWER(name)",
            (self._owner, self._repo),
        )
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
        rows = self._fetchall(
            "SELECT DISTINCT file_path FROM package_manifests WHERE owner=%s AND repo=%s",
            (self._owner, self._repo),
        )
        existing = {r[0] for r in rows}
        stale = existing - live_paths
        if not stale:
            return 0
        with self._conn.cursor() as cur:
            cur.executemany(
                "DELETE FROM package_manifests WHERE owner=%s AND repo=%s AND file_path=%s",
                [(self._owner, self._repo, p) for p in stale],
            )
        self._conn.commit()
        return len(stale)

    # ── Vulnerabilities ──

    def replace_vulnerabilities_for_package(
        self,
        package_name: str,
        ecosystem: str,
        package_version: str,
        vulns: list[dict],
    ) -> int:
        now = time.time()
        existing = {
            r[0]: r[1]
            for r in self._fetchall(
                "SELECT cve_id, first_seen_at FROM vulnerabilities "
                "WHERE owner=%s AND repo=%s AND package_name=%s "
                "AND ecosystem=%s AND package_version=%s",
                (self._owner, self._repo, package_name, ecosystem, package_version),
            )
        }
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM vulnerabilities "
                "WHERE owner=%s AND repo=%s AND package_name=%s "
                "AND ecosystem=%s AND package_version=%s",
                (self._owner, self._repo, package_name, ecosystem, package_version),
            )
            if vulns:
                rows = [
                    (
                        self._owner,
                        self._repo,
                        package_name,
                        ecosystem,
                        package_version,
                        v["cve_id"],
                        v.get("summary", ""),
                        v.get("severity", "unknown"),
                        v.get("advisory_url", ""),
                        v.get("fixed_in", ""),
                        existing.get(v["cve_id"], now),
                        now,
                    )
                    for v in vulns
                ]
                cur.executemany(
                    "INSERT INTO vulnerabilities "
                    "(owner, repo, package_name, ecosystem, package_version, "
                    "cve_id, summary, severity, advisory_url, fixed_in, "
                    "first_seen_at, last_seen_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    rows,
                )
        self._conn.commit()
        return len(vulns)

    def prune_stale_vulnerabilities(
        self, active_keys: set[tuple[str, str, str]]
    ) -> int:
        """Delete vulnerability rows whose (name, ecosystem, version) tuple
        is no longer in this repo's dependency set.

        Called by the OSV poller before each scan so that, e.g., when a
        manifest constraint (`>=1.30`) is replaced by a lockfile resolution
        (`1.81.10`), the stale `1.30` advisories don't linger in the UI.
        """
        rows = self._fetchall(
            "SELECT DISTINCT package_name, ecosystem, package_version "
            "FROM vulnerabilities WHERE owner=%s AND repo=%s",
            (self._owner, self._repo),
        )
        stale = [(n, e, v) for n, e, v in rows if (n, e, v) not in active_keys]
        if not stale:
            return 0
        with self._conn.cursor() as cur:
            cur.executemany(
                "DELETE FROM vulnerabilities WHERE owner=%s AND repo=%s "
                "AND package_name=%s AND ecosystem=%s AND package_version=%s",
                [(self._owner, self._repo, *k) for k in stale],
            )
        self._conn.commit()
        return len(stale)

    def list_vulnerabilities(self):  # type: ignore[no-untyped-def]
        from mira.index.store import VulnerabilityRow

        rows = self._fetchall(
            "SELECT id, package_name, ecosystem, package_version, cve_id, "
            "summary, severity, advisory_url, fixed_in, first_seen_at, last_seen_at "
            "FROM vulnerabilities WHERE owner=%s AND repo=%s "
            "ORDER BY CASE severity "
            "WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
            "WHEN 'moderate' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, "
            "LOWER(package_name)",
            (self._owner, self._repo),
        )
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
        rows = self._fetchall(
            "SELECT severity, COUNT(*) FROM vulnerabilities "
            "WHERE owner=%s AND repo=%s GROUP BY severity",
            (self._owner, self._repo),
        )
        return {r[0]: r[1] for r in rows}

    def close(self) -> None:
        """No-op — connection is shared across all instances."""
