"""Tests for the indexing pipeline."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from mira.config import MiraConfig
from mira.index.indexer import (
    _build_file_summary,
    _content_hash,
    _parse_summarize_response,
    _should_index,
    index_diff,
    index_repo,
)
from mira.index.store import IndexStore


class TestShouldIndex:
    def test_python_file(self):
        assert _should_index("src/main.py") is True

    def test_javascript_file(self):
        assert _should_index("lib/index.js") is True

    def test_typescript_file(self):
        assert _should_index("src/component.tsx") is True

    def test_lock_file(self):
        assert _should_index("package-lock.json") is False

    def test_image_file(self):
        assert _should_index("logo.png") is False

    def test_node_modules(self):
        assert _should_index("node_modules/pkg/index.js") is False

    def test_vendor(self):
        assert _should_index("vendor/lib/main.go") is False

    def test_binary(self):
        assert _should_index("app.exe") is False

    def test_min_js(self):
        assert _should_index("bundle.min.js") is False

    def test_unknown_extension(self):
        assert _should_index("README.md") is False


class TestContentHash:
    def test_deterministic(self):
        assert _content_hash("hello") == _content_hash("hello")

    def test_different_content(self):
        assert _content_hash("hello") != _content_hash("world")


class TestParseSummarizeResponse:
    def test_files_key(self):
        raw = json.dumps({"files": [{"path": "a.py", "summary": "Test"}]})
        result = _parse_summarize_response(raw)
        assert len(result) == 1
        assert result[0]["path"] == "a.py"

    def test_list_format(self):
        raw = json.dumps([{"path": "a.py", "summary": "Test"}])
        result = _parse_summarize_response(raw)
        assert len(result) == 1

    def test_invalid_json(self):
        result = _parse_summarize_response("not json")
        assert result == []

    def test_empty_response(self):
        result = _parse_summarize_response("{}")
        assert result == []


class TestBuildFileSummary:
    def test_basic(self):
        data = {
            "language": "python",
            "summary": "A test file.",
            "symbols": [
                {
                    "name": "foo", "kind": "function",
                    "signature": "def foo()",
                    "description": "Does foo",
                },
            ],
            "imports": ["src/bar.py"],
            "symbol_references": [
                {"source": "foo", "calls": [{"path": "src/bar.py", "symbol": "bar_func"}]},
            ],
        }
        fs = _build_file_summary("test.py", "content", data)
        assert fs.path == "test.py"
        assert fs.language == "python"
        assert fs.summary == "A test file."
        assert len(fs.symbols) == 1
        assert fs.symbols[0].name == "foo"
        assert fs.imports == ["src/bar.py"]
        assert len(fs.symbol_refs) == 1
        assert fs.symbol_refs[0] == ("foo", "src/bar.py", "bar_func")

    def test_missing_fields(self):
        fs = _build_file_summary("empty.py", "content", {})
        assert fs.path == "empty.py"
        assert fs.symbols == []
        assert fs.imports == []


@pytest.mark.asyncio
class TestIndexRepo:
    async def test_index_repo_basic(self, tmp_path):
        """Test full repo indexing with mocked GitHub API and LLM."""
        store = IndexStore(str(tmp_path / "test.db"))

        mock_llm = AsyncMock()
        mock_llm.complete = AsyncMock(return_value=json.dumps({
            "files": [
                {
                    "path": "src/main.py",
                    "language": "python",
                    "summary": "Main entry point.",
                    "symbols": [{
                        "name": "main", "kind": "function",
                        "signature": "def main()",
                        "description": "Entry",
                    }],
                    "imports": [],
                    "symbol_references": [],
                },
            ],
        }))

        with patch(
            "mira.index.indexer._fetch_repo_tree",
            return_value=["src/main.py", "README.md"],
        ), patch(
            "mira.index.indexer._fetch_file_content",
            return_value="print('hello')",
        ):
            count = await index_repo(
                owner="test", repo="repo", token="fake-token",
                config=MiraConfig(), store=store, llm=mock_llm, full=True,
            )

        assert count == 1
        summary = store.get_summary("src/main.py")
        assert summary is not None
        assert summary.summary == "Main entry point."
        store.close()


@pytest.mark.asyncio
class TestIndexDiff:
    async def test_index_diff_basic(self, tmp_path):
        """Test incremental indexing with mocked content fetch and LLM."""
        store = IndexStore(str(tmp_path / "test.db"))

        mock_llm = AsyncMock()
        mock_llm.complete = AsyncMock(return_value=json.dumps({
            "files": [
                {
                    "path": "src/utils.py",
                    "language": "python",
                    "summary": "Utility functions.",
                    "symbols": [],
                    "imports": [],
                    "symbol_references": [],
                },
            ],
        }))

        with patch("mira.index.indexer._fetch_file_content", return_value="def helper(): pass"):
            count = await index_diff(
                owner="test", repo="repo", token="fake-token",
                config=MiraConfig(), store=store, llm=mock_llm,
                changed_paths=["src/utils.py"],
            )

        assert count == 1
        assert store.get_summary("src/utils.py") is not None
        store.close()

    async def test_index_diff_removes_deleted(self, tmp_path):
        store = IndexStore(str(tmp_path / "test.db"))
        from mira.index.store import FileSummary
        store.upsert_summary(FileSummary(
            path="old.py", language="python",
            summary="Old.", content_hash="h",
        ))

        mock_llm = AsyncMock()
        mock_llm.complete = AsyncMock(return_value='{"files": []}')

        await index_diff(
            owner="test", repo="repo", token="fake-token",
            config=MiraConfig(), store=store, llm=mock_llm,
            removed_paths=["old.py"],
        )

        assert store.get_summary("old.py") is None
        store.close()
