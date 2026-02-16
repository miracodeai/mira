"""Main review orchestration engine."""

from __future__ import annotations

import logging

from mira.analysis.noise_filter import filter_noise
from mira.analysis.severity import classify_severity
from mira.config import MiraConfig
from mira.core.chunker import chunk_files
from mira.core.context import expand_context
from mira.core.diff_parser import parse_diff
from mira.core.file_filter import filter_files
from mira.exceptions import ResponseParseError
from mira.llm.prompts.review import build_review_prompt, build_walkthrough_prompt
from mira.llm.prompts.verify_fixes import build_verify_fixes_prompt, parse_verify_fixes_response
from mira.llm.provider import LLMProvider
from mira.llm.response_parser import (
    convert_to_review_comments,
    convert_to_walkthrough_result,
    parse_llm_response,
    parse_walkthrough_response,
)
from mira.models import (
    WALKTHROUGH_MARKER,
    PRInfo,
    ReviewComment,
    ReviewResult,
    ThreadDecision,
    UnresolvedThread,
    WalkthroughResult,
    build_review_stats,
)
from mira.providers.base import BaseProvider

logger = logging.getLogger(__name__)

_MAX_FULL_FILE_LINES = 500
_LARGE_FILE_CONTEXT_LINES = 50  # ±50 lines = 100-line window


def _number_lines(content: str) -> str:
    """Add line numbers to file content for LLM context."""
    lines = content.splitlines()
    width = len(str(len(lines)))
    return "\n".join(f"{i + 1:>{width}}| {line}" for i, line in enumerate(lines))


def _extract_sections(
    lines: list[str],
    threads: list[UnresolvedThread],
    context_lines: int,
) -> str:
    """Extract and merge relevant sections around each thread's comment line.

    Returns a line-numbered string with merged windows joined by ``...`` separators.
    """
    total = len(lines)
    width = len(str(total))
    # Collect (start, end) ranges for each thread
    ranges: list[tuple[int, int]] = []
    for t in threads:
        start = max(0, t.line - 1 - context_lines)
        end = min(total, t.line - 1 + context_lines + 1)
        ranges.append((start, end))

    # Sort and merge overlapping ranges
    ranges.sort()
    merged: list[tuple[int, int]] = [ranges[0]]
    for start, end in ranges[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))

    # Build snippet with original line numbers
    parts: list[str] = []
    for start, end in merged:
        numbered = [f"{i + 1:>{width}}| {lines[i]}" for i in range(start, end)]
        parts.append("\n".join(numbered))
    return "\n...\n".join(parts)


