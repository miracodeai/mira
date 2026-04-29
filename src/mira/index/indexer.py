"""LLM-based file summarization pipeline for building the codebase index."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Callable
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import httpx
from jinja2 import Environment, FileSystemLoader

from mira.config import MiraConfig, load_config
from mira.index.manifests import is_manifest, parse_manifest
from mira.index.store import DirectorySummary, ExternalRef, FileSummary, IndexStore, SymbolInfo
from mira.llm.provider import LLMProvider

logger = logging.getLogger(__name__)


class IndexingCancelled(Exception):
    """Raised by index_repo when a cancel_check callback returns True.

    The partial count of files indexed before cancellation is attached as
    the exception's single arg.
    """

    def __init__(self, files_indexed: int) -> None:
        super().__init__(f"Indexing cancelled after {files_indexed} files")
        self.files_indexed = files_indexed


_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "llm" / "prompts" / "templates"

# File extensions we index (source code only)
_INDEXABLE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".rb",
    ".php",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".swift",
    ".kt",
    ".scala",
    ".sh",
    ".bash",
    ".zsh",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
    ".sql",
    ".graphql",
    ".proto",
}

# Patterns to always skip (binaries, vendored code, lock files, etc.)
_SKIP_PATTERNS = [
    "*.lock",
    "*.lockb",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Pipfile.lock",
    "poetry.lock",
    "go.sum",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.svg",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.ico",
    "*.woff",
    "*.woff2",
    "*.ttf",
    "*.eot",
    "*.pdf",
    "*.zip",
    "*.tar.gz",
    "*.gz",
    "*.bz2",
    "*.exe",
    "*.dll",
    "*.so",
    "*.dylib",
    "node_modules/*",
    "vendor/*",
    ".git/*",
    "__pycache__/*",
    "dist/*",
    "build/*",
    ".next/*",
    ".nuxt/*",
]

_FILE_FETCH_SEMAPHORE = 10
_LLM_SEMAPHORE = 3
_BATCH_SIZE = 5  # files per LLM summarization call


def _should_index(path: str) -> bool:
    """Check if a file path should be indexed."""
    filename = os.path.basename(path)
    # Check skip patterns
    for pattern in _SKIP_PATTERNS:
        if fnmatch(path, pattern) or fnmatch(filename, pattern):
            return False
    # Check extension
    _, ext = os.path.splitext(filename)
    return ext.lower() in _INDEXABLE_EXTENSIONS


def _content_hash(content: str) -> str:
    """Compute SHA256 hash of file content."""
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


async def _fetch_default_branch(owner: str, repo: str, token: str) -> str:
    """Fetch the default branch name for a repo."""
    url = f"https://api.github.com/repos/{owner}/{repo}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            return str(resp.json().get("default_branch", "main"))
    except Exception as exc:
        logger.warning("Failed to fetch default branch for %s/%s: %s", owner, repo, exc)
        return "main"


async def _fetch_repo_tree(owner: str, repo: str, token: str, branch: str = "main") -> list[str]:
    """Fetch the file tree for a repo via GitHub API."""
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}?recursive=1"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

    paths = []
    for item in data.get("tree", []):
        if item.get("type") == "blob":
            paths.append(item["path"])
    return paths


async def _fetch_file_content(
    owner: str,
    repo: str,
    path: str,
    token: str,
    ref: str = "main",
    semaphore: asyncio.Semaphore | None = None,
) -> str | None:
    """Fetch a single file's content from GitHub."""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}?ref={ref}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.raw+json",
    }

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


def _strip_code_fences(raw: str) -> str:
    """Strip markdown code fences (```json ... ```) from LLM output."""
    text = raw.strip()
    if text.startswith("```"):
        # Remove opening fence (```json or ```)
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]
        # Remove closing fence
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


def _parse_summarize_response(raw: str) -> list[dict[str, Any]]:
    """Parse the LLM response from the summarization prompt."""
    text = _strip_code_fences(raw)
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "files" in data:
            result: list[dict[str, Any]] = data["files"]
            return result
        if isinstance(data, list):
            return list(data)
        logger.warning(
            "Summarization response has unexpected structure (keys: %s): %s",
            list(data.keys()) if isinstance(data, dict) else type(data).__name__,
            text[:200],
        )
        return []
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning(
            "Failed to parse summarization response (%s): %s",
            exc,
            raw[:300],
        )
        return []


