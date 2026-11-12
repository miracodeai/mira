"""Tests for the agentic tool executor used on unindexed-repo reviews."""

from __future__ import annotations

import pytest

from mira.core.passes import agentic_review_loop
from mira.llm.agentic_tools import (
    AGENTIC_TOOLS,
    GREP_REPO_TOOL,
    READ_FILE_TOOL,
    AgenticToolExecutor,
)


class _FakeFetcher:
    def __init__(self, sources: dict[str, str | None]):
        self._sources = sources

    async def fetch(self, path: str) -> str | None:
        return self._sources.get(path)


def _executor(sources: dict[str, str | None], tree: list[str]) -> AgenticToolExecutor:
    return AgenticToolExecutor(source_fetcher=_FakeFetcher(sources), repo_tree=tree)


class TestSchemas:
    def test_tool_set_exposes_both_helpers(self):
        names = [t["function"]["name"] for t in AGENTIC_TOOLS]
        assert "read_file" in names
        assert "grep_repo" in names

    def test_read_file_requires_path(self):
        assert READ_FILE_TOOL["function"]["parameters"]["required"] == ["path"]

    def test_grep_repo_requires_pattern(self):
        assert GREP_REPO_TOOL["function"]["parameters"]["required"] == ["pattern"]


class TestReadFile:
    @pytest.mark.asyncio
    async def test_returns_numbered_content(self):
        ex = _executor({"src/a.py": "alpha\nbeta\n"}, ["src/a.py"])
        out = await ex.execute("read_file", {"path": "src/a.py"})
        assert "src/a.py" in out
        assert "    1  alpha" in out
        assert "    2  beta" in out

    @pytest.mark.asyncio
    async def test_truncates_huge_files(self):
        big = "x" * 20_000
        ex = _executor({"src/big.py": big}, ["src/big.py"])
        out = await ex.execute("read_file", {"path": "src/big.py"})
        assert "truncated" in out

    @pytest.mark.asyncio
    async def test_missing_path_returns_error_string(self):
        ex = _executor({}, [])
        out = await ex.execute("read_file", {})
        assert out.startswith("[error")

    @pytest.mark.asyncio
    async def test_unknown_path_in_tree_suggests_close_match(self):
        ex = _executor({}, ["src/auth/middleware.py", "src/util.py"])
        out = await ex.execute("read_file", {"path": "AUTH/middleware.py"})
        assert "not found" in out
        assert "src/auth/middleware.py" in out

    @pytest.mark.asyncio
    async def test_caches_repeated_reads(self):
        seen: list[str] = []

        class _Counting:
            async def fetch(self, path: str) -> str | None:
                seen.append(path)
                return "hello"

        ex = AgenticToolExecutor(source_fetcher=_Counting(), repo_tree=["src/a.py"])
        await ex.execute("read_file", {"path": "src/a.py"})
        await ex.execute("read_file", {"path": "src/a.py"})
        assert seen == ["src/a.py"]  # only fetched once


class TestGrepRepo:
    @pytest.mark.asyncio
    async def test_path_only_returns_matching_paths(self):
        ex = _executor({}, ["src/auth.py", "src/util.py", "tests/test_auth.py"])
        out = await ex.execute("grep_repo", {"pattern": "auth", "path_only": True})
        assert "src/auth.py" in out
        assert "tests/test_auth.py" in out
        assert "src/util.py" not in out

    @pytest.mark.asyncio
    async def test_content_search_returns_line_hits(self):
        sources = {
            "src/a.py": "def foo():\n    return BAR\n",
            "src/b.py": "import os\nBAR = 1\n",
        }
        ex = _executor(sources, list(sources))
        out = await ex.execute("grep_repo", {"pattern": r"\bBAR\b"})
        assert "src/a.py:2" in out
        assert "src/b.py:2" in out

    @pytest.mark.asyncio
    async def test_path_glob_filters_candidates(self):
        sources = {
            "src/a.py": "needle\n",
            "src/a.go": "needle\n",
        }
        ex = _executor(sources, list(sources))
        out = await ex.execute("grep_repo", {"pattern": "needle", "path_glob": "**/*.go"})
        assert "src/a.go" in out
        assert "src/a.py" not in out

    @pytest.mark.asyncio
    async def test_invalid_regex_returns_error_string(self):
        ex = _executor({"a.py": "x"}, ["a.py"])
        out = await ex.execute("grep_repo", {"pattern": "[unclosed"})
        assert out.startswith("[invalid regex")


