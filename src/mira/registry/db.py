"""One org-wide registry database: Postgres when ``DATABASE_URL`` is set,
otherwise ``$MIRA_INDEX_DIR/registry.db``.

SQL is written once with ``?`` placeholders and portable types; the Postgres
path rewrites placeholders to ``%s``.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS registry_objects (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        summary TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        scope TEXT NOT NULL DEFAULT 'repo',
        repo TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'proposed',
        status_reason TEXT NOT NULL DEFAULT '',
        realization TEXT NOT NULL DEFAULT 'stated',
        confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
        author_kind TEXT NOT NULL DEFAULT 'agent',
        author TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        decided_by TEXT NOT NULL DEFAULT '',
        decided_at DOUBLE PRECISION NOT NULL DEFAULT 0,
        ticket TEXT NOT NULL DEFAULT '',
        session TEXT NOT NULL DEFAULT '',
        branch TEXT NOT NULL DEFAULT '',
        pr TEXT NOT NULL DEFAULT '',
        merge_commit TEXT NOT NULL DEFAULT '',
        source_url TEXT NOT NULL DEFAULT '',
        basis TEXT NOT NULL DEFAULT 'statement',
        rationale TEXT NOT NULL DEFAULT '',
        alternatives TEXT NOT NULL DEFAULT '',
        conflict_check TEXT NOT NULL DEFAULT 'none',
        goal TEXT NOT NULL DEFAULT '',
        work_scope TEXT NOT NULL DEFAULT '',
        plan TEXT NOT NULL DEFAULT '',
        owner TEXT NOT NULL DEFAULT '',
        delivery_state TEXT NOT NULL DEFAULT '',
        tested_revision TEXT NOT NULL DEFAULT '',
        method TEXT NOT NULL DEFAULT '',
        result TEXT NOT NULL DEFAULT '',
        artifact_url TEXT NOT NULL DEFAULT '',
        source_system TEXT NOT NULL DEFAULT '',
        source_id TEXT NOT NULL DEFAULT '',
        source_version BIGINT NOT NULL DEFAULT 0,
        sync_status TEXT NOT NULL DEFAULT '',
        synced_at DOUBLE PRECISION NOT NULL DEFAULT 0,
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0,
        updated_at DOUBLE PRECISION NOT NULL DEFAULT 0,
        restored_at DOUBLE PRECISION NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS registry_objects_branch ON registry_objects (repo, branch)",
    "CREATE INDEX IF NOT EXISTS registry_objects_status ON registry_objects (kind, status)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS registry_objects_source
    ON registry_objects (source_system, source_id)
    WHERE source_system <> ''
    """,
    """
    CREATE TABLE IF NOT EXISTS registry_relationships (
        id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        target_id TEXT NOT NULL,
        type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        relation TEXT NOT NULL DEFAULT '',
        new_clause TEXT NOT NULL DEFAULT '',
        existing_clause TEXT NOT NULL DEFAULT '',
        origin TEXT NOT NULL DEFAULT '',
        created_by TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0,
        resolved_by TEXT NOT NULL DEFAULT '',
        resolved_at DOUBLE PRECISION NOT NULL DEFAULT 0,
        UNIQUE (source_id, target_id, type)
    )
    """,
    "CREATE INDEX IF NOT EXISTS registry_rel_target ON registry_relationships (target_id, type)",
    """
    CREATE TABLE IF NOT EXISTS registry_anchors (
        object_id TEXT NOT NULL,
        repo TEXT NOT NULL,
        path TEXT NOT NULL,
        qualified_name TEXT NOT NULL DEFAULT '',
        symbol_kind TEXT NOT NULL DEFAULT '',
        anchor_id TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0,
        PRIMARY KEY (object_id, repo, path, qualified_name)
    )
    """,
    "CREATE INDEX IF NOT EXISTS registry_anchors_path ON registry_anchors (repo, path)",
    """
    CREATE TABLE IF NOT EXISTS registry_conflict_answers (
        object_id TEXT NOT NULL,
        session TEXT NOT NULL,
        choice TEXT NOT NULL,
        answered_by TEXT NOT NULL DEFAULT '',
        answered_at DOUBLE PRECISION NOT NULL DEFAULT 0,
        PRIMARY KEY (object_id, session)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS registry_conflict_prompts (
        id TEXT PRIMARY KEY,
        session TEXT NOT NULL DEFAULT '',
        object_id TEXT NOT NULL DEFAULT '',
        candidate_id TEXT NOT NULL DEFAULT '',
        created_at DOUBLE PRECISION NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS registry_conflict_prompts_session "
    "ON registry_conflict_prompts (session)",
    # Comments mirrored from a source system (HAI-11606). ``parent_source_id`` is
    # the source id of the object the comment sits on, so a comment can arrive
    # before its parent and still attach once the parent is imported.
    """
    CREATE TABLE IF NOT EXISTS registry_comments (
        id TEXT PRIMARY KEY,
        source_system TEXT NOT NULL,
        source_id TEXT NOT NULL,
        parent_source_id TEXT NOT NULL,
        author TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        source_url TEXT NOT NULL DEFAULT '',
        source_version BIGINT NOT NULL DEFAULT 0,
        source_created_at DOUBLE PRECISION NOT NULL DEFAULT 0,
        archived BIGINT NOT NULL DEFAULT 0,
        synced_at DOUBLE PRECISION NOT NULL DEFAULT 0,
        UNIQUE (source_system, source_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS registry_comments_parent "
    "ON registry_comments (source_system, parent_source_id)",
    # Resumable import walks: one row per (source system, stream).
    """
    CREATE TABLE IF NOT EXISTS registry_sync_state (
        source_system TEXT NOT NULL,
        stream TEXT NOT NULL,
        watermark DOUBLE PRECISION NOT NULL DEFAULT 0,
        walk_since DOUBLE PRECISION NOT NULL DEFAULT 0,
        walk_started_at DOUBLE PRECISION NOT NULL DEFAULT 0,
        after_cursor TEXT NOT NULL DEFAULT '',
        updated_at DOUBLE PRECISION NOT NULL DEFAULT 0,
        PRIMARY KEY (source_system, stream)
    )
    """,
]


class RegistryDB:
    """Thread-safe connection wrapper. Rows come back as tuples."""

    def __init__(self, *, url: str | None = None, path: str | None = None) -> None:
        self._lock = threading.RLock()
        self._depth = 0
        self.postgres = bool(url)
        self._url = url
        if url:
            from mira.db.postgres import connect

            self._conn: Any = connect(url)
        else:
            assert path is not None
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self._conn.execute("PRAGMA journal_mode=WAL")
        for stmt in SCHEMA:
            self.execute(stmt)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.postgres else sql

    def execute(self, sql: str, params: tuple | list = ()) -> list[tuple]:
        with self._lock:
            if self.postgres and self._depth:
                # Inside a transaction a reconnect would silently drop the
                # statements already run, so a dropped connection raises instead.
                with self._conn.cursor() as cur:
                    cur.execute(self._sql(sql), tuple(params))
                    return [tuple(r) for r in cur.fetchall()] if cur.description else []
            if self.postgres:
                from mira.db.postgres import ReconnectingCursor, reconnect

                def _reopen() -> Any:
                    self._conn = reconnect(self._url or "", self._conn)
                    return self._conn

                with ReconnectingCursor(self._conn.cursor(), on_reconnect=_reopen) as cur:
                    cur.execute(self._sql(sql), tuple(params))
                    return [tuple(r) for r in cur.fetchall()] if cur.description else []
            cur = self._conn.execute(sql, tuple(params))
            return [tuple(r) for r in cur.fetchall()]

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run the enclosed statements atomically. Nested calls join the outer one.

        Holds the connection lock for the whole block, so other threads wait.
        """
        with self._lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            if self.postgres:
                with self._conn.transaction():
                    self._depth = 1
                    try:
                        yield
                    finally:
                        self._depth = 0
                return
            self._conn.execute("BEGIN IMMEDIATE")
            self._depth = 1
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
            finally:
                self._depth = 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def default_location() -> tuple[str | None, str]:
    """Return ``(postgres_url, sqlite_path)`` for this deployment."""
    db_url = os.environ.get("DATABASE_URL", "")
    url = db_url if db_url.startswith(("postgresql://", "postgres://")) else None
    index_dir = os.environ.get("MIRA_INDEX_DIR", "/data/indexes")
    return url, os.path.join(index_dir, "registry.db")


def is_unique_violation(exc: BaseException) -> bool:
    """True when ``exc`` is a unique-constraint failure on either backend."""
    if isinstance(exc, sqlite3.IntegrityError):
        return "UNIQUE" in str(exc)
    try:
        from psycopg.errors import UniqueViolation
    except ImportError:  # pragma: no cover - psycopg is a dependency
        return False
    return isinstance(exc, UniqueViolation)
