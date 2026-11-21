"""Final package-search limits apply to groups after lockfile preference."""

from __future__ import annotations

import os
from urllib.parse import quote, urlencode
from uuid import uuid4

import pytest

from mira.index.store import IndexStore


@pytest.fixture
def postgres_packages(monkeypatch):
    url = os.environ.get("MIRA_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set MIRA_TEST_POSTGRES_URL to a disposable PostgreSQL database")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    from mira.index import pg_store

    schema = "package_search_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    separator = "&" if "?" in url else "?"
    scoped_url = (
        url + separator + urlencode({"options": f"-c search_path={schema}"}, quote_via=quote)
    )
    monkeypatch.setattr(pg_store, "_pg_conn", None)
    monkeypatch.setattr(pg_store, "_schema_initialized", False)
    try:
        yield scoped_url
    finally:
        pg_store._drop_pg_conn()
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
@pytest.mark.parametrize("limit", [1, 2])
def test_package_search_limit_keeps_unique_groups_and_lockfile_winner(
    tmp_path, monkeypatch, request, backend, limit
):
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "")
    from mira.dashboard.routers.vulnerabilities import search_packages

    path = tmp_path / "acme" / "demo.db"
    path.parent.mkdir()
    if backend == "postgres":
        from mira.index.pg_store import PgIndexStore

        url = request.getfixturevalue("postgres_packages")
        monkeypatch.setenv("DATABASE_URL", url)
        writer = PgIndexStore("acme", "demo", url)
    else:
        writer = IndexStore(str(path))
    try:
        writer.replace_manifest_packages(
            "pyproject.toml",
            [{"name": "alpha", "kind": "pip", "version": ">=1", "file_path": "pyproject.toml"}],
        )
        writer.replace_manifest_packages(
            "uv.lock",
            [
                {"name": "alpha", "kind": "pip", "version": "1.2.3", "file_path": "uv.lock"},
                {"name": "bravo", "kind": "pip", "version": "2.0", "file_path": "uv.lock"},
            ],
        )
    finally:
        writer.close()
    rows = search_packages(limit=limit)

    expected = [("alpha", "1.2.3", "uv.lock"), ("bravo", "2.0", "uv.lock")][:limit]
    assert [(r.name, r.version, r.file_path) for r in rows] == expected


def _write_packages(path, packages):
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = IndexStore(str(path))
    try:
        by_file = {}
        for name, file_path in packages:
            by_file.setdefault(file_path, []).append(
                {"name": name, "kind": "pip", "version": "1", "file_path": file_path}
            )
        for file_path, rows in by_file.items():
            writer.replace_manifest_packages(file_path, rows)
    finally:
        writer.close()


def _group_raw_rows(rows, limit):
    from mira.index.manifests import _is_lockfile_path

    groups = {}
    for row in rows:
        key = (row["owner"], row["repo"], row["kind"], row["name"].lower())
        old = groups.get(key)
        if old is None or (
            _is_lockfile_path(row["file_path"]) and not _is_lockfile_path(old["file_path"])
        ):
            groups[key] = row
    return list(groups.values())[:limit]


