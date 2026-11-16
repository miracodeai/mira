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


@pytest.mark.asyncio
@pytest.mark.parametrize("flow", ["repo", "org"])
@pytest.mark.parametrize(
    "kind,path,wire_ecosystem", [("npm", "package-lock.json", "npm"), ("pip", "uv.lock", "PyPI")]
)
@pytest.mark.parametrize("outcome", ["timeout", "http-error", "clean", "affected", "partial"])
async def test_unavailable_poll_results_preserve_alerts(
    polling_stores, monkeypatch, caplog, flow, kind, path, wire_ecosystem, outcome
):
    import logging
    from dataclasses import asdict

    store, other = polling_stores
    store.replace_manifest_packages(
        path,
        [
            {"name": name, "kind": kind, "version": "2.0.0", "file_path": path}
            for name in ["alpha", "beta"]
        ],
    )
    with monkeypatch.context() as clock:
        clock.setattr(time, "time", lambda: 123.0)
        for name in ["alpha", "beta", "gone"]:
            store.replace_vulnerabilities_for_package(
                name,
                kind,
                "2.0.0",
                [{"cve_id": "SYNTHETIC-" + name, "severity": "high", "summary": "Existing alert"}],
            )
        other.replace_vulnerabilities_for_package(
            "alpha", kind, "2.0.0", [{"cve_id": "SYNTHETIC-peer", "severity": "low"}]
        )
    before = {r.package_name: asdict(r) for r in store.list_vulnerabilities()}
    peer_before = other.list_vulnerabilities()
    requests = []
    queried_names = []

    def serve(request):
        requests.append(request)
        if request.method == "GET":
            assert outcome == "affected"
            assert request.url.path in {"/v1/vulns/SYNTHETIC-alpha", "/v1/vulns/SYNTHETIC-beta"}
            return httpx.Response(
                200,
                json={
                    "id": request.url.path.rsplit("/", 1)[1],
                    "summary": "Updated alert",
                    "database_specific": {"severity": "HIGH"},
                },
            )
        assert request.method == "POST"
        queries = json.loads(request.content)["queries"]
        assert len(queries) == 2
        queried_names.extend(q["package"]["name"] for q in queries)
        assert sorted(queried_names) == ["alpha", "beta"]
        assert all(
            q["package"]["ecosystem"] == wire_ecosystem and q["version"] == "2.0.0" for q in queries
        )
        if outcome == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if outcome == "http-error":
            return httpx.Response(503)
        results = (
            [{"vulns": [{"id": "SYNTHETIC-" + name}]} for name in queried_names]
            if outcome == "affected"
            else [{}]
            if outcome == "partial"
            else [{}, {}]
        )
        return httpx.Response(200, json={"results": results})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(serve), **kwargs),
    )
    caplog.set_level(logging.INFO, logger="mira.security.poller")

    counts = (
        await poller.poll_repo("example", "app") if flow == "repo" else await poller.poll_org_wide()
    )

    remaining = {r.package_name: asdict(r) for r in store.list_vulnerabilities()}
    expected_names = (
        {"alpha", "beta"}
        if outcome in {"timeout", "http-error", "affected"}
        else set(queried_names[1:])
        if outcome == "partial"
        else set()
    )
    assert set(remaining) == expected_names
    assert counts == ({"high": 2} if outcome == "affected" else {})
    for name, row in remaining.items():
        if outcome == "affected":
            assert row["first_seen_at"] == 123.0
            assert row["last_seen_at"] > 123.0
            assert row["summary"] == "Updated alert"
        else:
            assert row == before[name]
    assert other.list_vulnerabilities() == peer_before
    assert sum(r.method == "POST" for r in requests) == 1
    incomplete = outcome in {"timeout", "http-error", "partial"}
    logs = [
        record.getMessage() for record in caplog.records if record.name == "mira.security.poller"
    ]
    assert any("incomplete" in message.lower() for message in logs) is incomplete
    if incomplete:
        assert not any("no open vulnerabilities" in message.lower() for message in logs)


@pytest.mark.asyncio
async def test_org_poll_preserves_alerts_for_failed_batch(polling_stores, monkeypatch, caplog):
    import logging
    from dataclasses import asdict

    store, _other = polling_stores
    names = [f"pkg{i:04d}" for i in range(1001)]
    store.replace_manifest_packages(
        "package-lock.json",
        [
            {"name": name, "kind": "npm", "version": "2.0.0", "file_path": "package-lock.json"}
            for name in names
        ],
    )
    for name in [names[0], names[-1]]:
        store.replace_vulnerabilities_for_package(
            name, "npm", "2.0.0", [{"cve_id": "SYNTHETIC-" + name, "severity": "high"}]
        )
    before = {r.package_name: asdict(r) for r in store.list_vulnerabilities()}
    batches = []

    def serve(request):
        assert request.method == "POST"
        queries = json.loads(request.content)["queries"]
        batches.append([q["package"]["name"] for q in queries])
        assert all(q["package"]["ecosystem"] == "npm" and q["version"] == "2.0.0" for q in queries)
        if len(batches) == 1:
            assert len(queries) == 1000
            return httpx.Response(200, json={"results": [{} for _ in queries]})
        assert len(batches) == 2 and len(queries) == 1
        return httpx.Response(503)

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(serve), **kwargs),
    )
    caplog.set_level(logging.INFO, logger="mira.security.poller")

    assert await poller.poll_org_wide() == {}

    assert [len(batch) for batch in batches] == [1000, 1]
    assert [name for batch in batches for name in batch] == names
    remaining = {r.package_name: asdict(r) for r in store.list_vulnerabilities()}
    assert remaining == {names[-1]: before[names[-1]]}
    logs = [r.getMessage() for r in caplog.records if r.name == "mira.security.poller"]
    assert any("incomplete" in message.lower() for message in logs)
    assert not any("no open vulnerabilities" in message.lower() for message in logs)
