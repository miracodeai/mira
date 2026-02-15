"""Prompt builder for verifying whether review issues have been fixed."""

from __future__ import annotations

from mira.models import UnresolvedThread


def build_verify_fixes_prompt(
    file_groups: list[tuple[str, str, list[UnresolvedThread]]],
) -> list[dict[str, str]]:
    """Build a prompt asking the LLM which review issues have been fixed.

    Each entry in *file_groups* is a ``(path, file_content, threads)`` tuple
    where *file_content* is the current code (full file or relevant sections)
    and *threads* lists the unresolved review comments in that file.
    """
    sections: list[str] = []
    for path, content, threads in file_groups:
        issues = "\n".join(
            f'{idx}. (id: "{t.thread_id}") Line {t.line}: "{t.body}"'
            for idx, t in enumerate(threads, 1)
        )
        sections.append(
            f"File: {path}\n```\n{content}\n```\n\nIssues to verify in this file:\n{issues}"
        )

    user_content = "\n\n---\n\n".join(sections)

    return [
        {
            "role": "system",
            "content": (
                "You are verifying whether code review issues have been fixed.\n\n"
                "For each issue below, you will see the current file content "
                "(full or relevant sections) and a list of previously flagged issues.\n"
                "Examine the current file content to determine if each issue has been "
                "addressed. Mark as fixed if the specific concern is no longer present "
                "in the code.\n\n"
                "Respond with JSON: "
                '{"results": [{"id": "<thread_id>", "fixed": true/false}, ...]}'
            ),
        },
        {"role": "user", "content": user_content},
    ]


def parse_verify_fixes_response(raw: str) -> list[str]:
    """Parse the LLM response and return thread IDs confirmed as fixed."""
    import json

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []

    results = data.get("results")
    if not isinstance(results, list):
        return []

    fixed_ids: list[str] = []
    for entry in results:
        if not isinstance(entry, dict):
            continue
        if entry.get("fixed") is True and isinstance(entry.get("id"), str):
            fixed_ids.append(entry["id"])
    return fixed_ids