@pytest.mark.parametrize(
    "fault", ["connect", "execute", "iterate", "cursor-close", "connection-close"]
)
def test_sqlite_package_groups_preserve_raw_reader_failure_and_cleanup_semantics(
    tmp_path, monkeypatch, fault
):
    import sqlite3

    from mira.index import store

    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    _write_packages(
        tmp_path / "_gitlab" / "acme" / "demo.db",
        [("middle", "pyproject.toml"), ("zulu", "uv.lock")],
    )
    broken = tmp_path / "acme" / "demo.db"
    _write_packages(broken, [("alpha", "uv.lock"), ("middle", "uv.lock")])
    _write_packages(tmp_path / "z-owner" / "last.db", [("bravo", "uv.lock")])
    real_connect = sqlite3.connect
    connections = []

    # Explicit database-boundary doubles let a read or cleanup fail after real
    # rows were read. The old raw helper remains the compatibility oracle.
    class Cursor:
        def __init__(self, cursor, broken_store):
            self.real = cursor
            self.broken = broken_store

        def execute(self, *args, **kwargs):
            if self.broken and fault == "execute":
                raise sqlite3.OperationalError("synthetic query failure")
            self.real.execute(*args, **kwargs)
            return self

        def fetchall(self):
            rows = self.real.fetchall()
            if self.broken and fault == "iterate":
                raise sqlite3.OperationalError("synthetic fetch failure")
            return rows

        def __iter__(self):
            yield from self.real
            if self.broken and fault == "iterate":
                raise sqlite3.OperationalError("synthetic fetch failure after rows")

        def close(self):
            self.real.close()
            if self.broken and fault == "cursor-close":
                raise sqlite3.OperationalError("synthetic cursor cleanup failure")

    class Connection:
        def __init__(self, real, broken_store):
            self.real = real
            self.broken = broken_store
            self.cursors = []
            self.closed = False

        def cursor(self):
            cursor = Cursor(self.real.cursor(), self.broken)
            self.cursors.append(cursor)
            return cursor

        def execute(self, *args, **kwargs):
            return self.cursor().execute(*args, **kwargs)

        def close(self):
            self.real.close()
            self.closed = True
            if self.broken and fault == "connection-close":
                raise sqlite3.OperationalError("synthetic connection cleanup failure")

    def connect(database, *args, **kwargs):
        broken_store = str(database) in {str(broken), broken.as_uri() + "?mode=ro"}
        if broken_store and fault == "connect":
            raise sqlite3.OperationalError("synthetic connection failure")
        connection = Connection(real_connect(database, *args, **kwargs), broken_store)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    expected = _group_raw_rows(store.search_packages_org_wide_sqlite(limit=100), 2)

    actual = store.search_package_groups_org_wide_sqlite(limit=2)

    assert actual == expected
    assert [r["name"] for r in actual] == (
        ["alpha", "bravo"] if fault.endswith("close") else ["bravo", "middle"]
    )
    assert connections and all(c.closed for c in connections)
    for connection in connections:
        for cursor in connection.cursors:
            with pytest.raises(sqlite3.ProgrammingError):
                cursor.real.execute("SELECT 1")


@pytest.fixture(params=["sqlite", "postgres"])
def package_source(request, tmp_path, monkeypatch):
    """Real isolated backend data; PostgreSQL always owns a unique test schema."""
    from functools import partial

    from mira.index import pg_store, store

    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    if request.param == "postgres":
        url = request.getfixturevalue("postgres_packages")
        writer = pg_store.PgIndexStore("acme", "demo", url)
        raw = partial(pg_store.search_packages_org_wide, url)
        grouped = partial(pg_store.search_package_groups_org_wide, url)
    else:
        path = tmp_path / "acme" / "demo.db"
        path.parent.mkdir()
        writer = IndexStore(str(path))
        raw = store.search_packages_org_wide_sqlite
        grouped = store.search_package_groups_org_wide_sqlite
    try:
        for file_path, rows in {
            "pyproject.toml": [
                {"name": "alpha", "kind": "pip", "version": ">=1", "file_path": "pyproject.toml"}
            ],
            "uv.lock": [
                {"name": "alpha", "kind": "pip", "version": "1.2.3", "file_path": "uv.lock"},
                {
                    "name": "beta100%",
                    "kind": "pip",
                    "version": "V_1",
                    "file_path": "uv.lock",
                    "is_dev": True,
                },
                {
                    "name": "beta100X",
                    "kind": "pip",
                    "version": "Va1",
                    "file_path": "uv.lock",
                    "is_dev": True,
                },
            ],
            "package.json": [
                {
                    "name": "alpha",
                    "kind": "npm",
                    "version": "^2",
                    "file_path": "package.json",
                    "is_dev": True,
                }
            ],
        }.items():
            writer.replace_manifest_packages(file_path, rows)
        yield request.param, raw, grouped
    finally:
        writer.close()


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        (
            {"name": "ALP", "version": "1.", "kind": "pip", "is_dev": False},
            {("alpha", "pip", "1.2.3")},
        ),
        ({"version": ">=1"}, {("alpha", "pip", ">=1")}),
        ({"is_dev": True, "kind": "npm"}, {("alpha", "npm", "^2")}),
        ({"name": "absent"}, set()),
    ],
)
def test_package_groups_preserve_filtering_before_lockfile_preference(
    package_source, filters, expected
):
    _backend, raw, grouped = package_source

    rows = grouped(**filters, limit=100)

    assert {(r["name"], r["kind"], r["version"]) for r in rows} == expected
    assert rows == _group_raw_rows(raw(**filters, limit=100), 100)


