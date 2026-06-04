"""Noise filtering pipeline for review comments."""

from __future__ import annotations

from mira.config import FilterConfig
from mira.models import ReviewComment, Severity


def _jaccard_similarity(a: str, b: str) -> float:
    """Compute Jaccard similarity between two strings (word-level)."""
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def _lines_overlap(c1: ReviewComment, c2: ReviewComment) -> bool:
    """Check if two comments target the same file and overlapping lines."""
    if c1.path != c2.path:
        return False
    end1 = c1.end_line or c1.line
    end2 = c2.end_line or c2.line
    return c1.line <= end2 and c2.line <= end1


def _is_duplicate(
    a: ReviewComment,
    b: ReviewComment,
    title_threshold: float = 0.6,
) -> bool:
    """Determine if two comments are duplicates using a composite check.

    Two comments are duplicates if:
    - Same file + overlapping lines + any title similarity (>= 0.25), OR
    - Same file + exact same line + different titles (line-overlap alone is enough)
    - Same file + non-overlapping lines but near-identical titles (>= title_threshold)
    """
    title_sim = _jaccard_similarity(a.title, b.title)

    # Cross-file: near-identical title + similar body → same conceptual issue
    if a.path != b.path:
        body_sim = _jaccard_similarity(a.body, b.body)
        return title_sim >= 0.8 and body_sim >= 0.5

    overlap = _lines_overlap(a, b)

    # Exact same line — almost certainly about the same code
    end_a = a.end_line or a.line
    end_b = b.end_line or b.line
    same_line = (a.line == b.line) and (end_a == end_b)
    if same_line:
        return True

    # Overlapping lines — lower title similarity bar
    if overlap and title_sim >= 0.2:
        return True

    # Non-overlapping but very similar titles in the same file
    return title_sim >= title_threshold


def _deduplicate(
    comments: list[ReviewComment],
    title_threshold: float = 0.6,
) -> list[ReviewComment]:
    """Remove duplicate comments using composite similarity."""
    if not comments:
        return []

    kept: list[ReviewComment] = []
    for comment in comments:
        if not any(_is_duplicate(comment, existing, title_threshold) for existing in kept):
            kept.append(comment)

    return kept


def filter_noise(
    comments: list[ReviewComment],
    config: FilterConfig,
    review_round: int = 1,
) -> list[ReviewComment]:
    """Apply the full noise filtering pipeline.

    1. Drop below confidence threshold (escalates per ``review_round``)
    2. Drop below minimum severity (escalates per ``review_round``)
    3. Sort by severity (desc) then confidence (desc)
    4. Deduplicate (first occurrence = highest quality)
    5. Apply the cap **only to suggestions and nitpicks** — every blocker
       and warning that survives the floor gets posted, no matter how
       many. Real bugs deserve to be flagged; the cap is a noise control
       for low-priority findings, not a quality cap on real ones.

    ``review_round`` is the 1-indexed count of bot reviews on this PR.
    Round 1 uses ``config`` defaults; round 2+ raise the floor so the bot
    converges quickly across follow-up pushes — the principle being
    "round 1 sets the bar; later rounds only flag new high-confidence
    findings, typically bugs the team introduced while applying fixes."
    """
    confidence_floor = config.confidence_threshold
    min_severity = Severity.from_str(config.min_severity)

    if review_round >= 2:
        # Tighten without dropping real catches: confident bugs are scored
        # 0.85+ by the LLM, so 0.8 still lets them through while filtering
        # the marginal/hedged ones.
        confidence_floor = max(confidence_floor, 0.8)
        min_severity = max(min_severity, Severity.WARNING)
    if review_round >= 3:
        confidence_floor = max(confidence_floor, 0.85)

    result = [c for c in comments if c.confidence >= confidence_floor]
    result = [c for c in result if c.severity >= min_severity]
    result.sort(key=lambda c: (c.severity, c.confidence), reverse=True)
    result = _deduplicate(result)

    # Split: blockers + warnings always post; suggestions + nitpicks share
    # the cap so they can't drown out real findings.
    urgent = [c for c in result if c.severity >= Severity.WARNING]
    low_priority = [c for c in result if c.severity < Severity.WARNING]
    return urgent + low_priority[: config.max_comments]
