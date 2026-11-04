"""Plain completion output must be usable before retry/fallback succeeds."""

from __future__ import annotations

import json

import httpx
import pytest

from mira.config import LLMConfig
from mira.exceptions import LLMError, NonRetriableLLMError
from mira.llm.provider import LLMProvider
from mira.llm.responses import ResponsesProvider

_MISSING = object()
_REASONING = "SYNTHETIC_REASONING_DO_NOT_RETURN_OR_LOG"
_GOOD = '  {"ok": true}\n'


def _reply(protocol, text=_MISSING):
    if protocol == "chat":
        message = {"role": "assistant", "reasoning": _REASONING}
        if text is not _MISSING:
            message["content"] = text
        return {
            "choices": [{"finish_reason": "stop", "message": message}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3},
        }
    output = [{"type": "reasoning", "summary": [{"type": "summary_text", "text": _REASONING}]}]
    data = {
        "status": "completed",
        "output": output,
        "usage": {"input_tokens": 2, "output_tokens": 3},
    }
    if isinstance(text, str):
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        )
    elif text is None:
        data["output_text"] = None
    return data


@pytest.fixture
def completion(monkeypatch):
    def make(protocol, reply, *, fallback=None, attempts=2):
        requests = []
        config = LLMConfig(
            model="primary",
            fallback_model=fallback,
            base_url="https://llm.example.invalid/v1",
            api_key_env="",
            max_retries=attempts,
            retry_min_wait=0,
            retry_max_wait=0,
        )

        def serve(request):
            assert request.url.host == "llm.example.invalid"
            assert "authorization" not in request.headers
            body = json.loads(request.content)
            requests.append(body)
            return httpx.Response(200, json=reply(body, len(requests)))

        client_type = httpx.AsyncClient
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kwargs: client_type(transport=httpx.MockTransport(serve), **kwargs),
        )
        provider_type = LLMProvider if protocol == "chat" else ResponsesProvider
        return provider_type(config), requests

    return make


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize(
    "blank", ["", " \t\n", None, _MISSING], ids=["empty", "whitespace", "null", "absent"]
)
async def test_blank_completion_retries_same_model_before_succeeding(
    completion, protocol, json_mode, blank
):
    provider, requests = completion(
        protocol,
        lambda body, number: _reply(protocol, blank if number == 1 else _GOOD),
        fallback="fallback",
    )
    messages = [{"role": "user", "content": "Return a synthetic response."}]

    result = await provider.complete(messages, json_mode=json_mode, temperature=0.4, max_tokens=71)

    assert result == _GOOD
    assert [r["model"] for r in requests] == ["primary", "primary"]
    assert requests[0] == requests[1]
    token_field = "max_tokens" if protocol == "chat" else "max_output_tokens"
    assert all(r[token_field] == 71 and r["temperature"] == 0.4 for r in requests)
    format_field = "response_format" if protocol == "chat" else "text"
    assert all((format_field in r) is json_mode for r in requests)
    assert messages == [{"role": "user", "content": "Return a synthetic response."}]
    assert provider.usage == {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}


