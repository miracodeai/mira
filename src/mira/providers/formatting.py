"""Markdown formatting for review comments — shared across providers.

GitHub and GitLab both render the same Markdown (category badge, severity,
``suggestion`` blocks), so the comment body builders live here rather than in
either provider. Provider-specific concerns (how a comment is *posted*) stay in
the provider classes.
"""

from __future__ import annotations

import html
import re

from mira.models import (
    KeyIssue,
    PRInfo,
    ReviewComment,
    ReviewResult,
    Severity,
    build_review_stats,
)

_CATEGORY_DISPLAY: dict[str, tuple[str, str]] = {
    "bug": ("\U0001f41b", "Bug"),
    "security": ("\U0001f512", "Security issue"),
    "performance": ("⚡", "Performance"),
    "error-handling": ("⚠️", "Error Handling"),
    "race-condition": ("\U0001f3c1", "Race Condition"),
    "resource-leak": ("\U0001f4a7", "Resource Leak"),
    "maintainability": ("\U0001f527", "Refactor suggestion"),
    "style": ("\U0001f3a8", "Style"),
    "clarity": ("\U0001f4dd", "Clarity"),
    "configuration": ("⚙️", "Configuration"),
    "other": ("\U0001f4cc", "Note"),
}

_SEVERITY_BADGE: dict[Severity, str] = {
    Severity.BLOCKER: "\U0001f6d1 Blocker — must fix before merge",
    Severity.WARNING: "⚠️ Warning",
    Severity.SUGGESTION: "\U0001f4a1 Suggestion",
    Severity.NITPICK: "\U0001f4ac Nitpick",
}

_LABEL_TO_CATEGORY = {label: cat for cat, (_, label) in _CATEGORY_DISPLAY.items()}
_CATEGORY_EMOJI_TO_NAME = {emoji: cat for cat, (emoji, _) in _CATEGORY_DISPLAY.items()}
_SEVERITY_EMOJI_MAP: dict[str, str] = {
    "\U0001f6d1": "blocker",
    "⚠️": "warning",
    "⚠": "warning",
    "\U0001f4a1": "suggestion",
    "\U0001f4ac": "nitpick",
}

_CATEGORY_LINE_RE = re.compile(r"^(\S+)\s+\*\*([^*]+)\*\*\s*$")
_BOLD_LINE_RE = re.compile(r"^\*\*([^*\n]+)\*\*\s*$")


def parse_bot_comment_metadata(body: str) -> dict[str, str]:
    """Extract category, severity, and title from a bot review-comment body.

    The bot formats comments as:
        **{Category Label}**
        {severity-emoji} {severity label}

        **{Title}**

        {body...}

    Older comments led the category line with an emoji; both forms parse.

    Returns a dict with keys 'category', 'severity', 'title'. Missing fields
    default to empty string. Safe on malformed input.
    """
    category = ""
    severity = ""
    title = ""

    for raw in body.split("\n"):
        line = raw.strip()
        if not line:
            continue

        if not category:
            m = _CATEGORY_LINE_RE.match(line)
            if m:
                emoji, label = m.group(1), m.group(2).strip()
                if label in _LABEL_TO_CATEGORY:
                    category = _LABEL_TO_CATEGORY[label]
                    continue
                if emoji in _CATEGORY_EMOJI_TO_NAME:
                    category = _CATEGORY_EMOJI_TO_NAME[emoji]
                    continue
            # New format: a plain bold line whose label names a known category.
            mb = _BOLD_LINE_RE.match(line)
            if mb and mb.group(1).strip() in _LABEL_TO_CATEGORY:
                category = _LABEL_TO_CATEGORY[mb.group(1).strip()]
                continue

        if not severity:
            matched = False
            for emoji, sev in _SEVERITY_EMOJI_MAP.items():
                if line.startswith(emoji):
                    severity = sev
                    matched = True
                    break
            if matched:
                continue

        if not title:
            m = _BOLD_LINE_RE.match(line)
            if m:
                title = m.group(1).strip()

        if category and severity and title:
            break

    return {"category": category, "severity": severity, "title": title}


def format_key_issues(key_issues: list[KeyIssue]) -> str:
    """Format key issues as a markdown table for the review body."""
    lines = [
        "",
        "",
        "### Key Issues",
        "",
        "| | Issue | Location |",
        "|---|---|---|",
    ]
    for ki in key_issues:
        lines.append(f"| :red_circle: | {ki.issue} | `{ki.path}:{ki.line}` |")
    return "\n".join(lines)


