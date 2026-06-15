"""Unit tests for the judge's counting math and aggregation. No LLM calls."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

from mira.models import ReviewComment, Severity

from .judge import JudgeResult, aggregate, count_results, judge_pr, prf1


def _comment(path: str = "a.py", line: int = 1, title: str = "t") -> ReviewComment:
    return ReviewComment(
        path=path,
        line=line,
        end_line=None,
        severity=Severity.WARNING,
        category="bug",
        title=title,
        body="body",
        confidence=0.9,
    )


FINDINGS = [
    {"id": "f1", "description": "bug one", "category": "bug", "severity": "blocker"},
    {"id": "f2", "description": "bug two", "category": "bug", "severity": "warning"},
]


def _v(idx: int, addresses=None, duplicate_of=None, reason="r") -> dict:
    return {
        "comment_index": idx,
        "addresses_finding": addresses,
        "duplicate_of_comment": duplicate_of,
        "reason": reason,
    }


def test_count_basic_matching():
    comments = [_comment(title="catches f1"), _comment(title="noise"), _comment(title="noise2")]
    verdicts = [_v(0, "f1", reason="same defect"), _v(1, None), _v(2, None)]
    r = count_results(FINDINGS, comments, verdicts)
    assert (r.tp, r.fp, r.fn) == (1, 2, 1)
    assert r.matched == {"f1": 0}
    assert r.missed == ["f2"]
    assert r.fp_indices == [1, 2]


def test_count_out_of_range_index_ignored():
    comments = [_comment()]
    verdicts = [_v(5, "f1"), _v(-1, "f2")]
    r = count_results(FINDINGS, comments, verdicts)
    # No valid verdict for the one comment → it's an unmatched FP cluster.
    assert (r.tp, r.fp, r.fn) == (0, 1, 2)


def test_count_one_comment_covers_two_findings():
    comments = [_comment(), _comment(title="c2")]
    verdicts = [_v(0, "f1"), _v(1, "f2")]
    r = count_results(FINDINGS, comments, verdicts)
    assert (r.tp, r.fp, r.fn) == (2, 0, 0)


def test_duplicate_comment_counts_one_fp():
    # Two comments flag the same non-finding issue; the second is a duplicate.
    comments = [_comment(title="noise"), _comment(title="noise restated")]
    verdicts = [_v(0, None), _v(1, None, duplicate_of=0)]
    r = count_results(FINDINGS, comments, verdicts)
    assert r.fp == 1
    assert r.fp_indices == [0, 1]


def test_duplicate_of_matched_comment_is_not_fp():
    # Inline comment matches f1; a summary line restates it → no FP.
    comments = [_comment(title="inline f1"), _comment(title="summary f1")]
    verdicts = [_v(0, "f1"), _v(1, None, duplicate_of=0)]
    r = count_results(FINDINGS, comments, verdicts)
    assert (r.tp, r.fp) == (1, 0)


def test_transitive_duplicate_cluster():
    comments = [_comment(title="a"), _comment(title="b"), _comment(title="c")]
    verdicts = [_v(0, None), _v(1, None, duplicate_of=0), _v(2, None, duplicate_of=1)]
    r = count_results(FINDINGS, comments, verdicts)
    assert r.fp == 1


def test_missing_verdict_comment_is_an_fp():
    comments = [_comment(), _comment(title="no verdict")]
    r = count_results(FINDINGS, comments, [_v(0, "f1")])
    assert (r.tp, r.fp, r.fn) == (1, 1, 1)
    assert r.fp_indices == [1]


async def test_judge_pr_no_comments_skips_llm():
    llm = AsyncMock()
    r = await judge_pr({"findings": FINDINGS}, [], llm)
    assert (r.tp, r.fp, r.fn) == (0, 0, 2)
    llm.complete_with_tools.assert_not_called()


async def test_judge_pr_parses_tool_response():
    llm = AsyncMock()
    llm.complete_with_tools.return_value = json.dumps(
        {"comment_verdicts": [_v(0, "f1", reason="ok"), _v(1, None, reason="noise")]}
    )
    r = await judge_pr({"findings": FINDINGS}, [_comment(), _comment(title="noise")], llm)
    assert (r.tp, r.fp, r.fn) == (1, 1, 1)


def test_prf1_zero_safe():
    assert prf1(0, 0, 0) == (0.0, 0.0, 0.0)


def test_prf1_known_values():
    # v10 baseline: 29 TP / 51 FP / 39 FN → F1 ≈ 39.2
    p, r, f1 = prf1(29, 51, 39)
    assert round(p, 3) == 0.362
    assert round(r, 3) == 0.426
    assert round(f1 * 100, 1) == 39.2


def test_aggregate_slices_by_language_and_source():
    fx_py = {"language": "python", "source": "martian-bench"}
    fx_go = {"language": "go", "source": "supplemental"}
    results = [
        (fx_py, JudgeResult(tp=2, fp=1, fn=1)),
        (fx_go, JudgeResult(tp=1, fp=3, fn=2)),
    ]
    report = aggregate(results)
    assert report["overall"]["tp"] == 3
    assert report["overall"]["fp"] == 4
    assert report["overall"]["fn"] == 3
    assert report["by_language"]["python"]["tp"] == 2
    assert report["by_language"]["go"]["fp"] == 3
    assert report["by_source"]["martian-bench"]["prs"] == 1
