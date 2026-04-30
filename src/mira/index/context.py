"""Review-time context builder. Queries the index to enrich the review prompt.

Now async with support for fetching real source code from the PR's head branch.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol, runtime_checkable

from mira.index.extract import extract_symbols, find_symbol_by_name
from mira.index.store import IndexStore
from mira.models import PRInfo
from mira.providers.base import BaseProvider

logger = logging.getLogger(__name__)

_DEFAULT_TOKEN_BUDGET = 8_000
_CHARS_PER_TOKEN = 4  # conservative estimate


@runtime_checkable
class SourceFetcher(Protocol):
    """Protocol for fetching source code from a repository."""

    async def fetch(self, path: str) -> str | None:
        """Fetch the content of a file. Returns None on failure."""
        ...


class ProviderSourceFetcher:
    """Fetches source code from the PR's head branch via a provider."""

    def __init__(self, provider: BaseProvider, pr_info: PRInfo, ref: str) -> None:
        self._provider = provider
        self._pr_info = pr_info
        self._ref = ref
        self._cache: dict[str, str | None] = {}

    async def fetch(self, path: str) -> str | None:
        if path in self._cache:
            return self._cache[path]
        try:
            content = await self._provider.get_file_content(self._pr_info, path, self._ref)
            self._cache[path] = content if content else None
            return self._cache[path]
        except Exception as exc:
            logger.debug("Failed to fetch source for %s: %s", path, exc)
            self._cache[path] = None
            return None


