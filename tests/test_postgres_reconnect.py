"""Regression tests for stale Postgres connection recovery.

These encode the production failure mode on upstream main: long-lived psycopg
handles in AppDatabase and pg_store go dead after idle, and the next real query
raises OperationalError until the process restarts. A correct fix reconnects
transparently on that error without adding per-query liveness probes.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import psycopg

from mira.dashboard.db import AppDatabase
from mira.index import pg_store


class _FakeCursor:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        if self._conn.dead:
            raise psycopg.OperationalError("the connection is closed")
        if "FROM users" in sql and "password_hash" in sql:
            self._conn.last_row = (1, "admin", True, "dark")
        elif "FROM files" in sql and "path" in sql:
            self._conn.last_row = ("warmup.py", "py", "summary", "hash", 1, 0.0)
        elif "COUNT(*)" in sql and "is_admin" in sql:
            self._conn.last_row = (1,)
        else:
            self._conn.last_row = None

    def fetchone(self):
        return self._conn.last_row

    def fetchall(self) -> list:
        return []

    def executemany(self, sql: str, params_seq: list) -> None:
        if self._conn.dead:
            raise psycopg.OperationalError("the connection is closed")

    def close(self) -> None:
        return None


class _FakeConnection:
    def __init__(self, conn_id: int = 1) -> None:
        self.conn_id = conn_id
        self.dead = False
        self.autocommit = False
        self.last_row = None
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def kill(self) -> None:
        """Simulate server-side idle timeout closing the socket."""
        self.dead = True

    def close(self) -> None:
        self.dead = True
        self.closed = True

    def commit(self) -> None:
        if self.dead:
            raise psycopg.OperationalError("the connection is closed")


@contextmanager
def _fake_connections():
    connections: list[_FakeConnection] = []

    def factory(url: str) -> _FakeConnection:
        conn = _FakeConnection(len(connections) + 1)
        connections.append(conn)
        return conn

    with patch("mira.db.postgres.connect", side_effect=factory):
        yield connections


def _reset_pg_store() -> None:
    pg_store._drop_pg_conn()
    pg_store._schema_initialized = False


def test_pg_store_recovers_after_idle_drop_without_restart() -> None:
    """Core production bug: indexing must survive a dead pooled connection."""
    _reset_pg_store()
    with _fake_connections() as connections:
        store = pg_store.PgIndexStore("owner", "repo", "postgresql://example/db")
        assert store.get_summary("warmup.py") is not None

        connections[0].kill()
        summary = store.get_summary("warmup.py")

    assert summary is not None
    assert len(connections) == 2
    assert connections[1].conn_id == 2


def test_app_database_recovers_after_idle_drop_without_restart() -> None:
    """Dashboard auth must survive a dead pooled connection."""
    with _fake_connections() as connections:
        with patch.object(AppDatabase, "_ensure_default_admin", lambda self: None):
            db = AppDatabase("postgresql://example/db", admin_password="admin")

        connections[0].kill()
        user = db.authenticate("admin", "admin")

    assert user is not None
    assert user.username == "admin"
    assert len(connections) == 2


def test_org_wide_query_recovers_after_idle_drop() -> None:
    """Background pollers using pg_store module helpers must also recover."""
    _reset_pg_store()
    with _fake_connections() as connections:
        pg_store._get_conn("postgresql://example/db")
        connections[0].kill()
        result = pg_store.list_packages_org_wide("postgresql://example/db")

    assert result == []
    assert len(connections) == 2


def test_bulk_write_recovers_after_idle_drop() -> None:
    """Bulk writes via executemany must reconnect, not only single execute paths."""
    _reset_pg_store()
    with _fake_connections() as connections:
        store = pg_store.PgIndexStore("owner", "repo", "postgresql://example/db")
        store.record_bulk_feedback(
            [
                {
                    "pr_number": 1,
                    "pr_url": "https://example/pr/1",
                    "comment_path": "a.py",
                    "comment_line": 1,
                    "comment_category": "bug",
                    "comment_severity": "high",
                    "comment_title": "t",
                    "signal": "reject",
                    "actor": "alice",
                }
            ]
        )
        connections[0].kill()
        count = store.record_bulk_feedback(
            [
                {
                    "pr_number": 2,
                    "pr_url": "https://example/pr/2",
                    "comment_path": "b.py",
                    "comment_line": 2,
                    "comment_category": "bug",
                    "comment_severity": "high",
                    "comment_title": "t2",
                    "signal": "reject",
                    "actor": "bob",
                }
            ]
        )

    assert count == 1
    assert len(connections) == 2
    assert connections[1].conn_id == 2


def test_reconnect_does_not_add_liveness_probes() -> None:
    """Fix must not add SELECT 1 (or similar) before every query — main never did."""
    _reset_pg_store()
    executed: list[str] = []

    class _LoggingCursor(_FakeCursor):
        def execute(self, sql: str, params: tuple = ()) -> None:
            executed.append(sql.strip())
            super().execute(sql, params)

    class _LoggingConnection(_FakeConnection):
        def cursor(self) -> _LoggingCursor:
            return _LoggingCursor(self)

    def factory(url: str) -> _LoggingConnection:
        return _LoggingConnection(1)

    with patch("mira.db.postgres.connect", side_effect=factory):
        store = pg_store.PgIndexStore("owner", "repo", "postgresql://example/db")
        store.get_summary("warmup.py")

        with patch.object(AppDatabase, "_ensure_default_admin", lambda self: None):
            db = AppDatabase("postgresql://example/db", admin_password="admin")
        db.authenticate("admin", "admin")

    assert "SELECT 1" not in executed


def test_refresh_conn_replaces_stale_handle() -> None:
    """reconnect() helper closes the old handle and returns a new one."""
    with _fake_connections() as connections:
        import mira.db.postgres as pg

        conn = pg.connect("postgresql://example/db")
        conn.kill()
        live = pg.reconnect("postgresql://example/db", conn)

    assert live.conn_id == 2
    assert live is not connections[0]
    assert connections[0].closed
