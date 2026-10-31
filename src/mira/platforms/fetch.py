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
import re
import tarfile
from typing import Protocol
from urllib.parse import quote

import httpx

from mira.platforms import profiles

logger = logging.getLogger(__name__)

_MAX_TREE_PAGES = 1000
# A full commit hash; Bitbucket refs that match need no branch/tag lookup.
_COMMIT_HASH = re.compile(r"[0-9a-f]{40}")


class EmptyRepoError(Exception):
    """Raised when a repo has no commits/files to index (not a failure)."""

    def __init__(self, owner: str, repo: str) -> None:
        super().__init__(f"Repository {owner}/{repo} is empty — push code, then re-index.")


class IncompleteTreeError(RuntimeError):
    """Raised when a tree response cannot establish a complete file list."""


def _tree_json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError as exc:
        raise IncompleteTreeError("Invalid repository tree JSON") from exc


def _tree_paths(entries: object) -> tuple[list[str], set[str]]:
    if not isinstance(entries, list):
        raise IncompleteTreeError("Invalid repository tree entries")
    files: list[str] = []
    paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise IncompleteTreeError("Invalid repository tree entry")
        path = entry.get("path")
        kind = entry.get("type")
        if not isinstance(path, str) or not path or kind not in ("blob", "tree", "commit"):
            raise IncompleteTreeError("Invalid repository tree entry")
        if path in paths:
            raise IncompleteTreeError("Duplicate repository tree entry")
        paths.add(path)
        if kind == "blob":
            files.append(path)
    return files, paths


# Bitbucket lists submodules as ``commit_link``; like GitHub's ``commit`` they aren't files.
_BITBUCKET_TREE_KINDS = ("commit_file", "commit_directory", "commit_link")


def _bitbucket_tree_paths(entries: object) -> tuple[list[str], set[str]]:
    """Like ``_tree_paths`` for Bitbucket's ``src`` listing entry types."""
    if not isinstance(entries, list):
        raise IncompleteTreeError("Invalid repository tree entries")
    files: list[str] = []
    paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise IncompleteTreeError("Invalid repository tree entry")
        path = entry.get("path")
        kind = entry.get("type")
        if not isinstance(path, str) or not path or kind not in _BITBUCKET_TREE_KINDS:
            raise IncompleteTreeError("Invalid repository tree entry")
        if path in paths:
            raise IncompleteTreeError("Duplicate repository tree entry")
        paths.add(path)
        if kind == "commit_file":
            files.append(path)
    return files, paths


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
        self,
        owner: str,
        repo: str,
        ref: str,
        max_file_size: int = 1_048_576,
        indexable_paths: set[str] | None = None,
    ) -> dict[str, str] | None: ...


