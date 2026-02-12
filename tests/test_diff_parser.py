"""Tests for diff parsing."""

from __future__ import annotations

from mira.core.diff_parser import parse_diff
from mira.models import FileChangeType


class TestParseDiff:
    def test_parse_sample_diff(self, sample_diff_text: str):
        patch = parse_diff(sample_diff_text)
        assert patch.total_files == 2
        assert patch.files[0].path == "src/utils.py"
        assert patch.files[0].change_type == FileChangeType.ADDED
        assert patch.files[0].language == "python"
        assert patch.files[0].added_lines == 25

    def test_parse_modified_file(self, sample_diff_text: str):
        patch = parse_diff(sample_diff_text)
        main_file = patch.files[1]
        assert main_file.path == "src/main.py"
        assert main_file.change_type == FileChangeType.MODIFIED
        assert len(main_file.hunks) == 1

    def test_empty_diff(self):
        patch = parse_diff("")
        assert patch.total_files == 0

    def test_whitespace_only_diff(self):
        patch = parse_diff("   \n\n  ")
        assert patch.total_files == 0

    def test_invalid_diff_returns_empty(self):
        # unidiff silently ignores unparseable content
        patch = parse_diff("this is not a valid diff format\n<<<>>>")
        assert patch.total_files == 0

    def test_diff_stats(self, sample_diff_text: str):
        patch = parse_diff(sample_diff_text)
        assert patch.total_additions > 0
        assert patch.total_deletions >= 0

    def test_hunk_content(self, sample_diff_text: str):
        patch = parse_diff(sample_diff_text)
        hunk = patch.files[0].hunks[0]
        assert hunk.target_start == 1
        assert "def run_command" in hunk.content
