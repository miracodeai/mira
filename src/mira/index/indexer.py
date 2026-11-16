"""LLM-based file summarization pipeline for building the codebase index."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from mira.config import MiraConfig, load_config
from mira.core.file_types import is_indexable_path, language_from_path
from mira.index.manifests import is_manifest, parse_manifest
from mira.index.store import (
    DirectorySummary,
    ExternalRef,
    FileSummary,
    IndexingSuperseded,
    IndexRun,
    IndexStore,
    SymbolInfo,
)
from mira.llm import create_llm
from mira.llm.utils import strip_think_blocks
from mira.platforms.fetch import RepoFetcher, make_fetcher

logger = logging.getLogger(__name__)


class IndexingCancelled(Exception):
    """Raised by index_repo when a cancel_check callback returns True.

    The partial count of files indexed before cancellation is attached as
    the exception's single arg.
    """

    def __init__(self, files_indexed: int) -> None:
        super().__init__(f"Indexing cancelled after {files_indexed} files")
        self.files_indexed = files_indexed


class IndexingRecoveryPending(RuntimeError):
    """An interrupted index still needs a complete recovery pass."""


_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "llm" / "prompts" / "templates"

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
# Concurrent LLM summarization batches per repo is configurable via
# index.llm_concurrency (see config.IndexConfig). The default of 8 suits
# OpenRouter-style endpoints; subscription/self-hosted endpoints with low
# concurrency limits should lower it to avoid sustained 429 backoff loops.
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


def _should_index(path: str, exclude_patterns: list[str] | None = None) -> bool:
    """Check if a file path should be indexed.

    ``exclude_patterns`` are the user's ``filter.exclude_patterns`` globs; they
    layer on top of the built-in skip list so indexing honours the same
    exclusions as review (e.g. committed vendor dirs that bloat indexing cost).
    """
    filename = os.path.basename(path)
    for pattern in _SKIP_PATTERNS:
        if fnmatch(path, pattern) or fnmatch(filename, pattern):
            return False
    if exclude_patterns:
        for pattern in exclude_patterns:
            if fnmatch(path, pattern) or fnmatch(filename, pattern):
                return False
    return is_indexable_path(filename)


def _content_hash(content: str) -> str:
    """Compute SHA256 hash of file content."""
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()


def _language_from_path(path: str) -> str:
    """Best-effort language guess from file extension. Used for trivial-file
    entries that skip the LLM."""
    return language_from_path(path)


def _safe_call(call: Any) -> tuple[str, str]:
    """Safely extract (path, symbol) from a call entry that may be str or dict or None."""
    if isinstance(call, dict):
        return call.get("path", ""), call.get("symbol", "")
    return "", ""


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


_VALID_JSON_ESCAPES = set('"\\/bfnrtu')


def _escape_lone_backslashes(text: str) -> str:
    """Escape backslashes that aren't part of a valid JSON escape sequence.

    Models like DeepSeek mention PHP namespaces (``\\App\\Models``) or Windows
    paths in summaries and emit the backslashes unescaped, so json.loads bails
    with "Invalid \\escape". We walk string literals and double any backslash
    that doesn't start a real escape, consuming valid escapes as pairs so an
    escaped quote is never mistaken for a string boundary.
    """
    out: list[str] = []
    in_string = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if not in_string:
            if ch == '"':
                in_string = True
            out.append(ch)
            i += 1
        elif ch == "\\":
            nxt = text[i + 1] if i + 1 < n else ""
            if nxt in _VALID_JSON_ESCAPES:
                out.append(ch + nxt)
                i += 2
            else:
                out.append("\\\\")
                i += 1
        else:
            if ch == '"':
                in_string = False
            out.append(ch)
            i += 1
    return "".join(out)


def _parse_summarize_response(raw: str) -> list[dict[str, Any]]:
    """Parse the LLM response from the summarization prompt."""
    text = strip_think_blocks(_strip_code_fences(raw))
    # strict=False tolerates raw newlines in strings; a repair pass for lone
    # backslashes salvages otherwise-valid responses from models that don't
    # escape paths/namespaces, so one bad string doesn't drop the whole batch.
    data: Any = None
    for candidate in (text, _escape_lone_backslashes(text)):
        try:
            data = json.loads(candidate, strict=False)
            break
        except (json.JSONDecodeError, TypeError):
            continue
    else:
        logger.warning("Failed to parse summarization response: %s", raw[:300])
        return []

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


def _build_file_summary(path: str, content: str, file_data: dict[str, Any]) -> FileSummary:
    """Convert LLM output for a single file into a FileSummary."""
    symbols = []
    for sym in file_data.get("symbols", []):
        name = sym.get("name") or ""
        if not name:
            continue
        # `or ""` not `get(key, "")` — models emit explicit nulls that the
        # default wouldn't catch, and these columns are NOT NULL.
        symbols.append(
            SymbolInfo(
                name=name,
                kind=sym.get("kind") or "function",
                signature=sym.get("signature") or "",
                description=sym.get("description") or "",
            )
        )

    symbol_refs = []
    for ref in file_data.get("symbol_references", []):
        source_sym = ref.get("source", "")
        for call in ref.get("calls", []):
            target_path, target_sym = _safe_call(call)
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


def _indexing_temperature_kwargs(llm: Any) -> dict[str, Any]:
    config = llm.config
    if config.provider in {"bedrock", "codex-cli", "codex_cli", "codex"}:
        # Bedrock uses the established 0.0 for summaries. Codex CLI manages sampling itself.
        return {"temperature": 0.0}
    if config.indexing_temperature == "omit":
        return {"omit_temperature": True}
    return {"temperature": config.indexing_temperature}


async def _summarize_batch(
    files: list[tuple[str, str]],  # (path, content)
    llm: Any,
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
                **_indexing_temperature_kwargs(llm),
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


@contextmanager
def _complete_index_run(store: IndexStore, run: IndexRun) -> Iterator[None]:
    # Keep the pending marker after an exception or cancellation. A later
    # incremental push must reconcile paths it may not have in its payload.
    try:
        yield
    except IndexingSuperseded:
        raise
    except Exception as exc:
        # An error from a stale fetch must not be reported as the current run's
        # error. Check again before allowing its caller to handle the error.
        with store.index_write(run):
            pass
        if run.needs_reconciliation and not isinstance(
            exc, (IndexingCancelled, IndexingRecoveryPending)
        ):
            raise IndexingRecoveryPending(f"Index reconciliation incomplete: {exc}") from exc
        raise
    else:
        store.finish_index_run(run)


def _queue_vulnerability_scan(owner: str, repo: str) -> None:
    try:
        from mira.security.poller import poll_repo

        asyncio.create_task(poll_repo(owner, repo))
    except Exception as exc:
        logger.debug("Failed to schedule vuln poll for %s/%s: %s", owner, repo, exc)


async def index_repo(
    owner: str,
    repo: str,
    token: str | None = None,
    config: MiraConfig | None = None,
    store: IndexStore | None = None,
    llm: Any = None,
    full: bool = False,
    branch: str | None = None,
    cancel_check: Callable[[], bool] | None = None,
    fetcher: RepoFetcher | None = None,
    *,
    run: IndexRun | None = None,
    reset: bool = False,
    platform: str = "github",
    tree_paths: list[str] | None = None,
) -> int:
    """Index a full repository. Returns number of files indexed.

    Args:
        full: If True, re-index all files regardless of content hash.
        branch: Branch to index from.
        tree_paths: Complete tree already fetched for the supplied branch or revision.
        cancel_check: Optional zero-arg callable returning True to request
            early termination. Checked between summarization batches. Raises
            ``IndexingCancelled`` on cancel so callers can distinguish a
            deliberate stop from a failure.
    """
    if config is None:
        config = load_config()
    if llm is None:
        from mira.dashboard.models_config import llm_config_for

        llm = create_llm(llm_config_for("indexing", config.llm))
    owns_store = store is None
    if store is None:
        store = IndexStore.open(owner, repo, platform=platform, allow_fallback=False)
    # Back-compat: callers that pass a raw GitHub token get a GitHub fetcher.
    if fetcher is None:
        fetcher = make_fetcher("github", token or "")

    try:
        run = run if run is not None else store.claim_index_run()
        with _complete_index_run(store, run):
            count = await _index_repo(
                owner,
                repo,
                config,
                store,
                llm,
                fetcher,
                branch,
                run,
                full=full,
                reset=reset,
                cancel_check=cancel_check,
                platform=platform,
                tree_paths=tree_paths,
            )
        _queue_vulnerability_scan(owner, repo)
        return count
    finally:
        if owns_store:
            store.close()


async def _index_repo(
    owner: str,
    repo: str,
    config: MiraConfig,
    store: IndexStore,
    llm: Any,
    fetcher: RepoFetcher,
    branch: str | None,
    run: IndexRun,
    *,
    full: bool = False,
    reset: bool = False,
    cancel_check: Callable[[], bool] | None = None,
    platform: str = "github",
    tree_paths: list[str] | None = None,
) -> int:
    # Auto-detect default branch if not specified
    if branch is None:
        branch = await fetcher.default_branch(owner, repo)

    # Fetch repo tree
    if tree_paths is None:
        tree_paths = await fetcher.repo_tree(owner, repo, branch)
    indexable = [p for p in tree_paths if _should_index(p, config.filter.exclude_patterns)]
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
    max_file_size = config.index.max_file_size
    fetch_sem = asyncio.Semaphore(_FILE_FETCH_SEMAPHORE)
    indexable_set = set(indexable)
    tarball = await fetcher.repo_tarball(
        owner,
        repo,
        branch,
        max_file_size=max_file_size,
        indexable_paths=indexable_set,
    )
    contents = [tarball.get(p) if tarball is not None else None for p in indexable]
    if tarball is None:
        logger.info("Tarball unavailable, falling back to per-file fetch")
    if tarball is None or run.needs_reconciliation:
        # A missing archive entry can be a fetch failure or a deliberate
        # size cap. Retry omissions before a recovery clears pending.
        missing = [i for i, content in enumerate(contents) if content is None]
        fetch_tasks = [
            fetcher.file_content(owner, repo, indexable[i], branch, semaphore=fetch_sem)
            for i in missing
        ]
        for i, content in zip(missing, await asyncio.gather(*fetch_tasks), strict=True):
            contents[i] = content

    if run.needs_reconciliation and any(content is None for content in contents):
        raise IndexingRecoveryPending("Could not read every file needed to reconcile the index")

    # Filter out failed fetches and compute hashes for staleness check
    file_pairs: list[tuple[str, str]] = []
    trivial_pairs: list[tuple[str, str]] = []
    skipped_large = 0
    for path, content in zip(indexable, contents, strict=False):
        if content is None:
            continue
        # Catches the per-file fetch path, which the tarball cap doesn't cover.
        if max_file_size and len(content.encode("utf-8")) > max_file_size:
            skipped_large += 1
            continue
        if not (full or reset):
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
        "Indexing %d files (%d trivial / skipped %d unchanged / %d over size limit)",
        len(file_pairs) + len(trivial_pairs),
        len(trivial_pairs),
        len(indexable) - len(file_pairs) - len(trivial_pairs) - skipped_large,
        skipped_large,
    )

    with store.index_write(run):
        # Clean up deleted files
        existing_paths = store.all_paths()
        tree_set = set(tree_paths)
        deleted = existing_paths if reset else existing_paths - tree_set
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
                    loc=content.count("\n") + (0 if content.endswith("\n") else 1)
                    if content
                    else 0,
                )
            )

    # Batch summarize. The previous `for batch in batches: await ...` pattern
    # was serial — `_summarize_batch`'s internal semaphore allowed N
    # concurrent calls, but only one batch was ever in flight at a time, so
    # `_LLM_SEMAPHORE` was effectively pinned at 1. Fire all batches as
    # tasks and consume them as they complete; the inner semaphore bounds
    # actual parallelism. Cancellation still works between batch
    # completions.
    llm_sem = asyncio.Semaphore(config.index.llm_concurrency)
    batches = _build_batches(file_pairs)
    summary_tasks = [
        asyncio.create_task(_summarize_batch(batch, llm, llm_sem)) for batch in batches
    ]

    indexed_count = len(trivial_pairs)
    summarized: set[str] = set()
    try:
        for fut in asyncio.as_completed(summary_tasks):
            if cancel_check and cancel_check():
                for t in summary_tasks:
                    if not t.done():
                        t.cancel()
                logger.info(
                    "Indexing cancelled for %s/%s after %d files",
                    owner,
                    repo,
                    indexed_count,
                )
                raise IndexingCancelled(indexed_count)
            try:
                results = await fut
            except Exception as exc:
                # Skip a failed batch and keep going — one bad batch shouldn't
                # abort the repo (and aborting leaves the other tasks burning tokens).
                logger.warning("Skipping a summarization batch for %s/%s: %s", owner, repo, exc)
                continue
            summaries = []
            for path, content, data in results:
                try:
                    summaries.append(_build_file_summary(path, content, data))
                except Exception as exc:
                    logger.warning("Skipping file %s in %s/%s: %s", path, owner, repo, exc)
            if summaries:
                with store.index_write(run):
                    for summary in summaries:
                        store.upsert_summary(summary)
                indexed_count += len(summaries)
                summarized.update(summary.path for summary in summaries)
    finally:
        for task in summary_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*summary_tasks, return_exceptions=True)

    if run.needs_reconciliation and any(path not in summarized for path, _ in file_pairs):
        raise IndexingRecoveryPending("Missing file summaries during index reconciliation")

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
            fetcher,
            branch,
            store,
            tree_paths,
            fetch_sem,
            cached_contents=tarball,
            run=run,
        )
    except IndexingSuperseded:
        raise
    except Exception as exc:
        if run.needs_reconciliation:
            raise
        logger.warning("Manifest indexing failed for %s/%s: %s", owner, repo, exc)

    # ── Conventions pass (no LLM calls — pure file reads) ──
    # Pull CONTRIBUTING.md / AGENTS.md / STYLE.md from the tarball cache and
    # extract team-specific coding rules. Stored on the repos row so the
    # review prompt can inject them.
    try:
        await _index_conventions(
            owner,
            repo,
            fetcher,
            branch,
            tree_paths,
            fetch_sem,
            tarball,
            store=store,
            run=run,
            platform=platform,
        )
    except IndexingSuperseded:
        raise
    except Exception as exc:
        if run.needs_reconciliation:
            raise
        logger.warning("Conventions indexing failed for %s/%s: %s", owner, repo, exc)

    await _summarize_directories(store, llm, llm_sem, run)

    logger.info("Indexing complete: %d files indexed for %s/%s", indexed_count, owner, repo)
    return indexed_count


async def _read_paths(
    owner: str,
    repo: str,
    fetcher: RepoFetcher,
    branch: str,
    paths: list[str],
    fetch_sem: asyncio.Semaphore,
    cached_contents: dict[str, str] | None,
) -> dict[str, str | None]:
    """Contents for *paths*: whatever the cache holds, fetching the rest.

    The tarball cache is built from indexable paths alone, so a caller wanting a
    path indexing skips — a conventions file, a manifest the summarizer ignores —
    never finds it there. An absent key means "not cached" rather than "empty
    file", so those paths are fetched instead of being read as blank.
    """
    cached = cached_contents or {}
    missing = [p for p in paths if not cached.get(p)]
    fetched: dict[str, str | None] = {}
    if missing:
        results = await asyncio.gather(
            *(fetcher.file_content(owner, repo, p, branch, semaphore=fetch_sem) for p in missing)
        )
        fetched = dict(zip(missing, results, strict=False))
    return {p: cached.get(p) or fetched.get(p) for p in paths}


async def _index_conventions(
    owner: str,
    repo: str,
    fetcher: RepoFetcher,
    branch: str,
    tree_paths: list[str],
    fetch_sem: asyncio.Semaphore,
    cached_contents: dict[str, str] | None = None,
    *,
    store: IndexStore,
    run: IndexRun,
    platform: str = "github",
) -> None:
    """Read team-conventions files from the repo and persist the extracted
    rules so they can be injected into review prompts.
    """
    from mira.index.conventions import _CONVENTION_FILES, extract_conventions

    # Files we'll attempt — only ones the tree actually contains.
    tree_set = set(tree_paths)
    candidates = [p for p in _CONVENTION_FILES if p in tree_set]

    if not candidates:
        return

    resolved = await _read_paths(
        owner, repo, fetcher, branch, candidates, fetch_sem, cached_contents
    )
    if run.needs_reconciliation and any(content is None for content in resolved.values()):
        raise IndexingRecoveryPending("Could not read every conventions file during reconciliation")
    file_contents = {p: (c or "") for p, c in resolved.items()}

    extracted = extract_conventions(file_contents)
    if not extracted:
        return

    try:
        from mira.dashboard.api import _app_db

        with store.index_write(run):
            _app_db.set_repo_conventions(owner, repo, extracted, platform=platform)
        logger.info(
            "Extracted %d-char conventions block for %s/%s from %s",
            len(extracted),
            owner,
            repo,
            ", ".join(p for p, c in file_contents.items() if c),
        )
    except IndexingSuperseded:
        raise
    except Exception as exc:
        if run.needs_reconciliation:
            raise
        logger.warning("Failed to persist conventions for %s/%s: %s", owner, repo, exc)


def _store_manifest_file(store: IndexStore, path: str, content: str) -> int:
    """Parse one manifest and replace its package rows. An empty parse still
    replaces, so stale entries for the path are dropped. Returns package count."""
    packages = parse_manifest(path, content, for_inventory=True)
    store.replace_manifest_packages(
        path,
        [
            {
                "name": pkg.name,
                "kind": pkg.kind,
                "version": pkg.version,
                "file_path": pkg.file_path,
                "is_dev": pkg.is_dev,
                "scan_eligible": pkg.scan_eligible,
            }
            for pkg in packages
        ],
    )
    return len(packages)


async def _index_manifests(
    owner: str,
    repo: str,
    fetcher: RepoFetcher,
    branch: str,
    store: IndexStore,
    tree_paths: list[str],
    fetch_sem: asyncio.Semaphore,
    cached_contents: dict[str, str] | None = None,
    *,
    run: IndexRun,
) -> None:
    """Fetch known manifest files, parse them, and persist declared packages.

    Runs after the LLM summarization pass. Entirely deterministic — no LLM
    calls. ``cached_contents`` (typically a tarball-extracted dict) supplies
    whichever manifests it holds; the rest are fetched.
    """
    manifest_paths = [p for p in tree_paths if is_manifest(p)]
    resolved = await _read_paths(
        owner, repo, fetcher, branch, manifest_paths, fetch_sem, cached_contents
    )
    total_packages = 0
    live = {path for path in manifest_paths if resolved[path] is not None}
    if run.needs_reconciliation and len(live) != len(manifest_paths):
        raise IndexingRecoveryPending("Could not read every manifest during reconciliation")
    with store.index_write(run):
        for path in manifest_paths:
            content = resolved[path]
            if content is not None:
                total_packages += _store_manifest_file(store, path, content)
        # A failed fetch is not evidence that the manifest disappeared.
        store.clear_manifest_packages_for_missing_files(set(manifest_paths))

    if total_packages:
        logger.info(
            "Indexed %d package(s) across %d manifest file(s) for %s/%s",
            total_packages,
            len(live),
            owner,
            repo,
        )


async def _summarize_directories(
    store: IndexStore, llm: Any, semaphore: asyncio.Semaphore, run: IndexRun
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
                    **_indexing_temperature_kwargs(llm),
                    max_tokens=4096,
                )
                data = json.loads(strip_think_blocks(_strip_code_fences(raw)))
            except Exception as exc:
                if run.needs_reconciliation:
                    raise
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

        if run.needs_reconciliation and any(path not in by_path for path, _, _ in chunk):
            raise IndexingRecoveryPending("Missing directory summaries during reconciliation")
        with store.index_write(run):
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

    tasks = [asyncio.create_task(_process_chunk(chunk)) for chunk in chunks]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _refresh_manifests_incremental(
    owner: str,
    repo: str,
    fetcher: RepoFetcher,
    branch: str,
    store: IndexStore,
    changed_paths: list[str] | None,
    removed_paths: list[str] | None,
    run: IndexRun,
) -> int:
    """Refresh package rows for manifests touched by a push. Returns the
    number of manifest files updated or cleared."""
    changed = [p for p in changed_paths or [] if is_manifest(p)]
    removed = [p for p in removed_paths or [] if is_manifest(p)]
    if not changed and not removed:
        return 0
    fetch_sem = asyncio.Semaphore(_FILE_FETCH_SEMAPHORE)
    contents = await asyncio.gather(
        *(fetcher.file_content(owner, repo, p, branch, semaphore=fetch_sem) for p in changed)
    )
    touched = len(removed)
    with store.index_write(run):
        for path in removed:
            store.replace_manifest_packages(path, [])
        for path, content in zip(changed, contents, strict=False):
            if content is None:
                continue
            _store_manifest_file(store, path, content)
            touched += 1
    if touched:
        logger.info("Refreshed %d manifest file(s) for %s/%s", touched, owner, repo)
    return touched


async def index_diff(
    owner: str,
    repo: str,
    token: str | None = None,
    config: MiraConfig | None = None,
    store: IndexStore | None = None,
    llm: Any = None,
    changed_paths: list[str] | None = None,
    removed_paths: list[str] | None = None,
    branch: str = "main",
    fetcher: RepoFetcher | None = None,
    *,
    run: IndexRun | None = None,
    platform: str = "github",
    changes_only: bool = False,
) -> int:
    """Index a diff and return the number of files re-indexed.

    With changes_only, apply changed paths under the write guard and leave
    reconciliation pending. Use this when a complete tree is unavailable.
    """
    if config is None:
        config = load_config()
    if llm is None:
        from mira.dashboard.models_config import llm_config_for

        llm = create_llm(llm_config_for("indexing", config.llm))
    owns_store = store is None
    if store is None:
        store = IndexStore.open(owner, repo, platform=platform, allow_fallback=False)
    if fetcher is None:
        fetcher = make_fetcher("github", token or "")

    try:
        run = run if run is not None else store.claim_index_run()
        with nullcontext() if changes_only else _complete_index_run(store, run):
            if run.needs_reconciliation and not changes_only:
                logger.info("Reconciling unfinished index for %s/%s", owner, repo)
                count = await _index_repo(
                    owner, repo, config, store, llm, fetcher, branch, run, platform=platform
                )
                poll = True
            else:
                count, poll, missing_count = await _index_diff(
                    owner,
                    repo,
                    config,
                    store,
                    llm,
                    fetcher,
                    branch,
                    run,
                    changed_paths,
                    None if changes_only else removed_paths,
                )
                if missing_count and not changes_only:
                    # Manifest writes are independent of file summaries and already committed.
                    if poll:
                        _queue_vulnerability_scan(owner, repo)
                    raise IndexingRecoveryPending(
                        f"Missing content or summaries for {missing_count} changed files"
                    )
        if poll:
            _queue_vulnerability_scan(owner, repo)
        return count
    finally:
        if owns_store:
            store.close()


async def _index_diff(
    owner: str,
    repo: str,
    config: MiraConfig,
    store: IndexStore,
    llm: Any,
    fetcher: RepoFetcher,
    branch: str,
    run: IndexRun,
    changed_paths: list[str] | None,
    removed_paths: list[str] | None,
) -> tuple[int, bool, int]:
    # Remove deleted files
    if removed_paths:
        with store.index_write(run):
            store.remove_paths(removed_paths)
        logger.info("Removed %d deleted files from index", len(removed_paths))

    # Manifests/lockfiles are excluded from code indexing but feed the package
    # inventory — a dependency-bump push usually touches nothing else (#157).
    touched = 0
    try:
        touched = await _refresh_manifests_incremental(
            owner, repo, fetcher, branch, store, changed_paths, removed_paths, run
        )
    except IndexingSuperseded:
        raise
    except Exception as exc:
        logger.warning("Manifest refresh failed for %s/%s: %s", owner, repo, exc)

    # Filter to indexable files
    to_index = [p for p in changed_paths or [] if _should_index(p, config.filter.exclude_patterns)]
    if not to_index:
        return 0, bool(touched), 0

    # Fetch content
    fetch_sem = asyncio.Semaphore(_FILE_FETCH_SEMAPHORE)
    tasks = [
        fetcher.file_content(owner, repo, path, branch, semaphore=fetch_sem) for path in to_index
    ]
    contents = await asyncio.gather(*tasks)

    max_file_size = config.index.max_file_size
    file_pairs: list[tuple[str, str]] = []
    unreadable: set[str] = set()
    for path, content in zip(to_index, contents, strict=False):
        if content is None:
            unreadable.add(path)
            continue
        if max_file_size and len(content.encode("utf-8")) > max_file_size:
            continue
        file_pairs.append((path, content))

    # Summarize changed files
    llm_sem = asyncio.Semaphore(config.index.llm_concurrency)
    indexed_count = 0
    summarized: set[str] = set()

    if file_pairs:
        batches = _build_batches(file_pairs)
        # Run batches concurrently — the inner semaphore caps real parallelism.
        all_results = await asyncio.gather(
            *(_summarize_batch(batch, llm, llm_sem) for batch in batches)
        )
        for results in all_results:
            if results:
                with store.index_write(run):
                    for path, content, data in results:
                        summary = _build_file_summary(path, content, data)
                        store.upsert_summary(summary)
                indexed_count += len(results)
                summarized.update(path for path, _, _ in results)

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
        await _summarize_directories_selective(store, llm, llm_sem, affected_dirs, run)

    logger.info(
        "Incremental index: %d files re-indexed for %s/%s",
        indexed_count,
        owner,
        repo,
    )
    missing = {path for path, _ in file_pairs} - summarized
    return indexed_count, bool(touched), len(missing | unreadable)


async def _summarize_directories_selective(
    store: IndexStore,
    llm: Any,
    semaphore: asyncio.Semaphore,
    target_dirs: set[str],
    run: IndexRun,
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
                raw = await llm.complete(
                    messages, json_mode=True, **_indexing_temperature_kwargs(llm)
                )
                data = json.loads(strip_think_blocks(_strip_code_fences(raw)))
                summary_text = data.get("summary", "")
            except Exception as exc:
                logger.warning("Directory summary failed for %s: %s", display_path, exc)
                continue
        if summary_text:
            with store.index_write(run):
                store.upsert_directory(
                    DirectorySummary(
                        path=dir_path,
                        summary=summary_text,
                        file_count=len(file_paths),
                    )
                )
