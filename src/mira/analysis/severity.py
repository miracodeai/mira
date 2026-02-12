"""Severity classification and normalization."""

from __future__ import annotations

import re
from dataclasses import replace

from mira.models import ReviewComment, Severity

# Exploitable vulnerabilities — these deserve BLOCKER
_EXPLOITABLE_KEYWORDS = [
    "sql injection",
    "xss",
    "cross-site scripting",
    "command injection",
    "shell injection",
    "path traversal",
    "directory traversal",
    "remote code execution",
    "arbitrary code",
    "eval(",
    "exec(",
    "deserialization",
    "buffer overflow",
]

# Short keywords that need word-boundary matching to avoid false positives
_EXPLOITABLE_WORD_PATTERNS = [
    re.compile(r"\brce\b"),
    re.compile(r"\bcsrf\b"),
    re.compile(r"\bssrf\b"),
]

# Security smells — bad practice but not directly exploitable, cap at WARNING
_SECURITY_SMELL_KEYWORDS = [
    "hardcoded",
    "default key",
    "default password",
    "default secret",
    "insecure default",
    "missing error handling",
    "missing validation",
    "insecure",
    "vulnerability",
]

_STYLE_KEYWORDS = [
    "naming convention",
    "variable name",
    "formatting",
    "whitespace",
    "indentation",
    "line length",
    "import order",
    "unused import",
    "trailing whitespace",
    "blank line",
]


def normalize_severity(value: str) -> Severity:
    """Normalize an LLM-provided severity string to a Severity enum value."""
    return Severity.from_str(value)


def classify_severity(comment: ReviewComment) -> ReviewComment:
    """Apply heuristic overrides to a comment's severity.

    - Exploitable vulnerabilities (injection, RCE, etc.) get upgraded to BLOCKER
    - Security smells (hardcoded keys, etc.) are capped at WARNING
    - General security issues get upgraded to at least WARNING
    - Pure style issues get downgraded to at most NITPICK
    """
    text = f"{comment.title} {comment.body}".lower()
    is_security = comment.category == "security"

    is_exploitable = any(kw in text for kw in _EXPLOITABLE_KEYWORDS) or any(
        p.search(text) for p in _EXPLOITABLE_WORD_PATTERNS
    )
    if is_exploitable:
        if comment.severity < Severity.BLOCKER:
            return replace(comment, severity=Severity.BLOCKER)
        return comment

    if is_security or any(kw in text for kw in _SECURITY_SMELL_KEYWORDS):
        if comment.severity != Severity.WARNING:
            return replace(comment, severity=Severity.WARNING)
        return comment

    if (
        comment.category == "style" or _is_style_only(text)
    ) and comment.severity > Severity.NITPICK:
        return replace(comment, severity=Severity.NITPICK)

    return comment


def _is_style_only(text: str) -> bool:
    """Check if comment text is purely about style/formatting."""
    return any(kw in text for kw in _STYLE_KEYWORDS) and not any(
        kw in text for kw in ["bug", "error", "crash", "security", "vulnerability"]
    )
