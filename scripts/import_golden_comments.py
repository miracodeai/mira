#!/usr/bin/env python
"""Import Martian's published golden comments into our ground_truth.json.

The benchmark publishes human-verified findings for all 50 offline PRs at
github.com/withmartian/code-review-benchmark. Each golden entry already
carries the exact PR URL it was reviewed against, so this is a transcription,
not a labeling pass.

    uv run python scripts/import_golden_comments.py            # uses vendored copies
    uv run python scripts/import_golden_comments.py --refresh  # refetch from GitHub
    uv run python scripts/import_golden_comments.py --no-sha   # skip head_sha pinning

The bench versions monthly. We pin a commit SHA and vendor the raw JSONs under
tests/benchmarks/golden/ so a re-import is a deliberate act, not a silent drift.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.request
from pathlib import Path

PINNED_SHA = "279f279d6ef472a32f8055bae78b29ab8c40ece0"
RAW_BASE = f"https://raw.githubusercontent.com/withmartian/code-review-benchmark/{PINNED_SHA}/offline/golden_comments"

# golden file -> (our short repo key, language)
SOURCES = {
    "sentry.json": ("sentry", "python"),
    "grafana.json": ("grafana", "go"),
    "keycloak.json": ("keycloak", "java"),
    "discourse.json": ("discourse", "ruby"),
    "cal_dot_com.json": ("calcom", "typescript"),
}

SEVERITY_MAP = {
    "critical": "blocker",
    "high": "blocker",
    "medium": "warning",
    "low": "suggestion",
}

_SECURITY_RE = re.compile(
    r"inject|xss|csrf|ssrf|auth|origin|clickjack|secret|crypto|saniti|escap|"
    r"privilege|bypass|token|password|sql\b",
    re.I,
)
_PERF_RE = re.compile(r"perform|n\+1|latency|slow|o\(n|memory leak|throughput", re.I)

ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = ROOT / "tests" / "benchmarks" / "golden"
GROUND_TRUTH = ROOT / "tests" / "benchmarks" / "ground_truth.json"


def map_severity(s: str) -> str:
    return SEVERITY_MAP.get((s or "").strip().lower(), "warning")


def guess_category(text: str) -> str:
    if _SECURITY_RE.search(text or ""):
        return "security"
    if _PERF_RE.search(text or ""):
        return "performance"
    return "bug"


def make_slug(repo: str, pr_num: str, idx: int) -> str:
    return f"{repo}-{pr_num}-{idx}"


def _pr_num(url: str) -> str:
    m = re.search(r"/pull/(\d+)", url)
    return m.group(1) if m else "0"


def convert(golden_by_file: dict[str, list], head_shas: dict[str, str | None]) -> list[dict]:
    """Build ground_truth.json entries from the raw golden JSONs."""
    out: list[dict] = []
    for filename, entries in golden_by_file.items():
        repo, language = SOURCES[filename]
        for entry in entries:
            url = entry["url"]
            pr_num = _pr_num(url)
            findings = [
                {
                    "id": make_slug(repo, pr_num, i),
                    "path": None,
                    "line_start": None,
                    "line_end": None,
                    "description": c["comment"],
                    "category": guess_category(c["comment"]),
                    "severity": map_severity(c.get("severity", "")),
                }
                for i, c in enumerate(entry.get("comments", []))
            ]
            out.append(
                {
                    "pr_url": url,
                    "head_sha": head_shas.get(url),
                    "language": language,
                    "source": "martian-bench",
                    "pr_title": entry.get("pr_title", ""),
                    "findings": findings,
                }
            )
    return out


def _fetch(url: str, token: str | None = None) -> bytes:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"token {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def load_golden(refresh: bool) -> dict[str, list]:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    result: dict[str, list] = {}
    for filename in SOURCES:
        vendored = GOLDEN_DIR / filename
        if refresh or not vendored.exists():
            data = _fetch(f"{RAW_BASE}/{filename}")
            vendored.write_bytes(data)
            print(f"fetched {filename}")
        result[filename] = json.loads(vendored.read_text())
    return result


def fetch_head_shas(golden_by_file: dict[str, list]) -> dict[str, str | None]:
    token = os.environ.get("GITHUB_TOKEN")
    shas: dict[str, str | None] = {}
    for entries in golden_by_file.values():
        for entry in entries:
            url = entry["url"]
            m = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
            if not m:
                shas[url] = None
                continue
            owner, repo, num = m.groups()
            api = f"https://api.github.com/repos/{owner}/{repo}/pulls/{num}"
            try:
                data = json.loads(_fetch(api, token))
                shas[url] = (data.get("head") or {}).get("sha")
            except Exception as exc:  # noqa: BLE001
                print(f"  head_sha fetch failed for {url}: {exc}")
                shas[url] = None
    return shas


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="refetch golden JSONs from GitHub")
    parser.add_argument(
        "--no-sha", action="store_true", help="skip head_sha pinning (no API calls)"
    )
    args = parser.parse_args()

    golden = load_golden(refresh=args.refresh)
    head_shas = {} if args.no_sha else fetch_head_shas(golden)
    entries = convert(golden, head_shas)

    GROUND_TRUTH.write_text(json.dumps(entries, indent=2) + "\n")
    findings = sum(len(e["findings"]) for e in entries)
    print(f"wrote {len(entries)} PRs / {findings} findings -> {GROUND_TRUTH.relative_to(ROOT)}")
    by_lang: dict[str, int] = {}
    for e in entries:
        by_lang[e["language"]] = by_lang.get(e["language"], 0) + 1
    print("PRs per language:", by_lang)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
