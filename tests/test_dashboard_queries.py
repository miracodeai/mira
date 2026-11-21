"""aggregate_org_stats must release every store it opens, even when reading
a repo's stats fails part-way (the failure is logged and the loop goes on)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mira.dashboard import queries
from mira.dashboard.db import AppDatabase, RepoRecord


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    return AppDatabase(url="", admin_password="admin")


class _FailingStore:
    def __init__(self) -> None:
        self.closed = False

    def all_paths(self) -> list[str]:
        raise RuntimeError("index unreadable")

    def get_review_stats(self, since: float | None = None) -> dict:  # pragma: no cover
        raise AssertionError("not reached")

    def close(self) -> None:
        self.closed = True


def test_store_is_closed_when_reading_stats_fails(
    db: AppDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[_FailingStore] = []

    def fake_open(owner: str, repo: str, platform: str = "github") -> _FailingStore:
        store = _FailingStore()
        opened.append(store)
        return store

    monkeypatch.setattr(queries.IndexStore, "open", staticmethod(fake_open))
    repos = [RepoRecord(owner="acme", repo="web"), RepoRecord(owner="acme", repo="api")]

    stats = queries.aggregate_org_stats(repos)

    # Both repos were attempted, both stores released, and the failure didn't
    # abort the aggregate.
    assert [s.closed for s in opened] == [True, True]
    assert stats.total_repos == 2
    assert stats.total_files == 0
