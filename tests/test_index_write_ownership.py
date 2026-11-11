"""Reproduce overlapping index runs with synthetic provider and model inputs."""

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Barrier
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest

from mira.config import LLMConfig, MiraConfig
from mira.index import pg_store
from mira.index.indexer import IndexingRecoveryPending, index_diff, index_repo
from mira.index.pg_store import PgIndexStore
from mira.index.store import DirectorySummary, FileSummary, IndexingSuperseded, IndexStore


@pytest.fixture
def postgres_url(monkeypatch):
    url = os.environ.get("MIRA_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set MIRA_TEST_POSTGRES_URL to a disposable PostgreSQL database")
    psycopg = pytest.importorskip("psycopg")
    from psycopg import sql

    schema = "index_owner_" + uuid4().hex
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query))
    params["options"] = f"-c search_path={schema} -c statement_timeout=5000"
    scoped = urlunsplit(parts._replace(query=urlencode(params, quote_via=quote)))
    monkeypatch.setattr(pg_store, "_pg_conn", None)
    monkeypatch.setattr(pg_store, "_schema_initialized", False)
    try:
        yield scoped
    finally:
        pg_store._drop_pg_conn()
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture(params=["sqlite", "postgres"])
def stores(request, tmp_path):
    if request.param == "sqlite":
        with ExitStack() as stack:
            handles = [IndexStore(str(tmp_path / "index.db")) for _ in range(3)]
            for handle in handles:
                stack.callback(handle.close)
            yield handles
    else:
        url = request.getfixturevalue("postgres_url")
        yield [PgIndexStore("example", "repo", url) for _ in range(3)]


def _code(version):
    return f'def version():\n    return "{version}"\n' + "# sample padding\n" * 80


class Fetcher:
    def __init__(self, files, pause=None, *, file_pause=None):
        self.files = files
        self.pause = pause
        self.file_pause = file_pause
        self.tree_calls = 0

    async def repo_tree(self, *args):
        self.tree_calls += 1
        if self.pause is not None:
            entered, release = self.pause
            entered.set()
            await release.wait()
        return list(self.files)

    async def repo_tarball(self, *args, **kwargs):
        return self.files

    async def file_content(self, owner, repo, path, branch, **kwargs):
        if self.file_pause is not None:
            entered, release = self.file_pause
            entered.set()
            await release.wait()
        return self.files.get(path)


class Model:
    config = LLMConfig(model="synthetic-offline-model")

    def __init__(self, version, paths, pause=None, *, directory_pause=None):
        self.version = version
        self.paths = paths
        self.pause = pause
        self.directory_pause = directory_pause

    async def complete(self, messages, **kwargs):
        if messages[-1]["content"] == "Summarize the files above.":
            if self.pause is not None:
                entered, release = self.pause
                entered.set()
                await release.wait()
            return json.dumps(
                {
                    "files": [
                        {"path": path, "language": "python", "summary": self.version}
                        for path in self.paths
                    ]
                }
            )
        if self.directory_pause is not None:
            entered, release = self.directory_pause
            entered.set()
            await release.wait()
        return json.dumps(
            {
                "summary": self.version,
                "directories": [{"path": "(root)", "summary": self.version}],
            }
        )


async def _run(index, store, fetcher, model, **kwargs):
    return await index(
        owner="example",
        repo="repo",
        config=MiraConfig(),
        store=store,
        llm=model,
        branch="main",
        fetcher=fetcher,
        **kwargs,
    )


@pytest.mark.parametrize("older_run", ["summary", "deletion", "manual_reset", "directory"])
async def test_newer_index_survives_delayed_older_writer(stores, older_run):
    old_store, new_store, observer = stores
    old_paused, release_old = asyncio.Event(), asyncio.Event()
    pause = (old_paused, release_old)
    if older_run in {"summary", "directory"}:
        model = Model(
            "OLD",
            ["keep.py"],
            pause if older_run == "summary" else None,
            directory_pause=pause if older_run == "directory" else None,
        )
        operation = _run(
            index_diff,
            old_store,
            Fetcher({"keep.py": _code("OLD")}),
            model,
            changed_paths=["keep.py"],
        )
    else:
        operation = _run(
            index_repo,
            old_store,
            Fetcher({}, pause),
            Model("OLD", []),
            reset=older_run == "manual_reset",
        )
    old_task = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(old_paused.wait(), 5)
        new_files = {"keep.py": _code("NEW"), "other.py": _code("NEW")}
        current = Fetcher(new_files)
        await _run(
            index_diff,
            new_store,
            current,
            Model("NEW", list(new_files)),
            changed_paths=["other.py"]
            if older_run in {"summary", "directory"}
            else list(new_files),
        )
    finally:
        release_old.set()
        (outcome,) = await asyncio.wait_for(asyncio.gather(old_task, return_exceptions=True), 5)

    assert observer.all_paths() == {"keep.py", "other.py"}
    assert observer.get_summary("keep.py").summary == "NEW"
    assert observer.get_directory_summary("").summary == "NEW"
    assert current.tree_calls == 1
    assert isinstance(outcome, IndexingSuperseded)


def _manifest(version):
    return json.dumps({"dependencies": {"example": version}})


async def test_delayed_manifest_cannot_replace_newer_inventory(stores):
    old, new, observer = stores
    entered, release = asyncio.Event(), asyncio.Event()
    path = "package.json"
    operation = _run(
        index_diff,
        old,
        Fetcher({path: _manifest("1.0.0")}, file_pause=(entered, release)),
        Model("OLD", []),
        changed_paths=[path],
    )
    stale = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await _run(index_repo, new, Fetcher({path: _manifest("2.0.0")}), Model("NEW", []))
    finally:
        release.set()
        (outcome,) = await asyncio.wait_for(asyncio.gather(stale, return_exceptions=True), 5)
    assert isinstance(outcome, IndexingSuperseded)
    assert [p.version for p in observer.list_manifest_packages()] == ["2.0.0"]


