"""Parse and validate LLM JSON output."""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field

from mira.core.context import extract_hunk_lines
from mira.exceptions import ResponseParseError
from mira.models import FileDiff, ReviewComment, Severity


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
    existing_code: str = ""


class LLMMetadata(BaseModel):
    reviewed_files: int = 0
    skipped_reason: str | None = None


class LLMReviewResponse(BaseModel):
    comments: list[LLMComment] = Field(default_factory=list)
    summary: str = ""
    metadata: LLMMetadata = Field(default_factory=LLMMetadata)


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences wrapping JSON."""
    text = text.strip()
    # Remove ```json ... ``` or ``` ... ```
    pattern = r"^```(?:json)?\s*\n?(.*?)\n?\s*```$"
    match = re.match(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def parse_llm_response(raw_text: str) -> LLMReviewResponse:
    """Parse raw LLM text output into a validated LLMReviewResponse."""
    cleaned = _strip_code_fences(raw_text)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ResponseParseError(f"LLM response is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise ResponseParseError(f"Expected JSON object, got {type(data).__name__}")

    try:
        return LLMReviewResponse.model_validate(data)
    except Exception as e:
        raise ResponseParseError(f"LLM response validation failed: {e}") from e


def _build_hunk_text_index(files: list[FileDiff]) -> dict[str, str]:
    """Map each file path to its concatenated hunk content for lookup."""
    return {f.path: extract_hunk_lines(f) for f in files}


def convert_to_review_comments(
    response: LLMReviewResponse,
    valid_paths: set[str] | None = None,
    diff_files: list[FileDiff] | None = None,
) -> list[ReviewComment]:
    """Convert LLM response comments to ReviewComment models.

    Filters out comments with hallucinated file paths if valid_paths is provided.
    When diff_files is given, validates existing_code against actual hunk content
    and checks for no-op suggestions.
    """
    hunk_index: dict[str, str] = _build_hunk_text_index(diff_files) if diff_files else {}
    result: list[ReviewComment] = []

    for c in response.comments:
        if valid_paths is not None and c.path not in valid_paths:
            continue

        if c.line < 1:
            continue

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
            )
        )

    return result
