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

    def test_java_resolves_fqn_via_repo_tree(self):
        src = (
            "package com.acme.svc;\n"
            "import com.acme.crypto.KeystoreFactory;\n"
            "import static com.acme.util.Validators.requireNonNull;\n"
        )
        tree = {
            "src/main/java/com/acme/svc/Main.java",
            "src/main/java/com/acme/crypto/KeystoreFactory.java",
            "src/main/java/com/acme/util/Validators.java",
        }
        cands = extract_import_candidates(
            src, "java", "src/main/java/com/acme/svc/Main.java", repo_tree=tree
        )
        assert "src/main/java/com/acme/crypto/KeystoreFactory.java" in cands
        assert "src/main/java/com/acme/util/Validators.java" in cands

    def test_java_drops_wildcard_imports(self):
        src = "import com.acme.crypto.*;\n"
        tree = {"src/main/java/com/acme/crypto/Foo.java"}
        cands = extract_import_candidates(src, "java", "Main.java", repo_tree=tree)
        assert cands == []

    def test_java_prefers_path_with_matching_package(self):
        # Two files named Util.java exist in different packages — the one in
        # the imported package should win.
        src = "import com.acme.crypto.Util;\n"
        tree = {
            "src/main/java/com/acme/web/Util.java",
            "src/main/java/com/acme/crypto/Util.java",
        }
        cands = extract_import_candidates(src, "java", "Main.java", repo_tree=tree)
        assert cands[0] == "src/main/java/com/acme/crypto/Util.java"

    def test_go_resolves_module_path_tail(self):
        src = (
            "package x\n\nimport (\n"
            '    "github.com/grafana/grafana/pkg/services/auth/anon"\n'
            '    "fmt"\n'
            ")\n"
        )
        tree = {
            "pkg/services/auth/anon/store.go",
            "pkg/services/auth/anon/store_test.go",  # excluded
            "pkg/services/auth/anon/anon.go",
            "fmt/fmt.go",  # accidental match — this isn't really stdlib but we skip "fmt" by stdlib hint
        }
        cands = extract_import_candidates(src, "go", "pkg/svc/handler.go", repo_tree=tree)
        assert (
            "pkg/services/auth/anon/store.go" in cands or "pkg/services/auth/anon/anon.go" in cands
        )
        # Test files are excluded
        assert all("_test.go" not in p for p in cands)
        # `fmt` skipped as stdlib
        assert "fmt/fmt.go" not in cands

    def test_go_skips_stdlib_imports(self):
        src = 'import (\n    "net/http"\n    "encoding/json"\n)\n'
        tree = {"net/http/foo.go", "encoding/json/bar.go"}
        cands = extract_import_candidates(src, "go", "main.go", repo_tree=tree)
        assert cands == []

    def test_returns_empty_when_repo_tree_missing(self):
        # Java + Go need the tree; without it they should return nothing
        # rather than guess.
        src = "import com.acme.svc.Foo;\n"
        cands = extract_import_candidates(src, "java", "Main.java", repo_tree=None)
        assert cands == []
        cands_go = extract_import_candidates('import "x/y/z"\n', "go", "main.go", repo_tree=None)
        assert cands_go == []

    def test_go_does_not_match_string_constants_or_struct_tags(self):
        """Quoted strings in the body of a Go file aren't imports."""
        src = (
            "package main\n\n"
            'import (\n    "real/import/path"\n)\n\n'
            "type User struct {\n"
            '    Name string `json:"name" db:"users.name"`\n'
            "}\n\n"
            'var greeting = "hello world"\n'
            'func f() error { return fmt.Errorf("not an import") }\n'
        )
        tree = {
            "real/import/path/file.go",
            "users.name/file.go",  # would falsely match the loose regex
            "hello world/file.go",
        }
        cands = extract_import_candidates(src, "go", "main.go", repo_tree=tree)
        # Only the real import should resolve.
        assert cands == ["real/import/path/file.go"]

    def test_go_block_with_alias(self):
        src = (
            'import (\n    foo "real/path/foo"\n    . "real/path/dot"\n    _ "real/path/blank"\n)\n'
        )
        tree = {
            "real/path/foo/x.go",
            "real/path/dot/y.go",
            "real/path/blank/z.go",
        }
        cands = extract_import_candidates(src, "go", "main.go", repo_tree=tree)
        # Aliased and blank-imports should still resolve.
        assert any("real/path/foo" in p for p in cands)
        assert any("real/path/dot" in p for p in cands)
        assert any("real/path/blank" in p for p in cands)

    def test_enable_java_go_false_skips_resolution(self):
        java_src = "import com.acme.crypto.Keystore;\n"
        java_tree = {"src/main/java/com/acme/crypto/Keystore.java"}
        assert (
            extract_import_candidates(
                java_src, "java", "Main.java", repo_tree=java_tree, enable_java_go=False
            )
            == []
        )
        go_src = 'import "real/path"\n'
        go_tree = {"real/path/x.go"}
        assert (
            extract_import_candidates(
                go_src, "go", "main.go", repo_tree=go_tree, enable_java_go=False
            )
            == []
        )

    def test_java_capped_at_one_candidate(self):
        # If a class name appears in many files, we should return exactly one
        # — better to miss than to flood the prompt with off-topic matches.
        src = "import com.acme.svc.Util;\n"
        tree = {
            "a/b/c/Util.java",
            "x/y/z/Util.java",
            "deep/nested/path/Util.java",
        }
        cands = extract_import_candidates(src, "java", "Main.java", repo_tree=tree)
        assert len(cands) == 1


