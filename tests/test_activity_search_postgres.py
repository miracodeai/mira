"""Activity-search parity on an explicitly supplied disposable PostgreSQL DB."""

from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

from mira.dashboard import api
from mira.dashboard.db import AppDatabase
from mira.dashboard.routers import core
from mira.index import pg_store
from mira.index.store import IndexStore


@pytest.fixture
def postgres_activity(tmp_path, monkeypatch):
    url = os.environ.get("MIRA_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("MIRA_TEST_POSTGRES_URL must name a disposable test database")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    schema = "activity_search_" + uuid4().hex
    # Every table, migration and test row lives in this test's private schema.
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        # Keep the URL prefix used by IndexStore.open's backend dispatch.
        parts = urlsplit(url)
        assert parts.scheme in {"postgresql", "postgres"}
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["options"] = f"-csearch_path={schema}"
        scoped_url = urlunsplit(parts._replace(query=urlencode(query)))
        monkeypatch.setattr(pg_store, "_pg_conn", None)
        monkeypatch.setattr(pg_store, "_schema_initialized", False)
        monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
        registry = AppDatabase(url=":memory:", admin_password="synthetic")
        monkeypatch.setattr(api, "_app_db", registry)
        try:
            yield scoped_url, registry
        finally:
            if pg_store._pg_conn is not None:
                pg_store._pg_conn.close()
            registry.close()
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _record(store, number, title, timestamp, categories=""):
    store.record_review(
        pr_number=number,
        pr_title=title,
        pr_url=f"https://example.invalid/reviews/{number}",
        comments_posted=0,
        blockers=0,
        warnings=0,
        created_at=timestamp,
        categories=categories,
    )


def _search_both_backends(postgres_activity, monkeypatch, rows, query, *, limit=200):
    url, registry = postgres_activity
    for owner, repo, platform in rows:
        registry.register_repo(owner, repo, platform=platform)
    outputs = []
    for database_url in ("", url):
        monkeypatch.setenv("DATABASE_URL", database_url)
        for (owner, repo, platform), events in rows.items():
            store = IndexStore.open(owner, repo, platform=platform)
            try:
                assert isinstance(store, pg_store.PgIndexStore if database_url else IndexStore)
                for event in events:
                    _record(store, *event)
            finally:
                store.close()
        response = core.list_activity(q=query, limit=limit)
        outputs.append(
            (
                [event.model_dump(exclude={"id"}) for event in response.events],
                response.repos,
            )
        )
    assert outputs[0] == outputs[1]
    return outputs[1]


def test_postgres_search_finds_old_match_with_python_unicode_and_literal_terms(
    postgres_activity, monkeypatch
):
    rows = {
        ("acme", "web", "github"): [
            (0, "İstanbul needle %_ [x]", 0.0, "SeCuRiTy"),
            *[(i, "Unrelated review", float(i), "") for i in range(1, 501)],
        ]
    }
    events, repos = _search_both_backends(
        postgres_activity, monkeypatch, rows, "i\u0307stanbul\t#0 %_ [x]\nsecurity acme/web"
    )
    assert [event["pr_number"] for event in events] == [0]
    assert repos == ["acme/web"]


@pytest.mark.parametrize(
    "query,expected", [("STRASSE", []), ("STRAẞE", [1]), ("ος", [2]), ("οσ", [])]
)
def test_postgres_search_preserves_python_lower_instead_of_sql_collation(
    postgres_activity, monkeypatch, query, expected
):
    rows = {
        ("acme", "web", "github"): [
            (1, "Straße", 1.0, ""),
            (2, "ΟΣ", 2.0, ""),
        ]
    }
    events, _ = _search_both_backends(postgres_activity, monkeypatch, rows, query)
    assert [event["pr_number"] for event in events] == expected


def test_postgres_search_keeps_500_matching_cap_and_closes_stream(postgres_activity, monkeypatch):
    from mira.db import postgres

    rows = {
        ("acme", "web", "github"): [
            (i, "needle" if i <= 500 else "Unrelated", float(i), "") for i in range(1001)
        ]
    }
    events, _ = _search_both_backends(postgres_activity, monkeypatch, rows, "needle", limit=1000)
    assert [event["pr_number"] for event in events] == list(range(500, 0, -1))

    connections = []
    real_connect = postgres.connect

    def connect(url):
        conn = real_connect(url)
        connections.append(conn)
        return conn

    monkeypatch.setattr(postgres, "connect", connect)
    assert len(core.list_activity(q="needle", limit=1000).events) == 500
    assert len(connections) == 1
    assert connections[0].closed
    assert not pg_store._pg_conn.closed


def test_postgres_search_preserves_platform_scope_and_global_order(postgres_activity, monkeypatch):
    rows = {
        ("acme", "web", "github"): [(1, "needle", 1.0, ""), (2, "needle", 1.0, "")],
        ("acme", "web", "gitlab"): [(3, "needle", 2.0, "")],
        ("other", "api", "github"): [(4, "needle", 3.0, "")],
    }
    events, repos = _search_both_backends(postgres_activity, monkeypatch, rows, "needle", limit=3)
    assert [event["pr_number"] for event in events[:2]] == [4, 3]
    assert events[2]["pr_number"] == 1
    assert repos == ["acme/web", "acme/web", "other/api"]


def test_postgres_search_continues_after_connection_failure(postgres_activity, monkeypatch):
    from mira.db import postgres

    rows = {
        ("acme", "first", "github"): [(1, "needle", 1.0, "")],
        ("acme", "second", "github"): [(2, "needle", 2.0, "")],
    }
    _search_both_backends(postgres_activity, monkeypatch, rows, "needle")
    _, registry = postgres_activity
    first_repo = registry.list_repos()[0].repo
    real_connect = postgres.connect
    connections = []
    calls = 0

    def connect(url):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic unavailable read connection")
        conn = real_connect(url)
        connections.append(conn)
        return conn

    monkeypatch.setattr(postgres, "connect", connect)
    result = core.list_activity(q="needle")
    assert len(result.events) == 1
    assert result.events[0].repo != first_repo
    assert calls == 2
    assert all(conn.closed for conn in connections)
