"""F1 scorecard — the `benchmark` marker pyproject already declares.

Tracked, not a release gate: the assertion is a sanity floor, not a
quality threshold. Run with:

    OPENROUTER_API_KEY=... GITHUB_TOKEN=... uv run pytest -m benchmark -v
"""

from __future__ import annotations

import os

import pytest

from .runner import format_report, run_benchmark


def _api_keys_present() -> bool:
    return bool(
        (os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY"))
        and os.environ.get("GITHUB_TOKEN")
    )


@pytest.mark.benchmark
@pytest.mark.asyncio
async def test_f1_scorecard() -> None:
    if not _api_keys_present():
        pytest.skip("OPENROUTER_API_KEY (or OPENAI_API_KEY) and GITHUB_TOKEN required")

    report = await run_benchmark(label="pytest")
    print("\n" + format_report(report))

    overall = report["scores"]["overall"]
    # Sanity floor: the harness ran and judged something, and reviews aren't
    # all crashing. Real acceptance decisions use scripts/run_benchmark.py
    # with N runs and the variance rule.
    assert overall["tp"] + overall["fp"] + overall["fn"] > 0
    errored = [p["pr_url"] for p in report["prs"] if p["error"]]
    assert len(errored) <= len(report["prs"]) // 2, f"too many review crashes: {errored}"
