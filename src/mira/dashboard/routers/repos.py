"""Dashboard repos routes"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Literal

from fastapi import HTTPException, Request
from fastapi import Response as FastAPIResponse

from mira.dashboard import api as _api
from mira.dashboard import queries as _queries
from mira.dashboard.api import (
    BlastRadiusModel,
    BlastRadiusResponse,
    CrossRepoBlastEntry,
    DependencyGraph,
    DependentEdge,
    ExternalRefModel,
    FileModel,
    ImportEdge,
    PackageModel,
    PackageVersionModel,
    PackageVersionSourceModel,
    RepoBaseBranchPolicy,
    RepoDetail,
    RepoListItem,
    ReviewEventModel,
    SymbolModel,
    _open_relationships,
    _open_store,
    _pick_platform_record,
    _require_admin,
    logger,
    router,
)
from mira.index.store import IndexingSuperseded, IndexStore, PackageManifestRow


def _parse_base_branch_policy(raw: str) -> RepoBaseBranchPolicy | None:
    """Parse the stored policy blob; ``None`` when unset or malformed.

    Uses the same parser as the dispatcher, so the dashboard and the
    webhook path judge stored data identically.
    """
    from mira.platforms.base_branch_policy import MODE_DISABLE, MODE_OVERRIDE, parse_repo_policy

    policy = parse_repo_policy(raw)
    if policy is None:
        return None
    mode: Literal["inherit", "override", "disable"] = policy["mode"]
    if mode == MODE_OVERRIDE:
        return RepoBaseBranchPolicy(mode=MODE_OVERRIDE, branches=policy["branches"])
    if mode == MODE_DISABLE:
        return RepoBaseBranchPolicy(mode=MODE_DISABLE)
    return RepoBaseBranchPolicy(mode="inherit")


@router.get("/api/repos", response_model=list[RepoListItem])
def list_repos() -> list[RepoListItem]:
    """List all repos from the registry."""
    return [_queries.repo_list_item(r) for r in _api._app_db.list_repos()]


@router.post("/api/repos/sync")
async def sync_repos(request: Request) -> dict:
    """Reconcile the repos table with actual GitHub App installations.

    Removes repos that are no longer accessible and adds any new ones.
    """
    _require_admin(request)
    app_id = os.environ.get("MIRA_GITHUB_APP_ID", "")
    private_key = os.environ.get("MIRA_GITHUB_PRIVATE_KEY", "")
    if not app_id or not private_key:
        raise HTTPException(status_code=400, detail="GitHub App not configured")

    import asyncio as _asyncio

    from mira.platforms.github.auth import GitHubAppAuth
    from mira.platforms.github.webhook import _count_files_for_repos

    auth = GitHubAppAuth(app_id=app_id, private_key=private_key)

    # Collect repos currently accessible via GitHub App
    actual_repos: set[tuple[str, str]] = set()
    installations_reachable = False
    try:
        installations = await auth.list_installations()
        installations_reachable = True
        for inst in installations:
            inst_id = int(inst.get("id", 0))
            if not inst_id:
                continue
            try:
                repos_list = await auth.list_installation_repos(inst_id)
            except Exception as exc:
                # One installation failing shouldn't poison the whole sync — log
                # and skip. Without this, a stale/revoked installation would
                # cause us to treat the DB as fully empty and wipe it below.
                logger.warning("Skipping installation %s in sync: %s", inst_id, exc)
                continue
            for r in repos_list:
                full_name = str(r.get("full_name", ""))
                if "/" in full_name:
                    owner, repo = full_name.split("/", 1)
                    actual_repos.add((owner, repo))
                    _api._app_db.register_repo(owner, repo, inst_id)
                    _api._app_db.set_repo_visibility(owner, repo, bool(r.get("private", False)))
            # Count files in background
            _asyncio.create_task(_count_files_for_repos(auth, inst_id, repos_list))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to list installations: {exc}") from exc

    # Only delete DB repos when we successfully reached GitHub AND at least one
    # installation returned at least one repo. Treating "App has zero visible
    # installations" as "delete every repo in the DB" is dangerous — a single
    # auth failure, misconfigured App ID, or transient GitHub outage would wipe
    # user data. Require positive confirmation before pruning.
    removed = 0
    if installations_reachable and actual_repos:
        for db_repo in _api._app_db.list_repos():
            # This sync only reconciles GitHub installations — leave GitLab rows alone.
            if db_repo.platform != "github":
                continue
            if (db_repo.owner, db_repo.repo) not in actual_repos:
                _api._app_db.delete_repo(db_repo.owner, db_repo.repo, platform=db_repo.platform)
                removed += 1

    return {
        "synced": len(actual_repos),
        "removed": removed,
        "installations_reachable": installations_reachable,
    }


@router.get("/api/repos/{owner:path}/{repo}/files", response_model=list[FileModel])
def list_files(owner: str, repo: str) -> list[FileModel]:
    """List all indexed files with summaries."""
    with _open_store(owner, repo) as store:
        paths = sorted(store.all_paths())
        summaries = store.get_summaries(paths)

        result: list[FileModel] = []
        for path in paths:
            fs = summaries.get(path)
            if fs is None:
                continue
            result.append(
                FileModel(
                    path=fs.path,
                    language=fs.language,
                    summary=fs.summary,
                    symbols=[
                        SymbolModel(name=s.name, kind=s.kind, signature=s.signature)
                        for s in fs.symbols
                    ],
                    imports=fs.imports,
                    loc=fs.loc,
                )
            )
        return result


@router.get("/api/repos/{owner:path}/{repo}/dependencies", response_model=DependencyGraph)
def get_dependencies(owner: str, repo: str) -> DependencyGraph:
    """Get the dependency graph for a repo."""
    with _open_store(owner, repo) as store:
        paths = sorted(store.all_paths())
        summaries = store.get_summaries(paths)

        imports: list[ImportEdge] = []
        dependents: list[DependentEdge] = []

        for path in paths:
            fs = summaries.get(path)
            if fs is None:
                continue
            for target in fs.imports:
                imports.append(ImportEdge(source=fs.path, target=target))

        for path in paths:
            for dep_path in store.get_dependents(path):
                dependents.append(DependentEdge(path=path, dependent_path=dep_path))

        return DependencyGraph(imports=imports, dependents=dependents)


def _is_known_public(full_name: str) -> bool:
    """True only when the registry records ``owner/repo`` as public; unknown
    visibility counts as private, as it does everywhere else."""
    owner, _, repo = full_name.rpartition("/")
    records = _api._app_db.get_repo_any_platform(owner, repo)
    return bool(records) and _pick_platform_record(records).private is False


@router.get("/api/repos/{owner:path}/{repo}/blast-radius.svg")
def get_blast_radius_svg(owner: str, repo: str, request: Request) -> FastAPIResponse:
    """Render blast radius as an SVG image.

    AuthMiddleware lets this path through without a session so GitHub can embed
    the badge in a README, which makes it world-readable. An anonymous viewer
    therefore gets file names only for a repo *known* to be public, and
    cross-repo dependents only when they are known public too, so neither a
    private repo's structure nor a private repo's name (via its dependency on
    a public one) leaks. A signed-in dashboard user sees the full picture.
    """
    from mira.dashboard.blast_svg import generate_blast_svg

    records = _api._app_db.get_repo_any_platform(owner, repo)
    if not records:
        raise HTTPException(status_code=404, detail=f"Repo {owner}/{repo} not found")
    signed_in = getattr(request.state, "user", None) is not None
    reviewed_private = _pick_platform_record(records).private
    if not signed_in and reviewed_private is not False:
        svg = generate_blast_svg([], [], [], caption="Private repository — sign in to view")
        return FastAPIResponse(
            content=svg,
            media_type="image/svg+xml",
            headers={"Cache-Control": "public, max-age=300", "Vary": "Cookie"},
        )

    # Rank files by how many other files depend on them
    file_scores: list[tuple[str, int]] = []
    try:
        with _open_store(owner, repo) as store:
            all_paths = sorted(store.all_paths())
            for path in all_paths:
                summary_obj = store.get_summary(path)
                if not summary_obj:
                    continue
                dep_count = 0
                for sym in summary_obj.symbols:
                    callers = store.get_call_graph(path, sym.name)
                    dep_count += sum(1 for cp, _ in callers if cp != path)
                if dep_count > 0:
                    file_scores.append((path, dep_count))
    except Exception:
        pass

    file_scores.sort(key=lambda x: -x[1])

    # Top 3 most-depended-on = "core" files (center)
    core_files = [f for f, _ in file_scores[:3]]
    # Next batch = internal dependents (middle ring)
    internal_files = [f for f, _ in file_scores[3:9]]

    # Get cross-repo deps
    cross_repo: list[str] = []
    try:
        with _open_relationships() as rs:
            full_name = f"{owner}/{repo}"
            for edge in rs.resolve_edges():
                if edge.target_repo == full_name:
                    cross_repo.append(edge.source_repo)
    except Exception:
        pass
    if not signed_in:
        cross_repo = [name for name in cross_repo if _is_known_public(name)]

    svg = generate_blast_svg(
        changed_files=core_files,
        internal_deps=internal_files,
        cross_repo_deps=cross_repo,
    )

    return FastAPIResponse(
        content=svg,
        media_type="image/svg+xml",
        headers={
            # The signed-in rendering must not be served to anonymous viewers
            # from a shared cache.
            "Cache-Control": f"{'private' if signed_in else 'public'}, max-age=300",
            "Vary": "Cookie",
        },
    )


@router.get("/api/repos/{owner:path}/{repo}/blast-radius", response_model=BlastRadiusResponse)
def get_blast_radius(owner: str, repo: str, changed_paths: str = "") -> BlastRadiusResponse:
    """Get the blast radius for a set of changed files.

    Returns both:
    - internal: files within the same repo that depend on the changed files
    - cross_repo: other repos that reference this one via external_refs

    Query param `changed_paths` is a comma-separated list of file paths.
    If empty, shows the most-depended-on files and all dependent repos.
    """
    internal: list[BlastRadiusModel] = []

    with _open_store(owner, repo) as store:
        if changed_paths:
            paths = [p.strip() for p in changed_paths.split(",") if p.strip()]
            entries = store.get_blast_radius(paths)
            internal = [
                BlastRadiusModel(
                    path=e.path,
                    summary=e.summary,
                    affected_symbols=e.affected_symbols,
                    depth=e.depth,
                )
                for e in entries
            ]
        else:
            # No changed paths — rank files by inbound dependencies
            all_paths = sorted(store.all_paths())
            rankings: list[tuple[str, str, list[str], int]] = []

            for path in all_paths:
                summary_obj = store.get_summary(path)
                if not summary_obj:
                    continue
                called_by: set[tuple[str, str]] = set()
                referenced_symbols: set[str] = set()
                for sym in summary_obj.symbols:
                    callers = store.get_call_graph(path, sym.name)
                    for caller_path, caller_symbol in callers:
                        if caller_path != path:
                            called_by.add((caller_path, caller_symbol))
                            referenced_symbols.add(sym.name)
                if called_by:
                    rankings.append(
                        (
                            path,
                            summary_obj.summary,
                            sorted(referenced_symbols),
                            len(called_by),
                        )
                    )

            rankings.sort(key=lambda x: -x[3])
            internal = [
                BlastRadiusModel(
                    path=path,
                    summary=f"{summary} — {dependent_count} dependent{'s' if dependent_count != 1 else ''}",
                    affected_symbols=syms,
                    depth=1,
                )
                for path, summary, syms, dependent_count in rankings
            ]

    # ── Cross-repo blast radius ──
    # Find repos whose external_refs point at this repo
    cross_repo: list[CrossRepoBlastEntry] = []
    try:
        with _open_relationships() as rs:
            full_name = f"{owner}/{repo}"
            edges = rs.resolve_edges()
            for edge in edges:
                if edge.target_repo == full_name:
                    # This dependent repo references our repo
                    dep_files = [
                        {
                            "path": ref.file_path,
                            "kind": ref.kind,
                            "target": ref.target,
                            "description": ref.description,
                        }
                        for ref in edge.refs
                    ]
                    cross_repo.append(
                        CrossRepoBlastEntry(
                            repo=edge.source_repo,
                            files=dep_files,
                            edge_kind=edge.kind,
                        )
                    )
    except Exception as exc:
        logger.warning("Failed to compute cross-repo blast radius: %s", exc)

    return BlastRadiusResponse(internal=internal, cross_repo=cross_repo)


@router.get("/api/repos/{owner:path}/{repo}/external-refs", response_model=list[ExternalRefModel])
def get_external_refs(owner: str, repo: str) -> list[ExternalRefModel]:
    """Get all external references for a repo."""
    with _open_store(owner, repo) as store:
        paths = sorted(store.all_paths())
        refs = store.get_external_refs_for_paths(paths)

        return [
            ExternalRefModel(
                file_path=ref.file_path,
                kind=ref.kind,
                target=ref.target,
                description=ref.description,
            )
            for ref in refs
        ]


@router.get("/api/repos/{owner:path}/{repo}/packages", response_model=list[PackageModel])
def get_packages(owner: str, repo: str) -> list[PackageModel]:
    """List dependencies with their indexed versions and manifest sources.

    The representative fields continue to prefer lockfiles. The versions
    list also includes the declarations and their source paths.
    """
    from mira.index.manifests import _is_lockfile_path

    with _open_store(owner, repo) as store:
        rows = store.list_manifest_packages()

    by_key: dict[tuple[str, str], PackageManifestRow] = {}
    sources_by_version: dict[tuple[str, str], dict[str, set[tuple[str, str, bool]]]] = {}
    for r in rows:
        key = (r.kind, r.name.lower())
        sources_by_version.setdefault(key, {}).setdefault(r.version, set()).add(
            (r.file_path, r.name, r.is_dev)
        )
        existing = by_key.get(key)
        if (
            existing is None
            or (_is_lockfile_path(r.file_path) and not _is_lockfile_path(existing.file_path))
            # Prefer the last encountered version within one npm lockfile
            # while the dashboard still shows one row per package.
            or (
                r.kind == "npm"
                and r.name == existing.name
                and r.file_path == existing.file_path
                and r.id > existing.id
            )
        ):
            by_key[key] = r
    packages = []
    for key, representative in sorted(by_key.items()):
        sources = sources_by_version[key]
        versions = [
            PackageVersionModel(
                version=version,
                sources=[
                    PackageVersionSourceModel(name=name, file_path=path, is_dev=is_dev)
                    for path, name, is_dev in sorted(sources[version])
                ],
            )
            for version in sorted(
                sources, key=lambda value: (value != representative.version, value)
            )
        ]
        packages.append(
            PackageModel(
                name=representative.name,
                kind=representative.kind,
                version=representative.version,
                file_path=representative.file_path,
                is_dev=representative.is_dev,
                versions=versions,
            )
        )
    return packages


@router.post("/api/repos/{owner:path}/{repo}/index")
async def trigger_index(owner: str, repo: str, request: Request, full: bool = False) -> dict:
    """Trigger indexing for a repo. full=true wipes and re-indexes everything."""
    _require_admin(request)
    from mira.index.status import tracker

    full_name = f"{owner}/{repo}"

    # Check if already indexing
    for j in tracker.get_active():
        if j.repo == full_name:
            return {"status": "already_indexing"}

    # Resolve the repo's platform and build the matching fetcher.
    records = _api._app_db.get_repo_any_platform(owner, repo)
    # Same owner/repo can exist on multiple platforms; prefer
    # github → gitlab → forgejo → bitbucket (the historical fallback order).
    platform = _pick_platform_record(records).platform if records else "github"

    from mira.platforms.fetch import EmptyRepoError, make_fetcher

    if platform == "gitlab":
        token = os.environ.get("MIRA_GITLAB_TOKEN", "")
        if not token:
            raise HTTPException(status_code=400, detail="MIRA_GITLAB_TOKEN is not configured.")
    elif platform == "forgejo":
        token = os.environ.get("MIRA_FORGEJO_TOKEN", "")
        if not token:
            raise HTTPException(status_code=400, detail="MIRA_FORGEJO_TOKEN is not configured.")
    elif platform == "bitbucket":
        token = os.environ.get("MIRA_BITBUCKET_TOKEN", "")
        if not token:
            raise HTTPException(status_code=400, detail="MIRA_BITBUCKET_TOKEN is not configured.")
    else:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            try:
                from mira.platforms.github.auth import GitHubAppAuth

                app_id = os.environ.get("MIRA_GITHUB_APP_ID", "")
                private_key = os.environ.get("MIRA_GITHUB_PRIVATE_KEY", "")
                if app_id and private_key:
                    auth = GitHubAppAuth(app_id=app_id, private_key=private_key)
                    installations = await auth.list_installations()
                    for inst in installations:
                        inst_id = int(inst.get("id", 0))
                        if inst_id:
                            repos = await auth.list_installation_repos(inst_id)
                            if any(r.get("full_name") == full_name for r in repos):
                                token = await auth.get_installation_token(inst_id)
                                break
            except Exception as exc:
                logger.warning("Failed to get GitHub token: %s", exc)

        if not token:
            raise HTTPException(
                status_code=400,
                detail="No GitHub token available. Set GITHUB_TOKEN or configure GitHub App.",
            )

    fetcher = make_fetcher(platform, token)

    # Run indexing in background

    from mira.config import load_config
    from mira.index.indexer import IndexingCancelled, index_repo
    from mira.llm import create_llm

    async def _do_index() -> None:
        store = None
        run = None
        job = tracker.start(full_name)
        try:
            from mira.dashboard.models_config import llm_config_for

            config = load_config()
            llm = create_llm(llm_config_for("indexing", config.llm))
            store = IndexStore.open(owner, repo, platform=platform, allow_fallback=False)
            run = store.claim_index_run()
            count = await index_repo(
                owner=owner,
                repo=repo,
                fetcher=fetcher,
                config=config,
                store=store,
                llm=llm,
                full=full,
                reset=full,
                cancel_check=lambda: job.cancel_requested,
                run=run,
                platform=platform,
            )
            with store.index_write(run):
                _api._app_db.set_repo_status(
                    owner,
                    repo,
                    "ready",
                    files_indexed=len(store.all_paths()),
                    bump_last_indexed=True,
                    platform=platform,
                )
                tracker.complete(full_name, count, expected=job)
            logger.info(
                "Index %s for %s: %d files", "rebuild" if full else "update", full_name, count
            )
            from mira.outbound_webhooks import INDEXING_COMPLETED, dispatch_event

            await dispatch_event(INDEXING_COMPLETED, {"repo": full_name, "files_indexed": count})
        except IndexingSuperseded:
            tracker.discard(job)
            logger.info("Indexing superseded for %s", full_name)
        except Exception as exc:
            try:
                with store.index_write(run) if store is not None and run else nullcontext():
                    if isinstance(exc, IndexingCancelled):
                        tracker.cancel(full_name, exc.files_indexed, expected=job)
                        logger.info(
                            "Indexing cancelled for %s after %d files", full_name, exc.files_indexed
                        )
                    elif isinstance(exc, EmptyRepoError):
                        _api._app_db.set_repo_status(
                            owner, repo, "empty", error=str(exc), platform=platform
                        )
                        tracker.complete(full_name, 0, expected=job)
                    else:
                        tracker.fail(full_name, str(exc), expected=job)
                        logger.exception("Indexing failed for %s", full_name)
            except IndexingSuperseded:
                tracker.discard(job)
        finally:
            if store is not None:
                store.close()

    asyncio.create_task(_do_index())
    return {"status": "indexing", "full": full}


@router.delete("/api/repos/{owner:path}/{repo}/index")
async def cancel_index(owner: str, repo: str, request: Request) -> dict:
    """Request cancellation of an in-progress indexing job.

    Returns ``{"status": "cancelling"}`` if a job was active,
    ``{"status": "not_indexing"}`` otherwise. The job transitions to
    ``cancelled`` once the indexer notices the flag (at the next batch
    boundary).
    """
    _require_admin(request)
    from mira.index.status import tracker

    full_name = f"{owner}/{repo}"
    if tracker.request_cancel(full_name):
        return {"status": "cancelling"}
    return {"status": "not_indexing"}


@router.get("/api/repos/{owner:path}/{repo}/reviews", response_model=list[ReviewEventModel])
def list_reviews(owner: str, repo: str, limit: int = 50) -> list[ReviewEventModel]:
    """List recent review events for a repo."""
    with _open_store(owner, repo) as store:
        return [_queries.review_event_model(e) for e in store.list_review_events(limit=limit)]


@router.get("/api/repos/{owner:path}/{repo}/branch-policy", response_model=RepoBaseBranchPolicy)
def get_branch_policy(owner: str, repo: str) -> RepoBaseBranchPolicy:
    """The stored per-repo auto-review branch policy (admin UI backing)."""
    records = _api._app_db.get_repo_any_platform(owner, repo)
    record = _pick_platform_record(records) if records else None
    return _parse_base_branch_policy(record.base_branch_policy if record else "") or (
        RepoBaseBranchPolicy(mode="inherit")
    )


@router.put("/api/repos/{owner:path}/{repo}/branch-policy", response_model=RepoBaseBranchPolicy)
def set_branch_policy(
    owner: str, repo: str, body: RepoBaseBranchPolicy, request: Request
) -> RepoBaseBranchPolicy:
    """Store the per-repo auto-review branch policy.

    ``override`` requires at least one branch pattern (an empty override would
    silently disable reviews, which is what ``disable`` is for). Takes effect
    on the next webhook event; no restart needed.
    """
    _require_admin(request)
    from mira.platforms.base_branch_policy import validate_patterns

    branches: list[str] = []
    if body.mode == "override":
        # An empty override would silently disable reviews; "disable" is the
        # explicit, labelled way to do that.
        if not body.branches:
            raise HTTPException(
                status_code=400, detail="An override needs at least one branch pattern"
            )
        try:
            branches = validate_patterns(body.branches)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    records = _api._app_db.get_repo_any_platform(owner, repo)
    record = _pick_platform_record(records) if records else None
    if record is None:
        raise HTTPException(
            status_code=404, detail="Repository not registered with this Mira instance"
        )
    # Only override uses branches; don't persist stale values for inherit/disable.
    blob = json.dumps({"mode": body.mode, "branches": branches})
    _api._app_db.set_repo_base_branch_policy(owner, repo, blob, platform=record.platform)
    return RepoBaseBranchPolicy(mode=body.mode, branches=branches)


# Keep the catch-all detail route last.  For nested GitLab namespaces it can
# otherwise consume more specific paths such as ``.../files`` or ``.../rules``.
@router.get("/api/repos/{owner:path}/{repo}", response_model=RepoDetail)
def get_repo_detail(owner: str, repo: str) -> RepoDetail:
    """Get details for a specific repo."""
    with _open_store(owner, repo) as store:
        paths = sorted(store.all_paths())
        summaries = store.get_summaries(paths)

        files: list[FileModel] = []
        total_symbols = 0
        total_imports = 0
        total_external_refs = 0
        total_loc = 0

        for path in paths:
            fs = summaries.get(path)
            if fs is None:
                continue
            files.append(
                FileModel(
                    path=fs.path,
                    language=fs.language,
                    summary=fs.summary,
                    symbols=[
                        SymbolModel(name=s.name, kind=s.kind, signature=s.signature)
                        for s in fs.symbols
                    ],
                    imports=fs.imports,
                    loc=fs.loc,
                )
            )
            total_symbols += len(fs.symbols)
            total_imports += len(fs.imports)
            total_external_refs += len(fs.external_refs)
            total_loc += fs.loc or 0

        repo_records = _api._app_db.get_repo_any_platform(owner, repo)
        repo_record = _pick_platform_record(repo_records) if repo_records else None
        last_indexed = (
            datetime.fromtimestamp(repo_record.last_indexed_at, tz=UTC).isoformat()
            if repo_record and repo_record.last_indexed_at
            else None
        )

        return RepoDetail(
            owner=owner,
            repo=repo,
            file_count=len(paths),
            files=files,
            symbols_count=total_symbols,
            imports_count=total_imports,
            external_refs_count=total_external_refs,
            lines_count=total_loc,
            last_indexed=last_indexed,
            base_branch_policy=_parse_base_branch_policy(
                repo_record.base_branch_policy if repo_record else ""
            ),
        )
