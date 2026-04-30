"""Tests for cancellable indexing."""

from __future__ import annotations

import pytest

from mira.index.indexer import IndexingCancelled
from mira.index.status import IndexingTracker


class TestIndexingTracker:
    def test_request_cancel_on_active_job(self):
        t = IndexingTracker()
        t.start("acme/web")
        assert t.request_cancel("acme/web") is True
        assert t.is_cancel_requested("acme/web") is True

    def test_request_cancel_no_job(self):
        t = IndexingTracker()
        assert t.request_cancel("acme/web") is False

    def test_request_cancel_completed_job_noop(self):
        t = IndexingTracker()
        t.start("acme/web")
        t.complete("acme/web", 10)
        assert t.request_cancel("acme/web") is False

    def test_cancel_transitions_status(self):
        t = IndexingTracker()
        t.start("acme/web")
        t.request_cancel("acme/web")
        t.cancel("acme/web", 5)
        jobs = t.get_all()
        assert len(jobs) == 1
        assert jobs[0].status == "cancelled"
        assert jobs[0].files_done == 5

    def test_cancelled_job_is_not_active(self):
        t = IndexingTracker()
        t.start("acme/web")
        t.cancel("acme/web", 0)
        assert t.get_active() == []

    def test_is_cancel_requested_default_false(self):
        t = IndexingTracker()
        t.start("acme/web")
        assert t.is_cancel_requested("acme/web") is False


class TestIndexingCancelledException:
    def test_carries_partial_count(self):
        exc = IndexingCancelled(42)
        assert exc.files_indexed == 42
        assert "42" in str(exc)


@pytest.mark.asyncio
async def test_index_repo_raises_on_cancel(tmp_path, monkeypatch):
    """index_repo raises IndexingCancelled when cancel_check returns True."""
    from unittest.mock import AsyncMock

    from mira.config import MiraConfig
    from mira.index import indexer as idx_mod
    from mira.index.indexer import index_repo
    from mira.index.store import IndexStore

    # Stub out the network-dependent helpers.
    async def fake_branch(*a, **kw):
        return "main"

    async def fake_tree(*a, **kw):
        return ["a.py", "b.py", "c.py", "d.py"]

    # Larger than the trivial-file threshold so each file routes through the
    # LLM batch path (where the cancel check fires). A trivial file would
    # bypass the loop and skip the cancellation point.
    _content_pad = "# pad line\n" * 80

    async def fake_fetch(owner, repo, path, token, ref, semaphore):
        return f"# contents of {path}\n{_content_pad}"

    async def fake_tarball(owner, repo, token, ref="main"):
        return {p: f"# contents of {p}\n{_content_pad}" for p in ["a.py", "b.py", "c.py", "d.py"]}

    async def fake_summarize_batch(batch, llm, sem):
        return [
            (path, content, {"summary": "x", "symbols": [], "imports": []})
            for path, content in batch
        ]

    async def fake_summarize_dirs(store, llm, sem):
        return None

    monkeypatch.setattr(idx_mod, "_fetch_default_branch", fake_branch)
    monkeypatch.setattr(idx_mod, "_fetch_repo_tree", fake_tree)
    monkeypatch.setattr(idx_mod, "_fetch_file_content", fake_fetch)
    monkeypatch.setattr(idx_mod, "_fetch_repo_tarball", fake_tarball)
    monkeypatch.setattr(idx_mod, "_summarize_batch", fake_summarize_batch)
    monkeypatch.setattr(idx_mod, "_summarize_directories", fake_summarize_dirs)
    # Force batch size of 1 so we can cancel between files deterministically.
    monkeypatch.setattr(idx_mod, "_BATCH_SIZE", 1)

    store = IndexStore(str(tmp_path / "t.db"))

    calls = {"count": 0}

    def cancel_after_first() -> bool:
        calls["count"] += 1
        return calls["count"] > 1  # allow first batch, cancel before second

    config = MiraConfig()
    llm = AsyncMock()

    with pytest.raises(IndexingCancelled) as excinfo:
        await index_repo(
            owner="a",
            repo="b",
            token="t",
            config=config,
            store=store,
            llm=llm,
            full=False,
            branch="main",
            cancel_check=cancel_after_first,
        )

    # First batch processed (1 file) before cancel kicked in.
    assert excinfo.value.files_indexed == 1
    store.close()
