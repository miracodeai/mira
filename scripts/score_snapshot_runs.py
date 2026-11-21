"""Score completed snapshot runs with a pinned upstream Martian checkout.

Tracked copy of the local scoring harness so the five-PR benchmark is
reproducible from the repository (see ``scripts/run_benchmark_sample.py``).

Requires the upstream benchmark's Python dependencies. Golden answers are used
only here, after reviews finish. Every judge response is cached for auditing and
resuming; use a fresh output directory to obtain independent judgments.
Example: --benchmark /tmp/code-review-benchmark --inputs /tmp/cases
--run baseline=/tmp/baseline --run candidate=/tmp/candidate --output /tmp/scores
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_COMMIT_SUFFIX_RE = re.compile(r"-([0-9a-f]{40})$")


def _benchmark_revision(path):
    """Upstream commit of the pinned benchmark checkout.

    A git clone answers ``rev-parse``; a tarball extracted from GitHub has no
    ``.git`` but carries the commit in its directory name
    (``code-review-benchmark-<sha>``).
    """
    try:
        return subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (subprocess.CalledProcessError, OSError):
        match = _COMMIT_SUFFIX_RE.search(Path(path).resolve().name)
        if not match:
            raise ValueError(
                f"{path} is neither a git checkout nor a code-review-benchmark-<sha> directory"
            ) from None
        return match.group(1)


def parse_json(raw):
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


async def run(args):
    bench = args.benchmark / "offline"
    sys.path.insert(0, str(bench))
    if args.dependency_path:
        sys.path.append(str(args.dependency_path))
    from analysis.score_profiles import _load_golden_categories, score_tools
    from code_review_benchmark.step2_5_dedup_candidates import DEDUP_PROMPT, _parse_groups_response
    from code_review_benchmark.step3_judge_comments import JUDGE_PROMPT, evaluate_review

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from mira.config import LLMConfig
    from mira.llm.provider import LLMProvider

    selection = json.loads((args.inputs / "selection.json").read_text())
    revision = _benchmark_revision(args.benchmark)
    if revision != selection["benchmark_commit"]:
        raise ValueError(
            f"Benchmark revision {revision!r} differs from the pinned selection "
            f"{selection['benchmark_commit']!r}"
        )
    datasets = json.loads((bench / "results/benchmark_data.json").read_text())
    published = json.loads(
        (bench / "results" / args.published_judge / "evaluations.json").read_text()
    )
    args.output.mkdir(parents=True, exist_ok=True)
    cache_path = args.output / "judge-calls.json"
    calls = json.loads(cache_path.read_text()) if cache_path.exists() else []
    llm = LLMProvider(
        LLMConfig(model=args.judge_model, max_tokens=2048, temperature=0.0, reasoning_effort="off")
    )
    semaphore = asyncio.Semaphore(8)

    async def call(
        prompt, system="You are a precise code review evaluator. Always respond with valid JSON."
    ):
        for row in calls:
            if (
                row["prompt"] == prompt
                and row["system"] == system
                and row.get("model", args.judge_model) == args.judge_model
            ):
                return parse_json(row["raw_response"])
        async with semaphore:
            raw = await llm.complete(
                [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                json_mode=True,
            )
            calls.append(
                {"prompt": prompt, "system": system, "model": args.judge_model, "raw_response": raw}
            )
            cache_path.write_text(json.dumps(calls, indent=2))
            return parse_json(raw)

    class Judge:
        async def match_comment(self, golden_comment, candidate):
            return await call(
                JUDGE_PROMPT.format(golden_comment=golden_comment, candidate=candidate)
            )

    evaluations, inputs = {}, {}
    repeats = None
    for item in args.run:
        label, location = item.split("=", 1)
        folder = Path(location)
        manifest = json.loads((folder / "run.json").read_text())
        if manifest["selection"] != selection:
            raise ValueError("All runs must use the same selection")
        if repeats is not None and repeats != manifest["repeats"]:
            raise ValueError("All runs must have the same number of repeats")
        repeats = manifest["repeats"]
        for trial in range(1, repeats + 1):
            for case in selection["cases"]:
                key = f"{case['case']}-r{trial}"
                status = json.loads((folder / key / "status.json").read_text())
                if status["exit_code"]:
                    raise ValueError(f"Review failed: {label}/{key}; do not silently omit it")
                report = json.loads((folder / key / "result.json").read_text())
                candidates = [
                    f"{c['title']}\n{c['path']}:{c['line']}\n{c['body']}"
                    for c in report["result"]["comments"]
                ]
                groups = [[i] for i in range(len(candidates))]
                if len(candidates) > 1:
                    prompt = DEDUP_PROMPT.format(
                        candidates="\n".join(f"{i}. {c}" for i, c in enumerate(candidates))
                    )
                    response = await call(
                        prompt,
                        "You group duplicate code review comments. Always respond with valid JSON only.",
                    )
                    groups = _parse_groups_response(json.dumps(response), len(candidates))
                    if groups is None:
                        raise ValueError("Invalid deduplication response")
                verdict = await evaluate_review(
                    Judge(), datasets[case["url"]]["golden_comments"], candidates, groups
                )
                if verdict["errors_count"]:
                    for err in verdict.get("errors", [])[:3]:
                        print("judge error:", str(err.get("error", err))[:500], file=sys.stderr)
                    raise ValueError(f"Judge failed: {label}/{key}")
                evaluations.setdefault(key, dict(published[case["url"]]))[label] = verdict
                inputs.setdefault(key, {})[label] = {
                    "candidates": candidates,
                    "dedup_groups": groups,
                    "seconds": report["duration_seconds"],
                    "manifest": report["manifest"],
                    "case": case,
                }
                (args.output / "progress.json").write_text(
                    json.dumps({"evaluations": evaluations, "inputs": inputs}, indent=2)
                )
                print(label, key, {k: verdict[k] for k in ("tp", "fp", "fn")}, flush=True)
    categories = _load_golden_categories(bench / "results")
    result = {
        "selection": selection,
        "judge_model": args.judge_model,
        "published_judge": args.published_judge,
        "method": "Upstream pairwise matching, deduplication and core/F2. Published competitor results repeat per Mira trial; competitors were not rerun.",
        "upstream_hashes": {
            str(p.relative_to(bench)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                bench / "code_review_benchmark/step3_judge_comments.py",
                bench / "code_review_benchmark/step2_5_dedup_candidates.py",
                bench / "analysis/score_profiles.py",
            ]
        },
        "scores": score_tools(evaluations, categories, "core", 2.0),
        "evaluations": evaluations,
        "inputs": inputs,
        "judge_usage_this_invocation": llm.usage,
    }
    (args.output / "scores.json").write_text(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument(
        "--run", action="append", required=True, help="unique-label=completed-run-directory"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dependency-path", type=Path)
    parser.add_argument("--judge-model", default="anthropic/claude-sonnet-4.5")
    parser.add_argument("--published-judge", default="anthropic_claude-sonnet-4-5-20250929")
    parser.add_argument("--prompt-key", action="store_true")
    args = parser.parse_args()
    labels = [x.split("=", 1)[0] for x in args.run]
    if any("=" not in x for x in args.run) or len(labels) != len(set(labels)):
        parser.error("runs must have unique labels and use label=path")
    if args.prompt_key:
        os.environ["OPENROUTER_API_KEY"] = getpass.getpass("OpenRouter credential (hidden): ")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
