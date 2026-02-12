"""Prompt builder for conversational PR questions."""

from __future__ import annotations

from mira.llm.prompts.review import _get_template_env


def build_conversation_prompt(
    question: str,
    diff_text: str,
    pr_title: str = "",
    pr_description: str = "",
) -> list[dict[str, str]]:
    """Build prompt messages for a conversational reply about a PR.

    Returns a list of message dicts with 'role' and 'content' keys.
    """
    env = _get_template_env()
    template = env.get_template("conversation.jinja2")

    system_content = template.render(
        pr_title=pr_title,
        pr_description=pr_description,
    )

    user_content = f"## Diff\n\n```diff\n{diff_text}\n```\n\n## Question\n\n{question}"

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]
