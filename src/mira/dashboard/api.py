"""FastAPI dashboard API for the Mira UI."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi import Response as FastAPIResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from mira.dashboard.auth import AuthMiddleware, create_auth_router
from mira.dashboard.db import AppDatabase
from mira.index.relationships import RelationshipStore
from mira.index.store import IndexStore

logger = logging.getLogger(__name__)

# Database + auth
_db_url = os.environ.get("DATABASE_URL", "")
_admin_password = os.environ.get("ADMIN_PASSWORD", "admin")
_app_db = AppDatabase(_db_url, admin_password=_admin_password)

# All dashboard routes register on this router. `register_dashboard()` wires
# router + middleware into any FastAPI app, so the routes can run inside the
# unified webhook+UI server (production) or the standalone app below (dev).
router = APIRouter()


def register_dashboard(app: FastAPI) -> None:
    """Wire dashboard routes + middleware into a FastAPI app."""
    # CORS must be added AFTER auth so it runs BEFORE auth (Starlette reverses order)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(AuthMiddleware, db=_app_db)
    app.include_router(create_auth_router(_app_db))
    app.include_router(router)


# Standalone app — initialized at module load, but routes are registered at
# the bottom of this file, *after* all @router decorators have run.
app = FastAPI(title="Mira Dashboard API", version="0.1.0")

_INDEX_DIR = os.environ.get("MIRA_INDEX_DIR", "/data/indexes")


def _get_index_dir() -> str:
    return os.environ.get("MIRA_INDEX_DIR", _INDEX_DIR)


@contextmanager
def _open_store(owner: str, repo: str) -> Generator[IndexStore, None, None]:
    """Open an IndexStore via the factory (Postgres or SQLite)."""
    # Check the repos registry first — raises 404 if the repo isn't known
    repo_record = _app_db.get_repo(owner, repo)
    if repo_record is None:
        raise HTTPException(status_code=404, detail=f"Repo {owner}/{repo} not found")

    store = IndexStore.open(owner, repo)
    try:
        yield store
    finally:
        store.close()


@contextmanager
def _open_relationships() -> Generator[RelationshipStore, None, None]:
    rs = RelationshipStore(_get_index_dir())
    try:
        yield rs
    finally:
        rs.close()


# ── Pydantic response models ───────────────────────────────────────


class RepoListItem(BaseModel):
    owner: str
    repo: str
    status: str = "pending"
    index_mode: str = "full"
    file_count: int = 0
    file_count_estimate: int = 0
    installation_id: int = 0
    error: str = ""
    last_indexed: str | None = None


class SymbolModel(BaseModel):
    name: str
    kind: str
    signature: str


class FileModel(BaseModel):
    path: str
    language: str
    summary: str
    symbols: list[SymbolModel] = []
    imports: list[str] = []
    loc: int = 0


class RepoDetail(BaseModel):
    owner: str
    repo: str
    file_count: int
    files: list[FileModel]
    symbols_count: int
    imports_count: int
    external_refs_count: int
    last_indexed: str | None = None


class ImportEdge(BaseModel):
    source: str
    target: str


class DependentEdge(BaseModel):
    path: str
    dependent_path: str


class DependencyGraph(BaseModel):
    imports: list[ImportEdge]
    dependents: list[DependentEdge]


class ExternalRefModel(BaseModel):
    file_path: str
    kind: str
    target: str
    description: str


class RepoEdgeModel(BaseModel):
    source_repo: str
    target_repo: str
    kind: str
    ref_count: int


class RepoGroupModel(BaseModel):
    name: str
    repos: list[str]
    confidence: float
    evidence: list[str]


class RelationshipsResponse(BaseModel):
    edges: list[RepoEdgeModel]
    groups: list[RepoGroupModel]


class RelatedRepoModel(BaseModel):
    repo: str
    relationship_type: str
    edge_count: int


class ReviewEventModel(BaseModel):
    id: int
    pr_number: int
    pr_title: str
    pr_url: str
    comments_posted: int
    blockers: int
    warnings: int
    suggestions: int
    files_reviewed: int
    lines_changed: int
    tokens_used: int
    duration_ms: int
    categories: str
    created_at: float


class ReviewStatsModel(BaseModel):
    total_reviews: int
    total_comments: int
    total_blockers: int
    total_warnings: int
    total_suggestions: int
    total_files_reviewed: int
    total_lines_changed: int
    total_tokens: int
    avg_duration_ms: int
    categories: dict[str, int] = {}
    avg_comments_per_pr: float = 0.0


class OrgStatsModel(BaseModel):
    total_repos: int
    total_files: int
    total_edges: int
    total_groups: int
    review_stats: ReviewStatsModel


class ReviewContextModel(BaseModel):
    id: int
    title: str
    content: str
    created_at: float
    updated_at: float


class ReviewContextCreate(BaseModel):
    title: str
    content: str


class OverrideRequest(BaseModel):
    source_repo: str
    target_repo: str
    status: str  # "confirmed" or "denied"


class OverrideModel(BaseModel):
    source_repo: str
    target_repo: str
    status: str
    created_at: float


class CustomEdgeRequest(BaseModel):
    source_repo: str
    target_repo: str
    reason: str


class CustomEdgeModel(BaseModel):
    id: int
    source_repo: str
    target_repo: str
    reason: str
    created_at: float


# ── Endpoints ───────────────────────────────────────────────────────


class IndexStatusModel(BaseModel):
    repo: str
    status: str
    files_total: int
    files_done: int
    started_at: float
    finished_at: float
    error: str


@router.get("/api/indexing/status", response_model=list[IndexStatusModel])
def get_indexing_status() -> list[IndexStatusModel]:
    """Get current indexing status for all repos."""
    from mira.index.status import tracker

    return [
        IndexStatusModel(
            repo=j.repo,
            status=j.status,
            files_total=j.files_total,
            files_done=j.files_done,
            started_at=j.started_at,
            finished_at=j.finished_at,
            error=j.error,
        )
        for j in tracker.get_all()
    ]


@router.get("/api/repos", response_model=list[RepoListItem])
def list_repos() -> list[RepoListItem]:
    """List all repos from the registry."""
    repos = _app_db.list_repos()
    return [
        RepoListItem(
            owner=r.owner,
            repo=r.repo,
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


class CostEstimate(BaseModel):
    estimated_usd: float
    input_tokens: int
    output_tokens: int
    model: str
    file_count: int


@router.get("/api/indexing/estimate", response_model=CostEstimate)
def estimate_cost() -> CostEstimate:
    """Estimate the cost of indexing all pending repos with the current model."""
    from mira.config import load_config
    from mira.dashboard.models_config import (
        estimate_indexing_cost,
        get_indexing_model,
    )

    config = load_config()
    model = get_indexing_model(config.llm, _app_db.get_setting("indexing_model"))

    # Sum file counts across all pending repos
    total_files = sum(r.file_count_estimate for r in _app_db.list_repos() if r.status == "pending")

    est = estimate_indexing_cost(total_files, model)
    return CostEstimate(
        estimated_usd=est["estimated_usd"],
        input_tokens=est["input_tokens"],
        output_tokens=est["output_tokens"],
        model=model,
        file_count=total_files,
    )


class ModelOption(BaseModel):
    value: str
    label: str
    recommended: bool = False


class ModelsResponse(BaseModel):
    indexing_model: str
    review_model: str
    indexing_options: list[ModelOption]
    review_options: list[ModelOption]


class ModelsUpdate(BaseModel):
    indexing_model: str
    review_model: str


@router.get("/api/settings/models", response_model=ModelsResponse)
def get_models() -> ModelsResponse:
    from mira.config import load_config
    from mira.dashboard.models_config import (
        INDEXING_MODELS,
        REVIEW_MODELS,
        get_indexing_model,
        get_review_model,
    )

    config = load_config()
    indexing = get_indexing_model(config.llm, _app_db.get_setting("indexing_model"))
    review = get_review_model(config.llm, _app_db.get_setting("review_model"))

    return ModelsResponse(
        indexing_model=indexing,
        review_model=review,
        indexing_options=[ModelOption(**m) for m in INDEXING_MODELS],
        review_options=[ModelOption(**m) for m in REVIEW_MODELS],
    )


@router.put("/api/settings/models")
def set_models(body: ModelsUpdate) -> dict:
    from mira.llm.registry import is_supported

    # Reject unsupported or wrong-purpose models. Without this, an admin can
    # silently configure a model that's broken (no JSON mode, missing from
    # the registry, miscategorized for the role).
    if not is_supported(body.indexing_model, purpose="indexing"):
        raise HTTPException(
            status_code=400,
            detail=f"{body.indexing_model!r} is not a supported indexing model.",
        )
    if not is_supported(body.review_model, purpose="review"):
        raise HTTPException(
            status_code=400,
            detail=f"{body.review_model!r} is not a supported review model.",
        )
    _app_db.set_setting("indexing_model", body.indexing_model)
    _app_db.set_setting("review_model", body.review_model)
    _app_db.mark_setup_complete()
    return {"ok": True}


@router.get("/api/events")
async def events_stream(request: Request) -> StreamingResponse:
    """Server-Sent Events stream for real-time dashboard updates."""
    from mira.dashboard.events import bus, format_sse

    async def generate():
        q = await bus.subscribe()
        try:
            # Send a heartbeat immediately
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield format_sse(event)
                except TimeoutError:
                    # Heartbeat to keep connection alive
                    yield ": heartbeat\n\n"
        finally:
            await bus.unsubscribe(q)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


class PendingUninstallModel(BaseModel):
    installation_id: int
    owner: str


@router.get("/api/uninstalls/pending", response_model=list[PendingUninstallModel])
def list_pending_uninstalls() -> list[PendingUninstallModel]:
    return [
        PendingUninstallModel(installation_id=iid, owner=owner)
        for iid, owner in _app_db.list_pending_uninstalls()
    ]


@router.post("/api/uninstalls/{installation_id}/keep")
def keep_uninstall_data(installation_id: int) -> dict:
    """User chose to keep data after uninstall — just dismiss the popup."""
    _app_db.remove_pending_uninstall(installation_id)
    return {"ok": True}


@router.post("/api/uninstalls/{installation_id}/delete")
def delete_uninstall_data(installation_id: int) -> dict:
    """User chose to delete all data for this installation."""
    removed = _app_db.delete_repos_by_installation(installation_id)
    _app_db.remove_pending_uninstall(installation_id)
    return {"ok": True, "removed": removed}


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

    from mira.github_app.auth import GitHubAppAuth
    from mira.github_app.index_handlers import _count_files_for_repos

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
                    _app_db.register_repo(owner, repo, inst_id)
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
        for db_repo in _app_db.list_repos():
            if (db_repo.owner, db_repo.repo) not in actual_repos:
                _app_db.delete_repo(db_repo.owner, db_repo.repo)
                removed += 1

    return {
        "synced": len(actual_repos),
        "removed": removed,
        "installations_reachable": installations_reachable,
    }


@router.get("/api/setup/status")
async def get_setup_status() -> dict:
    """Check if initial setup has been completed. Auto-syncs repos from GitHub if none registered."""
    repo_count = len(_app_db.list_repos())

    # If no repos registered, try to sync from GitHub App
    if repo_count == 0:
        try:
            app_id = os.environ.get("MIRA_GITHUB_APP_ID", "")
            private_key = os.environ.get("MIRA_GITHUB_PRIVATE_KEY", "")
            if app_id and private_key:
                import asyncio as _asyncio

                from mira.github_app.auth import GitHubAppAuth
                from mira.github_app.index_handlers import _count_files_for_repos

                auth = GitHubAppAuth(app_id=app_id, private_key=private_key)
                installations = await auth.list_installations()
                for inst in installations:
                    inst_id = int(inst.get("id", 0))
                    if not inst_id:
                        continue
                    repos_list = await auth.list_installation_repos(inst_id)
                    for r in repos_list:
                        full_name = str(r.get("full_name", ""))
                        if "/" in full_name:
                            owner, repo = full_name.split("/", 1)
                            _app_db.register_repo(owner, repo, inst_id)
                            repo_count += 1
                    # Count files in background
                    _asyncio.create_task(_count_files_for_repos(auth, inst_id, repos_list))
                logger.info("Synced %d repos from GitHub App", repo_count)
        except Exception as exc:
            logger.warning("Failed to sync repos from GitHub: %s", exc)

    return {"setup_complete": _app_db.setup_complete, "repo_count": repo_count}


class SetupRequest(BaseModel):
    repos: list[dict]  # [{"owner": "x", "repo": "y", "enabled": true}]
    index_mode: str  # "full" or "light"


@router.post("/api/setup/complete")
async def complete_setup(body: SetupRequest) -> dict:
    """Save setup choices and start indexing selected repos."""
    enabled_count = 0
    for r in body.repos:
        owner, repo = r["owner"], r["repo"]
        enabled = r.get("enabled", True)
        mode = body.index_mode if enabled else "none"
        _app_db.set_repo_index_mode(owner, repo, mode)
        if enabled:
            _app_db.set_repo_status(owner, repo, "indexing")
            enabled_count += 1

    _app_db.mark_setup_complete()

    # Only fire the background indexer if this request actually enabled repos.
    # Otherwise (Skip for now / all-disabled), don't start anything — the
    # indexer reads ALL repos from the DB and would race with sibling requests
    # that haven't yet set their repos to mode='none'.
    if enabled_count > 0:
        import asyncio

        asyncio.create_task(_run_initial_indexing(body.index_mode))

    return {"status": "indexing" if enabled_count else "skipped", "repos": enabled_count}


async def _run_initial_indexing(default_mode: str) -> None:
    """Index all repos that aren't set to 'none'."""
    from mira.index.status import tracker

    repos = _app_db.list_repos()
    to_index = [r for r in repos if r.index_mode != "none"]

    if not to_index:
        return

    # Get GitHub token
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        try:
            from mira.github_app.auth import GitHubAppAuth

            app_id = os.environ.get("MIRA_GITHUB_APP_ID", "")
            private_key = os.environ.get("MIRA_GITHUB_PRIVATE_KEY", "")
            if app_id and private_key:
                auth = GitHubAppAuth(app_id=app_id, private_key=private_key)
                if to_index[0].installation_id:
                    token = await auth.get_installation_token(to_index[0].installation_id)
        except Exception as exc:
            logger.warning("Failed to get GitHub token for indexing: %s", exc)
            return

    from mira.config import load_config
    from mira.dashboard.models_config import llm_config_for
    from mira.index.indexer import index_repo
    from mira.llm.provider import LLMProvider

    config = load_config()
    llm = LLMProvider(llm_config_for("indexing", config.llm))

    for repo_record in to_index:
        full_name = f"{repo_record.owner}/{repo_record.repo}"
        try:
            _app_db.set_repo_status(repo_record.owner, repo_record.repo, "indexing")
            tracker.start(full_name)
            store = IndexStore.open(repo_record.owner, repo_record.repo)
            count = await index_repo(
                owner=repo_record.owner,
                repo=repo_record.repo,
                token=token,
                config=config,
                store=store,
                llm=llm,
                full=(repo_record.index_mode == "full"),
            )
            store.close()
            _app_db.set_repo_status(
                repo_record.owner,
                repo_record.repo,
                "ready",
                files_indexed=count,
                bump_last_indexed=True,
            )
            tracker.complete(full_name, count)
            logger.info("Indexed %s: %d files", full_name, count)
        except Exception as exc:
            _app_db.set_repo_status(repo_record.owner, repo_record.repo, "failed", error=str(exc))
            tracker.fail(full_name, str(exc))
            logger.exception("Failed to index %s", full_name)


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

        repo_record = _app_db.get_repo(owner, repo)
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


