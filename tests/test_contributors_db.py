"""Tests for contributor analytics: AppDatabase storage, IndexStore review-quality
attribution, and the contributors API endpoints."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mira.dashboard import api
from mira.dashboard.db import AppDatabase, _epoch_to_day
from mira.index.store import IndexStore


@pytest.fixture
def db(tmp_path: Path) -> AppDatabase:
    return AppDatabase(url=str(tmp_path / "app.db"), admin_password="admin")


# ── AppDatabase contributor storage ──


def test_record_contribution_is_idempotent_and_rolls_up_once(db: AppDatabase) -> None:
    ts = 1_700_000_000.0
    first = db.record_contribution_for_login(
        "github", "alice", "o", "r", "pr_opened", "pr:1", event_at=ts, additions=10
    )
    second = db.record_contribution_for_login(
        "github", "alice", "o", "r", "pr_opened", "pr:1", event_at=ts, additions=99
    )
    assert first is True
    assert second is False  # duplicate key → no new row

    contributor = db.get_contributor_by_login("github", "alice")
    assert contributor is not None
    days = db.get_contributor_days(contributor.id, "2000-01-01", "2100-01-01")
    assert len(days) == 1
    # Rollup counted the event exactly once despite two writes.
    assert days[0].prs_opened == 1
    assert days[0].total == 1
    assert days[0].day == _epoch_to_day(ts)


def test_duplicate_refreshes_mutable_metadata(db: AppDatabase) -> None:
    ts = 1_700_000_000.0
    db.record_contribution_for_login(
        "github", "alice", "o", "r", "pr_opened", "pr:1", event_at=ts, additions=10
    )
    # A synchronize re-fire grows the diff; metadata is refreshed in place.
    db.record_contribution_for_login(
        "github", "alice", "o", "r", "pr_opened", "pr:1", event_at=ts, additions=42
    )
    contributor = db.get_contributor_by_login("github", "alice")
    assert contributor is not None
    totals = db.get_contributor_totals(contributor.id)
    assert totals["additions"] == 42  # not 52, not 10


def test_rollup_sums_distinct_kinds_same_day(db: AppDatabase) -> None:
    ts = 1_700_000_000.0
    db.record_contribution_for_login("github", "alice", "o", "r", "pr_opened", "pr:1", event_at=ts)
    db.record_contribution_for_login("github", "alice", "o", "r", "pr_merged", "prm:1", event_at=ts)
    db.record_contribution_for_login(
        "github", "alice", "o", "r", "commit", "commit:abc", event_at=ts
    )
    contributor = db.get_contributor_by_login("github", "alice")
    assert contributor is not None
    [day] = db.get_contributor_days(contributor.id, "2000-01-01", "2100-01-01")
    assert (day.prs_opened, day.prs_merged, day.commits, day.total) == (1, 1, 1, 3)


def test_upsert_contributor_keeps_richer_metadata(db: AppDatabase) -> None:
    db.upsert_contributor("github", "alice", avatar_url="http://a/avatar.png", external_id=7)
    # A later sparse update (e.g. from a webhook with no avatar) must not wipe it.
    db.upsert_contributor("github", "alice", avatar_url="", external_id=0)
    c = db.get_contributor_by_login("github", "alice")
    assert c is not None
    assert c.avatar_url == "http://a/avatar.png"
    assert c.external_id == 7


def test_list_contributors_sort_and_aggregate(db: AppDatabase) -> None:
    ts = 1_700_000_000.0
    for i in range(3):
        db.record_contribution_for_login(
            "github", "alice", "o", "r", "commit", f"commit:a{i}", event_at=ts
        )
    db.record_contribution_for_login("github", "bob", "o", "r", "commit", "commit:b0", event_at=ts)
    db.record_contribution_for_login("github", "bob", "o", "r2", "review", "review:1", event_at=ts)

    by_commits = db.list_contributors(sort="commits")
    assert [c["login"] for c in by_commits] == ["alice", "bob"]
    assert by_commits[0]["commits"] == 3
    assert by_commits[0]["repos_touched"] == 1

    by_reviews = db.list_contributors(sort="reviews")
    assert by_reviews[0]["login"] == "bob"
    assert by_reviews[0]["reviews"] == 1
    bob = next(c for c in by_reviews if c["login"] == "bob")
    assert bob["repos_touched"] == 2


def test_list_contributors_excludes_bots_by_default(db: AppDatabase) -> None:
    ts = 1_700_000_000.0
    db.record_contribution_for_login("github", "alice", "o", "r", "commit", "commit:a", event_at=ts)
    db.record_contribution_for_login(
        "github", "dependabot[bot]", "o", "r", "commit", "commit:b", event_at=ts, is_bot=True
    )
    logins = {c["login"] for c in db.list_contributors()}
    assert logins == {"alice"}
    with_bots = {c["login"] for c in db.list_contributors(include_bots=True)}
    assert "dependabot[bot]" in with_bots


def test_get_contributor_days_respects_range(db: AppDatabase) -> None:
    db.record_contribution_for_login(
        "github", "alice", "o", "r", "commit", "commit:old", event_at=1_600_000_000.0
    )
    db.record_contribution_for_login(
        "github", "alice", "o", "r", "commit", "commit:new", event_at=1_700_000_000.0
    )
    c = db.get_contributor_by_login("github", "alice")
    assert c is not None
    new_day = _epoch_to_day(1_700_000_000.0)
    days = db.get_contributor_days(c.id, new_day, "2100-01-01")
    assert [d.day for d in days] == [new_day]


# ── IndexStore review-quality attribution ──


def test_index_store_review_quality_by_author(tmp_path: Path) -> None:
    store = IndexStore(str(tmp_path / "idx.db"))
    store.record_review(
        pr_number=1,
        pr_title="t",
        pr_url="u",
        comments_posted=3,
        blockers=2,
        warnings=1,
        author="alice",
    )
    store.record_review(
        pr_number=2,
        pr_title="t2",
        pr_url="u2",
        comments_posted=1,
        blockers=0,
        warnings=1,
        author="bob",
    )
    q = store.get_review_quality_by_author("alice")
    assert q["reviews"] == 1
    assert q["blockers"] == 2
    assert q["warnings"] == 1
    assert store.get_review_quality_by_author("bob")["warnings"] == 1
    store.close()


def test_index_store_feedback_quality_by_author(tmp_path: Path) -> None:
    store = IndexStore(str(tmp_path / "idx.db"))
    store.record_bulk_feedback(
        [
            {
                "pr_number": 1,
                "pr_url": "u",
                "comment_path": "a.py",
                "comment_line": 1,
                "comment_category": "bug",
                "comment_severity": "high",
                "comment_title": "x",
                "signal": "accepted",
                "actor": "merger",
                "pr_author": "alice",
            },
            {
                "pr_number": 1,
                "pr_url": "u",
                "comment_path": "b.py",
                "comment_line": 2,
                "comment_category": "bug",
                "comment_severity": "low",
                "comment_title": "y",
                "signal": "rejected",
                "actor": "merger",
                "pr_author": "alice",
            },
        ]
    )
    fq = store.get_feedback_quality_by_author("alice")
    assert fq["accepted"] == 1
    assert fq["rejected"] == 1
    store.close()


# ── API endpoints (call route functions directly with a patched singleton) ──


@pytest.fixture
def patched_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    db = AppDatabase(url=str(tmp_path / "app.db"), admin_password="admin")
    monkeypatch.setattr(api, "_app_db", db)
    return db


def _admin_request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(is_admin=True)))


def _viewer_request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(is_admin=False)))


def test_api_list_contributors(patched_api: AppDatabase) -> None:
    patched_api.record_contribution_for_login(
        "github", "alice", "o", "r", "commit", "commit:a", event_at=time.time()
    )
    result = api.list_contributors(_admin_request(), sort="commits")
    assert len(result) == 1
    assert result[0].login == "alice"
    assert result[0].commits == 1


def test_api_get_contributor_detail(patched_api: AppDatabase) -> None:
    now = time.time()
    patched_api.record_contribution_for_login(
        "github", "alice", "o", "r", "pr_opened", "pr:1", event_at=now, additions=5
    )
    detail = api.get_contributor("alice", _admin_request())
    assert detail.contributor.login == "alice"
    assert detail.contributor.prs_opened == 1
    # Heatmap spans the trailing 365 days and the recent event lands in it.
    assert len(detail.heatmap) >= 1
    assert any(d.total > 0 for d in detail.heatmap)
    assert detail.repos[0].owner == "o"
    # No indexed repos → quality is zeroed but well-formed.
    assert detail.quality.accept_rate == 0.0


def test_api_get_contributor_404(patched_api: AppDatabase) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        api.get_contributor("nobody", _admin_request())
    assert exc.value.status_code == 404


def test_api_contributors_requires_admin(patched_api: AppDatabase) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        api.list_contributors(_viewer_request())
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc2:
        api.get_contributor("anyone", _viewer_request())
    assert exc2.value.status_code == 403


def test_aggregate_contributions_window(db: AppDatabase) -> None:
    now = time.time()
    db.record_contribution_for_login(
        "github", "alice", "o", "r", "commit", "commit:recent", event_at=now - 1 * 86400
    )
    db.record_contribution_for_login(
        "github", "alice", "o", "r", "commit", "commit:older", event_at=now - 10 * 86400
    )
    current = db.aggregate_contributions(now - 7 * 86400, now)
    previous = db.aggregate_contributions(now - 14 * 86400, now - 7 * 86400)
    assert current["commits"] == 1
    assert current["contributors"] == 1
    assert previous["commits"] == 1


def test_aggregate_contributions_excludes_bots(db: AppDatabase) -> None:
    now = time.time()
    db.record_contribution_for_login(
        "github", "ci[bot]", "o", "r", "commit", "commit:b", event_at=now - 86400, is_bot=True
    )
    assert db.aggregate_contributions(now - 7 * 86400, now)["commits"] == 0
    assert db.aggregate_contributions(now - 7 * 86400, now, include_bots=True)["commits"] == 1


def test_api_contributors_summary(patched_api: AppDatabase) -> None:
    now = time.time()
    patched_api.record_contribution_for_login(
        "github", "alice", "o", "r", "pr_merged", "prm:1", event_at=now - 86400, merged=True
    )
    patched_api.record_contribution_for_login(
        "github", "alice", "o", "r", "pr_merged", "prm:2", event_at=now - 10 * 86400, merged=True
    )
    s = api.contributors_summary(_admin_request(), days=7)
    assert s.days == 7
    assert s.current.prs_merged == 1
    assert s.previous.prs_merged == 1


def test_api_summary_requires_admin(patched_api: AppDatabase) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        api.contributors_summary(_viewer_request())
    assert exc.value.status_code == 403
