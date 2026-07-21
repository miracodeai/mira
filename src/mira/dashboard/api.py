"""FastAPI dashboard API for the Mira UI."""

from __future__ import annotations

import asyncio
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
app = FastAPI(title="Mira Dashboard API", version="0.7.0")

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
    author_username: str = ""
    author_avatar_url: str = ""


class ActivityResponse(BaseModel):
    events: list[ActivityEventModel]
    repos: list[str]


class ReviewCommentModel(BaseModel):
    id: int
    review_id: int
    path: str
    line: int
    severity: str
    category: str
    title: str
    body: str
    github_comment_id: int = 0
    created_at: float


class PRReplyModel(BaseModel):
    id: int
    author: str
    author_avatar_url: str
    body: str
    comment_path: str
    comment_line: int
    in_reply_to_id: int
    created_at: float


class ActivityReviewModel(ReviewEventModel):
    """One review pass plus the individual comments it posted."""

    reviewed_paths: list[str] = []
    comments: list[ReviewCommentModel] = []


class ActivityDetailModel(BaseModel):
    owner: str
    repo: str
    pr_number: int
    pr_title: str
    pr_url: str
    author_username: str = ""
    author_avatar_url: str = ""
    reviews: list[ActivityReviewModel]
    replies: list[PRReplyModel]


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


# ── Contributors ───────────────────────────────────────────────────


class ContributorListItem(BaseModel):
    id: int
    provider: str
    login: str
    display_name: str = ""
    avatar_url: str = ""
    is_bot: bool = False
    prs_opened: int = 0
    prs_merged: int = 0
    commits: int = 0
    reviews: int = 0
    additions: int = 0
    deletions: int = 0
    last_active: float | None = None
    repos_touched: int = 0


class HeatmapDay(BaseModel):
    day: str
    total: int = 0
    commits: int = 0
    prs_opened: int = 0
    prs_merged: int = 0
    reviews: int = 0


class ContributorRepoBreakdown(BaseModel):
    owner: str
    repo: str
    commits: int = 0
    prs_opened: int = 0
    prs_merged: int = 0
    reviews: int = 0


class ReviewQuality(BaseModel):
    """Mira's differentiated signal: how much review attention a person's PRs
    drew, plus how often Mira's feedback on them was accepted."""

    reviews: int = 0
    blockers: int = 0
    warnings: int = 0
    suggestions: int = 0
    feedback_accepted: int = 0
    feedback_rejected: int = 0
    accept_rate: float = 0.0


class ContributorDetailModel(BaseModel):
    contributor: ContributorListItem
    heatmap: list[HeatmapDay]
    repos: list[ContributorRepoBreakdown]
    quality: ReviewQuality


class ContributionWindow(BaseModel):
    commits: int = 0
    prs_opened: int = 0
    prs_merged: int = 0
    reviews: int = 0
    additions: int = 0
    contributors: int = 0


class ContributorSummary(BaseModel):
    """Org-wide totals for the trailing window plus the window before it, so the
    UI can show up/down deltas."""

    days: int
    current: ContributionWindow
    previous: ContributionWindow


def _build_app_auth():  # type: ignore[no-untyped-def]
    """Construct GitHubAppAuth from env, or 400 if the App isn't configured."""
    app_id = os.environ.get("MIRA_GITHUB_APP_ID", "")
    private_key = os.environ.get("MIRA_GITHUB_PRIVATE_KEY", "")
    if not app_id or not private_key:
        raise HTTPException(status_code=400, detail="GitHub App not configured")
    from mira.platforms.github.auth import GitHubAppAuth

    return GitHubAppAuth(app_id=app_id, private_key=private_key)


@router.get("/api/contributors", response_model=list[ContributorListItem])
def list_contributors(
    request: Request, sort: str = "commits", period: str = "", include_bots: bool = False
) -> list[ContributorListItem]:
    """Cross-repo contributor leaderboard (admin only). sort: commits|prs|reviews|recent|additions."""
    _require_admin(request)
    since = _period_to_since(period) if period else None
    rows = _app_db.list_contributors(sort=sort, since=since, include_bots=include_bots)
    return [ContributorListItem(**r) for r in rows]


