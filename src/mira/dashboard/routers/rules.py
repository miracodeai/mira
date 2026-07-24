"""Dashboard rules routes"""

from __future__ import annotations

import os

from fastapi import HTTPException, Request

from mira.dashboard import api as _api
from mira.dashboard.api import (
    LearnedRuleActiveInput,
    LearnedRuleInput,
    LearnedRuleModel,
    OrgLearnedRuleModel,
    ReviewContextCreate,
    ReviewContextModel,
    RuleCreate,
    RuleModel,
    _open_store,
    _require_admin,
    router,
)


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
                id=r.id,
                rule_text=r.rule_text,
                source_signal=r.source_signal,
                category=r.category,
                path_pattern=r.path_pattern,
                sample_count=r.sample_count,
                active=r.active,
                status=r.status,
                created_by=r.created_by,
                updated_at=r.updated_at,
            )
            for r in rules
        ]


@router.get("/api/learned-rules", response_model=list[OrgLearnedRuleModel])
def list_org_learned_rules(limit: int = 500, status: str = "") -> list[OrgLearnedRuleModel]:
    """Learned rules across every repo in the org.

    `status` filters by approval state ('pending'|'approved'|'rejected');
    empty returns all so admins can manage the full set.
    """
    db_url = os.environ.get("DATABASE_URL", "")
    capped = max(1, min(limit, 2000))
    status_filter = status or None
    if db_url.startswith("postgresql://") or db_url.startswith("postgres://"):
        from mira.index.pg_store import list_learned_rules_org_wide

        rows = list_learned_rules_org_wide(db_url, limit=capped, status=status_filter)
    else:
        from mira.index.store import list_learned_rules_org_wide_sqlite

        rows = list_learned_rules_org_wide_sqlite(limit=capped, status=status_filter)
    return [
        OrgLearnedRuleModel(
            id=r.get("id", 0),
            owner=r["owner"],
            repo=r["repo"],
            rule_text=r["rule_text"],
            source_signal=r["source_signal"],
            category=r["category"],
            path_pattern=r["path_pattern"],
            sample_count=r["sample_count"],
            active=r.get("active", True),
            status=r.get("status", "approved"),
            created_by=r.get("created_by", ""),
            updated_at=r["updated_at"] or 0.0,
        )
        for r in rows
    ]


# ── Learnings approval queue + CRUD (admin only) ───────────────────────────
# Auto-synthesized learnings land 'pending' and must be approved by an admin
# before they influence reviews. Admins can also author/edit/delete rules.


@router.get(
    "/api/learned-rules/{owner}/{repo}/{rule_id}",
    response_model=OrgLearnedRuleModel,
)
def get_learned_rule_detail(
    owner: str, repo: str, rule_id: int, request: Request
) -> OrgLearnedRuleModel:
    """Single learned rule — backs the edit page. Readable by any authenticated
    user (so a creator can load their own pending rule to edit)."""
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    is_admin = bool(getattr(user, "is_admin", False))
    username = getattr(user, "username", "") if user else ""
    with _open_store(owner, repo) as store:
        r = store.get_learned_rule(rule_id)
    if not r:
        raise HTTPException(status_code=404, detail="Learning not found")
    if r.status != "approved" and not is_admin and r.created_by != username:
        raise HTTPException(status_code=403, detail="Not allowed to view this learning")
    return OrgLearnedRuleModel(
        id=r.id,
        owner=owner,
        repo=repo,
        rule_text=r.rule_text,
        source_signal=r.source_signal,
        category=r.category,
        path_pattern=r.path_pattern,
        sample_count=r.sample_count,
        active=r.active,
        status=r.status,
        created_by=r.created_by,
        updated_at=r.updated_at,
    )


@router.post("/api/learned-rules/{owner}/{repo}/{rule_id}/approve")
def approve_learned_rule(owner: str, repo: str, rule_id: int, request: Request) -> dict:
    _require_admin(request)
    with _open_store(owner, repo) as store:
        store.set_learned_rule_status(rule_id, "approved")
    return {"ok": True}


@router.post("/api/learned-rules/{owner}/{repo}/{rule_id}/reject")
def reject_learned_rule(owner: str, repo: str, rule_id: int, request: Request) -> dict:
    _require_admin(request)
    with _open_store(owner, repo) as store:
        store.set_learned_rule_status(rule_id, "rejected")
    return {"ok": True}


@router.patch("/api/learned-rules/{owner}/{repo}/{rule_id}/active")
def set_learned_rule_active(
    owner: str, repo: str, rule_id: int, body: LearnedRuleActiveInput, request: Request
) -> dict:
    _require_admin(request)
    with _open_store(owner, repo) as store:
        store.set_learned_rule_active(rule_id, body.active)
    return {"ok": True}


@router.post("/api/learned-rules/{owner}/{repo}", response_model=LearnedRuleModel)
def create_learned_rule(
    owner: str, repo: str, body: LearnedRuleInput, request: Request
) -> LearnedRuleModel:
    # Anyone authenticated may author a learning; admins' land approved, while
    # everyone else's go to the pending queue for an admin to approve.
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    is_admin = bool(getattr(user, "is_admin", False))
    with _open_store(owner, repo) as store:
        r = store.create_learned_rule(
            rule_text=body.rule_text,
            category=body.category,
            path_pattern=body.path_pattern,
            status="approved" if is_admin else "pending",
            created_by=getattr(user, "username", "") if user else "",
        )
    return LearnedRuleModel(
        id=r.id,
        rule_text=r.rule_text,
        source_signal=r.source_signal,
        category=r.category,
        path_pattern=r.path_pattern,
        sample_count=r.sample_count,
        active=r.active,
        status=r.status,
        created_by=r.created_by,
        updated_at=r.updated_at,
    )


@router.put("/api/learned-rules/{owner}/{repo}/{rule_id}")
def update_learned_rule(
    owner: str, repo: str, rule_id: int, body: LearnedRuleInput, request: Request
) -> dict:
    # Admins may edit any rule; a non-admin may edit only their own rule while
    # it's still pending approval.
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    is_admin = bool(getattr(user, "is_admin", False))
    username = getattr(user, "username", "") if user else ""
    with _open_store(owner, repo) as store:
        existing = store.get_learned_rule(rule_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Learning not found")
        if not (is_admin or (existing.created_by == username and existing.status == "pending")):
            raise HTTPException(status_code=403, detail="Not allowed to edit this learning")
        store.update_learned_rule(rule_id, body.rule_text, body.category, body.path_pattern)
    return {"ok": True}


@router.delete("/api/learned-rules/{owner}/{repo}/{rule_id}")
def delete_learned_rule(owner: str, repo: str, rule_id: int, request: Request) -> dict:
    _require_admin(request)
    with _open_store(owner, repo) as store:
        store.delete_learned_rule(rule_id)
    return {"ok": True}


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


@router.get("/api/rules/global", response_model=list[RuleModel])
def list_global_rules() -> list[RuleModel]:
    rules = _api._app_db.list_global_rules()
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
    r = _api._app_db.upsert_global_rule(title=body.title, content=body.content)
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
    existing = _api._app_db.get_global_rule(rule_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Rule not found")
    r = _api._app_db.upsert_global_rule(title=body.title, content=body.content, rule_id=rule_id)
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
    _api._app_db.delete_global_rule(rule_id)
    return {"ok": True}


@router.patch("/api/rules/global/{rule_id}/toggle", response_model=RuleModel)
def toggle_global_rule(rule_id: int) -> RuleModel:
    r = _api._app_db.toggle_global_rule(rule_id)
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
