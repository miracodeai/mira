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
    ("/api/repos/{owner}/{repo}/index", "POST", {"owner": "o", "repo": "r"}),
    ("/api/repos/{owner}/{repo}/index", "DELETE", {"owner": "o", "repo": "r"}),
]


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
