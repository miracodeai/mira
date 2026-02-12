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
    if a.path != b.path:
        return False

    overlap = _lines_overlap(a, b)
    title_sim = _jaccard_similarity(a.title, b.title)

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


def filter_noise(comments: list[ReviewComment], config: FilterConfig) -> list[ReviewComment]:
    """Apply the full noise filtering pipeline.

    1. Drop below confidence threshold
    2. Drop below minimum severity
    3. Sort by severity (desc) then confidence (desc)
    4. Deduplicate (first occurrence = highest quality)
    5. Cap at max_comments
    """
    min_severity = Severity.from_str(config.min_severity)

    result = [c for c in comments if c.confidence >= config.confidence_threshold]
    result = [c for c in result if c.severity >= min_severity]
    result.sort(key=lambda c: (c.severity, c.confidence), reverse=True)
    result = _deduplicate(result)
    return result[: config.max_comments]