def test_package_groups_preserve_backend_wildcard_matching(package_source):
    backend, raw, grouped = package_source
    rows = grouped(name="beta100%", limit=100)
    assert {r["name"] for r in rows} == (
        {"beta100%", "beta100X"} if backend == "postgres" else {"beta100%"}
    )
    assert rows == _group_raw_rows(raw(name="beta100%", limit=100), 100)
    rows = grouped(version="_", limit=100)
    assert {r["name"] for r in rows} == (
        {"alpha", "beta100%", "beta100X"} if backend == "postgres" else {"beta100%"}
    )
    assert rows == _group_raw_rows(raw(version="_", limit=100), 100)


@pytest.mark.parametrize("relative", [False, True])
def test_sqlite_group_search_reads_wal_and_encoded_paths(tmp_path, monkeypatch, relative):
    from mira.index.store import search_package_groups_org_wide_sqlite

    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    path = tmp_path / "org ?#%日本" / "repo ?#%.db"
    path.parent.mkdir()
    writer = IndexStore(str(path))
    try:
        writer._conn.execute("PRAGMA wal_autocheckpoint=0")
        writer.replace_manifest_packages(
            "uv.lock",
            [{"name": "alpha", "kind": "pip", "version": "2", "file_path": "uv.lock"}],
        )
        assert path.with_name(path.name + "-wal").exists()
        if relative:
            monkeypatch.chdir(tmp_path.parent)
            monkeypatch.setenv("MIRA_INDEX_DIR", tmp_path.name)
        before = path.read_bytes()

        rows = search_package_groups_org_wide_sqlite(limit=1)

        assert [(r["owner"], r["repo"], r["name"], r["version"]) for r in rows] == [
            ("org ?#%日本", "repo ?#%", "alpha", "2")
        ]
        assert path.read_bytes() == before
    finally:
        writer.close()


def test_sqlite_group_search_does_not_recreate_database_removed_at_connect(tmp_path, monkeypatch):
    import sqlite3

    from mira.index.store import search_package_groups_org_wide_sqlite

    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    path = tmp_path / "acme" / "demo.db"
    _write_packages(path, [("alpha", "uv.lock")])
    real_connect = sqlite3.connect

    def connect(database, *args, **kwargs):
        path.unlink()
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    assert search_package_groups_org_wide_sqlite() == []
    assert not path.exists()


