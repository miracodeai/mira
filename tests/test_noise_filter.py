"""Tests for noise filtering."""

from __future__ import annotations

from mira.config import FilterConfig
from mira.core.noise_filter import _is_duplicate, _jaccard_similarity, filter_noise
from mira.models import ReviewComment, Severity


def _make_comment(
    path: str = "test.py",
    line: int = 1,
    severity: Severity = Severity.WARNING,
    confidence: float = 0.8,
    title: str = "Issue",
) -> ReviewComment:
    return ReviewComment(
        path=path,
        line=line,
        end_line=None,
        severity=severity,
        category="bug",
        title=title,
        body="Description",
        confidence=confidence,
    )


class TestNoiseFilter:
    def test_filters_low_confidence(self):
        comments = [
            _make_comment(confidence=0.5),
            _make_comment(confidence=0.9, line=2),
        ]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.7))
        assert len(result) == 1
        assert result[0].confidence == 0.9

    def test_deduplicates(self):
        comments = [
            _make_comment(line=10, title="Null pointer issue"),
            _make_comment(line=10, title="Null pointer issue found"),
        ]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.0))
        assert len(result) == 1

    def test_no_dedup_different_paths_different_issues(self):
        """Different files with distinct issues should not be deduped."""
        comments = [
            _make_comment(path="a.py", title="Missing null check on user input"),
            _make_comment(path="b.py", title="Database connection leak in handler"),
        ]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.0))
        assert len(result) == 2

    def test_dedup_cross_file_identical_issue(self):
        """Near-identical title+body across different files should be deduped."""
        comments = [
            _make_comment(path="a.py", title="Same issue"),
            _make_comment(path="b.py", title="Same issue"),
        ]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.0))
        assert len(result) == 1

    def test_sorts_by_severity_then_confidence(self):
        comments = [
            _make_comment(severity=Severity.NITPICK, confidence=0.9, line=1, title="Style nit"),
            _make_comment(severity=Severity.BLOCKER, confidence=0.8, line=2, title="Critical bug"),
            _make_comment(
                severity=Severity.WARNING,
                confidence=0.95,
                line=3,
                title="Possible problem",
            ),
        ]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.0))
        assert result[0].severity == Severity.BLOCKER
        assert result[1].severity == Severity.WARNING
        assert result[2].severity == Severity.NITPICK

    def test_caps_at_max_comments_for_low_priority(self):
        """The max_comments cap applies to suggestions and nitpicks only —
        every blocker/warning above the floor should be posted regardless."""
        comments = [
            _make_comment(line=i, title=f"Nit {i}", severity=Severity.NITPICK) for i in range(20)
        ]
        result = filter_noise(
            comments,
            FilterConfig(
                confidence_threshold=0.0,
                max_comments=3,
                min_severity="nitpick",
            ),
        )
        assert len(result) == 3

    def test_does_not_cap_blockers_or_warnings(self):
        """A PR with many real bugs should surface all of them, not be silenced
        by max_comments. The cap is for low-priority noise, not real findings."""
        comments = (
            [_make_comment(line=i, title=f"Bug {i}", severity=Severity.BLOCKER) for i in range(8)]
            + [
                _make_comment(line=100 + i, title=f"Warn {i}", severity=Severity.WARNING)
                for i in range(5)
            ]
            + [
                _make_comment(line=200 + i, title=f"Nit {i}", severity=Severity.NITPICK)
                for i in range(10)
            ]
        )
        result = filter_noise(
            comments,
            FilterConfig(
                confidence_threshold=0.0,
                max_comments=3,
                min_severity="nitpick",
            ),
        )
        # 8 blockers + 5 warnings always pass; nitpicks capped at 3 → 16 total.
        assert len(result) == 16
        assert sum(1 for c in result if c.severity == Severity.BLOCKER) == 8
        assert sum(1 for c in result if c.severity == Severity.WARNING) == 5
        assert sum(1 for c in result if c.severity == Severity.NITPICK) == 3

    def test_empty_input(self):
        result = filter_noise([], FilterConfig())
        assert result == []

    def test_min_severity_filter(self):
        comments = [
            _make_comment(severity=Severity.NITPICK, confidence=0.9, line=1),
            _make_comment(severity=Severity.WARNING, confidence=0.9, line=2),
        ]
        config = FilterConfig(confidence_threshold=0.0, min_severity="warning")
        result = filter_noise(comments, config)
        assert len(result) == 1
        assert result[0].severity == Severity.WARNING

    def test_dedup_overlapping_lines_low_title_similarity(self):
        """Two comments on overlapping lines should dedup even with different titles."""
        comments = [
            _make_comment(line=8, title="Shell injection vulnerability"),
            _make_comment(line=8, title="No error handling for commands"),
        ]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.0))
        assert len(result) == 1

    def test_dedup_same_file_similar_titles_different_lines(self):
        """Same file, similar titles but different lines should dedup."""
        comments = [
            _make_comment(line=5, title="Missing null check"),
            _make_comment(line=50, title="Missing null check here"),
        ]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.0))
        assert len(result) == 1

    def test_no_dedup_different_lines_different_titles(self):
        """Different lines + different titles = keep both."""
        comments = [
            _make_comment(line=5, title="Shell injection risk"),
            _make_comment(line=50, title="Hardcoded API key"),
        ]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.0))
        assert len(result) == 2

    def test_dedup_keeps_higher_severity(self):
        """Fix 3: When duplicates exist, the higher-severity comment is kept."""
        comments = [
            _make_comment(line=10, severity=Severity.NITPICK, confidence=0.8, title="Null check"),
            _make_comment(line=10, severity=Severity.BLOCKER, confidence=0.9, title="Null check"),
        ]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.0))
        assert len(result) == 1
        assert result[0].severity == Severity.BLOCKER

    def test_dedup_keeps_higher_confidence_same_severity(self):
        """Fix 3: Among same-severity dups, the higher-confidence comment is kept."""
        comments = [
            _make_comment(line=10, severity=Severity.WARNING, confidence=0.7, title="Issue here"),
            _make_comment(line=10, severity=Severity.WARNING, confidence=0.95, title="Issue here"),
        ]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.0))
        assert len(result) == 1
        assert result[0].confidence == 0.95

    def test_sort_before_dedup_order_independence(self):
        """Fix 3: Input order should not affect which duplicate is kept."""
        low = _make_comment(
            line=10,
            severity=Severity.NITPICK,
            confidence=0.75,
            title="Same problem",
        )
        high = _make_comment(
            line=10,
            severity=Severity.BLOCKER,
            confidence=0.95,
            title="Same problem",
        )
        config = FilterConfig(confidence_threshold=0.0)

        # Low first
        result_a = filter_noise([low, high], config)
        # High first
        result_b = filter_noise([high, low], config)

        assert len(result_a) == 1
        assert len(result_b) == 1
        assert result_a[0].severity == Severity.BLOCKER
        assert result_b[0].severity == Severity.BLOCKER