class ReviewEngine:
    """Orchestrates the full PR review pipeline."""

    def __init__(
        self,
        config: MiraConfig,
        llm: LLMProvider,
        provider: BaseProvider | None = None,
        bot_name: str = "miracodeai",
        dry_run: bool = False,
    ) -> None:
        self.config = config
        self.llm = llm
        self.provider = provider
        self.bot_name = bot_name
        self.dry_run = dry_run

    async def review_pr(self, pr_url: str) -> ReviewResult:
        """Full pipeline: fetch PR -> review -> post results."""
        if not self.provider:
            raise RuntimeError("A provider is required for PR review")

        pr_info = await self.provider.get_pr_info(pr_url)

        # Resolve previously-posted threads that are now fixed
        llm_resolved = 0
        threads_checked = 0
        unresolved_threads: list[UnresolvedThread] = []
        thread_decisions: list[ThreadDecision] = []

        if self.bot_name:
            try:
                (
                    threads_checked,
                    llm_resolved,
                    unresolved_threads,
                    thread_decisions,
                ) = await self._resolve_verified_threads(pr_info)
            except Exception as exc:
                logger.warning("Thread resolution failed, continuing: %s", exc)

        diff_text = await self.provider.get_pr_diff(pr_info)

        result = await self._review_diff_internal(
            diff_text,
            pr_title=pr_info.title,
            pr_description=pr_info.description,
            existing_comments=unresolved_threads or None,
        )

        # Post walkthrough comment before inline review (upsert: edit if exists)
        if result.walkthrough:
            if self.dry_run:
                logger.info("Dry run: skipping walkthrough comment posting")
            else:
                try:
                    stats = build_review_stats(result.comments)
                    markdown = result.walkthrough.to_markdown(
                        bot_name=self.bot_name, review_stats=stats
                    )
                    existing_id = await self.provider.find_bot_comment(pr_info, WALKTHROUGH_MARKER)
                    if existing_id is not None:
                        await self.provider.update_comment(pr_info, existing_id, markdown)
                    else:
                        await self.provider.post_comment(pr_info, markdown)
                except Exception as exc:
                    logger.warning("Failed to post walkthrough comment: %s", exc)

        logger.info(
            "Thread resolution for PR %s: checked %d, resolved %d",
            pr_info.url,
            threads_checked,
            llm_resolved,
        )

        # Only post if there are comments
        if result.comments:
            if self.dry_run:
                logger.info(
                    "Dry run: would post %d comment(s) on PR %s",
                    len(result.comments),
                    pr_info.url,
                )
            else:
                await self.provider.post_review(pr_info, result)
        else:
            logger.info("No code suggestions for PR %s", pr_info.url)

        result.thread_decisions = thread_decisions
        return result

    async def review_diff(self, diff_text: str) -> ReviewResult:
        """Review a diff from stdin — no provider needed."""
        return await self._review_diff_internal(diff_text)

    async def _review_diff_internal(
        self,
        diff_text: str,
        pr_title: str = "",
        pr_description: str = "",
        existing_comments: list[UnresolvedThread] | None = None,
    ) -> ReviewResult:
        """Core review pipeline."""
        # Enforce max_diff_size — truncate at last file boundary to avoid mangled hunks
        max_diff_size = self.config.review.max_diff_size
        if len(diff_text) > max_diff_size:
            logger.warning(
                "Diff size %d exceeds max_diff_size %d, truncating",
                len(diff_text),
                max_diff_size,
            )
            truncated = diff_text[:max_diff_size]
            last_boundary = truncated.rfind("\ndiff --git ")
            diff_text = truncated[:last_boundary] if last_boundary > 0 else truncated

        # Parse
        patch = parse_diff(diff_text)
        if not patch.files:
            return ReviewResult(summary="No files to review.")

        # Filter
        filtered = filter_files(patch.files, self.config.filter)
        if not filtered:
            return ReviewResult(
                summary="All files were filtered out.",
                skipped_reason="All files matched exclusion rules",
            )

        # Walkthrough
        walkthrough: WalkthroughResult | None = None
        if self.config.review.walkthrough:
            try:
                wt_messages = build_walkthrough_prompt(
                    files=filtered,
                    config=self.config,
                    pr_title=pr_title,
                    pr_description=pr_description,
                )
                wt_raw = await self.llm.complete(wt_messages)
                wt_parsed = parse_walkthrough_response(wt_raw)
                walkthrough = convert_to_walkthrough_result(wt_parsed)
            except Exception as exc:
                logger.warning("Walkthrough generation failed, skipping: %s", exc)

        # Expand context
        expanded = expand_context(filtered, self.config.review.context_lines)

        # Chunk
        chunks = chunk_files(
            expanded,
            max_tokens=self.config.llm.max_context_tokens,
            provider=self.llm,
        )

        # Review each chunk
        all_comments: list[ReviewComment] = []
        valid_paths = {f.path for f in filtered}
        summaries: list[str] = []

        # Accumulate prior chunk suggestions so later chunks avoid duplicates
        combined_existing = list(existing_comments) if existing_comments else []

        for i, chunk in enumerate(chunks):
            logger.info("Reviewing chunk %d/%d (%d files)", i + 1, len(chunks), len(chunk.files))

            try:
                messages = build_review_prompt(
                    files=chunk.files,
                    config=self.config,
                    pr_title=pr_title,
                    pr_description=pr_description,
                    existing_comments=combined_existing or None,
                )

                raw_response = await self.llm.complete(messages)
                parsed = parse_llm_response(raw_response)
                comments = convert_to_review_comments(parsed, valid_paths, diff_files=chunk.files)

                all_comments.extend(comments)
                for c in comments:
                    combined_existing.append(
                        UnresolvedThread(
                            thread_id=f"_pending_{i}_{c.path}_{c.line}",
                            path=c.path,
                            line=c.line,
                            body=f"{c.title}: {c.body}",
                        )
                    )
                if parsed.summary:
                    summaries.append(parsed.summary)
            except ResponseParseError as exc:
                logger.warning("Chunk %d/%d failed to parse, skipping: %s", i + 1, len(chunks), exc)

        # Classify severity
        all_comments = [classify_severity(c) for c in all_comments]

        # Noise filter
        final_comments = filter_noise(all_comments, self.config.filter)

        if self.config.review.include_summary:
            summary = " ".join(summaries) if summaries else "No issues found."
        else:
            summary = ""

        return ReviewResult(
            comments=final_comments,
            summary=summary,
            reviewed_files=len(filtered),
            token_usage=self.llm.usage,
            walkthrough=walkthrough,
        )

    async def _resolve_verified_threads(
        self, pr_info: PRInfo
    ) -> tuple[int, int, list[UnresolvedThread], list[ThreadDecision]]:
        """Check all unresolved bot threads and resolve those the LLM confirms as fixed.

        Returns:
            Tuple of (threads_checked, threads_resolved, remaining_unresolved, decisions).
        """
        assert self.provider is not None

        threads = await self.provider.get_unresolved_bot_threads(pr_info, self.bot_name)
        if not threads:
            logger.debug("No unresolved bot threads found for PR %s", pr_info.url)
            return 0, 0, [], []

        logger.info(
            "Found %d unresolved bot thread(s) to verify on PR %s",
            len(threads),
            pr_info.url,
        )

        # Fetch current code for each thread's file (dedupe by path)
        file_contents: dict[str, str] = {}
        for t in threads:
            if t.path not in file_contents:
                file_contents[t.path] = await self.provider.get_file_content(
                    pr_info, t.path, pr_info.head_branch
                )

        # Group threads by file and build size-aware context
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
                    # Can't extract targeted sections without valid line numbers
                    file_groups.append((path, _number_lines(content), path_threads))
                else:
                    snippet = _extract_sections(lines, path_threads, _LARGE_FILE_CONTEXT_LINES)
                    file_groups.append((path, snippet, path_threads))

        # Single LLM call to verify which issues are fixed
        verified_ids = await self._verify_fixes(file_groups)
        verified_set = set(verified_ids)

        # Build per-thread decisions
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
            if self.dry_run:
                resolved = len(verified_ids)
                logger.info("Dry run: would resolve %d thread(s): %s", resolved, verified_ids)
            else:
                resolved = await self.provider.resolve_threads(pr_info, verified_ids)
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

    async def _verify_fixes(
        self, file_groups: list[tuple[str, str, list[UnresolvedThread]]]
    ) -> list[str]:
        """Ask the LLM which review issues have been fixed."""
        prompt = build_verify_fixes_prompt(file_groups)
        logger.debug("Verify-fixes prompt:\n%s", prompt[1]["content"])
        response = await self.llm.complete(prompt, json_mode=True, temperature=0.0)
        logger.debug("Verify-fixes raw response:\n%s", response)
        return parse_verify_fixes_response(response)
