"""Optional real-PostgreSQL checks for complete, bounded review-history streams."""

from __future__ import annotations

import math
import os
from contextlib import nullcontext
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from mira.index import pg_store
from mira.index.pg_store import PgIndexStore
from mira.index.store import IndexStore


@pytest.fixture
def postgres_history(monkeypatch):
    url = os.environ.get("MIRA_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set MIRA_TEST_POSTGRES_URL to a disposable PostgreSQL database")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = "history_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped_url = make_conninfo(url, options=f"-c search_path={schema}")
    monkeypatch.setattr(pg_store, "_pg_conn", None)
    monkeypatch.setattr(pg_store, "_schema_initialized", False)
    try:
        yield scoped_url
    finally:
        pg_store._drop_pg_conn()
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _record_history(store, count=513):
    for number in range(1, count + 1):
        store.record_review(
            number,
            f"Synthetic review {number}",
            "",
            comments_posted=number * 2,
            blockers=number,
            warnings=number * 3,
            suggestions=number * 4,
            files_reviewed=number * 5,
            lines_changed=number * 6,
            tokens_used=number * 7,
            duration_ms=number * 8,
            categories=" bug,security,bug,,\u00a0style\u00a0 ",
            created_at=number // 2,
            author="synthetic-author",
            author_avatar_url="https://example.invalid/avatar",
            reviewed_paths='["synthetic.py"]',
        )


def test_postgres_history_matches_sqlite_and_is_repo_scoped(postgres_history, tmp_path):
    pg = PgIndexStore("example", "app", postgres_history)
    assert type(pg) is PgIndexStore
    assert pg_store._get_conn(postgres_history).info.server_version > 0
    sqlite = IndexStore(str(tmp_path / "history.db"))
    try:
        _record_history(pg)
        _record_history(sqlite)
        other_repo = PgIndexStore("example", "other", postgres_history)
        other_platform = PgIndexStore("_gitlab/example", "app", postgres_history)
        _record_history(other_repo, 1)
        _record_history(other_platform, 1)

        with pg.iter_review_events() as pg_events, sqlite.iter_review_events() as sqlite_events:
            actual = [asdict(event) for event in pg_events]
            expected = [asdict(event) for event in sqlite_events]
        assert len(actual) == 513
        assert actual == expected
    finally:
        sqlite.close()


@pytest.mark.parametrize("exit_kind", ["exhausted", "early", "consumer-error", "iteration-error"])
def test_postgres_server_cursor_is_bounded_isolated_and_closed(
    postgres_history, monkeypatch, exit_kind
):
    import psycopg

    from mira.db import postgres

    store = PgIndexStore("example", "app", postgres_history)
    _record_history(store)
    shared = pg_store._get_conn(postgres_history)
    connections = []
    cursors = []
    real_connect = postgres.connect

    def connect(url):
        conn = real_connect(url)
        connections.append(conn)

        def server_cursor(*args, **kwargs):
            cursor = psycopg.ServerCursor(*args, **kwargs)
            cursors.append(cursor)
            return cursor

        conn.server_cursor_factory = server_cursor
        return conn

    monkeypatch.setattr(postgres, "connect", connect)
    expected_error = (
        pytest.raises(RuntimeError, match="consumer failed")
        if exit_kind == "consumer-error"
        else pytest.raises(psycopg.errors.InvalidCursorName)
        if exit_kind == "iteration-error"
        else nullcontext()
    )
    with expected_error, store.iter_review_events() as events:
        conn = connections[0]
        cursor = cursors[0]
        assert conn is not shared
        assert cursor.itersize == 128
        assert conn.execute("SHOW transaction_read_only").fetchone() == ("on",)
        assert conn.execute("SELECT name FROM pg_cursors").fetchall() == [("mira_review_history",)]
        assert next(events).pr_number == 512
        assert cursor.pgresult.ntuples == 128
        if exit_kind == "consumer-error":
            raise RuntimeError("consumer failed")
        if exit_kind == "iteration-error":
            conn.execute("CLOSE mira_review_history")
        if exit_kind in {"exhausted", "iteration-error"}:
            count = 1
            for _event in events:
                count += 1
                assert cursor.pgresult.ntuples <= 128
            assert count == 513
    assert len(connections) == len(cursors) == 1
    assert connections[0].closed
    assert cursors[0].closed
    assert not shared.closed
    assert shared.execute("SELECT 1").fetchone() == (1,)


def test_postgres_query_failure_closes_dedicated_resources(postgres_history, monkeypatch):
    import psycopg

    from mira.db import postgres

    store = PgIndexStore("example", "app", postgres_history)
    connections = []
    real_connect = postgres.connect

    def connect(url):
        conn = real_connect(url)
        connections.append(conn)
        conn.execute("SET search_path TO missing_history_schema")
        return conn

    monkeypatch.setattr(postgres, "connect", connect)
    with pytest.raises(psycopg.errors.UndefinedTable), store.iter_review_events():
        pytest.fail("Query failure must propagate")
    assert len(connections) == 1
    assert connections[0].closed
    assert pg_store._get_conn(postgres_history).execute("SELECT 1").fetchone() == (1,)


@pytest.mark.parametrize("period", ["day", "week", "month"])
def test_postgres_timeseries_matches_python_sqlite_oracle(
    postgres_history, tmp_path, monkeypatch, period
):
    from mira.dashboard.routers import core

    pg = PgIndexStore("example", "app", postgres_history)
    assert type(pg) is PgIndexStore
    assert pg_store._get_conn(postgres_history).info.server_version > 0
    sqlite = IndexStore(str(tmp_path / "oracle.db"))
    midnight = datetime(2025, 1, 1, tzinfo=UTC).timestamp()
    dates = [
        datetime(2024, 12, 30, tzinfo=UTC).timestamp(),
        math.nextafter(midnight, -math.inf),
        datetime(2025, 1, 8, tzinfo=UTC).timestamp(),
    ]
    try:
        for store in (pg, sqlite):
            _record_history(store)
            for number, timestamp in enumerate(dates, 1000):
                store.record_review(
                    number,
                    "Boundary review",
                    "",
                    11,
                    2,
                    3,
                    suggestions=4,
                    lines_changed=5,
                    tokens_used=6,
                    categories=" security,security,,\tbug\u00a0 ",
                    created_at=timestamp,
                )
        monkeypatch.setattr(
            core._api,
            "_app_db",
            SimpleNamespace(
                list_repos=lambda: [SimpleNamespace(owner="example", repo="app", platform="github")]
            ),
        )
        monkeypatch.setattr(core, "IndexStore", SimpleNamespace(open=lambda *a, **k: pg))
        actual = [point.model_dump() for point in core.get_timeseries(period)]
        # Keep the SQLite handle open so both real stores can serve the same route.
        monkeypatch.setattr(sqlite, "close", lambda: None)
        monkeypatch.setattr(core, "IndexStore", SimpleNamespace(open=lambda *a, **k: sqlite))
        expected = [point.model_dump() for point in core.get_timeseries(period)]
        assert actual == expected
        assert sum(point["reviews"] for point in actual) == 516
    finally:
        sqlite._conn.close()
