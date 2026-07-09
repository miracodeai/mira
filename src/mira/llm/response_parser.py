"""Parse and validate LLM JSON output."""

from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, Field

from mira.core.context import extract_hunk_lines
from mira.exceptions import ResponseParseError
from mira.llm.utils import strip_code_fences, strip_think_blocks
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

logger = logging.getLogger(__name__)


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


# Anthropic-style tool-call XML delimiters some models leak into the JSON
# arguments string (seen on Haiku via OpenRouter), e.g. a valid object
# followed by ``</parameter></invoke>``. We cut the response at the first such
# tag, then re-balance braces.
_TOOL_XML_TAGS = ("</parameter>", "</invoke>", "</function_calls>", "<parameter", "<invoke")


def _balance_json(text: str) -> str:
    """Close any unclosed strings/brackets so a truncated object parses."""
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif (ch == "}" and stack and stack[-1] == "{") or (
            ch == "]" and stack and stack[-1] == "["
        ):
            stack.pop()
    out = text.rstrip().rstrip(",").rstrip()
    if in_string:
        out += '"'
    closers = {"{": "}", "[": "]"}
    return out + "".join(closers[c] for c in reversed(stack))


def _repair_json(text: str) -> str:
    """Best-effort repair: drop leaked tool-call XML, then re-balance brackets."""
    cut = min((i for i in (text.find(t) for t in _TOOL_XML_TAGS) if i != -1), default=-1)
    if cut != -1:
        text = text[:cut]
    return _balance_json(text)


def loads_lenient(text: str) -> object | None:
    """Parse JSON, repairing leaked tool-call XML / missing braces. None on failure."""
    for candidate in (text, _repair_json(text)):
        try:
            return json.loads(candidate, strict=False)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def parse_llm_response(raw_text: str) -> LLMReviewResponse:
    """Parse raw LLM text output into a validated LLMReviewResponse."""
    cleaned = strip_think_blocks(raw_text)
    cleaned = strip_code_fences(cleaned)

    # strict=False tolerates raw newlines from models that double-encode the
    # comments array as a pretty-printed JSON string; the repair pass salvages
    # responses with leaked tool-call XML or a missing closing brace.
    data = loads_lenient(cleaned)
    if data is None:
        raise ResponseParseError("LLM response is not valid JSON (even after repair)")

    if not isinstance(data, dict):
        raise ResponseParseError(f"Expected JSON object, got {type(data).__name__}")

    data = _unstring_nested_json(data)

    try:
        return LLMReviewResponse.model_validate(data)
    except Exception as e:
        raise ResponseParseError(f"LLM response validation failed: {e}") from e


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
    hunk_index: dict[str, str] = (
        {f.path: extract_hunk_lines(f) for f in diff_files} if diff_files else {}
    )
    diff_ranges: dict[str, list[tuple[int, int]]] = (
        _build_diff_line_ranges(diff_files) if diff_files else {}
    )
    result: list[ReviewComment] = []

    for c in response.comments:
        if valid_paths is not None and c.path not in valid_paths:
            continue

        if c.line < 1:
            continue

        if diff_ranges and c.path in diff_ranges:
            file_ranges = diff_ranges[c.path]
            if not any(start <= c.line <= end for start, end in file_ranges):
                snapped = _snap_to_diff(c.line, file_ranges)
                if snapped is not None:
                    c.line = snapped
                    c.end_line = None
                else:
                    continue

        if c.suggestion and not c.body.strip():
            continue

        # Drop hallucinated citations (present existing_code that isn't in the diff).
        if hunk_index and c.existing_code:
            hunk_text = hunk_index.get(c.path, "")
            if c.existing_code.strip() not in hunk_text:
                continue

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
                existing_code=c.existing_code,
            )
        )

    return result


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

    Some models (via tool calling) double-encode nested objects as JSON
    strings — for example returning the ``comments`` array as
    ``"[{...}, {...}]"`` instead of a real list. We try strict JSON first,
    then ``strict=False`` to tolerate raw control chars (newlines inside
    strings).
    """
    result = {}
    for key, value in data.items():
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith(("[", "{")):
                parsed = _try_load_json(stripped)
                if isinstance(parsed, (dict, list)):
                    value = parsed
        if isinstance(value, dict):
            value = _unstring_nested_json(value)
        elif isinstance(value, list):
            value = [
                _unstring_nested_json(item) if isinstance(item, dict) else item for item in value
            ]
        result[key] = value
    return result


def _try_load_json(text: str) -> object | None:
    """Best-effort parse: strict, then lenient (control chars allowed)."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        return json.loads(text, strict=False)
    except (json.JSONDecodeError, TypeError):
        return None


