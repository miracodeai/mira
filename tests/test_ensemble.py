"""Ensemble merge logic and engine wiring."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from mira.config import MiraConfig
from mira.core.engine import ReviewEngine
from mira.core.ensemble import merge_ensemble_runs
from mira.llm.provider import LLMProvider
from mira.models import ReviewComment, Severity


def _comment(
    line: int,
    title: str = "Issue",
    confidence: float = 0.9,
    category: str = "bug",
    path: str = "app.py",
) -> ReviewComment:
    return ReviewComment(
        path=path,
        line=line,
        end_line=None,
        severity=Severity.WARNING,
        category=category,
        title=title,
        body="body text here",
        confidence=confidence,
    )


class TestMergeEnsembleRuns:
    def test_single_run_passthrough(self):
        run = [_comment(1), _comment(2)]
        assert merge_ensemble_runs([run]) == run
        assert merge_ensemble_runs([]) == []

    def test_majority_vote_2_of_3(self):
        recurring = lambda: _comment(10, "Null deref on user")  # noqa: E731
        flicker = _comment(50, "Speculative race condition")
        merged = merge_ensemble_runs([[recurring(), flicker], [recurring()], [recurring()]])
        assert len(merged) == 1
        assert merged[0].title == "Null deref on user"

    def test_all_runs_agree_keeps_everything(self):
        runs = [[_comment(10, "A"), _comment(20, "B")] for _ in range(3)]
        merged = merge_ensemble_runs(runs)
        assert {c.title for c in merged} == {"A", "B"}

    def test_confidence_is_cluster_mean(self):
        runs = [
            [_comment(10, "A", confidence=0.9)],
            [_comment(10, "A", confidence=0.7)],
            [_comment(10, "A", confidence=0.8)],
        ]
        merged = merge_ensemble_runs(runs)
        assert merged[0].confidence == 0.8

    def test_representative_is_highest_confidence_member(self):
        weak = _comment(10, "A", confidence=0.6)
        strong = _comment(10, "A", confidence=0.95)
        strong.suggestion = "the good fix"
        merged = merge_ensemble_runs([[weak], [strong]])
        assert merged[0].suggestion == "the good fix"

    def test_distinct_findings_do_not_cluster(self):
        # Same line, different category = distinct findings (noise_filter rule)
        leak = _comment(10, "Resource leak", category="resource-leak")
        injection = _comment(10, "SQL injection", category="security")
        merged = merge_ensemble_runs([[leak, injection], [leak, injection]], min_votes=2)
        assert len(merged) == 2

    def test_explicit_min_votes(self):
        once = _comment(10, "A")
        merged = merge_ensemble_runs([[once], [], []], min_votes=1)
        assert len(merged) == 1


def _review_response(comments: list[dict]) -> str:
    return json.dumps({"comments": comments, "summary": "s", "metadata": {}})


def _raw(line: int, title: str) -> dict:
    return {
        "path": "src/utils.py",
        "line": line,
        "end_line": None,
        "severity": "warning",
        "category": "bug",
        "title": title,
        "body": f"Body for {title}",
        "confidence": 0.9,
        "existing_code": "x",
    }


class TestEnsembleEngineWiring:
    @pytest.mark.asyncio
    async def test_three_runs_consensus(self, sample_diff_text: str):
        config = MiraConfig()
        config.review.ensemble_runs = 3
        config.review.walkthrough = False
        config.review.security_pass = False
        config.review.self_critique = False
        config.review.include_summary = False
        config.review.code_context = False

        responses = [
            _review_response([_raw(9, "Recurring A"), _raw(16, "Recurring B")]),
            _review_response([_raw(9, "Recurring A"), _raw(21, "One-off C")]),
            _review_response([_raw(16, "Recurring B"), _raw(9, "Recurring A")]),
        ]
        llm = MagicMock(spec=LLMProvider)
        llm.review = AsyncMock(side_effect=responses)
        llm.complete = AsyncMock(return_value="")
        llm.count_tokens = MagicMock(return_value=100)
        llm.usage = {"total_tokens": 300}

        engine = ReviewEngine(config=config, llm=llm)
        result = await engine.review_diff(sample_diff_text)

        assert llm.review.call_count == 3
        titles = {c.title for c in result.comments}
        assert titles == {"Recurring A", "Recurring B"}

        # Extra runs sample at the ensemble temperature.
        temps = [c.kwargs.get("temperature") for c in llm.review.call_args_list]
        assert temps.count(0.3) == 2

    @pytest.mark.asyncio
    async def test_failed_extra_run_degrades_gracefully(self, sample_diff_text: str):
        config = MiraConfig()
        config.review.ensemble_runs = 3
        config.review.walkthrough = False
        config.review.security_pass = False
        config.review.self_critique = False
        config.review.include_summary = False
        config.review.code_context = False

        responses = [
            _review_response([_raw(9, "Recurring A")]),
            RuntimeError("LLM down"),
            _review_response([_raw(9, "Recurring A")]),
        ]
        llm = MagicMock(spec=LLMProvider)
        llm.review = AsyncMock(side_effect=responses)
        llm.complete = AsyncMock(return_value="")
        llm.count_tokens = MagicMock(return_value=100)
        llm.usage = {"total_tokens": 300}

        engine = ReviewEngine(config=config, llm=llm)
        result = await engine.review_diff(sample_diff_text)

        # 2 surviving runs, finding present in both — kept.
        assert {c.title for c in result.comments} == {"Recurring A"}

    @pytest.mark.asyncio
    async def test_default_config_is_single_run(self, sample_diff_text: str):
        config = MiraConfig()
        config.review.walkthrough = False
        config.review.security_pass = False
        config.review.self_critique = False
        config.review.include_summary = False
        config.review.code_context = False

        llm = MagicMock(spec=LLMProvider)
        llm.review = AsyncMock(return_value=_review_response([_raw(9, "A")]))
        llm.complete = AsyncMock(return_value="")
        llm.count_tokens = MagicMock(return_value=100)
        llm.usage = {"total_tokens": 100}

        engine = ReviewEngine(config=config, llm=llm)
        await engine.review_diff(sample_diff_text)
        assert llm.review.call_count == 1
