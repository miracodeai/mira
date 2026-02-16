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
        issue_lines: list[str] = []
        for idx, t in enumerate(threads, 1):
            line_label = f"Line {t.line}" if t.line > 0 else "Location unknown (outdated comment)"
            outdated_tag = " [OUTDATED — code has changed]" if t.is_outdated else ""
            issue_lines.append(
                f'{idx}. (id: "{t.thread_id}") {line_label}{outdated_tag}: '
                f"{_extract_issue_description(t.body)}"
            )
        issues = "\n".join(issue_lines)
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
                "(full or relevant sections) with line numbers, and a list of "
                "previously flagged issues.\n\n"
                "For each issue:\n"
                "1. Look at the referenced line number in the code.\n"
                "2. Check if the specific problematic code pattern described in "
                "the issue (e.g. a dangerous function call) is still present.\n"
                "3. If the problematic pattern has been removed or replaced with "
                "a safe alternative, mark it as fixed.\n\n"
                "Issues tagged [OUTDATED] have been flagged by GitHub as having "
                "changed code around them — pay extra attention as these are "
                "likely fixed. For issues with unknown location, search the "
                "entire file.\n\n"
                "Respond with JSON: "
                '{"results": [{"id": "<thread_id>", "fixed": true/false}, ...]}'
            ),
        },
        {"role": "user", "content": user_content},
    ]


def _strip_code_fences(text: str | None) -> str:
    """Remove markdown code fences wrapping JSON.

    Handles trailing text after the closing fence (e.g. LLM explanations).
    """
    import re

    if not text:
        return ""
    text = text.strip()
    match = re.match(r"^```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
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
