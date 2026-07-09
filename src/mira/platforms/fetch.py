"""Repo content fetching for the indexer, per platform.

Indexing operates on a whole repo (tree, file contents, tarball) with no PR in
hand, so it doesn't go through the PR-shaped ``BaseProvider``. ``RepoFetcher`` is
the thin seam the indexer fetches through; ``make_fetcher(platform, token)``
returns the right implementation.
"""

from __future__ import annotations

import asyncio
import io
import logging
import tarfile
from typing import Protocol
from urllib.parse import quote

import httpx

from mira.platforms import profiles

logger = logging.getLogger(__name__)


class EmptyRepoError(Exception):
    """Raised when a repo has no commits/files to index (not a failure)."""

    def __init__(self, owner: str, repo: str) -> None:
        super().__init__(f"Repository {owner}/{repo} is empty — push code, then re-index.")


class RepoFetcher(Protocol):
    async def default_branch(self, owner: str, repo: str) -> str: ...

    async def repo_tree(self, owner: str, repo: str, branch: str) -> list[str]: ...

    async def file_content(
        self,
        owner: str,
        repo: str,
        path: str,
        ref: str,
        semaphore: asyncio.Semaphore | None = None,
    ) -> str | None: ...

    async def repo_tarball(
        self, owner: str, repo: str, ref: str, max_file_size: int = 1_048_576
    ) -> dict[str, str] | None: ...


def _strip_tarball(blob: bytes, max_file_size: int, label: str) -> dict[str, str] | None:
    """Decode a gzipped tarball into ``{repo-relative path: text}``.

    Both GitHub and GitLab wrap files under a single top-level dir
    (``owner-repo-{sha}/`` / ``repo-{ref}-{sha}/``); we strip whatever it is.
    """
    out: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            for member in tf:
                if not member.isfile():
                    continue
                if max_file_size and member.size > max_file_size:
                    continue
                parts = member.name.split("/", 1)
                if len(parts) != 2 or not parts[1]:
                    continue
                f = tf.extractfile(member)
                if f is None:
                    continue
                try:
                    out[parts[1]] = f.read().decode("utf-8")
                except UnicodeDecodeError:
                    continue
    except (tarfile.TarError, OSError) as exc:
        logger.warning("Tarball extract failed for %s: %s", label, exc)
        return None
    logger.info("Tarball: fetched %d files for %s in one request", len(out), label)
    return out


class GitHubRepoFetcher:
    """Fetches repo content via the GitHub REST API."""

    def __init__(self, token: str, api_url: str = "https://api.github.com") -> None:
        self._token = token
        self._api = api_url.rstrip("/")

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept": accept}

    async def default_branch(self, owner: str, repo: str) -> str:
        url = f"{self._api}/repos/{owner}/{repo}"
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers=self._headers(), timeout=15)
                resp.raise_for_status()
                return str(resp.json().get("default_branch", "main"))
        except Exception as exc:
            logger.warning("Failed to fetch default branch for %s/%s: %s", owner, repo, exc)
            return "main"

    async def repo_tree(self, owner: str, repo: str, branch: str) -> list[str]:
        url = f"{self._api}/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=self._headers(), timeout=30)
            # GitHub returns 409 (and sometimes 404) for an empty repo.
            if resp.status_code in (404, 409):
                raise EmptyRepoError(owner, repo)
            resp.raise_for_status()
            data = resp.json()
        return [item["path"] for item in data.get("tree", []) if item.get("type") == "blob"]

    async def file_content(
        self, owner: str, repo: str, path: str, ref: str, semaphore=None
    ) -> str | None:
        url = f"{self._api}/repos/{owner}/{repo}/contents/{path}?ref={ref}"
        headers = self._headers("application/vnd.github.raw+json")

        async def _fetch() -> str | None:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(url, headers=headers, timeout=30)
                    if resp.status_code == 404:
                        return None
                    resp.raise_for_status()
                    return resp.text
            except Exception as exc:
                logger.warning("Failed to fetch %s: %s", path, exc)
                return None

        if semaphore:
            async with semaphore:
                return await _fetch()
        return await _fetch()

    async def repo_tarball(
        self, owner: str, repo: str, ref: str, max_file_size: int = 1_048_576
    ) -> dict[str, str] | None:
        url = f"{self._api}/repos/{owner}/{repo}/tarball/{ref}"
        headers = {**self._headers(), "User-Agent": "mira-indexer"}
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code != 200:
                    logger.warning(
                        "Tarball fetch failed for %s/%s: %d", owner, repo, resp.status_code
                    )
                    return None
                blob = resp.content
        except Exception as exc:
            logger.warning("Tarball fetch failed for %s/%s: %s", owner, repo, exc)
            return None
        return _strip_tarball(blob, max_file_size, f"{owner}/{repo}")


