"""Dashboard repos routes"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

from fastapi import HTTPException
from fastapi import Response as FastAPIResponse

from mira.dashboard import api as _api
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
    RepoDetail,
    RepoListItem,
    ReviewEventModel,
    SymbolModel,
    _open_relationships,
    _open_store,
    logger,
    router,
)
from mira.index.store import IndexStore


@router.get("/api/repos", response_model=list[RepoListItem])
def list_repos() -> list[RepoListItem]:
    """List all repos from the registry."""
    repos = _api._app_db.list_repos()
    return [
        RepoListItem(
            owner=r.owner,
            repo=r.repo,
            platform=r.platform,
            status=r.status,
            index_mode=r.index_mode,
            file_count=r.files_indexed,
            file_count_estimate=r.file_count_estimate,
            installation_id=r.installation_id,
            error=r.error,
            last_indexed=datetime.fromtimestamp(r.last_indexed_at, tz=UTC).isoformat()
            if r.last_indexed_at
            else None,
        )
        for r in repos
    ]


@router.post("/api/repos/sync")
async def sync_repos() -> dict:
    """Reconcile the repos table with actual GitHub App installations.

    Removes repos that are no longer accessible and adds any new ones.
    """
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


@router.get("/api/repos/{owner}/{repo}", response_model=RepoDetail)
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

        repo_record = _api._app_db.get_repo(owner, repo)
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
        )


@router.get("/api/repos/{owner}/{repo}/files", response_model=list[FileModel])
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


@router.get("/api/repos/{owner}/{repo}/dependencies", response_model=DependencyGraph)
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


@router.get("/api/repos/{owner}/{repo}/blast-radius.svg")
def get_blast_radius_svg(owner: str, repo: str) -> FastAPIResponse:
    """Render blast radius as an SVG image."""
    from mira.dashboard.blast_svg import generate_blast_svg

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

    svg = generate_blast_svg(
        changed_files=core_files,
        internal_deps=internal_files,
        cross_repo_deps=cross_repo,
    )

    return FastAPIResponse(
        content=svg,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "public, max-age=300",
        },
    )


@router.get("/api/repos/{owner}/{repo}/blast-radius", response_model=BlastRadiusResponse)
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


@router.get("/api/repos/{owner}/{repo}/external-refs", response_model=list[ExternalRefModel])
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


@router.get("/api/repos/{owner}/{repo}/packages", response_model=list[PackageModel])
def get_packages(owner: str, repo: str) -> list[PackageModel]:
    """List dependencies parsed from manifest and lockfile files.

    When the same package appears in both a manifest (e.g. `pyproject.toml`
    declaring `>=1.30`) and a lockfile (e.g. `uv.lock` resolving to
    `1.99.5`), the lockfile entry wins — its concrete version is what's
    actually installed.
    """
    from mira.index.manifests import _is_lockfile_path

    with _open_store(owner, repo) as store:
        rows = store.list_manifest_packages()

    # Dedupe by (kind, name), preferring lockfile rows.
    by_key: dict[tuple[str, str], PackageModel] = {}
    for r in rows:
        model = PackageModel(
            name=r.name,
            kind=r.kind,
            version=r.version,
            file_path=r.file_path,
            is_dev=r.is_dev,
        )
        key = (r.kind, r.name.lower())
        existing = by_key.get(key)
        if existing is None or (
            _is_lockfile_path(r.file_path) and not _is_lockfile_path(existing.file_path)
        ):
            by_key[key] = model
    return sorted(by_key.values(), key=lambda p: (p.kind, p.name.lower()))


@router.post("/api/repos/{owner}/{repo}/index")
async def trigger_index(owner: str, repo: str, full: bool = False) -> dict:
    """Trigger indexing for a repo. full=true wipes and re-indexes everything."""
    from mira.index.status import tracker

    full_name = f"{owner}/{repo}"

    # Check if already indexing
    for j in tracker.get_active():
        if j.repo == full_name:
            return {"status": "already_indexing"}

    # Resolve the repo's platform (github by default; gitlab if that's where
    # it's registered) and build the matching fetcher.
    record = _api._app_db.get_repo(owner, repo, platform="github") or _api._app_db.get_repo(
        owner, repo, platform="gitlab"
    )
    platform = record.platform if record else "github"

    from mira.platforms.fetch import EmptyRepoError, make_fetcher

    if platform == "gitlab":
        token = os.environ.get("MIRA_GITLAB_TOKEN", "")
        if not token:
            raise HTTPException(status_code=400, detail="MIRA_GITLAB_TOKEN is not configured.")
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
        count = 0
        store = None
        try:
            from mira.dashboard.models_config import llm_config_for

            tracker.start(full_name)
            config = load_config()
            # Use the configured indexing model — without this swap we'd
            # silently fall back to the review model, which is slower and
            # more expensive per token.
            llm = create_llm(llm_config_for("indexing", config.llm))
            store = IndexStore.open(owner, repo, platform=platform)
            if full:
                # Wipe existing index
                for path in list(store.all_paths()):
                    store.remove_paths([path])
            count = await index_repo(
                owner=owner,
                repo=repo,
                fetcher=fetcher,
                config=config,
                store=store,
                llm=llm,
                full=full,
                cancel_check=lambda: tracker.is_cancel_requested(full_name),
            )
            # Real indexing run finished — bump last_indexed_at so the
            # dashboard's "Indexed N ago" reflects this completion.
            _api._app_db.set_repo_status(
                owner,
                repo,
                "ready",
                files_indexed=count,
                bump_last_indexed=True,
                platform=platform,
            )
            tracker.complete(full_name, count)
            logger.info(
                "Index %s for %s: %d files", "rebuild" if full else "update", full_name, count
            )
            from mira.outbound_webhooks import INDEXING_COMPLETED, dispatch_event

            await dispatch_event(INDEXING_COMPLETED, {"repo": full_name, "files_indexed": count})
        except IndexingCancelled as cancelled:
            tracker.cancel(full_name, cancelled.files_indexed)
            logger.info(
                "Indexing cancelled for %s after %d files", full_name, cancelled.files_indexed
            )
        except EmptyRepoError as empty:
            _api._app_db.set_repo_status(owner, repo, "empty", error=str(empty), platform=platform)
            tracker.complete(full_name, 0)
            logger.info("Index skipped for %s — empty repository", full_name)
        except Exception as exc:
            tracker.fail(full_name, str(exc))
            logger.exception("Indexing failed for %s", full_name)
        finally:
            if store is not None:
                store.close()

    asyncio.create_task(_do_index())
    return {"status": "indexing", "full": full}


@router.delete("/api/repos/{owner}/{repo}/index")
async def cancel_index(owner: str, repo: str) -> dict:
    """Request cancellation of an in-progress indexing job.

    Returns ``{"status": "cancelling"}`` if a job was active,
    ``{"status": "not_indexing"}`` otherwise. The job transitions to
    ``cancelled`` once the indexer notices the flag (at the next batch
    boundary).
    """
    from mira.index.status import tracker

    full_name = f"{owner}/{repo}"
    if tracker.request_cancel(full_name):
        return {"status": "cancelling"}
    return {"status": "not_indexing"}


@router.get("/api/repos/{owner}/{repo}/reviews", response_model=list[ReviewEventModel])
def list_reviews(owner: str, repo: str, limit: int = 50) -> list[ReviewEventModel]:
    """List recent review events for a repo."""
    with _open_store(owner, repo) as store:
        events = store.list_review_events(limit=limit)
        return [
            ReviewEventModel(
                id=e.id,
                pr_number=e.pr_number,
                pr_title=e.pr_title,
                pr_url=e.pr_url,
                comments_posted=e.comments_posted,
                blockers=e.blockers,
                warnings=e.warnings,
                suggestions=e.suggestions,
                files_reviewed=e.files_reviewed,
                lines_changed=e.lines_changed,
                tokens_used=e.tokens_used,
                duration_ms=e.duration_ms,
                categories=e.categories,
                created_at=e.created_at,
            )
            for e in events
        ]