def _table_cell(value: object) -> str:
    """Keep model-provided text from breaking the summary tables."""
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _format_key_issues_section(key_issues: list[KeyIssue]) -> str:
    """Render key issues in the standard collapsed review section."""
    ordered = sorted(key_issues, key=lambda issue: (issue.path, issue.line, issue.issue))
    lines = [
        "<details>",
        f"<summary>📌 Key issues ({len(ordered)})</summary>",
        "",
        "| | Issue | Location |",
        "|---|---|---|",
    ]
    for issue in ordered:
        lines.append(
            f"| 🔴 | {_table_cell(issue.issue)} | `{_table_cell(issue.path)}:{issue.line}` |"
        )
    lines.extend(["", "</details>"])
    return "\n".join(lines)


def _format_severity_counts(stats: dict[Severity, int]) -> str:
    """Format the compact icon/count line shown beside the review title."""
    labels = {
        Severity.BLOCKER: "blocker",
        Severity.WARNING: "warning",
        Severity.SUGGESTION: "suggestion",
        Severity.NITPICK: "nitpick",
    }
    parts = []
    for severity in (Severity.BLOCKER, Severity.WARNING, Severity.SUGGESTION, Severity.NITPICK):
        count = stats.get(severity, 0)
        if count:
            label = labels[severity] + ("s" if count != 1 else "")
            parts.append(f"{severity.emoji} {count} {label}")
    return f" · {' · '.join(parts)}" if parts else ""


def _review_scope(result: ReviewResult) -> tuple[int, int]:
    """Return reviewed/total file counts while supporting older result payloads."""
    reviewed = len(result.reviewed_paths) if result.reviewed_paths else result.reviewed_files
    total = len(result.total_paths) if result.total_paths else reviewed + len(result.skipped_paths)
    return reviewed, total


def format_review_summary(
    result: ReviewResult,
    bot_name: str = "miracodeai",
    pr_info: PRInfo | None = None,
) -> str:
    """Render the provider-neutral, CodeRabbit-style top-level review body.

    The individual findings remain inline comments. This body is deliberately
    deterministic: sections, severity order, checks, and file lists render in
    the same order on GitHub, GitLab, and Forgejo.
    """
    stats = build_review_stats(result.comments)
    actionable = len(result.comments)
    reviewed, total = _review_scope(result)
    suggestions = sum(1 for comment in result.comments if comment.suggestion)

    parts = [
        "<!-- mira-review-summary -->",
        "## Mira Review Summary",
        "",
        f"**Actionable comments posted: {actionable}**{_format_severity_counts(stats)}",
        "",
    ]
    if actionable == 0:
        parts.append("✅ No actionable comments found.")
        parts.append("")

    parts.extend(
        [
            "<details>",
            "<summary>🤖 Prompt for all review comments with AI agents</summary>",
            "",
            f"Reply to an inline finding with `@{bot_name}` to continue that finding's "
            "conversation. Any comment that supports agent assistance includes its "
            "scoped prompt below the recommendation.",
            "",
            "</details>",
            "",
            "<details>",
            "<summary>🪄 Autofix</summary>",
            "",
            (
                f"{suggestions} inline finding{'s' if suggestions != 1 else ''} "
                "include a GitHub suggestion block. Apply those blocks from the "
                "corresponding inline comments."
                if suggestions
                else "No code suggestions were generated for this review."
            ),
            "",
            "</details>",
            "",
            "---",
            "",
            "<details open>",
            "<summary>ℹ️ Review info</summary>",
            "",
            "| Check | Result |",
            "|---|---|",
            f"| Actionable findings | {'⚠️' if actionable else '✅'} {actionable} |",
            f"| Blocking findings | {'🛑' if stats.get(Severity.BLOCKER, 0) else '✅'} "
            f"{stats.get(Severity.BLOCKER, 0)} |",
            f"| Warnings | {'⚠️' if stats.get(Severity.WARNING, 0) else '✅'} "
            f"{stats.get(Severity.WARNING, 0)} |",
            f"| Suggestions | {'💡' if stats.get(Severity.SUGGESTION, 0) else '✅'} "
            f"{stats.get(Severity.SUGGESTION, 0)} |",
            f"| Nitpicks | {'💬' if stats.get(Severity.NITPICK, 0) else '✅'} "
            f"{stats.get(Severity.NITPICK, 0)} |",
            (
                f"| Review scope | {'⚠️' if result.skipped_paths else '✅'} "
                f"{reviewed} of {total} files processed |"
                if total
                else "| Review scope | ℹ️ File scope not reported |"
            ),
            "",
            "</details>",
            "",
            "<details>",
            "<summary>⚙️ Run configuration</summary>",
            "",
            "- Reviewer: `Mira`",
            f"- Bot: `@{bot_name}`",
        ]
    )
    if pr_info:
        if pr_info.base_branch or pr_info.head_branch:
            parts.append(f"- Branches: `{pr_info.base_branch}` ← `{pr_info.head_branch}`")
        if pr_info.head_sha:
            parts.append(f"- Reviewed commit: `{pr_info.head_sha[:8]}`")
    parts.extend(["", "</details>", ""])

    if result.summary:
        parts.extend(
            [
                "<details open>",
                "<summary>📝 Review summary</summary>",
                "",
                result.summary.strip(),
                "",
                "</details>",
                "",
            ]
        )

    selected_paths = sorted(set(result.reviewed_paths))
    parts.extend(
        [
            "<details>",
            f"<summary>📁 Files selected for processing ({reviewed})</summary>",
            "",
        ]
    )
    if selected_paths:
        parts.extend(f"- `{path}`" for path in selected_paths)
    else:
        parts.append("- File manifest not reported by this review.")
    parts.extend(["", "</details>"])

    if result.skipped_paths:
        skipped_paths = sorted(set(result.skipped_paths))
        parts.extend(
            [
                "",
                "<details>",
                f"<summary>⏭️ Files skipped from review ({len(skipped_paths)})</summary>",
                "",
                *[f"- `{path}`" for path in skipped_paths],
                "",
                "</details>",
            ]
        )

    if result.key_issues:
        parts.extend(["", _format_key_issues_section(result.key_issues)])

    return "\n".join(parts)


