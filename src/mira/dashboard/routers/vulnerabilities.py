"""Dashboard vulnerabilities routes"""

from __future__ import annotations

import os

from mira.dashboard.api import (
    OrgVulnerabilityModel,
    PackageSearchHit,
    VulnerabilityModel,
    VulnerabilitySummary,
    _open_store,
    router,
)


@router.get(
    "/api/repos/{owner}/{repo}/vulnerabilities",
    response_model=list[VulnerabilityModel],
)
def get_repo_vulnerabilities(owner: str, repo: str) -> list[VulnerabilityModel]:
    """All open vulnerabilities for a single repo (across all of its packages)."""
    with _open_store(owner, repo) as store:
        rows = store.list_vulnerabilities()
        return [
            VulnerabilityModel(
                package_name=r.package_name,
                ecosystem=r.ecosystem,
                package_version=r.package_version,
                cve_id=r.cve_id,
                summary=r.summary,
                severity=r.severity,
                advisory_url=r.advisory_url,
                fixed_in=r.fixed_in,
                last_seen_at=r.last_seen_at,
            )
            for r in rows
        ]


@router.get("/api/vulnerabilities/summary", response_model=VulnerabilitySummary)
def get_vulnerabilities_summary() -> VulnerabilitySummary:
    """Org-wide vulnerability count by severity, for the dashboard widget."""
    db_url = os.environ.get("DATABASE_URL", "")
    if not (db_url.startswith("postgresql://") or db_url.startswith("postgres://")):
        # SQLite single-repo deployments don't support org-wide aggregation.
        return VulnerabilitySummary()
    from mira.index.pg_store import count_vulnerabilities_org_wide

    counts = count_vulnerabilities_org_wide(db_url)
    return VulnerabilitySummary(
        total=sum(counts.values()),
        critical=counts.get("critical", 0),
        high=counts.get("high", 0),
        moderate=counts.get("moderate", 0),
        low=counts.get("low", 0),
        unknown=counts.get("unknown", 0),
    )


@router.get("/api/vulnerabilities", response_model=list[OrgVulnerabilityModel])
def list_org_vulnerabilities(limit: int = 1000) -> list[OrgVulnerabilityModel]:
    """List every open vulnerability across the org."""
    db_url = os.environ.get("DATABASE_URL", "")
    capped = max(1, min(limit, 5000))
    if db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
        from mira.index.pg_store import list_vulnerabilities_org_wide

        rows = list_vulnerabilities_org_wide(db_url, limit=capped)
    else:
        from mira.index.store import list_vulnerabilities_org_wide_sqlite

        rows = list_vulnerabilities_org_wide_sqlite(limit=capped)
    return [
        OrgVulnerabilityModel(
            owner=r["owner"],
            repo=r["repo"],
            package_name=r["package_name"],
            ecosystem=r["ecosystem"],
            package_version=r["package_version"],
            cve_id=r["cve_id"],
            summary=r["summary"],
            severity=r["severity"],
            advisory_url=r["advisory_url"],
            fixed_in=r["fixed_in"],
            last_seen_at=r.get("last_seen_at") or 0.0,
        )
        for r in rows
    ]


@router.get("/api/packages/search", response_model=list[PackageSearchHit])
def search_packages(
    name: str | None = None,
    version: str | None = None,
    kind: str | None = None,
    is_dev: bool | None = None,
    limit: int = 500,
) -> list[PackageSearchHit]:
    """Find every occurrence of a package/version across the org. Most
    valuable for security incident response ("which repos use lodash@4.17.20
    after this CVE?") and upgrade audits.

    Dedupes by ``(owner, repo, kind, name)`` preferring lockfile rows over
    manifest rows so the same package isn't shown twice (e.g. ``click 8.3.1``
    from ``uv.lock`` plus ``click >=8.1`` from ``pyproject.toml``).
    """
    from mira.index.manifests import _is_lockfile_path

    db_url = os.environ.get("DATABASE_URL", "")
    capped_limit = max(1, min(limit, 2000))
    if db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
        from mira.index.pg_store import search_packages_org_wide

        rows = search_packages_org_wide(
            db_url,
            name=name,
            version=version,
            kind=kind,
            is_dev=is_dev,
            limit=capped_limit,
        )
    else:
        from mira.index.store import search_packages_org_wide_sqlite

        rows = search_packages_org_wide_sqlite(
            name=name,
            version=version,
            kind=kind,
            is_dev=is_dev,
            limit=capped_limit,
        )

    deduped: dict[tuple[str, str, str, str], dict] = {}
    for r in rows:
        # Case-insensitive on name — PyPI normalises `PyJWT`/`pyjwt` to the
        # same package; without this the dropdown shows both spellings.
        key = (r["owner"], r["repo"], r["kind"], r["name"].lower())
        existing = deduped.get(key)
        if existing is None or (
            _is_lockfile_path(r.get("file_path", ""))
            and not _is_lockfile_path(existing.get("file_path", ""))
        ):
            deduped[key] = r
    return [PackageSearchHit(**r) for r in deduped.values()]
