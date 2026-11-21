"""Review metrics aggregate through the dashboard route with an isolated event store."""

import math
import sqlite3
from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest


@pytest.fixture
def timeseries_events(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", ":memory:")
    monkeypatch.setenv("ADMIN_PASSWORD", "offline-test-only")
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    from mira.dashboard.routers import core

    def load(dates):
        events = [
            SimpleNamespace(
                created_at=dt.timestamp(),
                comments_posted=1,
                blockers=0,
                warnings=1,
                suggestions=0,
                lines_changed=10,
                tokens_used=100,
                categories="correctness",
            )
            for dt in dates
        ]
        store = SimpleNamespace(
            iter_review_events=lambda: nullcontext(iter(events)), close=lambda: None
        )
        database = SimpleNamespace(
            list_repos=lambda: [SimpleNamespace(owner="example", repo="app", platform="github")]
        )
        monkeypatch.setattr(core._api, "_app_db", database)
        monkeypatch.setattr(core, "IndexStore", SimpleNamespace(open=lambda *a, **k: store))
        return core.get_timeseries

    return load


@pytest.mark.parametrize(
    "dates, expected",
    [
        ([(2024, 12, 30), (2025, 1, 1)], "2025-W01"),
        ([(2020, 12, 31), (2021, 1, 1)], "2020-W53"),
        ([(2018, 12, 31), (2019, 1, 1)], "2019-W01"),
    ],
)
def test_iso_week_spans_calendar_year(timeseries_events, dates, expected):
    get_timeseries = timeseries_events([datetime(*date, tzinfo=UTC) for date in dates])

    points = get_timeseries("week")

    assert len(points) == 1
    assert points[0].model_dump() == {
        "date": expected,
        "reviews": 2,
        "comments": 2,
        "blockers": 0,
        "warnings": 2,
        "suggestions": 0,
        "lines_changed": 20,
        "tokens_used": 200,
        "categories": {"correctness": 2},
    }


def test_distinct_iso_weeks_remain_separate(timeseries_events):
    get_timeseries = timeseries_events(
        [datetime(2025, 1, 8, tzinfo=UTC), datetime(2025, 1, 2, tzinfo=UTC)]
    )

    points = get_timeseries("week")

    assert [(p.date, p.reviews) for p in points] == [("2025-W01", 1), ("2025-W02", 1)]


@pytest.mark.parametrize(
    "period, expected",
    [
        ("day", ["2024-12-30", "2025-01-01"]),
        ("month", ["2024-12", "2025-01"]),
    ],
)
def test_day_and_month_keys_are_unchanged(timeseries_events, period, expected):
    get_timeseries = timeseries_events(
        [datetime(2024, 12, 30, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC)]
    )

    points = get_timeseries(period)

    assert [p.date for p in points] == expected
    assert [p.reviews for p in points] == [1, 1]


def test_empty_event_store(timeseries_events):
    get_timeseries = timeseries_events([])

    assert get_timeseries("week") == []


@pytest.fixture
def persisted_timeseries(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    from mira.dashboard import api
    from mira.dashboard.db import AppDatabase
    from mira.dashboard.routers import core

    database = AppDatabase(url="", admin_password="offline-test-only")
    monkeypatch.setattr(api, "_app_db", database)
    yield core, database
    database.close()


def test_timeseries_includes_persisted_history_before_per_repo_cap(persisted_timeseries):
    from mira.index.store import IndexStore

    core, database = persisted_timeseries
    database.register_repo("example", "app")
    store = IndexStore.open("example", "app")
    try:
        store.record_review(
            1,
            "Older review",
            "",
            comments_posted=7,
            blockers=2,
            warnings=3,
            suggestions=4,
            lines_changed=19,
            tokens_used=23,
            categories="older",
            created_at=datetime(2019, 1, 1, tzinfo=UTC).timestamp(),
        )
        for number in range(2, 502):
            store.record_review(
                number,
                "Newer review",
                "",
                comments_posted=1,
                blockers=0,
                warnings=0,
                created_at=datetime(2025, 1, 1, tzinfo=UTC).timestamp(),
            )
    finally:
        store.close()

    points = core.get_timeseries("day")

    assert [p.date for p in points] == ["2019-01-01", "2025-01-01"]
    assert points[0].model_dump() == {
        "date": "2019-01-01",
        "reviews": 1,
        "comments": 7,
        "blockers": 2,
        "warnings": 3,
        "suggestions": 4,
        "lines_changed": 19,
        "tokens_used": 23,
        "categories": {"older": 1},
    }
    assert points[1].reviews == 500


@pytest.mark.parametrize(
    "period,groups",
    [
        ("day", {"2024-12-30": [1], "2025-01-01": [2], "2025-01-08": [3], "2025-02-01": [4]}),
        ("week", {"2025-W01": [1, 2], "2025-W02": [3], "2025-W05": [4]}),
        ("month", {"2024-12": [1], "2025-01": [2, 3], "2025-02": [4]}),
        ("other", {"2024-12-30": [1], "2025-01-01": [2], "2025-01-08": [3], "2025-02-01": [4]}),
    ],
)
def test_persisted_metrics_and_python_bucket_semantics(persisted_timeseries, period, groups):
    from mira.index.store import IndexStore

    core, database = persisted_timeseries
    midnight = datetime(2025, 1, 1, tzinfo=UTC)
    rounded_midnight = math.nextafter(midnight.timestamp(), -math.inf)
    assert rounded_midnight < midnight.timestamp()
    assert datetime.fromtimestamp(rounded_midnight, tz=UTC) == midnight
    dates = [
        datetime(2024, 12, 30, tzinfo=UTC).timestamp(),
        rounded_midnight,
        datetime(2025, 1, 8, tzinfo=UTC).timestamp(),
        datetime(2025, 2, 1, tzinfo=UTC).timestamp(),
    ]
    categories = [
        " bug, security ,,bug, ",
        "\tsecurity,\u00a0style\u00a0,bug,bug",
        "security,STYLE",
        "",
    ]
    category_counts = [
        {"bug": 2, "security": 1},
        {"security": 1, "style": 1, "bug": 2},
        {"security": 1, "STYLE": 1},
        {},
    ]
    for i, (timestamp, cats) in enumerate(zip(dates, categories, strict=True), 1):
        platform = "gitlab" if i == 2 else "github"
        owner, repo = ("example", "app") if i < 3 else ("other", "app")
        database.register_repo(owner, repo, platform=platform)
        store = IndexStore.open(owner, repo, platform=platform)
        try:
            store.record_review(
                i,
                "Synthetic review",
                "",
                comments_posted=i * 2,
                blockers=i,
                warnings=i * 3,
                suggestions=i * 4,
                lines_changed=i * 5,
                tokens_used=i * 7,
                categories=cats,
                created_at=timestamp,
            )
        finally:
            store.close()

    expected = []
    for date, numbers in groups.items():
        counts = {}
        for number in numbers:
            for category, count in category_counts[number - 1].items():
                counts[category] = counts.get(category, 0) + count
        total = sum(numbers)
        expected.append(
            {
                "date": date,
                "reviews": len(numbers),
                "comments": total * 2,
                "blockers": total,
                "warnings": total * 3,
                "suggestions": total * 4,
                "lines_changed": total * 5,
                "tokens_used": total * 7,
                "categories": counts,
            }
        )
    assert [point.model_dump() for point in core.get_timeseries(period)] == expected


@pytest.mark.parametrize("invalid", ["timestamp", "categories"])
def test_bucket_errors_escape_and_close_the_store(persisted_timeseries, monkeypatch, invalid):
    from mira.index.store import IndexStore

    core, database = persisted_timeseries
    database.register_repo("example", "app")
    store = IndexStore.open("example", "app")
    store.record_review(1, "Synthetic review", "", 1, 0, 0, created_at=1)
    if invalid == "timestamp":
        store._conn.execute("UPDATE review_events SET created_at = ?", (math.inf,))
    else:
        store._conn.execute("UPDATE review_events SET categories = ?", (b"bug",))
    store._conn.commit()
    store.close()
    real_open = IndexStore.open
    opened = []

    def open_store(*args, **kwargs):
        result = real_open(*args, **kwargs)
        opened.append(result)
        return result

    monkeypatch.setattr(IndexStore, "open", open_store)
    with pytest.raises(
        (OverflowError, OSError, ValueError) if invalid == "timestamp" else TypeError
    ):
        core.get_timeseries()
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0]._conn.execute("SELECT 1")


def test_store_read_failure_is_best_effort_and_closes_stores(persisted_timeseries, monkeypatch):
    from mira.index.store import IndexStore

    core, database = persisted_timeseries
    for repo in ("broken", "healthy"):
        database.register_repo("example", repo)
        store = IndexStore.open("example", repo)
        store.record_review(1, "Synthetic review", "", 1, 0, 0, created_at=1)
        store.close()
    real_open = IndexStore.open
    opened = []

    def open_store(owner, repo, **kwargs):
        result = real_open(owner, repo, **kwargs)
        opened.append(result)
        if repo == "broken":
            result._conn.set_authorizer(lambda *_: sqlite3.SQLITE_DENY)
        return result

    monkeypatch.setattr(IndexStore, "open", open_store)
    points = core.get_timeseries()
    assert len(points) == 1
    assert points[0].reviews == 1
    assert len(opened) == 2
    for store in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            store._conn.execute("SELECT 1")