_FENCE_RE = re.compile(r"^(`{3,})")


def _strip_suggestion_fences(text: str) -> str:
    """Remove wrapping triple-backtick fences the LLM may add to suggestion code.

    The suggestion content is placed inside a ```suggestion``` fence by the
    caller, so any fences inside the content itself would break rendering. We
    strip a leading fence line, a trailing fence line, and any stray
    backtick-only lines in the middle.
    """
    lines = text.split("\n")
    if lines and _FENCE_RE.match(lines[0].strip()):
        lines = lines[1:]
    if lines and _FENCE_RE.match(lines[-1].strip()):
        lines = lines[:-1]
    lines = [ln for ln in lines if not re.fullmatch(r"`{3,}\s*", ln.strip())]
    return "\n".join(lines)


def _close_open_fences(parts: list[str]) -> None:
    """If the accumulated body has an unclosed code fence, close it."""
    open_fence = False
    for part in parts:
        for line in part.split("\n"):
            if _FENCE_RE.match(line.strip()):
                open_fence = not open_fence
    if open_fence:
        parts.append("```")


def format_comment_body(comment: ReviewComment, bot_name: str = "miracodeai") -> str:
    """Format a review comment body with category badge, severity, and suggestion block."""
    label = _CATEGORY_DISPLAY.get(comment.category, ("\U0001f4cc", "Note"))[1]
    badge = _SEVERITY_BADGE.get(comment.severity, "")

    # Two trailing spaces = a Markdown hard break. GitHub renders a bare
    # newline as a break but GitLab doesn't, so the category and severity
    # would otherwise run together on one line on GitLab.
    parts = [f"**{label}**" + ("  " if badge else "")]
    if badge:
        parts.append(badge)
    parts.append("")
    parts.append(f"**{comment.title}**")
    parts.append("")
    parts.append(comment.body)

    if comment.suggestion:
        clean_suggestion = html.unescape(comment.suggestion)
        clean_suggestion = _strip_suggestion_fences(clean_suggestion)
        # Close any unbalanced fence in the body so it doesn't swallow the suggestion.
        _close_open_fences(parts)
        parts.append("")
        parts.append("```suggestion")
        parts.append(clean_suggestion)
        parts.append("```")

    if comment.agent_prompt:
        prompt_text = comment.agent_prompt
        if comment.suggestion:
            prompt_text += f"\n\nApply this code change:\n\n{html.unescape(comment.suggestion)}"

        # A fenced block (not <pre>) — GitHub 422'd on <pre>-wrapped prompts.
        max_run = 0
        run = 0
        for ch in prompt_text:
            if ch == "`":
                run += 1
                max_run = max(max_run, run)
            else:
                run = 0
        fence = "`" * max(3, max_run + 1)

        parts.append("")
        parts.append("---")
        parts.append("")
        parts.append(
            "<details>\n"
            "<summary>Prompt for AI Agents</summary>\n"
            "\n"
            f"{fence}\n{prompt_text}\n{fence}\n"
            "\n"
            "</details>"
        )

    parts.append("")
    parts.append(f"> Not useful? Reply `@{bot_name} reject` to dismiss this suggestion.")

    return "\n".join(parts)
