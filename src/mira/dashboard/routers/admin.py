"""Dashboard admin routes"""

from __future__ import annotations

import asyncio
import os

from fastapi import HTTPException, Request
from pydantic import BaseModel

from mira.dashboard import api as _api
from mira.dashboard.api import (
    _ALLOWED_OVERRIDE_SECTIONS,
    ForgejoRepoRegister,
    GitLabRepoRegister,
    GlobalSettingsResponse,
    GlobalSettingsUpdate,
    ModelOption,
    ModelsResponse,
    ModelsUpdate,
    PendingUninstallModel,
    SetupRequest,
    WebhookCreate,
    WebhookUpdate,
    _humanize_pydantic_message,
    _require_admin,
    _run_initial_indexing,
    _webhook_public,
    logger,
    router,
)
from mira.index.store import IndexStore


async def _sync_platform_repos(
    platform: str, env_token: str, auth_class, backfill_fn, default_base_url: str, env_api_url: str
) -> dict:
    token = os.environ.get(env_token, "")
    if not token:
        raise HTTPException(status_code=400, detail=f"{env_token} is not configured")
    base_url = os.environ.get(env_api_url, default_base_url)
    count = await backfill_fn(auth_class(token, base_url))
    return {"registered": count}


async def _register_and_index_repo(platform: str, env_token: str, body: BaseModel) -> dict:
    token = os.environ.get(env_token, "")
    if not token:
        raise HTTPException(status_code=400, detail=f"{env_token} is not configured")
    owner, _, repo = body.project.strip("/").rpartition("/")
    if not owner or not repo:
        raise HTTPException(status_code=400, detail="project must be 'owner/repo'")

    _api._app_db.register_repo(owner, repo, platform=platform)
    _api._app_db.set_repo_status(owner, repo, "indexing", platform=platform)

    async def _index() -> None:
        from mira.index.indexer import index_repo
        from mira.platforms.fetch import EmptyRepoError, make_fetcher

        store = None
        try:
            store = IndexStore.open(owner, repo, platform=platform)
            count = await index_repo(
                owner=owner, repo=repo, store=store, fetcher=make_fetcher(platform, token)
            )
            _api._app_db.set_repo_status(
                owner, repo, "ready", files_indexed=count, bump_last_indexed=True, platform=platform
            )
        except EmptyRepoError as empty:
            _api._app_db.set_repo_status(owner, repo, "empty", error=str(empty), platform=platform)
        except Exception as exc:
            logger.exception("%s indexing failed for %s/%s", platform.title(), owner, repo)
            _api._app_db.set_repo_status(owner, repo, "failed", error=str(exc), platform=platform)
        finally:
            if store is not None:
                store.close()

    asyncio.create_task(_index())
    return {"status": "indexing", "owner": owner, "repo": repo, "platform": platform}


@router.post("/api/gitlab/sync")
async def sync_gitlab_projects() -> dict:
    """Discover every GitLab project the configured token can access and
    register them (status pending), so they appear ready-to-index without
    waiting for a webhook. Idempotent."""
    from mira.platforms.gitlab.auth import GitLabTokenAuth
    from mira.platforms.gitlab.webhook import backfill_gitlab_projects

    return await _sync_platform_repos(
        "gitlab",
        "MIRA_GITLAB_TOKEN",
        GitLabTokenAuth,
        backfill_gitlab_projects,
        "https://gitlab.com/api/v4",
        "MIRA_GITLAB_API_URL",
    )


@router.post("/api/gitlab/repos")
async def register_gitlab_repo(body: GitLabRepoRegister) -> dict:
    """Register a GitLab project and index it in the background.

    GitLab has no installation webhook, so repos are added explicitly. The
    access token comes from MIRA_GITLAB_TOKEN; add a project webhook pointing
    at ``/gitlab/webhook`` to get auto-review on new MRs.
    """
    return await _register_and_index_repo("gitlab", "MIRA_GITLAB_TOKEN", body)


@router.post("/api/forgejo/sync")
async def sync_forgejo_repos() -> dict:
    """Discover every Forgejo repo the configured token can access and
    register them (status pending), so they appear ready-to-index without
    waiting for a webhook. Idempotent."""
    from mira.platforms.forgejo.auth import ForgejoTokenAuth
    from mira.platforms.forgejo.webhook import backfill_forgejo_repos

    return await _sync_platform_repos(
        "forgejo",
        "MIRA_FORGEJO_TOKEN",
        ForgejoTokenAuth,
        backfill_forgejo_repos,
        "https://codeberg.org/api/v1",
        "MIRA_FORGEJO_API_URL",
    )


