"""Diff parsing using unidiff."""

from __future__ import annotations

import unidiff

from mira.core.file_types import language_from_path
from mira.exceptions import DiffParseError
from mira.models import FileChangeType, FileDiff, HunkInfo, PatchSet


def _detect_language(path: str) -> str:
    """Detect programming language from file extension."""
    return language_from_path(path)


def _determine_change_type(patched_file: unidiff.PatchedFile) -> FileChangeType:
    if patched_file.is_added_file:
        return FileChangeType.ADDED
    if patched_file.is_removed_file:
        return FileChangeType.DELETED
    if patched_file.is_rename:
        return FileChangeType.RENAMED
    return FileChangeType.MODIFIED


def parse_diff(diff_text: str) -> PatchSet:
    """Parse unified diff text into a PatchSet."""
    if not diff_text.strip():
        return PatchSet()

    try:
        patch = unidiff.PatchSet(diff_text)
    except Exception as e:
        raise DiffParseError(f"Failed to parse diff: {e}") from e

    files: list[FileDiff] = []
    for patched_file in patch:
        path = patched_file.path
        change_type = _determine_change_type(patched_file)

        hunks: list[HunkInfo] = []
        for hunk in patched_file:
            hunks.append(
                HunkInfo(
                    source_start=hunk.source_start,
                    source_length=hunk.source_length,
                    target_start=hunk.target_start,
                    target_length=hunk.target_length,
                    content=str(hunk),
                )
            )

        old_path = None
        if change_type == FileChangeType.RENAMED:
            old_path = patched_file.source_file
            if old_path and old_path.startswith("a/"):
                old_path = old_path[2:]

        files.append(
            FileDiff(
                path=path,
                change_type=change_type,
                hunks=hunks,
                language=_detect_language(path),
                old_path=old_path,
                is_binary=patched_file.is_binary_file,
                added_lines=patched_file.added,
                deleted_lines=patched_file.removed,
            )
        )

    return PatchSet(files=files)
