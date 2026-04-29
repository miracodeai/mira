"""Language-agnostic symbol extractor using heuristic patterns.

Finds functions/classes by name in Python, JS/TS, Go, Rust, Java.
Handles decorators, indentation-based and brace-based scoping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Languages that use indentation-based scoping
_INDENTATION_LANGUAGES = {"python", "py"}

# Languages that use brace-based scoping
_BRACE_LANGUAGES = {
    "javascript",
    "js",
    "typescript",
    "ts",
    "tsx",
    "jsx",
    "go",
    "rust",
    "rs",
    "java",
    "c",
    "cpp",
    "cs",
    "swift",
    "kotlin",
    "kt",
    "scala",
    "php",
}

# Python patterns
_PY_DEF = re.compile(r"^(\s*)(async\s+)?def\s+(\w+)")
_PY_CLASS = re.compile(r"^(\s*)class\s+(\w+)")
_PY_DECORATOR = re.compile(r"^\s*@")

# JS/TS patterns
_JS_FUNCTION = re.compile(r"^(\s*)(?:export\s+)?(?:async\s+)?function\s+(\w+)")
_JS_ARROW = re.compile(
    r"^(\s*)(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[^=])\s*=>"
)
_JS_CLASS = re.compile(r"^(\s*)(?:export\s+)?(?:default\s+)?class\s+(\w+)")
_JS_METHOD = re.compile(r"^(\s*)(?:async\s+)?(\w+)\s*\([^)]*\)\s*\{")

# Go patterns
_GO_FUNC = re.compile(r"^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\s*\(")
_GO_TYPE = re.compile(r"^type\s+(\w+)\s+struct\s*\{")

# Rust patterns
_RUST_FN = re.compile(r"^(\s*)(?:pub\s+)?(?:async\s+)?fn\s+(\w+)")
_RUST_STRUCT = re.compile(r"^(\s*)(?:pub\s+)?struct\s+(\w+)")
_RUST_IMPL = re.compile(r"^(\s*)impl(?:<[^>]*>)?\s+(\w+)")

# Java patterns
_JAVA_METHOD = re.compile(
    r"^(\s*)(?:public|private|protected)?\s*(?:static\s+)?(?:final\s+)?(?:synchronized\s+)?"
    r"(?:\w+(?:<[^>]*>)?(?:\[\])*\s+)(\w+)\s*\("
)
_JAVA_CLASS = re.compile(r"^(\s*)(?:public\s+)?(?:abstract\s+)?(?:final\s+)?class\s+(\w+)")


@dataclass
class SymbolSpan:
    """A located symbol extracted from source code."""

    name: str
    kind: str  # "function", "class", "method", "struct", "impl"
    start_line: int  # 1-based
    end_line: int  # 1-based, inclusive
    source: str


def extract_symbols(source: str, language: str) -> list[SymbolSpan]:
    """Extract all top-level symbols from source code.

    Args:
        source: The source code text.
        language: Language identifier (e.g. "python", "js", "go").

    Returns:
        List of extracted symbols with their source code spans.
    """
    if not source.strip():
        return []

    style = _detect_style(source, language)
    if style == "indentation":
        return _extract_indentation_based(source)
    return _extract_brace_based(source, language)


def find_symbol_by_name(source: str, language: str, name: str) -> SymbolSpan | None:
    """Find a specific symbol by name in source code."""
    for sym in extract_symbols(source, language):
        if sym.name == name:
            return sym
    return None


def _detect_style(source: str, language: str) -> str:
    """Detect whether to use indentation-based or brace-based extraction.

    Bug fix: checks for def/class keywords before defaulting to brace-based,
    so Python files with dict literals aren't misclassified.
    """
    lang = language.lower().strip()

    if lang in _INDENTATION_LANGUAGES:
        return "indentation"
    if lang in _BRACE_LANGUAGES:
        return "brace"

    # Unknown language — heuristic: check for Python-style def/class keywords
    lines = source.splitlines()
    has_def_class = any(_PY_DEF.match(line) or _PY_CLASS.match(line) for line in lines)
    if has_def_class:
        return "indentation"

    return "brace"


def _extract_indentation_based(source: str) -> list[SymbolSpan]:
    """Extract symbols from indentation-based languages (Python)."""
    lines = source.splitlines()
    symbols: list[SymbolSpan] = []

    i = 0
    while i < len(lines):
        line = lines[i]

        # Check for def or class
        def_match = _PY_DEF.match(line)
        cls_match = _PY_CLASS.match(line)
        match = def_match or cls_match

        if match:
            indent = len(match.group(1))
            if def_match:
                name = def_match.group(3)
                kind = "function" if indent == 0 else "method"
            else:
                assert cls_match is not None
                name = cls_match.group(2)
                kind = "class"

            # Look backward for decorators
            start = i
            while start > 0 and _PY_DECORATOR.match(lines[start - 1]):
                start -= 1

            # Find end of block by tracking indentation
            end = i + 1
            while end < len(lines):
                next_line = lines[end]
                # Skip empty lines
                if not next_line.strip():
                    end += 1
                    continue
                # Measure indentation
                next_indent = len(next_line) - len(next_line.lstrip())
                if next_indent <= indent:
                    break
                end += 1

            body = "\n".join(lines[start:end])
            symbols.append(
                SymbolSpan(
                    name=name,
                    kind=kind,
                    start_line=start + 1,
                    end_line=end,
                    source=body,
                )
            )

            # For classes, don't skip ahead — let methods inside be found too
            # But for top-level, skip to end to avoid re-processing inner methods
            if kind == "class":
                i += 1
            else:
                i = end
        else:
            i += 1

    return symbols


def _extract_brace_based(source: str, language: str) -> list[SymbolSpan]:
    """Extract symbols from brace-based languages."""
    lang = language.lower().strip()
    lines = source.splitlines()

    if lang in ("go",):
        return _extract_go(lines)
    if lang in ("rust", "rs"):
        return _extract_rust(lines)
    if lang in ("java",):
        return _extract_java(lines)
    # Default: JS/TS/C-like
    return _extract_js_ts(lines)


def _extract_js_ts(lines: list[str]) -> list[SymbolSpan]:
    """Extract symbols from JavaScript/TypeScript."""
    symbols: list[SymbolSpan] = []

    i = 0
    while i < len(lines):
        line = lines[i]

        # Check class
        cls_match = _JS_CLASS.match(line)
        if cls_match:
            end = _find_brace_end(lines, i)
            symbols.append(
                SymbolSpan(
                    name=cls_match.group(2),
                    kind="class",
                    start_line=i + 1,
                    end_line=end + 1,
                    source="\n".join(lines[i : end + 1]),
                )
            )
            i = end + 1
            continue

        # Check function declaration
        fn_match = _JS_FUNCTION.match(line)
        if fn_match:
            end = _find_brace_end(lines, i)
            symbols.append(
                SymbolSpan(
                    name=fn_match.group(2),
                    kind="function",
                    start_line=i + 1,
                    end_line=end + 1,
                    source="\n".join(lines[i : end + 1]),
                )
            )
            i = end + 1
            continue

        # Check arrow function
        arrow_match = _JS_ARROW.match(line)
        if arrow_match:
            end = _find_brace_end(lines, i)
            symbols.append(
                SymbolSpan(
                    name=arrow_match.group(2),
                    kind="function",
                    start_line=i + 1,
                    end_line=end + 1,
                    source="\n".join(lines[i : end + 1]),
                )
            )
            i = end + 1
            continue

        i += 1

    return symbols


def _extract_go(lines: list[str]) -> list[SymbolSpan]:
    """Extract symbols from Go."""
    symbols: list[SymbolSpan] = []

    i = 0
    while i < len(lines):
        line = lines[i]

        # Check func
        fn_match = _GO_FUNC.match(line)
        if fn_match:
            end = _find_brace_end(lines, i)
            symbols.append(
                SymbolSpan(
                    name=fn_match.group(1),
                    kind="function",
                    start_line=i + 1,
                    end_line=end + 1,
                    source="\n".join(lines[i : end + 1]),
                )
            )
            i = end + 1
            continue

        # Check type struct
        type_match = _GO_TYPE.match(line)
        if type_match:
            end = _find_brace_end(lines, i)
            symbols.append(
                SymbolSpan(
                    name=type_match.group(1),
                    kind="struct",
                    start_line=i + 1,
                    end_line=end + 1,
                    source="\n".join(lines[i : end + 1]),
                )
            )
            i = end + 1
            continue

        i += 1

    return symbols


def _extract_rust(lines: list[str]) -> list[SymbolSpan]:
    """Extract symbols from Rust."""
    symbols: list[SymbolSpan] = []

    i = 0
    while i < len(lines):
        line = lines[i]

        # Check impl
        impl_match = _RUST_IMPL.match(line)
        if impl_match:
            end = _find_brace_end(lines, i)
            symbols.append(
                SymbolSpan(
                    name=impl_match.group(2),
                    kind="impl",
                    start_line=i + 1,
                    end_line=end + 1,
                    source="\n".join(lines[i : end + 1]),
                )
            )
            i = end + 1
            continue

        # Check struct
        struct_match = _RUST_STRUCT.match(line)
        if struct_match:
            end = _find_brace_end(lines, i)
            symbols.append(
                SymbolSpan(
                    name=struct_match.group(2),
                    kind="struct",
                    start_line=i + 1,
                    end_line=end + 1,
                    source="\n".join(lines[i : end + 1]),
                )
            )
            i = end + 1
            continue

        # Check fn
        fn_match = _RUST_FN.match(line)
        if fn_match:
            end = _find_brace_end(lines, i)
            symbols.append(
                SymbolSpan(
                    name=fn_match.group(2),
                    kind="function",
                    start_line=i + 1,
                    end_line=end + 1,
                    source="\n".join(lines[i : end + 1]),
                )
            )
            i = end + 1
            continue

        i += 1

    return symbols


def _extract_java(lines: list[str]) -> list[SymbolSpan]:
    """Extract symbols from Java."""
    symbols: list[SymbolSpan] = []

    i = 0
    while i < len(lines):
        line = lines[i]

        # Check class
        cls_match = _JAVA_CLASS.match(line)
        if cls_match:
            end = _find_brace_end(lines, i)
            symbols.append(
                SymbolSpan(
                    name=cls_match.group(2),
                    kind="class",
                    start_line=i + 1,
                    end_line=end + 1,
                    source="\n".join(lines[i : end + 1]),
                )
            )
            i = end + 1
            continue

        # Check method
        method_match = _JAVA_METHOD.match(line)
        if method_match:
            end = _find_brace_end(lines, i)
            symbols.append(
                SymbolSpan(
                    name=method_match.group(2),
                    kind="method",
                    start_line=i + 1,
                    end_line=end + 1,
                    source="\n".join(lines[i : end + 1]),
                )
            )
            i = end + 1
            continue

        i += 1

    return symbols


def _find_brace_end(lines: list[str], start: int) -> int:
    """Find the line where braces balance out, starting from a given line.

    Scans from ``start`` counting ``{`` and ``}`` until the count returns to zero.
    Returns the line index of the closing brace. If braces never balance,
    returns the last line index.
    """
    depth = 0
    for i in range(start, len(lines)):
        line = lines[i]
        # Skip single-line comments and strings (rough heuristic)
        for ch in line:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
    return len(lines) - 1
