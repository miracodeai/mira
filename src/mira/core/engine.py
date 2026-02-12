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
from mira.llm.prompts.review import build_review_prompt
from mira.llm.provider import LLMProvider
from mira.llm.response_parser import convert_to_review_comments, parse_llm_response
from mira.models import ReviewComment, ReviewResult
from mira.providers.base import BaseProvider

logger = logging.getLogger(__name__)


class ReviewEngine:
    """Orchestrates the full PR review pipeline."""

    def __init__(
        self,
        config: MiraConfig,
        llm: LLMProvider,
        provider: BaseProvider | None = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.provider = provider

    async def review_pr(self, pr_url: str) -> ReviewResult:
        """Full pipeline: fetch PR -> review -> post results."""
        if not self.provider:
            raise RuntimeError("A provider is required for PR review")

        pr_info = await self.provider.get_pr_info(pr_url)
        diff_text = await self.provider.get_pr_diff(pr_info)

        result = await self._review_diff_internal(
            diff_text,
            pr_title=pr_info.title,
            pr_description=pr_info.description,
        )

        # Only post if there are comments
        if result.comments:
            await self.provider.post_review(pr_info, result)

        return result

    async def review_diff(self, diff_text: str) -> ReviewResult:
        """Review a diff from stdin — no provider needed."""
        return await self._review_diff_internal(diff_text)

    async def _review_diff_internal(
        self,
        diff_text: str,
        pr_title: str = "",
        pr_description: str = "",
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

        for i, chunk in enumerate(chunks):
            logger.info("Reviewing chunk %d/%d (%d files)", i + 1, len(chunks), len(chunk.files))

            try:
                messages = build_review_prompt(
                    files=chunk.files,
                    config=self.config,
                    pr_title=pr_title,
                    pr_description=pr_description,
                )

                raw_response = await self.llm.complete(messages)
                parsed = parse_llm_response(raw_response)
                comments = convert_to_review_comments(parsed, valid_paths, diff_files=chunk.files)

                all_comments.extend(comments)
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
        )
