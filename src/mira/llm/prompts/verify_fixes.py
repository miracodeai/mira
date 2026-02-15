"""Prompt builder for verifying whether review issues have been fixed."""

from __future__ import annotations

from mira.models import UnresolvedThread


def build_verify_fixes_prompt(
    thread_contexts: list[tuple[UnresolvedThread, str]],
) -> list[dict[str, str]]:
    """Build a prompt asking the LLM which review issues have been fixed.

    Each entry in *thread_contexts* is a ``(thread, code_snippet)`` pair where
    *code_snippet* contains ~20 lines of current code around the original
    comment location.
    """
    issues: list[str] = []
    for idx, (thread, snippet) in enumerate(thread_contexts, 1):
        issues.append(
            f'Issue {idx} (id: "{thread.thread_id}"):\n'
            f"- File: {thread.path}, Line {thread.line}\n"
            f'- Original comment: "{thread.body}"\n'
            f"- Current code:\n```\n{snippet}\n```"
        )

    user_content = "\n\n".join(issues)

    return [
        {
            "role": "system",
            "content": (
                "You are verifying whether code review issues have been fixed.\n\n"
                "For each issue below, you will see the original review comment and "
                "the current code around the commented line.\n"
                "Respond with JSON: "
                '{"results": [{"id": "<thread_id>", "fixed": true/false}, ...]}\n\n'
                "Only mark as fixed if the specific issue described in the comment "
                "has been addressed in the current code. If you are unsure, mark it "
                "as not fixed."
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
