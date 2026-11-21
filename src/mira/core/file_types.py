"""Shared file extension and language utilities.

Covers the source, config, and markup formats recognized by GitHub, GitLab,
and Forgejo. Documentation formats are labelled but excluded from indexing.
"""

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
    FSHARP = "fsharp"
    VISUAL_BASIC = "visual-basic"
    CPP = "cpp"
    C = "c"
    OBJECTIVE_C = "objective-c"
    OBJECTIVE_CPP = "objective-cpp"
    SWIFT = "swift"
    PHP = "php"
    SCALA = "scala"
    GROOVY = "groovy"
    DART = "dart"
    PERL = "perl"
    RAKU = "raku"
    LUA = "lua"
    JULIA = "julia"
    R = "r"
    D = "d"
    NIM = "nim"
    CRYSTAL = "crystal"
    ZIG = "zig"
    ODIN = "odin"
    HAXE = "haxe"
    VALA = "vala"
    GDSCRIPT = "gdscript"
    APEX = "apex"
    ABAP = "abap"
    PASCAL = "pascal"
    ADA = "ada"
    FORTRAN = "fortran"
    COBOL = "cobol"
    ASSEMBLY = "assembly"
    ELIXIR = "elixir"
    ERLANG = "erlang"
    HASKELL = "haskell"
    OCAML = "ocaml"
    CLOJURE = "clojure"
    ELM = "elm"
    PURESCRIPT = "purescript"
    REASON = "reason"
    RESCRIPT = "rescript"
    SCHEME = "scheme"
    RACKET = "racket"
    LISP = "lisp"
    EMACS_LISP = "emacs-lisp"
    PROLOG = "prolog"
    SMALLTALK = "smalltalk"
    FORTH = "forth"
    APL = "apl"
    LEAN = "lean"
    AGDA = "agda"
    IDRIS = "idris"
    TLA = "tla"
    ALLOY = "alloy"
    SMT = "smt"
    DAFNY = "dafny"
    FSTAR = "fstar"
    STANDARD_ML = "standard-ml"
    ISABELLE = "isabelle"
    BASH = "bash"
    ZSH = "zsh"
    CSH = "csh"
    FISH = "fish"
    POWERSHELL = "powershell"
    BATCH = "batch"
    AWK = "awk"
    SED = "sed"
    TCL = "tcl"
    CUDA = "cuda"
    GLSL = "glsl"
    HLSL = "hlsl"
    SHADERLAB = "shaderlab"
    METAL = "metal"
    WGSL = "wgsl"
    VERILOG = "verilog"
    SYSTEM_VERILOG = "system-verilog"
    VHDL = "vhdl"
    WEBASSEMBLY = "webassembly"
    LLVM = "llvm"
    SOLIDITY = "solidity"
    VYPER = "vyper"
    MOVE = "move"
    CAIRO = "cairo"
    YUL = "yul"
    HUFF = "huff"
    CIRCOM = "circom"
    NOIR = "noir"
    LEO = "leo"
    TACT = "tact"
    FUNC = "func"
    SWAY = "sway"
    COFFEESCRIPT = "coffeescript"
    LIVESCRIPT = "livescript"
    MOONSCRIPT = "moonscript"
    FENNEL = "fennel"
    HY = "hy"
    SAS = "sas"
    STATA = "stata"
    STAN = "stan"
    MATHEMATICA = "mathematica"
    HTML = "html"
    JSP = "jsp"
    ASP_NET = "asp-net"
    CSS = "css"
    SCSS = "scss"
    SASS = "sass"
    LESS = "less"
    STYLUS = "stylus"
    VUE = "vue"
    SVELTE = "svelte"
    ASTRO = "astro"
    RAZOR = "razor"
    JINJA = "jinja"
    HANDLEBARS = "handlebars"
    EJS = "ejs"
    ERB = "erb"
    HAML = "haml"
    SLIM = "slim"
    PUG = "pug"
    LIQUID = "liquid"
    TWIG = "twig"
    NUNJUCKS = "nunjucks"
    SMARTY = "smarty"
    GO_TEMPLATE = "go-template"
    YAML = "yaml"
    JSON = "json"
    TOML = "toml"
    XML = "xml"
    XQUERY = "xquery"
    SQL = "sql"
    GRAPHQL = "graphql"
    PROTOBUF = "protobuf"
    THRIFT = "thrift"
    AVRO = "avro"
    FLATBUFFERS = "flatbuffers"
    CAPN_PROTO = "capn-proto"
    SMITHY = "smithy"
    PRISMA = "prisma"
    DBML = "dbml"
    CYPHER = "cypher"
    HCL = "hcl"
    TERRAFORM = "terraform"
    NIX = "nix"
    DHALL = "dhall"
    JSONNET = "jsonnet"
    CUE = "cue"
    PKL = "pkl"
    REGO = "rego"
    SENTINEL = "sentinel"
    INI = "ini"
    DOTENV = "dotenv"
    DOCKERFILE = "dockerfile"
    MAKEFILE = "makefile"
    CMAKE = "cmake"
    MESON = "meson"
    STARLARK = "starlark"
    NINJA = "ninja"
    JUST = "just"
    VIM = "vim"
    MARKDOWN = "markdown"
    RST = "rst"
    ASCIIDOC = "asciidoc"
    ORG = "org"
    TEX = "tex"
    BIBTEX = "bibtex"
    TYPST = "typst"


