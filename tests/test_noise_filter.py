"""Tests for noise filtering."""

from __future__ import annotations

from mira.analysis.noise_filter import filter_noise
from mira.config import FilterConfig
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

    def test_no_dedup_different_paths(self):
        comments = [
            _make_comment(path="a.py", title="Same issue"),
            _make_comment(path="b.py", title="Same issue"),
        ]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.0))
        assert len(result) == 2

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

    def test_caps_at_max_comments(self):
        comments = [_make_comment(line=i, title=f"Issue {i}") for i in range(20)]
        result = filter_noise(comments, FilterConfig(confidence_threshold=0.0, max_comments=3))
        assert len(result) == 3

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
