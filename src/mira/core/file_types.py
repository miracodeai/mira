"""Shared file-extension enum."""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePath


class Language(StrEnum):
    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    RUBY = "ruby"
    GO = "go"
    RUST = "rust"
    JAVA = "java"
    KOTLIN = "kotlin"
    CSHARP = "csharp"
    CPP = "cpp"
    C = "c"
    SWIFT = "swift"
    PHP = "php"
    SCALA = "scala"
    BASH = "bash"
    ZSH = "zsh"
    YAML = "yaml"
    JSON = "json"
    TOML = "toml"
    XML = "xml"
    HTML = "html"
    CSS = "css"
    SCSS = "scss"
    SQL = "sql"
    MARKDOWN = "markdown"
    R = "r"
    DART = "dart"
    LUA = "lua"
    ELIXIR = "elixir"
    ERLANG = "erlang"
    HASKELL = "haskell"
    OCAML = "ocaml"
    CLOJURE = "clojure"
    VIM = "vim"
    TERRAFORM = "terraform"
    GRAPHQL = "graphql"
    PROTOBUF = "protobuf"


EXTENSION_LANGUAGES: dict[str, Language] = {
    ".py": Language.PYTHON,
    ".js": Language.JAVASCRIPT,
    ".jsx": Language.JAVASCRIPT,
    ".ts": Language.TYPESCRIPT,
    ".tsx": Language.TYPESCRIPT,
    ".rb": Language.RUBY,
    ".go": Language.GO,
    ".rs": Language.RUST,
    ".java": Language.JAVA,
    ".kt": Language.KOTLIN,
    ".kts": Language.KOTLIN,
    ".cs": Language.CSHARP,
    ".cpp": Language.CPP,
    ".cc": Language.CPP,
    ".c": Language.C,
    ".h": Language.C,
    ".hpp": Language.CPP,
    ".swift": Language.SWIFT,
    ".php": Language.PHP,
    ".scala": Language.SCALA,
    ".sh": Language.BASH,
    ".bash": Language.BASH,
    ".zsh": Language.ZSH,
    ".yml": Language.YAML,
    ".yaml": Language.YAML,
    ".json": Language.JSON,
    ".toml": Language.TOML,
    ".xml": Language.XML,
    ".html": Language.HTML,
    ".css": Language.CSS,
    ".scss": Language.SCSS,
    ".sql": Language.SQL,
    ".md": Language.MARKDOWN,
    ".r": Language.R,
    ".dart": Language.DART,
    ".lua": Language.LUA,
    ".ex": Language.ELIXIR,
    ".exs": Language.ELIXIR,
    ".erl": Language.ERLANG,
    ".hs": Language.HASKELL,
    ".ml": Language.OCAML,
    ".clj": Language.CLOJURE,
    ".vim": Language.VIM,
    ".tf": Language.TERRAFORM,
    ".graphql": Language.GRAPHQL,
    ".proto": Language.PROTOBUF,
}

INDEXABLE_EXTENSIONS = frozenset(
    {
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
        ".lua",
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
)


def normalize_extension(extension: str) -> str:
    """Return a lowercase extension with one leading dot."""
    value = extension.strip().lower()
    if not value:
        return ""
    return value if value.startswith(".") else f".{value}"


def extension_from_path(path: str) -> str:
    """Return the normalized final suffix of a path."""
    return normalize_extension(PurePath(path).suffix)


def language_from_extension(extension: str, default: str = "") -> str:
    """Return the canonical language identifier for an extension."""
    language = EXTENSION_LANGUAGES.get(normalize_extension(extension))
    return language.value if language else default


def language_from_path(path: str, default: str = "") -> str:
    """Return the canonical language identifier for a path's final suffix."""
    return language_from_extension(extension_from_path(path), default=default)


def is_indexable_path(path: str) -> bool:
    """Whether a path has a source/config extension supported by indexing."""
    return extension_from_path(path) in INDEXABLE_EXTENSIONS
