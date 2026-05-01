"""Just-in-time cross-file context for unindexed repos.

When a review runs on a repo that hasn't been indexed yet (CLI usage, fresh
GitHub App install before the indexer has finished), ``build_code_context``
falls through with empty summaries and blast radius — silently. This module
fetches imported files from HEAD on demand using the existing ``source_fetcher``
and inlines their symbols as context, so the LLM has cross-file knowledge
without waiting for a multi-minute indexing pass first.

Designed to be cheap: one tree call (1 API request) tells us which import
candidates exist, then we only fetch contents for files we know are real.
Capped at ``_MAX_FILES`` fetched files per review and a hard char budget.
"""

from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath

from mira.index.extract import extract_symbols
from mira.models import FileDiff

logger = logging.getLogger(__name__)


_PY_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))",
    re.MULTILINE,
)

_JS_IMPORT_RE = re.compile(
    r"""(?:from|require\s*\()\s*['"]([^'"]+)['"]""",
)

_RUBY_REQUIRE_RE = re.compile(
    r"""^\s*require(?:_relative)?\s+['"]([^'"]+)['"]""",
    re.MULTILINE,
)

_EXT_TO_LANG = {
    "py": "python",
    "js": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "rb": "ruby",
}

_MAX_FILES = 8
_MAX_PER_FILE_CHARS = 1500


def _ext(path: str) -> str:
    return PurePosixPath(path).suffix.lstrip(".")


def _candidates_python(module: str, source_path: str) -> list[str]:
    """Resolve a Python dotted import to candidate file paths."""
    if not module:
        return []
    rel = "/".join(module.split("."))
    cands = [f"{rel}.py", f"{rel}/__init__.py"]
    src_dir = str(PurePosixPath(source_path).parent)
    if src_dir not in (".", ""):
        cands += [f"{src_dir}/{rel}.py", f"{src_dir}/{rel}/__init__.py"]
    for root in ("src", "lib"):
        cands += [f"{root}/{rel}.py", f"{root}/{rel}/__init__.py"]
    return cands


def _normalize_relative(src_dir: str, rel: str) -> str:
    """Resolve a relative import (`./foo`, `../bar/baz`) against ``src_dir``.

    PurePosixPath doesn't collapse ``..`` segments on its own — we need
    to walk the path manually to get a clean filesystem-style path.
    """
    if rel.startswith("/"):
        return rel.lstrip("/")
    parts = [p for p in src_dir.split("/") if p and p != "."]
    for seg in rel.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


def _candidates_js(import_path: str, source_path: str) -> list[str]:
    """Resolve a JS/TS import string to candidate file paths."""
    if not import_path or not import_path.startswith("."):
        return []  # bare imports (npm packages) — skip
    src_dir = str(PurePosixPath(source_path).parent)
    base = _normalize_relative(src_dir, import_path)
    if PurePosixPath(base).suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs"):
        return [base]
    cands = []
    for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs"):
        cands.append(f"{base}{ext}")
    for ext in (".ts", ".tsx", ".js", ".jsx"):
        cands.append(f"{base}/index{ext}")
    return cands


def _candidates_ruby(import_path: str, source_path: str) -> list[str]:
    if not import_path:
        return []
    src_dir = str(PurePosixPath(source_path).parent)
    base = _normalize_relative(src_dir, import_path)
    return [base if base.endswith(".rb") else f"{base}.rb"]


def extract_import_candidates(
    source: str,
    language: str,
    source_path: str,
) -> list[str]:
    """Return candidate file paths referenced by imports in ``source``."""
    seen: set[str] = set()
    out: list[str] = []

    def add(path: str) -> None:
        if path and path not in seen:
            seen.add(path)
            out.append(path)

    if language == "python":
        for m in _PY_IMPORT_RE.finditer(source):
            module = m.group(1) or m.group(2) or ""
            for c in _candidates_python(module, source_path):
                add(c)
    elif language in ("javascript", "typescript"):
        for m in _JS_IMPORT_RE.finditer(source):
            for c in _candidates_js(m.group(1), source_path):
                add(c)
    elif language == "ruby":
        for m in _RUBY_REQUIRE_RE.finditer(source):
            for c in _candidates_ruby(m.group(1), source_path):
                add(c)
    return out


