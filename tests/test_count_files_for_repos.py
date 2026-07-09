"""Tests for _count_files_for_repos and the webhook paths that call it.

Regression for https://github.com/miracodeai/mira/issues/122 — file-count
must use the repo's actual default branch, not a hardcoded ``"main"``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.platforms.github.webhook import _count_files_for_repos


@pytest.mark.asyncio
class TestCountFilesForReposResolvesDefaultBranch:
    async def test_uses_resolved_branch_not_main(self):
        """When the repo's default branch is ``master``, the tree fetch must
        be issued against ``master`` — otherwise GitHub returns 404 and the
        file count silently fails.
        """
        app_auth = MagicMock()
        app_auth.get_installation_token = AsyncMock(return_value="fake-token")

        app_db = MagicMock()
        app_db.set_repo_file_count = MagicMock()

        repos = [{"full_name": "acme/widget", "private": False}]

        fetcher = MagicMock()
        fetcher.default_branch = AsyncMock(return_value="master")
        fetcher.repo_tree = AsyncMock(return_value=["src/foo.py", "README.md"])

        with (
            patch("mira.platforms.github.webhook.make_fetcher", return_value=fetcher),
            patch("mira.platforms.github.webhook._get_app_db", return_value=app_db),
            patch("mira.platforms.github.webhook.load_config"),
        ):
            await _count_files_for_repos(app_auth, installation_id=42, repos=repos)

        fetcher.default_branch.assert_awaited_once_with("acme", "widget")
        # Tree fetch must use the resolved branch, not the default "main".
        fetcher.repo_tree.assert_awaited_once_with("acme", "widget", "master")
        app_db.set_repo_file_count.assert_called_once_with("acme", "widget", 1)

    async def test_resolves_branch_per_repo(self):
        """Each repo should have its own default branch resolved."""
        app_auth = MagicMock()
        app_auth.get_installation_token = AsyncMock(return_value="fake-token")

        app_db = MagicMock()
        app_db.set_repo_file_count = MagicMock()

        repos = [
            {"full_name": "acme/on-main", "private": False},
            {"full_name": "acme/on-master", "private": False},
        ]

        async def fake_branch(owner, repo):
            return "main" if repo == "on-main" else "master"

        async def fake_tree(owner, repo, branch):
            return [f"{repo}/file.py"]

        fetcher = MagicMock()
        fetcher.default_branch = AsyncMock(side_effect=fake_branch)
        fetcher.repo_tree = AsyncMock(side_effect=fake_tree)

        with (
            patch("mira.platforms.github.webhook.make_fetcher", return_value=fetcher),
            patch("mira.platforms.github.webhook._get_app_db", return_value=app_db),
            patch("mira.platforms.github.webhook.load_config"),
        ):
            await _count_files_for_repos(app_auth, installation_id=42, repos=repos)

        # Branches passed to the tree call, in order, should match the per-repo
        # default branch each repo actually reports.
        assert fetcher.repo_tree.await_count == 2
        branches_used = [call.args[2] for call in fetcher.repo_tree.await_args_list]
        assert branches_used == ["main", "master"]
