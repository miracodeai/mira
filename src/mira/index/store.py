"""SQLite-backed storage for file summaries. One DB per repo."""

from __future__ import annotations

import json
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
    symbol_refs: list[tuple[str, str, str]] = field(default_factory=list)  # (source_symbol, target_path, target_symbol)
    content_hash: str = ""
    updated_at: float = 0.0


@dataclass
class DirectorySummary:
    path: str
    summary: str
    file_count: int
    updated_at: float = 0.0


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
    def open(cls, owner: str, repo: str) -> IndexStore:
        """Open (or create) the index DB for a repo."""
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
                "INSERT INTO symbols (file_path, name, kind, signature, description) VALUES (?, ?, ?, ?, ?)",
                (summary.path, sym.name, sym.kind, sym.signature, sym.description),
            )
        # Replace imports
        self._conn.execute("DELETE FROM imports WHERE source_path = ?", (summary.path,))
        for target in summary.imports:
            self._conn.execute(
                "INSERT INTO imports (source_path, target_path) VALUES (?, ?)",
                (summary.path, target),
            )
        # Replace symbol refs
        self._conn.execute("DELETE FROM symbol_refs WHERE source_path = ?", (summary.path,))
        for src_sym, tgt_path, tgt_sym in summary.symbol_refs:
            self._conn.execute(
                "INSERT INTO symbol_refs (source_path, source_symbol, target_path, target_symbol) VALUES (?, ?, ?, ?)",
                (summary.path, src_sym, tgt_path, tgt_sym),
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
            "SELECT source_path, source_symbol FROM symbol_refs WHERE target_path = ? AND target_symbol = ?",
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
            "SELECT source_symbol, target_path, target_symbol FROM symbol_refs WHERE source_path = ?",
            (path,),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]
