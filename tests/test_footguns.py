"""Tests for the framework-footguns prompt section."""

from __future__ import annotations

from mira.llm.prompts.footguns import get_footguns_for_files
from mira.models import FileChangeType, FileDiff


def _file(path: str) -> FileDiff:
    return FileDiff(
        path=path,
        change_type=FileChangeType.MODIFIED,
        language="",
        added_lines=1,
        deleted_lines=0,
        hunks=[],
    )


class TestGetFootguns:
    def test_python_file_includes_python_section(self):
        out = get_footguns_for_files([_file("app/main.py")])
        assert "### Python" in out
        # Specific known rule
        assert "Negative slicing on Django QuerySets" in out

    def test_typescript_file_includes_ts_section(self):
        out = get_footguns_for_files([_file("src/main.ts")])
        assert "### Typescript" in out
        assert "forEach" in out

    def test_go_file_includes_go_section(self):
        out = get_footguns_for_files([_file("cmd/main.go")])
        assert "### Go" in out
        assert "Loop-variable capture" in out

    def test_universal_rules_always_included(self):
        out = get_footguns_for_files([_file("a.py")])
        assert "### Cross-language" in out
        assert "TOCTOU" in out

    def test_unknown_extension_returns_empty(self):
        # No language match, but universal rules still render
        out = get_footguns_for_files([_file("README.md")])
        # README.md isn't in EXT_TO_LANG, so no language sections.
        # Universal rules are always there.
        assert "### Python" not in out
        assert "TOCTOU" in out

    def test_multi_language_pr_includes_all_relevant(self):
        out = get_footguns_for_files(
            [
                _file("api/handler.py"),
                _file("ui/main.tsx"),
                _file("backend/server.go"),
            ]
        )
        assert "### Python" in out
        assert "### Typescript" in out
        assert "### Go" in out

    def test_empty_input(self):
        # No files → only universal rules
        out = get_footguns_for_files([])
        assert "### Python" not in out
        assert "TOCTOU" in out
