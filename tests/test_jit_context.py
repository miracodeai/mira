"""Tests for just-in-time cross-file context (unindexed-repo fallback)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mira.index.jit_context import (
    build_jit_cross_file_context,
    extract_import_candidates,
)
from mira.models import FileChangeType, FileDiff


def _file(path: str) -> FileDiff:
    return FileDiff(
        path=path,
        change_type=FileChangeType.MODIFIED,
        language=path.rsplit(".", 1)[-1] if "." in path else "",
        added_lines=1,
        deleted_lines=0,
        hunks=[],
    )


class _FakeFetcher:
    """Synchronous source map dressed up as an async fetcher."""

    def __init__(self, sources: dict[str, str]):
        self._sources = sources

    async def fetch(self, path: str) -> str | None:
        return self._sources.get(path)


class TestExtractImportCandidates:
    def test_python_relative_resolution(self):
        src = "from foo.bar import baz\nimport util\n"
        cands = extract_import_candidates(src, "python", "pkg/sub/main.py")
        # Should include both same-dir and dotted-path resolutions.
        assert "foo/bar.py" in cands
        assert "foo/bar/__init__.py" in cands
        assert "pkg/sub/foo/bar.py" in cands
        assert "util.py" in cands

    def test_python_skips_blank(self):
        cands = extract_import_candidates("", "python", "x.py")
        assert cands == []

    def test_typescript_relative(self):
        src = "import {x} from './utils'\nimport y from '../shared/y'"
        cands = extract_import_candidates(src, "typescript", "src/feature/main.ts")
        # ./utils → src/feature/utils + extension permutations
        assert "src/feature/utils.ts" in cands
        assert "src/feature/utils/index.ts" in cands
        # ../shared/y → src/shared/y...
        assert "src/shared/y.ts" in cands

    def test_typescript_skips_npm_packages(self):
        src = "import React from 'react'\nimport {z} from '@scope/pkg'"
        cands = extract_import_candidates(src, "typescript", "src/main.ts")
        assert cands == []

    def test_ruby_require_relative(self):
        src = "require_relative './helpers'\nrequire 'some/lib'"
        cands = extract_import_candidates(src, "ruby", "app/main.rb")
        # require_relative './helpers' → app/helpers.rb
        assert "app/helpers.rb" in cands

    def test_unknown_language_returns_empty(self):
        cands = extract_import_candidates("import x", "rust", "main.rs")
        assert cands == []


class TestBuildJITContext:
    @pytest.mark.asyncio
    async def test_empty_when_no_changed_files(self):
        out = await build_jit_cross_file_context(
            changed_files=[],
            source_fetcher=_FakeFetcher({}),
            repo_tree=set(),
        )
        assert out == ""

    @pytest.mark.asyncio
    async def test_empty_when_fetcher_is_none(self):
        out = await build_jit_cross_file_context(
            changed_files=[_file("x.py")],
            source_fetcher=None,
            repo_tree=None,
        )
        assert out == ""

    @pytest.mark.asyncio
    async def test_pulls_in_imported_python_file(self):
        """Changed file imports `helpers` — JIT should fetch helpers.py and
        inline its symbols."""
        sources = {
            "app/main.py": "from app.helpers import compute\n\ndef run():\n    return compute(1)\n",
            "app/helpers.py": "def compute(x):\n    return x * 2\n",
        }
        out = await build_jit_cross_file_context(
            changed_files=[_file("app/main.py")],
            source_fetcher=_FakeFetcher(sources),
            repo_tree={"app/main.py", "app/helpers.py"},
        )
        assert "app/helpers.py" in out
        assert "compute" in out

    @pytest.mark.asyncio
    async def test_skips_candidates_not_in_tree(self):
        """When repo_tree is provided, only paths that exist should be fetched."""
        sources = {
            "app/main.py": "from app.helpers import compute\n",
            # helpers.py exists in sources but NOT in tree → should be skipped
            "app/helpers.py": "def compute(): pass\n",
        }
        out = await build_jit_cross_file_context(
            changed_files=[_file("app/main.py")],
            source_fetcher=_FakeFetcher(sources),
            repo_tree={"app/main.py"},  # helpers.py NOT listed
        )
        assert "app/helpers.py" not in out

    @pytest.mark.asyncio
    async def test_skips_changed_files_themselves(self):
        """If a changed file imports another changed file, don't duplicate it
        in JIT — the regular source-tier already shows it."""
        sources = {
            "a.py": "from b import x\n",
            "b.py": "def x(): pass\n",
        }
        out = await build_jit_cross_file_context(
            changed_files=[_file("a.py"), _file("b.py")],
            source_fetcher=_FakeFetcher(sources),
            repo_tree={"a.py", "b.py"},
        )
        assert "b.py (imported by" not in out

    @pytest.mark.asyncio
    async def test_respects_char_budget(self):
        """A tight char_budget caps how much JIT context gets emitted."""
        sources = {
            "a.py": "from helpers import x\n",
            "helpers.py": "def x():\n    " + "y = 1\n    " * 200,
        }
        out = await build_jit_cross_file_context(
            changed_files=[_file("a.py")],
            source_fetcher=_FakeFetcher(sources),
            repo_tree={"a.py", "helpers.py"},
            char_budget=200,
        )
        assert len(out) <= 600  # header + small block, well under unbounded

    @pytest.mark.asyncio
    async def test_continues_when_one_fetch_fails(self):
        """A failing fetch shouldn't abort the whole pass."""

        async def flaky_fetch(path):
            if path == "app/main.py":
                return "from app.helpers import x\nfrom app.utils import y\n"
            if path == "app/helpers.py":
                raise RuntimeError("network blip")
            if path == "app/utils.py":
                return "def y(): pass\n"
            return None

        fetcher = AsyncMock()
        fetcher.fetch.side_effect = flaky_fetch
        out = await build_jit_cross_file_context(
            changed_files=[_file("app/main.py")],
            source_fetcher=fetcher,
            repo_tree={"app/main.py", "app/helpers.py", "app/utils.py"},
        )
        # utils.py made it in despite helpers.py failing
        assert "app/utils.py" in out