class BlastRadiusModel(BaseModel):
    path: str
    summary: str
    affected_symbols: list[str]
    depth: int


class CrossRepoBlastEntry(BaseModel):
    repo: str  # "owner/repo"
    files: list[dict]  # [{"path", "kind", "target", "description"}]
    edge_kind: str  # how the dependent repo references this one


class BlastRadiusResponse(BaseModel):
    internal: list[BlastRadiusModel]  # within this repo
    cross_repo: list[CrossRepoBlastEntry]  # other repos that depend on this one


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


class PackageModel(BaseModel):
    name: str
    kind: str  # "npm" | "pip" | "docker" | "go" | "rust"
    version: str
    file_path: str
    is_dev: bool = False


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
        key = (r.kind, r.name)
        existing = by_key.get(key)
        if existing is None or (
            _is_lockfile_path(r.file_path) and not _is_lockfile_path(existing.file_path)
        ):
            by_key[key] = model
    return sorted(by_key.values(), key=lambda p: (p.kind, p.name.lower()))


class PackageSearchHit(BaseModel):
    owner: str
    repo: str
    name: str
    kind: str
    version: str
    file_path: str
    is_dev: bool


class VulnerabilityModel(BaseModel):
    package_name: str
    ecosystem: str
    package_version: str
    cve_id: str
    summary: str
    severity: str  # "critical" | "high" | "moderate" | "low" | "unknown"
    advisory_url: str
    fixed_in: str
    last_seen_at: float = 0.0


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