def _strip_tarball(
    blob: bytes,
    max_file_size: int,
    label: str,
    indexable_paths: set[str] | None = None,
) -> dict[str, str] | None:
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
                rel_path = parts[1]
                if indexable_paths is not None and rel_path not in indexable_paths:
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
        url = f"{self._api}/repos/{owner}/{repo}/git/trees/{quote(branch, safe='')}?recursive=1"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=self._headers(), timeout=30)
            # GitHub returns 409 (and sometimes 404) for an empty repo.
            if resp.status_code in (404, 409):
                raise EmptyRepoError(owner, repo)
            resp.raise_for_status()
            data = _tree_json(resp)
        if not isinstance(data, dict) or data.get("truncated") is not False:
            raise IncompleteTreeError("Repository tree is incomplete")
        paths, _ = _tree_paths(data.get("tree"))
        return paths

    async def file_content(
        self, owner: str, repo: str, path: str, ref: str, semaphore=None
    ) -> str | None:
        url = f"{self._api}/repos/{owner}/{repo}/contents/{quote(path, safe='/')}"
        headers = self._headers("application/vnd.github.raw+json")

        async def _fetch() -> str | None:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(url, params={"ref": ref}, headers=headers, timeout=30)
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
        self,
        owner: str,
        repo: str,
        ref: str,
        max_file_size: int = 1_048_576,
        indexable_paths: set[str] | None = None,
    ) -> dict[str, str] | None:
        url = f"{self._api}/repos/{owner}/{repo}/tarball/{quote(ref, safe='')}"
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
        return _strip_tarball(blob, max_file_size, f"{owner}/{repo}", indexable_paths)


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
        initial_url = str(
            httpx.URL(
                f"{self._project(owner, repo)}/repository/tree",
                params={
                    "recursive": "true",
                    "per_page": "100",
                    "pagination": "keyset",
                    "ref": branch,
                },
            )
        )
        url: str | None = initial_url
        paths: list[str] = []
        seen_urls: set[str] = set()
        seen_paths: set[str] = set()
        endpoint = httpx.URL(initial_url)
        async with httpx.AsyncClient() as client:
            while url:
                if url in seen_urls:
                    raise IncompleteTreeError("Repository tree pagination is cyclic")
                if len(seen_urls) >= _MAX_TREE_PAGES:
                    raise IncompleteTreeError("Repository tree page limit exceeded")
                seen_urls.add(url)
                resp = await client.get(url, headers=self._headers(), timeout=30)
                # An empty repo (or a ref that doesn't exist yet) 404s.
                if resp.status_code == 404:
                    raise EmptyRepoError(owner, repo)
                resp.raise_for_status()
                files, entries = _tree_paths(_tree_json(resp))
                if seen_paths & entries:
                    raise IncompleteTreeError("Duplicate repository tree page entries")
                seen_paths.update(entries)
                paths.extend(files)
                url = _next_link(resp.headers.get("link", ""))
                if url:
                    if not entries:
                        raise IncompleteTreeError("Repository tree pagination made no progress")
                    try:
                        target = httpx.URL(url)
                    except httpx.InvalidURL as exc:
                        raise IncompleteTreeError(
                            "Invalid repository tree pagination link"
                        ) from exc
                    if (target.scheme, target.host, target.port, target.path) != (
                        endpoint.scheme,
                        endpoint.host,
                        endpoint.port,
                        endpoint.path,
                    ) or target.userinfo:
                        raise IncompleteTreeError("Invalid repository tree pagination target")
                    if (
                        target.params.remove("page_token") != endpoint.params
                        or len(target.params.get_list("page_token")) != 1
                        or not target.params["page_token"]
                    ):
                        raise IncompleteTreeError("Repository tree pagination query changed")
        return paths

    async def file_content(
        self, owner: str, repo: str, path: str, ref: str, semaphore=None
    ) -> str | None:
        url = f"{self._project(owner, repo)}/repository/files/{quote(path, safe='')}/raw"

        async def _fetch() -> str | None:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        url, params={"ref": ref}, headers=self._headers(), timeout=30
                    )
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
        self,
        owner: str,
        repo: str,
        ref: str,
        max_file_size: int = 1_048_576,
        indexable_paths: set[str] | None = None,
    ) -> dict[str, str] | None:
        url = f"{self._project(owner, repo)}/repository/archive.tar.gz"
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
                resp = await client.get(url, params={"sha": ref}, headers=self._headers())
                if resp.status_code != 200:
                    logger.warning(
                        "Tarball fetch failed for %s/%s: %d", owner, repo, resp.status_code
                    )
                    return None
                blob = resp.content
        except Exception as exc:
            logger.warning("Tarball fetch failed for %s/%s: %s", owner, repo, exc)
            return None
        return _strip_tarball(blob, max_file_size, f"{owner}/{repo}", indexable_paths)


class ForgejoRepoFetcher:
    """Fetches repo content via the Gitea/Forgejo REST API (/api/v1).

    Forgejo ``owner`` and ``repo`` are simple names (no nested groups).
    Uses ``Authorization: token <token>`` for auth.
    """

    def __init__(self, token: str, base_url: str = "https://codeberg.org/api/v1") -> None:
        self._token = token
        self._api = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"token {self._token}"}

    def _repo(self, owner: str, repo: str) -> str:
        return f"{self._api}/repos/{owner}/{repo}"

    async def default_branch(self, owner: str, repo: str) -> str:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    self._repo(owner, repo), headers=self._headers(), timeout=15
                )
                resp.raise_for_status()
                return str(resp.json().get("default_branch", "main"))
        except Exception as exc:
            logger.warning("Failed to fetch default branch for %s/%s: %s", owner, repo, exc)
            return "main"

    async def repo_tree(self, owner: str, repo: str, branch: str) -> list[str]:
        url = f"{self._repo(owner, repo)}/git/trees/{quote(branch, safe='')}?recursive=true&per_page=100"
        paths: list[str] = []
        seen: set[str] = set()
        total: int | None = None
        async with httpx.AsyncClient() as client:
            for page in range(1, _MAX_TREE_PAGES + 1):
                page_url = f"{url}&page={page}" if page > 1 else url
                resp = await client.get(page_url, headers=self._headers(), timeout=30)
                if resp.status_code == 404:
                    raise EmptyRepoError(owner, repo)
                resp.raise_for_status()
                data = _tree_json(resp)
                if (
                    not isinstance(data, dict)
                    or type(data.get("total_count")) is not int
                    or data["total_count"] < 0
                    or type(data.get("truncated")) is not bool
                ):
                    raise IncompleteTreeError("Invalid repository tree pagination")
                if total is None:
                    total = data["total_count"]
                elif total != data["total_count"]:
                    raise IncompleteTreeError("Repository tree count changed during pagination")
                files, entries = _tree_paths(data.get("tree"))
                if seen & entries:
                    raise IncompleteTreeError("Duplicate repository tree page entries")
                seen.update(entries)
                paths.extend(files)
                # Forgejo can keep truncated=true even on the final page.
                if len(seen) == total:
                    return paths
                if len(seen) > total or not entries or not data["truncated"]:
                    raise IncompleteTreeError("Repository tree ended before its reported count")
        raise IncompleteTreeError("Repository tree page limit exceeded")

    async def file_content(
        self, owner: str, repo: str, path: str, ref: str, semaphore=None
    ) -> str | None:
        import base64

        url = f"{self._repo(owner, repo)}/contents/{quote(path, safe='')}"

        async def _fetch() -> str | None:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        url, params={"ref": ref}, headers=self._headers(), timeout=30
                    )
                    if resp.status_code == 404:
                        return None
                    resp.raise_for_status()
                    data = resp.json()
                    raw = data["content"]
                    return base64.b64decode(raw).decode("utf-8")
            except Exception as exc:
                logger.warning("Failed to fetch %s: %s", path, exc)
                return None

        if semaphore:
            async with semaphore:
                return await _fetch()
        return await _fetch()

    async def repo_tarball(
        self,
        owner: str,
        repo: str,
        ref: str,
        max_file_size: int = 1_048_576,
        indexable_paths: set[str] | None = None,
    ) -> dict[str, str] | None:
        url = f"{self._repo(owner, repo)}/archive/{quote(ref, safe='')}.tar.gz"
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
        return _strip_tarball(blob, max_file_size, f"{owner}/{repo}", indexable_paths)


