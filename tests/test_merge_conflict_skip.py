"""Regression: merge-conflict PRs must not look like clean empty reviews."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from mira.core.engine import ReviewEngine
from mira.models import PRInfo, ReviewResult


@pytest.mark.asyncio
async def test_review_pr_skips_when_mergeable_state_dirty():
    provider = MagicMock()
    provider.get_pr_info = AsyncMock(
        return_value=PRInfo(
            title="t",
            description="",
            base_branch="main",
            head_branch="feat",
            url="https://github.com/o/r/pull/1",
            number=1,
            owner="o",
            repo="r",
            mergeable_state="dirty",
        )
    )
    provider.post_comment = AsyncMock()
    provider.get_pr_diff = AsyncMock(side_effect=AssertionError("diff must not be fetched"))

    engine = ReviewEngine.__new__(ReviewEngine)
    engine.provider = provider
    engine.dry_run = False
    engine.bot_name = "mira"
    engine.config = MagicMock()
    engine.config.review.auto_resolve_conversations = False

    result = await ReviewEngine.review_pr(engine, "https://github.com/o/r/pull/1")

    assert isinstance(result, ReviewResult)
    assert result.skipped_reason is not None
    assert "merge conflicts" in result.skipped_reason
    assert result.comments == []
    provider.post_comment.assert_awaited()
    provider.get_pr_diff.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_pr_continues_when_mergeable_state_unknown():
    """Empty mergeable_state means GitHub is still computing — do not skip."""
    provider = MagicMock()
    provider.get_pr_info = AsyncMock(
        return_value=PRInfo(
            title="t",
            description="",
            base_branch="main",
            head_branch="feat",
            url="https://github.com/o/r/pull/2",
            number=2,
            owner="o",
            repo="r",
            mergeable_state="",
        )
    )
    # Short-circuit the rest of the pipeline after the dirty check.
    provider.get_pr_diff = AsyncMock(return_value="")
    provider.get_all_bot_threads = AsyncMock(return_value=[])

    engine = ReviewEngine.__new__(ReviewEngine)
    engine.provider = provider
    engine.dry_run = True
    engine.bot_name = "mira"
    engine.config = MagicMock()
    engine.config.review.auto_resolve_conversations = False
    engine.config.review.blast_radius = False
    engine.llm = MagicMock()

    # May proceed into review; only assert we did not take the dirty early-return
    # without fetching the diff.
    try:
        await ReviewEngine.review_pr(engine, "https://github.com/o/r/pull/2")
    except Exception:
        # Pipeline may fail later without full engine wiring; that is OK.
        pass
    provider.get_pr_diff.assert_awaited()