@router.get("/api/contributors/summary", response_model=ContributorSummary)
def contributors_summary(request: Request, days: int = 7) -> ContributorSummary:
    """Org-wide totals for the trailing `days` window and the window before it,
    so the UI can show up/down trends. Admin only."""
    _require_admin(request)
    days = max(1, min(days, 365))
    now = datetime.now(tz=UTC).timestamp()
    window = days * 86400
    current = _app_db.aggregate_contributions(now - window, now)
    previous = _app_db.aggregate_contributions(now - 2 * window, now - window)
    return ContributorSummary(
        days=days,
        current=ContributionWindow(**current),
        previous=ContributionWindow(**previous),
    )


@router.get("/api/contributors/backfill/status")
def contributors_backfill_status(request: Request) -> list[dict]:
    """Per-repo backfill progress blobs (status/prs_done/total/error). Admin only."""
    _require_admin(request)
    from mira.platforms.github.contributor_backfill import get_backfill_status

    out: list[dict] = []
    for rec in _app_db.list_repos():
        status = get_backfill_status(_app_db, rec.owner, rec.repo)
        if status:
            out.append({"owner": rec.owner, "repo": rec.repo, **status})
    return out


@router.post("/api/contributors/refresh")
async def refresh_contributors(request: Request, include_commits: bool = True) -> dict:
    """Kick off a background backfill of every repo (admin only)."""
    _require_admin(request)
    auth = _build_app_auth()
    from mira.platforms.github.contributor_backfill import backfill_all_repos

    asyncio.create_task(backfill_all_repos(auth, include_commits=include_commits))
    return {"status": "refreshing"}


@router.post("/api/contributors/{owner}/{repo}/refresh")
async def refresh_contributors_repo(
    owner: str, repo: str, request: Request, include_commits: bool = True
) -> dict:
    """Kick off a background backfill of one repo (admin only)."""
    _require_admin(request)
    rec = _app_db.get_repo(owner, repo)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"Repo {owner}/{repo} not found")
    auth = _build_app_auth()
    from mira.platforms.github.contributor_backfill import backfill_repo_contributions

    asyncio.create_task(
        backfill_repo_contributions(
            owner, repo, auth, installation_id=rec.installation_id, include_commits=include_commits
        )
    )
    return {"status": "refreshing"}


@router.get("/api/contributors/{login}", response_model=ContributorDetailModel)
def get_contributor(login: str, request: Request, period: str = "") -> ContributorDetailModel:
    """One contributor's aggregated stats, 365-day heatmap, per-repo breakdown,
    and Mira review-quality signal (the quality fan-out mirrors /api/stats). Admin only."""
    _require_admin(request)
    contributor = _app_db.get_contributor_by_login("github", login)
    if contributor is None:
        raise HTTPException(status_code=404, detail=f"Contributor {login} not found")
    since = _period_to_since(period) if period else None

    totals = _app_db.get_contributor_totals(contributor.id, since=since)
    item = ContributorListItem(
        id=contributor.id,
        provider=contributor.provider,
        login=contributor.external_login,
        display_name=contributor.display_name,
        avatar_url=contributor.avatar_url,
        is_bot=contributor.is_bot,
        last_active=totals["last_active"],
        repos_touched=totals["repos_touched"],
        prs_opened=totals["prs_opened"],
        prs_merged=totals["prs_merged"],
        commits=totals["commits"],
        reviews=totals["reviews"],
        additions=totals["additions"],
        deletions=totals["deletions"],
    )

    # 365-day heatmap window ending today (UTC).
    today = datetime.now(tz=UTC).date()
    start_day = (today - timedelta(days=364)).strftime("%Y-%m-%d")
    end_day = today.strftime("%Y-%m-%d")
    heatmap = [
        HeatmapDay(
            day=d.day,
            total=d.total,
            commits=d.commits,
            prs_opened=d.prs_opened,
            prs_merged=d.prs_merged,
            reviews=d.reviews,
        )
        for d in _app_db.get_contributor_days(contributor.id, start_day, end_day)
    ]

    repos = [
        ContributorRepoBreakdown(**b)
        for b in _app_db.get_contributor_repo_breakdown(contributor.id, since=since)
    ]

    # Review-quality lives per-repo in IndexStore; fan out like /api/stats.
    q = {"reviews": 0, "blockers": 0, "warnings": 0, "suggestions": 0}
    accepted = rejected = 0
    for repo_record in _app_db.list_repos():
        try:
            store = IndexStore.open(
                repo_record.owner, repo_record.repo, platform=repo_record.platform
            )
            try:
                rq = store.get_review_quality_by_author(contributor.external_login, since=since)
                fq = store.get_feedback_quality_by_author(contributor.external_login)
            finally:
                store.close()
            for key in q:
                q[key] += rq[key]
            accepted += fq["accepted"]
            rejected += fq["rejected"]
        except Exception:
            logger.warning(
                "Failed to read contributor quality for %s/%s",
                repo_record.owner,
                repo_record.repo,
                exc_info=True,
            )
    total_fb = accepted + rejected
    quality = ReviewQuality(
        reviews=q["reviews"],
        blockers=q["blockers"],
        warnings=q["warnings"],
        suggestions=q["suggestions"],
        feedback_accepted=accepted,
        feedback_rejected=rejected,
        accept_rate=round(accepted / total_fb, 3) if total_fb else 0.0,
    )

    return ContributorDetailModel(contributor=item, heatmap=heatmap, repos=repos, quality=quality)