class TestExecutorBudget:
    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self):
        ex = _executor({}, [])
        out = await ex.execute("delete_repo", {})
        assert "unknown tool" in out

    @pytest.mark.asyncio
    async def test_exhausted_budget_blocks_further_calls(self):
        ex = _executor({"a.py": "hi"}, ["a.py"])
        ex.bytes_used = 1_000_000  # simulate exhaustion
        out = await ex.execute("read_file", {"path": "a.py"})
        assert "budget exhausted" in out


class TestAgenticReasoningReplay:
    @pytest.mark.asyncio
    async def test_preserves_reasoning_content_across_tool_turns(self):
        class _FakeLLM:
            def __init__(self):
                self.calls: list[list[dict]] = []
                self._responses = [
                    {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "I need the file.",
                        "reasoning_items": [{"type": "reasoning", "id": "rs_1"}],
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"path": "a.py"}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "Now I can review.",
                        "tool_calls": [
                            {
                                "id": "call_2",
                                "type": "function",
                                "function": {
                                    "name": "submit_review",
                                    "arguments": '{"comments": []}',
                                },
                            }
                        ],
                    },
                ]

            async def complete_agentic(self, messages, tools):
                self.calls.append([dict(m) for m in messages])
                return self._responses.pop(0)

        class _Executor:
            async def execute(self, name, args):
                return "file contents"

        llm = _FakeLLM()
        result = await agentic_review_loop(
            llm, [{"role": "user", "content": "review"}], _Executor()
        )

        assert result.raw_arguments == '{"comments": []}'
        assert result.hops == 2
        assert result.conversation[0]["content"] == "review"
        second_turn = llm.calls[1]
        assistant = second_turn[1]
        assert assistant["reasoning_content"] == "I need the file."
        assert assistant["reasoning_items"] == [{"type": "reasoning", "id": "rs_1"}]
        assert second_turn[2] == {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "file contents",
        }


class TestReadFileWindows:
    @pytest.mark.asyncio
    async def test_start_and_end_line_select_a_window(self):
        src = "\n".join(f"line{i}" for i in range(1, 51))
        ex = _executor({"src/a.py": src}, ["src/a.py"])
        out = await ex.execute("read_file", {"path": "src/a.py", "start_line": 10, "end_line": 12})
        assert "   10  line10" in out
        assert "   12  line12" in out
        assert "line13" not in out
        assert "line9" not in out
        assert "showing lines 10-12 of 50" in out

    @pytest.mark.asyncio
    async def test_capped_window_tells_model_where_to_continue(self):
        src = "\n".join("y" * 100 for _ in range(400))  # ~40KB, well past one window
        ex = _executor({"src/big.py": src}, ["src/big.py"])
        out = await ex.execute("read_file", {"path": "src/big.py"})
        assert "truncated" in out
        assert "start_line=" in out
        assert "of 400" in out

    @pytest.mark.asyncio
    async def test_string_line_numbers_are_tolerated(self):
        ex = _executor({"src/a.py": "a\nb\nc\nd"}, ["src/a.py"])
        out = await ex.execute(
            "read_file", {"path": "src/a.py", "start_line": "3", "end_line": "junk"}
        )
        assert "    3  c" in out
        assert "    4  d" in out
        assert "    2  b" not in out

    @pytest.mark.asyncio
    async def test_start_past_end_is_reported(self):
        ex = _executor({"src/a.py": "a\nb"}, ["src/a.py"])
        out = await ex.execute("read_file", {"path": "src/a.py", "start_line": 9})
        assert "past the end" in out