EXTENSION_LANGUAGES: dict[str, Language] = {
    ".py": Language.PYTHON,
    ".pyw": Language.PYTHON,
    ".pyi": Language.PYTHON,
    ".pyx": Language.PYTHON,
    ".pxd": Language.PYTHON,
    ".pxi": Language.PYTHON,
    ".sage": Language.PYTHON,
    ".js": Language.JAVASCRIPT,
    ".jsx": Language.JAVASCRIPT,
    ".mjs": Language.JAVASCRIPT,
    ".cjs": Language.JAVASCRIPT,
    ".ts": Language.TYPESCRIPT,
    ".tsx": Language.TYPESCRIPT,
    ".mts": Language.TYPESCRIPT,
    ".cts": Language.TYPESCRIPT,
    ".rb": Language.RUBY,
    ".rbw": Language.RUBY,
    ".rake": Language.RUBY,
    ".gemspec": Language.RUBY,
    ".ru": Language.RUBY,
    ".podspec": Language.RUBY,
    ".go": Language.GO,
    ".rs": Language.RUST,
    ".java": Language.JAVA,
    ".kt": Language.KOTLIN,
    ".kts": Language.KOTLIN,
    ".cs": Language.CSHARP,
    ".csx": Language.CSHARP,
    ".fs": Language.FSHARP,
    ".fsi": Language.FSHARP,
    ".fsx": Language.FSHARP,
    ".fsscript": Language.FSHARP,
    ".vb": Language.VISUAL_BASIC,
    ".vbs": Language.VISUAL_BASIC,
    ".bas": Language.VISUAL_BASIC,
    ".frm": Language.VISUAL_BASIC,
    ".vba": Language.VISUAL_BASIC,
    ".cpp": Language.CPP,
    ".cc": Language.CPP,
    ".cxx": Language.CPP,
    ".c++": Language.CPP,
    ".hpp": Language.CPP,
    ".hh": Language.CPP,
    ".hxx": Language.CPP,
    ".h++": Language.CPP,
    ".ipp": Language.CPP,
    ".tpp": Language.CPP,
    ".ixx": Language.CPP,
    ".cppm": Language.CPP,
    ".mxx": Language.CPP,
    ".inl": Language.CPP,
    ".c": Language.C,
    ".h": Language.C,
    ".m": Language.OBJECTIVE_C,
    ".mm": Language.OBJECTIVE_CPP,
    ".swift": Language.SWIFT,
    ".php": Language.PHP,
    ".php3": Language.PHP,
    ".php4": Language.PHP,
    ".php5": Language.PHP,
    ".php7": Language.PHP,
    ".php8": Language.PHP,
    ".phtml": Language.PHP,
    ".scala": Language.SCALA,
    ".sc": Language.SCALA,
    ".sbt": Language.SCALA,
    ".groovy": Language.GROOVY,
    ".gvy": Language.GROOVY,
    ".gy": Language.GROOVY,
    ".gsh": Language.GROOVY,
    ".gradle": Language.GROOVY,
    ".dart": Language.DART,
    ".pl": Language.PERL,
    ".pm": Language.PERL,
    ".t": Language.PERL,
    ".perl": Language.PERL,
    ".raku": Language.RAKU,
    ".rakumod": Language.RAKU,
    ".rakutest": Language.RAKU,
    ".rakudoc": Language.RAKU,
    ".p6": Language.RAKU,
    ".pm6": Language.RAKU,
    ".pl6": Language.RAKU,
    ".p6l": Language.RAKU,
    ".p6m": Language.RAKU,
    ".nqp": Language.RAKU,
    ".lua": Language.LUA,
    ".wlua": Language.LUA,
    ".luau": Language.LUA,
    ".jl": Language.JULIA,
    ".r": Language.R,
    ".rmd": Language.R,
    ".d": Language.D,
    ".di": Language.D,
    ".nim": Language.NIM,
    ".nimrod": Language.NIM,
    ".nims": Language.NIM,
    ".nimble": Language.NIM,
    ".cr": Language.CRYSTAL,
    ".zig": Language.ZIG,
    ".odin": Language.ODIN,
    ".hx": Language.HAXE,
    ".hxml": Language.HAXE,
    ".vala": Language.VALA,
    ".vapi": Language.VALA,
    ".gd": Language.GDSCRIPT,
    ".cls": Language.APEX,
    ".trigger": Language.APEX,
    ".abap": Language.ABAP,
    ".pas": Language.PASCAL,
    ".dpr": Language.PASCAL,
    ".lpr": Language.PASCAL,
    ".adb": Language.ADA,
    ".ads": Language.ADA,
    ".ada": Language.ADA,
    ".f": Language.FORTRAN,
    ".for": Language.FORTRAN,
    ".f77": Language.FORTRAN,
    ".f90": Language.FORTRAN,
    ".f95": Language.FORTRAN,
    ".f03": Language.FORTRAN,
    ".f08": Language.FORTRAN,
    ".ftn": Language.FORTRAN,
    ".fpp": Language.FORTRAN,
    ".cob": Language.COBOL,
    ".cbl": Language.COBOL,
    ".cpy": Language.COBOL,
    ".asm": Language.ASSEMBLY,
    ".s": Language.ASSEMBLY,
    ".nasm": Language.ASSEMBLY,
    ".ex": Language.ELIXIR,
    ".exs": Language.ELIXIR,
    ".eex": Language.ELIXIR,
    ".leex": Language.ELIXIR,
    ".heex": Language.ELIXIR,
    ".erl": Language.ERLANG,
    ".hrl": Language.ERLANG,
    ".es": Language.ERLANG,
    ".escript": Language.ERLANG,
    ".hs": Language.HASKELL,
    ".lhs": Language.HASKELL,
    ".ml": Language.OCAML,
    ".mli": Language.OCAML,
    ".mll": Language.OCAML,
    ".mly": Language.OCAML,
    ".clj": Language.CLOJURE,
    ".cljs": Language.CLOJURE,
    ".cljc": Language.CLOJURE,
    ".edn": Language.CLOJURE,
    ".elm": Language.ELM,
    ".purs": Language.PURESCRIPT,
    ".re": Language.REASON,
    ".rei": Language.REASON,
    ".res": Language.RESCRIPT,
    ".resi": Language.RESCRIPT,
    ".scm": Language.SCHEME,
    ".ss": Language.SCHEME,
    ".rkt": Language.RACKET,
    ".rktd": Language.RACKET,
    ".rktl": Language.RACKET,
    ".lisp": Language.LISP,
    ".lsp": Language.LISP,
    ".cl": Language.LISP,
    ".el": Language.EMACS_LISP,
    ".pro": Language.PROLOG,
    ".prolog": Language.PROLOG,
    ".st": Language.SMALLTALK,
    ".frt": Language.FORTH,
    ".fth": Language.FORTH,
    ".4th": Language.FORTH,
    ".forth": Language.FORTH,
    ".apl": Language.APL,
    ".aplf": Language.APL,
    ".aplo": Language.APL,
    ".apln": Language.APL,
    ".aplc": Language.APL,
    ".apli": Language.APL,
    ".dyalog": Language.APL,
    ".lean": Language.LEAN,
    ".agda": Language.AGDA,
    ".lagda": Language.AGDA,
    ".idr": Language.IDRIS,
    ".lidr": Language.IDRIS,
    ".tla": Language.TLA,
    ".als": Language.ALLOY,
    ".smt": Language.SMT,
    ".smt2": Language.SMT,
    ".dfy": Language.DAFNY,
    ".fst": Language.FSTAR,
    ".fsti": Language.FSTAR,
    ".sml": Language.STANDARD_ML,
    ".fun": Language.STANDARD_ML,
    ".thy": Language.ISABELLE,
    ".sh": Language.BASH,
    ".bash": Language.BASH,
    ".ksh": Language.BASH,
    ".ebuild": Language.BASH,
    ".eclass": Language.BASH,
    ".zsh": Language.ZSH,
    ".csh": Language.CSH,
    ".tcsh": Language.CSH,
    ".fish": Language.FISH,
    ".ps1": Language.POWERSHELL,
    ".psm1": Language.POWERSHELL,
    ".psd1": Language.POWERSHELL,
    ".bat": Language.BATCH,
    ".cmd": Language.BATCH,
    ".awk": Language.AWK,
    ".sed": Language.SED,
    ".tcl": Language.TCL,
    ".tk": Language.TCL,
    ".cu": Language.CUDA,
    ".cuh": Language.CUDA,
    ".glsl": Language.GLSL,
    ".vert": Language.GLSL,
    ".frag": Language.GLSL,
    ".geom": Language.GLSL,
    ".tesc": Language.GLSL,
    ".tese": Language.GLSL,
    ".comp": Language.GLSL,
    ".hlsl": Language.HLSL,
    ".hlsli": Language.HLSL,
    ".shader": Language.SHADERLAB,
    ".metal": Language.METAL,
    ".wgsl": Language.WGSL,
    ".v": Language.VERILOG,
    ".vh": Language.VERILOG,
    ".sv": Language.SYSTEM_VERILOG,
    ".svh": Language.SYSTEM_VERILOG,
    ".vhd": Language.VHDL,
    ".vhdl": Language.VHDL,
    ".wat": Language.WEBASSEMBLY,
    ".wast": Language.WEBASSEMBLY,
    ".ll": Language.LLVM,
    ".sol": Language.SOLIDITY,
    ".vy": Language.VYPER,
    ".move": Language.MOVE,
    ".cairo": Language.CAIRO,
    ".yul": Language.YUL,
    ".huff": Language.HUFF,
    ".circom": Language.CIRCOM,
    ".nr": Language.NOIR,
    ".leo": Language.LEO,
    ".tact": Language.TACT,
    ".fc": Language.FUNC,
    ".func": Language.FUNC,
    ".sway": Language.SWAY,
    ".coffee": Language.COFFEESCRIPT,
    ".litcoffee": Language.COFFEESCRIPT,
    ".ls": Language.LIVESCRIPT,
    ".moon": Language.MOONSCRIPT,
    ".fnl": Language.FENNEL,
    ".hy": Language.HY,
    ".sas": Language.SAS,
    ".do": Language.STATA,
    ".ado": Language.STATA,
    ".stan": Language.STAN,
    ".wl": Language.MATHEMATICA,
    ".wls": Language.MATHEMATICA,
    ".nb": Language.MATHEMATICA,
    ".html": Language.HTML,
    ".htm": Language.HTML,
    ".xhtml": Language.HTML,
    ".jsp": Language.JSP,
    ".jspx": Language.JSP,
    ".jspf": Language.JSP,
    ".aspx": Language.ASP_NET,
    ".ascx": Language.ASP_NET,
    ".ashx": Language.ASP_NET,
    ".asmx": Language.ASP_NET,
    ".asax": Language.ASP_NET,
    ".axd": Language.ASP_NET,
    ".css": Language.CSS,
    ".scss": Language.SCSS,
    ".sass": Language.SASS,
    ".less": Language.LESS,
    ".styl": Language.STYLUS,
    ".vue": Language.VUE,
    ".svelte": Language.SVELTE,
    ".astro": Language.ASTRO,
    ".razor": Language.RAZOR,
    ".cshtml": Language.RAZOR,
    ".j2": Language.JINJA,
    ".jinja": Language.JINJA,
    ".jinja2": Language.JINJA,
    ".hbs": Language.HANDLEBARS,
    ".handlebars": Language.HANDLEBARS,
    ".mustache": Language.HANDLEBARS,
    ".ejs": Language.EJS,
    ".erb": Language.ERB,
    ".rhtml": Language.ERB,
    ".haml": Language.HAML,
    ".slim": Language.SLIM,
    ".pug": Language.PUG,
    ".jade": Language.PUG,
    ".liquid": Language.LIQUID,
    ".twig": Language.TWIG,
    ".njk": Language.NUNJUCKS,
    ".tpl": Language.SMARTY,
    ".tmpl": Language.GO_TEMPLATE,
    ".gotmpl": Language.GO_TEMPLATE,
    ".yml": Language.YAML,
    ".yaml": Language.YAML,
    ".sls": Language.YAML,
    ".json": Language.JSON,
    ".json5": Language.JSON,
    ".jsonc": Language.JSON,
    ".jsonl": Language.JSON,
    ".ndjson": Language.JSON,
    ".jsonld": Language.JSON,
    ".geojson": Language.JSON,
    ".topojson": Language.JSON,
    ".har": Language.JSON,
    ".avsc": Language.JSON,
    ".toml": Language.TOML,
    ".xml": Language.XML,
    ".xsl": Language.XML,
    ".xslt": Language.XML,
    ".xsd": Language.XML,
    ".dtd": Language.XML,
    ".rss": Language.XML,
    ".atom": Language.XML,
    ".plist": Language.XML,
    ".csproj": Language.XML,
    ".fsproj": Language.XML,
    ".vbproj": Language.XML,
    ".vcxproj": Language.XML,
    ".props": Language.XML,
    ".targets": Language.XML,
    ".nuspec": Language.XML,
    ".pom": Language.XML,
    ".xaml": Language.XML,
    ".axml": Language.XML,
    ".resx": Language.XML,
    ".wsdl": Language.XML,
    ".xbrl": Language.XML,
    ".config": Language.XML,
    ".xq": Language.XQUERY,
    ".xql": Language.XQUERY,
    ".xqm": Language.XQUERY,
    ".xquery": Language.XQUERY,
    ".xqy": Language.XQUERY,
    ".sql": Language.SQL,
    ".psql": Language.SQL,
    ".pgsql": Language.SQL,
    ".plsql": Language.SQL,
    ".pls": Language.SQL,
    ".pks": Language.SQL,
    ".pkb": Language.SQL,
    ".prc": Language.SQL,
    ".trg": Language.SQL,
    ".mysql": Language.SQL,
    ".tsql": Language.SQL,
    ".hql": Language.SQL,
    ".cql": Language.SQL,
    ".graphql": Language.GRAPHQL,
    ".gql": Language.GRAPHQL,
    ".graphqls": Language.GRAPHQL,
    ".proto": Language.PROTOBUF,
    ".thrift": Language.THRIFT,
    ".avdl": Language.AVRO,
    ".fbs": Language.FLATBUFFERS,
    ".capnp": Language.CAPN_PROTO,
    ".smithy": Language.SMITHY,
    ".prisma": Language.PRISMA,
    ".dbml": Language.DBML,
    ".cyp": Language.CYPHER,
    ".cypher": Language.CYPHER,
    ".hcl": Language.HCL,
    ".nomad": Language.HCL,
    ".tf": Language.TERRAFORM,
    ".tfvars": Language.TERRAFORM,
    ".nix": Language.NIX,
    ".dhall": Language.DHALL,
    ".jsonnet": Language.JSONNET,
    ".libsonnet": Language.JSONNET,
    ".cue": Language.CUE,
    ".pkl": Language.PKL,
    ".rego": Language.REGO,
    ".sentinel": Language.SENTINEL,
    ".ini": Language.INI,
    ".cfg": Language.INI,
    ".conf": Language.INI,
    ".cnf": Language.INI,
    ".inf": Language.INI,
    ".properties": Language.INI,
    ".desktop": Language.INI,
    ".service": Language.INI,
    ".socket": Language.INI,
    ".timer": Language.INI,
    ".target": Language.INI,
    ".mount": Language.INI,
    ".path": Language.INI,
    ".slice": Language.INI,
    ".scope": Language.INI,
    ".env": Language.DOTENV,
    ".dockerfile": Language.DOCKERFILE,
    ".mk": Language.MAKEFILE,
    ".mak": Language.MAKEFILE,
    ".make": Language.MAKEFILE,
    ".cmake": Language.CMAKE,
    ".meson": Language.MESON,
    ".bzl": Language.STARLARK,
    ".bazel": Language.STARLARK,
    ".star": Language.STARLARK,
    ".ninja": Language.NINJA,
    ".just": Language.JUST,
    ".vim": Language.VIM,
    ".md": Language.MARKDOWN,
    ".markdown": Language.MARKDOWN,
    ".mdx": Language.MARKDOWN,
    ".mdown": Language.MARKDOWN,
    ".mkdn": Language.MARKDOWN,
    ".rst": Language.RST,
    ".rest": Language.RST,
    ".adoc": Language.ASCIIDOC,
    ".asciidoc": Language.ASCIIDOC,
    ".org": Language.ORG,
    ".tex": Language.TEX,
    ".latex": Language.TEX,
    ".ltx": Language.TEX,
    ".sty": Language.TEX,
    ".bib": Language.BIBTEX,
    ".typ": Language.TYPST,
}

