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

# Java: `import com.foo.bar.Baz;` or `import static com.foo.bar.Baz.method;`.
# We capture the dotted FQN; wildcard imports (`import com.foo.*`) are
# captured but yield no useful single-file candidate (we drop them downstream).
_JAVA_IMPORT_RE = re.compile(
    r"^\s*import\s+(?:static\s+)?([\w.]+(?:\.\*)?)\s*;",
    re.MULTILINE,
)

# Go imports come in two shapes:
#   import "single/path"
#   import (
#       "first"
#       aliased "second"
#   )
# A regex over the whole file is too broad (it matches any quoted string,
# which is everywhere in Go — struct tags, error messages, format strings).
# Instead we walk lines and only treat quoted strings as imports when we're
# inside an `import (` block or on an `import "..."` line.
_GO_SINGLE_IMPORT_RE = re.compile(r'^\s*import\s+(?:(?:[A-Za-z_]\w*|\.)\s+)?"([^"]+)"\s*$')
_GO_BLOCK_PATH_RE = re.compile(r'^\s*(?:(?:[A-Za-z_]\w*|\.)\s+)?"([^"]+)"\s*$')
_GO_MODULE_RE = re.compile(r"^module\s+(\S+)", re.MULTILINE)

# Standard-library Go packages: short single-word paths and well-known
# multi-word paths. Cheap heuristic to avoid even trying to resolve them.
_GO_STDLIB_HINTS = (
    "net/",
    "encoding/",
    "crypto/",
    "io/",
    "os/",
    "path/",
    "text/",
    "html/",
    "log/",
    "regexp/",
    "compress/",
    "container/",
    "database/",
    "debug/",
    "go/",
    "image/",
    "math/",
    "mime/",
    "runtime/",
    "sync/",
    "syscall/",
    "testing/",
    "unicode/",
    "archive/",
    "hash/",
    "index/",
    "reflect/",
    "sort/",
    "strconv/",
    "time/",
)

_EXT_TO_LANG = {
    "py": "python",
    "js": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "rb": "ruby",
    "java": "java",
    "go": "go",
}

_MAX_FILES = 8
_MAX_PER_FILE_CHARS = 1500
# Java and go.mod-less Go resolution is heuristic — picking the wrong file
# pollutes the prompt with off-topic symbols and hurts more than it helps.
# Cap at 1 candidate so we either find the right file or skip cleanly.
# (go.mod-resolved Go imports are deterministic and allowed 2 files.)
_MAX_CANDIDATES_PER_IMPORT = 1


def _extract_go_import_paths(source: str) -> list[str]:
    """Pull every imported package path from a Go source file.

    Handles both forms:
        import "foo"
        import (
            "first"
            aliased "second"
            // a comment
        )
    """
    paths: list[str] = []
    in_block = False
    for raw in source.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("import"):
            if "(" in line and ")" not in line:
                in_block = True
                continue
            m = _GO_SINGLE_IMPORT_RE.match(raw)
            if m:
                paths.append(m.group(1))
            continue
        if in_block:
            if line == ")":
                in_block = False
                continue
            m = _GO_BLOCK_PATH_RE.match(raw)
            if m:
                paths.append(m.group(1))
    return paths


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


def _candidates_java(fqn: str, repo_tree: set[str] | None) -> list[str]:
    """Resolve a Java FQN like `com.foo.bar.Baz` to candidate file paths.

    Java doesn't tell us where in the source tree a class lives — Maven and
    Gradle layouts vary, and a sub-project may have its own `src/main/java`.
    We use the class name from the FQN and ask the repo tree for any file
    ending in `/<ClassName>.java`. With a unique class name the answer is
    one or two files; with collisions we cap at 2.
    """
    if not fqn or fqn.endswith(".*") or repo_tree is None:
        return []
    parts = fqn.split(".")
    # `import static com.foo.Bar.method` — drop the trailing lowercase
    # method/field name so we resolve the enclosing class instead.
    while parts and parts[-1] and parts[-1][0].islower():
        parts = parts[:-1]
    if len(parts) < 2:
        return []
    class_name = parts[-1]
    if not class_name or not class_name[0].isupper():
        return []
    suffix = f"/{class_name}.java"
    matches = [p for p in repo_tree if p.endswith(suffix) or p == f"{class_name}.java"]
    # Prefer matches whose path also contains the package segment — that
    # disambiguates `Util.java` files when there are several. Sort: more
    # package-segment overlap first.
    pkg_path = ".".join(parts[:-1])
    matches.sort(key=lambda p: (-_path_overlap(p, pkg_path), len(p)))
    return matches[:_MAX_CANDIDATES_PER_IMPORT]