class TestGrepRanking:
    def test_filename_token_match_ranks_first(self):
        from mira.llm.agentic_tools import rank_for_grep

        tree = [
            "aaa/first.java",
            "crypto/fips/FIPSProvider.java",
            "crypto/def/DefaultCryptoProvider.java",
            "zzz/last.java",
        ]
        ranked = rank_for_grep(
            tree, ["common/crypto/CryptoProvider.java"], "implements CryptoProvider"
        )
        assert ranked[0] == "crypto/def/DefaultCryptoProvider.java"

    def test_directory_proximity_beats_alphabetical(self):
        from mira.llm.agentic_tools import rank_for_grep

        tree = ["a/one.py", "pkg/plugins/log/other.py", "pkg/plugins/log/fake.py", "z/last.py"]
        ranked = rank_for_grep(tree, ["pkg/plugins/log/logger.py"], "FromContext")
        assert ranked[:2] == ["pkg/plugins/log/fake.py", "pkg/plugins/log/other.py"]
        assert ranked[-1] in {"a/one.py", "z/last.py"}

    def test_test_paths_rank_below_production_code(self):
        from mira.llm.agentic_tools import rank_for_grep

        tree = ["src/x/tests/test_thing.py", "src/x/thing.py"]
        ranked = rank_for_grep(tree, ["src/x/other.py"], "do_thing")
        assert ranked[0] == "src/x/thing.py"

    @pytest.mark.asyncio
    async def test_content_search_scans_related_files_not_alphabetical_head(self):
        """Regression: the old scan read the first 15 tree entries alphabetically,
        so a symbol living anywhere else came back as "no matches"."""
        tree = [f"aaa/filler{i:03}.py" for i in range(60)] + ["pkg/core/impl.py"]
        sources = dict.fromkeys(tree, "nothing here")
        sources["pkg/core/impl.py"] = "class Impl(Base):\n    pass"
        ex = AgenticToolExecutor(
            source_fetcher=_FakeFetcher(sources),
            repo_tree=tree,
            changed_paths=["pkg/core/base.py"],
        )
        out = await ex.execute("grep_repo", {"pattern": r"class \w+\(Base\)"})
        assert "pkg/core/impl.py:1:" in out

    @pytest.mark.asyncio
    async def test_no_match_reports_unscanned_remainder(self):
        tree = [f"src/f{i:03}.py" for i in range(100)]
        ex = _executor(dict.fromkeys(tree, "x"), tree)
        out = await ex.execute("grep_repo", {"pattern": "needle"})
        assert "no content matches" in out
        assert "not scanned" in out
        assert "path_glob" in out

    @pytest.mark.asyncio
    async def test_fetch_failure_does_not_sink_search(self):
        class _Flaky:
            async def fetch(self, path: str) -> str | None:
                if path == "src/bad.py":
                    raise RuntimeError("boom")
                return "needle here"

        ex = AgenticToolExecutor(source_fetcher=_Flaky(), repo_tree=["src/bad.py", "src/good.py"])
        out = await ex.execute("grep_repo", {"pattern": "needle"})
        assert "src/good.py:1:" in out


class _SubmitCapableLLM:
    """complete_agentic plays scripted hops; complete_with_tools records the forced submit."""

    def __init__(self, hops: list[dict], forced: str = '{"comments": [{"forced": true}]}'):
        self._hops = list(hops)
        self.forced_calls: list[dict] = []
        self.agentic_calls = 0
        self._forced = forced

    async def complete_agentic(self, messages, tools):
        self.agentic_calls += 1
        if not self._hops:
            raise RuntimeError("provider down")
        return self._hops.pop(0)

    async def complete_with_tools(self, messages, tools, temperature=None, force_tool=None):
        self.forced_calls.append(
            {"messages": [dict(m) for m in messages], "tools": tools, "force_tool": force_tool}
        )
        return self._forced


def _tool_hop(name: str = "read_file", call_id: str = "c1") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": '{"path": "a.py"}'},
            }
        ],
    }


