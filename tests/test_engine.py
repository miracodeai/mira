"""Tests for review engine."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.config import MiraConfig
from mira.core.engine import (
    ReviewEngine,
    _clamp_confidence_to_findings,
    _drop_orphan_key_issues,
    _security_relevant_files,
)
from mira.core.threads import _extract_sections
from mira.llm.provider import LLMProvider
from mira.models import (
    FileChangeType,
    FileDiff,
    KeyIssue,
    PRInfo,
    ReviewComment,
    Severity,
    UnresolvedThread,
    WalkthroughConfidenceScore,
    WalkthroughResult,
)


def _empty_filediff(path: str) -> FileDiff:
    return FileDiff(
        path=path,
        change_type=FileChangeType.MODIFIED,
        language="",
        added_lines=0,
        deleted_lines=0,
        hunks=[],
    )


class TestSecurityFileFilter:
    """The security pass narrows its input so the LLM isn't drowned in non-code files."""

    def test_drops_migrations_specs_styles_lockfiles(self):
        files = [
            _empty_filediff("app/controllers/embed_controller.rb"),
            _empty_filediff("app/assets/javascripts/embed.js"),
            _empty_filediff("db/migrate/20131217174004_create_topic_embeds.rb"),
            _empty_filediff("spec/controllers/embed_controller_spec.rb"),
            _empty_filediff("app/assets/stylesheets/embed.css.scss"),
            _empty_filediff("Gemfile.lock"),
            _empty_filediff("CHANGELOG.md"),
        ]
        keep = [k.path for k in _security_relevant_files(files)]
        assert "app/controllers/embed_controller.rb" in keep
        assert "app/assets/javascripts/embed.js" in keep
        assert all("/migrate/" not in p for p in keep)
        assert all("spec/" not in p for p in keep)
        assert all(not p.endswith(".scss") for p in keep)
        assert all(not p.endswith(".lock") for p in keep)
        assert all(not p.endswith(".md") for p in keep)

    def test_drops_test_files_by_suffix(self):
        files = [
            _empty_filediff("src/auth.go"),
            _empty_filediff("src/auth_test.go"),
            _empty_filediff("src/auth.test.ts"),
            _empty_filediff("src/login.spec.tsx"),
        ]
        keep = [k.path for k in _security_relevant_files(files)]
        assert keep == ["src/auth.go"]

    def test_keeps_app_code_unchanged(self):
        files = [
            _empty_filediff("src/middleware.py"),
            _empty_filediff("api/views.py"),
            _empty_filediff("frontend/login.tsx"),
        ]
        keep = [k.path for k in _security_relevant_files(files)]
        assert keep == ["src/middleware.py", "api/views.py", "frontend/login.tsx"]


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
    # Tool-calling methods used by engine
    llm.walkthrough = AsyncMock(return_value=_WALKTHROUGH_LLM_RESPONSE)
    llm.review = AsyncMock(return_value=sample_llm_response_text)
    # Legacy JSON-mode method used by verify_fixes, summarization
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
    provider.post_comment = AsyncMock()
    provider.find_bot_comment = AsyncMock(return_value=None)
    provider.update_comment = AsyncMock()
    provider.get_unresolved_bot_threads = AsyncMock(return_value=[])
    return provider


