"""Replay a pinned GitHub PR snapshot through Mira without any GitHub write capability.

Supply an extracted source snapshot, its original tar archive, PR metadata JSON
and the matching unified diff. Use a fresh output directory for every attempt.
All model roles use the selected model; the index is an empty isolated store.
Every model call is traced to ``call-NNN.json`` next to ``result.json`` so a
review can be audited offline (which tools were called, what was submitted,
what each pipeline stage dropped).

Driven by ``scripts/run_benchmark_sample.py``; usable on its own:

    python scripts/replay_snapshot_review.py --output /tmp/run/case-r1 \
        --model deepseek/deepseek-v4.1-flash-1017 --reasoning-effort medium \
        --max-tokens 16384 --snapshot ... --metadata ... --diff ... --tree-archive ...
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import sys
import tarfile
import time
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source-root", default=str(Path(__file__).resolve().parents[1]))
parser.add_argument("--snapshot", required=True)
parser.add_argument("--metadata", required=True)
parser.add_argument("--diff", required=True)
parser.add_argument("--tree-archive", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--prepare-only", action="store_true")
parser.add_argument("--model", default="anthropic/claude-sonnet-4-6")
parser.add_argument("--max-tokens", type=int, default=4096)
parser.add_argument("--reasoning-effort", default=None)
parser.add_argument("--agentic-max-hops", type=int, default=None)
args = parser.parse_args()
output = Path(args.output).resolve()
output.mkdir(parents=True, exist_ok=True)
if (output / "result.json").exists():
    raise SystemExit("Use a fresh output directory to preserve previous attempts")
# Isolated, empty index per attempt: no learned rules, no production overrides.
os.environ["MIRA_INDEX_DIR"] = str(output / "isolated-indexes")
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, str(Path(args.source_root).resolve() / "src"))

from mira.config import MiraConfig  # noqa: E402
from mira.core.diff_parser import parse_diff  # noqa: E402
from mira.core.engine import ReviewEngine  # noqa: E402
from mira.llm import create_llm  # noqa: E402
from mira.models import PRInfo  # noqa: E402

metadata = json.loads(Path(args.metadata).read_text())
snapshot = Path(args.snapshot).resolve()
diff = Path(args.diff).read_text()
with tarfile.open(args.tree_archive) as archive:
    tree = sorted(member.name.split("/", 1)[1] for member in archive if member.isfile())
with tarfile.open(args.tree_archive) as archive:
    for member in archive:
        if not member.isfile():
            continue
        relative = member.name.split("/", 1)[1]
        local = (snapshot / relative).resolve()
        if not local.is_relative_to(snapshot):
            raise SystemExit("Unsafe snapshot path")
        source = archive.extractfile(member)
        if source is None or not local.is_file() or local.read_bytes() != source.read():
            raise SystemExit(f"Snapshot differs from archive: {relative}")

config = MiraConfig()
config.llm.model = args.model
config.llm.max_tokens = args.max_tokens
config.llm.reasoning_effort = args.reasoning_effort
if args.agentic_max_hops is not None and hasattr(config.review, "agentic_max_hops"):
    config.review.agentic_max_hops = args.agentic_max_hops

pr = PRInfo(
    title=metadata["title"],
    description=metadata["body"],
    base_branch=metadata["base"]["ref"],
    head_branch=metadata["head"]["ref"],
    head_sha=metadata["head"]["sha"],
    url=metadata["html_url"],
    number=metadata["number"],
    owner=metadata["base"]["repo"]["owner"]["login"],
    repo=metadata["base"]["repo"]["name"],
    platform="github",
)

manifest = {
    "pr_url": pr.url,
    "head_sha": pr.head_sha,
    "base_sha": metadata["base"]["sha"],
    "archive_sha256": hashlib.sha256(Path(args.tree_archive).read_bytes()).hexdigest(),
    "source_hashes": {
        p.relative_to(Path(args.source_root) / "src").as_posix(): hashlib.sha256(
            p.read_bytes()
        ).hexdigest()
        for p in sorted((Path(args.source_root) / "src").rglob("*"))
        if p.is_file() and p.suffix in {".py", ".jinja2"}
    },
    "diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
    "diff_files": [f.path for f in parse_diff(diff).files],
    "model": config.llm.model,
    "source_root": str(Path(args.source_root).resolve()),
    "max_tokens": config.llm.max_tokens,
    "reasoning_effort": config.llm.reasoning_effort,
    "agentic_max_hops": getattr(config.review, "agentic_max_hops", None),
    "security_agentic": config.review.security_agentic,
    "self_critique": config.review.self_critique,
    "review_round": 1,
    "source_files": len(tree),
    "limitations": [
        "Empty isolated index; no production learned rules or overrides.",
        "No historical comments or file-history context are replayed.",
        "One stochastic run does not establish a recall improvement.",
    ],
}
(output / "manifest.json").write_text(json.dumps(manifest, indent=2))
if args.prepare_only:
    print(json.dumps({"prepared": True, **manifest}, indent=2))
    sys.exit(0)
if not os.environ.get(config.llm.api_key_env):
    raise SystemExit(f"Missing {config.llm.api_key_env}; no model calls were made.")


class SnapshotProvider:
    """Read-only provider backed by the extracted snapshot (no network)."""

    def __init__(self) -> None:
        self.source_reads: list[dict] = []

    async def get_file_content(self, pr_info, path, ref):
        self.source_reads.append({"path": path, "ref": ref})
        candidate = (snapshot / path).resolve()
        if not candidate.is_relative_to(snapshot) or not candidate.is_file():
            return ""
        return candidate.read_text(errors="replace")

    async def get_repo_tree(self, pr_info, ref):
        return tree

    async def get_file_history(self, pr_info, paths, max_per_file=5):
        return {}


class TracedLLM:
    """Wraps a provider and writes every call (request + response) to disk."""

    def __init__(self) -> None:
        self.inner = create_llm(config.llm)
        self.calls: list[dict] = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def invoke(self, kind, *pos, **kw):
        index = len(self.calls)
        entry = {"kind": kind, "messages": pos[0] if pos else kw.get("messages")}
        self.calls.append(entry)
        started = time.monotonic()
        try:
            result = await getattr(self.inner, kind)(*pos, **kw)
            entry["response"] = result
            return result
        except Exception as exc:
            entry["error_type"] = type(exc).__name__
            raise
        finally:
            entry["duration_seconds"] = round(time.monotonic() - started, 2)
            (output / f"call-{index:03}.json").write_text(json.dumps(entry, indent=2, default=str))

    async def complete(self, *pos, **kw):
        return await self.invoke("complete", *pos, **kw)

    async def complete_agentic(self, *pos, **kw):
        return await self.invoke("complete_agentic", *pos, **kw)

    async def complete_with_tools(self, *pos, **kw):
        return await self.invoke("complete_with_tools", *pos, **kw)

    async def review(self, *pos, **kw):
        return await self.invoke("review", *pos, **kw)

    async def walkthrough(self, *pos, **kw):
        return await self.invoke("walkthrough", *pos, **kw)


async def main() -> None:
    llm = TracedLLM()
    provider = SnapshotProvider()
    engine = ReviewEngine(
        config,
        llm,
        provider=provider,
        dry_run=True,
        indexing_llm=llm,
        security_llm=llm,
    )
    engine._pr_info = pr
    started = time.monotonic()
    result = await engine._review_diff_internal(diff, pr.title, pr.description)
    report = {
        "manifest": manifest,
        "duration_seconds": time.monotonic() - started,
        "index_was_empty": getattr(engine, "_index_was_empty", None),
        "jit_needed": getattr(engine, "_jit_needed", None),
        "result": dataclasses.asdict(result),
        "source_reads": provider.source_reads,
        "model_usage": llm.usage,
    }
    (output / "result.json").write_text(json.dumps(report, indent=2, default=str))
    print(
        json.dumps(
            {
                "findings": len(result.comments),
                "reviewed_files": result.reviewed_files,
                "skipped_paths": result.skipped_paths,
                "failed_chunks": [
                    {"reason": f.reason, "paths": f.paths} for f in result.failed_chunks
                ],
                "security_failures": [f.reason for f in result.security_failures],
                "walkthrough": result.walkthrough is not None,
                "seconds": round(report["duration_seconds"], 1),
                "comments": [
                    {
                        "path": c.path,
                        "line": c.line,
                        "severity": c.severity.name,
                        "confidence": c.confidence,
                        "title": c.title,
                    }
                    for c in result.comments
                ],
            },
            indent=2,
            default=str,
        )
    )


logging.basicConfig(level=logging.INFO)
asyncio.run(main())