async def test_delayed_incremental_removals_cannot_delete_accepted_rows(stores):
    old, new, observer = stores
    old_run = old.claim_index_run()
    files = {"keep.py": _code("NEW"), "package.json": _manifest("2.0.0")}
    await _run(index_repo, new, Fetcher(files), Model("NEW", ["keep.py"]))
    with pytest.raises(IndexingSuperseded):
        await _run(
            index_diff, old, Fetcher({}), Model("OLD", []), run=old_run, removed_paths=list(files)
        )
    assert observer.get_summary("keep.py").summary == "NEW"
    assert [p.version for p in observer.list_manifest_packages()] == ["2.0.0"]


async def test_abandoned_run_recovers_paths_outside_next_push(stores):
    old, new, observer = stores
    old.claim_index_run()  # Simulate a worker exiting before completing.
    recovered_files = {"a.py": _code("NEW"), "b.py": _code("NEW")}
    recovery = Fetcher(recovered_files)
    await _run(
        index_diff, new, recovery, Model("NEW", list(recovered_files)), changed_paths=["b.py"]
    )
    assert recovery.tree_calls == 1
    assert observer.all_paths() == set(recovered_files)
    ordinary = Fetcher({"a.py": _code("NEXT")})
    await _run(index_diff, new, ordinary, Model("NEXT", ["a.py"]), changed_paths=["a.py"])
    assert ordinary.tree_calls == 0
    assert observer.get_summary("a.py").summary == "NEXT"
    assert observer.get_summary("b.py").summary == "NEW"


def test_failed_batch_rolls_back_index_writes(stores):
    writer, _, observer = stores
    writer.upsert_summary(FileSummary("keep.py", "python", "before"))
    writer.upsert_directory(DirectorySummary("", "before", 1))
    run = writer.claim_index_run()
    with pytest.raises(RuntimeError, match="abort batch"), writer.index_write(run):
        writer.remove_paths(["keep.py"])
        writer.upsert_summary(FileSummary("new.py", "python", "after"))
        writer.upsert_directory(DirectorySummary("", "after", 1))
        writer.replace_manifest_packages(
            "package.json",
            [
                {
                    "name": "example",
                    "kind": "npm",
                    "version": "2.0.0",
                    "file_path": "package.json",
                }
            ],
        )
        raise RuntimeError("abort batch")
    assert observer.all_paths() == {"keep.py"}
    assert observer.get_summary("keep.py").summary == "before"
    assert observer.get_directory_summary("").summary == "before"
    assert observer.list_manifest_packages() == []


@pytest.mark.parametrize("missing", ["file", "summary", "manifest"])
async def test_incomplete_recovery_keeps_next_push_on_full_scan(stores, missing):
    old, new, observer = stores
    old.claim_index_run()
    lockfile = "package-lock.json"
    files = {
        "a.py": _code("NEW"),
        "b.py": _code("NEW"),
        lockfile: '{"packages":{"node_modules/example":{"version":"2.0.0"}}}',
    }
    incomplete_files = dict(files)
    model_paths = ["a.py", "b.py"]
    if missing == "file":
        incomplete_files["a.py"] = None
    elif missing == "summary":
        model_paths.remove("a.py")
    else:
        incomplete_files[lockfile] = None

    with pytest.raises(IndexingRecoveryPending):
        await _run(
            index_diff,
            new,
            Fetcher(incomplete_files),
            Model("NEW", model_paths),
            changed_paths=["b.py"],
        )
    retry = Fetcher(files)
    await _run(index_diff, new, retry, Model("NEW", ["a.py", "b.py"]), changed_paths=["b.py"])
    assert retry.tree_calls == 1
    assert observer.all_paths() == {"a.py", "b.py"}
    assert [p.version for p in observer.list_manifest_packages()] == ["2.0.0"]


def test_postgres_status_and_conventions_share_index_transaction(postgres_url):
    from mira.dashboard.db import AppDatabase

    app = AppDatabase(postgres_url, admin_password="synthetic-offline-only")
    try:
        assert app._backend == "postgres"
        app.register_repo("example", "repo")
        writer = PgIndexStore("example", "repo", postgres_url)
        run = writer.claim_index_run()
        with pytest.raises(RuntimeError, match="abort batch"), writer.index_write(run):
            writer.upsert_summary(FileSummary("a.py", "python", "changed"))
            app.set_repo_status("example", "repo", "ready", files_indexed=1)
            app.set_repo_conventions("example", "repo", "new rules")
            raise RuntimeError("abort batch")
        repo = app.get_repo("example", "repo")
        assert (repo.status, repo.files_indexed, repo.conventions) == ("pending", 0, "")
        assert writer.all_paths() == set()
    finally:
        app.close()


def test_postgres_two_workers_can_claim_on_first_use(postgres_url):
    stores = [PgIndexStore("example", "repo", postgres_url) for _ in range(2)]
    gate = Barrier(2, timeout=5)

    def claim(store):
        gate.wait()
        return store.claim_index_run()

    with ThreadPoolExecutor(2) as pool:
        runs = list(pool.map(claim, stores))
    older, newer = sorted(runs, key=lambda run: run.needs_reconciliation)
    assert not older.needs_reconciliation and newer.needs_reconciliation
    with pytest.raises(IndexingSuperseded), stores[0].index_write(older):
        pytest.fail("Old owner entered a write")
    with stores[1].index_write(newer):
        stores[1].upsert_summary(FileSummary("a.py", "python", "current"))
    assert stores[0].get_summary("a.py").summary == "current"
