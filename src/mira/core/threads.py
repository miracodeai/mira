"""Resolve and verify PR review threads filed in earlier rounds."""

from __future__ import annotations

import logging

from mira.llm.prompts.verify_fixes import build_verify_fixes_prompt, parse_verify_fixes_response
from mira.llm.provider import LLMProvider
from mira.models import PRInfo, ThreadDecision, UnresolvedThread
from mira.providers.base import BaseProvider

logger = logging.getLogger(__name__)

_MAX_FULL_FILE_LINES = 500
_LARGE_FILE_CONTEXT_LINES = 50  # ±50 lines = 100-line window


def short_thread_description(body: str) -> str:
    """One-line description of a bot review comment for 'already addressed' context.

    Strips badge/category lines and returns the first bold title; falls back
    to the first non-empty line.
    """
    body = body or ""
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith(("⚠️", "🐛", "💡", "🔒", "⚡", "🛑", "🔵", "🟡", "🟠", "🔴")):
            continue
        if s.startswith("**") and s.endswith("**") and len(s) > 4:
            return s.strip("* ")[:160]
        return s[:160]
    return ""


def _number_lines(content: str) -> str:
    lines = content.splitlines()
    width = len(str(len(lines)))
    return "\n".join(f"{i + 1:>{width}}| {line}" for i, line in enumerate(lines))


def _extract_sections(
    lines: list[str],
    threads: list[UnresolvedThread],
    context_lines: int,
) -> str:
    """Extract and merge ±context_lines windows around each thread's line."""
    total = len(lines)
    width = len(str(total))
    ranges: list[tuple[int, int]] = []
    for t in threads:
        start = max(0, t.line - 1 - context_lines)
        end = min(total, t.line - 1 + context_lines + 1)
        ranges.append((start, end))

    if not ranges:
        return ""
    ranges.sort()
    merged: list[tuple[int, int]] = [ranges[0]]
    for start, end in ranges[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))

    parts: list[str] = []
    for start, end in merged:
        numbered = [f"{i + 1:>{width}}| {lines[i]}" for i in range(start, end)]
        parts.append("\n".join(numbered))
    return "\n...\n".join(parts)


async def verify_fixes(
    llm: LLMProvider,
    file_groups: list[tuple[str, str, list[UnresolvedThread]]],
) -> list[str]:
    """Ask the LLM which previously-filed review issues are now fixed."""
    prompt = build_verify_fixes_prompt(file_groups)
    logger.debug("Verify-fixes prompt:\n%s", prompt[1]["content"])
    response = await llm.complete(prompt, json_mode=True, temperature=0.0)
    logger.debug("Verify-fixes raw response:\n%s", response)
    return parse_verify_fixes_response(response)


async def resolve_verified_threads(
    provider: BaseProvider,
    llm: LLMProvider,
    pr_info: PRInfo,
    bot_name: str | None,
    dry_run: bool,
) -> tuple[int, int, list[UnresolvedThread], list[ThreadDecision]]:
    """Resolve unresolved bot threads the LLM confirms as fixed.

    Returns (threads_checked, threads_resolved, remaining_unresolved, decisions).
    """
    threads = await provider.get_unresolved_bot_threads(pr_info, bot_name)
    if not threads:
        logger.debug("No unresolved bot threads found for PR %s", pr_info.url)
        return 0, 0, [], []

    logger.info(
        "Found %d unresolved bot thread(s) to verify on PR %s",
        len(threads),
        pr_info.url,
    )

    file_contents: dict[str, str] = {}
    for t in threads:
        if t.path not in file_contents:
            file_contents[t.path] = await provider.get_file_content(
                pr_info, t.path, pr_info.head_branch
            )

    threads_by_path: dict[str, list[UnresolvedThread]] = {}
    for t in threads:
        threads_by_path.setdefault(t.path, []).append(t)

    file_groups: list[tuple[str, str, list[UnresolvedThread]]] = []
    for path, path_threads in threads_by_path.items():
        content = file_contents.get(path, "")
        lines = content.splitlines()
        if len(lines) <= _MAX_FULL_FILE_LINES:
            file_groups.append((path, _number_lines(content), path_threads))
        else:
            has_unknown_lines = any(t.line <= 0 for t in path_threads)
            if has_unknown_lines:
                file_groups.append((path, _number_lines(content), path_threads))
            else:
                snippet = _extract_sections(lines, path_threads, _LARGE_FILE_CONTEXT_LINES)
                file_groups.append((path, snippet, path_threads))

    verified_ids = await verify_fixes(llm, file_groups)
    verified_set = set(verified_ids)

    decisions = [
        ThreadDecision(
            thread_id=t.thread_id,
            path=t.path,
            line=t.line,
            body=t.body,
            fixed=t.thread_id in verified_set,
        )
        for t in threads
    ]

    resolved = 0
    if verified_ids:
        if dry_run:
            resolved = len(verified_ids)
            logger.info("Dry run: would resolve %d thread(s): %s", resolved, verified_ids)
        else:
            resolved = await provider.resolve_threads(pr_info, verified_ids)
            if resolved < len(verified_ids):
                logger.error(
                    "Failed to resolve %d/%d verified-fixed thread(s) on PR %s",
                    len(verified_ids) - resolved,
                    len(verified_ids),
                    pr_info.url,
                )

    logger.info(
        "LLM verification: checked %d thread(s), %d confirmed fixed, %d resolved",
        len(threads),
        len(verified_ids),
        resolved,
    )

    remaining = [t for t in threads if t.thread_id not in verified_set]
    return len(threads), resolved, remaining, decisions
