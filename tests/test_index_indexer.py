"""Tests for the indexing pipeline."""

from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from mira.config import MiraConfig
from mira.index.indexer import (
    _build_file_summary,
    _content_hash,
    _index_conventions,
    _parse_summarize_response,
    _read_paths,
    _should_index,
    _strip_code_fences,
    index_diff,
    index_repo,
)
from mira.index.store import IndexStore
from mira.llm import create_llm


class TestShouldIndex:
    def test_python_file(self):
        assert _should_index("src/main.py") is True

    def test_javascript_file(self):
        assert _should_index("lib/index.js") is True

    def test_typescript_file(self):
        assert _should_index("src/component.tsx") is True

    def test_lua_file(self):
        assert _should_index("src/main.lua") is True

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

    def test_user_exclude_pattern(self):
        assert _should_index("app/generated/api.py", ["*/generated/*"]) is False

    def test_user_exclude_glob_by_extension(self):
        assert _should_index("src/schema.proto", ["*.proto"]) is False

    def test_user_exclude_does_not_affect_others(self):
        assert _should_index("src/main.py", ["*.proto"]) is True


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

    def test_markdown_fenced_json(self):
        inner = json.dumps({"files": [{"path": "a.py", "summary": "Test"}]})
        raw = f"```json\n{inner}\n```"
        result = _parse_summarize_response(raw)
        assert len(result) == 1
        assert result[0]["path"] == "a.py"

    def test_markdown_fenced_no_lang(self):
        inner = json.dumps({"files": [{"path": "b.py", "summary": "B"}]})
        raw = f"```\n{inner}\n```"
        result = _parse_summarize_response(raw)
        assert len(result) == 1

    def test_unescaped_backslash_in_string(self):
        # DeepSeek-style: PHP namespace backslashes left unescaped (issue #96).
        raw = '{"files": [{"path": "a.php", "summary": "Model in \\App\\Models namespace"}]}'
        result = _parse_summarize_response(raw)
        assert len(result) == 1
        assert result[0]["summary"] == "Model in \\App\\Models namespace"

    def test_valid_escapes_preserved(self):
        raw = json.dumps({"files": [{"path": "a.py", "summary": 'tab\there "quote"'}]})
        result = _parse_summarize_response(raw)
        assert result[0]["summary"] == 'tab\there "quote"'


