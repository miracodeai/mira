"""Tests for the provider-neutral review summary format."""

from __future__ import annotations

from mira.models import KeyIssue, PRInfo, ReviewComment, ReviewResult, Severity
from mira.providers.formatting import format_review_summary


def _pr_info() -> PRInfo:
    return PRInfo(
        title="Example",
        description="",
        base_branch="main",
        head_branch="feature/review-layout",
        url="https://github.com/example/repo/pull/7",
        number=7,
        owner="example",
        repo="repo",
        head_sha="1234567890abcdef",
    )


def _comment(
    path: str,
    line: int,
    severity: Severity,
    suggestion: str | None = None,
) -> ReviewComment:
    return ReviewComment(
        path=path,
        line=line,
        end_line=None,
        severity=severity,
        category="bug",
        title="Finding",
        body="Details",
        confidence=0.9,
        suggestion=suggestion,
    )


def test_review_summary_is_segmented_ordered_and_uses_icons() -> None:
    result = ReviewResult(
        comments=[
            _comment("z.py", 20, Severity.SUGGESTION, suggestion="return value"),
            _comment("a.py", 4, Severity.BLOCKER),
            _comment("m.py", 9, Severity.WARNING),
        ],
        summary="The change needs a safer validation path.",
        reviewed_paths=["z.py", "a.py", "m.py"],
        skipped_paths=["README.md"],
        total_paths=["README.md", "z.py", "a.py", "m.py"],
        key_issues=[KeyIssue(issue="Critical path", path="z.py", line=20)],
    )

    body = format_review_summary(result, bot_name="mira", pr_info=_pr_info())

    assert body.startswith("<!-- mira-review-summary -->\n## Mira Review")
    assert "**Actionable comments posted: 3**" in body
    assert "🛑 1 blocker" in body
    assert "🤖 Prompt for all review comments with AI agents" in body
    assert "🪄 Autofix" in body
    assert "ℹ️ Review info" in body
    assert "⚙️ Run configuration" in body
    assert "📁 Files selected for processing (3)" in body
    assert "⏭️ Files skipped from review (1)" in body
    assert "📌 Key issues (1)" in body
    assert body.index("`a.py`") < body.index("`m.py`") < body.index("`z.py`")
    assert "`main` ← `feature/review-layout`" in body
    assert "`12345678`" in body


def test_clean_review_still_publishes_a_standard_summary() -> None:
    body = format_review_summary(
        ReviewResult(summary="No actionable defects were found."), bot_name="miracodeai"
    )

    assert "**Actionable comments posted: 0**" in body
    assert "✅ No actionable comments found." in body
    assert "| Blocking findings | ✅ 0 |" in body
    assert "| Review scope | ℹ️ File scope not reported |" in body
    assert "📁 Files selected for processing (0)" in body
