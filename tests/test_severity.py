"""Tests for severity classification."""

from __future__ import annotations

from mira.analysis.severity import classify_severity, normalize_severity
from mira.models import ReviewComment, Severity


class TestNormalizeSeverity:
    def test_standard_values(self):
        assert normalize_severity("blocker") == Severity.BLOCKER
        assert normalize_severity("warning") == Severity.WARNING
        assert normalize_severity("suggestion") == Severity.SUGGESTION
        assert normalize_severity("nitpick") == Severity.NITPICK

    def test_aliases(self):
        assert normalize_severity("critical") == Severity.BLOCKER
        assert normalize_severity("error") == Severity.BLOCKER
        assert normalize_severity("warn") == Severity.WARNING
        assert normalize_severity("nit") == Severity.NITPICK
        assert normalize_severity("style") == Severity.NITPICK

    def test_case_insensitive(self):
        assert normalize_severity("BLOCKER") == Severity.BLOCKER
        assert normalize_severity("Warning") == Severity.WARNING

    def test_unknown_defaults_to_suggestion(self):
        assert normalize_severity("unknown") == Severity.SUGGESTION
        assert normalize_severity("") == Severity.SUGGESTION


class TestClassifySeverity:
    def _make_comment(
        self,
        severity: Severity,
        category: str = "other",
        title: str = "",
        body: str = "",
    ) -> ReviewComment:
        return ReviewComment(
            path="test.py",
            line=1,
            end_line=None,
            severity=severity,
            category=category,
            title=title,
            body=body,
            confidence=0.9,
        )

    def test_exploitable_upgrade_to_blocker(self):
        comment = self._make_comment(
            Severity.WARNING,
            "security",
            "SQL injection risk",
        )
        result = classify_severity(comment)
        assert result.severity == Severity.BLOCKER

    def test_exploitable_keyword_upgrade(self):
        comment = self._make_comment(
            Severity.NITPICK,
            "other",
            "xss vulnerability found",
        )
        result = classify_severity(comment)
        assert result.severity == Severity.BLOCKER

    def test_eval_is_exploitable(self):
        comment = self._make_comment(
            Severity.WARNING,
            "security",
            title="Use of eval()",
            body="eval() allows arbitrary code execution",
        )
        result = classify_severity(comment)
        assert result.severity == Severity.BLOCKER

    def test_security_smell_caps_at_warning(self):
        """Hardcoded keys marked BLOCKER by LLM should be capped to WARNING."""
        comment = self._make_comment(
            Severity.BLOCKER,
            "security",
            "Hardcoded API key in source",
        )
        result = classify_severity(comment)
        assert result.severity == Severity.WARNING

    def test_security_smell_upgrade_to_warning(self):
        """Hardcoded keys marked SUGGESTION should be upgraded to WARNING."""
        comment = self._make_comment(
            Severity.SUGGESTION,
            "security",
            "Hardcoded secret found",
        )
        result = classify_severity(comment)
        assert result.severity == Severity.WARNING

    def test_style_downgrade(self):
        comment = self._make_comment(Severity.WARNING, "style", "naming convention")
        result = classify_severity(comment)
        assert result.severity == Severity.NITPICK

    def test_no_change_for_normal(self):
        comment = self._make_comment(
            Severity.WARNING,
            "bug",
            "Null pointer dereference",
        )
        result = classify_severity(comment)
        assert result.severity == Severity.WARNING

    def test_severity_ordering(self):
        assert Severity.BLOCKER > Severity.WARNING > Severity.SUGGESTION > Severity.NITPICK


class TestSeverityEmoji:
    def test_all_severities_have_emoji(self):
        for sev in Severity:
            emoji = sev.emoji
            assert isinstance(emoji, str)
            assert len(emoji) > 0

    def test_specific_emojis(self):
        assert Severity.BLOCKER.emoji == "\U0001f6d1"
        assert Severity.NITPICK.emoji == "\U0001f4ac"
