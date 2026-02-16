"""Shared utilities for LLM output processing."""

from __future__ import annotations

import re


def strip_code_fences(text: str | None) -> str:
    """Remove markdown code fences wrapping JSON.

    Handles ``None`` input and trailing text after the closing fence
    (e.g. LLM explanations appended after the JSON block).
    """
    if not text:
        return ""
    text = text.strip()
    match = re.match(r"^```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    return match.group(1).strip() if match else text
