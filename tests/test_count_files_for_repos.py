"""Tests for _count_files_for_repos and the webhook paths that call it.

Regression for https://github.com/miracodeai/mira/issues/122 — file-count
must use the repo's actual default branch, not a hardcoded ``"main"``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.github_app.index_handlers import _count_files_for_repos


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

        with (
            patch(
                "mira.github_app.index_handlers._fetch_default_branch",
                new=AsyncMock(return_value="master"),
            ) as mock_branch,
            patch(
                "mira.github_app.index_handlers._fetch_repo_tree",
                new=AsyncMock(return_value=["src/foo.py", "README.md"]),
            ) as mock_tree,
            patch("mira.github_app.index_handlers._get_app_db", return_value=app_db),
            patch("mira.github_app.index_handlers.load_config"),
        ):
            await _count_files_for_repos(app_auth, installation_id=42, repos=repos)

        mock_branch.assert_awaited_once_with("acme", "widget", "fake-token")
        # Tree fetch must use the resolved branch, not the default "main".
        mock_tree.assert_awaited_once()
        args, kwargs = mock_tree.call_args
        # Args can be positional or keyword; both shapes should pass "master".
        branch = kwargs.get("branch") if "branch" in kwargs else args[3]
        assert branch == "master"
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

        async def fake_branch(owner, repo, token):
            return "main" if repo == "on-main" else "master"

        async def fake_tree(owner, repo, token, branch="main"):
            return [f"{repo}/file.py"]

        with (
            patch(
                "mira.github_app.index_handlers._fetch_default_branch",
                new=AsyncMock(side_effect=fake_branch),
            ),
            patch(
                "mira.github_app.index_handlers._fetch_repo_tree",
                new=AsyncMock(side_effect=fake_tree),
            ) as mock_tree,
            patch("mira.github_app.index_handlers._get_app_db", return_value=app_db),
            patch("mira.github_app.index_handlers.load_config"),
        ):
            await _count_files_for_repos(app_auth, installation_id=42, repos=repos)

        # Branches passed to the tree call, in order, should match the per-repo
        # default branch each repo actually reports.
        assert mock_tree.await_count == 2
        branches_used = [
            (call.kwargs.get("branch") if "branch" in call.kwargs else call.args[3])
            for call in mock_tree.await_args_list
        ]
        assert branches_used == ["main", "master"]
