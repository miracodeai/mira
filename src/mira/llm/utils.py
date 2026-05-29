"""Shared utilities for LLM output processing."""

from __future__ import annotations

import json
import re

_THINK_RE = re.compile(r"<think>.*?", re.DOTALL)


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