class TestStripCodeFences:
    def test_json_fence(self):
        assert _strip_code_fences('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_plain_fence(self):
        assert _strip_code_fences("```\nhello\n```") == "hello"

    def test_no_fence(self):
        assert _strip_code_fences('{"a": 1}') == '{"a": 1}'

    def test_whitespace(self):
        assert _strip_code_fences('  ```json\n{"a": 1}\n```  ') == '{"a": 1}'


class TestBuildFileSummary:
    def test_basic(self):
        data = {
            "language": "python",
            "summary": "A test file.",
            "symbols": [
                {
                    "name": "foo",
                    "kind": "function",
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

    def test_null_symbol_fields_coerced(self):
        # Models emit explicit nulls; columns are NOT NULL (issue #96).
        data = {"symbols": [{"name": "foo", "kind": None, "signature": None, "description": None}]}
        fs = _build_file_summary("a.py", "content", data)
        assert len(fs.symbols) == 1
        assert fs.symbols[0].kind == "function"
        assert fs.symbols[0].signature == ""
        assert fs.symbols[0].description == ""

    def test_nameless_symbol_skipped(self):
        data = {"symbols": [{"name": None, "signature": "x"}, {"name": "ok"}]}
        fs = _build_file_summary("a.py", "content", data)
        assert [s.name for s in fs.symbols] == ["ok"]


class _FakeFetcher:
    """In-memory RepoFetcher for indexer tests."""

    def __init__(self, tree=None, contents=None, tarball=None, branch="main"):
        self._tree = tree or []
        self._contents = contents
        self._tarball = tarball
        self._branch = branch

    async def default_branch(self, owner, repo):
        return self._branch

    async def repo_tree(self, owner, repo, branch):
        return list(self._tree)

    async def file_content(self, owner, repo, path, ref, semaphore=None):
        if isinstance(self._contents, dict):
            return self._contents.get(path)
        return self._contents

    async def repo_tarball(self, owner, repo, ref, max_file_size=1_048_576, **kwargs):
        return self._tarball


@pytest.mark.asyncio
class TestIndexRepo:
    @pytest.mark.parametrize("api_style", ["chat", "responses"])
    @pytest.mark.parametrize("policy", ["default", "omit", 0.7])
    @pytest.mark.parametrize("incremental", [False, True])
    async def test_indexing_temperature(
        self, tmp_path, monkeypatch, api_style, policy, incremental
    ):
        requests = []
        payload = {
            "files": [{"path": "src/main.py", "summary": "Adds integers."}],
            "directories": [{"path": "src", "summary": "Arithmetic helpers."}],
            "summary": "Arithmetic helpers.",
        }

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            if policy == "omit" and "temperature" in body:
                return httpx.Response(400, json={"error": {"param": "temperature"}})
            text = json.dumps(payload)
            envelope = (
                {"choices": [{"message": {"content": text}}]}
                if api_style == "chat"
                else {
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        }
                    ]
                }
            )
            return httpx.Response(200, json=envelope)

        client_class = httpx.AsyncClient
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: client_class(transport=httpx.MockTransport(respond), **kw),
        )
        llm_config = {
            "model": "test-model",
            "api_key_env": "",
            "api_style": api_style,
            "temperature": 0.9,
        }
        if policy != "default":
            llm_config["indexing_temperature"] = policy
        config = MiraConfig(llm=llm_config)
        store = IndexStore(str(tmp_path / "index.db"))
        content = "def add(a, b):\n    return a + b\n" + "# arithmetic helper\n" * 40
        fetcher = _FakeFetcher(tree=["src/main.py"], contents={"src/main.py": content})
        try:
            if incremental:
                count = await index_diff(
                    "example",
                    "project",
                    config=config,
                    store=store,
                    llm=create_llm(config.llm),
                    fetcher=fetcher,
                    changed_paths=["src/main.py"],
                )
            else:
                count = await index_repo(
                    "example",
                    "project",
                    config=config,
                    store=store,
                    llm=create_llm(config.llm),
                    fetcher=fetcher,
                )
            assert count == 1
            assert store.get_summary("src/main.py").summary == "Adds integers."
            assert store.get_directory_summaries(["src"])["src"].summary == "Arithmetic helpers."
            assert len(requests) == 2
            if policy == "omit":
                assert all("temperature" not in body for body in requests)
            else:
                expected = 0.0 if policy == "default" else policy
                assert all(body["temperature"] == expected for body in requests)
            assert config.llm.temperature == 0.9
        finally:
            store.close()

    @pytest.mark.parametrize("policy", ["omit", 0.7])
    async def test_bedrock_keeps_existing_indexing_temperature(self, tmp_path, policy):
        pytest.importorskip("boto3")
        config = MiraConfig(llm={"provider": "bedrock", "indexing_temperature": policy})
        payload = {
            "files": [{"path": "src/main.py", "summary": "Arithmetic."}],
            "directories": [{"path": "src", "summary": "Helpers."}],
        }
        with patch("boto3.Session") as session:
            client = session.return_value.client.return_value
            client.converse.return_value = {
                "output": {
                    "message": {
                        "content": [{"toolUse": {"name": "submit_json_response", "input": payload}}]
                    }
                }
            }
            store = IndexStore(str(tmp_path / "index.db"))
            try:
                count = await index_repo(
                    "example",
                    "project",
                    config=config,
                    store=store,
                    llm=create_llm(config.llm),
                    fetcher=_FakeFetcher(
                        tree=["src/main.py"], contents={"src/main.py": "# helper\n" * 100}
                    ),
                )
                assert count == 1
                assert client.converse.call_count == 2
                assert all(
                    call.kwargs["inferenceConfig"]["temperature"] == 0.0
                    for call in client.converse.call_args_list
                )
            finally:
                store.close()

    @pytest.mark.parametrize("provider", ["codex-cli", "codex_cli", "codex"])
    async def test_codex_keeps_sampling_out_of_argv(self, tmp_path, provider):
        payload = {
            "files": [{"path": "src/main.py", "summary": "Arithmetic."}],
            "directories": [{"path": "src", "summary": "Helpers."}],
        }
        script = tmp_path / "fake_codex.py"
        argv_file = tmp_path / "argv.json"
        script.write_text(
            f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
            + f"Path({str(argv_file)!r}).write_text(json.dumps(sys.argv))\n"
            + f"Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text({json.dumps(payload)!r})\n"
        )
        script.chmod(0o700)
        config = MiraConfig(
            llm={"provider": provider, "codex_command": str(script), "indexing_temperature": "omit"}
        )
        store = IndexStore(str(tmp_path / "index.db"))
        try:
            count = await index_repo(
                "example",
                "project",
                config=config,
                store=store,
                llm=create_llm(config.llm),
                fetcher=_FakeFetcher(
                    tree=["src/main.py"], contents={"src/main.py": "# helper\n" * 100}
                ),
            )
            assert count == 1
            assert store.get_directory_summaries(["src"])["src"].summary == "Helpers."
            assert "temperature" not in " ".join(json.loads(argv_file.read_text()))
        finally:
            store.close()

    async def test_index_repo_basic(self, tmp_path):
        """Test full repo indexing with mocked GitHub API and LLM."""
        store = IndexStore(str(tmp_path / "test.db"))

        mock_llm = AsyncMock()
        mock_llm.complete = AsyncMock(
            return_value=json.dumps(
                {
                    "files": [
                        {
                            "path": "src/main.py",
                            "language": "python",
                            "summary": "Main entry point.",
                            "symbols": [
                                {
                                    "name": "main",
                                    "kind": "function",
                                    "signature": "def main()",
                                    "description": "Entry",
                                }
                            ],
                            "imports": [],
                            "symbol_references": [],
                        },
                    ],
                }
            )
        )

        # Content needs to exceed the trivial-file threshold so it routes
        # through the LLM path rather than the no-summary fast path.
        big_content = "# main entry point\n" + "print('hello')\n" * 100

        fetcher = _FakeFetcher(
            tree=["src/main.py", "README.md"],
            tarball={"src/main.py": big_content, "README.md": big_content},
        )
        count = await index_repo(
            owner="test",
            repo="repo",
            config=MiraConfig(),
            store=store,
            llm=mock_llm,
            full=True,
            fetcher=fetcher,
        )

        assert count == 1
        summary = store.get_summary("src/main.py")
        assert summary is not None
        assert summary.summary == "Main entry point."
        store.close()

    async def test_skips_files_over_size_limit(self, tmp_path):
        """Files above index.max_file_size are dropped before summarization."""
        store = IndexStore(str(tmp_path / "test.db"))
        mock_llm = AsyncMock()
        mock_llm.complete = AsyncMock()

        config = MiraConfig()
        config.index.max_file_size = 1_000
        big_content = "print('x')\n" * 500  # ~5 KB, over the 1 KB limit

        fetcher = _FakeFetcher(tree=["src/main.py"], tarball={"src/main.py": big_content})
        count = await index_repo(
            owner="test",
            repo="repo",
            config=config,
            store=store,
            llm=mock_llm,
            full=True,
            fetcher=fetcher,
        )

        assert count == 0
        mock_llm.complete.assert_not_called()
        store.close()


@pytest.mark.asyncio
class TestIndexDiff:
    async def test_index_diff_basic(self, tmp_path):
        """Test incremental indexing with mocked content fetch and LLM."""
        store = IndexStore(str(tmp_path / "test.db"))

        mock_llm = AsyncMock()
        mock_llm.complete = AsyncMock(
            return_value=json.dumps(
                {
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
                }
            )
        )

        count = await index_diff(
            owner="test",
            repo="repo",
            config=MiraConfig(),
            store=store,
            llm=mock_llm,
            changed_paths=["src/utils.py"],
            fetcher=_FakeFetcher(contents="def helper(): pass"),
        )

        assert count == 1
        assert store.get_summary("src/utils.py") is not None
        store.close()

    async def test_index_diff_refreshes_changed_lockfile(self, tmp_path):
        """A lockfile-only push must refresh the package inventory even though
        lockfiles are excluded from code indexing (#157)."""
        import asyncio

        store = IndexStore(str(tmp_path / "test.db"))
        store.replace_manifest_packages(
            "package-lock.json",
            [
                {
                    "name": "lodash",
                    "kind": "npm",
                    "version": "4.17.20",
                    "file_path": "package-lock.json",
                    "is_dev": False,
                }
            ],
        )

        lock = json.dumps(
            {
                "name": "app",
                "lockfileVersion": 3,
                "packages": {"node_modules/lodash": {"version": "4.17.21"}},
            }
        )
        mock_llm = AsyncMock()
        mock_llm.complete = AsyncMock(return_value='{"files": []}')

        with patch("mira.security.poller.poll_repo", new=AsyncMock()) as poll:
            count = await index_diff(
                owner="test",
                repo="repo",
                config=MiraConfig(),
                store=store,
                llm=mock_llm,
                changed_paths=["package-lock.json"],
                fetcher=_FakeFetcher(contents=lock),
            )
            await asyncio.sleep(0)  # let the fire-and-forget vuln poll run

        # Not code-indexed (excluded), but the inventory picked up the bump.
        assert count == 0
        versions = {(p.name, p.version) for p in store.list_manifest_packages()}
        assert ("lodash", "4.17.21") in versions
        assert ("lodash", "4.17.20") not in versions
        mock_llm.complete.assert_not_called()
        poll.assert_called_once_with("test", "repo")
        store.close()

    async def test_index_diff_clears_removed_manifest(self, tmp_path):
        """Deleting a manifest in a push drops its package rows."""
        store = IndexStore(str(tmp_path / "test.db"))
        store.replace_manifest_packages(
            "poetry.lock",
            [
                {
                    "name": "requests",
                    "kind": "pypi",
                    "version": "2.31.0",
                    "file_path": "poetry.lock",
                    "is_dev": False,
                }
            ],
        )

        mock_llm = AsyncMock()
        mock_llm.complete = AsyncMock(return_value='{"files": []}')

        with patch("mira.security.poller.poll_repo", new=AsyncMock()):
            await index_diff(
                owner="test",
                repo="repo",
                config=MiraConfig(),
                store=store,
                llm=mock_llm,
                removed_paths=["poetry.lock"],
                fetcher=_FakeFetcher(),
            )

        assert store.list_manifest_packages() == []
        store.close()

    async def test_index_diff_removes_deleted(self, tmp_path):
        store = IndexStore(str(tmp_path / "test.db"))
        from mira.index.store import FileSummary

        store.upsert_summary(
            FileSummary(
                path="old.py",
                language="python",
                summary="Old.",
                content_hash="h",
            )
        )

        mock_llm = AsyncMock()
        mock_llm.complete = AsyncMock(return_value='{"files": []}')

        await index_diff(
            owner="test",
            repo="repo",
            token="fake-token",
            config=MiraConfig(),
            store=store,
            llm=mock_llm,
            removed_paths=["old.py"],
        )

        assert store.get_summary("old.py") is None
        store.close()


@pytest.mark.asyncio
class TestReadPaths:
    """The tarball cache holds indexable paths only, so a miss means "not cached"."""

    async def test_paths_absent_from_the_cache_are_fetched(self):
        fetcher = _FakeFetcher(contents={"AGENTS.md": "# rules", "go.mod": "module x"})

        resolved = await _read_paths(
            "o",
            "r",
            fetcher,
            "main",
            ["AGENTS.md", "go.mod"],
            asyncio.Semaphore(4),
            cached_contents={"main.py": "print()"},
        )

        assert resolved == {"AGENTS.md": "# rules", "go.mod": "module x"}

    async def test_cached_contents_are_not_refetched(self):
        fetcher = _FakeFetcher(contents={"AGENTS.md": "fetched"})

        resolved = await _read_paths(
            "o",
            "r",
            fetcher,
            "main",
            ["AGENTS.md"],
            asyncio.Semaphore(4),
            cached_contents={"AGENTS.md": "cached"},
        )

        assert resolved == {"AGENTS.md": "cached"}

    async def test_a_path_in_neither_resolves_to_none(self):
        fetcher = _FakeFetcher(contents={})

        resolved = await _read_paths(
            "o", "r", fetcher, "main", ["STYLE.md"], asyncio.Semaphore(4), cached_contents={}
        )

        assert resolved == {"STYLE.md": None}


@pytest.mark.asyncio
class TestIndexConventionsCacheMiss:
    async def test_conventions_load_when_the_tarball_omits_them(self):
        """`_should_index` rejects AGENTS.md, so it is never in the tarball cache."""
        fetcher = _FakeFetcher(
            contents={"AGENTS.md": "# Team rules\n\n- Always flag bare excepts.\n"}
        )

        store = IndexStore(":memory:")
        try:
            with patch("mira.dashboard.api._app_db") as app_db:
                await _index_conventions(
                    "o",
                    "r",
                    fetcher,
                    "main",
                    ["AGENTS.md", "main.py"],
                    asyncio.Semaphore(4),
                    cached_contents={"main.py": "print()"},
                    store=store,
                    run=store.claim_index_run(),
                )
        finally:
            store.close()

        app_db.set_repo_conventions.assert_called_once()
        owner, repo, extracted = app_db.set_repo_conventions.call_args.args
        assert (owner, repo) == ("o", "r")
        assert "bare excepts" in extracted