class TestGoModuleResolution:
    """With go.mod's module path, in-repo Go imports resolve deterministically."""

    TREE = {
        "go.mod",
        "pkg/services/auth/anon/anon.go",
        "pkg/services/auth/anon/store.go",
        "pkg/services/auth/anon/store_test.go",
        "pkg/services/auth/anon/deeper/inner.go",  # not a direct child — excluded
        "pkg/setting/setting.go",
    }

    def test_module_prefix_resolves_exact_package_dir(self):
        src = 'import "github.com/grafana/grafana/pkg/services/auth/anon"\n'
        cands = extract_import_candidates(
            src, "go", "main.go", repo_tree=self.TREE, go_module="github.com/grafana/grafana"
        )
        # Package-named file first, test files and subpackages excluded.
        assert cands[0] == "pkg/services/auth/anon/anon.go"
        assert "pkg/services/auth/anon/store.go" in cands
        assert all("_test.go" not in c and "deeper" not in c for c in cands)

    def test_external_dependency_returns_nothing(self):
        # Tail-match would have wrongly matched pkg/setting here.
        src = 'import "github.com/someone/else/pkg/setting"\n'
        cands = extract_import_candidates(
            src, "go", "main.go", repo_tree=self.TREE, go_module="github.com/grafana/grafana"
        )
        assert cands == []

    def test_no_module_falls_back_to_tail_match(self):
        src = 'import "github.com/grafana/grafana/pkg/services/auth/anon"\n'
        cands = extract_import_candidates(src, "go", "main.go", repo_tree=self.TREE)
        assert any("pkg/services/auth/anon/" in c for c in cands)

    def test_parse_go_module(self):
        from mira.index.jit_context import _parse_go_module

        assert (
            _parse_go_module("module github.com/grafana/grafana\n\ngo 1.21\n")
            == "github.com/grafana/grafana"
        )
        assert _parse_go_module("// just comments\n") is None
        assert _parse_go_module("") is None

    @pytest.mark.asyncio
    async def test_jit_context_fetches_go_mod_once(self):
        sources = {
            "go.mod": "module example.com/app\n",
            "cmd/main.go": 'package main\n\nimport "example.com/app/pkg/auth"\n',
            "pkg/auth/auth.go": (
                "package auth\n\n"
                "type Service struct {}\n\n"
                "func (s *Service) Login() error {\n\treturn nil\n}\n"
            ),
        }
        fetcher = _FakeFetcher(sources)
        out = await build_jit_cross_file_context(
            changed_files=[_file("cmd/main.go")],
            source_fetcher=fetcher,
            repo_tree=set(sources),
        )
        assert "pkg/auth/auth.go" in out
        assert "Login" in out


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