def _build_file_summary(path: str, content: str, file_data: dict[str, Any]) -> FileSummary:
    """Convert LLM output for a single file into a FileSummary."""
    symbols = []
    for sym in file_data.get("symbols", []):
        symbols.append(
            SymbolInfo(
                name=sym.get("name", ""),
                kind=sym.get("kind", "function"),
                signature=sym.get("signature", ""),
                description=sym.get("description", ""),
            )
        )

    symbol_refs = []
    for ref in file_data.get("symbol_references", []):
        source_sym = ref.get("source", "")
        for call in ref.get("calls", []):
            target_path = call.get("path", "")
            target_sym = call.get("symbol", "")
            if source_sym and target_path and target_sym:
                symbol_refs.append((source_sym, target_path, target_sym))

    external_refs = []
    for eref in file_data.get("external_refs", []):
        kind = eref.get("kind", "")
        target = eref.get("target", "")
        if kind and target:
            external_refs.append(
                ExternalRef(
                    file_path=path,
                    kind=kind,
                    target=target,
                    description=eref.get("description", ""),
                )
            )

    return FileSummary(
        path=path,
        language=file_data.get("language", ""),
        summary=file_data.get("summary", ""),
        symbols=symbols,
        imports=file_data.get("imports", []),
        symbol_refs=symbol_refs,
        external_refs=external_refs,
        content_hash=_content_hash(content),
    )


async def _summarize_batch(
    files: list[tuple[str, str]],  # (path, content)
    llm: LLMProvider,
    semaphore: asyncio.Semaphore,
) -> list[tuple[str, str, dict[str, Any]]]:
    """Summarize a batch of files using the LLM. Returns (path, content, data) triples."""
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("summarize.jinja2")

    file_entries = [{"path": path, "content": content} for path, content in files]
    prompt_text = template.render(files=file_entries)

    messages = [
        {"role": "system", "content": prompt_text},
        {"role": "user", "content": "Summarize the files above."},
    ]

    async with semaphore:
        try:
            raw = await llm.complete(messages, json_mode=True, temperature=0.0)
        except Exception as exc:
            logger.warning("LLM summarization failed for batch of %d files: %s", len(files), exc)
            return []

    parsed = _parse_summarize_response(raw)

    results = []
    parsed_by_path = {d.get("path", ""): d for d in parsed}
    for path, content in files:
        if path in parsed_by_path:
            results.append((path, content, parsed_by_path[path]))
        else:
            logger.debug("No summary returned for %s", path)
    return results


