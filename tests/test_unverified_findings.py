"""Opt-in "Worth a look (not verified)" walkthrough section (review.show_unverified_findings)."""

from __future__ import annotations

from mira.config import MiraConfig
from mira.core.engine import _clamp_confidence_to_findings
from mira.models import ReviewComment, Severity, WalkthroughConfidenceScore, WalkthroughResult


def _wt(score: int = 4) -> WalkthroughResult:
    return WalkthroughResult(
        summary="Adds a thing.",
        confidence_score=WalkthroughConfidenceScore(
            score=score, label="Safe with minor fixes", reason="why"
        ),
    )


def _finding(i: int) -> ReviewComment:
    return ReviewComment(
        path=f"src/f{i}.ts",
        line=i,
        end_line=None,
        severity=Severity.SUGGESTION,
        category="bug",
        title=f"maybe {i}",
        body="b",
        confidence=0.6,
    )


def test_off_by_default():
    assert MiraConfig().review.show_unverified_findings is False


def test_verdict_recomputed_when_unverified_findings_are_shown():
    wt = _wt(score=3)
    _clamp_confidence_to_findings(wt, [], unverified=2)
    cs = wt.confidence_score
    assert (cs.score, cs.label) == (4, "No confirmed issues")
    assert cs.reason.startswith("No confirmed issues")


def test_verdict_recomputed_when_nothing_is_shown():
    wt = _wt(score=3)
    _clamp_confidence_to_findings(wt, [], unverified=0)
    cs = wt.confidence_score
    assert (cs.score, cs.label) == (5, "No issues to flag")
    assert cs.reason == "No issues found in the reviewed changes."


def test_low_score_left_alone_even_with_unverified():
    wt = _wt(score=2)
    _clamp_confidence_to_findings(wt, [], unverified=3)
    cs = wt.confidence_score
    assert (cs.score, cs.label, cs.reason) == (2, "Safe with minor fixes", "why")


def test_section_is_collapsed_capped_and_marked_unverified():
    findings = [_finding(i) for i in range(7)]
    md = _wt().to_markdown(total_comments=0, unverified_findings=findings)
    assert "<details>\n<summary><b>Worth a look (not verified)</b> — 7</summary>" in md
    assert "- `src/f0.ts:0` — maybe 0" in md
    assert "src/f5.ts" not in md
    assert "…and 2 more" in md
    assert "check them yourself" in md


def test_absent_when_not_provided_or_in_progress():
    assert "Worth a look" not in _wt().to_markdown()
    in_progress = _wt().to_markdown(in_progress=True, unverified_findings=[_finding(1)])
    assert "Worth a look" not in in_progress
