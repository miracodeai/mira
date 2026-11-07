#!/usr/bin/env python
"""Combine scored benchmark runs over disjoint PR selections into one score.

Each run directory comes from ``run_benchmark_sample.py`` and already holds
``scores/scores.json`` (the upstream judge's verdicts for Mira and every
published tool on that run's PRs) and ``manifest.json`` (the selection).
This merges their evaluations, re-scores every tool with the upstream
``score_tools`` over the union, breaks the result down per language, and
collects Mira's timings — without re-running any review or judge call.

    uv run python scripts/combine_benchmark_runs.py \
        --benchmark /tmp/code-review-benchmark-<sha> \
        --run held_out=tests/benchmarks/runs/five-pr-nov06-twenty-held-out \
        --run dev=tests/benchmarks/runs/five-pr-nov06-shipped-default \
        --run rest=tests/benchmarks/runs/five-pr-nov06-rest \
        --mira-key candidate-deepseek --output tests/benchmarks/runs/combined-50
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--dependency-path", type=Path)
    parser.add_argument("--run", action="append", required=True, help="label=run-directory")
    parser.add_argument(
        "--mira-key", default="candidate-deepseek", help="Mira's key inside each run's scores"
    )
    parser.add_argument("--arm", default="candidate", help="arm directory holding status files")
    parser.add_argument("--model", default="deepseek", help="model alias directory")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", default="core")
    parser.add_argument("--beta", type=float, default=2.0)
    args = parser.parse_args()

    bench = args.benchmark / "offline"
    sys.path.insert(0, str(bench))
    if args.dependency_path:
        sys.path.append(str(args.dependency_path))
    from analysis.score_profiles import _load_golden_categories, score_tools

    categories = _load_golden_categories(bench / "results")

    evaluations: dict[str, dict] = {}
    cases: list[dict] = []
    seen_urls: set[str] = set()
    timings: list[dict] = []
    runs_meta = []
    for item in args.run:
        label, location = item.split("=", 1)
        folder = Path(location)
        scores = json.loads((folder / "scores" / "scores.json").read_text())
        manifest = json.loads((folder / "manifest.json").read_text())
        statuses = json.loads((folder / args.arm / args.model / "status.json").read_text())
        status_by_case = {s["case"]: s for s in statuses}
        for case in manifest["selection"]["cases"]:
            if case["url"] in seen_urls:
                raise SystemExit(f"{case['url']} appears in more than one run")
            seen_urls.add(case["url"])
            key = f"{case['case']}-r1"
            per_tool = dict(scores["evaluations"][key])
            if args.mira_key not in per_tool:
                raise SystemExit(f"{label}/{key} has no {args.mira_key} evaluation")
            per_tool["mira"] = per_tool.pop(args.mira_key)
            # Drop any other arm/model of Mira from the same run; one row per PR.
            for k in [k for k in per_tool if k.startswith(("candidate-", "main-"))]:
                per_tool.pop(k)
            evaluations[key] = per_tool
            st = status_by_case[case["case"]]
            timings.append(
                {
                    "case": case["case"],
                    "run": label,
                    "seconds": round(st.get("seconds", st["process_seconds"]), 1),
                    "exit_code": st["exit_code"],
                    "comments": st.get("comments"),
                }
            )
            cases.append({**case, "run": label})
        runs_meta.append(
            {
                "label": label,
                "directory": str(folder),
                "prs": len(manifest["selection"]["cases"]),
                "source_sha": manifest["arms"][args.arm]["git_sha"],
                "model": manifest["models"][args.model],
                "reasoning_effort": manifest["reasoning_effort"],
                "max_tokens": manifest["max_tokens"],
                "selection_rule": manifest["selection"]["rule"],
            }
        )

    overall = score_tools(evaluations, categories, args.profile, args.beta)
    by_language: dict[str, dict] = {}
    for language in sorted({c["language"] for c in cases}):
        keys = {f"{c['case']}-r1" for c in cases if c["language"] == language}
        subset = {k: v for k, v in evaluations.items() if k in keys}
        by_language[language] = score_tools(subset, categories, args.profile, args.beta)

    secs = [t["seconds"] for t in timings]
    result = {
        "profile": args.profile,
        "beta": args.beta,
        "prs": len(cases),
        "runs": runs_meta,
        "scores": overall,
        "by_language": by_language,
        "mira_timing": {
            "median_seconds": round(statistics.median(secs), 1),
            "minimum_seconds": round(min(secs), 1),
            "maximum_seconds": round(max(secs), 1),
            "under_120s": sum(1 for s in secs if s < 120),
            "completed": sum(1 for t in timings if t["exit_code"] == 0),
            "attempted": len(timings),
            "per_case": timings,
        },
        "cases": cases,
        "evaluations": evaluations,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "scores.json").write_text(json.dumps(result, indent=2))

    rows = sorted(overall.items(), key=lambda kv: -kv[1]["f1"])
    print(
        f"{'#':>2s} {'tool':22s} {'P':>7s} {'R':>7s} {'F1':>6s}  {'TP':>3s} {'FP':>3s} {'FN':>3s} PRs"
    )
    for i, (tool, s) in enumerate(rows, 1):
        mark = "  <-- Mira" if tool == "mira" else ""
        print(
            f"{i:2d} {tool:22s} {s['precision']:7.1%} {s['recall']:7.1%} {100 * s['f1']:6.1f}  "
            f"{s['tp']:3d} {s['fp']:3d} {s['fn']:3d} {s['prs']:3d}{mark}"
        )
    print()
    print("Mira by language:")
    for language, table in by_language.items():
        s = table.get("mira")
        if s:
            best = max(
                ((t, v["f1"]) for t, v in table.items() if t != "mira"), key=lambda kv: kv[1]
            )
            print(
                f"  {language:11s} P {s['precision']:6.1%} R {s['recall']:6.1%} F1 {100 * s['f1']:5.1f} "
                f"({s['tp']}/{s['fp']}/{s['fn']}, {s['prs']} PRs)  best other: {best[0]} {100 * best[1]:.1f}"
            )
    t = result["mira_timing"]
    print(
        f"\nMira timing: median {t['median_seconds']} s ({t['minimum_seconds']}-{t['maximum_seconds']}), "
        f"{t['under_120s']}/{t['attempted']} under 120 s, {t['completed']}/{t['attempted']} completed"
    )
    print(f"written: {args.output / 'scores.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
