"""On OpenRouter, an unset ``reasoning_effort`` means thinking off, as documented.

Mira sent nothing reasoning-shaped when the effort was unset, so a model that
thinks by default (DeepSeek v4.1 flash) reasoned without bound, burned the
whole output budget and returned no tool call. The release smoke eval caught
it on three of five real PRs; even the larger retry budget was not enough on
two of them. The OpenRouter profile now carries an explicit off payload, and
a model whose reasoning is mandatory refuses it once and is then sent nothing.
"""

from __future__ import annotations

import json

import httpx
import pytest

from mira.config import LLMConfig
from mira.llm.provider import LLMProvider
from mira.llm.tool_schemas import SUBMIT_REVIEW_TOOL

_SUBMISSION = json.dumps({"comments": [], "summary": "", "metadata": {"reviewed_files": 1}})
_MANDATORY = {
    "error": {"message": "Reasoning is mandatory for this endpoint and cannot be disabled."}
}


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
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def _text_reply() -> dict:
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


@pytest.fixture
def openrouter(monkeypatch):
    """An LLMProvider on the OpenRouter profile with a scripted HTTP layer."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def make(serve_fn, **config):
        requests: list[dict] = []
        params = {
            "model": "deepseek/deepseek-v4.1-flash-1017",
            "base_url": "https://openrouter.ai/api/v1",
            "max_retries": 1,
            "retry_min_wait": 0,
            "retry_max_wait": 0,
        }
        params.update(config)
        cfg = LLMConfig(**params)

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
        return LLMProvider(cfg), requests

    return make


async def _call(llm: LLMProvider, path: str):
    messages = [{"role": "user", "content": "review"}]
    if path == "tools":
        return await llm.complete_with_tools(messages, [SUBMIT_REVIEW_TOOL])
    if path == "agentic":
        return await llm.complete_agentic(messages, tools=[SUBMIT_REVIEW_TOOL])
    return await llm.complete(messages, json_mode=True)


def _reply_for(path: str) -> dict:
    return _text_reply() if path == "plain" else _tool_reply()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["tools", "agentic", "plain"])
async def test_unset_effort_sends_reasoning_off_on_openrouter(openrouter, path):
    llm, requests = openrouter(lambda body, n: httpx.Response(200, json=_reply_for(path)))

    await _call(llm, path)

    assert requests[0]["reasoning"] == {"enabled": False}
    assert "reasoning_effort" not in requests[0]


@pytest.mark.asyncio
async def test_explicit_effort_sends_effort_not_off(openrouter):
    llm, requests = openrouter(
        lambda body, n: httpx.Response(200, json=_tool_reply()), reasoning_effort="medium"
    )

    await _call(llm, "tools")

    assert requests[0]["reasoning_effort"] == "medium"
    assert "reasoning" not in requests[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["tools", "agentic", "plain"])
async def test_mandatory_reasoning_model_is_sent_nothing_after_one_refusal(openrouter, path):
    def serve(body, n):
        if "reasoning" in body:
            return httpx.Response(400, json=_MANDATORY)
        return httpx.Response(200, json=_reply_for(path))

    llm, requests = openrouter(serve, model="anthropic/claude-sonnet-5.5")

    await _call(llm, path)
    await _call(llm, path)

    # First call: refused once, retried bare. Second call: bare from the start.
    assert len(requests) == 3
    assert "reasoning" in requests[0]
    assert all("reasoning" not in r and "reasoning_effort" not in r for r in requests[1:])


@pytest.mark.asyncio
async def test_refused_effort_on_openrouter_sends_nothing_afterwards(openrouter):
    """Effort and the off switch are one parameter on OpenRouter: a model that
    refused the effort is not then sent the off payload either."""

    def serve(body, n):
        if "reasoning_effort" in body:
            return httpx.Response(400, json={"error": {"message": "reasoning_effort unsupported"}})
        return httpx.Response(200, json=_tool_reply())

    llm, requests = openrouter(serve, reasoning_effort="medium")

    await _call(llm, "tools")
    await _call(llm, "tools")

    assert len(requests) == 3
    assert all("reasoning" not in r and "reasoning_effort" not in r for r in requests[1:])


@pytest.mark.asyncio
async def test_refused_effort_on_deepseek_direct_keeps_thinking_off(monkeypatch):
    """DeepSeek's own endpoint has a separate thinking toggle: after the effort
    field is refused, the explicit off payload is still sent so default-on
    thinking is not left in place."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    requests: list[dict] = []

    def serve(request):
        body = json.loads(request.content)
        requests.append(body)
        if "reasoning_effort" in body:
            return httpx.Response(400, json={"error": {"message": "reasoning_effort unsupported"}})
        return httpx.Response(200, json=_tool_reply())

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(serve), **kwargs),
    )
    llm = LLMProvider(
        LLMConfig(
            model="deepseek-chat",
            base_url="https://api.deepseek.com/v1",
            reasoning_effort="medium",
            max_retries=1,
        )
    )

    await _call(llm, "tools")
    await _call(llm, "tools")

    assert len(requests) == 3
    assert "reasoning_effort" not in requests[1]
    assert requests[1]["thinking"] == {"type": "disabled"}
    assert requests[2]["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_plain_endpoint_default_sends_nothing(monkeypatch):
    """Endpoints with no documented disable keep their own default."""
    requests: list[dict] = []

    def serve(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=_tool_reply())

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(serve), **kwargs),
    )
    llm = LLMProvider(
        LLMConfig(
            model="m", base_url="https://llm.example.invalid/v1", api_key_env="", max_retries=1
        )
    )

    await llm.complete_with_tools([{"role": "user", "content": "review"}], [SUBMIT_REVIEW_TOOL])

    assert "reasoning" not in requests[0] and "reasoning_effort" not in requests[0]