@router.post("/api/forgejo/repos")
async def register_forgejo_repo(body: ForgejoRepoRegister) -> dict:
    """Register a Forgejo repo and index it in the background.

    Forgejo has no installation webhook, so repos are added explicitly. The
    access token comes from MIRA_FORGEJO_TOKEN; add a repo webhook pointing
    at ``/forgejo/webhook`` to get auto-review on new PRs.
    """
    return await _register_and_index_repo("forgejo", "MIRA_FORGEJO_TOKEN", body)


@router.get("/api/settings/models", response_model=ModelsResponse)
async def get_models() -> ModelsResponse:
    from mira.config import load_config
    from mira.dashboard.model_catalog import active_backend, build_options, fetch_catalog
    from mira.dashboard.models_config import (
        THINKING_MODES,
        get_indexing_model,
        get_review_model,
        get_review_thinking_mode,
    )

    config = load_config()
    db_indexing = _api._app_db.get_setting("indexing_model")
    db_review = _api._app_db.get_setting("review_model")
    indexing = get_indexing_model(config.llm, db_indexing)
    review = get_review_model(config.llm, db_review)
    thinking = get_review_thinking_mode(
        config.llm, _api._app_db.get_setting("review_thinking_mode")
    )

    backend = active_backend(config.llm)
    catalog = await fetch_catalog(config.llm)

    return ModelsResponse(
        indexing_model=indexing,
        review_model=review,
        backend=backend,
        indexing_source="dashboard" if db_indexing else "config",
        review_source="dashboard" if db_review else "config",
        config_indexing_model=get_indexing_model(config.llm),
        config_review_model=get_review_model(config.llm),
        indexing_options=[ModelOption(**m) for m in build_options(backend, catalog, "indexing")],
        review_options=[ModelOption(**m) for m in build_options(backend, catalog, "review")],
        review_thinking_mode=thinking or "off",
        thinking_options=[ModelOption(**m) for m in THINKING_MODES],
    )


@router.get("/api/admin/settings", response_model=GlobalSettingsResponse)
def get_global_settings(request: Request) -> GlobalSettingsResponse:
    """Return the admin override blob + the effective config."""
    user = getattr(request.state, "user", None)
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    from mira.config import load_config

    overrides = _api._app_db.get_global_review_overrides()
    effective = load_config().model_dump()
    return GlobalSettingsResponse(overrides=overrides, effective=effective)


@router.put("/api/admin/settings")
def set_global_settings(body: GlobalSettingsUpdate, request: Request) -> dict:
    """Replace the admin override blob. Pass `{"overrides": {}}` to clear."""
    user = getattr(request.state, "user", None)
    if not user or not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    bad = set(body.overrides.keys()) - _ALLOWED_OVERRIDE_SECTIONS
    if bad:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Override sections not allowed: {sorted(bad)}. "
                f"Permitted: {sorted(_ALLOWED_OVERRIDE_SECTIONS)}."
            ),
        )

    # Validate before persisting so a typo or wrong type fails the PUT
    # rather than the next PR review. Return a structured error so the
    # UI can render it inline under the offending input rather than as a
    # raw banner.
    from pydantic import ValidationError

    from mira.config import MiraConfig, _deep_merge, _global_defaults

    merged = _deep_merge(_global_defaults, body.overrides)
    try:
        MiraConfig.model_validate(merged)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise HTTPException(
            status_code=400,
            detail={
                "field": ".".join(str(p) for p in first.get("loc", ())),
                "message": _humanize_pydantic_message(first),
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=400, detail={"message": f"Invalid overrides: {exc}"}
        ) from exc

    _api._app_db.set_global_review_overrides(body.overrides)
    return {"ok": True}


@router.put("/api/settings/models")
def set_models(body: ModelsUpdate) -> dict:
    from mira.dashboard.models_config import THINKING_MODE_VALUES

    if body.review_thinking_mode not in THINKING_MODE_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"{body.review_thinking_mode!r} is not a valid thinking mode.",
        )
    # "" clears the override so mira.yaml is authoritative again. Any other id
    # is stored as-is — the dashboard accepts the same free-form model ids as
    # mira.yaml (the dropdown still guides toward registry models), and the
    # registry falls back gracefully for pricing/limits of unknown ids.
    _api._app_db.set_setting("indexing_model", body.indexing_model.strip())
    _api._app_db.set_setting("review_model", body.review_model.strip())
    # Clear "off" to "" rather than persisting the literal — "off" is the
    # default, and a stored value would shadow a mira.yaml
    # `review_reasoning_effort` override. "" (not None — the column is NOT NULL)
    # reads back as unset so the config fallback chain works.
    if body.review_thinking_mode and body.review_thinking_mode != "off":
        _api._app_db.set_setting("review_thinking_mode", body.review_thinking_mode)
    else:
        _api._app_db.set_setting("review_thinking_mode", "")
    _api._app_db.mark_setup_complete()
    return {"ok": True}


