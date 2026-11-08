"""Release smoke eval: real pull requests through the full review path.

The other evals review twenty-line synthetic diffs with no repository behind
them, so the agentic tool loop, the hop cap, the forced submission and the
walkthrough on a real-size PR are never exercised before a release. This
suite reviews a handful of merged PRs from this repository end to end (dry
run, nothing is posted) on the configured model, through the same agentic
path the server runs, and fails the gate when a review comes back
incomplete: a failed chunk, a failed security pass, a missing walkthrough,
or a review that never entered the tool loop. It is a health check, not a
quality score.

Needs an LLM key and a GitHub token (``GITHUB_TOKEN`` or ``MIRA_GIT_TOKEN``).
``MIRA_MODEL`` selects the model the same way it does for ``mira review``, so
the gate can run on the model the deployment actually uses.

Run with: pytest tests/evals/test_eval_release_smoke.py -m eval
"""

from __future__ import annotations

import os

import pytest

from mira.config import FilterConfig, load_config
from mira.core.engine import ReviewEngine
from mira.dashboard.models_config import llm_config_for
from mira.llm import create_llm
from mira.models import ReviewResult
from mira.providers import create_provider

_GIT_TOKEN = os.environ.get("MIRA_GIT_TOKEN") or os.environ.get("GITHUB_TOKEN")

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        not os.environ.get("OPENROUTER_API_KEY")
        and not os.environ.get("OPENAI_API_KEY")
        and not os.environ.get("ANTHROPIC_API_KEY"),
        reason="No LLM API key set (need OPENROUTER_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY)",
    ),
    pytest.mark.skipif(
        not _GIT_TOKEN,
        reason="No GitHub token set (need GITHUB_TOKEN or MIRA_GIT_TOKEN)",
    ),
]

# Merged PRs, so their head commits and diffs no longer change. Sizes are
# spread so the small ones finish in one chunk and the large ones need
# several, which is where the hop cap and the forced submission fire.
SMOKE_PRS = [
    "https://github.com/miracodeai/mira/pull/409",  # 3 files: emoji escape fix + test
    "https://github.com/miracodeai/mira/pull/410",  # 3 files: walkthrough placeholder on failure
    "https://github.com/miracodeai/mira/pull/414",  # 4 files: webhook payload field + tests
    "https://github.com/miracodeai/mira/pull/413",  # 13 files: prompt caching in the LLM layer
    "https://github.com/miracodeai/mira/pull/423",  # 30 files: review loop, prompts, scripts
]


def _make_engine() -> ReviewEngine:
    config = load_config()
    # Pin the filter to defaults so ambient dashboard overrides can't change
    # what the gate sees (see test_eval_llm.py).
    config.filter = FilterConfig()
    # Production defaults otherwise. In particular `code_context` stays on:
    # the agentic reviewer tools are wired up inside that step, so turning it
    # off silently drops the review to a single forced `submit_review` call,
    # which is a different (and on thinking models, much weaker) path than
    # the one the server and the benchmark run. The index store it opens
    # lives under MIRA_INDEX_DIR, which the test points at a temp directory.
    config.review.blast_radius = False
    return ReviewEngine(
        config=config,
        llm=create_llm(llm_config_for("review", config.llm)),
        provider=create_provider("github", _GIT_TOKEN or ""),
        dry_run=True,
        indexing_llm=create_llm(llm_config_for("indexing", config.llm)),
        security_llm=create_llm(llm_config_for("security", config.llm)),
    )


def _problems(result: ReviewResult) -> list[str]:
    """Everything that would have shown up on the PR as a broken review."""
    problems: list[str] = []
    for failure in result.failed_chunks:
        problems.append(f"chunk failed ({failure.reason}): {', '.join(failure.paths)}")
    for failure in result.security_failures:
        problems.append(f"security pass failed ({failure.reason})")
    if result.walkthrough is None:
        problems.append("walkthrough was not generated")
    if result.reviewed_files == 0:
        problems.append("no files were reviewed")
    if not any(entry.get("stage") == "agentic" for entry in result.audit):
        problems.append("agentic review loop did not run (single-shot path was used)")
    return problems


@pytest.mark.asyncio
@pytest.mark.parametrize("pr_url", SMOKE_PRS)
async def test_eval_real_pr_review_completes(pr_url: str, tmp_path, monkeypatch) -> None:
    """One retry absorbs a single bad model response; twice in a row is a red gate."""
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path / "indexes"))
    attempts: list[list[str]] = []
    for _ in range(2):
        result = await _make_engine().review_pr(pr_url)
        problems = _problems(result)
        attempts.append(problems)
        if not problems:
            return
    pytest.fail(f"{pr_url} came back incomplete on both attempts: {attempts}")