class GitLabRepoFetcher:
    """Fetches repo content via the GitLab REST v4 API.

    Project id is the URL-encoded ``owner/repo`` path (``owner`` may itself
    contain slashes for nested groups). The tree endpoint is paginated, unlike
    GitHub's single recursive call.
    """

    def __init__(self, token: str, base_url: str = "https://gitlab.com/api/v4") -> None:
        self._token = token
        self._api = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self._token}

    @staticmethod
    def _pid(owner: str, repo: str) -> str:
        return quote(f"{owner}/{repo}", safe="")

    def _project(self, owner: str, repo: str) -> str:
        return f"{self._api}/projects/{self._pid(owner, repo)}"

    async def default_branch(self, owner: str, repo: str) -> str:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    self._project(owner, repo), headers=self._headers(), timeout=15
                )
                resp.raise_for_status()
                return str(resp.json().get("default_branch", "main"))
        except Exception as exc:
            logger.warning("Failed to fetch default branch for %s/%s: %s", owner, repo, exc)
            return "main"

    async def repo_tree(self, owner: str, repo: str, branch: str) -> list[str]:
        # Keyset pagination via the Link header; recursive lists the whole tree.
        url: str | None = (
            f"{self._project(owner, repo)}/repository/tree"
            f"?recursive=true&per_page=100&pagination=keyset&ref={branch}"
        )
        paths: list[str] = []
        async with httpx.AsyncClient() as client:
            while url:
                resp = await client.get(url, headers=self._headers(), timeout=30)
                # An empty repo (or a ref that doesn't exist yet) 404s.
                if resp.status_code == 404:
                    raise EmptyRepoError(owner, repo)
                resp.raise_for_status()
                for item in resp.json():
                    if item.get("type") == "blob":
                        paths.append(item["path"])
                url = _next_link(resp.headers.get("link", ""))
        return paths

    async def file_content(
        self, owner: str, repo: str, path: str, ref: str, semaphore=None
    ) -> str | None:
        url = f"{self._project(owner, repo)}/repository/files/{quote(path, safe='')}/raw?ref={ref}"

        async def _fetch() -> str | None:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(url, headers=self._headers(), timeout=30)
                    if resp.status_code == 404:
                        return None
                    resp.raise_for_status()
                    return resp.text
            except Exception as exc:
                logger.warning("Failed to fetch %s: %s", path, exc)
                return None

        if semaphore:
            async with semaphore:
                return await _fetch()
        return await _fetch()

    async def repo_tarball(
        self, owner: str, repo: str, ref: str, max_file_size: int = 1_048_576
    ) -> dict[str, str] | None:
        url = f"{self._project(owner, repo)}/repository/archive.tar.gz?sha={ref}"
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
                resp = await client.get(url, headers=self._headers())
                if resp.status_code != 200:
                    logger.warning(
                        "Tarball fetch failed for %s/%s: %d", owner, repo, resp.status_code
                    )
                    return None
                blob = resp.content
        except Exception as exc:
            logger.warning("Tarball fetch failed for %s/%s: %s", owner, repo, exc)
            return None
        return _strip_tarball(blob, max_file_size, f"{owner}/{repo}")


def _next_link(link_header: str) -> str | None:
    """Extract the rel="next" URL from a Link header (GitLab keyset pagination)."""
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip().strip("<>")
    return None


def make_fetcher(platform: str, token: str) -> RepoFetcher:
    """Build the RepoFetcher for a platform, using its profile's api_url."""
    profile = profiles.resolve(platform)
    api_url = profile["api_url"]
    if platform == "gitlab":
        return GitLabRepoFetcher(token, api_url or "https://gitlab.com/api/v4")
    return GitHubRepoFetcher(token, api_url or "https://api.github.com")
