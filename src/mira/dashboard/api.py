"""FastAPI dashboard API for the Mira UI."""

from __future__ import annotations

import logging
import os
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

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
app = FastAPI(title="Mira Dashboard API", version="0.6.0")

_INDEX_DIR = os.environ.get("MIRA_INDEX_DIR", "/data/indexes")


def _get_index_dir() -> str:
    return os.environ.get("MIRA_INDEX_DIR", _INDEX_DIR)


# Cross-platform preference order used when the same owner/repo exists on
# more than one platform: github → gitlab → forgejo.
_PLATFORM_ORDER = {"github": 0, "gitlab": 1, "forgejo": 2}


def _pick_platform_record(records: list) -> object:
    """Return the highest-priority record from a cross-platform list."""
    return min(records, key=lambda r: _PLATFORM_ORDER.get(r.platform, 99))


@contextmanager
def _open_store(owner: str, repo: str) -> Generator[IndexStore, None, None]:
    """Open an IndexStore via the factory (Postgres or SQLite).

    The dashboard routes are keyed by (owner, repo) only, so resolve the
    platform from the registry with a single cross-platform lookup.
    """
    repo_records = _app_db.get_repo_any_platform(owner, repo)
    if not repo_records:
        raise HTTPException(status_code=404, detail=f"Repo {owner}/{repo} not found")
    repo_record = _pick_platform_record(repo_records)

    store = IndexStore.open(owner, repo, platform=repo_record.platform)
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
    platform: str = "github"
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
    lines_count: int = 0
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


class ActivityEventModel(ReviewEventModel):
    owner: str
    repo: str


class ActivityResponse(BaseModel):
    events: list[ActivityEventModel]
    repos: list[str]


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


class GitLabRepoRegister(BaseModel):
    project: str  # "group/project" or "group/subgroup/project"


class ForgejoRepoRegister(BaseModel):
    project: str  # "owner/repo"


class CostEstimate(BaseModel):
    estimated_usd: float
    input_tokens: int
    output_tokens: int
    model: str
    file_count: int


class ModelOption(BaseModel):
    value: str
    label: str
    recommended: bool = False


class ModelsResponse(BaseModel):
    indexing_model: str
    review_model: str
    backend: str  # "openrouter" | "bedrock" | "openai-compatible"
    indexing_source: str  # "dashboard" (DB override) | "config" (mira.yaml)
    review_source: str
    # What each model resolves to with no override — the "inherit" target.
    config_indexing_model: str
    config_review_model: str
    indexing_options: list[ModelOption]
    review_options: list[ModelOption]
    # Extended-thinking effort for reviews ("off"/"low"/"medium"/"high").
    review_thinking_mode: str
    thinking_options: list[ModelOption]


class ModelsUpdate(BaseModel):
    indexing_model: str
    review_model: str
    review_thinking_mode: str = "off"


class GlobalSettingsResponse(BaseModel):
    overrides: dict
    effective: dict


class GlobalSettingsUpdate(BaseModel):
    overrides: dict


# Only `filter` and `review` are admin-editable from the UI; LLM creds and
# DB settings stay env-only and would be silently overwritten if exposed
# here.
_ALLOWED_OVERRIDE_SECTIONS = {"filter", "review"}


def _humanize_pydantic_message(err: dict) -> str:
    """Pydantic 'Input should be less than or equal to 1' → 'must be ≤ 1'."""
    err_type = err.get("type", "")
    ctx = err.get("ctx") or {}
    if err_type == "less_than_equal":
        return f"must be ≤ {ctx.get('le')}"
    if err_type == "greater_than_equal":
        return f"must be ≥ {ctx.get('ge')}"
    if err_type == "less_than":
        return f"must be < {ctx.get('lt')}"
    if err_type == "greater_than":
        return f"must be > {ctx.get('gt')}"
    if err_type in ("int_parsing", "int_type", "float_parsing", "float_type"):
        return "must be a number"
    if err_type in ("bool_parsing", "bool_type"):
        return "must be true or false"
    if err_type == "string_type":
        return "must be text"
    return err.get("msg", "invalid value")


# ── Outbound webhooks (admin) ────────────────────────────────────────────────


class WebhookCreate(BaseModel):
    name: str = ""
    url: str
    events: list[str] = Field(default_factory=list)
    enabled: bool = True


class WebhookUpdate(BaseModel):
    name: str | None = None
    # Blank/omitted url keeps the stored one so the masked value round-trips
    # without forcing the admin to re-enter the secret.
    url: str | None = None
    events: list[str] | None = None
    enabled: bool | None = None


def _require_admin(request: Request) -> None:
    user = getattr(request.state, "user", None)
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")


def _webhook_public(w: dict) -> dict:
    """Webhook with its URL masked — safe to return from the API."""
    from mira.outbound_webhooks import detect_format, mask_url

    return {
        "id": w.get("id", ""),
        "name": w.get("name", ""),
        "url_masked": mask_url(w.get("url", "")),
        "events": w.get("events", []),
        "enabled": w.get("enabled", True),
        "format": detect_format(w.get("url", "")),
    }


class PendingUninstallModel(BaseModel):
    installation_id: int
    owner: str


class SetupRequest(BaseModel):
    repos: list[dict]  # [{"owner": "x", "repo": "y", "platform": "github", "enabled": true}]
    index_mode: str  # "full" or "light"


