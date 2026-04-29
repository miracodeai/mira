"""Tests for priority-based file selection and large-PR chunked review."""

from __future__ import annotations

import pytest

from mira.core.engine import _select_files_by_priority
from mira.core.priority import rank_files, score_file
from mira.models import FileChangeType, FileDiff, HunkInfo


def _file(
    path: str,
    *,
    change_type: FileChangeType = FileChangeType.MODIFIED,
    diff_text: str = "",
    added: int = 0,
    deleted: int = 0,
) -> FileDiff:
    """Build a synthetic FileDiff for tests. The hunks list contains a single
    hunk whose `content` is the supplied diff_text — used by the per-file
    size cap logic."""
    hunks = (
        [
            HunkInfo(
                source_start=1, source_length=1, target_start=1, target_length=1, content=diff_text
            )
        ]
        if diff_text
        else []
    )
    return FileDiff(
        path=path,
        change_type=change_type,
        hunks=hunks,
        added_lines=added,
        deleted_lines=deleted,
    )


# ── score_file ──


class TestScoreFile:
    def test_sensitive_path_scores_high(self):
        s = score_file(_file("src/auth/jwt.py", added=20, deleted=5)).score
        baseline = score_file(_file("src/utils/string_helpers.py", added=20, deleted=5)).score
        assert s > baseline

    def test_test_files_score_low(self):
        baseline = score_file(_file("src/auth/login.py", added=10, deleted=2)).score
        test_score = score_file(_file("tests/test_auth.py", added=10, deleted=2)).score
        assert test_score < baseline

    def test_large_change_outscores_small(self):
        small = score_file(_file("src/api.py", added=5, deleted=0)).score
        large = score_file(_file("src/api.py", added=400, deleted=100)).score
        assert large > small

    def test_lockfiles_blacklisted(self):
        s = score_file(_file("package-lock.json", added=1000, deleted=500)).score
        assert s < -50  # never-review

    def test_minified_blacklisted(self):
        assert score_file(_file("dist/bundle.min.js", added=10)).score < -50

    def test_payment_path_high(self):
        s = score_file(_file("internal/payments/stripe.go", added=30, deleted=5)).score
        assert s >= 6  # sensitive (+5) plus change-size + base bonus

    def test_docs_low(self):
        assert score_file(_file("docs/getting-started.md", added=20)).score < 0


# ── rank_files ──


class TestRankFiles:
    def test_orders_high_to_low(self):
        files = [
            _file("README.md", added=5),
            _file("src/auth/jwt.py", added=50, deleted=10),
            _file("tests/test_jwt.py", added=20),
        ]
        ranked = rank_files(files)
        ordered_paths = [f.path for f, _ in ranked]
        # auth file should beat the test file, which should beat README
        assert ordered_paths[0] == "src/auth/jwt.py"
        assert ordered_paths[-1] == "README.md"

    def test_stable_for_equal_scores(self):
        files = [
            _file("src/a.py", added=5),
            _file("src/b.py", added=5),
        ]
        ranked = rank_files(files)
        # Same score → alphabetical secondary sort
        assert [f.path for f, _ in ranked] == ["src/a.py", "src/b.py"]


# ── _select_files_by_priority (engine helper) ──