def _validate_change_groups(raw_groups: list) -> list[LLMWalkthroughChangeGroup]:
    """Validate change groups one at a time, skipping malformed entries.

    LLMs occasionally omit a required ``path`` or ``label`` on a single
    entry in a large diff; one bad entry shouldn't drop the whole
    walkthrough (issue #162).
    """
    groups: list[LLMWalkthroughChangeGroup] = []
    for item in raw_groups:
        if not isinstance(item, dict):
            logger.warning("Skipping malformed walkthrough change group: %r", item)
            continue
        raw_files = item.get("files")
        if isinstance(raw_files, list):
            files = []
            for f in raw_files:
                try:
                    files.append(LLMWalkthroughFileChange.model_validate(f))
                except Exception as exc:
                    logger.warning("Skipping malformed walkthrough file entry: %s", exc)
            try:
                group = LLMWalkthroughChangeGroup.model_validate({**item, "files": []})
                group.files = files
                groups.append(group)
            except Exception as exc:
                logger.warning("Skipping malformed walkthrough change group: %s", exc)
            continue
        try:
            groups.append(LLMWalkthroughChangeGroup.model_validate(item))
        except Exception as exc:
            logger.warning("Skipping malformed walkthrough change group: %s", exc)
    return groups


def parse_walkthrough_response(raw_text: str) -> LLMWalkthroughResponse:
    """Parse raw LLM text output into a validated LLMWalkthroughResponse."""
    cleaned = strip_think_blocks(raw_text)
    cleaned = strip_code_fences(cleaned)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ResponseParseError(f"Walkthrough response is not valid JSON: {e}") from e

    if not isinstance(data, dict):
        raise ResponseParseError(f"Expected JSON object, got {type(data).__name__}")

    data = _unstring_nested_json(data)

    raw_groups = data.pop("change_groups", None)

    try:
        response = LLMWalkthroughResponse.model_validate(data)
    except Exception as e:
        raise ResponseParseError(f"Walkthrough response validation failed: {e}") from e

    if isinstance(raw_groups, list):
        response.change_groups = _validate_change_groups(raw_groups)
    return response


_MERMAID_LABEL_RE = re.compile(r"\[([^\[\]]+)\]")


def _sanitize_mermaid(diagram: str) -> str:
    """Repair nested-quote labels that break Mermaid's parser.

    LLMs occasionally produce ``engine["core/"engine.py""]`` even when
    the prompt explicitly forbids it. The nested ``"`` closes the label
    early and Mermaid bails. We rewrite each ``[...]`` group whose
    content has malformed quotes — strip every ``"`` inside, leave one
    pair around the cleaned text — and pass through well-formed groups
    untouched.
    """
    if not diagram or '"' not in diagram:
        return diagram

    def fix(match: re.Match[str]) -> str:
        content = match.group(1)
        if '"' not in content:
            return match.group(0)
        # Well-formed: starts and ends with ", no internal " marks.
        if content.startswith('"') and content.endswith('"') and content.count('"') == 2:
            return match.group(0)
        cleaned = content.replace('"', "").strip()
        return f'["{cleaned}"]'

    return _MERMAID_LABEL_RE.sub(fix, diagram)


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
        sequence_diagram=_sanitize_mermaid(response.sequence_diagram)
        if response.sequence_diagram
        else None,
    )