@pytest.mark.asyncio
@pytest.mark.parametrize("json_mode", [False, True])
@pytest.mark.parametrize(
    "protocol,cue",
    [
        ("chat", "refusal"),
        ("chat", "filter"),
        ("responses", "refusal"),
        ("responses", "empty-refusal"),
        ("responses", "filter"),
    ],
)
async def test_blank_refusal_or_filter_is_terminal(completion, protocol, cue, json_mode):
    refused = "SYNTHETIC_REFUSAL_DO_NOT_LOG"
    payload = _reply(protocol, "")
    if protocol == "chat":
        choice = payload["choices"][0]
        if cue == "refusal":
            choice["message"]["refusal"] = refused
        else:
            choice["finish_reason"] = "content_filter"
    elif cue == "filter":
        payload["status"] = "incomplete"
        payload["incomplete_details"] = {"reason": "content_filter"}
    else:
        payload["output"][-1]["content"].append(
            {
                "type": "refusal",
                "refusal": "" if cue == "empty-refusal" else refused,
            }
        )
    provider, requests = completion(protocol, lambda body, number: payload, fallback="fallback")

    with pytest.raises(NonRetriableLLMError) as error:
        await provider.complete(
            [{"role": "user", "content": "Synthetic task"}], json_mode=json_mode
        )

    assert error.value.code == "completion_blocked"
    assert [r["model"] for r in requests] == ["primary"]
    assert provider.usage == {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
    assert refused not in str(error.value) and _REASONING not in str(error.value)
    assert refused not in error.value.safe_message and _REASONING not in error.value.safe_message


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("outcome", ["fallback-first", "fallback-retry", "exhausted"])
async def test_blank_completion_uses_bounded_fallback(completion, protocol, outcome, caplog):
    healthy_at = {"fallback-first": 3, "fallback-retry": 4, "exhausted": 5}[outcome]
    provider, requests = completion(
        protocol,
        lambda body, number: _reply(protocol, _GOOD if number == healthy_at else ""),
        fallback="fallback",
    )
    if outcome == "exhausted":
        with pytest.raises(LLMError) as error:
            await provider.complete([{"role": "user", "content": "Synthetic task"}])
        assert error.value.code == "both_models_failed"
        assert isinstance(error.value.__cause__, LLMError)
        assert error.value.__cause__.code == "empty_response"
        assert _REASONING not in str(error.value)
    else:
        assert await provider.complete([{"role": "user", "content": "Synthetic task"}]) == _GOOD
    count = min(healthy_at, 4)
    assert [r["model"] for r in requests] == ["primary", "primary"] + ["fallback"] * (count - 2)
    assert provider.usage == {
        "prompt_tokens": 2 * count,
        "completion_tokens": 3 * count,
        "total_tokens": 5 * count,
    }
    assert _REASONING not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("attempts", [1, 3])
async def test_blank_completion_exhausts_without_fallback(completion, protocol, attempts, caplog):
    marker = "SYNTHETIC_UNTRUSTED_RESPONSE_METADATA"
    payload = _reply(protocol, " \n")
    if protocol == "chat":
        payload["choices"][0]["finish_reason"] = marker
    else:
        payload["status"] = marker
    provider, requests = completion(protocol, lambda body, number: payload, attempts=attempts)

    with pytest.raises(LLMError) as error:
        await provider.complete([{"role": "user", "content": "Synthetic task"}])

    assert error.value.code == "completion_failed"
    cause = error.value.__cause__
    assert isinstance(cause, LLMError) and cause.code == "empty_response"
    assert "other" in str(cause) and "content_len=2" in str(cause)
    assert marker not in str(error.value) and marker not in caplog.text
    assert _REASONING not in str(error.value) and _REASONING not in caplog.text
    assert error.value.safe_message == "LLM completion failed"
    assert len(requests) == attempts
    assert provider.usage["total_tokens"] == attempts * 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cue", ["null-refusal", "empty-refusal", "non-assistant-refusal", "stray-filter-reason"]
)
async def test_absent_blocking_signal_still_retries(completion, cue):
    protocol = "chat" if cue in ("null-refusal", "empty-refusal") else "responses"
    payload = _reply(protocol, "")
    if protocol == "chat":
        payload["choices"][0]["message"]["refusal"] = None if cue == "null-refusal" else ""
    elif cue == "non-assistant-refusal":
        payload["output"].append(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "refusal", "refusal": "Synthetic refusal"}],
            }
        )
    else:
        payload["incomplete_details"] = {"reason": "content_filter"}
    provider, requests = completion(
        protocol, lambda body, number: payload if number == 1 else _reply(protocol, _GOOD)
    )

    assert await provider.complete([{"role": "user", "content": "Synthetic task"}]) == _GOOD
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat", "responses"])
async def test_nonblank_text_is_returned_unchanged_on_first_attempt(completion, protocol):
    payload = _reply(protocol, _GOOD)
    provider, requests = completion(protocol, lambda body, number: payload, fallback="fallback")

    assert await provider.complete([{"role": "user", "content": "Synthetic task"}]) == _GOOD
    assert len(requests) == 1
    assert provider.usage["total_tokens"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat", "responses"])
async def test_blank_token_limit_response_retries_without_raising_budget(completion, protocol):
    payload = _reply(protocol, "")
    if protocol == "chat":
        payload["choices"][0]["finish_reason"] = "length"
    else:
        payload["status"] = "incomplete"
        payload["incomplete_details"] = {"reason": "max_output_tokens"}
    provider, requests = completion(
        protocol, lambda body, number: payload if number == 1 else _reply(protocol, _GOOD)
    )

    assert (
        await provider.complete([{"role": "user", "content": "Synthetic task"}], max_tokens=71)
        == _GOOD
    )
    assert len(requests) == 2 and requests[0] == requests[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("cue", ["refusal", "filter"])
async def test_nonblank_text_preserves_existing_metadata_policy(completion, protocol, cue):
    payload = _reply(protocol, _GOOD)
    if protocol == "chat":
        if cue == "refusal":
            payload["choices"][0]["message"]["refusal"] = "Synthetic refusal"
        else:
            payload["choices"][0]["finish_reason"] = "content_filter"
    elif cue == "refusal":
        payload["output"][-1]["content"].append({"type": "refusal", "refusal": "Synthetic refusal"})
    else:
        payload["status"] = "incomplete"
        payload["incomplete_details"] = {"reason": "content_filter"}
    provider, requests = completion(protocol, lambda body, number: payload, fallback="fallback")

    assert await provider.complete([{"role": "user", "content": "Synthetic task"}]) == _GOOD
    assert len(requests) == 1
