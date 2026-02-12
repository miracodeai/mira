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

    def test_deleted_file(self):
        diff = (
            "diff --git a/old.py b/old.py\n"
            "deleted file mode 100644\n"
            "index abc1234..0000000\n"
            "--- a/old.py\n"
            "+++ /dev/null\n"
            "@@ -1,3 +0,0 @@\n"
            "-line1\n-line2\n-line3\n"
        )
        patch = parse_diff(diff)
        assert patch.total_files == 1
        assert patch.files[0].change_type == FileChangeType.DELETED
        assert patch.files[0].deleted_lines == 3

    def test_renamed_file(self):
        diff = (
            "diff --git a/old_name.py b/new_name.py\n"
            "similarity index 95%\n"
            "rename from old_name.py\n"
            "rename to new_name.py\n"
            "index abc1234..def5678 100644\n"
            "--- a/old_name.py\n"
            "+++ b/new_name.py\n"
            "@@ -1,2 +1,2 @@\n"
            " keep\n-old\n+new\n"
        )
        patch = parse_diff(diff)
        assert patch.total_files == 1
        f = patch.files[0]
        assert f.change_type == FileChangeType.RENAMED
        assert f.path == "new_name.py"
        assert f.old_path == "old_name.py"

    def test_language_detection(self):
        diff = (
            "diff --git a/app.tsx b/app.tsx\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/app.tsx\n"
            "@@ -0,0 +1 @@\n"
            "+export default function App() {}\n"
        )
        patch = parse_diff(diff)
        assert patch.files[0].language == "typescript"

    def test_unknown_extension_empty_language(self):
        diff = (
            "diff --git a/data.xyz b/data.xyz\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/data.xyz\n"
            "@@ -0,0 +1 @@\n"
            "+stuff\n"
        )
        patch = parse_diff(diff)
        assert patch.files[0].language == ""
