"""Dashboard endpoints support GitLab repositories in nested groups."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Match

from mira.dashboard import api
from mira.dashboard.db import AppDatabase
from mira.dashboard.routers import repos, rules


@pytest.fixture
def nested_repo_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[AppDatabase, None, None]:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    db = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr(api, "_app_db", db)
    db.register_repo("clients/mymedhub", "mymedhub-api-nestjs", platform="gitlab")
    yield db
    db.close()


@pytest.mark.parametrize(
    ("method", "path", "endpoint_name"),
    [
        (
            "GET",
            "/api/repos/clients/mymedhub/mymedhub-api-nestjs",
            "get_repo_detail",
        ),
        (
            "GET",
            "/api/repos/clients/mymedhub/mymedhub-api-nestjs/files",
            "list_files",
        ),
        (
            "GET",
            "/api/repos/clients/mymedhub/mymedhub-api-nestjs/dependencies",
            "get_dependencies",
        ),
        (
            "GET",
            "/api/repos/clients/mymedhub/mymedhub-api-nestjs/blast-radius",
            "get_blast_radius",
        ),
        (
            "GET",
            "/api/repos/clients/mymedhub/mymedhub-api-nestjs/packages",
            "get_packages",
        ),
        (
            "POST",
            "/api/repos/clients/mymedhub/mymedhub-api-nestjs/index",
            "trigger_index",
        ),
        (
            "DELETE",
            "/api/repos/clients/mymedhub/mymedhub-api-nestjs/index",
            "cancel_index",
        ),
        (
            "GET",
            "/api/repos/clients/mymedhub/mymedhub-api-nestjs/reviews",
            "list_reviews",
        ),
        (
            "GET",
            "/api/repos/clients/mymedhub/mymedhub-api-nestjs/vulnerabilities",
            "get_repo_vulnerabilities",
        ),
        (
            "POST",
            "/api/repos/clients/mymedhub/mymedhub-api-nestjs/rules",
            "create_repo_rule",
        ),
        (
            "PUT",
            "/api/repos/clients/mymedhub/mymedhub-api-nestjs/rules/7",
            "update_repo_rule",
        ),
        (
            "GET",
            "/api/repos/clients/mymedhub/mymedhub-api-nestjs/learned-rules",
            "list_repo_learned_rules",
        ),
        (
            "POST",
            "/api/learned-rules/clients/mymedhub/mymedhub-api-nestjs",
            "create_learned_rule",
        ),
        (
            "GET",
            "/api/learned-rules/clients/mymedhub/mymedhub-api-nestjs/7",
            "get_learned_rule_detail",
        ),
        (
            "POST",
            "/api/learned-rules/clients/mymedhub/mymedhub-api-nestjs/7/approve",
            "approve_learned_rule",
        ),
    ],
)
def test_nested_owner_routes_match(method: str, path: str, endpoint_name: str):
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "root_path": "",
        "headers": [],
    }

    for route in api.router.routes:
        if not isinstance(route, APIRoute):
            continue
        match, child_scope = route.matches(scope)
        if match is Match.FULL:
            assert route.name == endpoint_name
            assert child_scope["path_params"]["owner"] == "clients/mymedhub"
            assert child_scope["path_params"]["repo"] == "mymedhub-api-nestjs"
            break
    else:
        pytest.fail(f"No route matched {method} {path}")


def test_repo_rule_crud_with_nested_owner(nested_repo_db: AppDatabase):
    owner = "clients/mymedhub"
    repo = "mymedhub-api-nestjs"

    created = rules.create_repo_rule(
        owner,
        repo,
        api.RuleCreate(title="Keep modules focused", content="One responsibility per module."),
    )

    assert [rule.id for rule in rules.list_repo_rules(owner, repo)] == [created.id]

    updated = rules.update_repo_rule(
        owner,
        repo,
        created.id,
        api.RuleCreate(title="Keep modules focused", content="Updated guidance."),
    )
    assert updated.content == "Updated guidance."

    assert rules.delete_repo_rule(owner, repo, created.id) == {"ok": True}
    assert rules.list_repo_rules(owner, repo) == []


def test_repo_detail_opens_nested_owner(nested_repo_db: AppDatabase):
    nested_repo_db.set_repo_status(
        "clients/mymedhub",
        "mymedhub-api-nestjs",
        "ready",
        bump_last_indexed=True,
        platform="gitlab",
    )
    detail = repos.get_repo_detail("clients/mymedhub", "mymedhub-api-nestjs")

    assert detail.owner == "clients/mymedhub"
    assert detail.repo == "mymedhub-api-nestjs"
    assert detail.file_count == 0
    assert detail.last_indexed is not None
