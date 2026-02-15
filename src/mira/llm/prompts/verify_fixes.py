"""Prompt builder for verifying whether review issues have been fixed."""

from __future__ import annotations

from mira.models import UnresolvedThread

# Markers that signal the start of noise sections in formatted review comments.
# Everything from these markers onward is stripped before inclusion in prompts.
_BODY_NOISE_MARKERS = ("**Suggested fix:**", "```suggestion", "<details>")

_MAX_DESCRIPTION_LENGTH = 300


def _extract_issue_description(body: str) -> str:
    """Extract the core issue description from a formatted review comment body.

    Mira's posted comments follow this structure::

        {emoji} **{category_label}**
        {severity_badge}

        **{title}**

        {description}

        **Suggested fix:**
        ```suggestion ...```

        <details>🤖 Prompt for AI Agents ...</details>

    This function strips the badge header, suggestion blocks, and agent prompts,
    returning just the title and explanation text.
    """
    text = body

    # Cut off suggestion blocks and agent prompt sections
    for marker in _BODY_NOISE_MARKERS:
        pos = text.find(marker)
        if pos != -1:
            text = text[:pos]

    # Strip markdown bold markers
    text = text.replace("**", "")

    # Split into paragraphs (double-newline separated)
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    # The formatted comment starts with a compact badge paragraph
    # (emoji + category label + optional severity line).  Skip it when
    # there is more content after it.
    if len(paragraphs) > 1 and len(paragraphs[0]) < 80:
        paragraphs = paragraphs[1:]

    result = " ".join(paragraphs).strip()
    if not result:
        # Fallback: use the cleaned full text
        result = " ".join(text.split()).strip()

    if len(result) > _MAX_DESCRIPTION_LENGTH:
        result = result[:_MAX_DESCRIPTION_LENGTH].rsplit(" ", 1)[0] + "…"
    return result


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
            f'{idx}. (id: "{t.thread_id}") Line {t.line}: {_extract_issue_description(t.body)}'
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


def _strip_code_fences(text: str | None) -> str:
    """Remove markdown code fences wrapping JSON."""
    import re

    if not text:
        return ""
    text = text.strip()
    match = re.match(r"^```(?:json)?\s*\n?(.*?)\n?\s*```$", text, re.DOTALL)
    return match.group(1).strip() if match else text


def parse_verify_fixes_response(raw: str) -> list[str]:
    """Parse the LLM response and return thread IDs confirmed as fixed."""
    import json

    try:
        data = json.loads(_strip_code_fences(raw))
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
