#!/usr/bin/env python
"""Run a fixed Martian benchmark selection through one or more source trees.

One attempt per PR per model per arm, scored together with the pinned upstream
judge so arms (for example `main` and a branch) are compared on identical
inputs under one judge cache. Reviews never see golden answers; scoring
happens afterwards.

    # this checkout only (paths may also come from MIRA_BENCHMARK_INPUTS,
    # MIRA_BENCHMARK_CHECKOUT and MIRA_BENCHMARK_DEPS)
    uv run python scripts/run_benchmark_sample.py --name nov06 --arm candidate=. \
        --inputs /path/to/inputs --benchmark /path/to/code-review-benchmark

    # this checkout against a main worktree, both models, one key prompt
    uv run python scripts/run_benchmark_sample.py --name nov06 \
        --arm main=/path/to/main-worktree --arm candidate=.

    # re-score an existing run directory
    uv run python scripts/run_benchmark_sample.py --name nov06 --score-only

Requires an inputs directory from `scripts/prepare_benchmark_inputs.py`, the
upstream benchmark checkout at the pinned commit and (for scoring) its
Python dependencies on a path, plus the tracked
`scripts/replay_snapshot_review.py` / `scripts/score_snapshot_runs.py`
harness. The OpenRouter credential is read from `OPENROUTER_API_KEY` or
prompted for (hidden); it reaches subprocesses through the environment only.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import getpass
import hashlib
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MODELS = {
    "deepseek": "deepseek/deepseek-v4.1-flash-1017",
    "glm": "z-ai/glm-5.3-flash",
}
# Locations are machine-specific, so they come from flags or these variables.
ENV_INPUTS = "MIRA_BENCHMARK_INPUTS"  # directory from prepare_benchmark_inputs.py
ENV_BENCHMARK = "MIRA_BENCHMARK_CHECKOUT"  # pinned upstream code-review-benchmark checkout
ENV_DEPS = "MIRA_BENCHMARK_DEPS"  # optional: the upstream's Python dependencies


def _freeze_source(source_root: Path, target: Path) -> dict[str, str]:
    """Copy `src/` so edits made during a long run cannot leak into it."""
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(
        source_root / "src", target / "src", ignore=shutil.ignore_patterns("__pycache__")
    )
    hashes = {}
    for p in sorted((target / "src").rglob("*")):
        if p.is_file() and p.suffix in {".py", ".jinja2", ".json"}:
            hashes[p.relative_to(target / "src").as_posix()] = hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
    return hashes


def _git_sha(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


def _review(
    *,
    out_dir: Path,
    frozen: Path,
    case: dict,
    inputs: Path,
    arm: str,
    model_alias: str,
    reasoning: str,
    max_tokens: int,
    timeout: int,
    env: dict[str, str],
) -> dict:
    model = MODELS[model_alias]
    out = out_dir / f"{case['case']}-r1"
    out.mkdir(parents=True, exist_ok=False)
    source = json.loads((inputs / case["case"] / "input.json").read_text())
    command = [
        sys.executable,
        str(REPO / "scripts" / "replay_snapshot_review.py"),
        "--source-root",
        str(frozen),
        "--output",
        str(out),
        "--model",
        model,
        "--reasoning-effort",
        reasoning,
        "--max-tokens",
        str(max_tokens),
    ]
    for flag in ("snapshot", "metadata", "diff", "tree-archive"):
        command += ["--" + flag, source[flag.replace("-", "_")]]
    started = time.monotonic()
    print(json.dumps({"started": f"{arm}/{model_alias}/{case['case']}"}), flush=True)
    with (out / "execution.log").open("w") as log:
        proc = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            code = 124
    status: dict = {
        "arm": arm,
        "model": model_alias,
        "case": case["case"],
        "trial": 1,
        "exit_code": code,
        "process_seconds": round(time.monotonic() - started, 1),
    }
    result_path = out / "result.json"
    if result_path.exists():
        data = json.loads(result_path.read_text())
        res = data["result"]
        status.update(
            seconds=round(data["duration_seconds"], 1),
            comments=len(res["comments"]),
            titles=[
                f"{c['path'].rsplit('/', 1)[-1]}:{c['line']} {c['title']}" for c in res["comments"]
            ],
            skipped=res.get("skipped_paths", []),
        )
    (out / "status.json").write_text(json.dumps(status, indent=2))
    print(json.dumps(status), flush=True)
    return status


def _score(
    *,
    run_dirs: dict[str, Path],
    scores_dir: Path,
    inputs: Path,
    benchmark: Path,
    deps: Path,
    env: dict,
) -> dict | None:
    scores_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(REPO / "scripts" / "score_snapshot_runs.py"),
        "--benchmark",
        str(benchmark),
        "--dependency-path",
        str(deps),
        "--inputs",
        str(inputs),
        "--output",
        str(scores_dir),
    ]
    for label, folder in run_dirs.items():
        command += ["--run", f"{label}={folder}"]
    print(json.dumps({"started": "judge", "runs": list(run_dirs)}), flush=True)
    with (scores_dir / "execution.log").open("w") as log:
        code = subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT).returncode
    if code != 0:
        print(f"scoring failed (exit {code}); see {scores_dir / 'execution.log'}", file=sys.stderr)
        return None
    return json.loads((scores_dir / "scores.json").read_text())


def _summarize(scores: dict, statuses: list[dict]) -> list[dict]:
    rows = []
    seen: list[tuple[str, str]] = []
    for st in statuses:
        key = (st["arm"], st["model"])
        if key not in seen:
            seen.append(key)
    for arm, alias in seen:
        mine = [s for s in statuses if s["arm"] == arm and s["model"] == alias]
        s = scores["scores"].get(f"{arm}-{alias}", {})
        secs = [r["process_seconds"] for r in mine]
        rows.append(
            {
                "arm": arm,
                "model": alias,
                "model_id": MODELS[alias],
                "precision": s.get("precision"),
                "recall": s.get("recall"),
                "f1": s.get("f1"),
                "tp": s.get("tp"),
                "fp": s.get("fp"),
                "fn": s.get("fn"),
                "median_seconds": round(statistics.median(secs), 1) if secs else None,
                "range_seconds": [round(min(secs), 1), round(max(secs), 1)] if secs else None,
                "comments": {r["case"]: r.get("comments") for r in mine},
                "exit_codes": {r["case"]: r["exit_code"] for r in mine},
            }
        )
    return rows


def _parse_arm(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("arm must be NAME=PATH")
    name, path = value.split("=", 1)
    name = name.strip()
    if not name or "-" in name or "/" in name:
        raise argparse.ArgumentTypeError("arm name must be non-empty without '-' or '/'")
    root = Path(path).expanduser().resolve()
    if not (root / "src" / "mira").is_dir():
        raise argparse.ArgumentTypeError(f"{root} has no src/mira")
    return name, root


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--name", required=True, help="run name, e.g. nov06")
    parser.add_argument(
        "--arm",
        action="append",
        type=_parse_arm,
        default=None,
        help="NAME=PATH of a checkout to review with; repeatable (default: candidate=this repo)",
    )
    parser.add_argument(
        "--models", default="deepseek,glm", help="comma list from: " + ", ".join(MODELS)
    )
    parser.add_argument(
        "--output", type=Path, default=None, help="default: tests/benchmarks/runs/five-pr-<name>"
    )
    parser.add_argument(
        "--inputs",
        type=Path,
        default=os.environ.get(ENV_INPUTS),
        help=f"prepared inputs directory (default: ${ENV_INPUTS})",
    )
    parser.add_argument(
        "--benchmark",
        type=Path,
        default=os.environ.get(ENV_BENCHMARK),
        help=f"pinned upstream checkout (default: ${ENV_BENCHMARK})",
    )
    parser.add_argument(
        "--dependency-path",
        type=Path,
        default=os.environ.get(ENV_DEPS),
        help=f"upstream dependencies for scoring (default: ${ENV_DEPS}, optional)",
    )
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--max-tokens", type=int, default=16384)
    parser.add_argument("--timeout", type=int, default=420, help="seconds per review")
    parser.add_argument("--concurrency", type=int, default=2, help="reviews in flight at once")
    parser.add_argument("--score-only", action="store_true", help="re-score an existing run")
    parser.add_argument("--no-score", action="store_true", help="run reviews only")
    args = parser.parse_args()

    arms: list[tuple[str, Path]] = args.arm or [("candidate", REPO)]
    if len({a for a, _ in arms}) != len(arms):
        parser.error("arm names must be unique")
    aliases = [a.strip() for a in args.models.split(",") if a.strip()]
    unknown = [a for a in aliases if a not in MODELS]
    if unknown:
        parser.error(f"unknown model alias(es): {unknown}")
    if args.inputs is None:
        parser.error(f"--inputs is required (or set {ENV_INPUTS})")
    if args.benchmark is None and not args.no_score:
        parser.error(f"--benchmark is required for scoring (or set {ENV_BENCHMARK})")
    out_root = args.output or (REPO / "tests" / "benchmarks" / "runs" / f"five-pr-{args.name}")

    env = dict(os.environ, PYTHONHASHSEED="0")
    if not env.get("OPENROUTER_API_KEY"):
        env["OPENROUTER_API_KEY"] = getpass.getpass("OpenRouter credential (hidden): ")
    if not env["OPENROUTER_API_KEY"]:
        parser.error("an OpenRouter key is required")

    required_paths = [
        args.inputs / "selection.json",
        REPO / "scripts" / "replay_snapshot_review.py",
        REPO / "scripts" / "score_snapshot_runs.py",
    ]
    if args.benchmark is not None:
        required_paths.append(args.benchmark / "offline")
    for path in required_paths:
        if not path.exists():
            parser.error(f"missing {path}")
    selection = json.loads((args.inputs / "selection.json").read_text())

    statuses: list[dict] = []
    if not args.score_only:
        out_root.mkdir(parents=True, exist_ok=False)
        manifest: dict = {
            "name": args.name,
            "arms": {},
            "models": {a: MODELS[a] for a in aliases},
            "reasoning_effort": args.reasoning_effort,
            "max_tokens": args.max_tokens,
            "timeout_seconds": args.timeout,
            "concurrency": args.concurrency,
            "selection": selection,
            "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        frozen_by_arm: dict[str, Path] = {}
        for arm, root in arms:
            frozen = out_root / arm / "frozen"
            hashes = _freeze_source(root, frozen)
            frozen_by_arm[arm] = frozen
            manifest["arms"][arm] = {
                "source_root": str(root),
                "git_sha": _git_sha(root),
                "source_hashes": hashes,
            }
            for alias in aliases:
                (out_root / arm / alias).mkdir(parents=True)
                (out_root / arm / alias / "run.json").write_text(
                    json.dumps(
                        {
                            "selection": selection,
                            "model": MODELS[alias],
                            "repeats": 1,
                            "source_root": str(frozen),
                            "reasoning_effort": args.reasoning_effort,
                            "max_tokens": args.max_tokens,
                            "concurrency": args.concurrency,
                            "strict_verification": False,
                        },
                        indent=2,
                    )
                )
        (out_root / "manifest.json").write_text(json.dumps(manifest, indent=2))

        # Interleave arms and models per case so both arms see the same
        # provider conditions over the run rather than one arm going first.
        jobs = [
            (arm, alias, case)
            for case in selection["cases"]
            for arm, _root in arms
            for alias in aliases
        ]
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [
                pool.submit(
                    _review,
                    out_dir=out_root / arm / alias,
                    frozen=frozen_by_arm[arm],
                    case=case,
                    inputs=args.inputs,
                    arm=arm,
                    model_alias=alias,
                    reasoning=args.reasoning_effort,
                    max_tokens=args.max_tokens,
                    timeout=args.timeout,
                    env=env,
                )
                for arm, alias, case in jobs
            ]
            statuses = [f.result() for f in futures]
        for arm, _root in arms:
            for alias in aliases:
                rows = [s for s in statuses if s["arm"] == arm and s["model"] == alias]
                (out_root / arm / alias / "status.json").write_text(json.dumps(rows, indent=2))
    else:
        for arm, _root in arms:
            for alias in aliases:
                statuses.extend(json.loads((out_root / arm / alias / "status.json").read_text()))

    if args.no_score:
        return 0
    failed = [r for r in statuses if r["exit_code"]]
    if failed:
        print(
            "some reviews failed; the upstream scorer refuses incomplete runs: "
            + ", ".join(
                f"{r['arm']}/{r['model']}/{r['case']} exit {r['exit_code']}" for r in failed
            ),
            file=sys.stderr,
        )
        return 1
    scores = _score(
        run_dirs={
            f"{arm}-{alias}": out_root / arm / alias for arm, _r in arms for alias in aliases
        },
        scores_dir=out_root / "scores",
        inputs=args.inputs,
        benchmark=args.benchmark,
        deps=args.dependency_path,
        env=env,
    )
    if scores is None:
        return 1
    summary = _summarize(scores, statuses)
    (out_root / "summary.json").write_text(json.dumps(summary, indent=2))
    print()
    print(
        f"{'arm':12s} {'model':9s} {'P':>7s} {'R':>7s} {'F1':>6s}  "
        f"{'TP':>3s} {'FP':>3s} {'FN':>3s}  median s"
    )
    for s in summary:
        print(
            f"{s['arm']:12s} {s['model']:9s} {s['precision']:7.1%} {s['recall']:7.1%} "
            f"{100 * s['f1']:6.1f}  {s['tp']:3d} {s['fp']:3d} {s['fn']:3d}  {s['median_seconds']}"
        )
    print(f"\nartifacts: {out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