class _RecordingExecutor:
    def __init__(self):
        self.calls: list[str] = []

    async def execute(self, name, args):
        self.calls.append(name)
        return f"result of {name}"


class TestForcedSubmission:
    @pytest.mark.asyncio
    async def test_hop_cap_forces_submit_over_full_transcript(self):
        """Hitting the cap must not throw the investigation away: the forced
        submit sees every tool result and the submit_review tool only."""
        llm = _SubmitCapableLLM([_tool_hop(call_id=f"c{i}") for i in range(5)])
        ex = _RecordingExecutor()
        out = await agentic_review_loop(
            llm,
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "diff"}],
            ex,
            max_hops=3,
        )
        assert out.raw_arguments == '{"comments": [{"forced": true}]}'
        assert out.hit_hop_cap is True and out.hops == 3
        assert llm.agentic_calls == 3
        assert ex.calls == ["read_file"] * 3
        forced = llm.forced_calls[0]
        assert forced["force_tool"] == "submit_review"
        roles = [m["role"] for m in forced["messages"]]
        assert roles.count("tool") == 3
        assert roles[-1] == "user" and "submit_review" in forced["messages"][-1]["content"]
        assert "at most 3 investigation turns" in forced["messages"][0]["content"]

    @pytest.mark.asyncio
    async def test_prose_answer_is_kept_and_submission_forced(self):
        prose_hop = {
            "role": "assistant",
            "content": "I found a null deref at a.py:3.",
            "reasoning_content": "thinking",
        }
        llm = _SubmitCapableLLM([_tool_hop(), prose_hop])
        out = await agentic_review_loop(
            llm, [{"role": "user", "content": "diff"}], _RecordingExecutor()
        )
        assert out.raw_arguments == '{"comments": [{"forced": true}]}'
        msgs = llm.forced_calls[0]["messages"]
        kept = [
            m
            for m in msgs
            if m["role"] == "assistant" and m.get("content") == "I found a null deref at a.py:3."
        ]
        assert kept and kept[0]["reasoning_content"] == "thinking"

    @pytest.mark.asyncio
    async def test_submit_review_tool_call_short_circuits(self):
        submit = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "s",
                    "type": "function",
                    "function": {"name": "submit_review", "arguments": '{"comments": []}'},
                }
            ],
        }
        llm = _SubmitCapableLLM([submit])
        out = await agentic_review_loop(
            llm, [{"role": "user", "content": "diff"}], _RecordingExecutor()
        )
        assert out.raw_arguments == '{"comments": []}'
        assert out.error is None
        assert llm.forced_calls == []

    @pytest.mark.asyncio
    async def test_provider_failure_after_investigation_still_submits(self):
        llm = _SubmitCapableLLM([_tool_hop()])  # second hop raises
        out = await agentic_review_loop(
            llm, [{"role": "user", "content": "diff"}], _RecordingExecutor()
        )
        assert out.raw_arguments == '{"comments": [{"forced": true}]}'
        assert out.error is None
        assert len(llm.forced_calls) == 1

    @pytest.mark.asyncio
    async def test_provider_failure_before_any_tool_result_bails(self):
        """Nothing gathered yet → no submission and an error, so the caller falls back."""
        llm = _SubmitCapableLLM([])
        out = await agentic_review_loop(
            llm, [{"role": "user", "content": "diff"}], _RecordingExecutor()
        )
        assert out.raw_arguments == ""
        assert out.error and "provider down" in out.error
        assert llm.forced_calls == []

    @pytest.mark.asyncio
    async def test_forced_submit_failure_returns_empty(self):
        class _Broken(_SubmitCapableLLM):
            async def complete_with_tools(self, messages, tools, temperature=None, force_tool=None):
                raise RuntimeError("rejected")

        llm = _Broken([_tool_hop()], forced="")
        out = await agentic_review_loop(
            llm, [{"role": "user", "content": "diff"}], _RecordingExecutor(), max_hops=1
        )
        assert out.raw_arguments == ""
        assert out.error and "rejected" in out.error
