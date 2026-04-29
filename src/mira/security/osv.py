"""OSV.dev (Open Source Vulnerabilities) batch client.

We only call ``/v1/querybatch`` — given a list of (ecosystem, package, version)
tuples, returns the vulnerabilities affecting *only* those exact entries. We
never pull the full advisory database; everything is request-scoped to the
packages the customer's manifests actually declare.

Reference: https://google.github.io/osv.dev/post-v1-querybatch/
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_OSV_URL = "https://api.osv.dev/v1/querybatch"
_OSV_VULN_URL = "https://api.osv.dev/v1/vulns"

# Mira's internal ecosystem keys → OSV.dev's ecosystem names.
# Docker images aren't really packages in the OSV sense; we skip them.
_ECOSYSTEM_MAP: dict[str, str] = {
    "npm": "npm",
    "pip": "PyPI",
    "go": "Go",
    "rust": "crates.io",
}

# Stripping common version constraint operators. OSV.dev wants a concrete
# version string; for constraints like "^4.18.0" or ">=2.31.0" we use the
# bare version number and let OSV's range matching do the work.
_VERSION_PREFIX = re.compile(r"^[\^~=<>!]+\s*")


def normalize_version(raw: str) -> str:
    """Strip leading constraint operators so "^4.18.0" becomes "4.18.0"."""
    if not raw:
        return ""
    cleaned = _VERSION_PREFIX.sub("", raw.strip()).strip()
    # Take the first whitespace-separated token to handle "==2.31.0,<3.0".
    cleaned = cleaned.split(",", 1)[0].split(" ", 1)[0].strip()
    return cleaned


def osv_ecosystem(mira_kind: str) -> str | None:
    return _ECOSYSTEM_MAP.get(mira_kind)


@dataclass
class VulnEntry:
    """A single vulnerability affecting a specific (ecosystem, package, version).

    Mirrors the relevant subset of the OSV advisory format.
    """

    cve_id: str
    summary: str
    severity: str  # "critical" | "high" | "moderate" | "low" | "unknown"
    advisory_url: str
    fixed_in: str  # comma-separated list of fix versions, or ""


def _normalize_severity(adv: dict[str, Any]) -> str:
    """Extract a normalized severity from an OSV advisory.

    OSV stores severity in two places:
      - ``database_specific.severity`` (string label like "HIGH")
      - ``severity[]`` (CVSS vectors with type/score)
    """
    db_specific = adv.get("database_specific") or {}
    raw = db_specific.get("severity")
    if isinstance(raw, str) and raw:
        return raw.lower()

    sev_arr = adv.get("severity") or []
    if isinstance(sev_arr, list):
        for entry in sev_arr:
            if not isinstance(entry, dict):
                continue
            score_str = entry.get("score") or ""
            cvss = _cvss_score(score_str)
            if cvss is None:
                continue
            if cvss >= 9.0:
                return "critical"
            if cvss >= 7.0:
                return "high"
            if cvss >= 4.0:
                return "moderate"
            return "low"
    return "unknown"


_CVSS_SCORE_RE = re.compile(r"/AV:\w/.*?(?:^|/)CVSS:?")


def _cvss_score(vector_or_score: str) -> float | None:
    """Best-effort extract a numeric CVSS base score from a string. OSV may
    give us either a vector ("CVSS:3.1/AV:N/...") or a numeric score."""
    if not vector_or_score:
        return None
    try:
        return float(vector_or_score)
    except ValueError:
        pass
    # Look for a 'baseScore' style number in vector strings — most don't include
    # one, but some do; fall back to severity heuristics if missing.
    m = re.search(r"(\d+(?:\.\d+)?)", vector_or_score)
    if m:
        try:
            value = float(m.group(1))
            if 0.0 <= value <= 10.0:
                return value
        except ValueError:
            return None
    return None


def _extract_fixed_versions(adv: dict[str, Any]) -> str:
    """Pull a comma-separated list of "fixed in" versions from an advisory.

    OSV ``ranges`` events may be ``introduced`` / ``fixed`` / ``last_affected``.
    We only care about ``fixed`` entries.
    """
    out: list[str] = []
    for affected in adv.get("affected", []) or []:
        ranges = affected.get("ranges") or []
        if not isinstance(ranges, list):
            continue
        for r in ranges:
            for ev in r.get("events") or []:
                fixed = ev.get("fixed")
                if fixed and isinstance(fixed, str):
                    out.append(fixed)
    # Dedupe while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for v in out:
        if v not in seen:
            deduped.append(v)
            seen.add(v)
    return ", ".join(deduped[:5])


def _advisory_url(adv: dict[str, Any]) -> str:
    """Pick the most user-friendly URL from an advisory's references."""
    refs = adv.get("references") or []
    # Prefer GHSA / advisory references if present.
    for r in refs:
        if not isinstance(r, dict):
            continue
        url = r.get("url", "")
        if r.get("type") == "ADVISORY":
            return url
    if refs and isinstance(refs[0], dict):
        return refs[0].get("url", "")
    cve_id = adv.get("id", "")
    if cve_id.startswith("GHSA"):
        return f"https://github.com/advisories/{cve_id}"
    if cve_id.startswith("CVE"):
        return f"https://nvd.nist.gov/vuln/detail/{cve_id}"
    return f"https://osv.dev/vulnerability/{cve_id}" if cve_id else ""