class VulnerabilitySummary(BaseModel):
    total: int = 0
    critical: int = 0
    high: int = 0
    moderate: int = 0
    low: int = 0
    unknown: int = 0


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


class OrgVulnerabilityModel(VulnerabilityModel):
    owner: str
    repo: str


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
    after this CVE?") and upgrade audits."""
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
    return [PackageSearchHit(**r) for r in rows]


@router.get("/api/relationships", response_model=RelationshipsResponse)
def get_relationships() -> RelationshipsResponse:
    """Get all cross-repo edges and groups."""
    with _open_relationships() as rs:
        edges = rs.resolve_edges()
        groups = rs.group_repos(rs.repos)

        return RelationshipsResponse(
            edges=[
                RepoEdgeModel(
                    source_repo=e.source_repo,
                    target_repo=e.target_repo,
                    kind=e.kind,
                    ref_count=len(e.refs),
                )
                for e in edges
            ],
            groups=[
                RepoGroupModel(
                    name=g.name,
                    repos=g.repos,
                    confidence=g.confidence,
                    evidence=g.evidence,
                )
                for g in groups
            ],
        )


@router.get("/api/relationships/{owner}/{repo}", response_model=list[RelatedRepoModel])
def get_related_repos(owner: str, repo: str) -> list[RelatedRepoModel]:
    """Get repos related to a specific repo."""
    with _open_relationships() as rs:
        full_name = f"{owner}/{repo}"
        if full_name not in rs.repos:
            raise HTTPException(status_code=404, detail=f"Repo {full_name} not found in index")

        related = rs.get_related_repos(owner, repo)

        return [
            RelatedRepoModel(
                repo=repo_name,
                relationship_type=rel_type,
                edge_count=len(edges),
            )
            for repo_name, rel_type, edges in related
        ]


