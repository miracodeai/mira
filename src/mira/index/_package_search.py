"""Bounded grouping for org-wide package search without changing backend order."""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from mira.index.manifests import _is_lockfile_path

_GroupKey = tuple[str, str, str, str]
_Rank = tuple[Any, ...]


@dataclass(frozen=True)
class _WorstGroup:
    rank: _Rank
    key: _GroupKey

    def __lt__(self, other: _WorstGroup) -> bool:
        return self.rank > other.rank


_Checkpoint = tuple[dict[_GroupKey, tuple[_Rank, dict]], list[_WorstGroup], int]


class PackageGroups:
    """Retain at most ``limit`` groups and one heap entry per retained group.

    SQLite needs its Python stable-sort rank; PostgreSQL supplies its existing
    SQL order, so first encounter defines the rank. Discarded groups cannot
    re-enter: later encounters only worsen their rank while the threshold
    improves. Winner changes never change ranks or add heap entries.
    """

    def __init__(self, limit: int, *, postgres_order: bool = False) -> None:
        if limit < 1:
            raise ValueError("Package group limit must be positive")
        self._limit = limit
        self._postgres_order = postgres_order
        self._groups: dict[_GroupKey, tuple[_Rank, dict]] = {}
        self._heap: list[_WorstGroup] = []
        self._ordinal = 0

    def add(self, row: dict) -> None:
        ordinal = self._ordinal
        self._ordinal += 1
        name = row["name"].lower()
        key = (row["owner"], row["repo"], row["kind"], name)
        existing = self._groups.get(key)
        if existing is not None:
            first_rank, winner = existing
            if _is_lockfile_path(row.get("file_path", "")) and not _is_lockfile_path(
                winner.get("file_path", "")
            ):
                self._groups[key] = first_rank, row
            return
        rank = (ordinal,) if self._postgres_order else (name, row["owner"], row["repo"], ordinal)
        entry = _WorstGroup(rank, key)
        if len(self._groups) < self._limit:
            self._groups[key] = rank, row
            heapq.heappush(self._heap, entry)
        elif rank < self._heap[0].rank:
            evicted = heapq.heapreplace(self._heap, entry)
            del self._groups[evicted.key]
            self._groups[key] = rank, row

    def checkpoint(self) -> _Checkpoint:
        """Save bounded state so a partially read SQLite store can be skipped."""
        return self._groups.copy(), self._heap.copy(), self._ordinal

    def restore(self, checkpoint: _Checkpoint) -> None:
        self._groups, self._heap, self._ordinal = checkpoint

    def finish(self) -> list[dict]:
        return [row for _rank, row in sorted(self._groups.values(), key=lambda item: item[0])]


def matching_sqlite_packages(
    rows: Iterable[tuple],
    name: str | None = None,
    version: str | None = None,
    kind: str | None = None,
    is_dev: bool | None = None,
) -> Iterator[tuple]:
    """Keep SQLite's literal substring matching before group selection."""
    name_l = name.lower() if name else None
    version_l = version.lower() if version else None
    for row in rows:
        r_name, r_kind, r_version, _r_file, r_dev = row
        if name_l and name_l not in r_name.lower():
            continue
        if version_l and version_l not in r_version.lower():
            continue
        if kind and r_kind != kind:
            continue
        if is_dev is not None and bool(r_dev) != is_dev:
            continue
        yield row
