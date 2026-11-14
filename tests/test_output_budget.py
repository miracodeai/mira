"""The default output budget is 16k, and an endpoint that rejects it is remembered.

Thinking tokens count against ``max_tokens`` on most providers, so the default
is sized for a medium-effort review of a large chunk (what the published
benchmark ran with). Older models cap output lower and answer a too-large
budget with a 400; that model is retried at 4096 and kept there.
"""

from __future__ import annotations

import json

import httpx
import pytest

from mira.config import LLMConfig
from mira.exceptions import LLMError
from mira.llm.base import BUDGET_FLOOR_TOKENS
from mira.llm.provider import LLMProvider
from mira.llm.tool_schemas import SUBMIT_REVIEW_TOOL

_SUBMISSION = json.dumps({"comments": [], "summary": "", "metadata": {"reviewed_files": 1}})
_TOO_LARGE = {
    "error": {
        "message": "max_tokens is too large: 16384. This model supports at most 8192 completion tokens."
    }
}


def _tool_reply(finish_reason: str = "tool_calls") -> dict:
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
                            "function": {"name": "submit_review", "arguments": _SUBMISSION},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


@pytest.fixture
def provider(monkeypatch):
    def make(serve_fn, **config):
        requests: list[dict] = []
        params = {
            "model": "m",
            "base_url": "https://llm.example.invalid/v1",
            "api_key_env": "",
            "max_retries": 1,
            "retry_min_wait": 0,
            "retry_max_wait": 0,
        }
        params.update(config)

        def serve(request):
            body = json.loads(request.content)
            requests.append(body)
            return serve_fn(body, len(requests))

        client_type = httpx.AsyncClient
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kwargs: client_type(transport=httpx.MockTransport(serve), **kwargs),
        )
        return LLMProvider(LLMConfig(**params)), requests

    return make


def test_default_budget_is_sixteen_k():
    assert LLMConfig().max_tokens == 16384


@pytest.mark.asyncio
async def test_default_budget_is_sent(provider):
    llm, requests = provider(lambda body, n: httpx.Response(200, json=_tool_reply()))

    await llm.complete_with_tools([{"role": "user", "content": "review"}], [SUBMIT_REVIEW_TOOL])

    assert requests[0]["max_tokens"] == 16384


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["tools", "agentic", "plain"])
async def test_rejected_budget_is_lowered_and_remembered(provider, path):
    def serve(body, n):
        if body["max_tokens"] > 8192:
            return httpx.Response(400, json=_TOO_LARGE)
        if path == "plain":
            return httpx.Response(
                200,
                json={
                    "choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}],
                    "usage": {},
                },
            )
        return httpx.Response(200, json=_tool_reply())

    llm, requests = provider(serve)
    messages = [{"role": "user", "content": "review"}]

    async def call():
        if path == "tools":
            return await llm.complete_with_tools(messages, [SUBMIT_REVIEW_TOOL])
        if path == "agentic":
            return await llm.complete_agentic(messages, tools=[SUBMIT_REVIEW_TOOL])
        return await llm.complete(messages, json_mode=True)

    await call()
    await call()

    # First call: rejected once, retried at the named limit. Second call: that limit from the start.
    assert [r["max_tokens"] for r in requests] == [16384, 8192, 8192]


@pytest.mark.asyncio
async def test_refused_named_limit_falls_back_to_the_floor(provider):
    def serve(body, n):
        if body["max_tokens"] > BUDGET_FLOOR_TOKENS:
            return httpx.Response(400, json=_TOO_LARGE)
        return httpx.Response(200, json=_tool_reply())

    llm, requests = provider(serve)

    await llm.complete_with_tools([{"role": "user", "content": "review"}], [SUBMIT_REVIEW_TOOL])

    assert [r["max_tokens"] for r in requests] == [16384, 8192, BUDGET_FLOOR_TOKENS]


@pytest.mark.asyncio
async def test_context_window_error_leaves_room_for_the_prompt(provider):
    message = (
        "This endpoint's maximum context length is 131072 tokens. However, you requested "
        "about 262244 tokens (100 of text input, 262144 in the output)."
    )

    def serve(body, n):
        if body["max_tokens"] > 131072 - 100:
            return httpx.Response(400, json={"error": {"message": message}})
        return httpx.Response(200, json=_tool_reply())

    llm, requests = provider(serve, max_tokens=262144)

    await llm.complete_with_tools([{"role": "user", "content": "review"}], [SUBMIT_REVIEW_TOOL])

    sent = [r["max_tokens"] for r in requests]
    assert sent[0] == 262144 and len(sent) == 2
    assert 120000 < sent[1] < 131072 - 100


@pytest.mark.asyncio
async def test_output_cap_named_after_a_context_cap_is_used(provider):
    """A model capped by its context window can still name a lower output limit."""
    window = (
        "This endpoint's maximum context length is 131072 tokens. However, you requested "
        "about 262244 tokens (100 of text input, 262144 in the output)."
    )

    def serve(body, n):
        if body["max_tokens"] > 131072 - 100:
            return httpx.Response(400, json={"error": {"message": window}})
        if body["max_tokens"] > 8192:
            # Real endpoints echo the budget that was sent.
            message = (
                f"max_tokens is too large: {body['max_tokens']}. "
                "This model supports at most 8192 completion tokens."
            )
            return httpx.Response(400, json={"error": {"message": message}})
        return httpx.Response(200, json=_tool_reply())

    llm, requests = provider(serve, max_tokens=262144)

    await llm.complete_with_tools([{"role": "user", "content": "review"}], [SUBMIT_REVIEW_TOOL])

    sent = [r["max_tokens"] for r in requests]
    assert sent[0] == 262144 and 120000 < sent[1] < 131072 - 100
    assert sent[2:] == [8192]


@pytest.mark.asyncio
async def test_unrelated_400_is_not_treated_as_a_budget_problem(provider):
    llm, requests = provider(
        lambda body, n: httpx.Response(400, json={"error": {"message": "invalid api key"}})
    )

    with pytest.raises(LLMError):
        await llm.complete_with_tools([{"role": "user", "content": "review"}], [SUBMIT_REVIEW_TOOL])

    assert len(requests) == 1


@pytest.mark.asyncio
async def test_cut_off_reply_is_not_retried_above_a_remembered_cap(provider):
    """Once a model is capped at its limit, a length cut-off cannot be answered
    with a bigger budget, so the reply is returned as it is."""
    truncated = _tool_reply(finish_reason="length")

    def serve(body, n):
        if body["max_tokens"] > 8192:
            return httpx.Response(400, json=_TOO_LARGE)
        return httpx.Response(200, json=truncated)

    llm, requests = provider(serve)

    await llm.complete_with_tools([{"role": "user", "content": "review"}], [SUBMIT_REVIEW_TOOL])

    assert [r["max_tokens"] for r in requests] == [16384, 8192]
