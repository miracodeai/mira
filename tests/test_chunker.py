"""Tests for token-aware chunking."""

from __future__ import annotations

from mira.core.chunker import _file_token_estimate, chunk_files
from mira.models import FileChangeType, FileDiff, HunkInfo


def _make_file(path: str, content_size: int = 100) -> FileDiff:
    content = "x" * content_size
    return FileDiff(
        path=path,
        change_type=FileChangeType.MODIFIED,
        hunks=[HunkInfo(1, 10, 1, 10, content)],
        added_lines=10,
        deleted_lines=0,
    )


class TestChunkFiles:
    def test_single_file_single_chunk(self):
        files = [_make_file("a.py", 100)]
        chunks = chunk_files(files, max_tokens=10000)
        assert len(chunks) == 1
        assert len(chunks[0].files) == 1

    def test_multiple_files_fit_one_chunk(self):
        files = [_make_file(f"file{i}.py", 100) for i in range(3)]
        chunks = chunk_files(files, max_tokens=10000)
        assert len(chunks) == 1
        assert len(chunks[0].files) == 3

    def test_splits_into_multiple_chunks(self):
        files = [_make_file(f"file{i}.py", 4000) for i in range(5)]
        chunks = chunk_files(files, max_tokens=5000)
        assert len(chunks) > 1

    def test_oversized_file_gets_truncated(self):
        large = _make_file("big.py", 100000)
        chunks = chunk_files([large], max_tokens=5000)
        assert len(chunks) == 1
        # File should still be included (truncated)
        assert len(chunks[0].files) == 1

    def test_empty_input(self):
        chunks = chunk_files([], max_tokens=10000)
        assert chunks == []

    def test_token_estimate(self):
        f = _make_file("a.py", 400)
        est = _file_token_estimate(f)
        assert est > 0
        assert est < 400  # Should be roughly 100 tokens for 400 chars