def test_postgres_group_search_keeps_native_order_and_bounded_server_cursor(
    postgres_packages, monkeypatch
):
    import psycopg

    from mira.db import postgres
    from mira.index import pg_store

    shared = pg_store._get_conn(postgres_packages)
    records = [("acme", "demo", f"p{n:04}", "pip", "1", "uv.lock", 0) for n in range(513)]
    records += [
        ("_forgejo/z", "demo", "X", "pip", ">=1", "pyproject.toml", 0),
        ("_gitlab/a", "demo", "x", "pip", "2", "uv.lock", 0),
        ("a", "demo", "x", "pip", ">=2", "pyproject.toml", 0),
        ("z", "demo", "X", "pip", "3", "uv.lock", 0),
        ("acme", "demo", "İ", "pip", "4", "uv.lock", 0),
        ("acme", "demo", "i\u0307", "pip", ">=4", "pyproject.toml", 0),
        ("acme", "demo", "I", "pip", "5", "uv.lock", 0),
        ("acme", "demo", "ß", "pip", "6", "uv.lock", 0),
        ("acme", "demo", "ss", "pip", "7", "uv.lock", 0),
    ]
    with shared.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO package_manifests (owner,repo,name,kind,version,file_path,is_dev) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            records,
        )
    expected = _group_raw_rows(
        pg_store.search_packages_org_wide(postgres_packages, limit=1000), 520
    )
    assert shared.info.server_version > 0
    connections = []
    cursors = []
    batches = []
    real_connect = postgres.connect

    class Cursor(psycopg.ServerCursor):
        def __next__(self):
            record = super().__next__()
            if not batches:
                assert self.itersize == 128
                assert self.connection.execute("SHOW transaction_read_only").fetchone() == ("on",)
                assert self.connection.execute("SELECT name FROM pg_cursors").fetchall() == [
                    ("mira_package_search",)
                ]
            batches.append(self.pgresult.ntuples)
            return record

    def connect(url):
        connection = real_connect(url)
        connections.append(connection)

        def server_cursor(*args, **kwargs):
            cursor = Cursor(*args, **kwargs)
            cursors.append(cursor)
            return cursor

        connection.server_cursor_factory = server_cursor
        return connection

    monkeypatch.setattr(postgres, "connect", connect)
    actual = pg_store.search_package_groups_org_wide(postgres_packages, limit=520)

    assert actual == expected
    assert len(batches) == len(records) and max(batches) == 128
    assert len(connections) == len(cursors) == 1
    assert connections[0] is not shared and connections[0].closed and cursors[0].closed
    assert not shared.closed and shared.execute("SELECT 1").fetchone() == (1,)


@pytest.mark.parametrize("fault", ["execute", "iterate"])
def test_postgres_group_search_propagates_errors_and_closes_resources(
    postgres_packages, monkeypatch, fault
):
    import psycopg

    from mira.db import postgres
    from mira.index import pg_store

    shared = pg_store._get_conn(postgres_packages)
    with shared.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO package_manifests (owner,repo,name,kind,version,file_path,is_dev) VALUES ('acme','demo',%s,'pip','1','uv.lock',0)",
            [(f"p{n:04}",) for n in range(257)],
        )
    real_connect = postgres.connect
    connections = []
    cursors = []
    closed_portals = set()

    class Cursor(psycopg.ServerCursor):
        def execute(self, query, params=None, **kwargs):
            if fault == "execute":
                query = "SELECT * FROM synthetic_absent_package_table"
                params = None
            return super().execute(query, params, **kwargs)

        def __next__(self):
            record = super().__next__()
            if id(self) not in closed_portals:
                self.connection.execute("CLOSE mira_package_search")
                closed_portals.add(id(self))
            return record

    def connect(url):
        connection = real_connect(url)
        connections.append(connection)

        def server_cursor(*args, **kwargs):
            cursor = Cursor(*args, **kwargs)
            cursors.append(cursor)
            return cursor

        connection.server_cursor_factory = server_cursor
        return connection

    monkeypatch.setattr(postgres, "connect", connect)
    error = (
        psycopg.errors.UndefinedTable if fault == "execute" else psycopg.errors.InvalidCursorName
    )
    with pytest.raises(error):
        pg_store.search_package_groups_org_wide(postgres_packages, limit=2)
    assert len(connections) == len(cursors) == 1
    assert connections[0].closed and cursors[0].closed
    assert not shared.closed and shared.execute("SELECT 1").fetchone() == (1,)


