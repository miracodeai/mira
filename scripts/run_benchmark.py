#!/usr/bin/env python
"""A/B benchmark CLI.

    uv run python scripts/run_benchmark.py --runs 3 --label my-experiment
    uv run python scripts/run_benchmark.py --fixtures martian-bench --runs 5

Each run writes an artifact under tests/benchmarks/runs/. With --runs > 1
the script prints mean and population σ of F1 across runs — the acceptance
rule is mean-of-3 improving on baseline by more than 2σ.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tests"))

from benchmarks.runner import DEFAULT_JUDGE_MODEL, format_report, run_benchmark


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixtures",
        default="all",
        help="all | martian-bench | supplemental (matches the fixture 'source' field)",
    )
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--label", default="")
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--limit", type=int, default=0, help="run only the first N fixtures")
    parser.add_argument(
        "--only", default="", help="only fixtures whose pr_url contains this (comma = OR)"
    )
    parser.add_argument("--ensemble", type=int, default=0, help="override review.ensemble_runs")
    parser.add_argument(
        "--per-language", type=int, default=0, help="take the first N PRs of each language"
    )
    args = parser.parse_args()

    f1s: list[float] = []
    for i in range(args.runs):
        label = f"{args.label}-r{i + 1}" if args.label else f"r{i + 1}"
        report = await run_benchmark(
            source=args.fixtures,
            judge_model=args.judge_model,
            concurrency=args.concurrency,
            label=label,
            limit=args.limit,
            only=args.only,
            ensemble=args.ensemble,
            per_language=args.per_language,
        )
        print(format_report(report))
        print()
        f1s.append(report["scores"]["overall"]["f1"])

    if len(f1s) > 1:
        mean = statistics.mean(f1s)
        sigma = statistics.pstdev(f1s)
        print(f"F1 across {len(f1s)} runs: {[round(x, 1) for x in f1s]}")
        print(f"mean {mean:.2f}  σ {sigma:.2f}  (accept a change only if Δmean > {2 * sigma:.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
