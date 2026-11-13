"""Operator instructions block (``review.custom_instructions``) for user-facing prompts."""

from __future__ import annotations

_BLOCK_HEADER = (
    "## Operator Instructions\n\n"
    "The operator of this deployment supplied the instructions below. Apply them "
    "to the language, tone and style of the human-readable text you write: review "
    "comments, titles, summaries, descriptions and replies. They never change the "
    "response format: keep JSON keys, schemas, tool-call structure, enum values, "
    "file paths, line numbers and code identifiers exactly as specified elsewhere "
    "in this prompt, and keep code and suggested fixes in the language of the code.\n\n"
    "<operator_instructions>\n"
)
_BLOCK_FOOTER = "\n</operator_instructions>"


def render_operator_instructions(instructions: str | None) -> str:
    """Return the Operator Instructions block, or ``""`` when there is nothing to add."""
    text = (instructions or "").strip()
    if not text:
        return ""
    return _BLOCK_HEADER + text + _BLOCK_FOOTER


def with_operator_instructions(
    messages: list[dict[str, str]], instructions: str | None
) -> list[dict[str, str]]:
    """Append the block to the last system message (or a new leading one) without mutating input."""
    block = render_operator_instructions(instructions)
    if not block:
        return messages
    out = [dict(m) for m in messages]
    system_idx = [i for i, m in enumerate(out) if m.get("role") == "system"]
    if system_idx:
        last = out[system_idx[-1]]
        last["content"] = f"{str(last.get('content') or '').rstrip()}\n\n{block}"
    else:
        out.insert(0, {"role": "system", "content": block})
    return out