# Language detection for well-known extensionless files. Keys are lowercase
# so lookups stay case-insensitive.
FILENAME_LANGUAGES: dict[str, Language] = {
    ".bash_aliases": Language.BASH,
    ".bash_login": Language.BASH,
    ".bash_logout": Language.BASH,
    ".bash_profile": Language.BASH,
    ".bashrc": Language.BASH,
    ".editorconfig": Language.INI,
    ".env": Language.DOTENV,
    ".gvimrc": Language.VIM,
    ".profile": Language.BASH,
    ".vimrc": Language.VIM,
    ".zlogin": Language.ZSH,
    ".zlogout": Language.ZSH,
    ".zprofile": Language.ZSH,
    ".zshenv": Language.ZSH,
    ".zshrc": Language.ZSH,
    "appfile": Language.RUBY,
    "brewfile": Language.RUBY,
    "bsdmakefile": Language.MAKEFILE,
    "buck": Language.STARLARK,
    "build": Language.STARLARK,
    "build.bazel": Language.STARLARK,
    "capfile": Language.RUBY,
    "cmakelists.txt": Language.CMAKE,
    "containerfile": Language.DOCKERFILE,
    "dockerfile": Language.DOCKERFILE,
    "fastfile": Language.RUBY,
    "gemfile": Language.RUBY,
    "gnumakefile": Language.MAKEFILE,
    "guardfile": Language.RUBY,
    "jenkinsfile": Language.GROOVY,
    "justfile": Language.JUST,
    "makefile": Language.MAKEFILE,
    "meson.build": Language.MESON,
    "meson_options.txt": Language.MESON,
    "module.bazel": Language.STARLARK,
    "pipfile": Language.TOML,
    "podfile": Language.RUBY,
    "rakefile": Language.RUBY,
    "sconscript": Language.PYTHON,
    "sconstruct": Language.PYTHON,
    "snakefile": Language.PYTHON,
    "thorfile": Language.RUBY,
    "vagrantfile": Language.RUBY,
    "workspace": Language.STARLARK,
    "workspace.bazel": Language.STARLARK,
    "wscript": Language.PYTHON,
}

