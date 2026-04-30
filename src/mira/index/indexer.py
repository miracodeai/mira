"""LLM-based file summarization pipeline for building the codebase index."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import tarfile
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
# Concurrent LLM summarization batches per repo. Haiku handles 6-8
# comfortably on OpenRouter; bumping from 3 nearly halves file-phase wall
# time without changing quality.
_LLM_SEMAPHORE = 8
# Smaller batches → faster individual calls → better wave parallelism.
# Empirically a 5-file batch with one large file bloated to 7k output
# tokens and took 87s, dominating an entire indexing run. With 3-file
# batches, the same content takes ~20-25s and the next wave starts sooner.
_BATCH_SIZE = 3
# Files larger than this go into their own solo batch instead of being
# packed with siblings. A single 8k-line file in a 3-file batch otherwise
# stretches generation to ~80s while its batch-mates wait. Solo batching
# isolates the cost.
_LARGE_FILE_BYTES = 5000
# Files smaller than this byte threshold skip the LLM batch entirely. The
# LLM summary adds little value for a 5-line `__init__.py` re-export or a
# tiny constants file, and counting those toward the batch quota slows the
# whole repo. Stored as no-summary entries so they still count for the
# file index and any deterministic features (manifest parsing, etc.).
_TRIVIAL_FILE_BYTES = 600


def _build_batches(file_pairs: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """Split files into batches, with large files getting solo batches.

    Regular files pack into groups of ``_BATCH_SIZE``; any file over
    ``_LARGE_FILE_BYTES`` becomes its own batch so a single oversized
    summarization doesn't block its batch-mates.
    """
    large = [p for p in file_pairs if len(p[1]) >= _LARGE_FILE_BYTES]
    small = [p for p in file_pairs if len(p[1]) < _LARGE_FILE_BYTES]
    batches: list[list[tuple[str, str]]] = [[p] for p in large]
    for i in range(0, len(small), _BATCH_SIZE):
        batches.append(small[i : i + _BATCH_SIZE])
    return batches


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


_EXT_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".cpp": "cpp",
    ".c": "c",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".swift": "swift",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
    ".sql": "sql",
    ".graphql": "graphql",
    ".proto": "protobuf",
}


def _language_from_path(path: str) -> str:
    """Best-effort language guess from file extension. Used for trivial-file
    entries that skip the LLM."""
    _, ext = os.path.splitext(path)
    return _EXT_LANG.get(ext.lower(), "")


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


async def _fetch_repo_tarball(
    owner: str,
    repo: str,
    token: str,
    ref: str = "main",
) -> dict[str, str] | None:
    """Download the entire repo as a tarball and return ``{path: content}``.

    GitHub's ``/repos/{owner}/{repo}/tarball/{ref}`` returns one redirected
    download containing every file. For a 50-file repo, this is ~5-10 s
    *total* — vs. ~5+ minutes worth of per-file Contents-API roundtrips at
    our previous concurrency cap.

    Returns ``None`` on failure (caller should fall back to per-file fetch).
    Files larger than 1 MB or undecodable as UTF-8 are skipped silently.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/tarball/{ref}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "mira-indexer",
    }
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.warning(
                    "Tarball fetch failed for %s/%s: %d",
                    owner,
                    repo,
                    resp.status_code,
                )
                return None
            blob = resp.content
    except Exception as exc:
        logger.warning("Tarball fetch failed for %s/%s: %s", owner, repo, exc)
        return None

    out: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            for member in tf:
                if not member.isfile():
                    continue
                if member.size > 1_048_576:  # 1 MB
                    continue
                # Tarballs are wrapped in a top-level dir like "owner-repo-{sha}/".
                # Strip that prefix so paths match the GitHub tree paths.
                parts = member.name.split("/", 1)
                if len(parts) != 2:
                    continue
                rel_path = parts[1]
                if not rel_path:
                    continue
                f = tf.extractfile(member)
                if f is None:
                    continue
                try:
                    out[rel_path] = f.read().decode("utf-8")
                except UnicodeDecodeError:
                    continue
    except (tarfile.TarError, OSError) as exc:
        logger.warning("Tarball extract failed for %s/%s: %s", owner, repo, exc)
        return None

    logger.info("Tarball: fetched %d files for %s/%s in one request", len(out), owner, repo)
    return out


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
        loc=content.count("\n") + (0 if content.endswith("\n") else 1) if content else 0,
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
            # Use the model's full output capacity instead of the default
            # 4096 — large batches were getting truncated mid-JSON
            # (`finish_reason: length` in OpenRouter logs).
            from mira.llm.registry import max_output_tokens

            cap = min(max_output_tokens(llm.config.model, default=16384), 32768)
            raw = await llm.complete(
                messages,
                json_mode=True,
                temperature=0.0,
                max_tokens=cap,
            )
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

    # Fetch content for all indexable files. Try the bulk tarball download
    # first — one request gets every file, ~10x faster than per-file fetches
    # for typical repos. Fall back to the per-file API if the tarball fails
    # (e.g. for a >100 MB repo where GitHub may reject the request).
    fetch_sem = asyncio.Semaphore(_FILE_FETCH_SEMAPHORE)
    tarball: dict[str, str] | None = await _fetch_repo_tarball(owner, repo, token, ref=branch)

    if tarball is not None:
        contents: list[str | None] = [tarball.get(p) for p in indexable]
    else:
        logger.info("Tarball unavailable, falling back to per-file fetch")
        tasks = [
            _fetch_file_content(owner, repo, path, token, ref=branch, semaphore=fetch_sem)
            for path in indexable
        ]
        contents = await asyncio.gather(*tasks)

    # Filter out failed fetches and compute hashes for staleness check
    file_pairs: list[tuple[str, str]] = []
    trivial_pairs: list[tuple[str, str]] = []
    for path, content in zip(indexable, contents, strict=False):
        if content is None:
            continue
        if not full:
            existing = store.get_summary(path)
            if existing and existing.content_hash == _content_hash(content):
                continue
        # Tiny files don't justify an LLM call. Routed into a separate
        # bucket and stored with a deterministic placeholder summary; they
        # still appear in the file index so blast radius / search work.
        if len(content) < _TRIVIAL_FILE_BYTES:
            trivial_pairs.append((path, content))
        else:
            file_pairs.append((path, content))

    logger.info(
        "Indexing %d files (%d trivial / skipped %d unchanged)",
        len(file_pairs) + len(trivial_pairs),
        len(trivial_pairs),
        len(indexable) - len(file_pairs) - len(trivial_pairs),
    )

    # Clean up deleted files
    existing_paths = store.all_paths()
    tree_set = set(tree_paths)
    deleted = existing_paths - tree_set
    if deleted:
        store.remove_paths(list(deleted))
        logger.info("Removed %d deleted files from index", len(deleted))

    # Persist trivial files immediately — no LLM, no batch quota.
    for path, content in trivial_pairs:
        store.upsert_summary(
            FileSummary(
                path=path,
                language=_language_from_path(path),
                summary="",
                symbols=[],
                imports=[],
                external_refs=[],
                content_hash=_content_hash(content),
                loc=content.count("\n") + (0 if content.endswith("\n") else 1) if content else 0,
            )
        )

    # Batch summarize. The previous `for batch in batches: await ...` pattern
    # was serial — `_summarize_batch`'s internal semaphore allowed N
    # concurrent calls, but only one batch was ever in flight at a time, so
    # `_LLM_SEMAPHORE` was effectively pinned at 1. Fire all batches as
    # tasks and consume them as they complete; the inner semaphore bounds
    # actual parallelism. Cancellation still works between batch
    # completions.
    llm_sem = asyncio.Semaphore(_LLM_SEMAPHORE)
    batches = _build_batches(file_pairs)
    tasks = [asyncio.create_task(_summarize_batch(batch, llm, llm_sem)) for batch in batches]

    indexed_count = len(trivial_pairs)
    try:
        for fut in asyncio.as_completed(tasks):
            if cancel_check and cancel_check():
                for t in tasks:
                    if not t.done():
                        t.cancel()
                logger.info(
                    "Indexing cancelled for %s/%s after %d files",
                    owner,
                    repo,
                    indexed_count,
                )
                raise IndexingCancelled(indexed_count)
            results = await fut
            for path, content, data in results:
                summary = _build_file_summary(path, content, data)
                store.upsert_summary(summary)
                indexed_count += 1
    except IndexingCancelled:
        raise

    if cancel_check and cancel_check():
        logger.info(
            "Indexing cancelled for %s/%s before directory pass",
            owner,
            repo,
        )
        raise IndexingCancelled(indexed_count)

    # ── Package manifest pass (no LLM calls — pure parsers) ──
    # Fetches known manifest files (package.json, requirements.txt, etc.) and
    # records each declared dependency. Reuses the tarball cache when we
    # already have it so manifests don't trigger their own fetch loop.
    try:
        await _index_manifests(
            owner,
            repo,
            token,
            branch,
            store,
            tree_paths,
            fetch_sem,
            cached_contents=tarball,
        )
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
    cached_contents: dict[str, str] | None = None,
) -> None:
    """Fetch known manifest files, parse them, and persist declared packages.

    Runs after the LLM summarization pass. Entirely deterministic — no LLM
    calls. If ``cached_contents`` (typically a tarball-extracted dict) is
    provided, manifest contents are read from there instead of re-fetching.
    """
    manifest_paths = [p for p in tree_paths if is_manifest(p)]
    if not manifest_paths:
        store.clear_manifest_packages_for_missing_files(set())
        return

    if cached_contents is not None:
        contents: list[str | None] = [cached_contents.get(p) for p in manifest_paths]
    else:
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

    Batched into chunks of ~15 directories per LLM call. Each directory was
    previously its own ~200-token round trip — at ~500ms of fixed network
    overhead per request, that meant 12 directories ate ~6s of pure latency
    for ~3K tokens of actual work. One batched call covers them all.
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

    logger.info("Generating summaries for %d directories (batched)", len(dirs))

    # Build a list of (dir_path, file_count, [file_summaries]) for every dir
    # that has any file with a non-empty LLM summary. Trivial-only dirs are
    # skipped — we have nothing for the LLM to compress.
    enriched: list[tuple[str, int, list[str]]] = []
    for dir_path, file_paths in sorted(dirs.items()):
        file_summaries = []
        for fp in file_paths[:30]:
            s = store.get_summary(fp)
            if s and s.summary:
                file_summaries.append(f"- {os.path.basename(fp)}: {s.summary}")
        if not file_summaries:
            continue
        enriched.append((dir_path, len(file_paths), file_summaries))

    if not enriched:
        return

    chunk_size = 15
    chunks = [enriched[i : i + chunk_size] for i in range(0, len(enriched), chunk_size)]

    async def _process_chunk(chunk: list[tuple[str, int, list[str]]]) -> None:
        sections: list[str] = []
        for dir_path, file_count, summaries in chunk:
            display = dir_path or "(root)"
            sections.append(f"### {display} ({file_count} files)\n" + "\n".join(summaries))
        prompt = (
            "You are a code indexing assistant. For each directory below, "
            "generate a concise 1-2 sentence summary describing what it "
            "contains and its purpose.\n\n" + "\n\n".join(sections) + "\n\nRespond with JSON: "
            '{"directories": [{"path": "<dir>", "summary": "..."}, ...]}. '
            'Use "(root)" as the path for the repo-root directory.'
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "Summarize these directories."},
        ]
        async with semaphore:
            try:
                raw = await llm.complete(
                    messages,
                    json_mode=True,
                    temperature=0.0,
                    max_tokens=4096,
                )
                data = json.loads(_strip_code_fences(raw))
            except Exception as exc:
                logger.warning("Directory batch summary failed: %s", exc)
                return

        # Map returned summaries back to dir paths. Use "(root)" → "" so the
        # store key matches what other queries use.
        by_path: dict[str, str] = {}
        for entry in data.get("directories") or []:
            if not isinstance(entry, dict):
                continue
            p = str(entry.get("path", "")).strip()
            if p == "(root)":
                p = ""
            s = str(entry.get("summary", "")).strip()
            if s:
                by_path[p] = s

        for dir_path, file_count, _summaries in chunk:
            summary_text = by_path.get(dir_path)
            if summary_text:
                store.upsert_directory(
                    DirectorySummary(
                        path=dir_path,
                        summary=summary_text,
                        file_count=file_count,
                    )
                )

    await asyncio.gather(*(_process_chunk(c) for c in chunks))


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
        batches = _build_batches(file_pairs)
        # Run batches concurrently — the inner semaphore caps real parallelism.
        all_results = await asyncio.gather(
            *(_summarize_batch(batch, llm, llm_sem) for batch in batches)
        )
        for results in all_results:
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
