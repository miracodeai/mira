"""Deterministic lint findings: ruff's bug-only rules on the PR's changed Python files.

Only rules that flag real errors without the project's dependencies installed: syntax
errors (E9), invalid comparisons (F63), misplaced statements (F7) and undefined names (F82).
Findings on lines the PR didn't add are ignored. Fails open: no ruff, no findings.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
from pathlib import Path

from mira.models import FileDiff, ReviewComment, Severity
from mira.security.pr_scan import _added_line_hits

logger = logging.getLogger(__name__)

RULES = "E9,F63,F7,F82"


async def lint_pass(files: list[FileDiff], fetcher) -> list[ReviewComment]:  # type: ignore[no-untyped-def]
    ruff = shutil.which("ruff")
    py = [f for f in files if f.path.endswith(".py")]
    if not ruff or not py or fetcher is None:
        return []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for f in py:
                content = await fetcher.fetch(f.path)
                if content:
                    target = Path(tmp, f.path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
            proc = await asyncio.create_subprocess_exec(
                ruff, "check", "--isolated", "--no-cache", "--exit-zero", "--select", RULES,
                "--output-format", "json", tmp,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )  # fmt: skip
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise
            diagnostics = json.loads(stdout or b"[]")
            root = Path(tmp).resolve()
            for d in diagnostics:
                d["path"] = Path(d["filename"]).resolve().relative_to(root).as_posix()
    except Exception as exc:
        logger.warning("Lint pass failed, skipping: %s", exc)
        return []

    added = {f.path: dict(_added_line_hits(f, None)) for f in py}
    comments = []
    for d in diagnostics:
        line = d["location"]["row"]
        code = added.get(d["path"], {}).get(line)
        if code is None:
            continue
        comments.append(
            ReviewComment(
                path=d["path"],
                line=line,
                end_line=None,
                severity=Severity.WARNING,
                category="bug",
                title=d["message"],
                body=f"`ruff {d['code']}`: {d['message']}. Found by running ruff on the changed file.",
                confidence=1.0,
                suggestion=None,
                existing_code=code,
                source_pass="lint",
            )
        )
    return comments
