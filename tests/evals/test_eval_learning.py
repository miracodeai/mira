"""Eval suite for the learning loop — runs against a real LLM.

Exercises the full feedback → synthesis → review pipeline:

  1. Seed feedback events (human comments + accept/reject signals)
  2. Run synthesis (`synthesize_rules` + `synthesize_from_human_reviews`)
  3. Verify rules emerged with the expected character
  4. Run a review on a target diff and verify the rules influenced output

Skipped when LLM API keys are not available. Run with:

    pytest tests/evals/test_eval_learning.py -m eval
"""

from __future__ import annotations

import os

# Re-use scenario fixtures from the play harness so both stay in sync.
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.play_learning import SCENARIOS, _seed_feedback  # noqa: E402

from mira.analysis.feedback import (  # noqa: E402
    synthesize_from_human_reviews,
    synthesize_rules,
)
from mira.config import load_config  # noqa: E402
from mira.core.engine import ReviewEngine  # noqa: E402
from mira.index.store import IndexStore  # noqa: E402
from mira.llm.provider import LLMProvider  # noqa: E402
from mira.models import PRInfo  # noqa: E402

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        not os.environ.get("OPENROUTER_API_KEY")
        and not os.environ.get("OPENAI_API_KEY")
        and not os.environ.get("ANTHROPIC_API_KEY"),
        reason="No LLM API key set",
    ),
]


@pytest.fixture
def isolated_index(monkeypatch):
    """Force SQLite, isolated tmp dir, no Postgres."""
    tmp = tempfile.mkdtemp(prefix="mira-eval-learn-")
    monkeypatch.setenv("MIRA_INDEX_DIR", tmp)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return tmp


def _engine() -> tuple[ReviewEngine, LLMProvider]:
    config = load_config()
    config.review.walkthrough = False
    config.review.code_context = False
    llm = LLMProvider(config.llm)
    engine = ReviewEngine(config=config, llm=llm, dry_run=True)
    return engine, llm


def _fake_pr_info(owner: str, repo: str) -> PRInfo:
    return PRInfo(
        title="Eval PR",
        description="",
        base_branch="main",
        head_branch="feature/x",
        url=f"https://github.com/{owner}/{repo}/pull/1",
        number=1,
        owner=owner,
        repo=repo,
    )


# ── synthesis evals ─────────────────────────────────────────────────


class TestSynthesis:
    """Synthesis turns feedback events into useful learned rules."""

    @pytest.mark.asyncio
    async def test_null_checks_synthesizes_avoid_rule(self, isolated_index):
        owner, repo = "eval", "null-checks"
        store = IndexStore.open(owner, repo)
        _seed_feedback(store, SCENARIOS["null-checks"])

        n_pat = synthesize_rules(store)
        _, llm = _engine()
        n_llm = await synthesize_from_human_reviews(store, llm)
        store.close()

        assert n_pat + n_llm >= 1, "no rules emerged from null-check feedback"

        store = IndexStore.open(owner, repo)
        rule_texts = " ".join(r.rule_text.lower() for r in store.list_active_learned_rules())
        store.close()

        # Either the reject-pattern rule (mentions "defensive") OR the LLM rule
        # (mentions "null"/"None"/"defensive") should be present.
        assert any(
            kw in rule_texts for kw in ("defensive", "null", "none check", "guard")
        ), f"learned rules don't mention the team's reject pattern: {rule_texts!r}"

    @pytest.mark.asyncio
    async def test_test_coverage_synthesizes_positive_rule(self, isolated_index):
        owner, repo = "eval", "tests"
        store = IndexStore.open(owner, repo)
        _seed_feedback(store, SCENARIOS["test-coverage"])

        synthesize_rules(store)
        _, llm = _engine()
        n_llm = await synthesize_from_human_reviews(store, llm)
        store.close()

        assert n_llm >= 1, "LLM did not produce a rule from 8 'where are the tests' comments"

        store = IndexStore.open(owner, repo)
        rule_texts = " ".join(r.rule_text.lower() for r in store.list_active_learned_rules())
        store.close()

        assert any(
            kw in rule_texts for kw in ("test", "coverage", "unit test")
        ), f"learned rules don't mention test coverage: {rule_texts!r}"


# ── review-time evals ───────────────────────────────────────────────


class TestRulesInfluenceReview:
    """Once rules are learned, they should change subsequent review behaviour."""

    @pytest.mark.asyncio
    async def test_null_check_rule_suppresses_defensive_comments(self, isolated_index):
        owner, repo = "eval", "null-suppress"
        scenario = SCENARIOS["null-checks"]
        store = IndexStore.open(owner, repo)
        _seed_feedback(store, scenario)
        synthesize_rules(store)
        engine, llm = _engine()
        await synthesize_from_human_reviews(store, llm)
        store.close()

        engine._pr_info = _fake_pr_info(owner, repo)
        result = await engine.review_diff(scenario.target_diff)

        defensive = [
            c
            for c in result.comments
            if c.category in {"defensive", "reliability"}
            or any(kw in c.title.lower() for kw in ("null check", "none check", "defensive"))
        ]
        assert (
            len(defensive) == 0
        ), f"rule failed to suppress defensive comments: {[c.title for c in defensive]}"

    @pytest.mark.asyncio
    async def test_test_coverage_rule_surfaces_test_comment(self, isolated_index):
        owner, repo = "eval", "tests-surface"
        scenario = SCENARIOS["test-coverage"]
        store = IndexStore.open(owner, repo)
        _seed_feedback(store, scenario)
        synthesize_rules(store)
        engine, llm = _engine()
        await synthesize_from_human_reviews(store, llm)
        store.close()

        engine._pr_info = _fake_pr_info(owner, repo)
        result = await engine.review_diff(scenario.target_diff)

        # The bot should now flag missing tests on the new public function.
        tests_mentioned = any(
            kw in (c.title + " " + c.body).lower()
            for c in result.comments
            for kw in ("test", "coverage")
        )
        assert tests_mentioned, (
            f"rule didn't influence review — no comment mentions tests/coverage. "
            f"Comments: {[c.title for c in result.comments]}"
        )
