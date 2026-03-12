"""Review-time context builder. Queries the index to enrich the review prompt."""

from __future__ import annotations

import logging
from pathlib import Path

from mira.index.store import IndexStore

logger = logging.getLogger(__name__)

_DEFAULT_TOKEN_BUDGET = 8_000
_CHARS_PER_TOKEN = 4  # conservative estimate


def build_code_context(
    changed_paths: list[str],
    store: IndexStore,
    token_budget: int = _DEFAULT_TOKEN_BUDGET,
) -> str:
    """Build a compact codebase context block for the review prompt.

    Queries the index for summaries of changed files, their imports,
    parent directories, and the blast radius of changes.

    Returns a formatted string ready to inject into the review prompt.
    """
    char_budget = token_budget * _CHARS_PER_TOKEN
    parts: list[str] = []
    parts.append("## Codebase Context\n")

    # 1. Directory summaries for parent directories
    parent_dirs = sorted({str(Path(p).parent) for p in changed_paths if str(Path(p).parent) != "."})
    if parent_dirs:
        dir_summaries = store.get_directory_summaries(parent_dirs)
        if dir_summaries:
            parts.append("### Repository Structure")
            for dir_path in sorted(dir_summaries):
                ds = dir_summaries[dir_path]
                parts.append(f"- `{ds.path}/`: {ds.summary} ({ds.file_count} files)")
            parts.append("")

    # 2. Changed files with full summary + symbol list
    changed_summaries = store.get_summaries(changed_paths)
    if changed_summaries:
        parts.append("### Changed Files")
        for path in sorted(changed_summaries):
            fs = changed_summaries[path]
            parts.append(f"- `{fs.path}`: {fs.summary}")
            for sym in fs.symbols:
                parts.append(f"  - `{sym.signature}`: {sym.description}")
            if fs.imports:
                imports_str = ", ".join(fs.imports)
                parts.append(f"  - Imports: {imports_str}")
        parts.append("")

    # 3. Related files (imported by changed files)
    import_paths: set[str] = set()
    for path in changed_paths:
        changed_fs = changed_summaries.get(path)
        if changed_fs:
            import_paths.update(changed_fs.imports)
    # Exclude changed files themselves
    import_paths -= set(changed_paths)

    if import_paths:
        import_summaries = store.get_summaries(list(import_paths))
        if import_summaries:
            parts.append("### Related Files (imported by changed files)")
            for path in sorted(import_summaries):
                fs = import_summaries[path]
                parts.append(f"- `{fs.path}`: {fs.summary}")
                for sym in fs.symbols:
                    parts.append(f"  - `{sym.signature}`: {sym.description}")
            parts.append("")

    # 4. Blast radius
    blast_radius = store.get_blast_radius(changed_paths)
    if blast_radius:
        parts.append("### Blast Radius (code that depends on changed files)")
        for entry in blast_radius:
            symbols_str = ", ".join(f"`{s}()`" for s in entry.affected_symbols)
            depth_label = f"depth {entry.depth}"
            parts.append(f"- `{entry.path}` \u2192 calls {symbols_str} ({depth_label})")
        parts.append("")

    result = "\n".join(parts)

    # Enforce token budget by truncating
    if len(result) > char_budget:
        result = result[:char_budget]
        # Truncate at last newline to avoid broken lines
        last_nl = result.rfind("\n")
        if last_nl > 0:
            result = result[:last_nl]
        result += "\n\n*(codebase context truncated to fit token budget)*"

    return result