class BitbucketRepoFetcher:
    """Fetches repo content via the Bitbucket Cloud REST 2.0 API.

    ``owner`` is the workspace slug, ``repo`` the repository slug. Listings are
    paginated through the ``next`` URL in each JSON page.
    """

    def __init__(self, token: str, base_url: str = "https://api.bitbucket.org/2.0") -> None:
        from mira.platforms.bitbucket.auth import bitbucket_auth_headers

        self._token = token
        self._api = base_url.rstrip("/")
        self._auth_headers = bitbucket_auth_headers(token)
        # ref -> commit hash. Fetchers are built per indexing run, so this also
        # pins every listing and file read in a run to one snapshot.
        self._resolved: dict[tuple[str, str, str], str] = {}

    def _headers(self) -> dict[str, str]:
        return dict(self._auth_headers)

    def _repo(self, owner: str, repo: str) -> str:
        return f"{self._api}/repositories/{quote(owner, safe='')}/{quote(repo, safe='')}"

    def _web_base(self) -> str:
        # api.bitbucket.org/2.0 → bitbucket.org, where archive downloads live.
        host = self._api.split("://", 1)[-1].split("/", 1)[0]
        return f"https://{host.removeprefix('api.')}"

    async def default_branch(self, owner: str, repo: str) -> str:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    self._repo(owner, repo), headers=self._headers(), timeout=15
                )
                resp.raise_for_status()
                return str((resp.json().get("mainbranch") or {}).get("name") or "main")
        except Exception as exc:
            logger.warning("Failed to fetch default branch for %s/%s: %s", owner, repo, exc)
            return "main"

    async def _commit_for(self, owner: str, repo: str, ref: str) -> str:
        """Resolve a branch or tag to its commit hash.

        ``/src/{ref}/`` 404s when a branch name contains ``/`` (``release/1.0``),
        encoded or not, while ``/refs/branches/{name}`` resolves it. Returns
        ``ref`` unchanged when it is already a hash or cannot be resolved, so a
        repo with no commits still surfaces as a 404 from ``/src``.
        """
        if not ref or _COMMIT_HASH.fullmatch(ref):
            return ref
        key = (owner, repo, ref)
        if key in self._resolved:
            return self._resolved[key]
        commit = ref
        async with httpx.AsyncClient(follow_redirects=True) as client:
            for kind in ("branches", "tags"):
                try:
                    resp = await client.get(
                        f"{self._repo(owner, repo)}/refs/{kind}/{quote(ref, safe='')}",
                        headers=self._headers(),
                        params={"fields": "target.hash"},
                        timeout=15,
                    )
                except Exception as exc:
                    logger.warning("Failed to resolve %s on %s/%s: %s", ref, owner, repo, exc)
                    break
                if resp.status_code == 404:
                    continue
                if resp.status_code != 200:
                    logger.warning(
                        "Failed to resolve %s on %s/%s: HTTP %d",
                        ref,
                        owner,
                        repo,
                        resp.status_code,
                    )
                    break
                commit_hash = ((resp.json() or {}).get("target") or {}).get("hash")
                if isinstance(commit_hash, str) and _COMMIT_HASH.fullmatch(commit_hash):
                    commit = commit_hash
                break
        self._resolved[key] = commit
        return commit

    async def repo_tree(self, owner: str, repo: str, branch: str) -> list[str]:
        commit = await self._commit_for(owner, repo, branch)
        initial_url = (
            f"{self._repo(owner, repo)}/src/{quote(commit, safe='')}/?max_depth=100&pagelen=100"
        )
        endpoint = httpx.URL(initial_url)
        url: str | None = initial_url
        paths: list[str] = []
        seen_urls: set[str] = set()
        seen_paths: set[str] = set()
        async with httpx.AsyncClient(follow_redirects=True) as client:
            while url:
                if url in seen_urls:
                    raise IncompleteTreeError("Repository tree pagination is cyclic")
                if len(seen_urls) >= _MAX_TREE_PAGES:
                    raise IncompleteTreeError("Repository tree page limit exceeded")
                seen_urls.add(url)
                resp = await client.get(url, headers=self._headers(), timeout=30)
                # A repo with no commits (or a missing ref) 404s.
                if resp.status_code == 404:
                    raise EmptyRepoError(owner, repo)
                resp.raise_for_status()
                data = _tree_json(resp)
                if not isinstance(data, dict):
                    raise IncompleteTreeError("Invalid repository tree page")
                files, entries = _bitbucket_tree_paths(data.get("values"))
                if seen_paths & entries:
                    raise IncompleteTreeError("Duplicate repository tree page entries")
                seen_paths.update(entries)
                paths.extend(files)
                url = data.get("next")
                if url is None:
                    break
                if not isinstance(url, str) or not url or not entries:
                    raise IncompleteTreeError("Repository tree pagination made no progress")
                try:
                    target = httpx.URL(url)
                except httpx.InvalidURL as exc:
                    raise IncompleteTreeError("Invalid repository tree pagination link") from exc
                # `next` must stay on the same listing; the page token is opaque.
                if (target.scheme, target.host, target.port, target.path) != (
                    endpoint.scheme,
                    endpoint.host,
                    endpoint.port,
                    endpoint.path,
                ) or target.userinfo:
                    raise IncompleteTreeError("Invalid repository tree pagination target")
        return paths

    async def file_content(
        self, owner: str, repo: str, path: str, ref: str, semaphore=None
    ) -> str | None:
        async def _fetch() -> str | None:
            try:
                commit = await self._commit_for(owner, repo, ref)
                url = f"{self._repo(owner, repo)}/src/{quote(commit, safe='')}/{quote(path, safe='/')}"
                async with httpx.AsyncClient(follow_redirects=True) as client:
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
        self,
        owner: str,
        repo: str,
        ref: str,
        max_file_size: int = 1_048_576,
        indexable_paths: set[str] | None = None,
    ) -> dict[str, str] | None:
        """Archive download from the web host. Returns None when the credential
        can't download archives, and the indexer falls back to per-file fetches."""
        commit = await self._commit_for(owner, repo, ref)
        url = (
            f"{self._web_base()}/{quote(owner, safe='')}/{quote(repo, safe='')}"
            f"/get/{quote(commit, safe='')}.tar.gz"
        )
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
        return _strip_tarball(blob, max_file_size, f"{owner}/{repo}", indexable_paths)