@router.get("/api/admin/webhooks")
def list_webhooks(request: Request) -> dict:
    _require_admin(request)
    from mira.outbound_webhooks import AVAILABLE_EVENTS

    webhooks = [_webhook_public(w) for w in _api._app_db.get_webhooks()]
    return {"webhooks": webhooks, "available_events": AVAILABLE_EVENTS}


@router.get("/api/admin/webhooks/{webhook_id}")
def get_webhook(webhook_id: str, request: Request) -> dict:
    """Full webhook incl. the unmasked URL — for the edit form (admin only).

    The list endpoint masks URLs to avoid leaking secrets at a glance, but
    editing a webhook needs the real value populated in the form.
    """
    _require_admin(request)
    from mira.outbound_webhooks import detect_format

    w = next((x for x in _api._app_db.get_webhooks() if x.get("id") == webhook_id), None)
    if w is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return {
        "id": w.get("id", ""),
        "name": w.get("name", ""),
        "url": w.get("url", ""),
        "events": w.get("events", []),
        "enabled": w.get("enabled", True),
        "format": detect_format(w.get("url", "")),
    }


@router.post("/api/admin/webhooks")
def create_webhook(body: WebhookCreate, request: Request) -> dict:
    _require_admin(request)
    import uuid

    from pydantic import ValidationError

    from mira.outbound_webhooks import WebhookConfig

    try:
        cfg = WebhookConfig(
            id=uuid.uuid4().hex,
            name=body.name,
            url=body.url,
            events=body.events,
            enabled=body.enabled,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc.errors()[0].get("msg"))) from exc

    webhooks = _api._app_db.get_webhooks()
    webhooks.append(cfg.model_dump())
    _api._app_db.set_webhooks(webhooks)
    return _webhook_public(cfg.model_dump())


@router.put("/api/admin/webhooks/{webhook_id}")
def update_webhook(webhook_id: str, body: WebhookUpdate, request: Request) -> dict:
    _require_admin(request)
    from pydantic import ValidationError

    from mira.outbound_webhooks import WebhookConfig

    webhooks = _api._app_db.get_webhooks()
    existing = next((w for w in webhooks if w.get("id") == webhook_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail="Webhook not found")

    merged = dict(existing)
    if body.name is not None:
        merged["name"] = body.name
    if body.url:  # blank → keep stored URL
        merged["url"] = body.url
    if body.events is not None:
        merged["events"] = body.events
    if body.enabled is not None:
        merged["enabled"] = body.enabled

    try:
        cfg = WebhookConfig(**merged)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc.errors()[0].get("msg"))) from exc

    webhooks = [cfg.model_dump() if w.get("id") == webhook_id else w for w in webhooks]
    _api._app_db.set_webhooks(webhooks)
    return _webhook_public(cfg.model_dump())


@router.delete("/api/admin/webhooks/{webhook_id}")
def delete_webhook(webhook_id: str, request: Request) -> dict:
    _require_admin(request)
    webhooks = _api._app_db.get_webhooks()
    remaining = [w for w in webhooks if w.get("id") != webhook_id]
    if len(remaining) == len(webhooks):
        raise HTTPException(status_code=404, detail="Webhook not found")
    _api._app_db.set_webhooks(remaining)
    return {"ok": True}


