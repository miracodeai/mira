"""Tests for the platform repo-fetch layer (mira.platforms.fetch)."""

from __future__ import annotations

import io
import tarfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.platforms.fetch import (
    EmptyRepoError,
    GitHubRepoFetcher,
    GitLabRepoFetcher,
    _strip_tarball,
    make_fetcher,
)


def _make_targz(prefix: str, files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=f"{prefix}/{path}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestStripTarball:
    def test_strips_top_level_dir(self):
        blob = _make_targz("acme-web-abc123", {"src/main.py": "x = 1", "README.md": "# hi"})
        out = _strip_tarball(blob, max_file_size=1_048_576, label="acme/web")
        assert out == {"src/main.py": "x = 1", "README.md": "# hi"}

    def test_gitlab_prefix_shape(self):
        # GitLab archives wrap as {repo}-{ref}-{sha}/ — still a single top dir.
        blob = _make_targz("web-main-deadbeef", {"a.py": "1"})
        assert _strip_tarball(blob, 1_048_576, "g/web") == {"a.py": "1"}

    def test_size_limit_skips_large(self):
        blob = _make_targz("r-sha", {"big.py": "y" * 50})
        assert _strip_tarball(blob, max_file_size=10, label="o/r") == {}


def _resp(json_data=None, *, text="", headers=None, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data
    r.text = text
    r.headers = headers or {}
    r.raise_for_status = MagicMock()
    return r


def _patch_client(get_side_effect):
    client = AsyncMock()
    client.get = AsyncMock(side_effect=get_side_effect)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return patch("mira.platforms.fetch.httpx.AsyncClient", return_value=client)


class TestGitLabTreePagination:
    @pytest.mark.asyncio
    async def test_follows_keyset_link(self):
        page1 = _resp(
            [{"type": "blob", "path": "a.py"}, {"type": "tree", "path": "sub"}],
            headers={"link": '<https://gitlab.com/api/v4/next>; rel="next"'},
        )
        page2 = _resp([{"type": "blob", "path": "sub/b.py"}], headers={})
        with _patch_client([page1, page2]):
            paths = await GitLabRepoFetcher("tok").repo_tree("grp/sub", "proj", "main")
        assert paths == ["a.py", "sub/b.py"]

    @pytest.mark.asyncio
    async def test_url_encodes_nested_project(self):
        captured = {}

        async def grab(url, **kw):
            captured["url"] = url
            return _resp([], headers={})

        with _patch_client(grab):
            await GitLabRepoFetcher("tok").repo_tree("group/sub", "web", "main")
        assert "group%2Fsub%2Fweb" in captured["url"]


class TestEmptyRepo:
    @pytest.mark.asyncio
    async def test_gitlab_tree_404_raises_empty(self):
        with (
            _patch_client(lambda url, **kw: _resp(status=404, text="404 Tree Not Found")),
            pytest.raises(EmptyRepoError),
        ):
            await GitLabRepoFetcher("t").repo_tree("g", "p", "main")

    @pytest.mark.asyncio
    async def test_github_tree_409_raises_empty(self):
        with (
            _patch_client(lambda url, **kw: _resp(status=409, text="empty")),
            pytest.raises(EmptyRepoError),
        ):
            await GitHubRepoFetcher("t").repo_tree("o", "r", "main")


class TestMakeFetcher:
    def test_github(self):
        assert isinstance(make_fetcher("github", "t"), GitHubRepoFetcher)

    def test_gitlab(self):
        f = make_fetcher("gitlab", "t")
        assert isinstance(f, GitLabRepoFetcher)