async def index_repo(
    owner: str,
    repo: str,
    token: str,
    config: MiraConfig | None = None,
    store: IndexStore | None = None,
    llm: LLMProvider | None = None,
    full: bool = False,
    branch: str | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> int:
    """Index a full repository. Returns number of files indexed.

    Args:
        full: If True, re-index all files regardless of content hash.
        branch: Branch to index from.
        cancel_check: Optional zero-arg callable returning True to request
            early termination. Checked between summarization batches. Raises
            ``IndexingCancelled`` on cancel so callers can distinguish a
            deliberate stop from a failure.
    """
    if config is None:
        config = load_config()
    if llm is None:
        from mira.dashboard.models_config import llm_config_for

        llm = LLMProvider(llm_config_for("indexing", config.llm))
    if store is None:
        store = IndexStore.open(owner, repo)

    # Auto-detect default branch if not specified
    if branch is None:
        branch = await _fetch_default_branch(owner, repo, token)

    # Fetch repo tree
    tree_paths = await _fetch_repo_tree(owner, repo, token, branch)
    indexable = [p for p in tree_paths if _should_index(p)]
    logger.info(
        "Found %d indexable files in %s/%s (out of %d total)",
        len(indexable),
        owner,
        repo,
        len(tree_paths),
    )

    # Fetch content for all indexable files
    fetch_sem = asyncio.Semaphore(_FILE_FETCH_SEMAPHORE)
    tasks = [
        _fetch_file_content(owner, repo, path, token, ref=branch, semaphore=fetch_sem)
        for path in indexable
    ]
    contents = await asyncio.gather(*tasks)

    # Filter out failed fetches and compute hashes for staleness check
    file_pairs: list[tuple[str, str]] = []
    for path, content in zip(indexable, contents, strict=False):
        if content is None:
            continue
        if not full:
            existing = store.get_summary(path)
            if existing and existing.content_hash == _content_hash(content):
                continue
        file_pairs.append((path, content))

    logger.info(
        "Indexing %d files (skipped %d unchanged)",
        len(file_pairs),
        len(indexable) - len(file_pairs),
    )

    # Clean up deleted files
    existing_paths = store.all_paths()
    tree_set = set(tree_paths)
    deleted = existing_paths - tree_set
    if deleted:
        store.remove_paths(list(deleted))
        logger.info("Removed %d deleted files from index", len(deleted))

    # Batch summarize
    llm_sem = asyncio.Semaphore(_LLM_SEMAPHORE)
    batches = [file_pairs[i : i + _BATCH_SIZE] for i in range(0, len(file_pairs), _BATCH_SIZE)]

    indexed_count = 0
    for batch in batches:
        if cancel_check and cancel_check():
            logger.info(
                "Indexing cancelled for %s/%s after %d files",
                owner,
                repo,
                indexed_count,
            )
            raise IndexingCancelled(indexed_count)
        results = await _summarize_batch(batch, llm, llm_sem)
        for path, content, data in results:
            summary = _build_file_summary(path, content, data)
            store.upsert_summary(summary)
            indexed_count += 1

    if cancel_check and cancel_check():
        logger.info(
            "Indexing cancelled for %s/%s before directory pass",
            owner,
            repo,
        )
        raise IndexingCancelled(indexed_count)

    # ── Package manifest pass (no LLM calls — pure parsers) ──
    # Fetches known manifest files (package.json, requirements.txt, etc.) and
    # records each declared dependency with its version constraint.
    try:
        await _index_manifests(owner, repo, token, branch, store, tree_paths, fetch_sem)
    except Exception as exc:
        logger.warning("Manifest indexing failed for %s/%s: %s", owner, repo, exc)

    # ── Vulnerability scan (fire-and-forget) ──
    # Triggers an OSV.dev poll for this repo's packages so freshly-indexed
    # manifests get a vuln check without waiting for the next hourly tick.
    # Doesn't block indexing completion.
    try:
        from mira.security.poller import poll_repo as _vuln_poll_repo

        asyncio.create_task(_vuln_poll_repo(owner, repo))
    except Exception as exc:
        logger.debug("Failed to schedule vuln poll for %s/%s: %s", owner, repo, exc)

    # Directory summarization pass
    await _summarize_directories(store, llm, llm_sem)

    logger.info("Indexing complete: %d files indexed for %s/%s", indexed_count, owner, repo)
    return indexed_count


async def _index_manifests(
    owner: str,
    repo: str,
    token: str,
    branch: str,
    store: IndexStore,
    tree_paths: list[str],
    fetch_sem: asyncio.Semaphore,
) -> None:
    """Fetch known manifest files, parse them, and persist declared packages.

    Runs after the LLM summarization pass. Entirely deterministic — no LLM
    calls, so the marginal indexing cost is one network round-trip per
    manifest file found in the repo tree.
    """
    manifest_paths = [p for p in tree_paths if is_manifest(p)]
    if not manifest_paths:
        store.clear_manifest_packages_for_missing_files(set())
        return

    tasks = [
        _fetch_file_content(owner, repo, p, token, ref=branch, semaphore=fetch_sem)
        for p in manifest_paths
    ]
    contents = await asyncio.gather(*tasks)

    live: set[str] = set()
    total_packages = 0
    for path, content in zip(manifest_paths, contents, strict=False):
        if content is None:
            continue
        live.add(path)
        packages = parse_manifest(path, content)
        if not packages:
            # Still replace with empty so stale entries for this path are dropped.
            store.replace_manifest_packages(path, [])
            continue
        store.replace_manifest_packages(
            path,
            [
                {
                    "name": pkg.name,
                    "kind": pkg.kind,
                    "version": pkg.version,
                    "file_path": pkg.file_path,
                    "is_dev": pkg.is_dev,
                }
                for pkg in packages
            ],
        )
        total_packages += len(packages)

    # Drop manifest entries whose source file disappeared from the repo.
    store.clear_manifest_packages_for_missing_files(live)

    if total_packages:
        logger.info(
            "Indexed %d package(s) across %d manifest file(s) for %s/%s",
            total_packages,
            len(live),
            owner,
            repo,
        )


async def _summarize_directories(
    store: IndexStore, llm: LLMProvider, semaphore: asyncio.Semaphore
) -> None:
    """Generate directory summaries from file summaries.

    Processes one directory at a time to avoid token limits and partial failures.
    """
    all_paths = store.all_paths()
    dirs: dict[str, list[str]] = {}
    for path in all_paths:
        parent = str(Path(path).parent)
        if parent == ".":
            parent = ""
        dirs.setdefault(parent, []).append(path)

    if not dirs:
        return

    logger.info("Generating summaries for %d directories", len(dirs))

    for dir_path, file_paths in sorted(dirs.items()):
        # Build file summaries list for this directory (truncate to prevent huge prompts)
        file_summaries = []
        for fp in file_paths[:30]:  # cap at 30 files per directory
            s = store.get_summary(fp)
            if s and s.summary:
                file_summaries.append(f"- {os.path.basename(fp)}: {s.summary}")
        if not file_summaries:
            continue

        display_path = dir_path or "(root)"
        prompt = (
            "You are a code indexing assistant. Generate a concise 1-2 sentence summary "
            f"describing what this directory contains and its purpose.\n\n"
            f"Directory: {display_path} ({len(file_paths)} files)\n"
            + "\n".join(file_summaries)
            + '\n\nRespond with JSON: {"summary": "..."}'
        )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "Summarize this directory."},
        ]

        async with semaphore:
            try:
                raw = await llm.complete(messages, json_mode=True, temperature=0.0)
                data = json.loads(_strip_code_fences(raw))
                summary_text = data.get("summary", "")
                if summary_text:
                    store.upsert_directory(
                        DirectorySummary(
                            path=dir_path,
                            summary=summary_text,
                            file_count=len(file_paths),
                        )
                    )
            except Exception as exc:
                logger.warning("Directory summary failed for %s: %s", display_path, exc)


