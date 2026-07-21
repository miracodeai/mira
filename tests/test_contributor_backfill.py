"""Tests for contributor backfill (mocked PyGithub) and the live-recording
webhook helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from mira.dashboard import api
from mira.dashboard.db import AppDatabase
from mira.platforms.github import contributor_backfill as cb
from mira.platforms.github import webhook as handlers
from mira.platforms.github import webhook as index_handlers


@pytest.fixture
def db(tmp_path: Path) -> AppDatabase:
    return AppDatabase(url=str(tmp_path / "app.db"), admin_password="admin")


def _user(login: str, uid: int = 1, type_: str = "User") -> MagicMock:
    u = MagicMock()
    u.login = login
    u.id = uid
    u.type = type_
    u.avatar_url = f"http://avatars/{login}.png"
    return u


def _pr(number: int, author: str, merged: bool = False, reviews=None) -> MagicMock:
    pr = MagicMock()
    pr.number = number
    pr.user = _user(author)
    pr.title = f"PR {number}"
    pr.additions = 10
    pr.deletions = 2
    pr.changed_files = 3
    pr.created_at = datetime(2024, 1, 1, tzinfo=UTC)
    pr.updated_at = datetime(2024, 1, 2, tzinfo=UTC)
    pr.merged_at = datetime(2024, 1, 3, tzinfo=UTC) if merged else None
    pr.closed_at = pr.merged_at
    pr.state = "closed" if merged else "open"
    pr.draft = False
    pr.html_url = f"https://github.com/o/r/pull/{number}"
    pr.requested_reviewers = []
    pr.get_reviews.return_value = reviews or []
    return pr


def _review(review_id: int, reviewer: str) -> MagicMock:
    r = MagicMock()
    r.id = review_id
    r.user = _user(reviewer, uid=99)
    r.submitted_at = datetime(2024, 1, 4, tzinfo=UTC)
    r.state = "approved"
    r.body = "Looks great, thanks for the thorough tests on this change"
    return r


def test_backfill_sync_records_prs_merges_reviews(db: AppDatabase) -> None:
    pr = _pr(1, "alice", merged=True, reviews=[_review(5, "bob")])
    gh = MagicMock()
    repo = MagicMock()
    pulls = MagicMock()
    pulls.totalCount = 1
    pulls.__iter__ = lambda self: iter([pr])
    repo.get_pulls.return_value = pulls
    gh.get_repo.return_value = repo

    counts: dict[str, int] = {"prs": 0, "merges": 0, "reviews": 0, "commits": 0}
    # Drive _backfill_sync directly with a mocked Github client.
    import mira.platforms.github.contributor_backfill as mod

    orig_github = mod.Github
    mod.Github = lambda _token: gh  # type: ignore[assignment]
    try:
        mod._backfill_sync(db, "tok", "o", "r", None, False, counts, None)
    finally:
        mod.Github = orig_github

    assert counts == {"prs": 1, "merges": 1, "reviews": 1, "commits": 0}
    alice = db.get_contributor_by_login("github", "alice")
    bob = db.get_contributor_by_login("github", "bob")
    assert alice is not None and bob is not None
    assert db.get_contributor_totals(alice.id)["prs_merged"] == 1
    assert db.get_contributor_totals(bob.id)["reviews"] == 1


def test_backfill_sync_is_idempotent(db: AppDatabase) -> None:
    def make_gh() -> MagicMock:
        pr = _pr(1, "alice", merged=True)
        gh = MagicMock()
        repo = MagicMock()
        pulls = MagicMock()
        pulls.totalCount = 1
        pulls.__iter__ = lambda self: iter([pr])
        repo.get_pulls.return_value = pulls
        gh.get_repo.return_value = repo
        return gh

    import mira.platforms.github.contributor_backfill as mod

    orig = mod.Github
    try:
        for _ in range(2):
            counts = {"prs": 0, "merges": 0, "reviews": 0, "commits": 0}
            mod.Github = lambda _t: make_gh()  # type: ignore[assignment]
            mod._backfill_sync(db, "tok", "o", "r", None, False, counts, None)
    finally:
        mod.Github = orig

    alice = db.get_contributor_by_login("github", "alice")
    assert alice is not None
    # Two full backfills, but each event counted once.
    totals = db.get_contributor_totals(alice.id)
    assert totals["prs_opened"] == 1
    assert totals["prs_merged"] == 1
    # pr_opened (created_at) and pr_merged (merged_at) fall on different days;
    # across both, the rollup totals 2 events — not 4 despite two backfills.
    days = db.get_contributor_days(alice.id, "2000-01-01", "2100-01-01")
    assert sum(d.total for d in days) == 2


def test_backfill_commits_skips_authorless(db: AppDatabase) -> None:
    linked = MagicMock()
    linked.sha = "sha1"
    linked.author = _user("alice")
    linked.commit = MagicMock()
    linked.commit.author.date = datetime(2024, 1, 1, tzinfo=UTC)
    linked.commit.message = "fix: thing\n\nbody"

    emailonly = MagicMock()
    emailonly.sha = "sha2"
    emailonly.author = None  # no GitHub login → must be skipped
    emailonly.commit = MagicMock()
    emailonly.commit.author.date = datetime(2024, 1, 1, tzinfo=UTC)
    emailonly.commit.message = "chore"

    gh = MagicMock()
    repo = MagicMock()
    repo.get_commits.return_value = [linked, emailonly]
    counts = {"prs": 0, "merges": 0, "reviews": 0, "commits": 0}
    cb._backfill_commits(db, gh, repo, "o", "r", None, counts)

    assert counts["commits"] == 1
    assert db.get_contributor_by_login("github", "alice") is not None


# ── Live webhook recording helpers ──


def test_record_pr_contribution_helper(monkeypatch: pytest.MonkeyPatch, db: AppDatabase) -> None:
    monkeypatch.setattr(api, "_app_db", db)
    payload = {
        "repository": {"owner": {"login": "o"}, "name": "r"},
        "pull_request": {
            "number": 7,
            "title": "Add feature",
            "user": {"login": "alice", "id": 3, "type": "User", "avatar_url": "http://a"},
            "created_at": "2024-05-01T00:00:00Z",
            "additions": 20,
            "deletions": 1,
            "changed_files": 4,
        },
    }
    handlers._record_pr_contribution(payload, "pr_opened")
    alice = db.get_contributor_by_login("github", "alice")
    assert alice is not None
    totals = db.get_contributor_totals(alice.id)
    assert totals["prs_opened"] == 1
    assert totals["additions"] == 20


def test_record_pr_contribution_no_double_count_on_synchronize(
    monkeypatch: pytest.MonkeyPatch, db: AppDatabase
) -> None:
    monkeypatch.setattr(api, "_app_db", db)
    payload = {
        "repository": {"owner": {"login": "o"}, "name": "r"},
        "pull_request": {
            "number": 7,
            "title": "x",
            "user": {"login": "alice", "id": 3},
            "created_at": "2024-05-01T00:00:00Z",
            "additions": 5,
        },
    }
    handlers._record_pr_contribution(payload, "pr_opened")
    payload["pull_request"]["additions"] = 50  # second synchronize, bigger diff
    handlers._record_pr_contribution(payload, "pr_opened")

    alice = db.get_contributor_by_login("github", "alice")
    assert alice is not None
    [day] = db.get_contributor_days(alice.id, "2000-01-01", "2100-01-01")
    assert day.prs_opened == 1  # counted once
    assert db.get_contributor_totals(alice.id)["additions"] == 50  # but metadata refreshed


def test_record_push_commits_default_branch_only(
    monkeypatch: pytest.MonkeyPatch, db: AppDatabase
) -> None:
    monkeypatch.setattr(index_handlers, "_get_app_db", lambda: db)
    base_repo = {"owner": {"login": "o"}, "name": "r", "default_branch": "main"}
    commit = {
        "id": "deadbeef",
        "author": {"username": "alice"},
        "timestamp": "2024-05-01T00:00:00Z",
        "message": "fix: thing",
    }

    # Push to a feature branch → ignored.
    index_handlers._record_push_commits(
        {"ref": "refs/heads/feature", "repository": base_repo, "commits": [commit]}
    )
    assert db.get_contributor_by_login("github", "alice") is None

    # Push to default branch → recorded.
    index_handlers._record_push_commits(
        {"ref": "refs/heads/main", "repository": base_repo, "commits": [commit]}
    )
    alice = db.get_contributor_by_login("github", "alice")
    assert alice is not None
    assert db.get_contributor_totals(alice.id)["commits"] == 1
