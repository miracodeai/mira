"""Heuristics for classifying the *quality* of a human review.

Currently detects "rubber-stamp" approvals — an APPROVED review with no real
scrutiny (empty / "LGTM" body and no substantive inline comments). Shared by the
live webhook handler and the backfill so both classify identically.
"""

from __future__ import annotations

import re

# Low-effort phrases that, on their own, don't count as a substantive review.
# Compared case-insensitively against the text with punctuation/emoji stripped.
TRIVIAL_APPROVAL_PHRASES = {
    "",
    "lgtm",
    "looks good",
    "looks good to me",
    "looks good thanks",
    "ship it",
    "shipit",
    "lg",
    "ok",
    "okay",
    "approved",
    "approve",
    "approving",
    "nice",
    "great",
    "thanks",
    "thank you",
    "ty",
    "cool",
    "yep",
    "yes",
    "done",
    "+1",
    "done thanks",
}

# Below this many "real" characters, a comment can't be substantive on its own.
_MIN_SUBSTANTIVE_LEN = 15

_NON_WORD = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Lower-case, drop emoji/punctuation, collapse whitespace."""
    lowered = (text or "").lower()
    lowered = _NON_WORD.sub(" ", lowered)
    return _WS.sub(" ", lowered).strip()


def is_substantive(text: str) -> bool:
    """Whether a review body or comment shows real engagement (not LGTM-ish)."""
    norm = _normalize(text)
    if not norm or norm in TRIVIAL_APPROVAL_PHRASES:
        return False
    return len(norm) >= _MIN_SUBSTANTIVE_LEN


def is_bare_approval(state: str, body: str, inline_comment_bodies: list[str]) -> bool:
    """True for an approval with zero substantive engagement — a rubber-stamp.

    Engagement = a substantive summary body OR any substantive inline comment;
    a lone "LGTM" comment doesn't save it (same triviality test applies).
    """
    if (state or "").lower() != "approved":
        return False
    if is_substantive(body):
        return False
    return not any(is_substantive(c) for c in inline_comment_bodies)
