"""LLM eval: does cross-PR overlap detection judge PR pairs correctly?

Runs the real ``detect_overlaps`` pipeline (pre-filter + LLM verdict) against
constructed PR pairs, with fingerprints pre-cached so no GitHub access is
needed. Three judgments matter:

  - two PRs editing the same file for related reasons → merge-conflict risk,
  - two PRs building the same feature in different files → duplicate effort,
  - two unrelated PRs that both touch a lockfile → no finding (noise control).

Probabilistic: each assertion retries a few times and accepts the first clean
result, since hosted models aren't fully deterministic at temperature 0.

Run with: pytest tests/evals/test_eval_overlap.py -m eval
"""

from __future__ import annotations

import os

import pytest

from mira.config import MiraConfig, load_config
from mira.core.overlap import detect_overlaps
from mira.dashboard.models_config import llm_config_for
from mira.llm.provider import LLMProvider
from mira.models import OpenPRRef, OverlapFinding, PRFingerprint, PRInfo

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        not os.environ.get("OPENROUTER_API_KEY")
        and not os.environ.get("OPENAI_API_KEY")
        and not os.environ.get("ANTHROPIC_API_KEY"),
        reason="No LLM API key set (need OPENROUTER_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY)",
    ),
]

_RETRIES = 3


def _llm() -> LLMProvider:
    return LLMProvider(llm_config_for("review", load_config().llm))


def _pr_info(title: str) -> PRInfo:
    return PRInfo(
        title=title,
        description="",
        base_branch="main",
        head_branch="feature/current",
        url="https://github.com/acme/app/pull/1",
        number=1,
        owner="acme",
        repo="app",
        head_sha="cur-sha",
    )


def _candidate(number: int, title: str, body: str, paths: list[str]):
    """Build a matching (ref, cached fingerprint) pair — no fetch needed."""
    ref = OpenPRRef(
        number=number,
        title=title,
        body=body,
        head_sha=f"sha-{number}",
        author="alice",
        base_ref="main",
        head_ref=f"feature/pr{number}",
        url=f"https://github.com/acme/app/pull/{number}",
    )
    fp = PRFingerprint(
        pr_number=number,
        head_sha=f"sha-{number}",
        title=title,
        body=body,
        paths=paths,
    )
    return ref, fp


async def _detect(current_title, current_body, current_paths, candidates):
    refs = [ref for ref, _ in candidates]
    cached = {fp.pr_number: fp for _, fp in candidates}
    current = PRFingerprint(
        pr_number=1,
        head_sha="cur-sha",
        title=current_title,
        body=current_body,
        paths=current_paths,
    )
    return await detect_overlaps(
        provider=object(),
        llm=_llm(),
        config=MiraConfig(),
        pr_info=_pr_info(current_title),
        current=current,
        cached=cached,
        candidates=refs,
    )


async def _passes(coro_factory, predicate) -> bool:
    for _ in range(_RETRIES):
        if predicate(await coro_factory()):
            return True
    return False


def _kinds(findings: list[OverlapFinding]) -> dict[int, str]:
    return {f.pr_number: f.kind for f in findings}


@pytest.mark.asyncio
async def test_eval_same_file_edits_flagged_as_conflict():
    candidates = [
        _candidate(
            7,
            "Add rate limiting to auth middleware",
            "Throttles login attempts per IP inside the auth middleware chain.",
            ["src/auth/middleware.py", "tests/test_middleware.py"],
        )
    ]
    assert await _passes(
        lambda: _detect(
            "Refactor session token validation in auth middleware",
            "Rewrites the token-checking flow in the middleware entry point.",
            ["src/auth/middleware.py"],
            candidates,
        ),
        lambda f: _kinds(f).get(7) in ("merge_conflict", "both"),
    )


@pytest.mark.asyncio
async def test_eval_same_goal_different_files_flagged_as_duplicate():
    candidates = [
        _candidate(
            8,
            "Retry failed HTTP requests with backoff",
            "Wraps outgoing requests in a retry loop with exponential backoff.",
            ["src/net/retry.py"],
        )
    ]
    assert await _passes(
        lambda: _detect(
            "Add retry with exponential backoff to HTTP client",
            "Adds automatic retries with exponential backoff to the HTTP client.",
            ["src/http/client.py"],
            candidates,
        ),
        lambda f: _kinds(f).get(8) in ("duplicate_effort", "both"),
    )


@pytest.mark.asyncio
async def test_eval_incidental_lockfile_overlap_not_flagged():
    candidates = [
        _candidate(
            9,
            "Add dark mode toggle to settings page",
            "New theme switcher in the UI settings, persisted per user.",
            ["src/ui/theme.py", "package-lock.json"],
        )
    ]
    assert await _passes(
        lambda: _detect(
            "Fix pagination bug in dashboard results table",
            "Off-by-one in the results table pager dropped the last page.",
            ["src/dashboard/table.py", "package-lock.json"],
            candidates,
        ),
        lambda f: 9 not in _kinds(f),
    )