async def build_code_context(
    changed_paths: list[str],
    store: IndexStore,
    token_budget: int = _DEFAULT_TOKEN_BUDGET,
    source_fetcher: SourceFetcher | None = None,
) -> str:
    """Build a compact codebase context block for the review prompt.

    Queries the index for summaries of changed files, their imports,
    parent directories, and the blast radius. When a source_fetcher is
    provided, fetches actual source code of affected functions.

    Token budget is split into tiers:
    - 60% real source code (when source_fetcher is available)
    - 30% summaries
    - 10% directory structure

    Returns a formatted string ready to inject into the review prompt.
    """
    char_budget = token_budget * _CHARS_PER_TOKEN
    parts: list[str] = []
    parts.append("## Codebase Context\n")

    # Rank changed files by inbound edge count so the most-depended-on files
    # get priority in the token budget.
    try:
        edge_counts = store.get_inbound_edge_counts(changed_paths)
        changed_paths = sorted(changed_paths, key=lambda p: edge_counts.get(p, 0), reverse=True)
    except Exception:
        pass  # Fall through with original order

    # Calculate tier budgets
    if source_fetcher:
        source_budget = int(char_budget * 0.60)
        summary_budget = int(char_budget * 0.30)
        dir_budget = int(char_budget * 0.10)
    else:
        source_budget = 0
        summary_budget = int(char_budget * 0.90)
        dir_budget = int(char_budget * 0.10)

    # Track which files have real source shown (excluded from summaries)
    files_with_source: set[str] = set()

    # 1. Directory summaries (10% budget)
    dir_parts: list[str] = []
    parent_dirs = sorted({str(Path(p).parent) for p in changed_paths if str(Path(p).parent) != "."})
    if parent_dirs:
        dir_summaries = store.get_directory_summaries(parent_dirs)
        if dir_summaries:
            dir_parts.append("### Repository Structure")
            for dir_path in sorted(dir_summaries):
                ds = dir_summaries[dir_path]
                dir_parts.append(f"- `{ds.path}/`: {ds.summary} ({ds.file_count} files)")
            dir_parts.append("")

    dir_text = "\n".join(dir_parts)
    if len(dir_text) > dir_budget:
        dir_text = dir_text[:dir_budget]
        last_nl = dir_text.rfind("\n")
        if last_nl > 0:
            dir_text = dir_text[:last_nl]
    if dir_text.strip():
        parts.append(dir_text)

    # 2. Real source code (60% budget, when source_fetcher available)
    if source_fetcher and source_budget > 0:
        source_parts: list[str] = []
        source_chars_used = 0

        # Get blast radius to know which files + symbols are affected
        blast_radius = store.get_blast_radius(changed_paths)
        blast_by_path = {e.path: e for e in blast_radius}

        # Fetch source for changed files, then blast radius ranked by
        # number of affected symbols (most impacted first)
        all_source_paths = list(changed_paths)
        blast_sorted = sorted(blast_radius, key=lambda e: len(e.affected_symbols), reverse=True)
        for entry in blast_sorted:
            if entry.path not in all_source_paths:
                all_source_paths.append(entry.path)

        source_parts.append("### Source Code")
        source_parts.append("")

        for path in all_source_paths:
            if source_chars_used >= source_budget:
                break

            try:
                source = await source_fetcher.fetch(path)
            except Exception as exc:
                logger.debug("Source fetch failed for %s: %s", path, exc)
                continue
            if not source:
                continue

            # Detect language from file extension
            ext = Path(path).suffix.lstrip(".")
            lang = _ext_to_language(ext)

            # For blast-radius files, only extract affected symbols
            blast_entry = blast_by_path.get(path)
            if blast_entry and path not in changed_paths:
                symbol_parts: list[str] = []
                for sym_name in blast_entry.affected_symbols:
                    span = find_symbol_by_name(source, lang, sym_name)
                    if span:
                        symbol_parts.append(
                            f"#### `{path}` — `{sym_name}` (lines {span.start_line}-{span.end_line})"
                        )
                        symbol_parts.append(f"```{lang}")
                        symbol_parts.append(span.source)
                        symbol_parts.append("```")
                        symbol_parts.append("")
                if symbol_parts:
                    block = "\n".join(symbol_parts)
                    if source_chars_used + len(block) <= source_budget:
                        source_parts.extend(symbol_parts)
                        source_chars_used += len(block)
                        files_with_source.add(path)
            else:
                # Changed files: extract all symbols
                symbols = extract_symbols(source, lang)
                if symbols:
                    for sym in symbols:
                        block_lines = [
                            f"#### `{path}` — `{sym.name}` (lines {sym.start_line}-{sym.end_line})",
                            f"```{lang}",
                            sym.source,
                            "```",
                            "",
                        ]
                        block = "\n".join(block_lines)
                        if source_chars_used + len(block) > source_budget:
                            break
                        source_parts.extend(block_lines)
                        source_chars_used += len(block)
                    files_with_source.add(path)

        if source_chars_used > 0:
            parts.extend(source_parts)

    # 3. Changed files with full summary + symbol list (30% budget)
    summary_parts: list[str] = []
    changed_summaries = store.get_summaries(changed_paths)
    if changed_summaries:
        summary_parts.append("### Changed Files")
        for path in sorted(changed_summaries):
            if path in files_with_source:
                continue  # Dedup: already shown as source
            fs = changed_summaries[path]
            summary_parts.append(f"- `{fs.path}`: {fs.summary}")
            for sym in fs.symbols:
                summary_parts.append(f"  - `{sym.signature}`: {sym.description}")
            if fs.imports:
                imports_str = ", ".join(fs.imports)
                summary_parts.append(f"  - Imports: {imports_str}")
        summary_parts.append("")

    # Related files (imported by changed files)
    import_paths: set[str] = set()
    for path in changed_paths:
        changed_fs = changed_summaries.get(path)
        if changed_fs:
            import_paths.update(changed_fs.imports)
    import_paths -= set(changed_paths)
    import_paths -= files_with_source  # Dedup

    if import_paths:
        import_summaries = store.get_summaries(list(import_paths))
        if import_summaries:
            summary_parts.append("### Related Files (imported by changed files)")
            for path in sorted(import_summaries):
                fs = import_summaries[path]
                summary_parts.append(f"- `{fs.path}`: {fs.summary}")
                for sym in fs.symbols:
                    summary_parts.append(f"  - `{sym.signature}`: {sym.description}")
            summary_parts.append("")

    summary_text = "\n".join(summary_parts)
    if len(summary_text) > summary_budget:
        summary_text = summary_text[:summary_budget]
        last_nl = summary_text.rfind("\n")
        if last_nl > 0:
            summary_text = summary_text[:last_nl]
    if summary_text.strip():
        parts.append(summary_text)

    # 4. Blast radius (from remaining summary budget)
    blast_radius = store.get_blast_radius(changed_paths)
    blast_parts: list[str] = []
    if blast_radius:
        blast_parts.append("### Blast Radius (code that depends on changed files)")
        for entry in blast_radius:
            if entry.path in files_with_source:
                continue  # Already shown as source
            symbols_str = ", ".join(f"`{s}()`" for s in entry.affected_symbols)
            depth_label = f"depth {entry.depth}"
            blast_parts.append(f"- `{entry.path}` \u2192 calls {symbols_str} ({depth_label})")
        blast_parts.append("")

    if blast_parts:
        parts.extend(blast_parts)

    result = "\n".join(parts)

    # Final truncation safeguard
    if len(result) > char_budget:
        result = result[:char_budget]
        last_nl = result.rfind("\n")
        if last_nl > 0:
            result = result[:last_nl]
        result += "\n\n*(codebase context truncated to fit token budget)*"

    return result


def _ext_to_language(ext: str) -> str:
    """Map file extension to language identifier."""
    mapping = {
        "py": "python",
        "js": "javascript",
        "ts": "typescript",
        "tsx": "typescript",
        "jsx": "javascript",
        "go": "go",
        "rs": "rust",
        "java": "java",
        "rb": "ruby",
        "php": "php",
        "c": "c",
        "cpp": "cpp",
        "h": "c",
        "hpp": "cpp",
        "cs": "cs",
        "swift": "swift",
        "kt": "kotlin",
        "scala": "scala",
    }
    return mapping.get(ext.lower(), ext.lower())
