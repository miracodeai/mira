"""LLM-based file summarization pipeline for building the codebase index."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from fnmatch import fnmatch
from pathlib import Path

import httpx
from jinja2 import Environment, FileSystemLoader

from mira.config import MiraConfig, load_config
from mira.index.store import DirectorySummary, FileSummary, IndexStore, SymbolInfo
from mira.llm.provider import LLMProvider

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "llm" / "prompts" / "templates"

# File extensions we index (source code only)
_INDEXABLE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
    ".rb", ".php", ".c", ".cpp", ".h", ".hpp", ".cs", ".swift",
    ".kt", ".scala", ".sh", ".bash", ".zsh", ".yaml", ".yml",
    ".toml", ".json", ".sql", ".graphql", ".proto",
}

# Patterns to always skip (binaries, vendored code, lock files, etc.)
_SKIP_PATTERNS = [
    "*.lock", "*.lockb", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Pipfile.lock", "poetry.lock", "go.sum",
    "*.min.js", "*.min.css", "*.map",
    "*.svg", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico",
    "*.woff", "*.woff2", "*.ttf", "*.eot",
    "*.pdf", "*.zip", "*.tar.gz", "*.gz", "*.bz2",
    "*.exe", "*.dll", "*.so", "*.dylib",
    "node_modules/*", "vendor/*", ".git/*", "__pycache__/*",
    "dist/*", "build/*", ".next/*", ".nuxt/*",
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
    owner: str, repo: str, path: str, token: str, ref: str = "main",
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


def _parse_summarize_response(raw: str) -> list[dict]:
    """Parse the LLM response from the summarization prompt."""
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "files" in data:
            return data["files"]
        if isinstance(data, list):
            return data
        return []
    except (json.JSONDecodeError, TypeError):
        logger.warning("Failed to parse summarization response")
        return []


def _build_file_summary(path: str, content: str, file_data: dict) -> FileSummary:
    """Convert LLM output for a single file into a FileSummary."""
    symbols = []
    for sym in file_data.get("symbols", []):
        symbols.append(SymbolInfo(
            name=sym.get("name", ""),
            kind=sym.get("kind", "function"),
            signature=sym.get("signature", ""),
            description=sym.get("description", ""),
        ))

    symbol_refs = []
    for ref in file_data.get("symbol_references", []):
        source_sym = ref.get("source", "")
        for call in ref.get("calls", []):
            target_path = call.get("path", "")
            target_sym = call.get("symbol", "")
            if source_sym and target_path and target_sym:
                symbol_refs.append((source_sym, target_path, target_sym))

    return FileSummary(
        path=path,
        language=file_data.get("language", ""),
        summary=file_data.get("summary", ""),
        symbols=symbols,
        imports=file_data.get("imports", []),
        symbol_refs=symbol_refs,
        content_hash=_content_hash(content),
    )


async def _summarize_batch(
    files: list[tuple[str, str]],  # (path, content)
    llm: LLMProvider,
    semaphore: asyncio.Semaphore,
) -> list[tuple[str, str, dict]]:
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
    branch: str = "main",
) -> int:
    """Index a full repository. Returns number of files indexed.

    Args:
        full: If True, re-index all files regardless of content hash.
        branch: Branch to index from.
    """
    if config is None:
        config = load_config()
    if llm is None:
        llm = LLMProvider(config.llm)
    if store is None:
        store = IndexStore.open(owner, repo)

    # Fetch repo tree
    tree_paths = await _fetch_repo_tree(owner, repo, token, branch)
    indexable = [p for p in tree_paths if _should_index(p)]
    logger.info(
        "Found %d indexable files in %s/%s (out of %d total)",
        len(indexable), owner, repo, len(tree_paths),
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
        len(file_pairs), len(indexable) - len(file_pairs),
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
    batches = [file_pairs[i:i + _BATCH_SIZE] for i in range(0, len(file_pairs), _BATCH_SIZE)]

    indexed_count = 0
    for batch in batches:
        results = await _summarize_batch(batch, llm, llm_sem)
        for path, content, data in results:
            summary = _build_file_summary(path, content, data)
            store.upsert_summary(summary)
            indexed_count += 1

    # Directory summarization pass
    await _summarize_directories(store, llm, llm_sem)

    logger.info("Indexing complete: %d files indexed for %s/%s", indexed_count, owner, repo)
    return indexed_count


async def _summarize_directories(
    store: IndexStore, llm: LLMProvider, semaphore: asyncio.Semaphore
) -> None:
    """Generate directory summaries from file summaries."""
    all_paths = store.all_paths()
    dirs: dict[str, list[str]] = {}
    for path in all_paths:
        parent = str(Path(path).parent)
        if parent == ".":
            parent = ""
        dirs.setdefault(parent, []).append(path)

    if not dirs:
        return

    # Build directory info for LLM
    dir_entries = []
    for dir_path, file_paths in sorted(dirs.items()):
        file_summaries = []
        for fp in file_paths:
            s = store.get_summary(fp)
            if s:
                file_summaries.append(f"- {fp}: {s.summary}")
        if file_summaries:
            dir_entries.append({
                "path": dir_path or "(root)",
                "file_count": len(file_paths),
                "files": "\n".join(file_summaries),
            })

    if not dir_entries:
        return

    prompt = (
        "You are a code indexing assistant. For each directory, generate a 1-2 sentence summary "
        "describing what the directory contains and its purpose. Respond in JSON:\n"
        '{"directories": [{"path": "...", "summary": "..."}]}\n\n'
    )
    for entry in dir_entries:
        prompt += (
            f"\n### Directory: {entry['path']} "
            f"({entry['file_count']} files)\n{entry['files']}\n"
        )

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "Summarize the directories above."},
    ]

    async with semaphore:
        try:
            raw = await llm.complete(messages, json_mode=True, temperature=0.0)
            data = json.loads(raw)
            for d in data.get("directories", []):
                dir_path = d.get("path", "")
                if dir_path == "(root)":
                    dir_path = ""
                store.upsert_directory(DirectorySummary(
                    path=dir_path,
                    summary=d.get("summary", ""),
                    file_count=len(dirs.get(dir_path, [])),
                ))
        except Exception as exc:
            logger.warning("Directory summarization failed: %s", exc)


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
        llm = LLMProvider(config.llm)
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

    if not file_pairs:
        return 0

    # Summarize
    llm_sem = asyncio.Semaphore(_LLM_SEMAPHORE)
    batches = [file_pairs[i:i + _BATCH_SIZE] for i in range(0, len(file_pairs), _BATCH_SIZE)]

    indexed_count = 0
    for batch in batches:
        results = await _summarize_batch(batch, llm, llm_sem)
        for path, content, data in results:
            summary = _build_file_summary(path, content, data)
            store.upsert_summary(summary)
            indexed_count += 1

    # Re-index dependents if exported symbols changed
    for path, _content in file_pairs:
        existing = store.get_summary(path)
        if existing:
            dependents = store.get_dependents(path)
            if dependents:
                logger.debug(
                    "File %s has %d dependents that may need re-indexing",
                    path, len(dependents),
                )

    logger.info(
        "Incremental index: %d files re-indexed for %s/%s",
        indexed_count, owner, repo,
    )
    return indexed_count
