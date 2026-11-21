"""/api/activity flattens review events across all repos, newest first, and
supports repo + search filtering."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from mira.dashboard import api
from mira.dashboard.db import AppDatabase
from mira.dashboard.routers import core
from mira.index.store import IndexStore


@pytest.fixture
def patched_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    db = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr(api, "_app_db", db)
    return db


def _seed(db: AppDatabase) -> None:
    db.register_repo("acme", "web")
    db.register_repo("acme", "api")

    web = IndexStore.open("acme", "web")
    web.record_review(
        pr_number=1,
        pr_title="Fix auth redirect loop",
        pr_url="https://github.com/acme/web/pull/1",
        comments_posted=3,
        blockers=1,
        warnings=2,
        suggestions=0,
        categories="bug,security",
        created_at=100.0,
    )
    web.record_review(
        pr_number=2,
        pr_title="Add dark mode",
        pr_url="https://github.com/acme/web/pull/2",
        comments_posted=1,
        blockers=0,
        warnings=0,
        suggestions=1,
        categories="style",
        created_at=300.0,
    )
    web.close()

    apirepo = IndexStore.open("acme", "api")
    apirepo.record_review(
        pr_number=7,
        pr_title="Rate limit login endpoint",
        pr_url="https://github.com/acme/api/pull/7",
        comments_posted=2,
        blockers=1,
        warnings=1,
        suggestions=0,
        categories="security,performance",
        created_at=200.0,
    )
    apirepo.close()


def test_lists_all_repos_newest_first(patched_db: AppDatabase):
    _seed(patched_db)
    out = core.list_activity()

    assert [(e.repo, e.pr_number) for e in out.events] == [
        ("web", 2),  # created_at 300
        ("api", 7),  # created_at 200
        ("web", 1),  # created_at 100
    ]
    # owner/repo are attached to each flattened event
    assert all(e.owner == "acme" for e in out.events)
    assert set(out.repos) == {"acme/web", "acme/api"}


def test_repo_filter_narrows_results(patched_db: AppDatabase):
    _seed(patched_db)
    out = core.list_activity(repo="acme/api")
    assert [e.pr_number for e in out.events] == [7]
    # repo dropdown still lists every repo, not just the filtered one
    assert set(out.repos) == {"acme/web", "acme/api"}


def test_search_matches_title_repo_and_category(patched_db: AppDatabase):
    _seed(patched_db)

    by_title = core.list_activity(q="dark")
    assert [e.pr_number for e in by_title.events] == [2]

    by_category = core.list_activity(q="security")
    assert {e.pr_number for e in by_category.events} == {1, 7}

    by_repo = core.list_activity(q="api")
    assert [e.pr_number for e in by_repo.events] == [7]


def test_search_ands_multiple_terms(patched_db: AppDatabase):
    _seed(patched_db)
    # "security" matches PRs 1 and 7; adding "web" narrows to just PR 1.
    out = core.list_activity(q="security web")
    assert [e.pr_number for e in out.events] == [1]


def test_search_finds_matching_review_below_newest_500(patched_db: AppDatabase):
    patched_db.register_repo("acme", "web")
    store = IndexStore.open("acme", "web")
    try:
        for number in range(501):
            store.record_review(
                pr_number=number,
                pr_title="archivedneedle" if number == 0 else "Unrelated review",
                pr_url=f"https://example.invalid/acme/web/pull/{number}",
                comments_posted=0,
                blockers=0,
                warnings=0,
                suggestions=0,
                created_at=float(number),
            )
    finally:
        store.close()

    result = core.list_activity(q="archivedneedle")
    assert [(event.pr_number, event.pr_title) for event in result.events] == [(0, "archivedneedle")]


def _record_search_review(store, number, title, created_at, categories=""):
    return store.record_review(
        pr_number=number,
        pr_title=title,
        pr_url=f"https://example.invalid/reviews/{number}",
        comments_posted=0,
        blockers=0,
        warnings=0,
        created_at=created_at,
        categories=categories,
    )


@pytest.mark.parametrize(
    "title,query,matches",
    [
        ("İstanbul release", "i\u0307stanbul", True),
        ("İstanbul release", "istanbul", False),
        ("Straße", "STRASSE", False),
        ("Straße", "STRAẞE", True),
        ("Σigma", "σIGMA", True),
        ("ΟΣ", "ος", True),
        ("ΟΣ", "οσ", False),
        ("Kelvin", "kelvin", True),
        ("literal %_ [x]", "%_ [x]", True),
        ("literal only", "%", False),
        ("literal only", "' OR 1=1 --", False),
    ],
)
def test_search_preserves_python_lower_and_literal_terms(patched_db, title, query, matches):
    patched_db.register_repo("acme", "web")
    store = IndexStore.open("acme", "web")
    try:
        _record_search_review(store, 42, title, 1.0)
    finally:
        store.close()
    assert [event.pr_number for event in core.list_activity(q=query).events] == (
        [42] if matches else []
    )


def test_search_terms_span_fields_and_split_on_python_whitespace(patched_db):
    patched_db.register_repo("acme", "web")
    store = IndexStore.open("acme", "web")
    try:
        _record_search_review(store, 42, "Release pipeline", 1.0, "SeCuRiTy,style")
    finally:
        store.close()

    result = core.list_activity(q=" \tReLeAsE\n#42\u2003acme/web\tsecurity ")
    assert [event.pr_number for event in result.events] == [42]


def test_search_retains_500_matching_events_per_repo_and_closes_early(patched_db, monkeypatch):
    patched_db.register_repo("acme", "web")
    store = IndexStore.open("acme", "web")
    try:
        for number in range(1001):
            _record_search_review(
                store, number, "needle" if number <= 500 else "Unrelated", float(number)
            )
    finally:
        store.close()
    real_open = IndexStore.open
    read_numbers = []
    exited = []
    opened = []

    # Observe the real stream so early termination and context cleanup are visible.
    class ObservedStore:
        def __init__(self, real_store):
            self.store = real_store

        @contextmanager
        def iter_review_events(self):
            try:
                with self.store.iter_review_events() as events:

                    def observe():
                        for event in events:
                            read_numbers.append(event.pr_number)
                            yield event

                    yield observe()
            finally:
                exited.append(True)

        def close(self):
            self.store.close()

    def open_store(*args, **kwargs):
        actual = real_open(*args, **kwargs)
        opened.append(actual)
        return ObservedStore(actual)

    monkeypatch.setattr(IndexStore, "open", open_store)
    result = core.list_activity(q="needle", limit=1000)
    assert [event.pr_number for event in result.events] == list(range(500, 0, -1))
    assert read_numbers == list(range(1000, 0, -1))
    assert exited == [True]
    for actual in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            actual.list_review_events()


@pytest.mark.parametrize("query", ["", " \t\n"])
def test_unfiltered_activity_keeps_recent_500_fast_path(patched_db, monkeypatch, query):
    patched_db.register_repo("acme", "web")
    store = IndexStore.open("acme", "web")
    try:
        for number in range(501):
            _record_search_review(store, number, "Review", float(number))
    finally:
        store.close()

    def unexpected_stream(self):
        pytest.fail("An empty search must retain the existing recent-events read")

    monkeypatch.setattr(IndexStore, "iter_review_events", unexpected_stream)
    result = core.list_activity(q=query, limit=1000)
    assert [event.pr_number for event in result.events] == list(range(500, 0, -1))


@pytest.mark.parametrize("query", ["", "security"])
@pytest.mark.parametrize("limit", [0, -1])
def test_activity_preserves_global_zero_and_negative_limit_slicing(patched_db, query, limit):
    _seed(patched_db)
    expected = [2, 7] if not query else [7]
    if limit == 0:
        expected = []
    assert [
        event.pr_number for event in core.list_activity(q=query, limit=limit).events
    ] == expected


def test_search_repo_filter_preserves_platform_selection_and_dropdown(patched_db):
    for platform in ("github", "gitlab"):
        patched_db.register_repo("acme", "web", platform=platform)
        store = IndexStore.open("acme", "web", platform=platform)
        try:
            _record_search_review(store, 1 if platform == "github" else 2, "needle", 1.0)
        finally:
            store.close()
    patched_db.register_repo("other", "app")
    store = IndexStore.open("other", "app")
    try:
        _record_search_review(store, 3, "needle", 3.0)
    finally:
        store.close()

    result = core.list_activity(q="needle", repo="acme/web")
    assert {event.pr_number for event in result.events} == {1, 2}
    assert result.repos == ["acme/web", "acme/web", "other/app"]


def test_search_closes_failed_repo_stream_and_continues(patched_db, monkeypatch):
    for name, number in (("broken", 1), ("healthy", 2)):
        patched_db.register_repo("acme", name)
        store = IndexStore.open("acme", name)
        try:
            _record_search_review(store, number, "needle", float(number))
        finally:
            store.close()
    real_open = IndexStore.open
    stream_exits = []
    closed = []

    class FaultingStore:
        def __init__(self, actual, repo):
            self.actual = actual
            self.repo = repo

        @contextmanager
        def iter_review_events(self):
            try:
                with self.actual.iter_review_events() as events:

                    def rows():
                        for event in events:
                            yield event
                            if self.repo == "broken":
                                # Inject an in-flight DB failure after a real persisted row.
                                raise sqlite3.OperationalError("synthetic interrupted read")

                    yield rows()
            finally:
                stream_exits.append(self.repo)

        def close(self):
            self.actual.close()
            closed.append(self.repo)

    def open_store(owner, repo, **kwargs):
        return FaultingStore(real_open(owner, repo, **kwargs), repo)

    monkeypatch.setattr(IndexStore, "open", open_store)
    result = core.list_activity(q="needle")
    assert [event.pr_number for event in result.events] == [2, 1]
    assert sorted(stream_exits) == ["broken", "healthy"]
    assert sorted(closed) == ["broken", "healthy"]


def test_search_merges_globally_and_keeps_stable_repo_order_for_timestamp_ties(
    patched_db, monkeypatch
):
    for repo, rows in (
        ("first", [(10, 100.0), (11, 100.0), (40, 50.0)]),
        ("second", [(20, 100.0), (30, 200.0)]),
    ):
        patched_db.register_repo("acme", repo)
        store = IndexStore.open("acme", repo)
        try:
            for number, timestamp in rows:
                _record_search_review(store, number, "needle", timestamp)
        finally:
            store.close()

    # The registry is the route's enumeration boundary; make that order explicit.
    records = {record.repo: record for record in patched_db.list_repos()}
    monkeypatch.setattr(patched_db, "list_repos", lambda: [records["second"], records["first"]])
    assert [event.pr_number for event in core.list_activity(q="needle").events] == [
        30,
        20,
        10,
        11,
        40,
    ]
    assert [event.pr_number for event in core.list_activity(q="needle", limit=3).events] == [
        30,
        20,
        10,
    ]
