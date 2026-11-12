"""Prompt caching for Anthropic models behind OpenAI-compatible endpoints.

Anthropic only caches a prompt prefix when the request marks where the
reusable part ends with a ``cache_control`` breakpoint. Mira's prompts open
with long, stable instructions followed by per-PR content (file paths, title,
rules, diffs), so the useful breakpoint sits at the end of the instructions.

Prompt builders mark that point by putting :data:`CACHE_BOUNDARY` on its own
line in a template and passing the rendered text through
:func:`split_system_prompt`, which turns it into two system messages: the
stable prefix first, the per-request remainder second. The sentinel never
reaches a provider. Every backend accepts consecutive system messages, so the
split is harmless where caching is off.

:func:`apply_cache_control` then adds the breakpoints to a Chat Completions
body when the active provider profile opts in (``"prompt_caching":
"anthropic"``) and the model is an Anthropic model:

* an explicit breakpoint on the first system message, but only when the
  builder split the prompt — a lone system message usually carries per-request
  content, and marking it would pay the cache-write premium for nothing;
* for multi-turn tool loops, a top-level automatic breakpoint so each hop
  reads the conversation the previous hop already wrote.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mira.config import LLMConfig

CACHE_BOUNDARY = "<<<MIRA_CACHE_BOUNDARY>>>"

_EPHEMERAL = {"type": "ephemeral"}


def split_system_prompt(rendered: str) -> list[dict]:
    """Split a rendered system prompt at :data:`CACHE_BOUNDARY`.

    Returns one system message when the sentinel is absent, otherwise the
    stable prefix and the per-request remainder as two system messages (the
    remainder is dropped when it is blank).
    """
    if CACHE_BOUNDARY not in rendered:
        return [{"role": "system", "content": rendered}]
    head, _, tail = rendered.partition(CACHE_BOUNDARY)
    head = head.rstrip()
    tail = tail.replace(CACHE_BOUNDARY, "").strip()
    messages = [{"role": "system", "content": head}]
    if tail:
        messages.append({"role": "system", "content": tail})
    return messages


def caching_enabled(config: LLMConfig, profile: dict, model: str) -> bool:
    """True when breakpoints should be sent for ``model`` on this endpoint."""
    if not config.prompt_caching:
        return False
    if profile.get("prompt_caching") != "anthropic":
        return False
    return model.lower().startswith("anthropic/")


def apply_cache_control(body: dict, *, conversation: bool = False) -> None:
    """Add Anthropic cache breakpoints to a Chat Completions ``body`` in place.

    ``body["messages"]`` is replaced with a copy, so caller-owned message
    dicts are never mutated. ``conversation`` adds the top-level automatic
    breakpoint used by multi-turn tool loops.
    """
    messages = list(body.get("messages") or [])
    system_idx = [i for i, m in enumerate(messages) if m.get("role") == "system"]
    if len(system_idx) >= 2:
        first = system_idx[0]
        msg = dict(messages[first])
        content = msg.get("content")
        if isinstance(content, str) and content:
            msg["content"] = [{"type": "text", "text": content, "cache_control": dict(_EPHEMERAL)}]
            messages[first] = msg
            body["messages"] = messages
    if conversation:
        body["cache_control"] = dict(_EPHEMERAL)
