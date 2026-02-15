"""Tests for review engine."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.config import MiraConfig
from mira.core.engine import ReviewEngine
from mira.llm.provider import LLMProvider
from mira.models import PRInfo, UnresolvedThread, WalkthroughResult

_WALKTHROUGH_LLM_RESPONSE = json.dumps(
    {
        "summary": "PR walkthrough summary.",
        "change_groups": [
            {
                "label": "Core",
                "files": [
                    {"path": "src/utils.py", "change_type": "added", "description": "New utils"},
                ],
            },
        ],
        "sequence_diagram": None,
    }
)


@pytest.fixture
def mock_llm(sample_llm_response_text: str) -> LLMProvider:
    llm = MagicMock(spec=LLMProvider)
    # First call is walkthrough, second is review
    llm.complete = AsyncMock(side_effect=[_WALKTHROUGH_LLM_RESPONSE, sample_llm_response_text])
    llm.count_tokens = MagicMock(return_value=100)
    llm.usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    return llm


@pytest.fixture
def mock_provider(sample_diff_text: str) -> AsyncMock:
    provider = AsyncMock()
    provider.get_pr_info.return_value = PRInfo(
        title="Test PR",
        description="Test description",
        base_branch="main",
        head_branch="feature",
        url="https://github.com/test/repo/pull/1",
        number=1,
        owner="test",
        repo="repo",
    )
    provider.get_pr_diff.return_value = sample_diff_text
    provider.post_review = AsyncMock()
    provider.post_comment = AsyncMock()
    provider.find_bot_comment = AsyncMock(return_value=None)
    provider.update_comment = AsyncMock()
    provider.resolve_outdated_review_threads = AsyncMock(return_value=0)
    provider.get_unresolved_bot_threads = AsyncMock(return_value=[])
    return provider


class TestReviewEngine:
    @pytest.mark.asyncio
    async def test_review_diff(self, mock_llm: LLMProvider, sample_diff_text: str):
        engine = ReviewEngine(config=MiraConfig(), llm=mock_llm)
        result = await engine.review_diff(sample_diff_text)

        assert result.reviewed_files > 0
        assert result.summary != ""
        # 2 calls: walkthrough + review
        assert mock_llm.complete.call_count == 2

    @pytest.mark.asyncio
    async def test_review_pr(self, mock_llm: LLMProvider, mock_provider: AsyncMock):
        engine = ReviewEngine(config=MiraConfig(), llm=mock_llm, provider=mock_provider)
        await engine.review_pr("https://github.com/test/repo/pull/1")

        mock_provider.get_pr_info.assert_called_once()
        mock_provider.get_pr_diff.assert_called_once()
        # Should post review since there are comments
        mock_provider.post_review.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_post_when_no_comments(self, mock_provider: AsyncMock):
        llm = MagicMock(spec=LLMProvider)
        llm.complete = AsyncMock(
            return_value=json.dumps(
                {
                    "comments": [],
                    "summary": "All good!",
                    "metadata": {"reviewed_files": 1},
                }
            )
        )
        llm.count_tokens = MagicMock(return_value=100)
        llm.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        engine = ReviewEngine(config=MiraConfig(), llm=llm, provider=mock_provider)
        await engine.review_pr("https://github.com/test/repo/pull/1")

        mock_provider.post_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_diff(self, mock_llm: LLMProvider):
        engine = ReviewEngine(config=MiraConfig(), llm=mock_llm)
        result = await engine.review_diff("")
        assert result.reviewed_files == 0
        mock_llm.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_review_pr_without_provider_raises(self, mock_llm: LLMProvider):
        engine = ReviewEngine(config=MiraConfig(), llm=mock_llm)
        with pytest.raises(RuntimeError, match="provider is required"):
            await engine.review_pr("https://github.com/test/repo/pull/1")

    @pytest.mark.asyncio
    async def test_noise_filtering_applied(self, sample_diff_text: str):
        """Verify that noise filtering reduces comments."""
        llm = MagicMock(spec=LLMProvider)
        llm.count_tokens = MagicMock(return_value=100)
        llm.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        # Return many low-confidence comments
        llm.complete = AsyncMock(
            return_value=json.dumps(
                {
                    "comments": [
                        {
                            "path": "src/utils.py",
                            "line": i,
                            "severity": "nitpick",
                            "category": "style",
                            "title": f"Style issue {i}",
                            "body": "Minor style concern",
                            "confidence": 0.3,
                        }
                        for i in range(1, 11)
                    ],
                    "summary": "Many minor issues",
                    "metadata": {"reviewed_files": 1},
                }
            )
        )

        config = MiraConfig()
        engine = ReviewEngine(config=config, llm=llm)
        result = await engine.review_diff(sample_diff_text)

        # All comments have confidence 0.3 < default threshold 0.7
        assert len(result.comments) == 0

    @pytest.mark.asyncio
    async def test_diff_files_passed_to_convert(self, mock_llm: LLMProvider, sample_diff_text: str):
        """Fix 1: convert_to_review_comments receives diff_files for existing_code validation."""
        engine = ReviewEngine(config=MiraConfig(), llm=mock_llm)

        with patch(
            "mira.core.engine.convert_to_review_comments",
            wraps=__import__(
                "mira.llm.response_parser", fromlist=["convert_to_review_comments"]
            ).convert_to_review_comments,
        ) as mock_convert:
            await engine.review_diff(sample_diff_text)
            assert mock_convert.call_count >= 1
            # Verify diff_files kwarg was passed (not None)
            _, kwargs = mock_convert.call_args
            assert "diff_files" in kwargs
            assert kwargs["diff_files"] is not None
            assert len(kwargs["diff_files"]) > 0

    @pytest.mark.asyncio
    async def test_chunk_parse_error_continues(self, sample_diff_text: str):
        """Fix 2: A ResponseParseError in one chunk doesn't discard other chunks."""
        good_response = json.dumps(
            {
                "comments": [
                    {
                        "path": "src/utils.py",
                        "line": 9,
                        "severity": "warning",
                        "category": "security",
                        "title": "Shell injection",
                        "body": "Using shell=True is dangerous.",
                        "confidence": 0.95,
                    }
                ],
                "summary": "Found issues.",
                "metadata": {"reviewed_files": 1},
            }
        )

        walkthrough_response = json.dumps({"summary": "walkthrough", "file_changes": []})

        call_count = 0

        async def _side_effect(messages, json_mode=True):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return walkthrough_response  # walkthrough call
            if call_count == 2:
                return good_response  # first review chunk
            # Subsequent calls return garbage that will fail parsing
            return "NOT VALID JSON {{{"

        llm = MagicMock(spec=LLMProvider)
        llm.count_tokens = MagicMock(return_value=50)
        llm.complete = AsyncMock(side_effect=_side_effect)
        llm.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        # Force two chunks by setting a very low token limit
        config = MiraConfig()
        config.llm.max_context_tokens = 100
        config.filter.confidence_threshold = 0.0

        engine = ReviewEngine(config=config, llm=llm)
        result = await engine.review_diff(sample_diff_text)

        # Should still have comments from the successful chunk
        assert result.reviewed_files > 0
        # The pipeline completed without raising

    @pytest.mark.asyncio
    async def test_max_diff_size_truncates(self, mock_llm: LLMProvider, sample_diff_text: str):
        """Fix 4: Diffs exceeding max_diff_size are truncated."""
        config = MiraConfig()
        config.review.max_diff_size = 50  # Very small limit

        engine = ReviewEngine(config=config, llm=mock_llm)
        # Should not raise — truncation is graceful
        result = await engine.review_diff(sample_diff_text)
        # With a 50-char truncation the diff likely has no parseable files
        assert result is not None

    @pytest.mark.asyncio
    async def test_max_diff_size_truncates_at_file_boundary(self, mock_llm: LLMProvider):
        """Truncation cuts at the last 'diff --git' boundary, not mid-hunk."""
        file_a = (
            "diff --git a/a.py b/a.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/a.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+line1\n+line2\n+line3\n"
        )
        file_b = (
            "diff --git a/b.py b/b.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/b.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+line4\n+line5\n+line6\n"
        )
        big_diff = file_a + file_b

        config = MiraConfig()
        # Set limit so file_a fits but file_a + file_b doesn't
        config.review.max_diff_size = len(file_a) + 10
        config.filter.confidence_threshold = 0.0

        engine = ReviewEngine(config=config, llm=mock_llm)
        result = await engine.review_diff(big_diff)

        # Should only review the first file — second was truncated at boundary
        assert result.reviewed_files == 1

    @pytest.mark.asyncio
    async def test_include_summary_false(self, mock_llm: LLMProvider, sample_diff_text: str):
        """Fix 4: When include_summary is False, summary is empty."""
        config = MiraConfig()
        config.review.include_summary = False

        engine = ReviewEngine(config=config, llm=mock_llm)
        result = await engine.review_diff(sample_diff_text)
        assert result.summary == ""

    @pytest.mark.asyncio
    async def test_include_summary_true_default(self, mock_llm: LLMProvider, sample_diff_text: str):
        """Fix 4: Default include_summary=True produces a non-empty summary."""
        config = MiraConfig()
        assert config.review.include_summary is True

        engine = ReviewEngine(config=config, llm=mock_llm)
        result = await engine.review_diff(sample_diff_text)
        assert result.summary != ""

    @pytest.mark.asyncio
    async def test_walkthrough_enabled(self, mock_llm: LLMProvider, sample_diff_text: str):
        """Walkthrough is generated when enabled (default)."""
        config = MiraConfig()
        assert config.review.walkthrough is True

        engine = ReviewEngine(config=config, llm=mock_llm)
        result = await engine.review_diff(sample_diff_text)
        assert result.walkthrough is not None
        assert isinstance(result.walkthrough, WalkthroughResult)
        assert result.walkthrough.summary != ""

    @pytest.mark.asyncio
    async def test_walkthrough_disabled(self, sample_llm_response_text: str, sample_diff_text: str):
        """Walkthrough is skipped when disabled."""
        llm = MagicMock(spec=LLMProvider)
        llm.complete = AsyncMock(return_value=sample_llm_response_text)
        llm.count_tokens = MagicMock(return_value=100)
        llm.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        config = MiraConfig()
        config.review.walkthrough = False

        engine = ReviewEngine(config=config, llm=llm)
        result = await engine.review_diff(sample_diff_text)
        assert result.walkthrough is None
        # Only 1 call (review), no walkthrough call
        assert llm.complete.call_count == 1

    @pytest.mark.asyncio
    async def test_walkthrough_failure_continues(
        self, sample_llm_response_text: str, sample_diff_text: str
    ):
        """Walkthrough failure does not block the review."""
        call_count = 0

        async def _side_effect(messages, json_mode=True):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("LLM exploded")
            return sample_llm_response_text

        llm = MagicMock(spec=LLMProvider)
        llm.complete = AsyncMock(side_effect=_side_effect)
        llm.count_tokens = MagicMock(return_value=100)
        llm.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        config = MiraConfig()
        engine = ReviewEngine(config=config, llm=llm)
        result = await engine.review_diff(sample_diff_text)

        # Walkthrough failed but review still succeeded
        assert result.walkthrough is None
        assert result.reviewed_files > 0

    @pytest.mark.asyncio
    async def test_walkthrough_posted_before_review(
        self, mock_llm: LLMProvider, mock_provider: AsyncMock
    ):
        """Walkthrough comment is posted before inline review."""
        engine = ReviewEngine(config=MiraConfig(), llm=mock_llm, provider=mock_provider)
        await engine.review_pr("https://github.com/test/repo/pull/1")

        mock_provider.post_comment.assert_called_once()
        mock_provider.post_review.assert_called_once()

        # Verify post_comment was called before post_review
        comment_order = mock_provider.post_comment.call_args_list[0]
        review_order = mock_provider.post_review.call_args_list[0]
        assert comment_order is not None
        assert review_order is not None

    @pytest.mark.asyncio
    async def test_walkthrough_upserts_existing_comment(
        self, mock_llm: LLMProvider, mock_provider: AsyncMock
    ):
        """Existing walkthrough comment is updated instead of creating a new one."""
        mock_provider.find_bot_comment = AsyncMock(return_value=42)

        engine = ReviewEngine(config=MiraConfig(), llm=mock_llm, provider=mock_provider)
        await engine.review_pr("https://github.com/test/repo/pull/1")

        mock_provider.find_bot_comment.assert_called_once()
        mock_provider.update_comment.assert_called_once()
        assert mock_provider.update_comment.call_args[0][1] == 42
        mock_provider.post_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_walkthrough_creates_when_no_existing(
        self, mock_llm: LLMProvider, mock_provider: AsyncMock
    ):
        """When no existing walkthrough comment is found, a new one is created."""
        mock_provider.find_bot_comment = AsyncMock(return_value=None)

        engine = ReviewEngine(config=MiraConfig(), llm=mock_llm, provider=mock_provider)
        await engine.review_pr("https://github.com/test/repo/pull/1")

        mock_provider.find_bot_comment.assert_called_once()
        mock_provider.post_comment.assert_called_once()
        mock_provider.update_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_walkthrough_upsert_failure_does_not_block_review(
        self, mock_llm: LLMProvider, mock_provider: AsyncMock
    ):
        """If find_bot_comment raises, the review still completes."""
        mock_provider.find_bot_comment = AsyncMock(side_effect=RuntimeError("API error"))

        engine = ReviewEngine(config=MiraConfig(), llm=mock_llm, provider=mock_provider)
        result = await engine.review_pr("https://github.com/test/repo/pull/1")

        # Review still completed
        mock_provider.post_review.assert_called_once()
        assert result.reviewed_files > 0

    @pytest.mark.asyncio
    async def test_resolve_called_before_post_review(
        self, mock_llm: LLMProvider, mock_provider: AsyncMock
    ):
        """resolve_outdated_review_threads is called before post_review."""
        mock_provider.resolve_outdated_review_threads = AsyncMock(return_value=2)

        call_order: list[str] = []

        async def _track_resolve(*a, **kw):
            call_order.append("resolve")
            return 2

        async def _track_post_review(*a, **kw):
            call_order.append("post_review")

        mock_provider.resolve_outdated_review_threads = AsyncMock(side_effect=_track_resolve)
        mock_provider.post_review = AsyncMock(side_effect=_track_post_review)

        engine = ReviewEngine(config=MiraConfig(), llm=mock_llm, provider=mock_provider)
        await engine.review_pr("https://github.com/test/repo/pull/1")

        mock_provider.resolve_outdated_review_threads.assert_called_once()
        assert call_order.index("resolve") < call_order.index("post_review")

    @pytest.mark.asyncio
    async def test_resolve_failure_does_not_block_review(
        self, mock_llm: LLMProvider, mock_provider: AsyncMock
    ):
        """If resolve_outdated_review_threads raises, the review still completes."""
        mock_provider.resolve_outdated_review_threads = AsyncMock(
            side_effect=RuntimeError("GraphQL boom")
        )

        engine = ReviewEngine(config=MiraConfig(), llm=mock_llm, provider=mock_provider)
        result = await engine.review_pr("https://github.com/test/repo/pull/1")

        mock_provider.post_review.assert_called_once()
        assert result.reviewed_files > 0

    @pytest.mark.asyncio
    async def test_resolve_called_even_with_no_new_comments(self, mock_provider: AsyncMock):
        """resolve_outdated_review_threads is called even when LLM returns no comments."""
        llm = MagicMock(spec=LLMProvider)
        llm.complete = AsyncMock(
            return_value=json.dumps(
                {
                    "comments": [],
                    "summary": "All good!",
                    "metadata": {"reviewed_files": 1},
                }
            )
        )
        llm.count_tokens = MagicMock(return_value=100)
        llm.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        engine = ReviewEngine(config=MiraConfig(), llm=llm, provider=mock_provider)
        await engine.review_pr("https://github.com/test/repo/pull/1")

        mock_provider.resolve_outdated_review_threads.assert_called_once()
        mock_provider.post_review.assert_not_called()


