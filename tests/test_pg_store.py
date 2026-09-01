"""PgIndexStore tests against a SQLite-backed psycopg stand-in.

The real ``_PG_SCHEMA`` happens to parse under SQLite, so these tests run the
store's actual SQL (with ``%s`` swapped for ``?``) against an in-memory
database — real behavior coverage without a Postgres server.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from mira.index import pg_store
from mira.index.pg_store import _PG_SCHEMA, PgIndexStore
from mira.index.store import IndexStore
from mira.models import PRFingerprint


class _FakeCursor:
    def __init__(self, conn: sqlite3.Connection):
        self._cur = conn.cursor()

    def execute(self, sql, params=()):
        self._cur.execute(sql.replace("%s", "?"), params)
        return self

    def executemany(self, sql, seq_of_params):
        self._cur.executemany(sql.replace("%s", "?"), seq_of_params)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def close(self):
        self._cur.close()


class _FakeConn:
    def __init__(self):
        self._conn = sqlite3.connect(":memory:")
        # SERIAL isn't a rowid alias in SQLite — ids would insert as NULL.
        self._conn.executescript(_PG_SCHEMA.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY"))

    def cursor(self):
        return _FakeCursor(self._conn)


@pytest.fixture
def fake_conn(monkeypatch):
    conn = _FakeConn()
    monkeypatch.setattr(pg_store, "_get_conn", lambda url: conn)
    return conn


@pytest.fixture
def store(fake_conn):
    return PgIndexStore("acme", "widgets", "postgresql://fake")


def _fp(number, *, head_sha="sha", updated_at=0.0, paths=None, symbols=None):
    return PRFingerprint(
        pr_number=number,
        head_sha=head_sha,
        title=f"PR {number}",
        body="",
        paths=paths or [],
        symbols=symbols or [],
        updated_at=updated_at,
    )


def test_fingerprint_upsert_and_list(store):
    store.upsert_pr_fingerprint(_fp(7, paths=["a.py", "b.py"], symbols=["foo"]))

    rows = store.list_pr_fingerprints()
    assert len(rows) == 1
    got = rows[0]
    assert got.pr_number == 7
    assert got.title == "PR 7"
    assert got.paths == ["a.py", "b.py"]
    assert got.symbols == ["foo"]
    assert got.updated_at > 0


def test_fingerprint_upsert_replaces_on_conflict(store):
    store.upsert_pr_fingerprint(_fp(7, head_sha="old", paths=["a.py"]))
    store.upsert_pr_fingerprint(_fp(7, head_sha="new", paths=["a.py", "c.py"]))

    rows = store.list_pr_fingerprints()
    assert len(rows) == 1
    assert rows[0].head_sha == "new"
    assert rows[0].paths == ["a.py", "c.py"]


def test_fingerprint_upsert_prunes_stale_rows(store):
    now = time.time()
    store.upsert_pr_fingerprint(_fp(1, updated_at=now - IndexStore._FINGERPRINT_TTL - 3600))
    store.upsert_pr_fingerprint(_fp(2, updated_at=now - 60))
    store.upsert_pr_fingerprint(_fp(3))

    numbers = {fp.pr_number for fp in store.list_pr_fingerprints()}
    assert numbers == {2, 3}


def test_add_and_list_review_comments(store):
    store.add_review_comments(
        1,
        42,
        "https://github.com/acme/widgets/pull/42",
        [
            {"path": "a.py", "line": 3, "severity": "warning", "title": "t1", "body": "b1"},
            {"path": "b.py", "line": 9, "severity": "blocker", "title": "t2", "body": "b2"},
        ],
    )

    rows = store.list_review_comments(42)
    assert [(r.path, r.line, r.severity) for r in rows] == [
        ("a.py", 3, "warning"),
        ("b.py", 9, "blocker"),
    ]


def test_record_and_list_replies(store):
    row = store.record_reply(
        42,
        "https://github.com/acme/widgets/pull/42",
        author="alice",
        body="looks fixed",
        comment_path="a.py",
        comment_line=3,
    )
    assert row.id > 0

    rows = store.list_replies(42)
    assert len(rows) == 1
    assert rows[0].author == "alice"
    assert rows[0].body == "looks fixed"


def test_fingerprints_scoped_by_repo(fake_conn):
    a = PgIndexStore("acme", "widgets", "postgresql://fake")
    b = PgIndexStore("acme", "gadgets", "postgresql://fake")
    now = time.time()

    # A stale row in repo B must survive repo A's prune-on-write.
    b.upsert_pr_fingerprint(_fp(1, updated_at=now - IndexStore._FINGERPRINT_TTL - 3600))
    a.upsert_pr_fingerprint(_fp(1, updated_at=now))

    assert [fp.pr_number for fp in a.list_pr_fingerprints()] == [1]
    assert [fp.pr_number for fp in b.list_pr_fingerprints()] == [1]
    assert b.list_pr_fingerprints()[0].updated_at < now - IndexStore._FINGERPRINT_TTL