class TestJaccardSimilarity:
    def test_identical_strings(self):
        assert _jaccard_similarity("hello world", "hello world") == 1.0

    def test_empty_string(self):
        assert _jaccard_similarity("", "hello") == 0.0
        assert _jaccard_similarity("hello", "") == 0.0
        assert _jaccard_similarity("", "") == 0.0

    def test_partial_overlap(self):
        sim = _jaccard_similarity("hello world", "hello there")
        assert 0.0 < sim < 1.0


class TestIsDuplicate:
    def test_different_paths_different_titles_not_duplicate(self):
        a = _make_comment(path="a.py", line=1, title="Null check missing")
        b = _make_comment(path="b.py", line=1, title="Connection leak in pool")
        assert _is_duplicate(a, b) is False

    def test_different_paths_identical_issue_is_duplicate(self):
        a = _make_comment(path="a.py", line=1, title="Same title")
        b = _make_comment(path="b.py", line=1, title="Same title")
        assert _is_duplicate(a, b) is True

    def test_same_line_always_duplicate(self):
        a = _make_comment(path="a.py", line=5, title="Totally different")
        b = _make_comment(path="a.py", line=5, title="Completely unrelated")
        assert _is_duplicate(a, b) is True

    def test_non_overlapping_similar_titles_duplicate(self):
        a = _make_comment(path="a.py", line=5, title="Missing null check here")
        b = _make_comment(path="a.py", line=100, title="Missing null check found")
        assert _is_duplicate(a, b) is True

    def test_non_overlapping_different_titles_not_duplicate(self):
        a = _make_comment(path="a.py", line=5, title="Shell injection risk")
        b = _make_comment(path="a.py", line=100, title="Hardcoded API key exposed")
        assert _is_duplicate(a, b) is False


class TestRoundAwareFiltering:
    """Round 1 sets the bar; round 2+ tightens to filter the long-tail drip."""

    def test_round_1_uses_config_defaults(self):
        comments = [
            _make_comment(severity=Severity.SUGGESTION, confidence=0.75, line=1, title="A"),
            _make_comment(severity=Severity.WARNING, confidence=0.9, line=2, title="B"),
        ]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.7), review_round=1)
        # Round 1 keeps both — they pass the 0.7 floor and any-severity floor.
        assert len(result) == 2

    def test_round_2_tightens_confidence_floor(self):
        # 0.75 passes round 1's 0.7 floor but is below round 2's implicit 0.8.
        comments = [
            _make_comment(severity=Severity.WARNING, confidence=0.75, line=1, title="A"),
            _make_comment(severity=Severity.WARNING, confidence=0.85, line=2, title="B"),
        ]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.7), review_round=2)
        assert len(result) == 1
        assert result[0].confidence == 0.85

    def test_round_2_drops_suggestions_keeps_warnings(self):
        # Suggestion-tier comments are exactly the long-tail drip we want to
        # stop on follow-up rounds. Real bugs land at warning+ and stay.
        comments = [
            _make_comment(severity=Severity.SUGGESTION, confidence=0.95, line=1, title="A"),
            _make_comment(severity=Severity.WARNING, confidence=0.85, line=2, title="B"),
        ]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.7), review_round=2)
        assert len(result) == 1
        assert result[0].severity == Severity.WARNING

    def test_round_3_tightens_further(self):
        # Round 3 raises confidence floor to 0.85; 0.82 is dropped.
        comments = [
            _make_comment(severity=Severity.WARNING, confidence=0.82, line=1, title="A"),
            _make_comment(severity=Severity.BLOCKER, confidence=0.9, line=2, title="B"),
        ]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.7), review_round=3)
        assert len(result) == 1
        assert result[0].severity == Severity.BLOCKER

    def test_round_n_does_not_bump_below_user_floor(self):
        # If the user already configured a stricter floor, round 2's bump to
        # 0.8 must not LOWER it.
        comments = [
            _make_comment(severity=Severity.WARNING, confidence=0.85, line=1, title="A"),
            _make_comment(severity=Severity.WARNING, confidence=0.95, line=2, title="B"),
        ]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.9), review_round=2)
        assert len(result) == 1
        assert result[0].confidence == 0.95

    def test_blocker_passes_round_2_filter(self):
        # A real bug introduced by a fix must still be flagged in round 2.
        comments = [
            _make_comment(severity=Severity.BLOCKER, confidence=0.85, line=1),
        ]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.7), review_round=2)
        assert len(result) == 1
        assert result[0].severity == Severity.BLOCKER
