"""Second-look turn after submission, and the security pass's defect handoff."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from mira.core.passes import (
    _SECOND_LOOK_PROMPT,
    _security_scan_once,
    agentic_review_loop,
    merge_submissions,
)
from mira.models import FileChangeType, FileDiff, HunkInfo

FIRST = json.dumps(
    {
        "comments": [{"path": "a.py", "line": 1, "title": "first", "existing_code": "x = 1"}],
        "key_issues": [{"issue": "first", "path": "a.py", "line": 1}],
        "summary": "one finding",
    }
)
EXTRA = json.dumps(
    {
        "comments": [{"path": "a.py", "line": 2, "title": "withheld", "existing_code": "y = 2"}],
        "key_issues": [],
        "summary": "found one more",
    }
)


class TestMergeSubmissions:
    def test_appends_extra_comments_and_keeps_first_summary(self):
        merged = json.loads(merge_submissions(FIRST, EXTRA))
        assert [c["title"] for c in merged["comments"]] == ["first", "withheld"]
        assert merged["summary"] == "one finding"
        assert merged["key_issues"] == [{"issue": "first", "path": "a.py", "line": 1}]

    def test_empty_or_garbage_extra_leaves_first_alone(self):
        assert merge_submissions(FIRST, "") is FIRST
        assert merge_submissions(FIRST, "not json at all {{{") is FIRST
        assert merge_submissions(FIRST, json.dumps({"comments": [], "summary": ""})) is FIRST

    def test_extra_over_empty_first_builds_a_submission(self):
        merged = json.loads(merge_submissions("", EXTRA))
        assert [c["title"] for c in merged["comments"]] == ["withheld"]
        assert merged["summary"] == "found one more"


def _submit_hop(call_id: str = "s1") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "reasoning_content": "I noticed a second issue but it seemed minor.",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "submit_review", "arguments": FIRST},
            }
        ],
    }


def _tool_hop() -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
            }
        ],
    }


class _LLM:
    def __init__(self, hops: list[dict], second: str = EXTRA, forced: str = FIRST):
        self._hops = list(hops)
        self.tool_calls: list[list[dict]] = []
        self._second = second
        self._forced = forced

    async def complete_agentic(self, messages, tools):
        if not self._hops:
            raise RuntimeError("provider down")
        return self._hops.pop(0)

    async def complete_with_tools(self, messages, tools, temperature=None, force_tool=None):
        self.tool_calls.append([dict(m) for m in messages])
        last = messages[-1]
        if last.get("role") == "user" and last["content"] == _SECOND_LOOK_PROMPT:
            return self._second
        return self._forced


class _Executor:
    def __init__(self):
        self.call_log: list[dict] = []

    async def execute(self, name, args):
        return "contents"


class TestSecondLookInLoop:
    @pytest.mark.asyncio
    async def test_voluntary_submission_gets_a_second_look_over_the_transcript(self):
        llm = _LLM([_tool_hop(), _submit_hop()])
        ex = _Executor()
        out = await agentic_review_loop(
            llm, [{"role": "user", "content": "diff"}], ex, second_look=True
        )
        merged = json.loads(out.raw_arguments)
        assert [c["title"] for c in merged["comments"]] == ["first", "withheld"]
        # One extra call, over the full transcript: the tool result, the
        # submitting assistant turn (with its reasoning), a receipt, the prompt.
        assert len(llm.tool_calls) == 1
        msgs = llm.tool_calls[0]
        roles = [m["role"] for m in msgs]
        assert roles[-1] == "user" and msgs[-1]["content"] == _SECOND_LOOK_PROMPT
        assert roles[-2] == "tool" and msgs[-2]["tool_call_id"] == "s1"
        assert "Recorded 1 comment" in msgs[-2]["content"]
        assert msgs[-3]["reasoning_content"] == "I noticed a second issue but it seemed minor."
        assert ex.call_log[-1] == {"tool": "second_look", "arg": "+1"}

    @pytest.mark.asyncio
    async def test_disabled_second_look_returns_submission_unchanged(self):
        llm = _LLM([_submit_hop()])
        out = await agentic_review_loop(llm, [{"role": "user", "content": "diff"}], _Executor())
        assert out.raw_arguments == FIRST
        assert llm.tool_calls == []

    @pytest.mark.asyncio
    async def test_forced_submission_also_gets_a_second_look(self):
        llm = _LLM([_tool_hop(), _tool_hop()])
        ex = _Executor()
        out = await agentic_review_loop(
            llm, [{"role": "user", "content": "diff"}], ex, max_hops=2, second_look=True
        )
        merged = json.loads(out.raw_arguments)
        assert [c["title"] for c in merged["comments"]] == ["first", "withheld"]
        assert len(llm.tool_calls) == 2  # forced submit, then the second look
        second = llm.tool_calls[1]
        assert second[-2]["role"] == "assistant"
        assert second[-2]["content"].startswith("Submitted review:")

    @pytest.mark.asyncio
    async def test_second_look_failure_keeps_first_submission(self):
        class _Broken(_LLM):
            async def complete_with_tools(self, messages, tools, temperature=None, force_tool=None):
                raise RuntimeError("rate limited")

        llm = _Broken([_submit_hop()])
        ex = _Executor()
        out = await agentic_review_loop(
            llm, [{"role": "user", "content": "diff"}], ex, second_look=True
        )
        assert out.raw_arguments == FIRST
        assert ex.call_log[-1] == {"tool": "second_look", "arg": "+0"}

    @pytest.mark.asyncio
    async def test_empty_second_look_adds_nothing(self):
        llm = _LLM([_submit_hop()], second=json.dumps({"comments": [], "summary": "none"}))
        out = await agentic_review_loop(
            llm, [{"role": "user", "content": "diff"}], _Executor(), second_look=True
        )
        assert out.raw_arguments == FIRST


def _diff_file(path: str = "a.py") -> FileDiff:
    return FileDiff(
        path=path,
        change_type=FileChangeType.MODIFIED,
        hunks=[HunkInfo(1, 3, 1, 3, "@@ -1,3 +1,3 @@\n+x = 1\n+y = 2\n+z = 3")],
        language="python",
        added_lines=3,
        deleted_lines=0,
    )


class TestSecurityHandoff:
    @pytest.mark.asyncio
    async def test_non_security_defect_keeps_its_category(self):
        raw = json.dumps(
            {
                "comments": [
                    {
                        "path": "a.py",
                        "line": 1,
                        "severity": "warning",
                        "category": "bug",
                        "title": "nil request dereferenced",
                        "body": "req is used without a nil check",
                        "confidence": 0.8,
                        "existing_code": "x = 1",
                    },
                    {
                        "path": "a.py",
                        "line": 2,
                        "severity": "warning",
                        "category": "other",
                        "title": "origin check by substring",
                        "body": "bypassable",
                        "confidence": 0.8,
                        "existing_code": "y = 2",
                    },
                ],
                "summary": "",
            }
        )
        llm = MagicMock()
        llm.complete_with_tools = AsyncMock(return_value=raw)
        outcome = await _security_scan_once(llm, llm, [_diff_file()], "title")
        assert outcome.failure is None
        by_title = {c.title: c for c in outcome.comments}
        assert by_title["nil request dereferenced"].category == "bug"
        assert by_title["origin check by substring"].category == "security"
        assert all(c.source_pass == "security" for c in outcome.comments)


class TestSiblingToolCalls:
    @pytest.mark.asyncio
    async def test_sibling_calls_after_submit_are_answered_before_second_look(self):
        """A message carrying [submit_review, grep_repo] must leave no dangling
        tool call: the grep gets its tool result, then the receipt, then the
        second look runs over a well-formed transcript."""
        hop = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "s1",
                    "type": "function",
                    "function": {"name": "submit_review", "arguments": FIRST},
                },
                {
                    "id": "g1",
                    "type": "function",
                    "function": {"name": "grep_repo", "arguments": '{"pattern": "x"}'},
                },
            ],
        }
        llm = _LLM([hop])
        ex = _Executor()
        out = await agentic_review_loop(
            llm, [{"role": "user", "content": "diff"}], ex, second_look=True
        )
        assert [c["title"] for c in json.loads(out.raw_arguments)["comments"]] == [
            "first",
            "withheld",
        ]
        msgs = llm.tool_calls[0]
        tool_ids = [m["tool_call_id"] for m in msgs if m["role"] == "tool"]
        assert tool_ids == ["g1", "s1"]
