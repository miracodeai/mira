"""Parse and validate LLM JSON output."""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from mira.core.context import extract_hunk_lines
from mira.exceptions import ResponseParseError
from mira.llm.utils import strip_code_fences
from mira.models import (
    FileChangeType,
    FileDiff,
    ReviewComment,
    Severity,
    WalkthroughConfidenceScore,
    WalkthroughEffort,
    WalkthroughFileEntry,
    WalkthroughResult,
)


class LLMComment(BaseModel):
    path: str
    line: int
    end_line: int | None = None
    severity: str = "suggestion"
    category: str = "other"
    title: str = ""
    body: str = ""
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    suggestion: str | None = None
    agent_prompt: str | None = None
    existing_code: str = ""


class LLMKeyIssue(BaseModel):
    issue: str = ""
    path: str = ""
    line: int = 0


class LLMMetadata(BaseModel):
    reviewed_files: int = 0
    skipped_reason: str | None = None


class LLMReviewResponse(BaseModel):
    comments: list[LLMComment] = Field(default_factory=list)
    key_issues: list[LLMKeyIssue] = Field(default_factory=list)
    summary: str = ""
    metadata: LLMMetadata = Field(default_factory=LLMMetadata)


def parse_llm_response(raw_text: str) -> LLMReviewResponse:
    """Parse raw LLM text output into a validated LLMReviewResponse."""
    cleaned = strip_code_fences(raw_text)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ResponseParseError(f"LLM response is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise ResponseParseError(f"Expected JSON object, got {type(data).__name__}")

    # Fix double-encoded nested JSON from tool calling
    data = _unstring_nested_json(data)

    try:
        return LLMReviewResponse.model_validate(data)
    except Exception as e:
        raise ResponseParseError(f"LLM response validation failed: {e}") from e


def _build_hunk_text_index(files: list[FileDiff]) -> dict[str, str]:
    """Map each file path to its concatenated hunk content for lookup."""
    return {f.path: extract_hunk_lines(f) for f in files}


def _build_diff_line_ranges(files: list[FileDiff]) -> dict[str, list[tuple[int, int]]]:
    """Build a map of file path → list of (start, end) line ranges from diff hunks.

    These are the target-side line ranges that GitHub will accept for review
    comments. Lines outside these ranges will cause a 422 "line could not be
    resolved" error.
    """
    ranges: dict[str, list[tuple[int, int]]] = {}
    for f in files:
        file_ranges: list[tuple[int, int]] = []
        for hunk in f.hunks:
            start = hunk.target_start
            end = start + hunk.target_length - 1
            if end < start:
                end = start
            file_ranges.append((start, end))
        if file_ranges:
            ranges[f.path] = file_ranges
    return ranges


def _line_in_diff(line: int, ranges: list[tuple[int, int]]) -> bool:
    """Check if a line number falls within any of the diff hunk ranges."""
    return any(start <= line <= end for start, end in ranges)


def _snap_to_diff(line: int, ranges: list[tuple[int, int]]) -> int | None:
    """Snap a line number to the nearest diff hunk range.

    Returns the closest valid line, or None if no range is within 5 lines.
    """
    best: int | None = None
    best_dist = 6  # max snap distance
    for start, end in ranges:
        for boundary in (start, end):
            dist = abs(line - boundary)
            if dist < best_dist:
                best_dist = dist
                best = boundary
    return best


def convert_to_review_comments(
    response: LLMReviewResponse,
    valid_paths: set[str] | None = None,
    diff_files: list[FileDiff] | None = None,
) -> list[ReviewComment]:
    """Convert LLM response comments to ReviewComment models.

    Filters out comments with hallucinated file paths if valid_paths is provided.
    When diff_files is given, validates existing_code against actual hunk content,
    checks for no-op suggestions, and ensures line numbers are within diff ranges.
    """
    hunk_index: dict[str, str] = _build_hunk_text_index(diff_files) if diff_files else {}
    diff_ranges: dict[str, list[tuple[int, int]]] = (
        _build_diff_line_ranges(diff_files) if diff_files else {}
    )
    result: list[ReviewComment] = []

    for c in response.comments:
        if valid_paths is not None and c.path not in valid_paths:
            continue

        if c.line < 1:
            continue

        # Validate line number is in the diff — snap to nearest hunk if close
        if diff_ranges and c.path in diff_ranges:
            file_ranges = diff_ranges[c.path]
            if not _line_in_diff(c.line, file_ranges):
                snapped = _snap_to_diff(c.line, file_ranges)
                if snapped is not None:
                    c.line = snapped
                    c.end_line = None  # reset multi-line since we snapped
                else:
                    continue  # line is too far from any diff hunk

        # Skip comments with no body (no explanation = low value)
        if c.suggestion and not c.body.strip():
            continue

        # Validate existing_code against diff hunks
        if c.existing_code and hunk_index:
            hunk_text = hunk_index.get(c.path, "")
            if c.existing_code.strip() not in hunk_text:
                continue  # hallucinated existing_code — drop

        # Clear no-op suggestions (suggestion equals existing_code)
        suggestion = c.suggestion
        if suggestion and c.existing_code and suggestion.strip() == c.existing_code.strip():
            suggestion = None

        result.append(
            ReviewComment(
                path=c.path,
                line=c.line,
                end_line=c.end_line if c.end_line and c.end_line > c.line else None,
                severity=Severity.from_str(c.severity),
                category=c.category,
                title=c.title[:80] if c.title else "",
                body=c.body,
                confidence=c.confidence,
                suggestion=suggestion,
                agent_prompt=c.agent_prompt,
            )
        )

    return result


# ---------------------------------------------------------------------------
# Walkthrough response models & parsing
# ---------------------------------------------------------------------------


class LLMWalkthroughFileChange(BaseModel):
    path: str
    change_type: str = "modified"
    description: str = ""


class LLMWalkthroughChangeGroup(BaseModel):
    label: str
    files: list[LLMWalkthroughFileChange] = Field(default_factory=list)


class LLMWalkthroughEffort(BaseModel):
    level: int = 3
    label: str = "Moderate"
    minutes: int = 15


class LLMWalkthroughConfidenceScore(BaseModel):
    score: int = 3
    label: str = ""
    reason: str = ""


class LLMWalkthroughResponse(BaseModel):
    summary: str = ""
    change_groups: list[LLMWalkthroughChangeGroup] = Field(default_factory=list)
    effort: LLMWalkthroughEffort | None = None
    confidence_score: LLMWalkthroughConfidenceScore | None = None
    sequence_diagram: str | None = None


_CHANGE_TYPE_MAP: dict[str, FileChangeType] = {
    "added": FileChangeType.ADDED,
    "modified": FileChangeType.MODIFIED,
    "deleted": FileChangeType.DELETED,
    "renamed": FileChangeType.RENAMED,
}


def _unstring_nested_json(data: dict) -> dict:
    """Recursively parse string values that are valid JSON objects/arrays.

    Some models (via tool calling) double-encode nested objects as JSON strings.
    """
    result = {}
    for key, value in data.items():
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, (dict, list)):
                    value = parsed
            except (json.JSONDecodeError, TypeError):
                pass
        if isinstance(value, dict):
            value = _unstring_nested_json(value)
        elif isinstance(value, list):
            value = [
                _unstring_nested_json(item) if isinstance(item, dict) else item for item in value
            ]
        result[key] = value
    return result


