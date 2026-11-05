"""Tests for the agentic tool executor used on unindexed-repo reviews."""

from __future__ import annotations

import pytest

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


# --------------------------------------- router-acp seat retrieval (HAI-12817)

_PRODUCER_DIFF = "-    return {'amount': cents}\n+    return {'amount_cents': cents}\n"
_DEFECT_CONSUMER = "def charge(payload):\n    return bill(payload['amount'])\n"
_CLEAN_CONSUMER = "def charge(payload):\n    return bill(payload['amount_cents'])\n"


def _seat_that_follows_results():
    """A stand-in seat: greps for the consumer, reads it, files only what it saw."""
    import json as _json

    prompts: list[str] = []

    async def transport(request):
        prompt = request["body"]["prompt"]
        prompts.append(prompt)
        if len(prompts) == 1:
            reply = {"tool": "grep_repo", "arguments": {"pattern": "payload\\[", "path_only": None}}
        elif len(prompts) == 2:
            reply = {"tool": "read_file", "arguments": {"path": "billing/consumer.py"}}
        else:
            stale = "bill(payload['amount'])" in prompt
            comments = (
                [
                    {
                        "path": "billing/consumer.py",
                        "line": 2,
                        "severity": "blocker",
                        "category": "bug",
                        "title": "Consumer still reads removed `amount` key",
                        "body": "`charge` reads `payload['amount']`, which the producer renamed.",
                        "confidence": 0.9,
                    }
                ]
                if stale
                else []
            )
            reply = {"tool": "submit_review", "arguments": {"comments": comments, "summary": "s"}}
        return {"text": _json.dumps(reply), "actual_model": "claude@x/opus"}

    return transport, prompts


@pytest.mark.parametrize(("consumer", "expect"), [(_DEFECT_CONSUMER, 1), (_CLEAN_CONSUMER, 0)])
@pytest.mark.asyncio
async def test_router_seat_retrieves_the_consumer_before_submitting(monkeypatch, consumer, expect):
    import json as _json

    from mira.config import LLMConfig
    from mira.core.passes import agentic_review_loop
    from mira.llm.router_acp import RouterAcpProvider

    monkeypatch.setenv("MIRA_ROUTER_ACP_ALLOW_UNTRACED", "1")
    monkeypatch.delenv("LLM_OTLP_ENDPOINT", raising=False)
    transport, prompts = _seat_that_follows_results()
    provider = RouterAcpProvider(
        LLMConfig(provider="router-acp", model="claude@x/opus"), transport=transport, relay=False
    )
    sources = {"billing/producer.py": "def build(cents):\n    return {'amount_cents': cents}\n"}
    sources["billing/consumer.py"] = consumer
    executor = _executor(sources, sorted(sources))
    messages = [
        {"role": "system", "content": "Review the diff."},
        {"role": "user", "content": _PRODUCER_DIFF},
    ]
    raw = await agentic_review_loop(provider, messages, executor)
    assert len(_json.loads(raw)["comments"]) == expect
    assert len(prompts) == 3
    assert "billing/consumer.py:2" in prompts[1]  # grep hit reached the seat
    assert "    2  " in prompts[2] and consumer.splitlines()[1].strip() in prompts[2]
    assert [c["tool"] for c in executor.call_log] == ["grep_repo", "read_file"]