async def _run_initial_indexing(default_mode: str) -> None:
    """Index repos that `complete_setup` just enabled.

    Filtering on ``status`` is what scopes this to "just this setup batch" —
    a bare ``index_mode != 'none'`` filter would re-index every previously
    ready repo every time a new install lands.
    """
    from mira.index.status import tracker

    repos = _app_db.list_repos()
    to_index = [r for r in repos if r.index_mode != "none" and r.status in ("pending", "indexing")]

    if not to_index:
        return

    # Resolve a GitHub token once (used for github repos). GitLab repos use
    # MIRA_GITLAB_TOKEN instead — each repo gets a fetcher for its platform.
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token and any(r.platform == "github" for r in to_index):
        try:
            from mira.platforms.github.auth import GitHubAppAuth

            app_id = os.environ.get("MIRA_GITHUB_APP_ID", "")
            private_key = os.environ.get("MIRA_GITHUB_PRIVATE_KEY", "")
            if app_id and private_key:
                auth = GitHubAppAuth(app_id=app_id, private_key=private_key)
                gh = next((r for r in to_index if r.platform == "github"), None)
                if gh and gh.installation_id:
                    github_token = await auth.get_installation_token(gh.installation_id)
        except Exception as exc:
            logger.warning("Failed to get GitHub token for indexing: %s", exc)
    gitlab_token = os.environ.get("MIRA_GITLAB_TOKEN", "")
    forgejo_token = os.environ.get("MIRA_FORGEJO_TOKEN", "")

    from mira.config import load_config
    from mira.dashboard.models_config import llm_config_for
    from mira.index.indexer import index_repo
    from mira.llm import create_llm
    from mira.platforms.fetch import EmptyRepoError, make_fetcher

    config = load_config()
    llm = create_llm(llm_config_for("indexing", config.llm))

    for repo_record in to_index:
        owner, repo, platform = repo_record.owner, repo_record.repo, repo_record.platform
        full_name = f"{owner}/{repo}"
        token = (
            gitlab_token
            if platform == "gitlab"
            else (forgejo_token if platform == "forgejo" else github_token)
        )
        if not token:
            # No usable token for this platform — leave it pending instead of
            # crashing on an empty auth header.
            logger.warning("Skipping initial index of %s — no %s token", full_name, platform)
            continue
        try:
            _app_db.set_repo_status(owner, repo, "indexing", platform=platform)
            tracker.start(full_name)
            store = IndexStore.open(owner, repo, platform=platform)
            count = await index_repo(
                owner=owner,
                repo=repo,
                fetcher=make_fetcher(platform, token),
                config=config,
                store=store,
                llm=llm,
                full=(repo_record.index_mode == "full"),
            )
            store.close()
            _app_db.set_repo_status(
                owner,
                repo,
                "ready",
                files_indexed=count,
                bump_last_indexed=True,
                platform=platform,
            )
            tracker.complete(full_name, count)
            logger.info("Indexed %s: %d files", full_name, count)
            from mira.outbound_webhooks import INDEXING_COMPLETED, dispatch_event

            await dispatch_event(INDEXING_COMPLETED, {"repo": full_name, "files_indexed": count})
        except EmptyRepoError as empty:
            _app_db.set_repo_status(owner, repo, "empty", error=str(empty), platform=platform)
            tracker.complete(full_name, 0)
        except Exception as exc:
            _app_db.set_repo_status(owner, repo, "failed", error=str(exc), platform=platform)
            tracker.fail(full_name, str(exc))
            logger.exception("Failed to index %s", full_name)


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


class PackageModel(BaseModel):
    name: str
    kind: str  # "npm" | "pip" | "docker" | "go" | "rust" | "composer"
    version: str
    file_path: str
    is_dev: bool = False


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


class VulnerabilitySummary(BaseModel):
    total: int = 0
    critical: int = 0
    high: int = 0
    moderate: int = 0
    low: int = 0
    unknown: int = 0


class OrgVulnerabilityModel(VulnerabilityModel):
    owner: str
    repo: str


# ── Review context endpoints ──


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
    id: int = 0
    rule_text: str
    source_signal: str  # "reject_pattern" | "accept_pattern" | "human_pattern" | "manual"
    category: str
    path_pattern: str = ""
    sample_count: int = 0
    active: bool = True
    status: str = "approved"  # 'pending' | 'approved' | 'rejected'
    created_by: str = ""  # admin username for manual rules; '' for synthesized
    updated_at: float = 0.0


class OrgLearnedRuleModel(LearnedRuleModel):
    owner: str
    repo: str


class LearnedRuleInput(BaseModel):
    rule_text: str
    category: str = "other"
    path_pattern: str = ""


class LearnedRuleActiveInput(BaseModel):
    active: bool


# ── Global rules endpoints ──


# ── Relationship override endpoints ──


# ── Custom edge endpoints ──


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


# Importing the router modules runs their @router decorators, populating
# `router` before it's wired onto the app below. Side-effect imports (the
# submodule form avoids binding names that collide with locals here).
import mira.dashboard.routers.admin  # noqa: E402,F401
import mira.dashboard.routers.core  # noqa: E402,F401
import mira.dashboard.routers.relationships  # noqa: E402,F401
import mira.dashboard.routers.repos  # noqa: E402,F401
import mira.dashboard.routers.rules  # noqa: E402,F401
import mira.dashboard.routers.vulnerabilities  # noqa: E402,F401

# Wire dashboard routes + middleware onto the standalone app, after all
# @router.<verb>(...) decorators above have populated `router`.
register_dashboard(app)
