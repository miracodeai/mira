"""Dashboard repo routes must stay platform-aware: the GitHub sync prune
leaves GitLab rows alone, and setup writes target the right platform row."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mira.dashboard import api
from mira.dashboard.api import SetupRequest
from mira.dashboard.db import AppDatabase
from mira.dashboard.routers.admin import complete_setup
from mira.dashboard.routers.repos import sync_repos


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.setenv("MIRA_GITHUB_APP_ID", "1")
    monkeypatch.setenv("MIRA_GITHUB_PRIVATE_KEY", "key")
    db = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr(api, "_app_db", db)
    return db


@pytest.mark.asyncio
async def test_sync_prunes_stale_github_but_leaves_gitlab(db: AppDatabase) -> None:
    db.register_repo("acme", "web", installation_id=1)
    db.register_repo("acme", "old", installation_id=1)
    db.register_repo("grp", "proj", platform="gitlab")

    auth = AsyncMock()
    auth.list_installations.return_value = [{"id": 1}]
    auth.list_installation_repos.return_value = [{"full_name": "acme/web", "private": False}]

    with (
        patch("mira.platforms.github.auth.GitHubAppAuth", return_value=auth),
        patch("mira.platforms.github.webhook._count_files_for_repos", new=AsyncMock()),
    ):
        result = await sync_repos()

    assert result["removed"] == 1
    remaining = {(r.platform, r.owner, r.repo) for r in db.list_repos()}
    assert remaining == {("github", "acme", "web"), ("gitlab", "grp", "proj")}


@pytest.mark.asyncio
async def test_complete_setup_writes_to_platform_rows(db: AppDatabase) -> None:
    db.register_repo("acme", "web")
    db.register_repo("grp", "proj", platform="gitlab")

    with patch("mira.dashboard.routers.admin._run_initial_indexing", new=AsyncMock()):
        await complete_setup(
            SetupRequest(
                repos=[
                    {"owner": "acme", "repo": "web", "platform": "github", "enabled": True},
                    {"owner": "grp", "repo": "proj", "platform": "gitlab", "enabled": False},
                ],
                index_mode="full",
            )
        )

    gh = db.get_repo("acme", "web")
    gl = db.get_repo("grp", "proj", platform="gitlab")
    assert gh is not None and gh.status == "indexing" and gh.index_mode == "full"
    # The disabled GitLab repo must be opted out on its own row, not a
    # phantom github one — otherwise the initial indexer picks it up.
    assert gl is not None and gl.index_mode == "none" and gl.status == "pending"