# Suffixed variants such as Dockerfile.dev and Makefile.am.
_FILENAME_PREFIX_LANGUAGES: tuple[tuple[str, Language], ...] = (
    ("containerfile.", Language.DOCKERFILE),
    ("dockerfile.", Language.DOCKERFILE),
    ("makefile.", Language.MAKEFILE),
)

# Prose formats add little summary value for their token cost, and dotenv
# files commonly hold secrets that should not reach the summarizer.
_NON_INDEXABLE_LANGUAGES: frozenset[Language] = frozenset(
    {
        Language.ASCIIDOC,
        Language.BIBTEX,
        Language.DOTENV,
        Language.MARKDOWN,
        Language.ORG,
        Language.RST,
        Language.TEX,
        Language.TYPST,
    }
)

INDEXABLE_LANGUAGES: frozenset[str] = frozenset(
    (
        set(EXTENSION_LANGUAGES.values())
        | set(FILENAME_LANGUAGES.values())
        | {language for _, language in _FILENAME_PREFIX_LANGUAGES}
    )
    - _NON_INDEXABLE_LANGUAGES
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


def _language_for_path(path: str) -> Language | None:
    """Return the language for a path, preferring its filename over its suffix."""
    filename = PurePath(path).name.lower()
    language = FILENAME_LANGUAGES.get(filename)
    if language is not None:
        return language
    for prefix, prefixed_language in _FILENAME_PREFIX_LANGUAGES:
        if filename.startswith(prefix):
            return prefixed_language
    return EXTENSION_LANGUAGES.get(extension_from_path(path))


def language_from_extension(extension: str, default: str = "") -> str:
    """Return the canonical language identifier for an extension."""
    language = EXTENSION_LANGUAGES.get(normalize_extension(extension))
    return language.value if language else default


def language_from_path(path: str, default: str = "") -> str:
    """Return the canonical language identifier for a path."""
    language = _language_for_path(path)
    return language.value if language else default


def is_indexable_path(path: str) -> bool:
    """Whether a path has a source/config extension supported by indexing."""
    language = _language_for_path(path)
    return language in INDEXABLE_LANGUAGES