# ── Review insights ────────────────────────────────────────────────


class ThroughputWindow(BaseModel):
    time_to_first_review_secs: float | None = None
    time_to_first_review_count: int = 0
    time_to_merge_secs: float | None = None
    time_to_merge_count: int = 0


class HealthComponent(BaseModel):
    key: str
    label: str
    score: float  # 0–1
    detail: str


class ReviewSummary(BaseModel):
    days: int
    open_prs: int
    stale_prs: int
    awaiting_review: int
    merged: int = 0
    approved_merged: int = 0
    approvals: int = 0  # approvals submitted in the window (org-wide)
    rubber_stamps: int = 0  # of those, approvals with no substantive review
    health_score: int | None = None  # 0–100
    health: list[HealthComponent] = []
    current: ThroughputWindow
    previous: ThroughputWindow


class ReviewerStat(BaseModel):
    reviewer: str
    avatar_url: str = ""
    pending: int = 0  # PRs currently waiting on this person's review
    reviews: int = 0  # reviews submitted in the window
    median_response_secs: float | None = None  # requested → first review
    approvals: int = 0  # approvals submitted in the window
    rubber_stamps: int = 0  # approvals with no substantive review
    rubber_stamp_rate: float = 0.0  # rubber_stamps / approvals × 100


class OpenPrReviewer(BaseModel):
    reviewer: str
    state: str = ""
    requested: bool = False
    responded: bool = False


class OpenPr(BaseModel):
    owner: str
    repo: str
    number: int
    author: str
    title: str
    url: str
    draft: bool
    created_at: float
    updated_at: float
    age_secs: float
    idle_secs: float
    reviewed: bool
    stale: bool
    status: str  # awaiting_review | commented | changes_requested | approved
    waiting_on: list[str]
    reviewers: list[OpenPrReviewer]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _now() -> float:
    return datetime.now(tz=UTC).timestamp()


def _fmt_secs(secs: float) -> str:
    """Compact human duration for health-component detail text."""
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


