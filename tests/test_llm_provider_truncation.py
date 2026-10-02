"""Reasoning models exhausting max_tokens produce empty responses.

A thinking model (GLM-5.x, o-series, Claude with extended thinking, ...) can
spend its entire output budget on reasoning and return a message with no
content and no tool calls (``finish_reason="length"``). This previously
surfaced as a generic ``LLMError: no_tool_call`` that failed the whole review.

The provider now retries once with a doubled output budget (bounded at 4x the
configured ``max_tokens``), remembers the escalated budget for subsequent
calls to the same model, and only then fails.
"""

from __future__ import annotations

import copy
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.config import LLMConfig
from mira.exceptions import LLMError
from mira.llm.provider import LLMProvider

os.environ.setdefault("OPENROUTER_API_KEY", "test-key-for-unit-tests")


def _resp(content: str | None, finish_reason: str, tool_args: str | None = None) -> MagicMock:
    """Mock httpx.Response with a chat-completions shaped payload."""
    message: dict = {"content": content}
    if tool_args is not None:
        message["tool_calls"] = [{"function": {"name": "submit_review", "arguments": tool_args}}]
    data = {
        "choices": [{"message": message, "finish_reason": finish_reason}],
    }
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = data
    resp.text = json.dumps(data)
    return resp


def _mock_client(responses: list[MagicMock]) -> MagicMock:
    mock_client = AsyncMock()
    # Snapshot the json body at call time — the provider mutates its body
    # dict in place (e.g. on max_tokens escalation), so keeping a reference
    # would make every recorded call look like the final state.
    recorded_bodies: list[dict] = []

    async def _post(*args: object, json: dict | None = None, **kwargs: object) -> MagicMock:
        recorded_bodies.append(copy.deepcopy(json))
        return responses.pop(0)

    mock_client.post = AsyncMock(side_effect=_post)
    mock_client.post_bodies = recorded_bodies
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    cls = MagicMock()
    cls.return_value = mock_client
    return cls


def _tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "submit_review",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


class TestReasoningTruncationEscalation:
    @pytest.mark.asyncio
    async def test_empty_length_response_escalates_once_and_succeeds(self):
        config = LLMConfig(model="test-model", max_tokens=4096, max_retries=1)
        provider = LLMProvider(config)

        truncated = _resp(None, "length")
        good = _resp(None, "tool_calls", tool_args='{"comments": []}')
        cls = _mock_client([truncated, good])

        with patch("mira.llm.provider.httpx.AsyncClient", cls):
            result = await provider.complete_with_tools(
                [{"role": "user", "content": "review this"}], _tools()
            )

        assert result == '{"comments": []}'
        # The escalated budget is remembered for subsequent calls to this model.
        assert provider._max_tokens_override["test-model"] == 8192
        # Second request carried the doubled budget.
        assert mock_post_bodies(cls)[1]["max_tokens"] == 8192

    @pytest.mark.asyncio
    async def test_escalation_is_capped_at_four_times_config(self):
        config = LLMConfig(model="test-model", max_tokens=4096, max_retries=1)
        provider = LLMProvider(config)

        truncated = _resp(None, "length")
        cls = _mock_client([truncated, truncated])

        with patch("mira.llm.provider.httpx.AsyncClient", cls), pytest.raises(LLMError):
            await provider.complete_with_tools(
                [{"role": "user", "content": "review this"}], _tools()
            )

        # Two attempts only: 4096 -> 8192, capped escalation never exceeds 4x.
        bodies = mock_post_bodies(cls)
        assert bodies[0]["max_tokens"] == 4096
        assert bodies[1]["max_tokens"] == 8192
        assert provider._max_tokens_override["test-model"] == 8192

    @pytest.mark.asyncio
    async def test_empty_stop_response_fails_without_escalation(self):
        # finish_reason=stop with nothing in it is not a budget problem —
        # no escalation, immediate failure.
        config = LLMConfig(model="test-model", max_tokens=4096, max_retries=1)
        provider = LLMProvider(config)

        cls = _mock_client([_resp(None, "stop")])

        with patch("mira.llm.provider.httpx.AsyncClient", cls), pytest.raises(LLMError):
            await provider.complete_with_tools(
                [{"role": "user", "content": "review this"}], _tools()
            )

        assert mock_post_bodies(cls)[0]["max_tokens"] == 4096
        assert provider._max_tokens_override == {}

    @pytest.mark.asyncio
    async def test_escalated_budget_persists_for_next_call(self):
        config = LLMConfig(model="test-model", max_tokens=4096, max_retries=1)
        provider = LLMProvider(config)

        truncated = _resp(None, "length")
        good = _resp(None, "tool_calls", tool_args="{}")
        cls = _mock_client([truncated, good, good])

        with patch("mira.llm.provider.httpx.AsyncClient", cls):
            await provider.complete_with_tools([{"role": "user", "content": "a"}], _tools())
            # Subsequent call to the same model starts at the escalated budget.
            await provider.complete_with_tools([{"role": "user", "content": "b"}], _tools())

        bodies = mock_post_bodies(cls)
        assert bodies[0]["max_tokens"] == 4096
        assert bodies[1]["max_tokens"] == 8192
        assert bodies[2]["max_tokens"] == 8192


def mock_post_bodies(cls: MagicMock) -> list[dict]:
    """Extract the json bodies from all calls made to the mocked client."""
    return cls.return_value.post_bodies