def parse_advisory(adv: dict[str, Any]) -> VulnEntry:
    return VulnEntry(
        cve_id=str(adv.get("id", "")),
        summary=str(adv.get("summary") or adv.get("details") or "")[:500],
        severity=_normalize_severity(adv),
        advisory_url=_advisory_url(adv),
        fixed_in=_extract_fixed_versions(adv),
    )


@dataclass
class PackageQuery:
    """Single (ecosystem, package, version) query to send to OSV."""

    ecosystem: str  # Mira's internal kind ("npm", "pip", ...)
    name: str
    version: str  # raw version as stored in package_manifests


async def query_batch(
    packages: list[PackageQuery],
    *,
    timeout_s: float = 30.0,
    fetch_details: bool = True,
) -> dict[tuple[str, str, str], list[VulnEntry]]:
    """Batch-query OSV for vulnerabilities affecting the given packages.

    Returns a dict keyed by ``(ecosystem, name, version)`` (using Mira's
    ecosystem names), mapping to the list of advisories affecting that exact
    combination. Packages with no advisories return an empty list.

    The first call returns advisory IDs only; if ``fetch_details`` is True we
    follow up with ``GET /v1/vulns/{id}`` to flesh out summary / severity.
    """
    if not packages:
        return {}

    queries: list[dict[str, Any]] = []
    valid_indexes: list[int] = []  # maps response index → original packages index
    for i, p in enumerate(packages):
        eco = osv_ecosystem(p.ecosystem)
        version = normalize_version(p.version)
        if not eco or not p.name or not version:
            continue
        queries.append(
            {
                "package": {"ecosystem": eco, "name": p.name},
                "version": version,
            }
        )
        valid_indexes.append(i)

    if not queries:
        return {}

    body = {"queries": queries}

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(_OSV_URL, json=body)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("OSV batch query failed: %s", exc)
        return {}

    results: dict[tuple[str, str, str], list[VulnEntry]] = {}
    response_results = data.get("results") or []

    # Collect all advisory IDs we want to expand.
    ids_needed: set[str] = set()
    for entry in response_results:
        for vuln in entry.get("vulns", []) or []:
            vid = vuln.get("id")
            if vid:
                ids_needed.add(vid)

    detail_map: dict[str, dict[str, Any]] = {}
    if fetch_details and ids_needed:
        detail_map = await _fetch_advisory_details(list(ids_needed), timeout_s)

    for response_index, entry in enumerate(response_results):
        if response_index >= len(valid_indexes):
            break
        original = packages[valid_indexes[response_index]]
        key = (original.ecosystem, original.name, original.version)
        vulns: list[VulnEntry] = []
        for stub in entry.get("vulns", []) or []:
            vid = stub.get("id", "")
            adv = detail_map.get(vid, stub)
            vulns.append(parse_advisory(adv))
        results[key] = vulns

    return results


async def _fetch_advisory_details(
    ids: list[str],
    timeout_s: float,
) -> dict[str, dict[str, Any]]:
    """Hydrate advisory IDs into full advisory objects via GET /v1/vulns/{id}.

    OSV's batch endpoint only returns IDs; details require a follow-up request
    per advisory. Limit fan-out to avoid rate-limit headaches.
    """
    out: dict[str, dict[str, Any]] = {}
    # Be polite — small concurrency bound.
    import asyncio

    sem = asyncio.Semaphore(8)

    async def _fetch_one(client: httpx.AsyncClient, vid: str) -> None:
        async with sem:
            try:
                r = await client.get(f"{_OSV_VULN_URL}/{vid}")
                if r.status_code == 200:
                    out[vid] = r.json()
            except httpx.HTTPError as exc:
                logger.debug("Failed to fetch advisory %s: %s", vid, exc)

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        await asyncio.gather(*[_fetch_one(client, vid) for vid in ids])

    return out
