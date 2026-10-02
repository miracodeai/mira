"""Subscription OAuth accounts + providers (ChatGPT / Claude), HTTP mocked."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from mira.config import LLMConfig
from mira.exceptions import NonRetriableLLMError
from mira.llm import create_llm, credential_store, oauth_accounts
from mira.llm.anthropic_oauth import AnthropicOAuthProvider, _to_anthropic
from mira.llm.chatgpt_oauth import ChatGPTOAuthProvider
from mira.llm.tool_schemas import SUBMIT_REVIEW_TOOL


@pytest.fixture(autouse=True)
def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    oauth_accounts._logins.clear()


def _route(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """Send every httpx.AsyncClient request through handler(request) -> Response."""
    seen: list[httpx.Request] = []
    real = httpx.AsyncClient.__init__

    def init(self, *args, **kwargs):
        def record(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        kwargs["transport"] = httpx.MockTransport(record)
        real(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    return seen


async def _add(provider: str, **creds) -> None:
    await oauth_accounts._add_account(
        provider, {"access": "tok", "refresh": "ref", "expires": time.time() + 3600, **creds}
    )


def test_model_prefix_routes_to_subscription_regardless_of_provider():
    claude = create_llm(LLMConfig(model="claude/claude-sonnet-x"))
    chatgpt = create_llm(LLMConfig(provider="bedrock", model="chatgpt/gpt-6-sol"))
    assert isinstance(claude, AnthropicOAuthProvider)
    assert (claude.config.model, claude.config.fallback_model) == ("claude-sonnet-x", None)
    assert isinstance(chatgpt, ChatGPTOAuthProvider)
    assert chatgpt.config.model == "gpt-6-sol"
    assert not isinstance(
        create_llm(LLMConfig(model="anthropic/claude-sonnet-4.6")), AnthropicOAuthProvider
    )


async def test_subscription_options_list_only_signed_in_accounts(monkeypatch):
    from mira.dashboard import model_catalog

    model_catalog._cache.clear()
    await _add("chatgpt")

    async def fake_list(account):
        return [{"value": "gpt-6-sol", "label": "GPT-6-Sol"}]

    monkeypatch.setattr(oauth_accounts, "list_models", fake_list)

    assert await model_catalog.subscription_options() == [
        {"value": "chatgpt/gpt-6-sol", "label": "GPT-6-Sol (ChatGPT)", "recommended": False}
    ]


async def test_expired_token_is_refreshed_and_persisted(monkeypatch):
    await _add("anthropic", email="me@example.com", expires=time.time() - 10)
    _route(
        monkeypatch,
        lambda r: httpx.Response(
            200, json={"access_token": "new", "refresh_token": "ref2", "expires_in": 28800}
        ),
    )

    account = await oauth_accounts.access_token("anthropic")

    assert account["access"] == "new"
    stored = oauth_accounts._load()["anthropic"][0]
    assert (stored["access"], stored["refresh"], stored["email"]) == (
        "new",
        "ref2",
        "me@example.com",
    )


async def test_public_accounts_mask_email_and_hide_tokens():
    await _add("chatgpt", email="someone@gmail.com")
    accounts = oauth_accounts.public_accounts()
    assert accounts["chatgpt"][0]["email"] == "s***e@gmail.com"
    assert "tok" not in json.dumps(accounts)


async def test_new_account_becomes_active_and_same_email_replaces():
    await _add("chatgpt", email="a@x.com")
    await _add("chatgpt", email="b@x.com")
    await _add("chatgpt", email="a@x.com", access="newer")
    accounts = oauth_accounts._load()["chatgpt"]
    assert [a["email"] for a in accounts] == ["b@x.com", "a@x.com"]
    assert oauth_accounts._active(accounts)["access"] == "newer"


async def test_claude_paste_accepts_redirect_url(monkeypatch):
    seen = _route(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json={
                "access_token": "a",
                "refresh_token": "r",
                "expires_in": 3600,
                "account": {"email_address": "me@yahoo.com"},
            },
        ),
    )

    async def _no_server() -> None:
        return None

    monkeypatch.setattr(oauth_accounts, "_ensure_callback_server", _no_server)
    login = await oauth_accounts.start_anthropic_login()
    url = f"http://localhost:54545/callback?code=abc&state={login['login_id']}"

    await oauth_accounts.complete_anthropic_login(login["login_id"], url)

    body = json.loads(seen[-1].content)
    assert body["code"] == "abc" and body["code_verifier"]
    assert oauth_accounts.public_accounts()["anthropic"][0]["email"] == "m***e@yahoo.com"


def test_to_anthropic_merges_tool_results_and_prefixes_tools():
    system, msgs = _to_anthropic(
        [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "review"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "t1", "function": {"name": "read_file", "arguments": '{"p": 1}'}},
                    {"id": "t2", "function": {"name": "grep_repo", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "a"},
            {"role": "tool", "tool_call_id": "t2", "content": "b"},
        ]
    )
    assert system == "rules"
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"][0]["name"] == "custom_read_file"
    assert [b["tool_use_id"] for b in msgs[2]["content"]] == ["t1", "t2"]


async def test_claude_review_sends_oauth_shape_and_returns_tool_input(monkeypatch):
    await _add("anthropic")
    seen = _route(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "tool_use",
                        "id": "x",
                        "name": "custom_submit_review",
                        "input": {"comments": []},
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 3},
            },
        ),
    )
    provider = AnthropicOAuthProvider(
        LLMConfig(provider="anthropic-oauth", model="claude-sonnet-x")
    )

    result = await provider.review(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    )

    assert json.loads(result) == {"comments": []}
    req = seen[-1]
    body = json.loads(req.content)
    assert req.headers["authorization"] == "Bearer tok"
    assert "oauth-2025-04-20" in req.headers["anthropic-beta"]
    assert body["system"][0]["text"] == oauth_accounts.CLAUDE_CODE_IDENTITY
    assert body["tool_choice"] == {
        "type": "tool",
        "name": "custom_" + SUBMIT_REVIEW_TOOL["function"]["name"],
    }
    assert provider.usage["total_tokens"] == 13


async def test_chatgpt_streams_and_moves_system_to_instructions(monkeypatch):
    await _add("chatgpt", account_id="acct-1")
    item = {
        "type": "function_call",
        "call_id": "c",
        "name": "submit_review",
        "arguments": '{"comments": []}',
    }
    sse = "\n".join(
        [
            "data: " + json.dumps({"type": "response.output_item.done", "item": item}),
            "data: "
            + json.dumps(
                {
                    "type": "response.completed",
                    "response": {"output": [], "usage": {"input_tokens": 5, "output_tokens": 2}},
                }
            ),
            "",
        ]
    )
    seen = _route(monkeypatch, lambda r: httpx.Response(200, text=sse))
    provider = ChatGPTOAuthProvider(LLMConfig(provider="chatgpt-oauth", model="gpt-6-sol"))

    result = await provider.review(
        [{"role": "system", "content": "rules"}, {"role": "user", "content": "u"}]
    )

    assert json.loads(result) == {"comments": []}
    body = json.loads(seen[-1].content)
    assert body["instructions"] == "rules" and body["stream"] is True and body["store"] is False
    assert all(i.get("role") != "system" for i in body["input"])
    assert "temperature" not in body and "max_output_tokens" not in body
    assert seen[-1].headers["chatgpt-account-id"] == "acct-1"
    assert provider.usage["total_tokens"] == 7


async def test_not_signed_in_raises():
    with pytest.raises(NonRetriableLLMError, match="Not signed in to Claude"):
        await oauth_accounts.access_token("anthropic")


async def test_store_is_encrypted_with_secret_key_and_reads_old_plain_json(monkeypatch):
    await _add("chatgpt", email="plain@x.com")  # written before a key was set
    monkeypatch.setenv("MIRA_SECRET_KEY", "s3cret")
    assert oauth_accounts._load()["chatgpt"][0]["email"] == "plain@x.com"

    await _add("chatgpt", email="new@x.com")
    raw = credential_store.path("accounts.json").read_bytes()
    assert b"new@x.com" not in raw and b"tok" not in raw
    assert len(oauth_accounts._load()["chatgpt"]) == 2

    monkeypatch.setenv("MIRA_SECRET_KEY", "wrong")
    # A store from another key reads as empty, so signing in again replaces it.
    assert not oauth_accounts._load().get("chatgpt")
    await _add("chatgpt", email="again@x.com")
    assert [a["email"] for a in oauth_accounts._load()["chatgpt"]] == ["again@x.com"]


async def test_usage_reads_both_providers(monkeypatch):
    await _add("anthropic")
    await _add("chatgpt")
    claude = {
        "five_hour": {"utilization": 16.0, "resets_at": "2026-10-01T21:40:00+00:00"},
        "seven_day": {"utilization": 42.0, "resets_at": "2026-10-06T16:00:00+00:00"},
    }
    window = {"used_percent": 35, "limit_window_seconds": 604800, "reset_at": 1791318256}
    chatgpt = {"rate_limit": {"primary_window": window, "secondary_window": None}}
    _route(
        monkeypatch,
        lambda r: httpx.Response(200, json=claude if "anthropic" in r.url.host else chatgpt),
    )

    usage = await oauth_accounts.usage()

    (claude_windows,) = usage["anthropic"].values()
    (chatgpt_windows,) = usage["chatgpt"].values()
    assert [(w["label"], w["percent"]) for w in claude_windows] == [("5-hour", 16), ("Weekly", 42)]
    assert chatgpt_windows == [
        {"label": "Weekly", "percent": 35.0, "resets_at": "2026-10-06T20:24:16+00:00"}
    ]


_CLAUDE_OK = {"content": [{"type": "text", "text": '{"ok": 1}'}], "usage": {}}


async def test_claude_drops_temperature_when_model_rejects_it(monkeypatch):
    await _add("anthropic")

    def handler(r: httpx.Request) -> httpx.Response:
        if "temperature" in json.loads(r.content):
            return httpx.Response(400, text="`temperature` is deprecated for this model.")
        return httpx.Response(200, json=_CLAUDE_OK)

    seen = _route(monkeypatch, handler)
    provider = AnthropicOAuthProvider(LLMConfig(model="claude-new"))

    assert await provider.complete([{"role": "user", "content": "u"}]) == '{"ok": 1}'
    assert await provider.complete([{"role": "user", "content": "u"}]) == '{"ok": 1}'
    assert len(seen) == 3  # the second call already omits temperature


async def test_claude_falls_back_to_auto_tool_choice(monkeypatch):
    await _add("anthropic")
    tool_use = {"type": "tool_use", "id": "x", "name": "custom_submit_review", "input": {"a": 1}}

    def handler(r: httpx.Request) -> httpx.Response:
        body = json.loads(r.content)
        if body["tool_choice"]["type"] == "tool":
            return httpx.Response(400, text='tool_choice: type "tool" is not supported')
        assert "calling the custom_submit_review tool" in body["system"][-1]["text"]
        return httpx.Response(200, json={"content": [tool_use], "usage": {}})

    seen = _route(monkeypatch, handler)
    provider = AnthropicOAuthProvider(LLMConfig(model="claude-auto-only"))

    assert json.loads(await provider.review([{"role": "user", "content": "u"}])) == {"a": 1}
    assert json.loads(await provider.review([{"role": "user", "content": "u"}])) == {"a": 1}
    assert len(seen) == 3  # the second call goes straight to auto


async def test_claude_auto_tool_choice_rejection_is_not_retried(monkeypatch):
    await _add("anthropic")
    seen = _route(monkeypatch, lambda r: httpx.Response(400, text="tool_choice: bad"))
    provider = AnthropicOAuthProvider(LLMConfig(model="claude-x"))

    with pytest.raises(NonRetriableLLMError):
        await provider.complete_agentic([{"role": "user", "content": "u"}], [SUBMIT_REVIEW_TOOL])
    assert len(seen) == 1


async def test_claude_thinking_uses_adaptive_then_budget(monkeypatch):
    await _add("anthropic")

    def handler(r: httpx.Request) -> httpx.Response:
        if json.loads(r.content)["thinking"]["type"] == "adaptive":
            return httpx.Response(400, text="adaptive thinking is not supported on this model")
        return httpx.Response(200, json=_CLAUDE_OK)

    seen = _route(monkeypatch, handler)
    provider = AnthropicOAuthProvider(LLMConfig(model="claude-old", reasoning_effort="high"))

    assert await provider.complete([{"role": "user", "content": "u"}]) == '{"ok": 1}'
    first, second = (json.loads(r.content) for r in seen)
    assert first["output_config"] == {"effort": "high"} and "temperature" not in first
    # Adaptive thinking also needs room beyond the default 4096 cap to reach the answer.
    assert first["max_tokens"] > 16384
    assert second["thinking"] == {"type": "enabled", "budget_tokens": 16384}
    assert "output_config" not in second and second["max_tokens"] > 16384


async def test_rate_limited_account_rotates_to_next(monkeypatch):
    oauth_accounts._rate_limited_until.clear()
    await _add("anthropic", email="a@x.com", access="tok-a")
    await _add("anthropic", email="b@x.com", access="tok-b")  # active

    def handler(r: httpx.Request) -> httpx.Response:
        if r.headers["authorization"] == "Bearer tok-b":
            return httpx.Response(429, headers={"retry-after": "600"}, text="limit")
        return httpx.Response(200, json=_CLAUDE_OK)

    seen = _route(monkeypatch, handler)
    provider = AnthropicOAuthProvider(
        LLMConfig(model="claude-x", retry_min_wait=0, retry_max_wait=0)
    )

    assert await provider.complete([{"role": "user", "content": "u"}]) == '{"ok": 1}'
    assert [r.headers["authorization"] for r in seen] == ["Bearer tok-b", "Bearer tok-a"]


async def test_model_chain_falls_through_providers_and_cools_failed_model(monkeypatch):
    from mira import llm

    llm._cooling_until.clear()
    oauth_accounts._rate_limited_until.clear()
    await _add("anthropic")
    await _add("chatgpt")
    text_part = {"type": "output_text", "text": '{"from": "gpt"}'}
    message = {"type": "message", "role": "assistant", "content": [text_part]}
    done = {"type": "response.completed", "response": {"output": [message]}}

    def handler(r: httpx.Request) -> httpx.Response:
        if "anthropic" in r.url.host:
            return httpx.Response(429, text="limit")
        return httpx.Response(200, text="data: " + json.dumps(done) + "\n")

    seen = _route(monkeypatch, handler)
    chain = create_llm(
        LLMConfig(
            model="claude/claude-x",
            fallback_models=["chatgpt/gpt-x"],
            max_retries=1,
        )
    )

    assert json.loads(await chain.complete([{"role": "user", "content": "u"}])) == {"from": "gpt"}
    assert json.loads(await chain.complete([{"role": "user", "content": "u"}])) == {"from": "gpt"}
    # The second call skips Claude while it cools down.
    assert [r.url.host for r in seen] == ["api.anthropic.com", "chatgpt.com", "chatgpt.com"]
