"""Additive outcome metadata survives upgrades without inventing historic proof."""

import os
import sqlite3
from contextlib import closing
from uuid import uuid4

import pytest

from mira.index import pg_store
from mira.index.pg_store import PgIndexStore
from mira.index.store import IndexStore
from mira.review_outcome import DeliveryStatus, ReviewOutcome


@pytest.fixture
def postgres_outcomes(monkeypatch):
    url = os.environ.get("MIRA_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("A disposable PostgreSQL database is required")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    schema = "outcome_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = make_conninfo(url, options=f"-c search_path={schema}")
    monkeypatch.setattr(pg_store, "_pg_conn", None)
    monkeypatch.setattr(pg_store, "_schema_initialized", False)
    try:
        yield scoped
    finally:
        pg_store._drop_pg_conn()
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _outcome():
    return ReviewOutcome(3, 2, 1, 1, 0, DeliveryStatus.PARTIAL, "complete")


def _assert_reads(store):
    recent = store.list_review_events()
    by_pr = store.list_review_events_for_pr(1)
    with store.iter_review_events() as stream:
        history = list(stream)
    assert recent == by_pr == history
    assert len(recent) == 2
    assert recent[1].comments_posted == 7
    assert recent[1].outcome_json == ""
    assert ReviewOutcome.from_json(recent[1].outcome_json) is None
    assert ReviewOutcome.from_json(recent[0].outcome_json) == _outcome()


def test_sqlite_upgrade_preserves_legacy_rows_and_outcome_on_reopen(tmp_path):
    path = str(tmp_path / "index.db")
    with closing(IndexStore(path)) as store:
        store.record_review(1, "Old", "", 7, 0, 0, created_at=100)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE review_events DROP COLUMN outcome_json")
    with closing(IndexStore(path)) as store:
        store.record_review(
            1, "New", "", 2, 0, 0, created_at=200, outcome_json=_outcome().to_json()
        )
        _assert_reads(store)
    with closing(IndexStore(path)) as store:
        _assert_reads(store)


def test_postgres_upgrade_preserves_history_and_outcome(postgres_outcomes):
    import psycopg

    store = PgIndexStore("acme", "app", postgres_outcomes)
    store.record_review(1, "Old", "", 7, 0, 0, created_at=100)
    pg_store._drop_pg_conn()
    pg_store._schema_initialized = False
    with psycopg.connect(postgres_outcomes, autocommit=True) as db:
        db.execute("ALTER TABLE review_events DROP COLUMN outcome_json")
    reopened = PgIndexStore("acme", "app", postgres_outcomes)
    with reopened.iter_review_events() as events:
        legacy = list(events)
    assert len(legacy) == 1 and legacy[0].outcome_json == ""
    reopened.record_review(1, "New", "", 2, 0, 0, created_at=200, outcome_json=_outcome().to_json())
    _assert_reads(reopened)
    pg_store._drop_pg_conn()
    pg_store._schema_initialized = False
    _assert_reads(PgIndexStore("acme", "app", postgres_outcomes))
