"""Tests for context building."""

from __future__ import annotations

from mira.core.context import build_file_context_string, expand_context
from mira.models import FileChangeType, FileDiff, HunkInfo


class TestExpandContext:
    def test_no_merge_needed(self):
        files = [
            FileDiff(
                path="a.py",
                change_type=FileChangeType.MODIFIED,
                hunks=[HunkInfo(1, 3, 1, 3, "hunk1")],
                added_lines=1,
                deleted_lines=0,
            )
        ]
        result = expand_context(files, context_lines=3)
        assert len(result[0].hunks) == 1

    def test_merge_adjacent_hunks(self):
        hunks = [
            HunkInfo(1, 3, 1, 3, "hunk1"),
            HunkInfo(5, 3, 5, 3, "hunk2"),
        ]
        files = [
            FileDiff(
                path="a.py",
                change_type=FileChangeType.MODIFIED,
                hunks=hunks,
                added_lines=2,
                deleted_lines=0,
            )
        ]
        result = expand_context(files, context_lines=3)
        # With 3 context lines, hunks at 1-3 and 5-7 overlap
        assert len(result[0].hunks) == 1

    def test_no_merge_distant_hunks(self):
        hunks = [
            HunkInfo(1, 3, 1, 3, "hunk1"),
            HunkInfo(100, 3, 100, 3, "hunk2"),
        ]
        files = [
            FileDiff(
                path="a.py",
                change_type=FileChangeType.MODIFIED,
                hunks=hunks,
                added_lines=2,
                deleted_lines=0,
            )
        ]
        result = expand_context(files, context_lines=3)
        assert len(result[0].hunks) == 2

    def test_single_hunk_unchanged(self):
        files = [
            FileDiff(
                path="a.py",
                change_type=FileChangeType.MODIFIED,
                hunks=[HunkInfo(10, 5, 10, 5, "content")],
                added_lines=1,
                deleted_lines=0,
            )
        ]
        result = expand_context(files)
        assert len(result) == 1
        assert result[0].hunks[0].content == "content"


class TestBuildFileContextString:
    def test_basic_format(self, sample_file_diff: FileDiff):
        result = build_file_context_string(sample_file_diff)
        assert "src/utils.py" in result
        assert "modified" in result
        assert "```python" in result

    def test_renamed_file(self):
        f = FileDiff(
            path="new_name.py",
            change_type=FileChangeType.RENAMED,
            old_path="old_name.py",
            hunks=[HunkInfo(1, 1, 1, 1, "content")],
            added_lines=0,
            deleted_lines=0,
        )
        result = build_file_context_string(f)
        assert "old_name.py" in result
        assert "renamed" in result

    def test_added_deleted_lines(self, sample_file_diff: FileDiff):
        result = build_file_context_string(sample_file_diff)
        assert "+2" in result
        assert "-1" in result
