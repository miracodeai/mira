"""Tests for the PR description summary block composition (append/replace)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from mira.config import MiraConfig
from mira.core.engine import ReviewEngine, compose_pr_description
from mira.core.passes import generate_pr_summary
from mira.llm.provider import LLMProvider
from mira.models import (
    PR_SUMMARY_END,
    PR_SUMMARY_START,
    FileChangeType,
    PRInfo,
    WalkthroughFileEntry,
    WalkthroughResult,
)


def _block(content: str = "## Summary by Mira\n\n- bullet") -> str:
    return f"{PR_SUMMARY_START}\n{content}\n{PR_SUMMARY_END}"


class TestComposePrDescription:
    def test_append_empty_body(self) -> None:
        block = _block()
        assert compose_pr_description("", block, "append") == block

    def test_append_empty_whitespace_body(self) -> None:
        block = _block()
        assert compose_pr_description("   \n  ", block, "append") == block

    def test_append_preserves_author_text(self) -> None:
        block = _block()
        author = "My PR description text."
        out = compose_pr_description(author, block, "append")
        assert out.startswith(author)
        assert block in out
        assert out.index(author) < out.index(PR_SUMMARY_START)
        assert "\n\n" in out

    def test_append_idempotent_replaces_existing_block(self) -> None:
        old = _block("old bullets")
        new = _block("new bullets")
        author = "Author text."
        current = f"{author}\n\n{old}"
        out = compose_pr_description(current, new, "append")
        assert "old bullets" not in out
        assert out.count(PR_SUMMARY_START) == 1
        assert out.count(PR_SUMMARY_END) == 1
        assert out.startswith(author)
        assert "new bullets" in out

    def test_append_replaces_block_and_keeps_trailing_author_text(self) -> None:
        old = _block("old bullets")
        new = _block("new bullets")
        trailing = "Trailing author note."
        current = f"Author intro.\n\n{old}\n\n{trailing}"
        out = compose_pr_description(current, new, "append")
        assert "old bullets" not in out
        assert out.count(PR_SUMMARY_START) == 1
        assert "Author intro." in out
        assert out.endswith(trailing)

    def test_append_malformed_lone_start_marker_appends(self) -> None:
        # A lone start marker without an end marker is a malformed edge case;
        # treat as no existing block and append after it.
        new = _block("new bullets")
        current = f"Author text.\n{PR_SUMMARY_START}"
        out = compose_pr_description(current, new, "append")
        assert new in out
        assert out.startswith("Author text.")

    def test_append_malformed_lone_end_marker_appends(self) -> None:
        # A lone end marker (no start) must not be treated as the block
        # terminator — append the block after the author content instead.
        new = _block("new bullets")
        current = f"Author text.\n{PR_SUMMARY_END}"
        out = compose_pr_description(current, new, "append")
        assert out.count(PR_SUMMARY_START) == 1
        assert out.count(PR_SUMMARY_END) == 2  # author's orphan + new block's
        assert out.index(PR_SUMMARY_START) < out.index(PR_SUMMARY_END, out.index(PR_SUMMARY_START))
        assert new in out
        assert out.startswith("Author text.")

    def test_append_malformed_lone_end_marker_before_block_is_idempotent(self) -> None:
        # Re-review scenario: author body contains an orphan END marker
        # BEFORE a previously appended Mira block. The block must be replaced,
        # not duplicated.
        old = _block("old bullets")
        new = _block("new bullets")
        current = f"Author text.\n{PR_SUMMARY_END}\n\n{old}"
        out = compose_pr_description(current, new, "append")
        assert out.count(PR_SUMMARY_START) == 1
        assert out.count("new bullets") == 1
        assert "old bullets" not in out
        assert out.startswith("Author text.")
        # Running the review a second time must not change the result.
        again = compose_pr_description(out, _block("second bullets"), "append")
        assert again.count(PR_SUMMARY_START) == 1
        assert "second bullets" in again and "new bullets" not in again

    def test_replace_ignores_author_text(self) -> None:
        block = _block()
        author = "My PR description text."
        assert compose_pr_description(author, block, "replace") == block

    def test_replace_with_existing_block_returns_only_block(self) -> None:
        old = _block("old bullets")
        new = _block("new bullets")
        current = f"Author text.\n\n{old}"
        assert compose_pr_description(current, new, "replace") == new


# ---------------------------------------------------------------------------
# generate_pr_summary
# ---------------------------------------------------------------------------

_WALKTHROUGH = WalkthroughResult(
    summary="Adds a login page and fixes a crash in the parser.",
    file_changes=[
        WalkthroughFileEntry(
            path="src/login.py",
            change_type=FileChangeType.ADDED,
            description="New login endpoint",
            group="Auth",
        ),
        WalkthroughFileEntry(
            path="src/parser.py",
            change_type=FileChangeType.MODIFIED,
            description="Fix null handling",
            group="Core",
        ),
    ],
)


def _mock_llm() -> SimpleNamespace:
    """Mock main-tier LLM; its usage dict is read by the engine."""
    llm = SimpleNamespace()
    llm.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    return llm


def _indexing_llm(text: str = "- **New Features**: added login") -> SimpleNamespace:
    indexing = SimpleNamespace()
    indexing.complete = AsyncMock(return_value=text)
    return indexing


class TestGeneratePrSummary:
    @pytest.mark.parametrize(
        "walkthrough",
        [
            None,
            WalkthroughResult(),  # empty summary
        ],
        ids=["no-walkthrough", "empty-summary"],
    )
    @pytest.mark.asyncio
    async def test_no_walkthrough_content_skips_llm(
        self, walkthrough: WalkthroughResult | None
    ) -> None:
        """No walkthrough (or empty summary) → early return: no LLM call, no block."""
        indexing = _indexing_llm()
        result = await generate_pr_summary(
            _mock_llm(), walkthrough, "PR title", "PR description", indexing_llm=indexing
        )
        assert result == ""
        indexing.complete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_strips_whitespace_from_llm_output(self) -> None:
        indexing = _indexing_llm("  - **Bug Fixes**: fixed crash\n\n")
        result = await generate_pr_summary(
            _mock_llm(), _WALKTHROUGH, "PR title", "", indexing_llm=indexing
        )
        assert result == "- **Bug Fixes**: fixed crash"
        indexing.complete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_llm_failure_returns_empty_without_raising(self) -> None:
        indexing = _indexing_llm()
        indexing.complete = AsyncMock(side_effect=RuntimeError("upstream 500"))
        result = await generate_pr_summary(
            _mock_llm(), _WALKTHROUGH, "PR title", "", indexing_llm=indexing
        )
        assert result == ""


# ---------------------------------------------------------------------------
# review_pr posting gate
# ---------------------------------------------------------------------------

_DIFF = (
    "diff --git a/src/utils.py b/src/utils.py\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/src/utils.py\n"
    "@@ -0,0 +1,3 @@\n"
    "+import os\n"
    "+x = 1\n"
    "+y = 2\n"
)

_REVIEW_RESPONSE = '{"comments": [], "summary": "All good!", "metadata": {"reviewed_files": 1}}'

_WALKTHROUGH_RESPONSE = (
    '{"summary": "Adds utils.", '
    '"change_groups": [{"label": "Core", "files": '
    '[{"path": "src/utils.py", "change_type": "added", "description": "New utils"}]}], '
    '"sequence_diagram": null}'
)


def _make_pr_provider() -> AsyncMock:
    """Provider mock for a full review_pr run, incl. description read/write."""
    provider = AsyncMock()
    provider.get_pr_info.return_value = PRInfo(
        title="Test PR",
        description="Author description.",
        base_branch="main",
        head_branch="feature",
        url="https://github.com/test/repo/pull/1",
        number=1,
        owner="test",
        repo="repo",
    )
    provider.get_pr_diff.return_value = _DIFF
    provider.find_bot_comment = AsyncMock(return_value=None)
    provider.get_unresolved_bot_threads = AsyncMock(return_value=[])
    provider.get_all_bot_threads = AsyncMock(return_value=[])
    provider.get_pr_description = AsyncMock(return_value="Author description.")
    return provider


def _make_engine(
    pr_summary: str = "append",
    *,
    dry_run: bool = False,
    walkthrough: bool = True,
    provider: AsyncMock | None = None,
    indexing_llm: MagicMock | None = None,
) -> tuple[ReviewEngine, MagicMock]:
    """Build an engine + main-tier LLM mock for the pr_summary posting gate."""
    config = MiraConfig()
    config.review.pr_summary = pr_summary
    config.review.walkthrough = walkthrough
    llm = MagicMock(spec=LLMProvider)
    llm.walkthrough = AsyncMock(return_value=_WALKTHROUGH_RESPONSE)
    llm.review = AsyncMock(return_value=_REVIEW_RESPONSE)
    llm.complete = AsyncMock(return_value="Summary prose.")
    llm.count_tokens = MagicMock(return_value=100)
    llm.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    if provider is None:
        provider = _make_pr_provider()
    return ReviewEngine(
        config=config,
        llm=llm,
        provider=provider,
        bot_name="mira",
        dry_run=dry_run,
        indexing_llm=indexing_llm or _indexing_llm(),
    ), llm


class TestReviewPrSummaryPostingGate:
    """review_pr writes the marked summary block only when pr_summary is
    enabled, it's not a dry run, and walkthrough content exists — and the
    indexing LLM is never called on paths where the result can't be posted."""

    @pytest.mark.asyncio
    async def test_posts_when_gate_conditions_met(self) -> None:
        provider = _make_pr_provider()
        engine, _ = _make_engine("append", provider=provider)
        await engine.review_pr("https://github.com/test/repo/pull/1")

        provider.get_pr_description.assert_awaited_once()
        provider.update_pr_description.assert_awaited_once()
        new_body = provider.update_pr_description.call_args.args[1]
        assert PR_SUMMARY_START in new_body and PR_SUMMARY_END in new_body
        assert "Author description." in new_body  # append preserves the author text
        assert new_body.index("Author description.") < new_body.index(PR_SUMMARY_START)

    @pytest.mark.asyncio
    async def test_replace_mode_sets_body_to_block_only(self) -> None:
        provider = _make_pr_provider()
        engine, _ = _make_engine("replace", provider=provider)
        await engine.review_pr("https://github.com/test/repo/pull/1")

        provider.update_pr_description.assert_awaited_once()
        new_body = provider.update_pr_description.call_args.args[1]
        assert new_body.startswith(PR_SUMMARY_START)
        assert new_body.endswith(PR_SUMMARY_END)
        assert "## Summary by Mira" in new_body
        assert "Author description." not in new_body

    @pytest.mark.asyncio
    async def test_disabled_leaves_description_untouched(self) -> None:
        provider = _make_pr_provider()
        engine, _ = _make_engine("disable", provider=provider)
        await engine.review_pr("https://github.com/test/repo/pull/1")

        provider.get_pr_description.assert_not_awaited()
        provider.update_pr_description.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dry_run_does_not_generate_or_post(self) -> None:
        provider = _make_pr_provider()
        indexing_llm = _indexing_llm()
        engine, _ = _make_engine(
            "append", dry_run=True, provider=provider, indexing_llm=indexing_llm
        )
        result = await engine.review_pr("https://github.com/test/repo/pull/1")

        assert result.pr_summary_block == ""
        indexing_llm.complete.assert_not_awaited()
        provider.get_pr_description.assert_not_awaited()
        provider.update_pr_description.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_walkthrough_content_leaves_description_untouched(self) -> None:
        provider = _make_pr_provider()
        indexing_llm = _indexing_llm()
        engine, _ = _make_engine(
            "append", walkthrough=False, provider=provider, indexing_llm=indexing_llm
        )
        result = await engine.review_pr("https://github.com/test/repo/pull/1")

        assert result.pr_summary_block == ""
        indexing_llm.complete.assert_not_awaited()
        provider.get_pr_description.assert_not_awaited()
        provider.update_pr_description.assert_not_awaited()
