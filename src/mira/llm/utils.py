"""Shared utilities for LLM output processing and request shaping."""

from __future__ import annotations

import json
import re

# Match a full <think>…</think> block. MiniMax has been seen to close with
# either </think> or </thinking>, so accept both.
_THINK_RE = re.compile(r"<think>.*?</think(?:ing)?>", re.DOTALL)


# Codex-class Responses API backends reject ``text.format`` of ``json_object``
# unless the request carries an explicit JSON-only instruction in a *user-role*
# content item. AxonHub moves system content into Responses ``instructions``
# and its channel selector may route a Chat Completions request to a
# Responses-only upstream, so the Chat builder needs the same guarantee: a
# stray ``json`` substring in a system prompt, function-call argument, or tool
# output does not count.
_JSON_HINT = "Respond with a JSON object only. No markdown fences, no prose."


def _ensure_json_hint(messages: list[dict]) -> list[dict]:
    """Return a copy of ``messages`` carrying ``_JSON_HINT`` in a user message.

    Works for both chat-shaped ``messages`` (``{"role", "content"}``) and
    Responses ``input`` items, whose user/system entries share that shape. The
    hint is appended to the text of the final user message, keeping the
    caller's original content intact; if there is no user message (or its final
    user message carries non-string content), a dedicated user message is
    added. System messages, function-call arguments, and tool outputs are never
    treated as satisfying the contract.

    Caller-owned messages are never mutated: every container is shallow-copied
    before it is touched. Idempotent — a conversation that already contains the
    hint literal is returned as a copy of itself unchanged.
    """
    copied = [dict(msg) for msg in messages]
    for msg in reversed(copied):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            if _JSON_HINT in content:
                return copied
            msg["content"] = f"{content}\n\n{_JSON_HINT}" if content else _JSON_HINT
            return copied
        break
    copied.append({"role": "user", "content": _JSON_HINT})
    return copied


def strip_think_blocks(text: str | None) -> str:
    """Remove <think>… reasoning blocks from model output.

    Some models (e.g. MiniMax) output <think>… blocks as part of their
    thinking process before the actual response. These must be stripped
    before JSON parsing.
    """
    if not text:
        return ""
    result = _THINK_RE.sub("", text).strip()
    try:
        idx = next(i for i, c in enumerate(result) if c in "{[")
        obj, _ = json.JSONDecoder().raw_decode(result[idx:])
        return json.dumps(obj)
    except (StopIteration, json.JSONDecodeError):
        return result


def strip_code_fences(text: str | None) -> str:
    """Remove markdown code fences wrapping JSON.

    Handles ``None`` input, leading text before the opening fence
    (e.g. LLM analysis preamble), and trailing text after the closing fence.

    When the response contains multiple code blocks (e.g. ``python`` snippets
    in an analysis section followed by a ``json`` result block), only the
    explicitly-tagged ``json`` block is extracted.
    """
    if not text:
        return ""
    text = text.strip()
    # Prefer an explicitly-tagged ```json block anywhere in the response,
    # so we skip unrelated code blocks (```python, etc.) in LLM analysis.
    # Note: re.search scans the entire text, which may be slower for very large
    # responses, but is acceptable for typical LLM output sizes.
    json_match = re.search(r"```json\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if json_match:
        return json_match.group(1).strip()
    # Fall back to a generic code fence at the start of the response
    match = re.match(r"^```\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    return match.group(1).strip() if match else text