@router.get("/api/review-insights/summary", response_model=ReviewSummary)
def review_summary(request: Request, days: int = 7, stale_days: int = 3) -> ReviewSummary:
    """Throughput medians (this window vs previous) + open/stale/awaiting counts. Admin."""
    _require_admin(request)
    days = max(1, min(days, 365))
    now = _now()
    window = days * 86400

    rows = _app_db.get_throughput_rows()

    def window_throughput(start: float, end: float) -> ThroughputWindow:
        ttfr = [
            r["first_review_at"] - r["created_at"]
            for r in rows
            if r["first_review_at"] > 0
            and start <= r["first_review_at"] < end
            and r["created_at"] > 0
            and r["first_review_at"] >= r["created_at"]
        ]
        ttm = [
            r["merged_at"] - r["created_at"]
            for r in rows
            if r["merged_at"] > 0
            and start <= r["merged_at"] < end
            and r["created_at"] > 0
            and r["merged_at"] >= r["created_at"]
        ]
        return ThroughputWindow(
            time_to_first_review_secs=_median(ttfr),
            time_to_first_review_count=len(ttfr),
            time_to_merge_secs=_median(ttm),
            time_to_merge_count=len(ttm),
        )

    open_prs = _app_db.get_open_pull_requests()
    stale_cutoff = stale_days * 86400
    active = [p for p in open_prs if not p["draft"]]
    stale = sum(1 for p in active if now - p["updated_at"] > stale_cutoff)
    awaiting = sum(1 for p in active if p["first_review_at"] == 0)

    current = window_throughput(now - window, now)
    previous = window_throughput(now - 2 * window, now - window)

    # ── Health score ── proof that humans review, approve, and merge.
    merged_rows = _app_db.get_merged_pr_quality(now - window, now)
    merged_n = len(merged_rows)
    approved_n = sum(1 for m in merged_rows if m["approved"])

    components: list[HealthComponent] = []
    weights: list[float] = []
    # 40% — share of merges that a human approved.
    if merged_n:
        rate = approved_n / merged_n
        components.append(
            HealthComponent(
                key="approvals",
                label="Merges approved by a human",
                score=rate,
                detail=f"{approved_n} of {merged_n} merged PRs had an approval",
            )
        )
        weights.append(0.4)
    # 30% — reviews start promptly (24h target).
    ttfr = current.time_to_first_review_secs
    if ttfr:
        resp = max(0.0, min(1.0, 86400 / ttfr))
        components.append(
            HealthComponent(
                key="responsiveness",
                label="Reviews start promptly",
                score=resp,
                detail=f"median first review in {_fmt_secs(ttfr)} (target 24h)",
            )
        )
        weights.append(0.3)
    # 30% — open PRs aren't going stale.
    backlog = 1.0 - (stale / len(active)) if active else 1.0
    components.append(
        HealthComponent(
            key="backlog",
            label="Open PRs aren't going stale",
            score=backlog,
            detail=(f"{stale} of {len(active)} open PRs are stale" if active else "no open PRs"),
        )
    )
    weights.append(0.3)

    total_w = sum(weights)
    health_score = (
        round(100 * sum(c.score * w for c, w in zip(components, weights, strict=True)) / total_w)
        if total_w
        else None
    )

    # ── Org-wide rubber-stamps ── approvals (in window) with no substantive review.
    org_approvals = 0
    org_rubber_stamps = 0
    for r in _app_db.get_reviewer_activity_rows():
        if r["responded_at"] >= now - window and r["review_state"] == "approved":
            org_approvals += 1
            if r["bare_approval"]:
                org_rubber_stamps += 1

    return ReviewSummary(
        days=days,
        open_prs=len(active),
        stale_prs=stale,
        awaiting_review=awaiting,
        merged=merged_n,
        approved_merged=approved_n,
        approvals=org_approvals,
        rubber_stamps=org_rubber_stamps,
        health_score=health_score,
        health=components,
        current=current,
        previous=previous,
    )


@router.get("/api/review-insights/open-prs", response_model=list[OpenPr])
def review_open_prs(request: Request, stale_days: int = 3) -> list[OpenPr]:
    """Open PRs as a status board: age, idle time, who they're waiting on,
    reviewer states, and a stale flag. Sorted oldest-first. Admin."""
    _require_admin(request)
    now = _now()
    stale_cutoff = stale_days * 86400

    by_pr: dict[tuple[str, str, int], list[dict]] = {}
    for r in _app_db.get_open_pr_reviewers():
        by_pr.setdefault((r["owner"], r["repo"], r["number"]), []).append(r)

    out: list[OpenPr] = []
    for p in _app_db.get_open_pull_requests():
        revs = by_pr.get((p["owner"], p["repo"], p["number"]), [])
        reviewers = [
            OpenPrReviewer(
                reviewer=r["reviewer"],
                state=r["state"],
                requested=r["requested_at"] > 0,
                responded=r["responded_at"] > 0,
            )
            for r in revs
        ]
        waiting_on = [r.reviewer for r in reviewers if r.requested and not r.responded]
        responded_states = [r.state for r in reviewers if r.responded]
        if "changes_requested" in responded_states:
            status = "changes_requested"
        elif "approved" in responded_states:
            status = "approved"
        elif p["first_review_at"] > 0 or responded_states:
            status = "commented"
        else:
            status = "awaiting_review"
        idle = now - p["updated_at"]
        out.append(
            OpenPr(
                owner=p["owner"],
                repo=p["repo"],
                number=p["number"],
                author=p["author"],
                title=p["title"],
                url=p["url"],
                draft=p["draft"],
                created_at=p["created_at"],
                updated_at=p["updated_at"],
                age_secs=now - p["created_at"],
                idle_secs=idle,
                reviewed=p["first_review_at"] > 0,
                stale=(not p["draft"]) and idle > stale_cutoff,
                status=status,
                waiting_on=waiting_on,
                reviewers=reviewers,
            )
        )
    out.sort(key=lambda o: o.age_secs, reverse=True)
    return out


