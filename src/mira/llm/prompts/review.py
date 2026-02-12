"""Prompt builder for PR review."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from mira.config import MiraConfig
from mira.core.context import build_file_context_string
from mira.models import FileDiff

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
) -> list[dict[str, str]]:
    """Build the review prompt messages for the LLM.

    Returns a list of message dicts with 'role' and 'content' keys.
    """
    env = _get_template_env()
    template = env.get_template("review.jinja2")

    file_contexts = [build_file_context_string(f) for f in files]
    file_paths = [f.path for f in files]

    system_content = template.render(
        pr_title=pr_title,
        pr_description=pr_description,
        file_contexts=file_contexts,
        file_paths=file_paths,
        confidence_threshold=config.filter.confidence_threshold,
        max_comments=config.filter.max_comments,
        focus_only_on_problems=config.review.focus_only_on_problems,
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "\n\n".join(file_contexts)},
    ]
