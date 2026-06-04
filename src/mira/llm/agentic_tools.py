"""Tools the reviewer LLM can call when the repo isn't indexed.

When a repo has no pre-built index, JIT cross-file context covers Python /
JS / TS / Ruby (parseable imports) but leaves Java and Go gaps because their
import resolution needs build-system context. To close that gap, give the
reviewer two tools — `read_file` and `grep_repo` — and let it pull the
specific cross-file context it actually needs to verify a candidate finding.

This module owns the tool *schemas* and a per-review *executor* that
dispatches calls, caches results, and hard-caps total output. The agentic
loop itself lives in `engine.py:_review_chunk`.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass, field

from mira.index.context import SourceFetcher

logger = logging.getLogger(__name__)


READ_FILE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read a file from the repository at the PR head. Use this to verify "
            "cross-file claims (does function X exist? what does the caller pass?) "
            "before filing a comment. Prefer reading specific files over guessing. "
            "Returns the file content with line numbers."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Repo-relative path of the file to read, e.g. `src/auth/middleware.py`."
                    ),
                },
            },
            "required": ["path"],
        },
    },
}


GREP_REPO_TOOL = {
    "type": "function",
    "function": {
        "name": "grep_repo",
        "description": (
            "Search the repo for files whose path or content matches a pattern. "
            "Use this to find where a symbol is defined or called when you don't "
            "already know the file path. Returns up to 30 matching paths (path "
            "search) or up to 30 line-level hits (content search)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": (
                        "What to look for. Treated as a regex against file content "
                        "unless `path_only` is true. Keep it specific — short or "
                        "common patterns return too many matches and get capped."
                    ),
                },
                "path_glob": {
                    "type": ["string", "null"],
                    "description": (
                        "Optional glob to restrict the search, e.g. `**/*.java` "
                        "or `src/auth/*`. Defaults to all files."
                    ),
                },
                "path_only": {
                    "type": ["boolean", "null"],
                    "description": (
                        "If true, only match paths (don't read file contents). "
                        "Faster and cheaper. Use when you're hunting for a file "
                        "by name."
                    ),
                },
            },
            "required": ["pattern"],
        },
    },
}


# Hard caps to keep tool output from blowing up the prompt or the API budget.
_MAX_FILE_BYTES = 12_000  # ~3k tokens of source; truncate larger files
_MAX_GREP_HITS = 30
_MAX_GREP_BYTES_PER_HIT = 240
_MAX_TOTAL_OUTPUT_BYTES = 50_000  # all tool calls in one review combined


@dataclass
class AgenticToolExecutor:
    """Per-review tool dispatcher with caching and an output-size budget.

    Construct one per chunk review. The `repo_tree` is captured once
    (already fetched for JIT) so `grep_repo` doesn't keep re-listing the
    repo. The `source_fetcher` already has its own per-path cache, so
    repeated `read_file` calls don't re-hit GitHub.
    """

    source_fetcher: SourceFetcher
    repo_tree: list[str]
    bytes_used: int = 0
    _content_cache: dict[str, str | None] = field(default_factory=dict)

    async def execute(self, name: str, args: dict) -> str:
        """Dispatch a tool call. Returns the tool result as a string.

        Always returns *something* — errors are reported back to the LLM
        rather than raised, so the model can recover (e.g. by trying a
        different path). The string is what gets fed back into the next
        LLM hop as a `tool` message.
        """
        if self.bytes_used >= _MAX_TOTAL_OUTPUT_BYTES:
            return "[tool budget exhausted — no more tool calls accepted; submit your review now]"

        try:
            if name == "read_file":
                path = (args or {}).get("path") or ""
                if not path:
                    return "[error: missing `path` argument]"
                result = await self._read_file(path)
            elif name == "grep_repo":
                pattern = (args or {}).get("pattern") or ""
                if not pattern:
                    return "[error: missing `pattern` argument]"
                path_glob = (args or {}).get("path_glob") or None
                path_only = bool((args or {}).get("path_only") or False)
                result = await self._grep_repo(pattern, path_glob, path_only)
            else:
                return f"[error: unknown tool `{name}`]"
        except Exception as exc:
            logger.debug("Tool %s failed: %s", name, exc)
            return f"[error executing `{name}`: {exc}]"

        self.bytes_used += len(result)
        return result

    async def _read_file(self, path: str) -> str:
        # Don't waste tokens hunting for files we know don't exist.
        if self.repo_tree and path not in self.repo_tree:
            close = [p for p in self.repo_tree if path.lower() in p.lower()][:5]
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            return f"[file not found at PR head: `{path}`.{hint}]"

        if path in self._content_cache:
            content = self._content_cache[path]
        else:
            content = await self.source_fetcher.fetch(path)
            self._content_cache[path] = content

        if content is None:
            return f"[failed to read `{path}`]"

        truncated = False
        if len(content) > _MAX_FILE_BYTES:
            content = content[:_MAX_FILE_BYTES]
            truncated = True

        # Number lines so the LLM can cite them by number when filing comments.
        numbered = "\n".join(f"{i + 1:>5}  {line}" for i, line in enumerate(content.split("\n")))
        suffix = (
            f"\n... [truncated; file is longer than {_MAX_FILE_BYTES} bytes]" if truncated else ""
        )
        return f"`{path}`:\n```\n{numbered}{suffix}\n```"

    async def _grep_repo(self, pattern: str, path_glob: str | None, path_only: bool) -> str:
        if not self.repo_tree:
            return "[grep unavailable: repo tree not loaded]"

        candidates: list[str]
        if path_glob:
            candidates = [p for p in self.repo_tree if fnmatch.fnmatch(p, path_glob)]
        else:
            candidates = list(self.repo_tree)

        if path_only:
            try:
                rx = re.compile(pattern)
            except re.error:
                # Treat as substring on regex error.
                path_hits = [p for p in candidates if pattern in p][:_MAX_GREP_HITS]
            else:
                path_hits = [p for p in candidates if rx.search(p)][:_MAX_GREP_HITS]
            if not path_hits:
                return f"[no path matches for `{pattern}`]"
            return "Path matches:\n" + "\n".join(f"- `{p}`" for p in path_hits)

        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return f"[invalid regex `{pattern}`: {exc}]"

        # Skip giant binaries / lockfiles / vendored dirs we'd never want to read.
        skip_exts = (
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".pdf",
            ".zip",
            ".gz",
            ".tar",
            ".woff",
            ".woff2",
            ".ttf",
            ".eot",
            ".ico",
            ".svg",
            ".map",
            ".lock",
        )
        skip_dirs = ("node_modules/", "vendor/", "dist/", "build/", ".git/")

        hits: list[str] = []
        files_scanned = 0
        # Each scanned file is a network round-trip via source_fetcher; keep tight.
        max_files_to_scan = 15

        for cand in candidates:
            if len(hits) >= _MAX_GREP_HITS or files_scanned >= max_files_to_scan:
                break
            if any(cand.endswith(ext) for ext in skip_exts):
                continue
            if any(d in cand for d in skip_dirs):
                continue

            files_scanned += 1
            content = self._content_cache.get(cand)
            if content is None and cand not in self._content_cache:
                content = await self.source_fetcher.fetch(cand)
                self._content_cache[cand] = content
            if not content:
                continue

            for lineno, line in enumerate(content.split("\n"), start=1):
                if rx.search(line):
                    snippet = line.strip()
                    if len(snippet) > _MAX_GREP_BYTES_PER_HIT:
                        snippet = snippet[:_MAX_GREP_BYTES_PER_HIT] + "…"
                    hits.append(f"{cand}:{lineno}: {snippet}")
                    if len(hits) >= _MAX_GREP_HITS:
                        break

        if not hits:
            return f"[no content matches for `{pattern}` (scanned {files_scanned} files)]"
        prefix = f"Content matches (scanned {files_scanned} files):"
        if len(hits) == _MAX_GREP_HITS:
            prefix += f" showing first {_MAX_GREP_HITS}"
        return prefix + "\n" + "\n".join(hits)


AGENTIC_TOOLS = [READ_FILE_TOOL, GREP_REPO_TOOL]