class TestReviewEngine:
    @pytest.mark.asyncio
    async def test_review_diff(self, mock_llm: LLMProvider, sample_diff_text: str):
        engine = ReviewEngine(config=MiraConfig(), llm=mock_llm)
        result = await engine.review_diff(sample_diff_text)

        assert result.reviewed_files > 0
        assert result.summary != ""
        # walkthrough + review via tool calling
        mock_llm.walkthrough.assert_called_once()
        mock_llm.review.assert_called_once()

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
        no_comments = json.dumps(
            {
                "comments": [],
                "summary": "All good!",
                "metadata": {"reviewed_files": 1},
            }
        )
        llm.review = AsyncMock(return_value=no_comments)
        llm.walkthrough = AsyncMock(return_value=_WALKTHROUGH_LLM_RESPONSE)
        llm.complete = AsyncMock(return_value=no_comments)
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
        mock_llm.review.assert_not_called()

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
        low_confidence_response = json.dumps(
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
        llm.review = AsyncMock(return_value=low_confidence_response)
        llm.walkthrough = AsyncMock(return_value=_WALKTHROUGH_LLM_RESPONSE)
        llm.complete = AsyncMock(return_value=low_confidence_response)

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

        review_call_count = 0

        async def _review_side_effect(messages):
            nonlocal review_call_count
            review_call_count += 1
            if review_call_count == 1:
                return good_response  # first review chunk
            # Subsequent calls return garbage that will fail parsing
            return "NOT VALID JSON {{{"

        llm = MagicMock(spec=LLMProvider)
        llm.count_tokens = MagicMock(return_value=50)
        llm.walkthrough = AsyncMock(
            return_value=json.dumps({"summary": "walkthrough", "change_groups": []})
        )
        llm.review = AsyncMock(side_effect=_review_side_effect)
        llm.complete = AsyncMock(return_value=good_response)
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
    async def test_max_diff_size_skips_low_priority_files(self, mock_llm: LLMProvider):
        """When the diff exceeds the size cap, low-priority files get skipped
        (not silently truncated mid-hunk) and recorded in result.skipped_paths
        so the walkthrough banner can surface them."""
        # Build two files with substantial hunks so the per-hunk size matters.
        # Use a low priority + high priority pair so we can verify priority
        # determines who gets dropped.
        big_hunk = "+" + "x" * 500 + "\n"
        sensitive = (
            "diff --git a/src/auth/jwt.py b/src/auth/jwt.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n+++ b/src/auth/jwt.py\n"
            "@@ -0,0 +1,1 @@\n" + big_hunk
        )
        readme = (
            "diff --git a/README.md b/README.md\n"
            "new file mode 100644\n"
            "--- /dev/null\n+++ b/README.md\n"
            "@@ -0,0 +1,1 @@\n" + big_hunk
        )
        big_diff = sensitive + readme

        config = MiraConfig()
        # Cap small enough that only one file fits.
        config.review.max_diff_size = 600
        config.filter.confidence_threshold = 0.0

        engine = ReviewEngine(config=config, llm=mock_llm)
        result = await engine.review_diff(big_diff)

        # The auth file is sensitive → priority-ranked first → reviewed.
        # The README is low-priority → skipped.
        assert result.reviewed_files == 1
        assert "src/auth/jwt.py" in result.reviewed_paths
        assert "README.md" in result.skipped_paths

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
        llm.review = AsyncMock(return_value=sample_llm_response_text)
        llm.walkthrough = AsyncMock(return_value=_WALKTHROUGH_LLM_RESPONSE)
        llm.complete = AsyncMock(return_value=sample_llm_response_text)
        llm.count_tokens = MagicMock(return_value=100)
        llm.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        config = MiraConfig()
        config.review.walkthrough = False

        engine = ReviewEngine(config=config, llm=llm)
        result = await engine.review_diff(sample_diff_text)
        assert result.walkthrough is None
        # No walkthrough call
        llm.walkthrough.assert_not_called()

    @pytest.mark.asyncio
    async def test_walkthrough_failure_continues(
        self, sample_llm_response_text: str, sample_diff_text: str
    ):
        """Walkthrough failure does not block the review."""
        llm = MagicMock(spec=LLMProvider)
        llm.walkthrough = AsyncMock(side_effect=RuntimeError("LLM exploded"))
        llm.review = AsyncMock(return_value=sample_llm_response_text)
        llm.complete = AsyncMock(return_value=sample_llm_response_text)
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
        """Walkthrough placeholder + update happen before inline review posts."""
        engine = ReviewEngine(config=MiraConfig(), llm=mock_llm, provider=mock_provider)
        await engine.review_pr("https://github.com/test/repo/pull/1")

        # Placeholder post + final walkthrough post (find_bot_comment mocked to None)
        assert mock_provider.post_comment.call_count >= 1
        mock_provider.post_review.assert_called_once()

    def _comment(self, severity: Severity) -> ReviewComment:
        return ReviewComment(
            path="x.py",
            line=1,
            end_line=None,
            severity=severity,
            category="other",
            title="t",
            body="b",
            confidence=0.9,
        )

    def test_clamp_blocker_forces_score_two(self):
        wt = WalkthroughResult(
            confidence_score=WalkthroughConfidenceScore(
                score=5,
                label="Safe",
                reason="looks fine",
            ),
        )
        _clamp_confidence_to_findings(wt, [self._comment(Severity.BLOCKER)])
        assert wt.confidence_score.score == 2
        assert wt.confidence_score.label == "Do not merge"
        assert "1 blocker" in wt.confidence_score.reason

    def test_clamp_many_warnings_forces_score_three(self):
        wt = WalkthroughResult(
            confidence_score=WalkthroughConfidenceScore(
                score=5,
                label="Safe",
                reason="looks fine",
            ),
        )
        _clamp_confidence_to_findings(
            wt,
            [self._comment(Severity.WARNING) for _ in range(3)],
        )
        assert wt.confidence_score.score == 3
        assert wt.confidence_score.label == "Needs review"

    def test_clamp_does_not_raise_score(self):
        wt = WalkthroughResult(
            confidence_score=WalkthroughConfidenceScore(
                score=1,
                label="Major concerns",
                reason="existing",
            ),
        )
        _clamp_confidence_to_findings(wt, [])
        # No findings and LLM already scored low → leave as-is.
        assert wt.confidence_score.score == 1
        assert wt.confidence_score.label == "Major concerns"

    def test_clamp_blocker_beats_warnings(self):
        wt = WalkthroughResult(
            confidence_score=WalkthroughConfidenceScore(
                score=4,
                label="Safe with fixes",
                reason="r",
            ),
        )
        comments = [
            self._comment(Severity.BLOCKER),
            self._comment(Severity.WARNING),
            self._comment(Severity.WARNING),
            self._comment(Severity.WARNING),
        ]
        _clamp_confidence_to_findings(wt, comments)
        # Blocker rule wins — score should be 2, not 3.
        assert wt.confidence_score.score == 2
        assert "1 blocker" in wt.confidence_score.reason

    def test_clamp_no_op_when_findings_match(self):
        wt = WalkthroughResult(
            confidence_score=WalkthroughConfidenceScore(
                score=2,
                label="Major concerns",
                reason="original",
            ),
        )
        _clamp_confidence_to_findings(wt, [self._comment(Severity.BLOCKER)])
        # Already ≤ 2 → don't overwrite the LLM's more detailed reason.
        assert wt.confidence_score.score == 2
        assert wt.confidence_score.reason == "original"

    def test_clamp_no_confidence_score_noop(self):
        wt = WalkthroughResult(confidence_score=None)
        # Should not crash.
        _clamp_confidence_to_findings(wt, [self._comment(Severity.BLOCKER)])
        assert wt.confidence_score is None

    @pytest.mark.asyncio
    async def test_streaming_walkthrough_three_stages(
        self, mock_llm: LLMProvider, mock_provider: AsyncMock
    ):
        """End-to-end streaming flow: placeholder → in-progress walkthrough
        → final walkthrough + inline review, in that order."""
        # Simulate GitHub: first lookup returns None (no existing comment),
        # subsequent lookups return the newly-created placeholder ID.
        mock_provider.find_bot_comment = AsyncMock(side_effect=[None, 7, 7, 7, 7])

        engine = ReviewEngine(config=MiraConfig(), llm=mock_llm, provider=mock_provider)
        await engine.review_pr("https://github.com/test/repo/pull/1")

        # 1. One placeholder post.
        mock_provider.post_comment.assert_called_once()
        placeholder_body = mock_provider.post_comment.call_args[0][1]
        assert "Reviewing this PR" in placeholder_body
        assert "<!-- mira-walkthrough -->" in placeholder_body

        # 2. In-progress walkthrough update (the one triggered by the callback)
        #    and 3. final walkthrough update with stats.
        update_bodies = [c[0][2] for c in mock_provider.update_comment.call_args_list]
        assert len(update_bodies) >= 2

        # One of the updates must have "Code review in progress" (in-progress mode)
        in_progress_update = next(
            (b for b in update_bodies if "Code review in progress" in b),
            None,
        )
        assert in_progress_update is not None, "expected an in-progress walkthrough update"
        # The final update must contain stats (review comments count).
        final_update = update_bodies[-1]
        assert "Code review in progress" not in final_update
        assert "PR walkthrough summary" in final_update

        # 4. Inline comments post fires once, at the end.
        mock_provider.post_review.assert_called_once()

    @pytest.mark.asyncio
    async def test_walkthrough_upserts_existing_comment(
        self, mock_llm: LLMProvider, mock_provider: AsyncMock
    ):
        """Existing walkthrough comment is edited in place for both the
        placeholder and the final walkthrough — no new comment created."""
        mock_provider.find_bot_comment = AsyncMock(return_value=42)

        engine = ReviewEngine(config=MiraConfig(), llm=mock_llm, provider=mock_provider)
        await engine.review_pr("https://github.com/test/repo/pull/1")

        # Placeholder update + final walkthrough update = 2 edits on comment 42
        assert mock_provider.update_comment.call_count >= 2
        for call in mock_provider.update_comment.call_args_list:
            assert call[0][1] == 42
        mock_provider.post_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_walkthrough_creates_when_no_existing(
        self, mock_llm: LLMProvider, mock_provider: AsyncMock
    ):
        """When no walkthrough comment exists, the placeholder creates one and
        the final walkthrough updates it in place."""
        # Simulate realistic behaviour: initial lookup returns None, a subsequent
        # lookup (after placeholder post) finds the newly-created comment by ID.
        mock_provider.find_bot_comment = AsyncMock(side_effect=[None, 99, 99])

        engine = ReviewEngine(config=MiraConfig(), llm=mock_llm, provider=mock_provider)
        await engine.review_pr("https://github.com/test/repo/pull/1")

        # Exactly one new comment (the placeholder); rest are updates.
        mock_provider.post_comment.assert_called_once()
        assert mock_provider.update_comment.call_count >= 1

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
    async def test_walkthrough_posted_with_summary(
        self, mock_llm: LLMProvider, mock_provider: AsyncMock
    ):
        """Final walkthrough markdown contains the summary."""
        engine = ReviewEngine(config=MiraConfig(), llm=mock_llm, provider=mock_provider)
        await engine.review_pr("https://github.com/test/repo/pull/1")

        # Collect every comment body sent to GitHub, across posts and updates.
        bodies = [c[0][1] for c in mock_provider.post_comment.call_args_list]
        bodies.extend(c[0][2] for c in mock_provider.update_comment.call_args_list)
        combined = "\n".join(bodies)
        assert "## Mira PR Walkthrough" in combined
        assert "PR walkthrough summary." in combined

    @pytest.mark.asyncio
    async def test_walkthrough_omits_review_stats_when_no_comments(self, mock_provider: AsyncMock):
        """Walkthrough markdown omits review stats when there are no comments."""
        no_comments_response = json.dumps(
            {
                "comments": [],
                "summary": "All good!",
                "metadata": {"reviewed_files": 1},
            }
        )
        llm = MagicMock(spec=LLMProvider)
        llm.walkthrough = AsyncMock(return_value=_WALKTHROUGH_LLM_RESPONSE)
        llm.review = AsyncMock(return_value=no_comments_response)
        llm.complete = AsyncMock(return_value=no_comments_response)
        llm.count_tokens = MagicMock(return_value=100)
        llm.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        engine = ReviewEngine(config=MiraConfig(), llm=llm, provider=mock_provider)
        await engine.review_pr("https://github.com/test/repo/pull/1")

        # Walkthrough was posted (placeholder + final) but without review stats
        bodies = [c[0][1] for c in mock_provider.post_comment.call_args_list]
        bodies.extend(c[0][2] for c in mock_provider.update_comment.call_args_list)
        combined = "\n".join(bodies)
        assert "### Review Status" not in combined

    @pytest.mark.asyncio
    async def test_no_brute_force_resolve_of_outdated_threads(
        self, mock_llm: LLMProvider, mock_provider: AsyncMock
    ):
        """Outdated threads are NOT blindly resolved — only LLM-verified ones are."""
        engine = ReviewEngine(config=MiraConfig(), llm=mock_llm, provider=mock_provider)
        await engine.review_pr("https://github.com/test/repo/pull/1")

        mock_provider.resolve_outdated_review_threads.assert_not_called()

    @pytest.mark.asyncio
    async def test_parallel_chunks_share_base_existing(self, sample_diff_text: str):
        """All parallel chunks receive the same base existing_comments (no cross-chunk injection)."""
        chunk_response = json.dumps(
            {
                "comments": [
                    {
                        "path": "src/utils.py",
                        "line": 9,
                        "severity": "warning",
                        "category": "security",
                        "title": "Shell injection risk",
                        "body": "Avoid shell=True.",
                        "confidence": 0.95,
                    }
                ],
                "summary": "Issues found.",
                "metadata": {"reviewed_files": 1},
            }
        )

        llm = MagicMock(spec=LLMProvider)
        llm.count_tokens = MagicMock(return_value=50)
        llm.walkthrough = AsyncMock(return_value=_WALKTHROUGH_LLM_RESPONSE)
        llm.review = AsyncMock(return_value=chunk_response)
        llm.complete = AsyncMock(return_value=chunk_response)
        llm.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        config = MiraConfig()
        config.llm.max_context_tokens = 100  # Force multiple chunks
        config.filter.confidence_threshold = 0.0

        engine = ReviewEngine(config=config, llm=llm)

        with patch(
            "mira.core.engine.build_review_prompt",
            wraps=__import__(
                "mira.llm.prompts.review", fromlist=["build_review_prompt"]
            ).build_review_prompt,
        ) as mock_build:
            await engine.review_diff(sample_diff_text)

            # Need at least 2 review chunks
            assert mock_build.call_count >= 2, (
                f"Expected >=2 chunk calls, got {mock_build.call_count}"
            )

            # All chunks should get the same existing_comments (None — no real threads)
            for i, call in enumerate(mock_build.call_args_list):
                _, kwargs = call
                assert kwargs.get("existing_comments") is None, (
                    f"Chunk {i + 1} should not receive synthetic cross-chunk comments"
                )


class TestDryRun:
    """Tests for dry-run mode — full pipeline without write operations."""

    @pytest.mark.asyncio
    async def test_dry_run_skips_writes_but_runs_reads_and_llm(
        self,
        sample_llm_response_text: str,
    ):
        """Dry-run exercises the full pipeline (reads + LLM) but never posts to GitHub."""
        threads = [
            UnresolvedThread(thread_id="T1", path="src/app.py", line=10, body="Hardcoded secret"),
        ]

        verify_response = json.dumps({"results": [{"id": "T1", "fixed": True}]})

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
        provider.get_pr_diff.return_value = (
            "diff --git a/src/utils.py b/src/utils.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/src/utils.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+import os\n+x = 1\n+y = 2\n"
        )
        provider.get_unresolved_bot_threads = AsyncMock(return_value=threads)
        provider.get_file_content = AsyncMock(return_value="import os\nx = 1\ny = 2\n")
        provider.resolve_threads = AsyncMock(return_value=1)
        provider.post_review = AsyncMock()
        provider.post_comment = AsyncMock()
        provider.update_comment = AsyncMock()
        provider.find_bot_comment = AsyncMock(return_value=None)

        llm = MagicMock(spec=LLMProvider)
        llm.count_tokens = MagicMock(return_value=100)
        llm.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        llm.complete = AsyncMock(return_value=verify_response)
        llm.walkthrough = AsyncMock(
            return_value=json.dumps({"summary": "walkthrough", "change_groups": []})
        )
        llm.review = AsyncMock(return_value=sample_llm_response_text)

        engine = ReviewEngine(
            config=MiraConfig(), llm=llm, provider=provider, bot_name="mira", dry_run=True
        )
        result = await engine.review_pr("https://github.com/test/repo/pull/1")

        # Read operations should be called
        provider.get_pr_info.assert_awaited_once()
        provider.get_pr_diff.assert_awaited_once()
        provider.get_unresolved_bot_threads.assert_awaited_once()
        provider.get_file_content.assert_awaited()

        # LLM should be called (verify-fixes via complete, walkthrough + review)
        assert llm.complete.call_count >= 1

        # Write operations should NOT be called
        provider.resolve_threads.assert_not_called()
        provider.post_review.assert_not_called()
        provider.post_comment.assert_not_called()
        provider.update_comment.assert_not_called()

        # Result should still be populated
        assert result is not None


class TestExtractSections:
    """Tests for the _extract_sections helper."""

    def test_single_thread_extracts_window(self):
        lines = [f"line{i}" for i in range(200)]
        thread = UnresolvedThread(thread_id="T1", path="f.py", line=100, body="issue")
        result = _extract_sections(lines, [thread], context_lines=5)
        # Should contain lines around line 100 (0-indexed: 99)
        assert "line95" in result
        assert "line104" in result

    def test_overlapping_windows_merged(self):
        lines = [f"line{i}" for i in range(200)]
        t1 = UnresolvedThread(thread_id="T1", path="f.py", line=50, body="a")
        t2 = UnresolvedThread(thread_id="T2", path="f.py", line=55, body="b")
        result = _extract_sections(lines, [t1, t2], context_lines=10)
        # Windows overlap, so no "..." separator
        assert "..." not in result

    def test_distant_windows_separated(self):
        lines = [f"line{i}" for i in range(500)]
        t1 = UnresolvedThread(thread_id="T1", path="f.py", line=10, body="a")
        t2 = UnresolvedThread(thread_id="T2", path="f.py", line=400, body="b")
        result = _extract_sections(lines, [t1, t2], context_lines=5)
        assert "..." in result

    def test_edge_clamps_to_file_bounds(self):
        lines = [f"line{i}" for i in range(20)]
        thread = UnresolvedThread(thread_id="T1", path="f.py", line=1, body="issue")
        result = _extract_sections(lines, [thread], context_lines=50)
        # Should not crash, should contain first line
        assert "line0" in result


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

        llm.complete = AsyncMock(return_value=verify_response)
        llm.walkthrough = AsyncMock(
            return_value=json.dumps({"summary": "walkthrough", "change_groups": []})
        )
        llm.review = AsyncMock(return_value=sample_llm_response_text)

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
    async def test_full_flow_passes_full_file_for_small_files(
        self,
        sample_llm_response_text: str,
        provider_with_threads: AsyncMock,
    ):
        """Small files (<= 500 lines) pass full content to verify-fixes prompt."""
        small_content = "line\n" * 100  # 100 lines — well under threshold
        provider_with_threads.get_file_content = AsyncMock(return_value=small_content)

        verify_response = json.dumps({"results": []})
        llm = MagicMock(spec=LLMProvider)
        llm.count_tokens = MagicMock(return_value=100)
        llm.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        llm.complete = AsyncMock(return_value=verify_response)
        llm.walkthrough = AsyncMock(
            return_value=json.dumps({"summary": "walkthrough", "change_groups": []})
        )
        llm.review = AsyncMock(return_value=sample_llm_response_text)

        engine = ReviewEngine(
            config=MiraConfig(), llm=llm, provider=provider_with_threads, bot_name="mira"
        )
        await engine.review_pr("https://github.com/test/repo/pull/1")

        # Verify the LLM was called with line-numbered file content in the prompt
        verify_call = llm.complete.call_args_list[0]
        prompt_content = verify_call[0][0][1]["content"]
        # Content should be line-numbered (e.g. "  1| line")
        assert "1| line" in prompt_content
        assert "100| line" in prompt_content

    @pytest.mark.asyncio
    async def test_unresolved_threads_passed_to_review(
        self,
        sample_llm_response_text: str,
        provider_with_threads: AsyncMock,
        threads: list[UnresolvedThread],
    ):
        """Unresolved threads are passed as existing_comments to the review prompt."""
        # T1 fixed, T2 not fixed — T2 should be passed to review
        verify_response = json.dumps(
            {"results": [{"id": "T1", "fixed": True}, {"id": "T2", "fixed": False}]}
        )

        llm = MagicMock(spec=LLMProvider)
        llm.count_tokens = MagicMock(return_value=100)
        llm.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        llm.complete = AsyncMock(return_value=verify_response)
        llm.walkthrough = AsyncMock(
            return_value=json.dumps({"summary": "walkthrough", "change_groups": []})
        )
        llm.review = AsyncMock(return_value=sample_llm_response_text)

        engine = ReviewEngine(
            config=MiraConfig(), llm=llm, provider=provider_with_threads, bot_name="mira"
        )

        with patch(
            "mira.core.engine.build_review_prompt",
            wraps=__import__(
                "mira.llm.prompts.review", fromlist=["build_review_prompt"]
            ).build_review_prompt,
        ) as mock_build:
            await engine.review_pr("https://github.com/test/repo/pull/1")

            # build_review_prompt should have been called with existing_comments
            assert mock_build.call_count >= 1
            # Check the first chunk call — should include the unresolved T2 thread
            _, first_kwargs = mock_build.call_args_list[0]
            assert "existing_comments" in first_kwargs
            existing = first_kwargs["existing_comments"]
            assert len(existing) == 1
            assert existing[0].thread_id == "T2"

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


class TestShortThreadDescription:
    """Helper that extracts a one-line summary from a bot review-comment body
    for use as 'already addressed' context in follow-up rounds."""

    def test_extracts_bold_title(self):
        from mira.core.threads import short_thread_description as _short_thread_description

        body = "🐛 **Bug**\n⚠️ Warning\n\n**Resource leak in handler**\n\nLong body text."
        assert _short_thread_description(body) == "Resource leak in handler"

    def test_skips_badge_lines(self):
        from mira.core.threads import short_thread_description as _short_thread_description

        body = "⚠️ **Error Handling**\n💡 Suggestion\n\n**Catches every exception too broadly**"
        assert "Catches every exception" in _short_thread_description(body)

    def test_falls_back_to_first_line(self):
        from mira.core.threads import short_thread_description as _short_thread_description

        body = "Plain text comment without a bold title.\nMore detail follows."
        assert _short_thread_description(body) == "Plain text comment without a bold title."

    def test_truncates_long_descriptions(self):
        from mira.core.threads import short_thread_description as _short_thread_description

        body = "**" + ("very long title " * 20) + "**"
        assert len(_short_thread_description(body)) <= 160

    def test_empty_returns_empty(self):
        from mira.core.threads import short_thread_description as _short_thread_description

        assert _short_thread_description("") == ""
        assert _short_thread_description("   \n  \n") == ""


class TestRoundDetectionWiring:
    """review_pr should detect round number from existing bot threads and
    pass it through, plus collect resolved threads as context."""

    @pytest.mark.asyncio
    async def test_review_pr_detects_round_2_when_threads_exist(self, monkeypatch):
        """If the bot has already left threads on the PR, review_round=2."""
        from unittest.mock import AsyncMock, MagicMock

        from mira.core.engine import ReviewEngine
        from mira.models import BotThreadRecord, PRInfo

        mock_provider = MagicMock()
        mock_provider.get_pr_info = AsyncMock(
            return_value=PRInfo(
                title="t",
                description="",
                base_branch="main",
                head_branch="f",
                url="https://github.com/o/r/pull/1",
                number=1,
                owner="o",
                repo="r",
            )
        )
        mock_provider.get_pr_diff = AsyncMock(return_value="")
        mock_provider.get_unresolved_bot_threads = AsyncMock(return_value=[])
        mock_provider.get_all_bot_threads = AsyncMock(
            return_value=[
                BotThreadRecord(
                    thread_id="t1",
                    path="a.py",
                    line=10,
                    body="**Already-fixed concern**",
                    is_resolved=True,
                ),
            ]
        )
        mock_provider.find_bot_comment = AsyncMock(return_value=None)
        mock_provider.post_comment = AsyncMock()
        mock_provider.update_comment = AsyncMock()
        mock_provider.resolve_outdated_review_threads = AsyncMock(return_value=0)

        captured: dict = {}

        async def fake_internal(self, diff_text, **kwargs):
            captured["review_round"] = kwargs.get("review_round")
            captured["resolved_threads"] = kwargs.get("resolved_threads")
            from mira.models import ReviewResult

            return ReviewResult(comments=[], summary="")

        monkeypatch.setattr(ReviewEngine, "_review_diff_internal", fake_internal)

        engine = ReviewEngine(
            config=MiraConfig(),
            llm=AsyncMock(),
            provider=mock_provider,
            bot_name="mira",
        )
        await engine.review_pr("https://github.com/o/r/pull/1")

        assert captured["review_round"] == 2
        assert captured["resolved_threads"] is not None
        assert len(captured["resolved_threads"]) == 1
        assert captured["resolved_threads"][0]["path"] == "a.py"
        assert captured["resolved_threads"][0]["line"] == 10
        assert "Already-fixed concern" in captured["resolved_threads"][0]["description"]

    @pytest.mark.asyncio
    async def test_review_pr_round_1_when_no_prior_threads(self, monkeypatch):
        """First review on a PR — no bot threads yet, round=1."""
        from unittest.mock import AsyncMock, MagicMock

        from mira.core.engine import ReviewEngine
        from mira.models import PRInfo

        mock_provider = MagicMock()
        mock_provider.get_pr_info = AsyncMock(
            return_value=PRInfo(
                title="t",
                description="",
                base_branch="main",
                head_branch="f",
                url="https://github.com/o/r/pull/1",
                number=1,
                owner="o",
                repo="r",
            )
        )
        mock_provider.get_pr_diff = AsyncMock(return_value="")
        mock_provider.get_unresolved_bot_threads = AsyncMock(return_value=[])
        mock_provider.get_all_bot_threads = AsyncMock(return_value=[])
        mock_provider.find_bot_comment = AsyncMock(return_value=None)
        mock_provider.post_comment = AsyncMock()
        mock_provider.update_comment = AsyncMock()
        mock_provider.resolve_outdated_review_threads = AsyncMock(return_value=0)

        captured: dict = {}

        async def fake_internal(self, diff_text, **kwargs):
            captured["review_round"] = kwargs.get("review_round")
            from mira.models import ReviewResult

            return ReviewResult(comments=[], summary="")

        monkeypatch.setattr(ReviewEngine, "_review_diff_internal", fake_internal)

        engine = ReviewEngine(
            config=MiraConfig(),
            llm=AsyncMock(),
            provider=mock_provider,
            bot_name="mira",
        )
        await engine.review_pr("https://github.com/o/r/pull/1")

        assert captured["review_round"] == 1


class TestIncrementalDiff:
    """Round 2+ should review only commits pushed since the last review."""

    def _make_thread(self, **kw):
        from mira.models import BotThreadRecord

        defaults = {"thread_id": "t", "path": "a.py", "line": 1, "body": "x", "is_resolved": False}
        defaults.update(kw)
        return BotThreadRecord(**defaults)

    def _provider_with_threads_and_compare(self, threads, full_diff="FULL", incremental="INCR"):
        from mira.models import PRInfo

        mock_provider = MagicMock()
        mock_provider.get_pr_info = AsyncMock(
            return_value=PRInfo(
                title="t",
                description="",
                base_branch="main",
                head_branch="f",
                url="https://github.com/o/r/pull/1",
                number=1,
                owner="o",
                repo="r",
                head_sha="HEAD_SHA",
            )
        )
        mock_provider.get_pr_diff = AsyncMock(return_value=full_diff)
        mock_provider.get_compare_diff = AsyncMock(return_value=incremental)
        mock_provider.get_unresolved_bot_threads = AsyncMock(return_value=[])
        mock_provider.get_all_bot_threads = AsyncMock(return_value=threads)
        mock_provider.find_bot_comment = AsyncMock(return_value=None)
        mock_provider.post_comment = AsyncMock()
        mock_provider.update_comment = AsyncMock()
        mock_provider.resolve_outdated_review_threads = AsyncMock(return_value=0)
        return mock_provider

    @pytest.mark.asyncio
    async def test_round_2_uses_incremental_when_sha_stored(self, monkeypatch):
        """If a last_reviewed_sha exists for this PR, fetch and use the
        incremental diff (last_sha..head_sha) instead of the full PR diff."""
        from mira.core.engine import ReviewEngine

        mock_provider = self._provider_with_threads_and_compare(
            threads=[self._make_thread()],
        )

        mock_db = MagicMock()
        mock_db.get_last_reviewed_sha = MagicMock(return_value="OLD_SHA")
        mock_db.get_repo = MagicMock(return_value=None)
        mock_db.set_last_reviewed_sha = MagicMock()
        monkeypatch.setattr("mira.dashboard.api._app_db", mock_db)

        captured: dict = {}

        async def fake_internal(self, diff_text, **kwargs):
            captured["diff_text"] = diff_text
            from mira.models import ReviewResult

            return ReviewResult(comments=[], summary="")

        monkeypatch.setattr(ReviewEngine, "_review_diff_internal", fake_internal)

        engine = ReviewEngine(
            config=MiraConfig(),
            llm=AsyncMock(),
            provider=mock_provider,
            bot_name="mira",
        )
        await engine.review_pr("https://github.com/o/r/pull/1")

        # Compare API was called with the right SHAs and result was used.
        mock_provider.get_compare_diff.assert_awaited_once()
        args = mock_provider.get_compare_diff.call_args
        assert args.args[1] == "OLD_SHA"
        assert args.args[2] == "HEAD_SHA"
        assert captured["diff_text"] == "INCR"

    @pytest.mark.asyncio
    async def test_round_2_falls_back_to_full_diff_when_no_sha(self, monkeypatch):
        """Missing last_reviewed_sha → no incremental fetch, full diff used.
        Backward compat for PRs that existed before the feature shipped."""
        from mira.core.engine import ReviewEngine

        mock_provider = self._provider_with_threads_and_compare(
            threads=[self._make_thread()],
        )

        mock_db = MagicMock()
        mock_db.get_last_reviewed_sha = MagicMock(return_value="")
        mock_db.get_repo = MagicMock(return_value=None)
        mock_db.set_last_reviewed_sha = MagicMock()
        monkeypatch.setattr("mira.dashboard.api._app_db", mock_db)

        captured: dict = {}

        async def fake_internal(self, diff_text, **kwargs):
            captured["diff_text"] = diff_text
            from mira.models import ReviewResult

            return ReviewResult(comments=[], summary="")

        monkeypatch.setattr(ReviewEngine, "_review_diff_internal", fake_internal)

        engine = ReviewEngine(
            config=MiraConfig(),
            llm=AsyncMock(),
            provider=mock_provider,
            bot_name="mira",
        )
        await engine.review_pr("https://github.com/o/r/pull/1")

        mock_provider.get_compare_diff.assert_not_called()
        assert captured["diff_text"] == "FULL"

    @pytest.mark.asyncio
    async def test_round_1_does_not_use_compare(self, monkeypatch):
        """Round 1 must always do a full review — no incremental."""
        from mira.core.engine import ReviewEngine

        mock_provider = self._provider_with_threads_and_compare(threads=[])

        mock_db = MagicMock()
        mock_db.get_last_reviewed_sha = MagicMock(return_value="OLD_SHA")
        mock_db.get_repo = MagicMock(return_value=None)
        mock_db.set_last_reviewed_sha = MagicMock()
        monkeypatch.setattr("mira.dashboard.api._app_db", mock_db)

        async def fake_internal(self, diff_text, **kwargs):
            from mira.models import ReviewResult

            return ReviewResult(comments=[], summary="")

        monkeypatch.setattr(ReviewEngine, "_review_diff_internal", fake_internal)

        engine = ReviewEngine(
            config=MiraConfig(),
            llm=AsyncMock(),
            provider=mock_provider,
            bot_name="mira",
        )
        await engine.review_pr("https://github.com/o/r/pull/1")

        mock_provider.get_compare_diff.assert_not_called()

    @pytest.mark.asyncio
    async def test_records_head_sha_after_review(self, monkeypatch):
        """After a successful review, the current head SHA is anchored so
        round 2 has a base for the incremental diff."""
        from mira.core.engine import ReviewEngine

        mock_provider = self._provider_with_threads_and_compare(threads=[])

        mock_db = MagicMock()
        mock_db.get_last_reviewed_sha = MagicMock(return_value="")
        mock_db.get_repo = MagicMock(return_value=None)
        mock_db.set_last_reviewed_sha = MagicMock()
        monkeypatch.setattr("mira.dashboard.api._app_db", mock_db)

        async def fake_internal(self, diff_text, **kwargs):
            from mira.models import ReviewResult

            return ReviewResult(comments=[], summary="")

        monkeypatch.setattr(ReviewEngine, "_review_diff_internal", fake_internal)

        engine = ReviewEngine(
            config=MiraConfig(),
            llm=AsyncMock(),
            provider=mock_provider,
            bot_name="mira",
        )
        await engine.review_pr("https://github.com/o/r/pull/1")

        mock_db.set_last_reviewed_sha.assert_called_once_with("o", "r", 1, "HEAD_SHA")


class TestSelfCritique:
    """Second-pass critique drops confidently-wrong findings before posting."""

    def _make_comment(self, **kw):
        defaults = {
            "path": "src/x.py",
            "line": 10,
            "end_line": None,
            "severity": Severity.WARNING,
            "category": "bug",
            "title": "Test issue",
            "body": "Body text",
            "confidence": 0.85,
            "existing_code": "def foo(): pass",
        }
        defaults.update(kw)
        return ReviewComment(**defaults)

    @pytest.mark.asyncio
    async def test_critique_drops_unkept_comments(self, monkeypatch):
        """LLM returns keep=false for one of two; that one gets dropped."""
        from unittest.mock import AsyncMock

        comments = [
            self._make_comment(line=10, title="Real bug"),
            self._make_comment(line=20, title="False positive"),
        ]

        # Mock the critic to drop comment index 1 (the false positive).
        verdict_response = json.dumps(
            {
                "verdicts": [
                    {"index": 0, "keep": True, "reason": "valid"},
                    {"index": 1, "keep": False, "reason": "speculation"},
                ]
            }
        )

        # self_critique instantiates its own critic LLM via load_config(),
        # so patch complete_with_tools on the provider class globally.
        from mira.llm import provider as provider_mod

        async def fake_complete_with_tools(self, messages, tools, temperature=None):
            return verdict_response

        monkeypatch.setattr(
            provider_mod.LLMProvider, "complete_with_tools", fake_complete_with_tools
        )

        from mira.core.passes import self_critique

        llm = AsyncMock()
        kept = await self_critique(llm, comments)

        assert len(kept) == 1
        assert kept[0].title == "Real bug"

    @pytest.mark.asyncio
    async def test_critique_keeps_all_when_llm_fails(self, monkeypatch):
        """LLM call failure must NOT silently drop comments — keep them all."""
        from unittest.mock import AsyncMock

        comments = [self._make_comment(line=10), self._make_comment(line=20, title="Two")]

        from mira.llm import provider as provider_mod

        async def fake_complete_with_tools(self, messages, tools, temperature=None):
            raise RuntimeError("LLM down")

        monkeypatch.setattr(
            provider_mod.LLMProvider, "complete_with_tools", fake_complete_with_tools
        )

        from mira.core.passes import self_critique

        llm = AsyncMock()
        kept = await self_critique(llm, comments)

        # All originals retained on critic failure (fail-open, not fail-closed).
        assert len(kept) == 2

    @pytest.mark.asyncio
    async def test_critique_empty_input_returns_empty(self):
        """Critic should not call the LLM if there are no comments to verify."""
        from unittest.mock import AsyncMock

        from mira.core.passes import self_critique

        kept = await self_critique(AsyncMock(), [])
        assert kept == []


class TestSecurityReviewPass:
    """Dedicated security pass returns comments tagged category=security."""

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_files(self):
        from mira.core.passes import security_review_pass

        out = await security_review_pass(AsyncMock(), [], [], "title")
        assert out == []

    @pytest.mark.asyncio
    async def test_runs_llm_and_parses_comments(self, sample_diff_text):
        """Happy path: LLM returns a security finding, we get a ReviewComment."""
        from mira.core.diff_parser import parse_diff

        files = parse_diff(sample_diff_text).files
        # Pick a verbatim line from the diff so the existing_code citation
        # validates — the parser drops comments whose citation isn't in the
        # diff (anti-hallucination check).
        cited_line = None
        for h in files[0].hunks:
            for raw in h.content.splitlines():
                if raw.startswith("+") and not raw.startswith("+++"):
                    cited_line = raw[1:]
                    break
            if cited_line:
                break
        assert cited_line, "fixture must contain at least one added line"
        canned = json.dumps(
            {
                "comments": [
                    {
                        "path": files[0].path,
                        "line": 1,
                        "severity": "blocker",
                        "category": "bug",  # LLM forgot — engine should fix to security
                        "title": "SQL injection",
                        "body": "Concatenating user input into a query.",
                        "confidence": 0.9,
                        "existing_code": cited_line,
                    }
                ],
                "summary": "",
                "metadata": {"reviewed_files": 1},
            }
        )
        from mira.core.passes import security_review_pass

        llm = MagicMock(spec=LLMProvider)
        llm.complete_with_tools = AsyncMock(return_value=canned)

        out = await security_review_pass(llm, files, files, "title")
        assert len(out) == 1
        # Engine forces category=security regardless of what the LLM returned.
        assert out[0].category == "security"
        assert out[0].title == "SQL injection"

    @pytest.mark.asyncio
    async def test_returns_empty_on_llm_failure(self, sample_diff_text):
        """LLM error must not crash — return empty so main review proceeds."""
        from mira.core.diff_parser import parse_diff
        from mira.core.passes import security_review_pass

        files = parse_diff(sample_diff_text).files
        llm = MagicMock(spec=LLMProvider)
        llm.complete_with_tools = AsyncMock(side_effect=RuntimeError("LLM down"))

        out = await security_review_pass(llm, files, files, "title")
        assert out == []


class TestRegenerateSummary:
    """Summary prose must describe only issues that were actually filed."""

    def _comment(self, **kw):
        defaults = {
            "path": "src/x.py",
            "line": 10,
            "end_line": None,
            "severity": Severity.WARNING,
            "category": "bug",
            "title": "t",
            "body": "b",
            "confidence": 0.9,
        }
        defaults.update(kw)
        return ReviewComment(**defaults)

    @pytest.mark.asyncio
    async def test_returns_no_issues_when_nothing_filed(self):
        """Empty inputs short-circuit before any LLM call."""
        from mira.core.passes import regenerate_summary

        out = await regenerate_summary(AsyncMock(), [], [], "title", "desc", fallback="x")
        assert out == "No issues found."

    @pytest.mark.asyncio
    async def test_uses_cheap_llm_output(self, monkeypatch):
        """Successful regen returns the cheap LLM's prose, stripped."""
        from mira.core.passes import regenerate_summary
        from mira.llm import provider as provider_mod

        async def fake_complete(self, messages, json_mode=True, temperature=None, max_tokens=None):
            return "  Fresh summary based on filed issues only.  "

        monkeypatch.setattr(provider_mod.LLMProvider, "complete", fake_complete)

        out = await regenerate_summary(
            AsyncMock(), [self._comment()], [], "title", "desc", fallback="old summary"
        )
        assert out == "Fresh summary based on filed issues only."

    @pytest.mark.asyncio
    async def test_falls_back_on_llm_failure(self, monkeypatch):
        """LLM error must not crash the review — use the original summary."""
        from mira.core.passes import regenerate_summary
        from mira.llm import provider as provider_mod

        async def fake_complete(self, messages, json_mode=True, temperature=None, max_tokens=None):
            raise RuntimeError("LLM down")

        monkeypatch.setattr(provider_mod.LLMProvider, "complete", fake_complete)

        out = await regenerate_summary(
            AsyncMock(), [self._comment()], [], "t", "d", fallback="original prose"
        )
        assert out == "original prose"

    @pytest.mark.asyncio
    async def test_falls_back_when_llm_returns_empty(self, monkeypatch):
        """Empty LLM output → use fallback so summary is never blank."""
        from mira.core.passes import regenerate_summary
        from mira.llm import provider as provider_mod

        async def fake_complete(self, messages, json_mode=True, temperature=None, max_tokens=None):
            return "   "

        monkeypatch.setattr(provider_mod.LLMProvider, "complete", fake_complete)

        out = await regenerate_summary(
            AsyncMock(), [self._comment()], [], "t", "d", fallback="fallback"
        )
        assert out == "fallback"


class TestDropOrphanKeyIssues:
    """Key Issues table must stay in sync with surviving inline comments."""

    def _comment(self, path="src/x.py", line=10, end_line=None):
        return ReviewComment(
            path=path,
            line=line,
            end_line=end_line,
            severity=Severity.WARNING,
            category="bug",
            title="t",
            body="b",
            confidence=0.9,
        )

    def test_drops_orphan_when_inline_filtered_out(self):
        # Inline comment for line 1037 was dropped; its key_issue must go too.
        comments = [self._comment(line=700)]
        key_issues = [
            KeyIssue(issue="kept", path="src/x.py", line=700),
            KeyIssue(issue="orphan", path="src/x.py", line=1037),
        ]
        kept = _drop_orphan_key_issues(key_issues, comments)
        assert [k.line for k in kept] == [700]

    def test_keeps_when_line_within_comment_range(self):
        # Multi-line comment covers lines 50-55; key_issue at 53 is kept.
        comments = [self._comment(line=50, end_line=55)]
        key_issues = [KeyIssue(issue="mid-range", path="src/x.py", line=53)]
        assert _drop_orphan_key_issues(key_issues, comments) == key_issues

    def test_keeps_within_line_tolerance(self):
        # LLM filed inline at 296 but key_issue at 290 — same finding, off
        # by a few lines. Should still be kept.
        comments = [self._comment(line=296)]
        key_issues = [KeyIssue(issue="off by 6", path="src/x.py", line=290)]
        assert _drop_orphan_key_issues(key_issues, comments) == []
        # Within ±3 should match.
        assert _drop_orphan_key_issues(
            [KeyIssue(issue="off by 3", path="src/x.py", line=293)],
            comments,
        ) == [KeyIssue(issue="off by 3", path="src/x.py", line=293)]

    def test_path_mismatch_drops_key_issue(self):
        # Same line, different path — must not match.
        comments = [self._comment(path="src/a.py", line=10)]
        key_issues = [KeyIssue(issue="other file", path="src/b.py", line=10)]
        assert _drop_orphan_key_issues(key_issues, comments) == []

    def test_empty_comments_drops_all(self):
        # If self-critique drops every comment, no key_issues should survive.
        key_issues = [KeyIssue(issue="x", path="src/x.py", line=1)]
        assert _drop_orphan_key_issues(key_issues, []) == []

    def test_empty_key_issues_returns_empty(self):
        comments = [self._comment(line=10)]
        assert _drop_orphan_key_issues([], comments) == []