def _trim_symbol(source: str, max_chars: int) -> str:
    """Keep the symbol's signature + first lines; drop the rest if too long."""
    if len(source) <= max_chars:
        return source
    # Prefer ending on a line boundary near the budget.
    truncated = source[:max_chars]
    last_nl = truncated.rfind("\n")
    if last_nl > 0:
        truncated = truncated[:last_nl]
    return truncated + "\n    # ... (truncated)"


async def build_jit_cross_file_context(
    changed_files: list[FileDiff],
    source_fetcher,  # SourceFetcher
    repo_tree: set[str] | None,
    char_budget: int = 12_000,
) -> str:
    """Fetch source for files imported by the changed files and inline symbols.

    Args:
        changed_files: files modified in this PR.
        source_fetcher: ``ProviderSourceFetcher``-compatible object.
        repo_tree: set of all blob paths in the repo (from a tree API call).
            Used to filter import-resolution candidates so we only attempt
            content fetches for paths we know exist. ``None`` means
            "filter disabled — try every candidate" (slower).
        char_budget: hard cap on total context size.

    Returns:
        Markdown block ready to inject into the review prompt. Empty string
        if no usable cross-file context could be assembled.
    """
    if source_fetcher is None or char_budget <= 0:
        return ""

    parts: list[str] = []
    chars_used = 0
    files_added = 0
    seen_imports: set[str] = set()
    changed_paths = {f.path for f in changed_files}

    for changed_file in changed_files:
        if files_added >= _MAX_FILES or chars_used >= char_budget:
            break

        ext = _ext(changed_file.path)
        lang = _EXT_TO_LANG.get(ext)
        if not lang:
            continue

        try:
            source = await source_fetcher.fetch(changed_file.path)
        except Exception as exc:
            logger.debug("JIT: source fetch failed for %s: %s", changed_file.path, exc)
            continue
        if not source:
            continue

        candidates = extract_import_candidates(source, lang, changed_file.path)

        for cand in candidates:
            if files_added >= _MAX_FILES or chars_used >= char_budget:
                break
            if cand in seen_imports or cand in changed_paths:
                continue
            # Only fetch candidates that exist in the repo.
            if repo_tree is not None and cand not in repo_tree:
                continue
            seen_imports.add(cand)

            try:
                imported_source = await source_fetcher.fetch(cand)
            except Exception as exc:
                logger.debug("JIT: import fetch failed for %s: %s", cand, exc)
                continue
            if not imported_source:
                continue

            cand_lang = _EXT_TO_LANG.get(_ext(cand), lang)
            symbols = extract_symbols(imported_source, cand_lang)
            if not symbols:
                continue

            block_lines = [
                f"#### `{cand}` (imported by `{changed_file.path}`)",
                f"```{cand_lang}",
            ]
            file_chars = 0
            for sym in symbols:
                trimmed = _trim_symbol(sym.source, _MAX_PER_FILE_CHARS - file_chars)
                if not trimmed.strip():
                    continue
                block_lines.append(trimmed)
                block_lines.append("")
                file_chars += len(trimmed)
                if file_chars >= _MAX_PER_FILE_CHARS:
                    break
            block_lines.append("```")
            block = "\n".join(block_lines)

            if chars_used + len(block) > char_budget:
                break
            parts.append(block)
            chars_used += len(block)
            files_added += 1

    if not parts:
        return ""

    return (
        "### Imported Files (fetched on-demand)\n\n"
        "Source code from files imported by the changed files. Use this to "
        "verify call signatures, type contracts, and downstream behaviour "
        "instead of speculating about what these symbols do.\n\n" + "\n\n".join(parts)
    )