@router.get("/api/review-insights/reviewers", response_model=list[ReviewerStat])
def review_reviewers(request: Request, days: int = 30) -> list[ReviewerStat]:
    """Per-reviewer responsiveness: current pending queue + median response time
    + reviews in the window. The bottleneck floats to the top. Admin."""
    _require_admin(request)
    since = _now() - max(1, days) * 86400

    pending: dict[str, int] = {}
    latencies: dict[str, list[float]] = {}
    reviews: dict[str, int] = {}
    approvals: dict[str, int] = {}
    rubber_stamps: dict[str, int] = {}
    for r in _app_db.get_reviewer_activity_rows():
        who = r["reviewer"]
        req, resp = r["requested_at"], r["responded_at"]
        if req > 0 and resp == 0 and r["pr_state"] == "open":
            pending[who] = pending.get(who, 0) + 1
        if resp > 0 and resp >= since:
            reviews[who] = reviews.get(who, 0) + 1
            if req > 0 and resp >= req:
                latencies.setdefault(who, []).append(resp - req)
            if r["review_state"] == "approved":
                approvals[who] = approvals.get(who, 0) + 1
                if r["bare_approval"]:
                    rubber_stamps[who] = rubber_stamps.get(who, 0) + 1

    avatars = {c["login"]: c["avatar_url"] for c in _app_db.list_contributors(include_bots=True)}
    everyone = set(pending) | set(reviews)
    stats = [
        ReviewerStat(
            reviewer=who,
            avatar_url=avatars.get(who, ""),
            pending=pending.get(who, 0),
            reviews=reviews.get(who, 0),
            median_response_secs=_median(latencies.get(who, [])),
            approvals=approvals.get(who, 0),
            rubber_stamps=rubber_stamps.get(who, 0),
            rubber_stamp_rate=(
                round(rubber_stamps.get(who, 0) / approvals[who] * 100, 1)
                if approvals.get(who)
                else 0.0
            ),
        )
        for who in everyone
    ]
    # Bottleneck first: biggest pending queue, then slowest median response.
    stats.sort(key=lambda s: (s.pending, s.median_response_secs or 0), reverse=True)
    return stats


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


@router.get("/api/activity/{owner}/{repo}/{pr_number}", response_model=ActivityDetailModel)
def get_activity_detail(owner: str, repo: str, pr_number: int) -> ActivityDetailModel:
    """Full detail for a single PR: every review pass (with the comments it
    posted and the files it reviewed) plus human replies — i.e. the data the
    activity timeline renders."""
    import json as _json

    with _open_store(owner, repo) as store:
        all_events = store.list_review_events_for_pr(pr_number)
        # Guard against a mis-scoped store: per-repo SQLite DBs have no
        # owner/repo columns, so validate via the pr_url each event recorded.
        marker = f"/{owner}/{repo}/"
        all_events = [e for e in all_events if not e.pr_url or marker in e.pr_url]
        if not all_events:
            raise HTTPException(status_code=404, detail="No reviews for this PR")

        comments_by_review: dict[int, list[ReviewCommentModel]] = {}
        for c in store.list_review_comments(pr_number):
            comments_by_review.setdefault(c.review_id, []).append(
                ReviewCommentModel(
                    id=c.id,
                    review_id=c.review_id,
                    path=c.path,
                    line=c.line,
                    severity=c.severity,
                    category=c.category,
                    title=c.title,
                    body=c.body,
                    github_comment_id=c.github_comment_id,
                    created_at=c.created_at,
                )
            )

        def _paths(raw: str) -> list[str]:
            try:
                return _json.loads(raw) if raw else []
            except Exception:
                return []

        reviews = [
            ActivityReviewModel(
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
                reviewed_paths=_paths(e.reviewed_paths),
                comments=comments_by_review.get(e.id, []),
            )
            for e in all_events
        ]

        replies = [
            PRReplyModel(
                id=r.id,
                author=r.author,
                author_avatar_url=r.author_avatar_url,
                body=r.body,
                comment_path=r.comment_path,
                comment_line=r.comment_line,
                in_reply_to_id=r.in_reply_to_id,
                created_at=r.created_at,
            )
            for r in store.list_replies(pr_number)
        ]

        latest = all_events[0]
        return ActivityDetailModel(
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            pr_title=latest.pr_title,
            pr_url=latest.pr_url,
            author_username=latest.author,
            author_avatar_url=latest.author_avatar_url,
            reviews=reviews,
            replies=replies,
        )


# Wire dashboard routes + middleware onto the standalone app, after all
# @router.<verb>(...) decorators above have populated `router`.
register_dashboard(app)
