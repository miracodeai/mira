"""Every state-changing admin endpoint must reject non-admin users (CWE-862)."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mira.dashboard.api import router

# (path, method, extra kwargs). Bodies are None: _require_admin fires first.
PROTECTED = [
    ("/api/gitlab/sync", "POST", {}),
    ("/api/gitlab/repos", "POST", {"body": None}),
    ("/api/forgejo/sync", "POST", {}),
    ("/api/forgejo/repos", "POST", {"body": None}),
    ("/api/settings/models", "PUT", {"body": None}),
    ("/api/uninstalls/{installation_id}/keep", "POST", {"installation_id": 1}),
    ("/api/uninstalls/{installation_id}/delete", "POST", {"installation_id": 1}),
    ("/api/setup/complete", "POST", {"body": None}),
    ("/api/repos/sync", "POST", {}),
    ("/api/repos/{owner:path}/{repo}/index", "POST", {"owner": "o", "repo": "r"}),
    ("/api/repos/{owner:path}/{repo}/index", "DELETE", {"owner": "o", "repo": "r"}),
    # Review context + rules go straight into the review prompt: anyone may
    # read them, only admins write.
    ("/api/repos/{owner:path}/{repo}/context", "POST", {"owner": "o", "repo": "r", "body": None}),
    (
        "/api/repos/{owner:path}/{repo}/context/{context_id}",
        "PUT",
        {"owner": "o", "repo": "r", "context_id": 1, "body": None},
    ),
    (
        "/api/repos/{owner:path}/{repo}/context/{context_id}",
        "DELETE",
        {"owner": "o", "repo": "r", "context_id": 1},
    ),
    ("/api/repos/{owner:path}/{repo}/rules", "POST", {"owner": "o", "repo": "r", "body": None}),
    (
        "/api/repos/{owner:path}/{repo}/rules/{rule_id}",
        "PUT",
        {"owner": "o", "repo": "r", "rule_id": 1, "body": None},
    ),
    (
        "/api/repos/{owner:path}/{repo}/rules/{rule_id}",
        "DELETE",
        {"owner": "o", "repo": "r", "rule_id": 1},
    ),
    ("/api/rules/global", "POST", {"body": None}),
    ("/api/rules/global/{rule_id}", "PUT", {"rule_id": 1, "body": None}),
    ("/api/rules/global/{rule_id}", "DELETE", {"rule_id": 1}),
    ("/api/rules/global/{rule_id}/toggle", "PATCH", {"rule_id": 1}),
    # Cross-repo overrides + custom edges rewrite the graph every blast-radius reads.
    ("/api/relationships/overrides", "POST", {"body": None}),
    ("/api/relationships/overrides", "DELETE", {"source_repo": "a/b", "target_repo": "c/d"}),
    ("/api/relationships/custom", "POST", {"body": None}),
    ("/api/relationships/custom/{edge_id}", "DELETE", {"edge_id": 1}),
]

# Mutations any authenticated user may perform. Each does its own ownership
# check (a non-admin may only author a pending learning or edit their own).
NON_ADMIN_MUTATIONS = {
    ("POST", "/api/learned-rules/{owner:path}/{repo}"),
    ("PUT", "/api/learned-rules/{owner:path}/{repo}/{rule_id}"),
}


def _endpoint(path: str, method: str):
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"route {method} {path} not found")


def _non_admin_request() -> SimpleNamespace:
    user = SimpleNamespace(id=2, username="bob", is_admin=False)
    return SimpleNamespace(state=SimpleNamespace(user=user))


@pytest.mark.parametrize("path,method,kwargs", PROTECTED)
async def test_rejects_non_admin(path: str, method: str, kwargs: dict) -> None:
    endpoint = _endpoint(path, method)
    with pytest.raises(HTTPException) as exc:
        result = endpoint(request=_non_admin_request(), **kwargs)
        if inspect.iscoroutine(result):
            await result
    assert exc.value.status_code == 403


@pytest.mark.parametrize("path,method,kwargs", PROTECTED)
async def test_rejects_missing_user(path: str, method: str, kwargs: dict) -> None:
    endpoint = _endpoint(path, method)
    request = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(HTTPException) as exc:
        result = endpoint(request=request, **kwargs)
        if inspect.iscoroutine(result):
            await result
    assert exc.value.status_code == 403


def test_every_mutating_route_checks_the_admin_role() -> None:
    """The admin boundary is enforced per endpoint (the shared router has no
    router-level dependency), so a forgotten check is otherwise silent. Every
    POST/PUT/PATCH/DELETE route must call _require_admin or test is_admin
    itself, unless it is deliberately listed in NON_ADMIN_MUTATIONS."""
    unguarded = []
    for route in router.routes:
        mutating = (getattr(route, "methods", None) or set()) & {"POST", "PUT", "PATCH", "DELETE"}
        for method in sorted(mutating):
            if (method, route.path) in NON_ADMIN_MUTATIONS:
                continue
            source = inspect.getsource(route.endpoint)
            if "_require_admin(" not in source and ".is_admin" not in source:
                unguarded.append(f"{method} {route.path}")
    assert unguarded == [], f"mutating routes without an admin check: {unguarded}"
