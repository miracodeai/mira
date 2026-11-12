import json
import os
import random
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

from mira.index import pg_store
from mira.index.indexer import _store_manifest_file
from mira.index.manifests import parse_manifest
from mira.index.pg_store import PgIndexStore
from mira.index.store import IndexStore, ManifestPackageMigrationError
from mira.security import poller


def test_index_preserves_nested_legacy_npm_versions():
    content = json.dumps(
        {
            "lockfileVersion": 1,
            "dependencies": {
                "example": {"version": "1.0.0", "dependencies": {"example": {"version": "2.0.0"}}},
                "parent": {
                    "dependencies": {
                        "child": {
                            "version": "3.0.0",
                            "dev": True,
                            "resolved": "child/-/child-3.0.0.tgz",
                        }
                    }
                },
            },
        }
    )
    store = IndexStore(":memory:")
    try:
        _store_manifest_file(store, "package-lock.json", content)
        assert {(p.name, p.version, p.is_dev) for p in store.list_manifest_packages()} == {
            ("example", "1.0.0", False),
            ("example", "2.0.0", False),
            ("child", "3.0.0", True),
        }
        assert all(p.scan_eligible for p in store.list_manifest_packages())
    finally:
        store.close()
    # Inline review retains its previous view until nested entries can be
    # tied to exact diff locations.
    assert [(p.name, p.version) for p in parse_manifest("package-lock.json", content)] == [
        ("example", "1.0.0")
    ]


def _modern_lockfile():
    return json.dumps(
        {
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "app", "version": "0.0.0"},
                "node_modules/example": {
                    "version": "1.0.0",
                    "resolved": "https://registry.npmjs.org/example/-/example-1.0.0.tgz",
                },
                "node_modules/a/node_modules/example": {"version": "2.0.0-beta.1"},
                "node_modules/b/node_modules/example": {
                    "version": "1.0.0",
                    "resolved": "file:../example",
                },
                "node_modules/c/node_modules/example": {
                    "version": "3.0.0",
                    "resolved": "file:../example",
                },
                "node_modules/other": {"version": "^4.0.0"},
                "node_modules/@node_modules/scoped": {"version": "5.0.0"},
                "node_modules/not-an-install/dist": {"version": "6.0.0"},
            },
        }
    )


def _seed_inventory(store):
    _store_manifest_file(store, "package-lock.json", _modern_lockfile())
    _store_manifest_file(
        store, "package.json", '{"dependencies":{"example":"^8.0.0","other":"^5.0.0"}}'
    )
    _store_manifest_file(store, "requirements.txt", "pydantic==1.0\npydantic==2.0")


def _assert_inventory(store):
    rows = store.list_manifest_packages()
    assert {
        (p.name, p.version, p.scan_eligible) for p in rows if p.file_path == "package-lock.json"
    } == {
        ("example", "1.0.0", True),
        ("example", "2.0.0-beta.1", True),
        ("example", "3.0.0", False),
        ("other", "^4.0.0", False),
        ("@node_modules/scoped", "5.0.0", True),
    }
    assert [p.version for p in rows if p.kind == "pip"] == ["==2.0"]


def test_modern_npm_inventory_keeps_versions_and_raw_specs():
    store = IndexStore(":memory:")
    try:
        _seed_inventory(store)
        _assert_inventory(store)
        _store_manifest_file(store, "package-lock.json", '{"packages":{}}')
        assert all(p.file_path != "package-lock.json" for p in store.list_manifest_packages())
    finally:
        store.close()


def test_dashboard_retains_previous_lockfile_representative(monkeypatch):
    from mira.dashboard.routers import repos

    store = IndexStore(":memory:")
    try:
        _store_manifest_file(
            store,
            "package-lock.json",
            '{"packages":{"node_modules/example":{"version":"1.0.0"},'
            '"node_modules/a/node_modules/example":{"version":"2.0.0"}}}',
        )
        monkeypatch.setattr(repos, "_open_store", lambda *args: nullcontext(store))
        assert [(p.name, p.version) for p in repos.get_packages("example", "app")] == [
            ("example", "2.0.0")
        ]
    finally:
        store.close()


def test_dashboard_returns_all_package_versions_with_manifest_sources(monkeypatch):
    from mira.dashboard.routers import repos

    store = IndexStore(":memory:")
    try:
        _store_manifest_file(
            store,
            "package-lock.json",
            '{"packages":{"node_modules/example":{"version":"1.0.0"},'
            '"node_modules/a/node_modules/example":{"version":"2.0.0"}}}',
        )
        _store_manifest_file(
            store,
            "tools/package-lock.json",
            '{"packages":{"node_modules/Example":{"version":"1.0.0","dev":true}}}',
        )
        _store_manifest_file(store, "package.json", '{"dependencies":{"example":"^1.0.0"}}')
        monkeypatch.setattr(repos, "_open_store", lambda *args: nullcontext(store))

        (package,) = repos.get_packages("example", "app")

        assert package.name == "example"
        assert package.versions[0].version == package.version
        assert {
            version.version: [
                (source.file_path, source.name, source.is_dev) for source in version.sources
            ]
            for version in package.versions
        } == {
            "1.0.0": [
                ("package-lock.json", "example", False),
                ("tools/package-lock.json", "Example", True),
            ],
            "2.0.0": [("package-lock.json", "example", False)],
            "^1.0.0": [("package.json", "example", False)],
        }
    finally:
        store.close()


