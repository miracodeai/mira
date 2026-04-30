"""Background OSV.dev vulnerability poller.

Reads ``package_manifests`` across the org once an hour, batch-queries OSV.dev
for each `(ecosystem, name, version)` tuple, and writes results into the
``vulnerabilities`` table. Designed to be cheap: typical orgs fit in 1-2 batch
requests per cycle.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from collections.abc import Iterable

from mira.index.manifests import _is_lockfile_path
from mira.security.osv import (
    PackageQuery,
    osv_ecosystem,
    query_batch,
)


def _prefer_resolved(rows: list[dict]) -> list[dict]:
    """Dedupe by (owner, repo, ecosystem, name) preferring lockfile entries.

    Manifests record version *constraints* like ``>=1.30`` — sending those
    to OSV produces false positives because OSV interprets them as concrete
    versions and matches against everything in that range. Lockfiles record
    the *resolved* version, which is what we actually want to scan against.
    """
    by_key: dict[tuple[str, str, str, str], dict] = {}
    for r in rows:
        key = (r["owner"], r["repo"], r["kind"], r["name"])
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = r
            continue
        # Prefer the row coming from a lockfile path. If both or neither are
        # lockfile rows, keep the first one.
        if _is_lockfile_path(r.get("file_path", "")) and not _is_lockfile_path(
            existing.get("file_path", "")
        ):
            by_key[key] = r
    return list(by_key.values())


logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = int(os.environ.get("MIRA_VULN_POLL_INTERVAL", "3600"))


async def poll_org_wide() -> dict[str, int]:
    """Run a full vulnerability scan across every indexed repo.

    Returns severity counts (e.g. ``{"high": 3, "critical": 1}``) for logging.
    Postgres-only — SQLite per-repo stores can't be enumerated globally.
    """
    db_url = os.environ.get("DATABASE_URL", "")
    if not (db_url.startswith("postgresql://") or db_url.startswith("postgres://")):
        logger.debug("Skipping vuln poll: DATABASE_URL is not Postgres")
        return {}

    from mira.index.pg_store import (
        PgIndexStore,
        list_packages_org_wide,
    )

    rows = list_packages_org_wide(db_url)
    if not rows:
        logger.debug("Vuln poller: no packages to scan")
        return {}

    # Per (owner, repo, ecosystem, name), prefer the lockfile entry (resolved
    # version) over the manifest entry (constraint like ">=1.30") so OSV
    # gets accurate concrete versions to match against.
    rows = _prefer_resolved(rows)

    # Build OSV queries — dedupe by (ecosystem, name, version) so we don't
    # hit the API multiple times for the same tuple appearing in many repos.
    unique_packages: dict[tuple[str, str, str], list[tuple[str, str]]] = defaultdict(list)
    for r in rows:
        if not osv_ecosystem(r["kind"]):
            continue
        key = (r["kind"], r["name"], r["version"])
        unique_packages[key].append((r["owner"], r["repo"]))

    if not unique_packages:
        return {}

    # Build the set of (name, ecosystem, version) tuples each repo currently
    # depends on, so stale rows from previous package versions can be GC'd.
    active_per_repo: dict[tuple[str, str], set[tuple[str, str, str]]] = defaultdict(set)
    for key, repos in unique_packages.items():
        kind, name, version = key
        eco = osv_ecosystem(kind)
        if not eco:
            continue
        for owner, repo in repos:
            active_per_repo[(owner, repo)].add((name, eco, version))

    for (owner, repo), active in active_per_repo.items():
        try:
            PgIndexStore(owner, repo, db_url).prune_stale_vulnerabilities(active)
        except Exception as exc:
            logger.warning("Failed to prune stale vulns for %s/%s: %s", owner, repo, exc)

    queries = [PackageQuery(ecosystem=k[0], name=k[1], version=k[2]) for k in unique_packages]

    logger.info(
        "Vuln poll: querying OSV.dev for %d unique packages across %d repos",
        len(queries),
        len({rr for refs in unique_packages.values() for rr in refs}),
    )

    # OSV's batch endpoint accepts up to 1000 queries; chunk to be safe.
    chunks = _chunk(queries, 1000)
    all_results: dict[tuple[str, str, str], list] = {}
    for batch in chunks:
        results = await query_batch(batch)
        all_results.update(results)

    # Persist per-(repo, package) so each repo's vulnerabilities row stays
    # accurate even if the same package version appears in multiple repos.
    severity_counts: dict[str, int] = defaultdict(int)
    repos_processed: set[tuple[str, str]] = set()

    for key, repos in unique_packages.items():
        ecosystem, name, version = key
        vulns = all_results.get(key, [])
        vuln_dicts = [
            {
                "cve_id": v.cve_id,
                "summary": v.summary,
                "severity": v.severity,
                "advisory_url": v.advisory_url,
                "fixed_in": v.fixed_in,
            }
            for v in vulns
            if v.cve_id
        ]
        for owner, repo in repos:
            store = PgIndexStore(owner, repo, db_url)
            store.replace_vulnerabilities_for_package(name, ecosystem, version, vuln_dicts)
            repos_processed.add((owner, repo))
            for v in vuln_dicts:
                severity_counts[v["severity"]] += 1

    counts = dict(severity_counts)
    if counts:
        logger.info(
            "Vuln poll: found %s across %d repos",
            ", ".join(f"{n} {sev}" for sev, n in sorted(counts.items())),
            len(repos_processed),
        )
    else:
        logger.info("Vuln poll: no open vulnerabilities found")

    return counts


async def poll_repo(owner: str, repo: str) -> dict[str, int]:
    """Scan a single repo's packages immediately. Called after a successful
    indexing run so freshly-added packages get a vuln check without waiting
    for the next hourly tick."""
    db_url = os.environ.get("DATABASE_URL", "")
    if not (db_url.startswith("postgresql://") or db_url.startswith("postgres://")):
        return {}

    from mira.index.pg_store import PgIndexStore

    store = PgIndexStore(owner, repo, db_url)
    pkgs = store.list_manifest_packages()
    if not pkgs:
        return {}

    # Prefer the lockfile entry over the manifest entry for the same package
    # (see _prefer_resolved comment for why).
    deduped = _prefer_resolved(
        [
            {
                "owner": owner,
                "repo": repo,
                "kind": p.kind,
                "name": p.name,
                "version": p.version,
                "file_path": p.file_path,
            }
            for p in pkgs
        ]
    )

    queries = [
        PackageQuery(ecosystem=r["kind"], name=r["name"], version=r["version"])
        for r in deduped
        if osv_ecosystem(r["kind"])
    ]
    if not queries:
        return {}

    # GC vulnerability rows whose (name, ecosystem, version) is no longer in
    # this repo's dep set — covers the manifest→lockfile transition.
    active = {
        (r["name"], osv_ecosystem(r["kind"]) or "", r["version"])
        for r in deduped
        if osv_ecosystem(r["kind"])
    }
    store.prune_stale_vulnerabilities(active)

    results = await query_batch(queries)

    severity_counts: dict[str, int] = defaultdict(int)
    for r in deduped:
        if not osv_ecosystem(r["kind"]):
            continue
        key = (r["kind"], r["name"], r["version"])
        vulns = results.get(key, [])
        vuln_dicts = [
            {
                "cve_id": v.cve_id,
                "summary": v.summary,
                "severity": v.severity,
                "advisory_url": v.advisory_url,
                "fixed_in": v.fixed_in,
            }
            for v in vulns
            if v.cve_id
        ]
        store.replace_vulnerabilities_for_package(r["name"], r["kind"], r["version"], vuln_dicts)
        for v in vuln_dicts:
            severity_counts[v["severity"]] += 1

    return dict(severity_counts)


def _chunk(items: Iterable, size: int) -> list[list]:
    items_list = list(items)
    return [items_list[i : i + size] for i in range(0, len(items_list), size)]


# ── Background loop ──


async def run_forever() -> None:
    """Run poll_org_wide() forever on a fixed interval. Started by the
    FastAPI lifespan in webhooks.py."""
    while True:
        try:
            await poll_org_wide()
        except Exception:
            logger.exception("Vuln poll cycle failed; will retry on next interval")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