def test_postgres_group_search_initializes_schema_before_readonly_stream(postgres_packages):
    from mira.index import pg_store

    assert pg_store._pg_conn is None and not pg_store._schema_initialized
    assert pg_store.search_package_groups_org_wide(postgres_packages) == []
    assert pg_store._schema_initialized
    assert pg_store._get_conn(postgres_packages).execute("SELECT 1").fetchone() == (1,)


@pytest.mark.parametrize("failure", ["corrupt", "missing-table", "missing-column"])
def test_sqlite_group_search_skips_legacy_unreadable_store_without_migration(
    tmp_path, monkeypatch, failure
):
    import sqlite3

    from mira.index.store import search_package_groups_org_wide_sqlite

    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    _write_packages(tmp_path / "z-owner" / "healthy.db", [("alpha", "uv.lock")])
    broken = tmp_path / "acme" / "broken.db"
    broken.parent.mkdir()
    if failure == "corrupt":
        broken.write_bytes(b"synthetic non-database")
    else:
        with sqlite3.connect(broken) as connection:
            table = "other" if failure == "missing-table" else "package_manifests"
            connection.execute(f"CREATE TABLE {table} (unrelated TEXT)")
    before = broken.read_bytes()
    rows = search_package_groups_org_wide_sqlite()
    assert [(r["owner"], r["repo"], r["name"]) for r in rows] == [("z-owner", "healthy", "alpha")]
    assert broken.read_bytes() == before


def test_route_keeps_default_and_clamped_final_group_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "")
    _write_packages(tmp_path / "acme" / "demo.db", [(f"p{n:04}", "uv.lock") for n in range(2003)])
    from mira.dashboard.routers.vulnerabilities import search_packages

    assert len(search_packages()) == 500
    for requested, expected in [(-3, 1), (0, 1), (1, 1), (2001, 2000)]:
        rows = search_packages(limit=requested)
        assert len(rows) == expected
        assert (rows[0].name, rows[-1].name) == ("p0000", f"p{expected - 1:04}")


def test_postgres_group_search_protocol_uses_named_cursor_without_materializing(monkeypatch):
    from contextlib import contextmanager

    from mira.db import postgres
    from mira.index import pg_store

    # Offline database adapter double: real-driver lifecycle/batches and matching
    # have separate disposable-PostgreSQL controls above.
    events = []
    closed = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            closed.append("cursor")

        def execute(self, query, params):
            assert "ORDER BY LOWER(name), owner, repo" in query and "LIMIT" not in query
            assert params == ("%alpha%", "pip", 0)
            assert self.itersize == 128

        def __iter__(self):
            yield from [
                ("_forgejo/z", "demo", "alpha", "pip", ">=1", "pyproject.toml", 0),
                ("_gitlab/a", "demo", "alpha", "pip", "2", "uv.lock", 0),
                ("a", "demo", "alpha", "pip", ">=2", "pyproject.toml", 0),
                ("z", "demo", "alpha", "pip", "3", "uv.lock", 0),
            ]

        def fetchall(self):
            raise AssertionError("A package stream must not materialize raw rows")

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            closed.append("connection")

        @contextmanager
        def transaction(self):
            try:
                yield
            finally:
                closed.append("transaction")

        def execute(self, query):
            assert query == "SET TRANSACTION READ ONLY"

        def cursor(self, *, name):
            assert name == "mira_package_search"
            return Cursor()

    def initialize(url):
        events.append(("initialize", url))

    def connect(url):
        events.append(("connect", url))
        return Connection()

    monkeypatch.setattr(pg_store, "_get_conn", initialize)
    monkeypatch.setattr(postgres, "connect", connect)
    rows = pg_store.search_package_groups_org_wide(
        "synthetic-url", name="alpha", kind="pip", is_dev=False, limit=1
    )
    assert [(r["owner"], r["version"], r["file_path"]) for r in rows] == [("z", "3", "uv.lock")]
    assert events == [("initialize", "synthetic-url"), ("connect", "synthetic-url")]
    assert closed == ["cursor", "transaction", "connection"]
