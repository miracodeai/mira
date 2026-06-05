"""Tests for drop_already_posted — cross-round dedup against open bot threads.

filter_noise dedupes within one review; drop_already_posted stops a re-review
from re-posting a finding that already has an open thread (the bug seen on
PR #68, where utils.py:10 got the same comment on three separate pushes).
"""

from __future__ import annotations

from mira.core.noise_filter import drop_already_posted
from mira.models import ReviewComment, Severity, UnresolvedThread


def _comment(
    path: str, line: int, end_line: int | None = None, title: str = "issue"
) -> ReviewComment:
    return ReviewComment(
        path=path,
        line=line,
        end_line=end_line,
        severity=Severity.WARNING,
        category="bug",
        title=title,
        body="b",
        confidence=0.9,
    )


def _thread(path: str, line: int) -> UnresolvedThread:
    return UnresolvedThread(thread_id=f"T:{path}:{line}", path=path, line=line, body="prev")


def test_drops_comment_on_open_thread_line():
    comments = [_comment("src/a.py", 10)]
    existing = [_thread("src/a.py", 10)]
    assert drop_already_posted(comments, existing) == []


def test_keeps_comment_with_no_open_thread():
    comments = [_comment("src/a.py", 10)]
    existing = [_thread("src/a.py", 99)]
    assert len(drop_already_posted(comments, existing)) == 1


def test_path_must_match():
    comments = [_comment("src/a.py", 10)]
    existing = [_thread("src/b.py", 10)]
    assert len(drop_already_posted(comments, existing)) == 1


def test_open_thread_line_within_multiline_comment_range():
    # New comment spans 8-12; an open thread sits at line 10 inside it.
    comments = [_comment("src/a.py", 8, end_line=12)]
    existing = [_thread("src/a.py", 10)]
    assert drop_already_posted(comments, existing) == []


def test_outdated_thread_unknown_line_does_not_suppress():
    comments = [_comment("src/a.py", 10)]
    existing = [_thread("src/a.py", 0)]  # unknown / outdated location
    assert len(drop_already_posted(comments, existing)) == 1


def test_no_existing_threads_is_passthrough():
    comments = [_comment("src/a.py", 10), _comment("src/b.py", 20)]
    assert drop_already_posted(comments, []) == comments


def test_mixed_some_dropped_some_kept():
    comments = [_comment("src/a.py", 10), _comment("src/a.py", 50), _comment("src/c.py", 5)]
    existing = [_thread("src/a.py", 10), _thread("src/c.py", 5)]
    kept = drop_already_posted(comments, existing)
    assert [(c.path, c.line) for c in kept] == [("src/a.py", 50)]
