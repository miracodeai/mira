"""Benchmark runner: review each fixture PR end-to-end, judge, aggregate.

Hits live GitHub + the LLM provider — costs real money. Artifacts land in
``tests/benchmarks/runs/`` so any two runs can be diffed offline without
re-running.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import subprocess
import time
from pathlib import Path

from mira.config import FilterConfig, LLMConfig, MiraConfig, ReviewConfig, load_config
from mira.core.engine import ReviewEngine
from mira.llm.provider import LLMProvider
from mira.providers.github import GitHubProvider

from .judge import JudgeResult, aggregate, judge_pr

logger = logging.getLogger(__name__)

GROUND_TRUTH_PATH = Path(__file__).parent / "ground_truth.json"
RUNS_DIR = Path(__file__).parent / "runs"

DEFAULT_JUDGE_MODEL = "anthropic/claude-sonnet-4-6"


def hermetic_config() -> MiraConfig:
    """Config for a reproducible benchmark: shipping-default review behavior.

    Keeps llm/provider settings (model, endpoint, keys come from env) but
    resets filter + review to defaults so a local dashboard DB override —
    e.g. a stale ``confidence_threshold: 1.0`` that silently drops every
    calibrated security-pass finding — can't skew the score.
    """
    try:
        base = load_config()
    except Exception:
        base = MiraConfig()
    return MiraConfig(
        llm=base.llm,
        provider=base.provider,
        database=base.database,
        filter=FilterConfig(),
        review=ReviewConfig(),
    )


def load_fixtures(source: str = "all") -> list[dict]:
    fixtures = json.loads(GROUND_TRUTH_PATH.read_text())
    if source != "all":
        fixtures = [fx for fx in fixtures if fx.get("source") == source]
    return fixtures


def _git_sha() -> str:
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=Path(__file__).parent,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


def _judge_provider(judge_model: str) -> LLMProvider:
    # Fresh default config on purpose: judging must never inherit the
    # experiment's review/indexing/reasoning settings.
    return LLMProvider(LLMConfig(model=judge_model, temperature=0.0))


async def _run_one(
    fixture: dict,
    config: MiraConfig,
    judge_llm: LLMProvider,
    semaphore: asyncio.Semaphore,
) -> dict:
    async with semaphore:
        llm = LLMProvider(config.llm)
        provider = GitHubProvider(token=os.environ["GITHUB_TOKEN"])
        engine = ReviewEngine(config=config, llm=llm, provider=provider, dry_run=True)

        start = time.monotonic()
        error: str | None = None
        comments = []
        audit: list[dict] = []
        tokens = 0
        try:
            result = await engine.review_pr(fixture["pr_url"])
            comments = result.comments
            audit = result.audit
            tokens = result.token_usage.get("total_tokens", 0)
        except Exception as exc:
            logger.warning("Review failed for %s: %s", fixture["pr_url"], exc)
            error = str(exc)
        duration_s = time.monotonic() - start

        if error is None:
            judgement = await judge_pr(fixture, comments, judge_llm)
        else:
            # A crashed review misses everything it was supposed to find.
            judgement = JudgeResult(
                fn=len(fixture["findings"]), missed=[f["id"] for f in fixture["findings"]]
            )

    return {
        "fixture": fixture,
        "judgement": judgement,
        "comments": [
            {
                "path": c.path,
                "line": c.line,
                "severity": c.severity.name,
                "category": c.category,
                "title": c.title,
                "body": c.body,
                "confidence": c.confidence,
                "source_pass": c.source_pass,
            }
            for c in comments
        ],
        "audit": audit,
        "duration_s": round(duration_s, 1),
        "tokens": tokens,
        "error": error,
    }


async def run_benchmark(
    source: str = "all",
    config: MiraConfig | None = None,
    judge_model: str = DEFAULT_JUDGE_MODEL,
    concurrency: int = 5,
    label: str = "",
    limit: int = 0,
    only: str = "",
    ensemble: int = 0,
    per_language: int = 0,
) -> dict:
    """Run the full benchmark once. Returns the report dict (also written to disk)."""
    if not os.environ.get("GITHUB_TOKEN"):
        raise SystemExit("GITHUB_TOKEN is required (reviews fetch live PR diffs)")

    fixtures = load_fixtures(source)
    if only:
        needles = [s for s in only.split(",") if s]
        fixtures = [fx for fx in fixtures if any(s in fx["pr_url"] for s in needles)]
    if per_language:
        seen: dict[str, int] = {}
        balanced = []
        for fx in fixtures:
            lang = fx.get("language", "?")
            if seen.get(lang, 0) < per_language:
                balanced.append(fx)
                seen[lang] = seen.get(lang, 0) + 1
        fixtures = balanced
    if limit:
        fixtures = fixtures[:limit]
    if config is None:
        config = hermetic_config()
    if ensemble:
        config.review.ensemble_runs = ensemble

    judge_llm = _judge_provider(judge_model)
    semaphore = asyncio.Semaphore(concurrency)

    rows = await asyncio.gather(*(_run_one(fx, config, judge_llm, semaphore) for fx in fixtures))

    scores = aggregate([(row["fixture"], row["judgement"]) for row in rows])
    durations = sorted(row["duration_s"] for row in rows)
    median_s = durations[len(durations) // 2] if durations else 0.0

    report = {
        "label": label,
        "git_sha": _git_sha(),
        "timestamp": time.strftime("%Y%m%d-%H%M%S"),
        "source": source,
        "judge_model": judge_model,
        "config": config.model_dump(),
        "scores": scores,
        "median_duration_s": median_s,
        "prs": [
            {
                "pr_url": row["fixture"]["pr_url"],
                "language": row["fixture"]["language"],
                "source": row["fixture"].get("source", "?"),
                "tp": row["judgement"].tp,
                "fp": row["judgement"].fp,
                "fn": row["judgement"].fn,
                "matched": row["judgement"].matched,
                "missed": row["judgement"].missed,
                "fp_indices": row["judgement"].fp_indices,
                "reasons": row["judgement"].reasons,
                "comments": row["comments"],
                "audit": row["audit"],
                "duration_s": row["duration_s"],
                "tokens": row["tokens"],
                "error": row["error"],
            }
            for row in rows
        ],
    }

    RUNS_DIR.mkdir(exist_ok=True)
    name = f"{report['git_sha']}-{report['timestamp']}"
    if label:
        name += f"-{label}"
    out_path = RUNS_DIR / f"{name}.json"
    out_path.write_text(json.dumps(report, indent=2, default=_jsonify))
    report["artifact"] = str(out_path)
    return report


def _jsonify(obj: object) -> object:
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    raise TypeError(f"not JSON serializable: {type(obj)}")


def format_report(report: dict) -> str:
    """Human-readable score table for terminal output."""
    s = report["scores"]
    lines = [
        f"run: {report['git_sha']} {report['timestamp']}"
        + (f" [{report['label']}]" if report.get("label") else ""),
        f"overall: F1 {s['overall']['f1']}  P {s['overall']['precision']:.1%}  "
        f"R {s['overall']['recall']:.1%}  "
        f"(TP {s['overall']['tp']} / FP {s['overall']['fp']} / FN {s['overall']['fn']})  "
        f"median {report['median_duration_s']}s",
    ]
    for lang, b in s["by_language"].items():
        lines.append(f"  {lang:<12} F1 {b['f1']:>6}  TP {b['tp']} / FP {b['fp']} / FN {b['fn']}")
    errors = [p for p in report["prs"] if p["error"]]
    if errors:
        lines.append(f"  {len(errors)} PR(s) errored — see artifact")
    if report.get("artifact"):
        lines.append(f"artifact: {report['artifact']}")
    return "\n".join(lines)