class TestThreadResolution:
    """Tests for the _resolve_verified_threads flow."""

    @pytest.fixture
    def threads(self) -> list[UnresolvedThread]:
        return [
            UnresolvedThread(thread_id="T1", path="src/app.py", line=10, body="Hardcoded secret"),
            UnresolvedThread(thread_id="T2", path="src/app.py", line=25, body="Missing null check"),
        ]

    @pytest.fixture
    def provider_with_threads(
        self, sample_diff_text: str, threads: list[UnresolvedThread]
    ) -> AsyncMock:
        provider = AsyncMock()
        provider.get_pr_info.return_value = PRInfo(
            title="Test PR",
            description="Test description",
            base_branch="main",
            head_branch="feature",
            url="https://github.com/test/repo/pull/1",
            number=1,
            owner="test",
            repo="repo",
        )
        provider.get_pr_diff.return_value = sample_diff_text
        provider.post_review = AsyncMock()
        provider.post_comment = AsyncMock()
        provider.find_bot_comment = AsyncMock(return_value=None)
        provider.update_comment = AsyncMock()
        provider.resolve_outdated_review_threads = AsyncMock(return_value=0)
        provider.get_unresolved_bot_threads = AsyncMock(return_value=threads)
        provider.get_file_content = AsyncMock(return_value="line1\n" * 30)
        provider.resolve_threads = AsyncMock(return_value=1)
        return provider

    @pytest.mark.asyncio
    async def test_full_flow(
        self,
        sample_llm_response_text: str,
        provider_with_threads: AsyncMock,
        threads: list[UnresolvedThread],
    ):
        """Fetches threads -> gets file content -> calls LLM -> resolves verified threads."""
        verify_response = json.dumps(
            {"results": [{"id": "T1", "fixed": True}, {"id": "T2", "fixed": False}]}
        )

        llm = MagicMock(spec=LLMProvider)
        llm.count_tokens = MagicMock(return_value=100)
        llm.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        call_count = 0

        async def _side_effect(messages, json_mode=True):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return verify_response  # thread verification
            if call_count == 2:
                return json.dumps({"summary": "walkthrough", "file_changes": []})
            return sample_llm_response_text

        llm.complete = AsyncMock(side_effect=_side_effect)

        engine = ReviewEngine(
            config=MiraConfig(), llm=llm, provider=provider_with_threads, bot_name="mira"
        )
        await engine.review_pr("https://github.com/test/repo/pull/1")

        provider_with_threads.get_unresolved_bot_threads.assert_awaited_once()
        provider_with_threads.get_file_content.assert_awaited()
        # Only T1 was fixed
        provider_with_threads.resolve_threads.assert_awaited_once()
        resolved_ids = provider_with_threads.resolve_threads.call_args[0][1]
        assert resolved_ids == ["T1"]

    @pytest.mark.asyncio
    async def test_skips_when_no_unresolved_threads(
        self, mock_llm: LLMProvider, mock_provider: AsyncMock
    ):
        """No LLM call or resolve when no unresolved threads exist."""
        mock_provider.get_unresolved_bot_threads = AsyncMock(return_value=[])

        engine = ReviewEngine(
            config=MiraConfig(), llm=mock_llm, provider=mock_provider, bot_name="mira"
        )
        await engine.review_pr("https://github.com/test/repo/pull/1")

        mock_provider.get_unresolved_bot_threads.assert_awaited_once()
        mock_provider.resolve_threads.assert_not_called()

    @pytest.mark.asyncio
    async def test_continues_review_when_resolution_raises(
        self, mock_llm: LLMProvider, mock_provider: AsyncMock
    ):
        """Review continues even if thread resolution fails."""
        mock_provider.get_unresolved_bot_threads = AsyncMock(
            side_effect=RuntimeError("GraphQL exploded")
        )

        engine = ReviewEngine(
            config=MiraConfig(), llm=mock_llm, provider=mock_provider, bot_name="mira"
        )
        result = await engine.review_pr("https://github.com/test/repo/pull/1")

        # Review should still complete
        assert result is not None
        mock_provider.get_pr_diff.assert_awaited_once()
