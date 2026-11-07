#!/usr/bin/env python
"""Prepare pinned PR snapshots from the Martian Code Review Bench for replay.

Picks entries from each repository's golden file in the pinned upstream
checkout (so the selection is fixed by rule, never by result), then fetches
for each PR its mirror metadata, unified diff and source tarball at the
head commit from GitHub, and extracts the tarball into a snapshot directory
the replay harness can serve files from. Writes ``selection.json`` in the
shape ``run_benchmark_sample.py`` and ``score_snapshot_runs.py`` expect.

    uv run python scripts/prepare_benchmark_inputs.py \
        --benchmark /tmp/code-review-benchmark-<sha> \
        --output /tmp/mira-twenty-pr-benchmark/inputs \
        --entries 1,2,4,5

Unauthenticated GitHub API access allows 60 requests per hour; each PR costs
two (metadata and diff), tarballs come from codeload and are not counted.
Set GITHUB_TOKEN to lift the limit. Downloads are resumable: a PR whose
files already exist is skipped.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

LANGUAGE_BY_GOLDEN_FILE = {
    "cal_dot_com.json": ("calcom", "typescript"),
    "discourse.json": ("discourse", "ruby"),
    "grafana.json": ("grafana", "go"),
    "keycloak.json": ("keycloak", "java"),
    "sentry.json": ("sentry", "python"),
}
_COMMIT_SUFFIX_RE = re.compile(r"-([0-9a-f]{40})$")
_USER_AGENT = "mira-benchmark-prepare"


def _benchmark_commit(path: Path) -> str:
    try:
        import subprocess

        return subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        match = _COMMIT_SUFFIX_RE.search(path.resolve().name)
        if not match:
            raise SystemExit(
                f"{path} is neither a git checkout nor a code-review-benchmark-<sha> dir"
            ) from None
        return match.group(1)


def _request(url: str, accept: str | None = None) -> bytes:
    headers = {"User-Agent": _USER_AGENT}
    if accept:
        headers["Accept"] = accept
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(6):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                reset = exc.headers.get("X-RateLimit-Reset")
                wait = max(5.0, float(reset) - time.time() + 2) if reset else 60.0 * (attempt + 1)
                print(f"rate limited on {url}; sleeping {wait:.0f}s", flush=True)
                time.sleep(min(wait, 3600))
                continue
            if exc.code >= 500:
                time.sleep(10 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            time.sleep(10 * (attempt + 1))
    raise SystemExit(f"giving up on {url}")


def _download(url: str, target: Path, accept: str | None = None) -> None:
    if target.exists():
        return
    data = _request(url, accept)
    tmp = target.with_suffix(target.suffix + ".part")
    tmp.write_bytes(data)
    tmp.rename(target)


def _extract(archive: Path, snapshot: Path) -> None:
    if snapshot.exists():
        return
    temp = snapshot.parent / "extracted"
    if temp.exists():
        shutil.rmtree(temp)
    temp.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(temp, filter="data")
    roots = list(temp.iterdir())
    if len(roots) != 1:
        raise SystemExit(f"{archive} did not extract to a single root: {roots}")
    roots[0].rename(snapshot)
    temp.rmdir()


def prepare_case(case: dict, dataset: dict, out: Path) -> dict:
    entry = dataset[case["url"]]
    mirror = next((r["pr_url"] for r in entry["reviews"] if r["tool"] == "augment"), None)
    if not mirror:
        raise SystemExit(f"no mirror PR for {case['url']}")
    owner, repository, _, number = mirror.split("github.com/", 1)[1].split("/")
    folder = out / case["case"]
    folder.mkdir(parents=True, exist_ok=True)
    endpoint = f"https://api.github.com/repos/{owner}/{repository}/pulls/{number}"
    _download(endpoint, folder / "metadata.json")
    metadata = json.loads((folder / "metadata.json").read_text())
    if metadata["html_url"] != mirror:
        raise SystemExit(f"{case['case']}: metadata html_url {metadata['html_url']} != {mirror}")
    _download(endpoint, folder / "pr.diff", "application/vnd.github.diff")
    head = metadata["head"]
    full_name = (head.get("repo") or metadata["base"]["repo"])["full_name"]
    _download(
        f"https://codeload.github.com/{full_name}/tar.gz/{head['sha']}", folder / "tree.tar.gz"
    )
    _extract(folder / "tree.tar.gz", folder / "snapshot")
    paths = {
        "snapshot": str(folder / "snapshot"),
        "metadata": str(folder / "metadata.json"),
        "diff": str(folder / "pr.diff"),
        "tree_archive": str(folder / "tree.tar.gz"),
    }
    (folder / "input.json").write_text(json.dumps(paths, indent=2))
    files = sum(1 for _ in (folder / "snapshot").rglob("*"))
    print(
        json.dumps(
            {
                "case": case["case"],
                "head": head["sha"][:10],
                "archive_bytes": (folder / "tree.tar.gz").stat().st_size,
                "snapshot_entries": files,
            }
        ),
        flush=True,
    )
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--benchmark", type=Path, required=True, help="pinned upstream checkout")
    parser.add_argument("--output", type=Path, required=True, help="inputs directory to create")
    parser.add_argument(
        "--entries",
        default="1,2,4,5",
        help="1-based positions in each repository's golden file (default 1,2,4,5: "
        "the four around the third entry used as the development sample)",
    )
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--split", default="held-out", help="label recorded per case")
    args = parser.parse_args()

    offline = args.benchmark / "offline"
    dataset = json.loads((offline / "results" / "benchmark_data.json").read_text())
    positions = [int(x) for x in args.entries.split(",") if x.strip()]
    cases: list[dict] = []
    for golden_file, (short, language) in LANGUAGE_BY_GOLDEN_FILE.items():
        entries = json.loads((offline / "golden_comments" / golden_file).read_text())
        for pos in positions:
            if pos < 1 or pos > len(entries):
                raise SystemExit(f"{golden_file} has no entry {pos}")
            entry = entries[pos - 1]
            url = entry.get("pr_url") or entry.get("url")
            if not url:
                url = next(v for v in entry.values() if isinstance(v, str) and v.startswith("http"))
            if url not in dataset:
                raise SystemExit(f"{url} from {golden_file} is not in benchmark_data.json")
            cases.append(
                {"case": f"{short}-{pos}", "url": url, "language": language, "split": args.split}
            )

    args.output.mkdir(parents=True, exist_ok=True)
    selection = {
        "rule": (
            f"Entries {args.entries} of each of the five repositories in the pinned upstream "
            "golden-file order. Fixed before reviews; no selection by findings or competitor "
            "performance."
        ),
        "benchmark_commit": _benchmark_commit(args.benchmark),
        "cases": cases,
    }
    (args.output / "selection.json").write_text(json.dumps(selection, indent=2))
    print(json.dumps({"cases": len(cases), "output": str(args.output)}), flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        list(pool.map(lambda c: prepare_case(c, dataset, args.output), cases))
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
