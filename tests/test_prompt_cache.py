"""Anthropic prompt-cache breakpoints for OpenAI-compatible endpoints."""

from __future__ import annotations

import copy
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.config import LLMConfig, MiraConfig
from mira.llm.prompt_cache import (
    CACHE_BOUNDARY,
    apply_cache_control,
    caching_enabled,
    split_system_prompt,
)
from mira.llm.prompts.review import build_review_prompt, build_security_review_prompt
from mira.llm.provider import LLMProvider
from mira.models import FileChangeType, FileDiff, HunkInfo

os.environ.setdefault("OPENROUTER_API_KEY", "test-key-for-unit-tests")

_OPENROUTER = {"name": "openrouter", "prompt_caching": "anthropic"}
_SPLIT = [
    {"role": "system", "content": "stable instructions"},
    {"role": "system", "content": "per-PR context"},
    {"role": "user", "content": "diff"},
]


def _diff(path: str = "src/app.py") -> FileDiff:
    return FileDiff(
        path=path,
        change_type=FileChangeType.MODIFIED,
        hunks=[HunkInfo(1, 1, 1, 1, "@@ -1 +1 @@\n-a\n+b")],
        added_lines=1,
        deleted_lines=1,
        language="python",
    )


class TestSplitSystemPrompt:
    def test_no_boundary_is_one_message(self):
        assert split_system_prompt("all of it") == [{"role": "system", "content": "all of it"}]

    def test_boundary_splits_and_is_removed(self):
        out = split_system_prompt(f"head\n\n{CACHE_BOUNDARY}\n\ntail")
        assert out == [
            {"role": "system", "content": "head"},
            {"role": "system", "content": "tail"},
        ]

    def test_blank_tail_is_dropped(self):
        assert split_system_prompt(f"head\n{CACHE_BOUNDARY}\n  \n") == [
            {"role": "system", "content": "head"}
        ]


class TestCachingEnabled:
    def test_anthropic_model_on_caching_profile(self):
        assert caching_enabled(LLMConfig(), _OPENROUTER, "anthropic/claude-opus-5.5")

    def test_non_anthropic_model(self):
        assert not caching_enabled(LLMConfig(), _OPENROUTER, "openai/gpt-6.1-sol")

    def test_profile_without_caching(self):
        assert not caching_enabled(LLMConfig(), {"name": "openai"}, "anthropic/claude-opus-5.5")

    def test_config_switch_off(self):
        config = LLMConfig(prompt_caching=False)
        assert not caching_enabled(config, _OPENROUTER, "anthropic/claude-opus-5.5")


class TestApplyCacheControl:
    def test_marks_first_system_message_when_split(self):
        original = copy.deepcopy(_SPLIT)
        body = {"model": "anthropic/x", "messages": _SPLIT}
        apply_cache_control(body)

        first = body["messages"][0]["content"]
        assert first == [
            {"type": "text", "text": "stable instructions", "cache_control": {"type": "ephemeral"}}
        ]
        assert body["messages"][1:] == _SPLIT[1:]
        assert "cache_control" not in body
        assert original == _SPLIT  # caller's messages untouched

    def test_single_system_message_is_not_marked(self):
        messages = [{"role": "system", "content": "unique"}, {"role": "user", "content": "x"}]
        body = {"model": "anthropic/x", "messages": messages}
        apply_cache_control(body)
        assert body["messages"] == messages

    def test_conversation_adds_top_level_breakpoint(self):
        body = {"model": "anthropic/x", "messages": list(_SPLIT)}
        apply_cache_control(body, conversation=True)
        assert body["cache_control"] == {"type": "ephemeral"}


class TestReviewPromptsSplit:
    def test_review_prompt_puts_per_pr_content_after_boundary(self):
        messages = build_review_prompt(
            files=[_diff()], config=MiraConfig(), pr_title="Fix the thing"
        )
        assert [m["role"] for m in messages] == ["system", "system", "user"]
        stable, per_pr = messages[0]["content"], messages[1]["content"]
        assert stable.startswith("You are Mira")
        assert "Fix the thing" not in stable and "src/app.py" not in stable
        assert "Fix the thing" in per_pr and "src/app.py" in per_pr
        assert all(CACHE_BOUNDARY not in m["content"] for m in messages)

    def test_stable_prefix_is_identical_across_prs(self):
        a = build_review_prompt(files=[_diff("a.py")], config=MiraConfig(), pr_title="A")
        b = build_review_prompt(files=[_diff("b.go")], config=MiraConfig(), pr_title="B")
        assert a[0]["content"] == b[0]["content"]

    def test_security_prompt_is_split(self):
        messages = build_security_review_prompt(files=[_diff()], pr_title="T")
        assert [m["role"] for m in messages] == ["system", "system", "user"]
        assert "src/app.py" not in messages[0]["content"]


def _client(payload: dict):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    resp.text = json.dumps(payload)
    client = AsyncMock()
    client.post = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


_TOOL = {"type": "function", "function": {"name": "submit_review", "parameters": {}}}
_USAGE = {
    "prompt_tokens": 3000,
    "completion_tokens": 10,
    "prompt_tokens_details": {"cached_tokens": 2500, "cache_write_tokens": 0},
}


class TestProviderSendsBreakpoints:
    @pytest.mark.asyncio
    async def test_tool_call_on_anthropic_model(self):
        provider = LLMProvider(LLMConfig(model="anthropic/claude-opus-5.5"))
        payload = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [{"function": {"name": "submit_review", "arguments": "{}"}}]
                    }
                }
            ],
            "usage": _USAGE,
        }
        with patch("mira.llm.provider.httpx.AsyncClient") as cls:
            cls.return_value = _client(payload)
            await provider.complete_with_tools(list(_SPLIT), tools=[_TOOL])
            body = cls.return_value.post.call_args.kwargs["json"]

        assert body["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in body
        assert provider.total_cache_read_tokens == 2500
        assert provider.usage["prompt_tokens"] == 3000

    @pytest.mark.asyncio
    async def test_agentic_hop_adds_top_level_breakpoint(self):
        provider = LLMProvider(LLMConfig(model="anthropic/claude-opus-5.5"))
        payload = {"choices": [{"message": {"content": "done"}}]}
        with patch("mira.llm.provider.httpx.AsyncClient") as cls:
            cls.return_value = _client(payload)
            await provider.complete_agentic(list(_SPLIT), tools=[_TOOL])
            body = cls.return_value.post.call_args.kwargs["json"]

        assert body["cache_control"] == {"type": "ephemeral"}

    @pytest.mark.asyncio
    async def test_non_anthropic_model_is_untouched(self):
        provider = LLMProvider(LLMConfig(model="openai/gpt-6.1-sol"))
        payload = {"choices": [{"message": {"content": "done"}}]}
        with patch("mira.llm.provider.httpx.AsyncClient") as cls:
            cls.return_value = _client(payload)
            await provider.complete_agentic(list(_SPLIT), tools=[_TOOL])
            body = cls.return_value.post.call_args.kwargs["json"]

        assert body["messages"] == _SPLIT
        assert "cache_control" not in body