class TestSelectFilesByPriority:
    def test_no_files_no_skips(self):
        sel, skip = _select_files_by_priority(
            [],
            max_total_size=10_000,
            max_per_file_size=5_000,
        )
        assert sel == [] and skip == []

    def test_drops_oversized_single_file(self):
        big = _file("src/generated.py", diff_text="x" * 6_000, added=1_000)
        small = _file("src/auth.py", diff_text="y" * 500, added=10)
        sel, skip = _select_files_by_priority(
            [big, small],
            max_total_size=100_000,
            max_per_file_size=5_000,
        )
        assert [f.path for f in sel] == ["src/auth.py"]
        assert any(p == "src/generated.py" for p, _ in skip)

    def test_drops_low_priority_when_over_size_cap(self):
        # Prioritized: auth.py first, even though it's not first alphabetically
        auth = _file("src/auth/login.py", diff_text="a" * 500, added=20)
        readme = _file("README.md", diff_text="b" * 500, added=5)
        sel, skip = _select_files_by_priority(
            [readme, auth],
            max_total_size=600,
            max_per_file_size=5_000,
        )
        assert sel[0].path == "src/auth/login.py"
        assert any(p == "README.md" for p, _ in skip)

    def test_only_paths_filter(self):
        a = _file("src/a.py", diff_text="x" * 100, added=10)
        b = _file("src/b.py", diff_text="x" * 100, added=10)
        c = _file("src/c.py", diff_text="x" * 100, added=10)
        sel, _skip = _select_files_by_priority(
            [a, b, c],
            max_total_size=10_000,
            max_per_file_size=5_000,
            only_paths={"src/b.py"},
        )
        assert [f.path for f in sel] == ["src/b.py"]


# ── PRReviewProgress persistence (uses dashboard.db) ──


class TestPRReviewProgressDB:
    @pytest.fixture
    def db(self, tmp_path, monkeypatch):
        from mira.dashboard.db import AppDatabase

        monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
        return AppDatabase("", admin_password="x")

    def test_upsert_and_get(self, db):
        from mira.dashboard.db import PRReviewProgress

        progress = PRReviewProgress(
            owner="acme",
            repo="web",
            pr_number=42,
            total_paths=["a.py", "b.py", "c.py"],
            reviewed_paths=["a.py"],
            skipped_paths=["b.py", "c.py"],
            chunk_index=1,
        )
        db.upsert_pr_review_progress(progress)
        loaded = db.get_pr_review_progress("acme", "web", 42)
        assert loaded is not None
        assert loaded.reviewed_paths == ["a.py"]
        assert loaded.skipped_paths == ["b.py", "c.py"]
        assert loaded.chunk_index == 1

    def test_upsert_replaces_existing(self, db):
        from mira.dashboard.db import PRReviewProgress

        db.upsert_pr_review_progress(
            PRReviewProgress(
                owner="o",
                repo="r",
                pr_number=1,
                total_paths=["a"],
                reviewed_paths=["a"],
                skipped_paths=[],
                chunk_index=1,
            )
        )
        db.upsert_pr_review_progress(
            PRReviewProgress(
                owner="o",
                repo="r",
                pr_number=1,
                total_paths=["a", "b"],
                reviewed_paths=["a", "b"],
                skipped_paths=[],
                chunk_index=2,
            )
        )
        loaded = db.get_pr_review_progress("o", "r", 1)
        assert loaded is not None
        assert loaded.reviewed_paths == ["a", "b"]
        assert loaded.chunk_index == 2

    def test_remaining_paths_property(self, db):
        from mira.dashboard.db import PRReviewProgress

        progress = PRReviewProgress(
            owner="o",
            repo="r",
            pr_number=1,
            total_paths=["a", "b", "c", "d"],
            reviewed_paths=["a"],
            skipped_paths=["b"],
        )
        assert progress.remaining_paths == ["c", "d"]
        assert not progress.is_complete

    def test_is_complete(self, db):
        from mira.dashboard.db import PRReviewProgress

        progress = PRReviewProgress(
            owner="o",
            repo="r",
            pr_number=1,
            total_paths=["a", "b"],
            reviewed_paths=["a"],
            skipped_paths=["b"],
        )
        assert progress.is_complete

    def test_delete(self, db):
        from mira.dashboard.db import PRReviewProgress

        db.upsert_pr_review_progress(
            PRReviewProgress(
                owner="o",
                repo="r",
                pr_number=1,
                total_paths=["a"],
                reviewed_paths=["a"],
                skipped_paths=[],
            )
        )
        db.delete_pr_review_progress("o", "r", 1)
        assert db.get_pr_review_progress("o", "r", 1) is None
