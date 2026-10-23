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
# Root config files, so the repo's per-file ignores, builtins and target version apply.
# shortcut: nested per-directory ruff configs are not fetched; add them if repos rely on them.
CONFIG_FILES = ("pyproject.toml", "ruff.toml", ".ruff.toml")


async def lint_pass(files: list[FileDiff], fetcher) -> list[ReviewComment]:  # type: ignore[no-untyped-def]
    ruff = shutil.which("ruff")
    py = [f for f in files if f.path.endswith(".py")]
    if not py or fetcher is None:
        return []
    if not ruff:
        logger.info("ruff is not installed; skipping the lint pass (install mira-reviewer[serve])")
        return []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            for name in [*CONFIG_FILES, *(f.path for f in py)]:
                content = await fetcher.fetch(name)
                if content:
                    target = Path(tmp, name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
            proc = await asyncio.create_subprocess_exec(
                ruff, "check", "--no-cache", "--exit-zero", "--select", RULES,
                "--output-format", "json", tmp,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )  # fmt: skip
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                raise
            if proc.returncode:
                # Usually a repo ruff config this ruff version can't read.
                logger.warning("ruff exited with code %s; skipping the lint pass", proc.returncode)
                return []
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
        # A repo config's extend-select can add rules; keep only the bug-only set.
        if d.get("code") and not d["code"].startswith(tuple(RULES.split(","))):
            continue
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