def parse_walkthrough_response(raw_text: str) -> LLMWalkthroughResponse:
    """Parse raw LLM text output into a validated LLMWalkthroughResponse."""
    cleaned = strip_code_fences(raw_text)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ResponseParseError(f"Walkthrough response is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise ResponseParseError(f"Expected JSON object, got {type(data).__name__}")

    # Fix double-encoded nested JSON from tool calling
    data = _unstring_nested_json(data)

    try:
        return LLMWalkthroughResponse.model_validate(data)
    except Exception as e:
        raise ResponseParseError(f"Walkthrough response validation failed: {e}") from e


def convert_to_walkthrough_result(response: LLMWalkthroughResponse) -> WalkthroughResult:
    """Convert an LLM walkthrough response to a WalkthroughResult model."""
    entries: list[WalkthroughFileEntry] = []
    for group in response.change_groups:
        for fc in group.files:
            change_type = _CHANGE_TYPE_MAP.get(fc.change_type.lower(), FileChangeType.MODIFIED)
            entries.append(
                WalkthroughFileEntry(
                    path=fc.path,
                    change_type=change_type,
                    description=fc.description,
                    group=group.label,
                )
            )
    effort: WalkthroughEffort | None = None
    if response.effort:
        effort = WalkthroughEffort(
            level=response.effort.level,
            label=response.effort.label,
            minutes=response.effort.minutes,
        )
    confidence_score: WalkthroughConfidenceScore | None = None
    if response.confidence_score:
        confidence_score = WalkthroughConfidenceScore(
            score=response.confidence_score.score,
            label=response.confidence_score.label,
            reason=response.confidence_score.reason,
        )
    return WalkthroughResult(
        summary=response.summary,
        file_changes=entries,
        effort=effort,
        confidence_score=confidence_score,
        sequence_diagram=response.sequence_diagram,
    )