_SQLITE_LEGACY = """
CREATE TABLE package_manifests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    file_path TEXT NOT NULL DEFAULT '',
    is_dev INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    UNIQUE(name, kind, file_path)
);
CREATE INDEX custom_package_version ON package_manifests(version);
INSERT INTO package_manifests VALUES (7, 'example', 'npm', '1.0.0', 'package-lock.json', 1, 123.5);
UPDATE sqlite_sequence SET seq=100 WHERE name='package_manifests';
"""


def test_sqlite_upgrade_preserves_existing_rows_and_reopens(tmp_path):
    path = str(tmp_path / "index.db")
    with sqlite3.connect(path) as db:
        db.executescript(_SQLITE_LEGACY)
    for _ in range(2):
        store = IndexStore(path)
        try:
            (row,) = store.list_manifest_packages()
            assert (row.id, row.version, row.is_dev, row.updated_at) == (7, "1.0.0", True, 123.5)
        finally:
            store.close()
    store = IndexStore(path)
    try:
        _seed_inventory(store)
        _assert_inventory(store)
        assert all(p.id > 100 for p in store.list_manifest_packages())
    finally:
        store.close()
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE name='custom_package_version'"
        ).fetchone()


def test_sqlite_migration_leaves_callers_transaction_intact():
    store = IndexStore(":memory:")
    try:
        conn = store._conn
        conn.executescript("DROP TABLE package_manifests;" + _SQLITE_LEGACY)
        conn.execute("UPDATE package_manifests SET version='2.0.0' WHERE id=7")
        assert conn.in_transaction
        with pytest.raises(ManifestPackageMigrationError, match="outside a transaction"):
            store._migrate_manifest_package_version_identity()
        assert conn.in_transaction
        assert conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert conn.execute("SELECT version FROM package_manifests WHERE id=7").fetchone() == (
            "2.0.0",
        )
        conn.rollback()
        assert conn.execute("SELECT version FROM package_manifests WHERE id=7").fetchone() == (
            "1.0.0",
        )
    finally:
        store.close()


@pytest.fixture
def postgres_url(monkeypatch):
    url = os.environ.get("MIRA_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set MIRA_TEST_POSTGRES_URL to a disposable PostgreSQL database")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    schema = "npm_inventory_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    parts = urlsplit(url)
    options = urlencode(
        {"options": f"-c search_path={schema} -c statement_timeout=5000"}, quote_via=quote
    )
    scoped_url = urlunsplit(parts._replace(query="&".join(filter(None, (parts.query, options)))))
    monkeypatch.setattr(pg_store, "_pg_conn", None)
    monkeypatch.setattr(pg_store, "_schema_initialized", False)
    monkeypatch.setenv("DATABASE_URL", scoped_url)
    try:
        yield scoped_url
    finally:
        pg_store._drop_pg_conn()
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.parametrize("schema", ["fresh", "legacy", "missing_scan_eligible"])
def test_postgres_inventory_and_upgrade(postgres_url, monkeypatch, schema):
    import psycopg

    old_raw_version = random.Random(0).randbytes(4096).hex()
    if schema != "fresh":
        with psycopg.connect(postgres_url, autocommit=True) as conn:
            conn.execute(
                """
                CREATE TABLE package_manifests (
                    id SERIAL PRIMARY KEY, owner TEXT NOT NULL, repo TEXT NOT NULL,
                    name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT '', version TEXT NOT NULL DEFAULT '',
                    file_path TEXT NOT NULL DEFAULT '', is_dev INTEGER NOT NULL DEFAULT 0,
                    updated_at DOUBLE PRECISION NOT NULL DEFAULT 0,
                    CONSTRAINT legacy_package_identity UNIQUE(owner, repo, name, kind, file_path)
                );
                CREATE INDEX custom_package_path ON package_manifests(file_path);
                INSERT INTO package_manifests VALUES
                    (7, 'example', 'app', 'example', 'npm', '1.0.0', 'package-lock.json', 1, 123.5);
                SELECT setval(pg_get_serial_sequence('package_manifests', 'id'), 100);
                """
            )
            conn.execute(
                "INSERT INTO package_manifests (id, owner, repo, name, kind, version, file_path) "
                "VALUES (8, 'peer', 'app', 'example', 'npm', %s, 'package-lock.json')",
                (old_raw_version,),
            )
            if schema == "missing_scan_eligible":
                conn.execute(
                    "ALTER TABLE package_manifests DROP CONSTRAINT legacy_package_identity"
                )
    for _ in range(2):
        store = PgIndexStore("example", "app", postgres_url)
        if schema != "fresh":
            (row,) = store.list_manifest_packages()
            assert (row.id, row.version, row.is_dev, row.updated_at) == (7, "1.0.0", True, 123.5)
            assert row.scan_eligible
        pg_store._drop_pg_conn()
        monkeypatch.setattr(pg_store, "_schema_initialized", False)
    store = PgIndexStore("example", "app", postgres_url)
    _seed_inventory(store)
    _assert_inventory(store)
    if schema != "fresh":
        assert all(p.id > 100 for p in store.list_manifest_packages())
        (peer_row,) = PgIndexStore("peer", "app", postgres_url).list_manifest_packages()
        assert (peer_row.id, peer_row.version) == (8, old_raw_version)
        with psycopg.connect(postgres_url) as conn:
            assert conn.execute(
                "SELECT to_regclass('custom_package_path') IS NOT NULL"
            ).fetchone() == (True,)


