"""Polling retains active alert history while pruning obsolete package keys."""

from __future__ import annotations

import json
import os
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx
import pytest

from mira.index import pg_store
from mira.index.store import IndexStore
from mira.security import poller


@pytest.fixture(params=["sqlite", "postgres"])
def polling_stores(request, monkeypatch):
    if request.param == "sqlite":
        # Exercise both PG-only poll entry points with real SQLite store methods.
        # The PostgreSQL variant below verifies actual dispatch and SQL as well.
        stores = {
            ("example", "app"): IndexStore(":memory:"),
            ("example", "other"): IndexStore(":memory:"),
        }
        monkeypatch.setenv("DATABASE_URL", "postgresql://synthetic.invalid/test")
        monkeypatch.setattr(
            pg_store, "PgIndexStore", lambda owner, repo, url: stores[(owner, repo)]
        )
        monkeypatch.setattr(
            pg_store,
            "list_packages_org_wide",
            lambda url: [
                {
                    "owner": owner,
                    "repo": repo,
                    "kind": p.kind,
                    "name": p.name,
                    "version": p.version,
                    "file_path": p.file_path,
                }
                for (owner, repo), store in stores.items()
                for p in store.list_manifest_packages()
            ],
        )
        try:
            yield stores[("example", "app")], stores[("example", "other")]
        finally:
            for store in stores.values():
                store.close()
        return

    url = os.environ.get("MIRA_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("MIRA_TEST_POSTGRES_URL must name a disposable PostgreSQL database")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    schema = "poll_pruning_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    parts = urlsplit(url)
    assert parts.scheme in {"postgresql", "postgres"}
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["options"] = f"-csearch_path={schema}"
    scoped_url = urlunsplit(parts._replace(query=urlencode(query)))
    monkeypatch.setenv("DATABASE_URL", scoped_url)
    monkeypatch.setattr(pg_store, "_pg_conn", None)
    monkeypatch.setattr(pg_store, "_schema_initialized", False)
    try:
        store = pg_store.PgIndexStore("example", "app", scoped_url)
        other = pg_store.PgIndexStore("example", "other", scoped_url)
        assert type(store) is pg_store.PgIndexStore
        assert pg_store._get_conn(scoped_url).info.server_version > 0
        yield store, other
    finally:
        pg_store._drop_pg_conn()
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["repo", "org"])
@pytest.mark.parametrize(
    ("kind", "path", "wire_ecosystem"),
    [
        ("npm", "package-lock.json", "npm"),
        ("pip", "uv.lock", "PyPI"),
        ("go", "go.sum", "Go"),
        ("rust", "Cargo.lock", "crates.io"),
        ("composer", "composer.lock", "Packagist"),
    ],
)
async def test_poll_pruning_preserves_active_first_seen(
    polling_stores, monkeypatch, flow, kind, path, wire_ecosystem
):
    store, other = polling_stores
    store.replace_manifest_packages(
        path, [{"name": "demo", "kind": kind, "version": "2.0.0", "file_path": path}]
    )
    with monkeypatch.context() as clock:
        clock.setattr(time, "time", lambda: 123.0)
        for name, ecosystem, version, advisory in [
            ("demo", kind, "2.0.0", "SYNTHETIC-active"),
            ("demo", kind, "1.0.0", "SYNTHETIC-old-version"),
            ("removed", kind, "2.0.0", "SYNTHETIC-removed"),
            ("demo", "docker", "2.0.0", "SYNTHETIC-other-kind"),
        ]:
            store.replace_vulnerabilities_for_package(
                name, ecosystem, version, [{"cve_id": advisory, "severity": "high"}]
            )
        other.replace_vulnerabilities_for_package(
            "demo", kind, "2.0.0", [{"cve_id": "SYNTHETIC-peer", "severity": "low"}]
        )
    peer_before = other.list_vulnerabilities()
    requests = []

    def serve(request):
        requests.append(request)
        if request.method == "POST":
            assert json.loads(request.content) == {
                "queries": [
                    {
                        "package": {"ecosystem": wire_ecosystem, "name": "demo"},
                        "version": "2.0.0",
                    }
                ]
            }
            return httpx.Response(200, json={"results": [{"vulns": [{"id": "SYNTHETIC-active"}]}]})
        assert request.method == "GET"
        assert request.url.path == "/v1/vulns/SYNTHETIC-active"
        return httpx.Response(
            200,
            json={
                "id": "SYNTHETIC-active",
                "summary": "Still affected",
                "database_specific": {"severity": "HIGH"},
            },
        )

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(serve), **kwargs),
    )
    counts = (
        await poller.poll_repo("example", "app") if flow == "repo" else await poller.poll_org_wide()
    )

    assert counts == {"high": 1}
    assert [r.method for r in requests] == ["POST", "GET"]
    remaining = store.list_vulnerabilities()
    assert len(remaining) == 1
    alert = remaining[0]
    assert (alert.package_name, alert.ecosystem, alert.package_version, alert.cve_id) == (
        "demo",
        kind,
        "2.0.0",
        "SYNTHETIC-active",
    )
    assert alert.first_seen_at == 123.0
    assert alert.last_seen_at > alert.first_seen_at
    assert other.list_vulnerabilities() == peer_before
