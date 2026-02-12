"""Tests for review engine."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.config import MiraConfig
from mira.core.engine import ReviewEngine
from mira.llm.provider import LLMProvider
from mira.models import PRInfo


@pytest.fixture
def mock_llm(sample_llm_response_text: str) -> LLMProvider:
    llm = MagicMock(spec=LLMProvider)
    llm.complete = AsyncMock(return_value=sample_llm_response_text)
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
    return provider


class TestReviewEngine:
    @pytest.mark.asyncio
    async def test_review_diff(self, mock_llm: LLMProvider, sample_diff_text: str):
        engine = ReviewEngine(config=MiraConfig(), llm=mock_llm)
        result = await engine.review_diff(sample_diff_text)

        assert result.reviewed_files > 0
        assert result.summary != ""
        mock_llm.complete.assert_called_once()

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

        call_count = 0

        async def _side_effect(messages, json_mode=True):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return good_response
            # Second call returns garbage that will fail parsing
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
