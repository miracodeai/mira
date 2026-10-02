"""ChatGPT via a Plus/Pro/Business subscription OAuth token — Codex Responses backend.

Reuses ResponsesProvider for request/response shaping; only the transport differs: account
token + chatgpt-account-id headers, system messages moved to "instructions", and a streamed
response folded back into one Responses object.
"""

from __future__ import annotations

import json

import httpx

from mira.exceptions import LLMError
from mira.llm import oauth_accounts
from mira.llm.responses import ResponsesProvider

# The Codex backend rejects these sampling/limit fields.
_UNSUPPORTED = ("temperature", "max_output_tokens")


class ChatGPTOAuthProvider(ResponsesProvider):
    async def _post(self, client: httpx.AsyncClient, body: dict) -> httpx.Response:
        account = await oauth_accounts.access_token("chatgpt")
        body = {k: v for k, v in body.items() if k not in _UNSUPPORTED}
        instructions = [i["content"] for i in body["input"] if i.get("role") == "system"]
        body["input"] = [i for i in body["input"] if i.get("role") != "system"]
        body["instructions"] = "\n\n".join(instructions) or "You are a careful code reviewer."
        body.update(stream=True, store=False)

        output: list[dict] = []
        final: dict = {}
        async with client.stream(
            "POST",
            f"{oauth_accounts.CHATGPT_API}/responses",
            headers={**oauth_accounts.chatgpt_headers(account), "Accept": "text/event-stream"},
            json=body,
        ) as resp:
            if resp.status_code != 200:
                await resp.aread()
                if oauth_accounts.is_quota_error(resp):
                    oauth_accounts.mark_rate_limited(account, resp)
                return resp
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                kind = event.get("type")
                if kind == "response.output_item.done":
                    output.append(event["item"])
                elif kind in ("response.completed", "response.incomplete"):
                    final = event.get("response") or {}
                elif kind in ("response.failed", "error"):
                    raise LLMError("api_error", status=200, body=json.dumps(event)[:2000])
        final["output"] = final.get("output") or output
        return httpx.Response(200, json=final)
