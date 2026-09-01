"""Review-time OSV scan: flag PR-added dependencies with known CVEs.

The background poller (security/poller.py) re-scans indexed repos hourly
after merge; this module closes the merge-time window by checking only the
packages a PR *adds or bumps*, anchored to the exact added diff line.
No LLM involved.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from mira.index.manifests import parse_manifest
from mira.models import FileDiff, ReviewComment, Severity
from mira.security.osv import (
    PackageQuery,
    normalize_version,
    osv_ecosystem,
    query_batch,
)

if TYPE_CHECKING:
    from mira.index.context import SourceFetcher

logger = logging.getLogger(__name__)

_MAX_VULNS_PER_COMMENT = 3


def _added_line_hits(file_diff: FileDiff, needle: str | re.Pattern | None) -> list[tuple[int, str]]:
    """(target line number, line text) of added lines matching `needle`.

    Hunk content includes the @@ header (diff_parser stores str(hunk)) —
    skip it WITHOUT incrementing; target_start is the first body line.

    - `str`: case-insensitive substring match (existing behavior).
    - compiled `re.Pattern`: `needle.search(line)` on the raw added line
      (the pattern carries its own flags, no lowercasing).
    - `None`: every added line.
    """
    if isinstance(needle, str):
        needle_l = needle.lower()

        def _match(line: str) -> bool:
            return needle_l in line.lower()
    elif isinstance(needle, re.Pattern):

        def _match(line: str) -> bool:
            return needle.search(line) is not None
    else:

        def _match(line: str) -> bool:
            return True

    hits: list[tuple[int, str]] = []
    for hunk in file_diff.hunks:
        line_no = hunk.target_start
        for raw in hunk.content.splitlines():
            if raw.startswith("@@"):
                continue
            if raw.startswith("+"):
                if _match(raw[1:]):
                    hits.append((line_no, raw[1:]))
                line_no += 1
            elif raw.startswith("-"):
                continue
            else:
                line_no += 1
    return hits


_SEVERITY_MAP = {
    "critical": Severity.BLOCKER,
    "high": Severity.BLOCKER,
    "moderate": Severity.WARNING,
    "low": Severity.SUGGESTION,
    "unknown": Severity.SUGGESTION,
}


async def scan_manifest_changes(
    manifest_files: list[FileDiff],
    fetcher: SourceFetcher,
    *,
    timeout_s: float = 15.0,
) -> list[ReviewComment]:
    """Flag PR-added packages with known OSV vulnerabilities. Fails open."""
    if not manifest_files:
        return []

    queries: list[PackageQuery] = []
    # (ecosystem, name, version) → (first_line_no, first_line_text)
    hit_map: dict[tuple[str, str, str], tuple[int, str]] = {}

    for f in manifest_files:
        try:
            content = await fetcher.fetch(f.path)
            if not content:
                continue
            packages = parse_manifest(f.path, content)
        except Exception as exc:
            logger.debug("Failed to parse manifest %s: %s", f.path, exc)
            continue

        for pkg in packages:
            hits = _added_line_hits(f, pkg.name)
            if not hits:
                continue
            eco = osv_ecosystem(pkg.kind)
            if eco is None:
                continue
            ver = normalize_version(pkg.version)
            if not ver:
                continue
            key = (eco, pkg.name, ver)
            queries.append(PackageQuery(ecosystem=eco, name=pkg.name, version=ver))
            if key not in hit_map:
                hit_map[key] = (hits[0][0], hits[0][1])

    if not queries:
        return []

    try:
        results = await query_batch(queries, timeout_s=timeout_s)
    except Exception as exc:
        logger.warning("OSV review-time scan failed: %s", exc)
        return []

    if not results:
        return []

    comments: list[ReviewComment] = []
    for q in queries:
        key = (q.ecosystem, q.name, q.version)
        vulns = results.get(key, [])
        if not vulns:
            continue
        line_no, line_text = hit_map[key]

        severity = max(
            (_SEVERITY_MAP.get(v.severity, Severity.SUGGESTION) for v in vulns),
            key=lambda s: int(s),
        )

        n = len(vulns)
        bullets: list[str] = []
        for v in vulns[:_MAX_VULNS_PER_COMMENT]:
            bullet = f"- [{v.cve_id}]({v.advisory_url}) **{v.severity}**: {v.summary}"
            if v.fixed_in:
                bullet += f" — fixed in {v.fixed_in}"
            bullets.append(bullet)

        body = (
            f"OSV.dev reports {n} known vulnerabilit{'y' if n == 1 else 'ies'} "
            f"affecting `{q.name}@{q.version}`, introduced by this PR:\n\n"
        )
        body += "\n".join(bullets)
        if n > _MAX_VULNS_PER_COMMENT:
            body += f"\n\n…and {n - _MAX_VULNS_PER_COMMENT} more — see the OSV links above."

        all_fixed: list[str] = [v.fixed_in for v in vulns if v.fixed_in]
        if all_fixed:
            first_fixed = all_fixed[0]
            body += f"\n\nUpgrade to {first_fixed} or later."

        distinct_fixes = sorted({v for fv in all_fixed for v in fv.split(", ") if v})
        fixes_str = ", ".join(distinct_fixes) if distinct_fixes else "see advisory links"

        agent_prompt = (
            f"In {f.path}, upgrade the dependency {q.name} from {q.version} "
            f"to a non-vulnerable version ({fixes_str}). "
            f"Update any associated lockfile entries."
        )

        comments.append(
            ReviewComment(
                path=f.path,
                line=line_no,
                end_line=None,
                severity=severity,
                category="security",
                title=f"Known vulnerabilities in {q.name}@{q.version}",
                body=body,
                confidence=0.9,
                suggestion=None,
                agent_prompt=agent_prompt,
                existing_code=line_text,
                source_pass="osv",
            )
        )

    return comments
