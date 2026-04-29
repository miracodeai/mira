"""Prompt builder for PR review."""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from mira.config import MiraConfig
from mira.core.context import build_file_context_string
from mira.llm.prompts.verify_fixes import _extract_issue_description
from mira.models import FileDiff, UnresolvedThread

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _get_template_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def build_review_prompt(
    files: list[FileDiff],
    config: MiraConfig,
    pr_title: str = "",
    pr_description: str = "",
    existing_comments: list[UnresolvedThread] | None = None,
    code_context: str = "",
    learned_rules: list[str] | None = None,
    custom_rules: list[dict[str, str]] | None = None,
    file_history: dict | None = None,
) -> list[dict[str, str]]:
    """Build the review prompt messages for the LLM.

    Returns a list of message dicts with 'role' and 'content' keys.
    """
    env = _get_template_env()
    template = env.get_template("review.jinja2")

    file_contexts = [build_file_context_string(f) for f in files]
    file_paths = [f.path for f in files]

    # Pre-clean existing comment bodies so the template gets concise descriptions
    cleaned_comments = None
    if existing_comments:
        cleaned_comments = [
            {"path": c.path, "line": c.line, "description": _extract_issue_description(c.body)}
            for c in existing_comments
        ]

    # Decision archaeology — flatten history dict into a list of (path, entries)
    history_for_template = None
    if file_history:
        history_for_template = [
            {
                "path": path,
                "commits": [
                    {
                        "sha": e.sha,
                        "message": e.message,
                        "author": e.author,
                        "date": e.date,
                    }
                    for e in entries
                ],
            }
            for path, entries in file_history.items()
            if entries
        ]

    system_content = template.render(
        pr_title=pr_title,
        pr_description=pr_description,
        file_contexts=file_contexts,
        file_paths=file_paths,
        confidence_threshold=config.filter.confidence_threshold,
        max_comments=config.filter.max_comments,
        focus_only_on_problems=config.review.focus_only_on_problems,
        existing_comments=cleaned_comments,
        has_code_context=bool(code_context),
        learned_rules=learned_rules,
        custom_rules=custom_rules,
        file_history=history_for_template,
    )

    # Build user message with optional code context before diffs
    user_parts = []
    if code_context:
        user_parts.append(code_context)
    user_parts.extend(file_contexts)

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


_HUNK_HEADER_RE = re.compile(r"^@@\s.*@@", re.MULTILINE)


def _extract_hunk_headers(f: FileDiff) -> list[str]:
    """Extract @@ ... @@ header lines from a file's hunks."""
    headers: list[str] = []
    for hunk in f.hunks:
        for m in _HUNK_HEADER_RE.finditer(hunk.content):
            headers.append(m.group(0))
    return headers


def build_walkthrough_prompt(
    files: list[FileDiff],
    config: MiraConfig,
    pr_title: str = "",
    pr_description: str = "",
) -> list[dict[str, str]]:
    """Build the walkthrough prompt messages for the LLM.

    Uses only file metadata (not full diffs) to keep the prompt compact.
    Returns a list of message dicts with 'role' and 'content' keys.
    """
    env = _get_template_env()
    template = env.get_template("walkthrough.jinja2")

    files_metadata = [
        {
            "path": f.path,
            "change_type": f.change_type.value,
            "language": f.language,
            "added_lines": f.added_lines,
            "deleted_lines": f.deleted_lines,
            "hunk_headers": _extract_hunk_headers(f),
        }
        for f in files
    ]

    system_content = template.render(
        pr_title=pr_title,
        pr_description=pr_description,
        files_metadata=files_metadata,
        include_sequence_diagram=config.review.walkthrough_sequence_diagram,
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "Generate the walkthrough for this PR."},
    ]
