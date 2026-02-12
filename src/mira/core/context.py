"""Hunk merging and context string building."""

from __future__ import annotations

from dataclasses import replace

from mira.models import FileDiff, HunkInfo


def expand_context(files: list[FileDiff], context_lines: int = 3) -> list[FileDiff]:
    """Merge adjacent/overlapping hunks in each file.

    Hunks whose expanded ranges (with context_lines padding) overlap
    are merged into a single hunk.
    """
    result: list[FileDiff] = []
    for f in files:
        if len(f.hunks) <= 1:
            result.append(f)
            continue

        sorted_hunks = sorted(f.hunks, key=lambda h: h.target_start)
        merged: list[HunkInfo] = [sorted_hunks[0]]

        for hunk in sorted_hunks[1:]:
            prev = merged[-1]
            prev_end = prev.target_start + prev.target_length + context_lines
            hunk_start = hunk.target_start - context_lines

            if hunk_start <= prev_end:
                new_end = max(
                    prev.target_start + prev.target_length,
                    hunk.target_start + hunk.target_length,
                )
                merged[-1] = HunkInfo(
                    source_start=prev.source_start,
                    source_length=prev.source_length + hunk.source_length,
                    target_start=prev.target_start,
                    target_length=new_end - prev.target_start,
                    content=prev.content + "\n" + hunk.content,
                )
            else:
                merged.append(hunk)

        result.append(replace(f, hunks=merged))

    return result


def extract_hunk_lines(file_diff: FileDiff) -> str:
    """Return the raw content of all hunks for a file as a single string.

    Used for validating that LLM-quoted ``existing_code`` actually appears in the diff.
    """
    return "\n".join(h.content for h in file_diff.hunks)


def build_file_context_string(file_diff: FileDiff) -> str:
    """Format a file diff as a markdown string for the LLM prompt."""
    parts: list[str] = []
    lang = file_diff.language or ""

    parts.append(f"### `{file_diff.path}` ({file_diff.change_type.value})")
    if file_diff.old_path:
        parts.append(f"Renamed from `{file_diff.old_path}`")
    parts.append(f"+{file_diff.added_lines} / -{file_diff.deleted_lines} lines\n")

    for hunk in file_diff.hunks:
        parts.append(f"```{lang}")
        parts.append(hunk.content.rstrip())
        parts.append("```\n")

    return "\n".join(parts)