def _next_link(link_header: str) -> str | None:
    """Extract the rel="next" URL from a Link header (GitLab keyset pagination)."""
    if not link_header:
        return None
    next_url = None
    for part in link_header.split(","):
        match = re.fullmatch(r'\s*<([^<>]+)>\s*;\s*rel=(?:"([^"]+)"|([^;\s]+))\s*', part)
        if not match:
            raise IncompleteTreeError("Invalid repository tree pagination link")
        if "next" in (match[2] or match[3]).split():
            if next_url is not None:
                raise IncompleteTreeError("Multiple repository tree next links")
            next_url = match[1]
    return next_url


def make_fetcher(platform: str, token: str) -> RepoFetcher:
    """Build the RepoFetcher for a platform, using its profile's api_url."""
    profile = profiles.resolve(platform)
    api_url = profile["api_url"]
    if platform == "gitlab":
        return GitLabRepoFetcher(token, api_url or "https://gitlab.com/api/v4")
    if platform == "forgejo":
        return ForgejoRepoFetcher(token, api_url or "https://codeberg.org/api/v1")
    if platform == "bitbucket":
        return BitbucketRepoFetcher(token, api_url or "https://api.bitbucket.org/2.0")
    return GitHubRepoFetcher(token, api_url or "https://api.github.com")
