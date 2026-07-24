"""Tests for review insights: pull_requests/pr_reviewers storage, the API
endpoints, and the live webhook capture handlers."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mira.dashboard import api
from mira.dashboard.db import AppDatabase
from mira.platforms.github import webhook as handlers

DAY = 86400
HOUR = 3600


@pytest.fixture
def db(tmp_path: Path) -> AppDatabase:
    return AppDatabase(url=str(tmp_path / "app.db"), admin_password="admin")


@pytest.fixture
def patched_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    d = AppDatabase(url=str(tmp_path / "app.db"), admin_password="admin")
    monkeypatch.setattr(api, "_app_db", d)
    return d


def _admin() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(is_admin=True)))


def _viewer() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(is_admin=False)))


# ── storage ──


def test_pr_reviewer_merge_keeps_earliest_and_latest(db: AppDatabase) -> None:
    db.upsert_pull_request("o", "r", 1, author="bob", state="open", created_at=100, updated_at=100)
    db.upsert_pr_reviewer("o", "r", 1, "alice", requested_at=100)
    db.upsert_pr_reviewer("o", "r", 1, "alice", responded_at=400, state="commented")
    db.upsert_pr_reviewer("o", "r", 1, "alice", responded_at=500, state="approved")
    [row] = db.get_open_pr_reviewers()
    assert row["requested_at"] == 100
    assert row["responded_at"] == 400  # earliest response wins
    assert row["state"] == "approved"  # latest state wins


def test_set_pr_first_review_keeps_earliest(db: AppDatabase) -> None:
    db.upsert_pull_request("o", "r", 1, state="open", created_at=100, updated_at=100)
    db.set_pr_first_review("o", "r", 1, 500)
    db.set_pr_first_review("o", "r", 1, 300)  # earlier
    db.set_pr_first_review("o", "r", 1, 800)  # later, ignored
    assert db.get_open_pull_requests()[0]["first_review_at"] == 300


def test_remove_pr_reviewer_only_if_unanswered(db: AppDatabase) -> None:
    db.upsert_pull_request("o", "r", 1, state="open", created_at=1, updated_at=1)
    db.upsert_pr_reviewer("o", "r", 1, "alice", requested_at=1, responded_at=2, state="approved")
    db.upsert_pr_reviewer("o", "r", 1, "carol", requested_at=1)
    db.remove_pr_reviewer("o", "r", 1, "alice")  # answered → kept
    db.remove_pr_reviewer("o", "r", 1, "carol")  # pending → removed
    assert {r["reviewer"] for r in db.get_open_pr_reviewers()} == {"alice"}


# ── API ──


def test_review_summary(patched_api: AppDatabase) -> None:
    now = time.time()
    # open + stale (idle 5d), nobody reviewed yet → awaiting
    patched_api.upsert_pull_request(
        "o", "r", 1, author="bob", state="open", created_at=now - 6 * DAY, updated_at=now - 5 * DAY
    )
    # merged this week: ttfr 1h, ttm 2d
    patched_api.upsert_pull_request(
        "o",
        "r",
        2,
        author="bob",
        state="merged",
        created_at=now - 3 * DAY,
        updated_at=now - 1 * DAY,
        merged_at=now - 1 * DAY,
    )
    patched_api.set_pr_first_review("o", "r", 2, now - 3 * DAY + HOUR)
    s = api.review_summary(_admin(), days=7, stale_days=3)
    assert s.open_prs == 1
    assert s.stale_prs == 1
    assert s.awaiting_review == 1
    assert s.current.time_to_merge_secs == 2 * DAY
    assert s.current.time_to_first_review_secs == HOUR


def test_is_bare_approval() -> None:
    from mira.platforms.github.review_signals import is_bare_approval

    assert is_bare_approval("approved", "", []) is True
    assert is_bare_approval("approved", "LGTM!", []) is True
    assert is_bare_approval("approved", "LGTM", ["nice"]) is True  # trivial comment doesn't save it
    assert (
        is_bare_approval("approved", "Please add a null check before the deref on line 40", [])
        is False
    )
    assert (
        is_bare_approval("approved", "LGTM", ["This will crash on an empty list — guard it"])
        is False
    )
    assert is_bare_approval("changes_requested", "", []) is False  # only approvals can be bare


def test_bare_approval_persists_and_is_guarded(db: AppDatabase) -> None:
    db.upsert_pull_request("o", "r", 1, state="open", created_at=1, updated_at=1)
    db.upsert_pr_reviewer("o", "r", 1, "alice", responded_at=5, state="approved", bare_approval=1)
    [row] = db.get_reviewer_activity_rows()
    assert row["bare_approval"] == 1 and row["review_state"] == "approved"
    # A later request-only upsert (no responded_at) must NOT clobber the flag.
    db.upsert_pr_reviewer("o", "r", 1, "alice", requested_at=1)
    assert db.get_reviewer_activity_rows()[0]["bare_approval"] == 1


def test_reviewer_rubber_stamp_rate(patched_api: AppDatabase) -> None:
    now = time.time()
    # diego: 2 approvals, both bare → 100%. eve: 2 approvals, 1 bare → 50%.
    for n, (who, bare) in enumerate([("diego", 1), ("diego", 1), ("eve", 1), ("eve", 0)], start=1):
        patched_api.upsert_pull_request(
            "o", "r", n, state="open", created_at=now - DAY, updated_at=now - DAY
        )
        patched_api.upsert_pr_reviewer(
            "o", "r", n, who, responded_at=now - HOUR, state="approved", bare_approval=bare
        )
    stats = {s.reviewer: s for s in api.review_reviewers(_admin(), days=30)}
    assert stats["diego"].rubber_stamps == 2 and stats["diego"].approvals == 2
    assert stats["diego"].rubber_stamp_rate == 100.0
    assert stats["eve"].rubber_stamp_rate == 50.0
    summary = api.review_summary(_admin(), days=7)
    assert summary.approvals == 4 and summary.rubber_stamps == 3


def test_review_health_score(patched_api: AppDatabase) -> None:
    now = time.time()
    # Two PRs merged this week — one approved by a human, one not.
    for n, approved in ((10, True), (11, False)):
        patched_api.upsert_pull_request(
            "o",
            "r",
            n,
            state="merged",
            created_at=now - 2 * DAY,
            updated_at=now - DAY,
            merged_at=now - DAY,
        )
        patched_api.set_pr_first_review("o", "r", n, now - 2 * DAY + HOUR)
        if approved:
            patched_api.upsert_pr_reviewer(
                "o", "r", n, "alice", responded_at=now - 2 * DAY + HOUR, state="approved"
            )
    s = api.review_summary(_admin(), days=7, stale_days=3)
    assert s.merged == 2
    assert s.approved_merged == 1
    assert s.health_score is not None
    keys = {c.key for c in s.health}
    assert keys == {"approvals", "responsiveness", "backlog"}
    approvals = next(c for c in s.health if c.key == "approvals")
    assert approvals.score == 0.5  # 1 of 2 merges approved


def test_open_prs_board(patched_api: AppDatabase) -> None:
    now = time.time()
    patched_api.upsert_pull_request(
        "o",
        "r",
        1,
        author="bob",
        title="Stuck",
        url="u1",
        state="open",
        created_at=now - 6 * DAY,
        updated_at=now - 5 * DAY,
    )
    patched_api.upsert_pr_reviewer("o", "r", 1, "alice", requested_at=now - 5 * DAY)
    board = api.review_open_prs(_admin(), stale_days=3)
    assert len(board) == 1
    pr = board[0]
    assert pr.stale is True
    assert pr.waiting_on == ["alice"]
    assert pr.status == "awaiting_review"


def test_reviewers_bottleneck_ordering(patched_api: AppDatabase) -> None:
    now = time.time()
    # open PR with two pending requests on jonas (bottleneck)
    for n in (1, 2):
        patched_api.upsert_pull_request(
            "o", "r", n, state="open", created_at=now - DAY, updated_at=now - DAY
        )
        patched_api.upsert_pr_reviewer("o", "r", n, "jonas", requested_at=now - DAY)
    # alice answered one quickly
    patched_api.upsert_pull_request(
        "o", "r", 3, state="open", created_at=now - DAY, updated_at=now - DAY
    )
    patched_api.upsert_pr_reviewer(
        "o",
        "r",
        3,
        "alice",
        requested_at=now - DAY,
        responded_at=now - DAY + HOUR,
        state="approved",
    )
    stats = api.review_reviewers(_admin(), days=30)
    assert stats[0].reviewer == "jonas"  # biggest pending queue first
    assert stats[0].pending == 2
    alice = next(s for s in stats if s.reviewer == "alice")
    assert alice.median_response_secs == HOUR


def test_review_endpoints_require_admin(patched_api: AppDatabase) -> None:
    from fastapi import HTTPException

    for call in (
        lambda: api.review_summary(_viewer()),
        lambda: api.review_open_prs(_viewer()),
        lambda: api.review_reviewers(_viewer()),
    ):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 403


# ── webhook capture ──


async def test_handle_review_request_and_submit(
    monkeypatch: pytest.MonkeyPatch, patched_api: AppDatabase
) -> None:
    auth = MagicMock()
    base_pr = {
        "number": 1,
        "user": {"login": "bob"},
        "title": "t",
        "html_url": "https://github.com/o/r/pull/1",
        "state": "open",
        "draft": False,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
    }
    repo = {"owner": {"login": "o"}, "name": "r"}

    # 1) review requested → alice pending
    await handlers.handle_pr_review_meta(
        {
            "action": "review_requested",
            "repository": repo,
            "pull_request": base_pr,
            "requested_reviewer": {"login": "alice"},
        },
        auth,
        "mira",
    )
    assert patched_api.get_open_pull_requests()[0]["number"] == 1
    [rv] = patched_api.get_open_pr_reviewers()
    assert rv["reviewer"] == "alice" and rv["responded_at"] == 0

    # 2) alice submits an approval → responded + first_review + review contribution
    await handlers.handle_pull_request_review(
        {
            "action": "submitted",
            "repository": repo,
            "pull_request": base_pr,
            "review": {
                "id": 99,
                "user": {"login": "alice", "id": 3, "type": "User"},
                "state": "APPROVED",
                "submitted_at": "2024-01-03T00:00:00Z",
            },
        },
        auth,
        "mira",
    )
    [rv2] = patched_api.get_open_pr_reviewers()
    assert rv2["responded_at"] > 0
    assert rv2["state"] == "approved"
    assert patched_api.get_open_pull_requests()[0]["first_review_at"] > 0
    alice = patched_api.get_contributor_by_login("github", "alice")
    assert alice is not None and patched_api.get_contributor_totals(alice.id)["reviews"] == 1


async def test_self_review_ignored(
    monkeypatch: pytest.MonkeyPatch, patched_api: AppDatabase
) -> None:
    auth = MagicMock()
    await handlers.handle_pull_request_review(
        {
            "action": "submitted",
            "repository": {"owner": {"login": "o"}, "name": "r"},
            "pull_request": {
                "number": 1,
                "user": {"login": "bob"},
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
            },
            "review": {
                "id": 1,
                "user": {"login": "bob", "type": "User"},
                "state": "APPROVED",
                "submitted_at": "2024-01-02T00:00:00Z",
            },
        },
        auth,
        "mira",
    )
    # bob reviewing his own PR is not a review of someone else's work
    assert patched_api.get_open_pr_reviewers() == []