@pytest.mark.parametrize("org_wide", [False, True])
async def test_postgres_background_scan_uses_only_resolved_inventory_versions(
    postgres_url, monkeypatch, org_wide
):
    store = PgIndexStore("example", "app", postgres_url)
    _seed_inventory(store)
    store.replace_vulnerabilities_for_package(
        "example", "npm", "3.0.0", [{"cve_id": "SYNTHETIC-unscanned", "severity": "high"}]
    )
    store.replace_vulnerabilities_for_package(
        "example", "npm", "7.0.0", [{"cve_id": "SYNTHETIC-removed", "severity": "high"}]
    )
    observed = []

    async def query_batch(queries):
        observed.extend((q.ecosystem, q.name, q.version) for q in queries)
        return {(q.ecosystem, q.name, q.version): [] for q in queries}

    monkeypatch.setattr(poller, "query_batch", query_batch)
    if org_wide:
        await poller.poll_org_wide()
    else:
        await poller.poll_repo("example", "app")
    assert sorted(observed) == [
        ("npm", "@node_modules/scoped", "5.0.0"),
        ("npm", "example", "1.0.0"),
        ("npm", "example", "2.0.0-beta.1"),
        ("pip", "pydantic", "==2.0"),
    ]
    assert [v.cve_id for v in store.list_vulnerabilities()] == ["SYNTHETIC-unscanned"]


def test_postgres_replaces_long_raw_versions_atomically(postgres_url):
    import psycopg

    store = PgIndexStore("example", "app", postgres_url)
    versions = [random.Random(seed).randbytes(4096).hex() for seed in (0, 1)]

    def replace(values):
        store.replace_manifest_packages(
            "package-lock.json",
            [
                {
                    "name": "example",
                    "kind": "npm",
                    "version": value,
                    "file_path": "package-lock.json",
                    "scan_eligible": False,
                }
                for value in values
            ],
        )

    replace(versions)
    before = store.list_manifest_packages()
    assert {p.version for p in before} == set(versions)
    with psycopg.connect(postgres_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE package_manifests ADD CHECK (version <> 'rejected')")
    with pytest.raises(psycopg.errors.CheckViolation):
        replace([versions[0], "rejected"])
    assert store.list_manifest_packages() == before


def test_postgres_waiting_replacement_sees_committed_rows(postgres_url, monkeypatch):
    import psycopg

    from mira.db import postgres

    repo = f"app-{uuid4().hex}"
    store = PgIndexStore("example", repo, postgres_url)
    path = "package-lock.json"

    def connect_repeatable_read(url):
        conn = psycopg.connect(url, autocommit=True)
        conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        return conn

    def replace(version):
        store.replace_manifest_packages(
            path, [{"name": "example", "kind": "npm", "version": version, "file_path": path}]
        )

    monkeypatch.setattr(postgres, "connect", connect_repeatable_read)
    key = json.dumps(["package_manifests", "example", repo, path])
    with psycopg.connect(postgres_url, autocommit=True) as observer, ThreadPoolExecutor(2) as pool:
        observer.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,))
        try:
            writers = [pool.submit(replace, version) for version in ("1.0.0", "2.0.0")]
            deadline = time.monotonic() + 4
            while True:
                (waiting,) = observer.execute(
                    "SELECT count(*) FROM pg_locks AS waiter JOIN pg_locks AS holder "
                    "USING (locktype, database, classid, objid, objsubid) "
                    "WHERE holder.pid = pg_backend_pid() AND holder.granted "
                    "AND holder.locktype = 'advisory' AND NOT waiter.granted"
                ).fetchone()
                if waiting == 2:
                    break
                if time.monotonic() >= deadline:
                    pytest.fail(f"Expected both replacements to queue, saw {waiting}")
                time.sleep(0.01)
        finally:
            observer.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))
        for writer in writers:
            writer.result(timeout=5)
    (row,) = store.list_manifest_packages()
    assert row.version in {"1.0.0", "2.0.0"}