async def index_diff(
    owner: str,
    repo: str,
    token: str,
    config: MiraConfig | None = None,
    store: IndexStore | None = None,
    llm: LLMProvider | None = None,
    changed_paths: list[str] | None = None,
    removed_paths: list[str] | None = None,
    branch: str = "main",
) -> int:
    """Incremental index for changed files. Returns number of files re-indexed."""
    if config is None:
        config = load_config()
    if llm is None:
        from mira.dashboard.models_config import llm_config_for

        llm = LLMProvider(llm_config_for("indexing", config.llm))
    if store is None:
        store = IndexStore.open(owner, repo)

    # Remove deleted files
    if removed_paths:
        store.remove_paths(removed_paths)
        logger.info("Removed %d deleted files from index", len(removed_paths))

    if not changed_paths:
        return 0

    # Filter to indexable files
    to_index = [p for p in changed_paths if _should_index(p)]
    if not to_index:
        return 0

    # Fetch content
    fetch_sem = asyncio.Semaphore(_FILE_FETCH_SEMAPHORE)
    tasks = [
        _fetch_file_content(owner, repo, path, token, ref=branch, semaphore=fetch_sem)
        for path in to_index
    ]
    contents = await asyncio.gather(*tasks)

    file_pairs: list[tuple[str, str]] = []
    for path, content in zip(to_index, contents, strict=False):
        if content is not None:
            file_pairs.append((path, content))

    # Summarize changed files
    llm_sem = asyncio.Semaphore(_LLM_SEMAPHORE)
    indexed_count = 0

    if file_pairs:
        batches = [file_pairs[i : i + _BATCH_SIZE] for i in range(0, len(file_pairs), _BATCH_SIZE)]
        for batch in batches:
            results = await _summarize_batch(batch, llm, llm_sem)
            for path, content, data in results:
                summary = _build_file_summary(path, content, data)
                store.upsert_summary(summary)
                indexed_count += 1

    # Re-generate directory summaries for affected parent dirs
    affected_dirs: set[str] = set()
    for path in changed_paths or []:
        parent = str(Path(path).parent)
        affected_dirs.add("" if parent == "." else parent)
    if removed_paths:
        for path in removed_paths:
            parent = str(Path(path).parent)
            affected_dirs.add("" if parent == "." else parent)
    if affected_dirs:
        logger.info("Re-generating summaries for %d affected directories", len(affected_dirs))
        await _summarize_directories_selective(store, llm, llm_sem, affected_dirs)

    logger.info(
        "Incremental index: %d files re-indexed for %s/%s",
        indexed_count,
        owner,
        repo,
    )
    return indexed_count


async def _summarize_directories_selective(
    store: IndexStore,
    llm: LLMProvider,
    semaphore: asyncio.Semaphore,
    target_dirs: set[str],
) -> None:
    """Re-generate directory summaries only for the specified directories."""
    all_paths = store.all_paths()

    for dir_path in sorted(target_dirs):
        # Collect files in this directory
        file_paths = [
            p
            for p in all_paths
            if (str(Path(p).parent) == dir_path) or (dir_path == "" and str(Path(p).parent) == ".")
        ]
        if not file_paths:
            continue

        file_summaries = []
        for fp in file_paths[:30]:
            s = store.get_summary(fp)
            if s and s.summary:
                file_summaries.append(f"- {os.path.basename(fp)}: {s.summary}")
        if not file_summaries:
            continue

        display_path = dir_path or "(root)"
        prompt = (
            "You are a code indexing assistant. Generate a concise 1-2 sentence summary "
            f"describing what this directory contains and its purpose.\n\n"
            f"Directory: {display_path} ({len(file_paths)} files)\n"
            + "\n".join(file_summaries)
            + '\n\nRespond with JSON: {"summary": "..."}'
        )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "Summarize this directory."},
        ]

        async with semaphore:
            try:
                raw = await llm.complete(messages, json_mode=True, temperature=0.0)
                data = json.loads(_strip_code_fences(raw))
                summary_text = data.get("summary", "")
                if summary_text:
                    store.upsert_directory(
                        DirectorySummary(
                            path=dir_path,
                            summary=summary_text,
                            file_count=len(file_paths),
                        )
                    )
            except Exception as exc:
                logger.warning("Directory summary failed for %s: %s", display_path, exc)