def _path_overlap(file_path: str, pkg_path: str) -> int:
    """How many segments of pkg_path appear in file_path, in order."""
    if not pkg_path:
        return 0
    fp_parts = file_path.split("/")
    pkg_parts = pkg_path.split(".")
    if not pkg_parts:
        return 0
    score = 0
    j = 0
    for seg in fp_parts:
        if j < len(pkg_parts) and seg == pkg_parts[j]:
            score += 1
            j += 1
    return score


def _parse_go_module(go_mod_source: str) -> str | None:
    """Pull the module path out of a go.mod file."""
    m = _GO_MODULE_RE.search(go_mod_source or "")
    return m.group(1) if m else None


def _candidates_go(
    import_path: str,
    repo_tree: set[str] | None,
    module: str | None = None,
) -> list[str]:
    """Resolve a Go import path like `github.com/grafana/grafana/pkg/x` to files.

    With ``module`` (parsed from go.mod) resolution is deterministic: an
    in-repo import is exactly `module + "/" + package_dir`, and anything
    else is an external dependency we can't fetch. Without it we fall back
    to a tail-match heuristic: take the path's last 2-3 segments and look
    for a directory in the repo whose path ends with them.
    """
    if not import_path or repo_tree is None:
        return []
    # Drop quotes / spaces just in case.
    p = import_path.strip().strip('"').strip()
    if not p or p.startswith("."):
        return []
    # Skip common stdlib paths so we don't burn cycles.
    if "/" not in p:
        return []  # single-segment imports are stdlib (`strings`, `fmt`)
    if p.startswith(_GO_STDLIB_HINTS):
        return []

    segs = p.split("/")

    if module:
        if p != module and not p.startswith(module + "/"):
            return []  # external dependency — not in this repo
        rel_dir = p[len(module) :].strip("/")
        prefix = f"{rel_dir}/" if rel_dir else ""
        files = [
            f
            for f in repo_tree
            if f.startswith(prefix)
            and "/" not in f[len(prefix) :]
            and f.endswith(".go")
            and not f.endswith("_test.go")
        ]
        # The file named after the package usually holds its core types.
        files.sort(key=lambda f: (f != f"{prefix}{segs[-1]}.go", f))
        return files[:2]
    # Try progressively longer tail matches: last 3 segments first, then 2.
    for tail_len in (3, 2):
        if tail_len > len(segs):
            continue
        suffix = "/".join(segs[-tail_len:])
        match_dir = f"/{suffix}/"
        files = [
            f
            for f in repo_tree
            if (match_dir in f"/{f}" or f"/{f}".endswith(f"/{suffix}"))
            and f.endswith(".go")
            and not f.endswith("_test.go")
        ]
        if files:
            files.sort(key=len)
            return files[:_MAX_CANDIDATES_PER_IMPORT]
    return []


def extract_import_candidates(
    source: str,
    language: str,
    source_path: str,
    repo_tree: set[str] | None = None,
    enable_java_go: bool = True,
    go_module: str | None = None,
) -> list[str]:
    """Return candidate file paths referenced by imports in ``source``.

    ``repo_tree`` is required for Java and Go (their imports don't encode
    enough information to reconstruct a path algorithmically — we have to
    look at what files actually exist). Optional for the other languages.

    ``enable_java_go`` lets the caller A/B disable the Java + Go resolvers
    without pulling them out of the dispatch table; their resolution is
    heuristic enough that it sometimes hurts on noisy repos.
    """
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
    elif language == "java" and enable_java_go:
        for m in _JAVA_IMPORT_RE.finditer(source):
            for c in _candidates_java(m.group(1), repo_tree):
                add(c)
    elif language == "go" and enable_java_go:
        for path in _extract_go_import_paths(source):
            for c in _candidates_go(path, repo_tree, module=go_module):
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
    enable_java_go: bool = True,
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

    # One go.mod fetch makes in-repo Go import resolution deterministic.
    go_module: str | None = None
    if (
        enable_java_go
        and any(_ext(f.path) == "go" for f in changed_files)
        and (repo_tree is None or "go.mod" in repo_tree)
    ):
        try:
            go_module = _parse_go_module(await source_fetcher.fetch("go.mod") or "")
        except Exception as exc:
            logger.debug("JIT: go.mod fetch failed: %s", exc)

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

        candidates = extract_import_candidates(
            source,
            lang,
            changed_file.path,
            repo_tree=repo_tree,
            enable_java_go=enable_java_go,
            go_module=go_module,
        )

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
