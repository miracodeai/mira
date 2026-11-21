"""Tools the reviewer LLM can call during review (`read_file`, `grep_repo`).

On unindexed repos the tools cover what JIT pre-fetch can't reach
(Java/Go import resolution); on indexed repos they let the model trace
callers and dispatch points beyond the pre-fetched index context.

This module owns the tool *schemas* and a per-review *executor* that
dispatches calls, caches results, and hard-caps total output. The agentic
loop itself lives in ``passes.py:agentic_review_loop``.
"""

from __future__ import annotations

import asyncio
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
            "Returns the file content with line numbers. Large files are returned "
            "as a window: pass `start_line`/`end_line` to read a specific region "
            "(for example the function a grep hit pointed at)."
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
                "start_line": {
                    "type": ["integer", "null"],
                    "description": "First line to return (1-based). Defaults to the file start.",
                },
                "end_line": {
                    "type": ["integer", "null"],
                    "description": (
                        "Last line to return (inclusive). Defaults to as much as fits "
                        "in one window."
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
            "Use this to find where a symbol is defined, implemented or called "
            "when you don't already know the file path. Content search scans the "
            "files most related to the PR first (same directories, same language, "
            "names mentioning the pattern) and returns up to 30 line-level hits; "
            "path search returns up to 30 matching paths. Narrow with `path_glob` "
            "when a search comes back empty."
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
_MAX_FILE_BYTES = 12_000  # ~3k tokens of source per read_file window
_MAX_GREP_HITS = 30
_MAX_GREP_BYTES_PER_HIT = 240
_MAX_TOTAL_OUTPUT_BYTES = 80_000  # all tool calls in one review combined
# Content search fetches each candidate file (a network round-trip on live
# providers); the ranking below keeps the scanned set relevant, and fetches
# run concurrently so the cap costs a few seconds, not a few minutes.
_MAX_FILES_TO_SCAN = 40
_GREP_FETCH_CONCURRENCY = 8

_SKIP_EXTS = (
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
    ".min.js",
    ".min.css",
)
_SKIP_DIRS = ("node_modules/", "vendor/", "dist/", "build/", ".git/", "__pycache__/")
_TEST_HINTS = ("/test/", "/tests/", "/spec/", "/__tests__/", "_test.", "_spec.", ".test.", ".spec.")

# Identifier-ish tokens in a grep pattern; keywords that say nothing about
# *which* file holds the match are ignored when ranking by filename.
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")
_RANK_STOPWORDS = frozenset(
    {
        "implements",
        "extends",
        "import",
        "export",
        "return",
        "class",
        "interface",
        "struct",
        "function",
        "func",
        "const",
        "static",
        "public",
        "private",
        "protected",
        "self",
        "this",
        "async",
        "await",
        "null",
        "none",
        "true",
        "false",
        "void",
        "default",
        "new",
        "type",
        "module",
        "require",
        "from",
        "package",
    }
)


def _ext(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name[name.rfind(".") :].lower() if "." in name else ""


def _dir_segments(path: str) -> list[str]:
    return path.split("/")[:-1]


def base_scores(candidates: list[str], changed_paths: list[str]) -> dict[str, int]:
    """Pattern-independent part of the grep ranking, computed once per review.

    Directory proximity to the PR's changed files, matching file extension,
    and a small penalty for test paths.
    """
    changed_dirs = [_dir_segments(p) for p in changed_paths]
    changed_exts = {_ext(p) for p in changed_paths} - {""}

    def _proximity(path: str) -> int:
        segs = _dir_segments(path)
        best = 0
        for cd in changed_dirs:
            n = 0
            for a, b in zip(segs, cd, strict=False):
                if a != b:
                    break
                n += 1
            if n == len(cd) and n == len(segs):
                n += 1  # same directory beats a shared parent
            best = max(best, n)
        return best

    scores: dict[str, int] = {}
    for path in candidates:
        s = min(_proximity(path), 4)
        if _ext(path) in changed_exts:
            s += 1
        if any(h in path.lower() for h in _TEST_HINTS):
            s -= 1
        scores[path] = s
    return scores


def rank_for_grep(
    candidates: list[str],
    changed_paths: list[str],
    pattern: str,
    base: dict[str, int] | None = None,
) -> list[str]:
    """Order repo paths so the files most likely to hold the match are scanned first.

    Scores (higher first): a filename containing an identifier from the
    pattern, then the pattern-independent ``base_scores`` (directory
    proximity, extension, test penalty). Ties keep path order so results are
    deterministic.
    """
    tokens = {
        t.lower() for t in _IDENT_RE.findall(pattern or "") if t.lower() not in _RANK_STOPWORDS
    }
    if base is None:
        base = base_scores(candidates, changed_paths)

    def _score(path: str) -> int:
        s = base.get(path, 0)
        if tokens:
            low = path.lower()
            if any(t in low.rsplit("/", 1)[-1] for t in tokens):
                s += 6
            elif any(t in low for t in tokens):
                s += 2
        return s

    return sorted(candidates, key=lambda p: (-_score(p), p))


@dataclass
class AgenticToolExecutor:
    """Per-review tool dispatcher with caching and an output-size budget.

    Construct one per chunk review. The `repo_tree` is captured once
    (already fetched for JIT) so `grep_repo` doesn't keep re-listing the
    repo. The `source_fetcher` already has its own per-path cache, so
    repeated `read_file` calls don't re-hit GitHub. `changed_paths` (the
    PR's reviewed files) steers content search toward related code.
    """

    source_fetcher: SourceFetcher
    repo_tree: list[str]
    changed_paths: list[str] = field(default_factory=list)
    bytes_used: int = 0
    _content_cache: dict[str, str | None] = field(default_factory=dict)
    call_log: list[dict] = field(default_factory=list)
    # Skip-filtered tree and its pattern-independent ranking, built on first grep.
    _scannable: list[str] | None = field(default=None, repr=False)
    _base_scores: dict[str, int] | None = field(default=None, repr=False)

    async def execute(self, name: str, args: dict) -> str:
        """Dispatch a tool call. Returns the tool result as a string.

        Always returns *something* — errors are reported back to the LLM
        rather than raised, so the model can recover (e.g. by trying a
        different path). The string is what gets fed back into the next
        LLM hop as a `tool` message.
        """
        arg = (args or {}).get("path") or (args or {}).get("pattern") or ""
        self.call_log.append({"tool": name, "arg": arg})

        if self.bytes_used >= _MAX_TOTAL_OUTPUT_BYTES:
            return "[tool budget exhausted — no more tool calls accepted; submit your review now]"

        try:
            if name == "read_file":
                path = (args or {}).get("path") or ""
                if not path:
                    return "[error: missing `path` argument]"
                result = await self._read_file(
                    path,
                    _as_int((args or {}).get("start_line")),
                    _as_int((args or {}).get("end_line")),
                )
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

    async def _fetch_cached(self, path: str) -> str | None:
        if path in self._content_cache:
            return self._content_cache[path]
        content = await self.source_fetcher.fetch(path)
        self._content_cache[path] = content
        return content

    async def _read_file(
        self, path: str, start_line: int | None = None, end_line: int | None = None
    ) -> str:
        # Don't waste tokens hunting for files we know don't exist.
        if self.repo_tree and path not in self.repo_tree:
            close = [p for p in self.repo_tree if path.lower() in p.lower()][:5]
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            return f"[file not found at PR head: `{path}`.{hint}]"

        content = await self._fetch_cached(path)
        if content is None:
            return f"[failed to read `{path}`]"

        lines = content.split("\n")
        total = len(lines)
        first = max(1, start_line or 1)
        if first > total:
            return f"[`{path}` has {total} lines; `start_line` {first} is past the end]"
        last = min(total, end_line) if end_line else total
        if last < first:
            last = first

        # Number lines so the LLM can cite them by number when filing comments,
        # then fill the window until the byte cap. A capped window reports
        # where it stopped so the model can ask for the rest explicitly.
        numbered: list[str] = []
        used = 0
        shown_last = first - 1
        line_cut = False
        for i in range(first, last + 1):
            text = f"{i:>5}  {lines[i - 1]}"
            if len(text) > _MAX_FILE_BYTES:
                # A single minified/generated line can exceed the whole window.
                text = text[:_MAX_FILE_BYTES] + "…"
                line_cut = True
            if used + len(text) + 1 > _MAX_FILE_BYTES and numbered:
                break
            numbered.append(text)
            used += len(text) + 1
            shown_last = i
            if line_cut:
                break

        suffix = ""
        if shown_last < last or line_cut:
            suffix = (
                f"\n... [truncated: window capped at line {shown_last} of {total}; call "
                f"read_file with start_line={shown_last + 1} to continue]"
            )
        elif first > 1 or shown_last < total:
            suffix = f"\n... [showing lines {first}-{shown_last} of {total}]"
        return f"`{path}`:\n```\n" + "\n".join(numbered) + f"{suffix}\n```"

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

        # Skip giant binaries / lockfiles / vendored dirs we'd never want to read,
        # then scan the most relevant files first (see `rank_for_grep`).
        if self._scannable is None or self._base_scores is None:
            self._scannable = [
                p
                for p in self.repo_tree
                if not p.endswith(_SKIP_EXTS) and not any(d in p for d in _SKIP_DIRS)
            ]
            self._base_scores = base_scores(self._scannable, self.changed_paths)
        scannable = (
            [p for p in self._scannable if fnmatch.fnmatch(p, path_glob)]
            if path_glob
            else self._scannable
        )
        to_scan = rank_for_grep(scannable, self.changed_paths, pattern, self._base_scores)[
            :_MAX_FILES_TO_SCAN
        ]

        sem = asyncio.Semaphore(_GREP_FETCH_CONCURRENCY)

        async def _load(path: str) -> None:
            if path in self._content_cache:
                return
            async with sem:
                try:
                    content = await self.source_fetcher.fetch(path)
                except Exception as exc:  # one bad fetch must not sink the search
                    logger.debug("grep fetch failed for %s: %s", path, exc)
                    content = None
                self._content_cache[path] = content

        await asyncio.gather(*(_load(p) for p in to_scan))

        hits: list[str] = []
        files_scanned = 0
        for cand in to_scan:
            if len(hits) >= _MAX_GREP_HITS:
                break
            content = self._content_cache.get(cand)
            files_scanned += 1
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
            unscanned = len(scannable) - len(to_scan)
            more = (
                f"; {unscanned} more files not scanned — narrow with `path_glob`"
                if unscanned > 0
                else ""
            )
            return f"[no content matches for `{pattern}` (scanned {files_scanned} files{more})]"
        prefix = f"Content matches (scanned {files_scanned} files):"
        if len(hits) == _MAX_GREP_HITS:
            prefix += f" showing first {_MAX_GREP_HITS}"
        return prefix + "\n" + "\n".join(hits)


def _as_int(value: object) -> int | None:
    """Coerce a tool argument to a positive int, tolerating strings and junk."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            n = int(value)
        elif isinstance(value, str):
            n = int(value.strip())
        else:
            return None
    except ValueError:
        return None
    return n if n > 0 else None


AGENTIC_TOOLS = [READ_FILE_TOOL, GREP_REPO_TOOL]
