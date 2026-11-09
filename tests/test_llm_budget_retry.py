"""A response cut off at ``max_tokens`` gets one retry with a larger budget.

Thinking models count their reasoning against the output budget. With the
default 4096 tokens a review-sized prompt can burn the whole budget thinking
and return no tool call, or a tool call truncated mid-JSON. The release smoke
eval caught exactly this on DeepSeek v4.1 flash: ``finish_reason=length``,
``reasoning_tokens=4096``, no tool call, on three of five real PRs.
"""

from __future__ import annotations

import json

import httpx
import pytest

from mira.config import LLMConfig
from mira.exceptions import LLMError
from mira.llm.base import BUDGET_RETRY_TOKENS
from mira.llm.provider import LLMProvider
from mira.llm.tool_schemas import SUBMIT_REVIEW_TOOL

_SUBMISSION = {"comments": [], "summary": "fine", "metadata": {"reviewed_files": 1}}


def _tool_reply(arguments: str, finish_reason: str = "tool_calls") -> dict:
    return {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "submit_review", "arguments": arguments},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 6000, "completion_tokens": 400},
    }


def _exhausted_reply() -> dict:
    """What a thinking model returns when reasoning ate the whole budget."""
    return {
        "choices": [
            {
                "finish_reason": "length",
                "message": {"role": "assistant", "content": None},
            }
        ],
        "usage": {
            "prompt_tokens": 6000,
            "completion_tokens": 4096,
            "completion_tokens_details": {"reasoning_tokens": 4096},
        },
    }


def _text_reply(text: str, finish_reason: str = "stop") -> dict:
    return {
        "choices": [{"finish_reason": finish_reason, "message": {"content": text}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


@pytest.fixture
def provider(monkeypatch):
    """An LLMProvider whose HTTP layer serves scripted replies and records requests."""

    def make(replies: list[dict], *, max_tokens: int = 4096):
        requests: list[dict] = []
        config = LLMConfig(
            model="thinker",
            base_url="https://llm.example.invalid/v1",
            api_key_env="",
            max_tokens=max_tokens,
            max_retries=1,
            retry_min_wait=0,
            retry_max_wait=0,
        )

        def serve(request):
            body = json.loads(request.content)
            requests.append(body)
            return httpx.Response(200, json=replies[min(len(requests) - 1, len(replies) - 1)])

        client_type = httpx.AsyncClient
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kwargs: client_type(transport=httpx.MockTransport(serve), **kwargs),
        )
        return LLMProvider(config), requests

    return make


@pytest.mark.asyncio
async def test_tool_call_retries_once_with_larger_budget_when_reasoning_exhausts_it(provider):
    llm, requests = provider([_exhausted_reply(), _tool_reply(json.dumps(_SUBMISSION))])

    raw = await llm.complete_with_tools(
        [{"role": "user", "content": "review"}], [SUBMIT_REVIEW_TOOL]
    )

    assert json.loads(raw) == _SUBMISSION
    assert [r["max_tokens"] for r in requests] == [4096, BUDGET_RETRY_TOKENS]
    # Everything else about the request is unchanged on the retry.
    assert requests[1]["tool_choice"] == requests[0]["tool_choice"]
    assert requests[1]["messages"] == requests[0]["messages"]
    # Both calls are paid for, so both are counted.
    assert llm.total_completion_tokens == 4096 + 400


@pytest.mark.asyncio
async def test_truncated_tool_arguments_are_retried_before_being_returned(provider):
    """A tool call cut off mid-JSON is the other shape of the same failure."""
    truncated = _tool_reply('{"comments": [{"path": "a.py", "li', finish_reason="length")
    llm, requests = provider([truncated, _tool_reply(json.dumps(_SUBMISSION))])

    raw = await llm.complete_with_tools(
        [{"role": "user", "content": "review"}], [SUBMIT_REVIEW_TOOL]
    )

    assert json.loads(raw) == _SUBMISSION
    assert len(requests) == 2
    assert requests[1]["max_tokens"] == BUDGET_RETRY_TOKENS


@pytest.mark.asyncio
async def test_budget_is_raised_only_once(provider):
    """A model that exhausts even the larger budget fails as before, not in a loop."""
    llm, requests = provider([_exhausted_reply()])

    with pytest.raises(LLMError):
        await llm.complete_with_tools([{"role": "user", "content": "review"}], [SUBMIT_REVIEW_TOOL])

    assert [r["max_tokens"] for r in requests] == [4096, BUDGET_RETRY_TOKENS]


@pytest.mark.asyncio
async def test_no_retry_when_the_budget_is_already_large(provider):
    llm, requests = provider([_exhausted_reply()], max_tokens=BUDGET_RETRY_TOKENS)

    with pytest.raises(LLMError):
        await llm.complete_with_tools([{"role": "user", "content": "review"}], [SUBMIT_REVIEW_TOOL])

    assert len(requests) == 1


@pytest.mark.asyncio
async def test_normal_completion_is_not_retried(provider):
    llm, requests = provider([_tool_reply(json.dumps(_SUBMISSION))])

    await llm.complete_with_tools([{"role": "user", "content": "review"}], [SUBMIT_REVIEW_TOOL])

    assert len(requests) == 1


@pytest.mark.asyncio
async def test_plain_completion_cut_off_is_retried(provider):
    """The JSON-mode path (critic, summaries, thread resolution) gets the same retry."""
    llm, requests = provider([_text_reply('{"verdicts": [', "length"), _text_reply('{"ok": true}')])

    text = await llm.complete([{"role": "user", "content": "grade"}], json_mode=True)

    assert json.loads(text) == {"ok": True}
    assert [r["max_tokens"] for r in requests] == [4096, BUDGET_RETRY_TOKENS]


@pytest.mark.asyncio
async def test_agentic_turn_cut_off_is_retried(provider):
    """The tool loop's own turns are reviewed the same way."""
    tools = [SUBMIT_REVIEW_TOOL]
    llm, requests = provider([_exhausted_reply(), _tool_reply(json.dumps(_SUBMISSION))])

    message = await llm.complete_agentic([{"role": "user", "content": "review"}], tools=tools)

    assert message["tool_calls"][0]["function"]["name"] == "submit_review"
    assert [r["max_tokens"] for r in requests] == [4096, BUDGET_RETRY_TOKENS]