@router.post("/api/admin/webhooks/{webhook_id}/test")
async def test_webhook(webhook_id: str, request: Request) -> dict:
    _require_admin(request)
    from mira.outbound_webhooks import REVIEW_COMPLETED, deliver_one, sample_data

    webhook = next((w for w in _api._app_db.get_webhooks() if w.get("id") == webhook_id), None)
    if webhook is None:
        raise HTTPException(status_code=404, detail="Webhook not found")

    ok, detail = await deliver_one(webhook, REVIEW_COMPLETED, sample_data(REVIEW_COMPLETED))
    return {"ok": ok, "detail": detail}


@router.get("/api/uninstalls/pending", response_model=list[PendingUninstallModel])
def list_pending_uninstalls() -> list[PendingUninstallModel]:
    return [
        PendingUninstallModel(installation_id=iid, owner=owner)
        for iid, owner in _api._app_db.list_pending_uninstalls()
    ]


@router.post("/api/uninstalls/{installation_id}/keep")
def keep_uninstall_data(installation_id: int) -> dict:
    """User chose to keep data after uninstall — just dismiss the popup."""
    _api._app_db.remove_pending_uninstall(installation_id)
    return {"ok": True}


@router.post("/api/uninstalls/{installation_id}/delete")
def delete_uninstall_data(installation_id: int) -> dict:
    """User chose to delete all data for this installation."""
    removed = _api._app_db.delete_repos_by_installation(installation_id)
    _api._app_db.remove_pending_uninstall(installation_id)
    return {"ok": True, "removed": removed}


@router.get("/api/setup/status")
async def get_setup_status() -> dict:
    """Check if initial setup has been completed. Auto-syncs repos from GitHub if none registered."""
    repo_count = len(_api._app_db.list_repos())

    # If no repos registered, try to sync from GitHub App
    if repo_count == 0:
        try:
            app_id = os.environ.get("MIRA_GITHUB_APP_ID", "")
            private_key = os.environ.get("MIRA_GITHUB_PRIVATE_KEY", "")
            if app_id and private_key:
                import asyncio as _asyncio

                from mira.platforms.github.auth import GitHubAppAuth
                from mira.platforms.github.webhook import _count_files_for_repos

                auth = GitHubAppAuth(app_id=app_id, private_key=private_key)
                installations = await auth.list_installations()
                for inst in installations:
                    inst_id = int(str(inst.get("id", "0")))
                    if not inst_id:
                        continue
                    repos_list = await auth.list_installation_repos(inst_id)
                    for r in repos_list:
                        full_name = str(r.get("full_name", ""))
                        if "/" in full_name:
                            owner, repo = full_name.split("/", 1)
                            _api._app_db.register_repo(owner, repo, inst_id)
                            _api._app_db.set_repo_visibility(
                                owner, repo, bool(r.get("private", False))
                            )
                            repo_count += 1
                    # Count files in background
                    _asyncio.create_task(_count_files_for_repos(auth, inst_id, repos_list))
                logger.info("Synced %d repos from GitHub App", repo_count)
        except Exception as exc:
            logger.warning("Failed to sync repos from GitHub: %s", exc)

    return {"setup_complete": _api._app_db.setup_complete, "repo_count": repo_count}


@router.post("/api/setup/complete")
async def complete_setup(body: SetupRequest) -> dict:
    """Save setup choices and start indexing selected repos."""
    enabled_count = 0
    for r in body.repos:
        owner, repo = r["owner"], r["repo"]
        platform = r.get("platform", "github")
        enabled = r.get("enabled", True)
        mode = body.index_mode if enabled else "none"
        _api._app_db.set_repo_index_mode(owner, repo, mode, platform=platform)
        if enabled:
            _api._app_db.set_repo_status(owner, repo, "indexing", platform=platform)
            enabled_count += 1

    _api._app_db.mark_setup_complete()

    # Only fire the background indexer if this request actually enabled repos.
    # Otherwise (Skip for now / all-disabled), don't start anything — the
    # indexer reads ALL repos from the DB and would race with sibling requests
    # that haven't yet set their repos to mode='none'.
    if enabled_count > 0:
        import asyncio

        asyncio.create_task(_run_initial_indexing(body.index_mode))

    return {"status": "indexing" if enabled_count else "skipped", "repos": enabled_count}
