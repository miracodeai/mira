import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from mira.config import MiraConfig
from mira.dashboard import models_config
from mira.index import indexer
from mira.index.store import FileSummary, IndexStore
from mira.platforms import index_handlers
from mira.platforms.fetch import IncompleteTreeError
from mira.platforms.gitlab import webhook

OLDER = "a" * 40
CURRENT = "b" * 40


class Fetcher:
    def __init__(self, files, revision=CURRENT, pause=None):
        self.files = files
        self.revision = revision
        self.pause = pause
        self.refs = []

    async def branch_head(self, owner, repo, branch):
        assert branch == "main"
        if self.pause is not None:
            entered, release = self.pause
            entered.set()
            await release.wait()
        return self.revision

    async def repo_tree(self, owner, repo, ref):
        self.refs.append(ref)
        if self.files is None:
            raise IncompleteTreeError("Incomplete synthetic tree")
        return list(self.files)

    async def repo_tarball(self, owner, repo, ref, **kwargs):
        self.refs.append(ref)
        return self.files

    async def file_content(self, owner, repo, path, ref, **kwargs):
        self.refs.append(ref)
        return self.files.get(path)


class Model:
    config = SimpleNamespace(model="synthetic-model")

    async def complete(self, messages, **kwargs):
        return json.dumps(
            {
                "files": [{"path": "keep.py", "summary": "current"}],
                "summary": "Current directory",
                "directories": [{"path": "(root)", "summary": "Current directory"}],
            }
        )


@pytest.fixture
def index_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", "")
    app_db = Mock()
    app_db.get_repo.return_value.status = "ready"
    monkeypatch.setattr(index_handlers, "_get_app_db", lambda: app_db)
    monkeypatch.setattr(index_handlers, "load_config", MiraConfig)
    monkeypatch.setattr(index_handlers, "create_llm", lambda _: Model())
    monkeypatch.setattr(models_config, "llm_config_for", lambda purpose, llm: llm)
    monkeypatch.setattr(indexer, "_queue_vulnerability_scan", lambda *args: None)
    store = IndexStore.open("example", "repo", platform="gitlab")
    try:
        store.upsert_summary(FileSummary("keep.py", "python", "previous"))
        store.upsert_summary(FileSummary("gone.py", "python", "previous"))
        yield store
    finally:
        store.close()


def current_files():
    return {"keep.py": "def current():\n    return 2\n" + "# padding\n" * 80}


async def push(fetcher, removed, changed=None):
    await index_handlers.run_incremental_index(
        "example", "repo", fetcher, changed or [], removed, "main", platform="gitlab"
    )


async def test_delayed_removal_payload_uses_current_tree(index_store, monkeypatch):
    fetcher = Fetcher(current_files())
    monkeypatch.setattr(webhook, "make_fetcher", lambda *args: fetcher)
    await webhook.handle_gitlab_push(
        {
            "project": {"path_with_namespace": "example/repo", "default_branch": "main"},
            "ref": "refs/heads/main",
            "after": OLDER,
            "commits": [{"removed": ["keep.py", "gone.py"]}],
        },
        SimpleNamespace(get_token=AsyncMock(return_value="synthetic-token")),
        "fixture-bot",
    )
    assert index_store.all_paths() == {"keep.py"}
    assert index_store.get_summary("keep.py").summary == "current"
    assert fetcher.refs == [CURRENT, CURRENT]


@pytest.mark.parametrize("removed_count", [1, 51])
async def test_incomplete_tree_indexes_changes_and_keeps_recovery_pending(
    index_store, monkeypatch, removed_count
):
    fetcher = Fetcher(current_files())
    fetcher.repo_tree = AsyncMock(side_effect=IncompleteTreeError("Incomplete synthetic tree"))
    removed = ["gone.py", *(f"removed-{n}.py" for n in range(removed_count - 1))]
    await push(fetcher, removed, ["keep.py"])
    assert index_store.all_paths() == {"keep.py", "gone.py"}
    assert index_store.get_summary("keep.py").summary == "current"
    assert fetcher.refs == [CURRENT]
    assert index_store.claim_index_run().needs_reconciliation
    index_handlers._get_app_db().set_repo_status.assert_not_called()

    # A subsequent changed-only push still makes progress while the tree is unavailable.
    with monkeypatch.context() as patch:
        patch.setattr(
            Model,
            "complete",
            AsyncMock(return_value='{"files":[{"path":"keep.py","summary":"later"}]}'),
        )
        await push(fetcher, [], ["keep.py"])
    assert index_store.get_summary("keep.py").summary == "later"
    assert index_store.all_paths() == {"keep.py", "gone.py"}
    assert index_store.claim_index_run().needs_reconciliation
    index_handlers._get_app_db().set_repo_status.assert_not_called()

    # When tree acquisition works again, recovery removes the absent path.
    await push(Fetcher(current_files()), [], ["keep.py"])
    assert index_store.all_paths() == {"keep.py"}
    assert not index_store.claim_index_run().needs_reconciliation


async def test_branch_lookup_http_error_still_indexes_changed_files(index_store):
    request = httpx.Request("GET", "https://provider.example.invalid/branch")
    fetcher = Fetcher(current_files())
    fetcher.branch_head = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "Unavailable", request=request, response=httpx.Response(503, request=request)
        )
    )
    await push(fetcher, ["gone.py"], ["keep.py"])
    assert index_store.all_paths() == {"keep.py", "gone.py"}
    assert index_store.get_summary("keep.py").summary == "current"
    assert fetcher.refs == ["main"]
    assert index_store.claim_index_run().needs_reconciliation


async def test_large_push_indexes_the_pinned_tree(index_store):
    fetcher = Fetcher(current_files())
    await push(fetcher, [f"removed-{n}.py" for n in range(51)])
    assert index_store.all_paths() == {"keep.py"}
    assert fetcher.refs and set(fetcher.refs) == {CURRENT}


async def test_delayed_head_lookup_cannot_delete_newer_index(index_store):
    entered, release = asyncio.Event(), asyncio.Event()
    older = Fetcher({}, revision=OLDER, pause=(entered, release))
    older_run = asyncio.create_task(push(older, ["keep.py"]))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        newer = Fetcher(current_files())
        await push(newer, ["gone.py"])
    finally:
        release.set()
        await asyncio.wait_for(older_run, 3)
    assert index_store.all_paths() == {"keep.py"}
    assert index_store.get_summary("keep.py").summary == "current"
    assert newer.refs and set(newer.refs) == {CURRENT}
    assert not index_store.claim_index_run().needs_reconciliation