# ── Review context endpoints ──


@router.get("/api/repos/{owner}/{repo}/context", response_model=list[ReviewContextModel])
def list_context(owner: str, repo: str) -> list[ReviewContextModel]:
    with _open_store(owner, repo) as store:
        entries = store.list_review_context()
        return [
            ReviewContextModel(
                id=e.id,
                title=e.title,
                content=e.content,
                created_at=e.created_at,
                updated_at=e.updated_at,
            )
            for e in entries
        ]


@router.post("/api/repos/{owner}/{repo}/context", response_model=ReviewContextModel)
def create_context(owner: str, repo: str, body: ReviewContextCreate) -> ReviewContextModel:
    with _open_store(owner, repo) as store:
        e = store.upsert_review_context(title=body.title, content=body.content)
        return ReviewContextModel(
            id=e.id,
            title=e.title,
            content=e.content,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )


@router.put("/api/repos/{owner}/{repo}/context/{context_id}", response_model=ReviewContextModel)
def update_context(
    owner: str, repo: str, context_id: int, body: ReviewContextCreate
) -> ReviewContextModel:
    with _open_store(owner, repo) as store:
        existing = store.get_review_context(context_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Context not found")
        e = store.upsert_review_context(
            title=body.title, content=body.content, context_id=context_id
        )
        return ReviewContextModel(
            id=e.id,
            title=e.title,
            content=e.content,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )


@router.delete("/api/repos/{owner}/{repo}/context/{context_id}")
def delete_context(owner: str, repo: str, context_id: int) -> dict:
    with _open_store(owner, repo) as store:
        store.delete_review_context(context_id)
        return {"ok": True}


# ── Per-repo rules endpoints ──


class RuleModel(BaseModel):
    id: int
    title: str
    content: str
    enabled: bool = True
    created_at: float
    updated_at: float


class RuleCreate(BaseModel):
    title: str
    content: str


class LearnedRuleModel(BaseModel):
    rule_text: str
    source_signal: str  # "reject_pattern" | "accept_pattern" | "human_pattern"
    category: str
    path_pattern: str = ""
    sample_count: int = 0
    updated_at: float = 0.0


class OrgLearnedRuleModel(LearnedRuleModel):
    owner: str
    repo: str


@router.get(
    "/api/repos/{owner}/{repo}/learned-rules",
    response_model=list[LearnedRuleModel],
)
def list_repo_learned_rules(owner: str, repo: str) -> list[LearnedRuleModel]:
    """Active learned rules synthesized from feedback signals on this repo."""
    with _open_store(owner, repo) as store:
        rules = store.list_active_learned_rules()
        return [
            LearnedRuleModel(
                rule_text=r.rule_text,
                source_signal=r.source_signal,
                category=r.category,
                path_pattern=r.path_pattern,
                sample_count=r.sample_count,
                updated_at=r.updated_at,
            )
            for r in rules
        ]


@router.get("/api/learned-rules", response_model=list[OrgLearnedRuleModel])
def list_org_learned_rules(limit: int = 500) -> list[OrgLearnedRuleModel]:
    """Active learned rules across every repo in the org."""
    db_url = os.environ.get("DATABASE_URL", "")
    capped = max(1, min(limit, 2000))
    if db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
        from mira.index.pg_store import list_learned_rules_org_wide

        rows = list_learned_rules_org_wide(db_url, limit=capped)
    else:
        from mira.index.store import list_learned_rules_org_wide_sqlite

        rows = list_learned_rules_org_wide_sqlite(limit=capped)
    return [
        OrgLearnedRuleModel(
            owner=r["owner"],
            repo=r["repo"],
            rule_text=r["rule_text"],
            source_signal=r["source_signal"],
            category=r["category"],
            path_pattern=r["path_pattern"],
            sample_count=r["sample_count"],
            updated_at=r["updated_at"] or 0.0,
        )
        for r in rows
    ]


@router.get("/api/repos/{owner}/{repo}/rules", response_model=list[RuleModel])
def list_repo_rules(owner: str, repo: str) -> list[RuleModel]:
    with _open_store(owner, repo) as store:
        entries = store.list_review_context()
        return [
            RuleModel(
                id=e.id,
                title=e.title,
                content=e.content,
                enabled=True,
                created_at=e.created_at,
                updated_at=e.updated_at,
            )
            for e in entries
        ]


@router.post("/api/repos/{owner}/{repo}/rules", response_model=RuleModel)
def create_repo_rule(owner: str, repo: str, body: RuleCreate) -> RuleModel:
    with _open_store(owner, repo) as store:
        e = store.upsert_review_context(title=body.title, content=body.content)
        return RuleModel(
            id=e.id,
            title=e.title,
            content=e.content,
            enabled=True,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )


@router.put("/api/repos/{owner}/{repo}/rules/{rule_id}", response_model=RuleModel)
def update_repo_rule(owner: str, repo: str, rule_id: int, body: RuleCreate) -> RuleModel:
    with _open_store(owner, repo) as store:
        existing = store.get_review_context(rule_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Rule not found")
        e = store.upsert_review_context(title=body.title, content=body.content, context_id=rule_id)
        return RuleModel(
            id=e.id,
            title=e.title,
            content=e.content,
            enabled=True,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )


@router.delete("/api/repos/{owner}/{repo}/rules/{rule_id}")
def delete_repo_rule(owner: str, repo: str, rule_id: int) -> dict:
    with _open_store(owner, repo) as store:
        store.delete_review_context(rule_id)
        return {"ok": True}


# ── Global rules endpoints ──


@router.get("/api/rules/global", response_model=list[RuleModel])
def list_global_rules() -> list[RuleModel]:
    rules = _app_db.list_global_rules()
    return [
        RuleModel(
            id=r.id,
            title=r.title,
            content=r.content,
            enabled=r.enabled,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in rules
    ]


@router.post("/api/rules/global", response_model=RuleModel)
def create_global_rule(body: RuleCreate) -> RuleModel:
    r = _app_db.upsert_global_rule(title=body.title, content=body.content)
    return RuleModel(
        id=r.id,
        title=r.title,
        content=r.content,
        enabled=r.enabled,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


@router.put("/api/rules/global/{rule_id}", response_model=RuleModel)
def update_global_rule(rule_id: int, body: RuleCreate) -> RuleModel:
    existing = _app_db.get_global_rule(rule_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Rule not found")
    r = _app_db.upsert_global_rule(title=body.title, content=body.content, rule_id=rule_id)
    return RuleModel(
        id=r.id,
        title=r.title,
        content=r.content,
        enabled=r.enabled,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


@router.delete("/api/rules/global/{rule_id}")
def delete_global_rule(rule_id: int) -> dict:
    _app_db.delete_global_rule(rule_id)
    return {"ok": True}


@router.patch("/api/rules/global/{rule_id}/toggle", response_model=RuleModel)
def toggle_global_rule(rule_id: int) -> RuleModel:
    r = _app_db.toggle_global_rule(rule_id)
    if not r:
        raise HTTPException(status_code=404, detail="Rule not found")
    return RuleModel(
        id=r.id,
        title=r.title,
        content=r.content,
        enabled=r.enabled,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


# ── Relationship override endpoints ──


@router.get("/api/relationships/overrides", response_model=list[OverrideModel])
def list_overrides() -> list[OverrideModel]:
    with _open_relationships() as rs:
        return [
            OverrideModel(
                source_repo=o.source_repo,
                target_repo=o.target_repo,
                status=o.status,
                created_at=o.created_at,
            )
            for o in rs.list_overrides()
        ]


@router.post("/api/relationships/overrides", response_model=OverrideModel)
def set_override(body: OverrideRequest) -> OverrideModel:
    if body.status not in ("confirmed", "denied"):
        raise HTTPException(status_code=400, detail="Status must be 'confirmed' or 'denied'")
    with _open_relationships() as rs:
        o = rs.set_override(body.source_repo, body.target_repo, body.status)
        return OverrideModel(
            source_repo=o.source_repo,
            target_repo=o.target_repo,
            status=o.status,
            created_at=o.created_at,
        )


@router.delete("/api/relationships/overrides")
def delete_override(source_repo: str, target_repo: str) -> dict:
    with _open_relationships() as rs:
        rs.delete_override(source_repo, target_repo)
        return {"ok": True}


# ── Custom edge endpoints ──


@router.get("/api/relationships/custom", response_model=list[CustomEdgeModel])
def list_custom_edges() -> list[CustomEdgeModel]:
    with _open_relationships() as rs:
        return [
            CustomEdgeModel(
                id=e.id,
                source_repo=e.source_repo,
                target_repo=e.target_repo,
                reason=e.reason,
                created_at=e.created_at,
            )
            for e in rs.list_custom_edges()
        ]


@router.post("/api/relationships/custom", response_model=CustomEdgeModel)
def add_custom_edge(body: CustomEdgeRequest) -> CustomEdgeModel:
    with _open_relationships() as rs:
        e = rs.add_custom_edge(body.source_repo, body.target_repo, body.reason)
        return CustomEdgeModel(
            id=e.id,
            source_repo=e.source_repo,
            target_repo=e.target_repo,
            reason=e.reason,
            created_at=e.created_at,
        )


@router.delete("/api/relationships/custom/{edge_id}")
def delete_custom_edge(edge_id: int) -> dict:
    with _open_relationships() as rs:
        rs.delete_custom_edge(edge_id)
        return {"ok": True}


# ── Metrics endpoints ──


def _period_to_since(period: str) -> float | None:
    """Convert a period string to a UTC epoch cutoff, or None for all time."""
    now = datetime.now(tz=UTC)
    if period == "day":
        return (now - timedelta(days=1)).timestamp()
    if period == "week":
        return (now - timedelta(weeks=1)).timestamp()
    if period == "month":
        return (now - timedelta(days=30)).timestamp()
    return None


@router.get("/api/stats", response_model=OrgStatsModel)
def get_org_stats(period: str = "") -> OrgStatsModel:
    """Aggregate stats across all repos, optionally filtered by period."""
    since = _period_to_since(period) if period else None

    repos = _app_db.list_repos()
    total_repos = len(repos)
    total_files = 0
    agg_stats: dict = {
        "total_reviews": 0,
        "total_comments": 0,
        "total_blockers": 0,
        "total_warnings": 0,
        "total_suggestions": 0,
        "total_files_reviewed": 0,
        "total_lines_changed": 0,
        "total_tokens": 0,
        "avg_duration_ms": 0,
        "categories": {},
        "avg_comments_per_pr": 0.0,
    }
    duration_sum = 0
    review_count = 0

    for repo_record in repos:
        try:
            store = IndexStore.open(repo_record.owner, repo_record.repo)
            total_files += len(store.all_paths())
            stats = store.get_review_stats(since=since)
            agg_stats["total_reviews"] += stats["total_reviews"]
            agg_stats["total_comments"] += stats["total_comments"]
            agg_stats["total_blockers"] += stats["total_blockers"]
            agg_stats["total_warnings"] += stats["total_warnings"]
            agg_stats["total_suggestions"] += stats["total_suggestions"]
            agg_stats["total_files_reviewed"] += stats["total_files_reviewed"]
            agg_stats["total_lines_changed"] += stats["total_lines_changed"]
            agg_stats["total_tokens"] += stats["total_tokens"]
            for cat, cnt in stats.get("categories", {}).items():
                agg_stats["categories"][cat] = agg_stats["categories"].get(cat, 0) + cnt
            if stats["total_reviews"] > 0:
                duration_sum += stats["avg_duration_ms"] * stats["total_reviews"]
                review_count += stats["total_reviews"]
            store.close()
        except Exception:
            logger.warning(
                "Failed to read stats for %s/%s", repo_record.owner, repo_record.repo, exc_info=True
            )

    agg_stats["avg_duration_ms"] = int(duration_sum / review_count) if review_count > 0 else 0
    agg_stats["avg_comments_per_pr"] = (
        round(agg_stats["total_comments"] / review_count, 1) if review_count > 0 else 0.0
    )

    # Get relationship counts
    total_edges = 0
    total_groups = 0
    try:
        with _open_relationships() as rs:
            total_edges = len(rs.resolve_edges())
            total_groups = len(rs.group_repos(rs.repos))
    except Exception:
        pass

    return OrgStatsModel(
        total_repos=total_repos,
        total_files=total_files,
        total_edges=total_edges,
        total_groups=total_groups,
        review_stats=ReviewStatsModel(**agg_stats),
    )


class TimeSeriesPoint(BaseModel):
    date: str
    reviews: int = 0
    comments: int = 0
    blockers: int = 0
    warnings: int = 0
    suggestions: int = 0
    lines_changed: int = 0
    tokens_used: int = 0
    categories: dict[str, int] = {}


@router.get("/api/stats/timeseries", response_model=list[TimeSeriesPoint])
def get_timeseries(period: str = "day") -> list[TimeSeriesPoint]:
    """Aggregate review metrics over time. Period: day, week, month."""
    all_events: list[dict] = []

    for repo_record in _app_db.list_repos():
        try:
            store = IndexStore.open(repo_record.owner, repo_record.repo)
            for e in store.list_review_events(limit=500):
                all_events.append(
                    {
                        "created_at": e.created_at,
                        "comments": e.comments_posted,
                        "blockers": e.blockers,
                        "warnings": e.warnings,
                        "suggestions": e.suggestions,
                        "lines": e.lines_changed,
                        "tokens": e.tokens_used,
                        "categories": e.categories,
                    }
                )
            store.close()
        except Exception:
            pass

    if not all_events:
        return []

    # Bucket by period
    from collections import defaultdict

    buckets: dict[str, dict] = defaultdict(
        lambda: {
            "reviews": 0,
            "comments": 0,
            "blockers": 0,
            "warnings": 0,
            "suggestions": 0,
            "lines_changed": 0,
            "tokens_used": 0,
            "categories": {},
        }
    )

    for ev in all_events:
        dt = datetime.fromtimestamp(ev["created_at"], tz=UTC)
        if period == "month":
            key = dt.strftime("%Y-%m")
        elif period == "week":
            key = dt.strftime("%Y-W%W")
        else:
            key = dt.strftime("%Y-%m-%d")

        b = buckets[key]
        b["reviews"] += 1
        b["comments"] += ev["comments"]
        b["blockers"] += ev["blockers"]
        b["warnings"] += ev["warnings"]
        b["suggestions"] += ev["suggestions"]
        b["lines_changed"] += ev["lines"]
        b["tokens_used"] += ev["tokens"]
        for c in (ev["categories"] or "").split(","):
            c = c.strip()
            if c:
                b["categories"][c] = b["categories"].get(c, 0) + 1

    return [TimeSeriesPoint(date=k, **v) for k, v in sorted(buckets.items())]


@router.post("/api/repos/{owner}/{repo}/index")
async def trigger_index(owner: str, repo: str, full: bool = False) -> dict:
    """Trigger indexing for a repo. full=true wipes and re-indexes everything."""
    from mira.index.status import tracker

    full_name = f"{owner}/{repo}"

    # Check if already indexing
    for j in tracker.get_active():
        if j.repo == full_name:
            return {"status": "already_indexing"}

    # Get GitHub token from app auth if available
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        try:
            from mira.github_app.auth import GitHubAppAuth

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

    # Run indexing in background
    import asyncio

    from mira.config import load_config
    from mira.index.indexer import IndexingCancelled, index_repo
    from mira.llm.provider import LLMProvider

    async def _do_index() -> None:
        count = 0
        store = None
        try:
            from mira.dashboard.models_config import llm_config_for

            tracker.start(full_name)
            config = load_config()
            # Use the configured indexing model (typically Haiku) — without
            # this swap we'd silently fall back to the review model (Sonnet),
            # ~3× slower and ~3× more expensive per token.
            llm = LLMProvider(llm_config_for("indexing", config.llm))
            store = IndexStore.open(owner, repo)
            if full:
                # Wipe existing index
                for path in list(store.all_paths()):
                    store.remove_paths([path])
            count = await index_repo(
                owner=owner,
                repo=repo,
                token=token,
                config=config,
                store=store,
                llm=llm,
                full=full,
                cancel_check=lambda: tracker.is_cancel_requested(full_name),
            )
            # Real indexing run finished — bump last_indexed_at so the
            # dashboard's "Indexed N ago" reflects this completion.
            _app_db.set_repo_status(
                owner,
                repo,
                "ready",
                files_indexed=count,
                bump_last_indexed=True,
            )
            tracker.complete(full_name, count)
            logger.info(
                "Index %s for %s: %d files", "rebuild" if full else "update", full_name, count
            )
        except IndexingCancelled as cancelled:
            tracker.cancel(full_name, cancelled.files_indexed)
            logger.info(
                "Indexing cancelled for %s after %d files", full_name, cancelled.files_indexed
            )
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


# Wire dashboard routes + middleware onto the standalone app, after all
# @router.<verb>(...) decorators above have populated `router`.
register_dashboard(app)
