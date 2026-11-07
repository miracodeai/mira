"""Tests for file filtering."""

from __future__ import annotations

from mira.config import FilterConfig
from mira.core.file_filter import filter_files
from mira.models import FileChangeType, FileDiff, HunkInfo


def _make_file(
    path: str,
    change_type: FileChangeType = FileChangeType.MODIFIED,
    **kwargs,
) -> FileDiff:
    return FileDiff(
        path=path,
        change_type=change_type,
        hunks=kwargs.get("hunks", [HunkInfo(1, 5, 1, 5, "content")]),
        added_lines=kwargs.get("added_lines", 5),
        deleted_lines=kwargs.get("deleted_lines", 2),
        is_binary=kwargs.get("is_binary", False),
    )


class TestFileFilter:
    def test_excludes_binary(self):
        files = [_make_file("image.png", is_binary=True)]
        result = filter_files(files, FilterConfig())
        assert len(result) == 0

    def test_excludes_lockfiles(self):
        files = [
            _make_file("package-lock.json"),
            _make_file("yarn.lock"),
            _make_file("src/app.py"),
        ]
        result = filter_files(files, FilterConfig())
        assert len(result) == 1
        assert result[0].path == "src/app.py"

    def test_excludes_deleted(self):
        files = [_make_file("old.py", FileChangeType.DELETED)]
        result = filter_files(files, FilterConfig(exclude_deleted=True))
        assert len(result) == 0

    def test_includes_deleted_when_configured(self):
        files = [_make_file("old.py", FileChangeType.DELETED)]
        result = filter_files(files, FilterConfig(exclude_deleted=False))
        assert len(result) == 1

    def test_excludes_generated(self):
        hunk = HunkInfo(1, 5, 1, 5, "# DO NOT EDIT - auto generated\ncode here")
        files = [_make_file("generated.py", hunks=[hunk])]
        result = filter_files(files, FilterConfig())
        assert len(result) == 0

    def test_max_files_cap(self):
        files = [_make_file(f"file{i}.py") for i in range(10)]
        result = filter_files(files, FilterConfig(max_files=3))
        assert len(result) == 3

    def test_priority_sorting(self):
        files = [
            _make_file("added.py", FileChangeType.ADDED, added_lines=10, deleted_lines=0),
            _make_file("modified.py", FileChangeType.MODIFIED, added_lines=20, deleted_lines=5),
        ]
        result = filter_files(files, FilterConfig())
        assert result[0].path == "modified.py"

    def test_glob_pattern_matching(self):
        files = [
            _make_file("src/app.min.js"),
            _make_file("src/app.js"),
        ]
        result = filter_files(files, FilterConfig())
        assert len(result) == 1
        assert result[0].path == "src/app.js"

    def test_empty_input(self):
        result = filter_files([], FilterConfig())
        assert result == []


class TestGitlinkFilter:
    def test_excludes_submodule_pointer(self):
        """A `mode 160000` gitlink carries no source — nothing to review."""
        hunk = HunkInfo(
            0, 0, 1, 1, "@@ -0,0 +1 @@\n+Subproject commit a5d290951def84afdcc4c88d2f1f20023fc36e2a"
        )
        files = [
            _make_file(
                "vendor-repo", FileChangeType.ADDED, hunks=[hunk], added_lines=1, deleted_lines=0
            ),
            _make_file("src/app.py"),
        ]
        result = filter_files(files, FilterConfig())
        assert [f.path for f in result] == ["src/app.py"]

    def test_submodule_bump_is_excluded(self):
        hunk = HunkInfo(
            1,
            1,
            1,
            1,
            "@@ -1 +1 @@\n-Subproject commit 1111111111111111111111111111111111111111"
            "\n+Subproject commit 2222222222222222222222222222222222222222",
        )
        files = [_make_file("deps/lib", hunks=[hunk], added_lines=1, deleted_lines=1)]
        assert filter_files(files, FilterConfig()) == []

    def test_real_file_mentioning_subproject_is_kept(self):
        hunk = HunkInfo(1, 2, 1, 2, "@@ -1,2 +1,2 @@\n+# Subproject commit notes\n+x = 1")
        files = [_make_file("notes.py", hunks=[hunk])]
        assert len(filter_files(files, FilterConfig())) == 1
