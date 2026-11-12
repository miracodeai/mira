"""When thinking itself ate the output budget, the retry turns thinking off.

The release smoke eval on DeepSeek v4.1 flash at medium effort still lost two
real PRs to ``finish_reason=length`` with every one of the 16,384 completion
tokens spent on reasoning. A bigger budget cannot fix that; a retry without
thinking can, and the same PRs completed in seconds with thinking off.
"""

from __future__ import annotations

import json

import httpx
import pytest

from mira.config import LLMConfig
from mira.exceptions import LLMError
from mira.llm.provider import LLMProvider
from mira.llm.tool_schemas import SUBMIT_REVIEW_TOOL

_SUBMISSION = json.dumps({"comments": [], "summary": "", "metadata": {"reviewed_files": 1}})


def _tool_reply() -> dict:
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "submit_review", "arguments": _SUBMISSION},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 14000, "completion_tokens": 900},
    }


def _thought_it_all_away(budget: int) -> dict:
    return {
        "choices": [{"finish_reason": "length", "message": {"role": "assistant", "content": None}}],
        "usage": {
            "prompt_tokens": 14000,
            "completion_tokens": budget,
            "completion_tokens_details": {"reasoning_tokens": budget},
        },
    }


def _cut_off_while_writing(budget: int) -> dict:
    """Length with little reasoning: the answer itself was too long."""
    reply = _thought_it_all_away(budget)
    reply["usage"]["completion_tokens_details"]["reasoning_tokens"] = 200
    return reply


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def make(replies: list[dict], **config):
        requests: list[dict] = []
        params = {
            "model": "deepseek/deepseek-v4.1-flash-1017",
            "base_url": "https://openrouter.ai/api/v1",
            "reasoning_effort": "medium",
            "max_retries": 1,
            "retry_min_wait": 0,
            "retry_max_wait": 0,
        }
        params.update(config)

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
        return LLMProvider(LLMConfig(**params)), requests

    return make


async def _review(llm: LLMProvider) -> str:
    return await llm.complete_with_tools(
        [{"role": "user", "content": "review"}], [SUBMIT_REVIEW_TOOL]
    )


@pytest.mark.asyncio
async def test_reasoning_that_ate_the_full_budget_is_switched_off_on_retry(provider):
    llm, requests = provider([_thought_it_all_away(16384), _tool_reply()])

    raw = await _review(llm)

    assert json.loads(raw) == json.loads(_SUBMISSION)
    assert requests[0]["reasoning_effort"] == "medium"
    assert requests[0]["max_tokens"] == 16384
    # Same budget (already at the ceiling), thinking explicitly off.
    assert requests[1]["max_tokens"] == 16384
    assert "reasoning_effort" not in requests[1]
    assert requests[1]["reasoning"] == {"enabled": False}


@pytest.mark.asyncio
async def test_small_budget_eaten_by_reasoning_is_raised_and_thinking_is_switched_off(provider):
    llm, requests = provider([_thought_it_all_away(4096), _tool_reply()], max_tokens=4096)

    await _review(llm)

    assert requests[1]["max_tokens"] == 16384
    assert requests[1]["reasoning"] == {"enabled": False}


@pytest.mark.asyncio
async def test_answer_that_was_merely_long_keeps_thinking_on(provider):
    """Little reasoning, long output: the budget is the problem, not thinking."""
    llm, requests = provider([_cut_off_while_writing(4096), _tool_reply()], max_tokens=4096)

    await _review(llm)

    assert requests[1]["max_tokens"] == 16384
    assert requests[1]["reasoning_effort"] == "medium"
    assert "reasoning" not in requests[1]


@pytest.mark.asyncio
async def test_thinking_is_switched_off_only_once(provider):
    llm, requests = provider([_thought_it_all_away(16384)])

    with pytest.raises(LLMError):
        await _review(llm)

    assert len(requests) == 2


@pytest.mark.asyncio
async def test_no_retry_without_a_way_to_turn_thinking_off(monkeypatch):
    """A plain endpoint has no documented off switch, so nothing can change."""
    requests: list[dict] = []

    def serve(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_thought_it_all_away(16384))

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(serve), **kwargs),
    )
    llm = LLMProvider(
        LLMConfig(
            model="m",
            base_url="https://llm.example.invalid/v1",
            api_key_env="",
            reasoning_effort="medium",
            max_retries=1,
        )
    )

    with pytest.raises(LLMError):
        await _review(llm)

    assert len(requests) == 1
