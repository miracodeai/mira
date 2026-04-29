"""Deterministic parsers for package manifest files.

Covers the common formats — package.json, requirements.txt, pyproject.toml,
go.mod, Dockerfile. Unlike LLM-based extraction, these parsers are precise,
zero-cost at inference time, and don't hallucinate versions. Each parser
returns a list of ``ParsedPackage`` entries; a dispatcher matches file paths
to the right parser.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


@dataclass(frozen=True)
class ParsedPackage:
    """A single dependency declared in a manifest file."""

    name: str
    kind: str  # "npm" | "pip" | "docker" | "go" | "rust"
    version: str  # raw constraint as written ("^4.18.0", ">=2.0", "4.18.0", etc.)
    file_path: str
    is_dev: bool = False


# ── package.json (npm, yarn, pnpm) ──


def parse_package_json(content: str, file_path: str) -> list[ParsedPackage]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.debug("Skipping %s (invalid JSON): %s", file_path, exc)
        return []

    out: list[ParsedPackage] = []
    for key, is_dev in (
        ("dependencies", False),
        ("devDependencies", True),
        ("peerDependencies", False),
        ("optionalDependencies", False),
    ):
        block = data.get(key) or {}
        if not isinstance(block, dict):
            continue
        for name, version in block.items():
            if not isinstance(name, str) or not isinstance(version, str):
                continue
            out.append(
                ParsedPackage(
                    name=name,
                    kind="npm",
                    version=version.strip(),
                    file_path=file_path,
                    is_dev=is_dev,
                )
            )
    return out


# ── requirements.txt (pip) ──

# Accepts lines like:
#   requests==2.31.0
#   django>=4.2,<5.0
#   numpy ~= 1.26
#   -e git+https://github.com/foo/bar.git@main#egg=bar
#   ./local-path  (ignored)
_PIP_SPEC = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9\-_.]*)\s*"
    r"([=<>!~]=?|===)?"
    r"\s*([^;#\s]*)",
)


def parse_requirements_txt(content: str, file_path: str) -> list[ParsedPackage]:
    out: list[ParsedPackage] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "-", ".", "/")):
            continue
        # Strip trailing comments
        if "#" in line:
            line = line.split("#", 1)[0].strip()
        # Strip extras, e.g. "requests[security]==2.31.0"
        line = re.sub(r"\[[^\]]*\]", "", line)
        m = _PIP_SPEC.match(line)
        if not m:
            continue
        name = m.group(1).strip()
        operator = m.group(2) or ""
        version = m.group(3).strip()
        constraint = f"{operator}{version}" if version else ""
        is_dev = "dev" in file_path.lower() or "test" in file_path.lower()
        out.append(
            ParsedPackage(
                name=name,
                kind="pip",
                version=constraint,
                file_path=file_path,
                is_dev=is_dev,
            )
        )
    return out


# ── pyproject.toml (PEP 621 + poetry) ──


def parse_pyproject_toml(content: str, file_path: str) -> list[ParsedPackage]:
    if tomllib is None:
        return []
    try:
        data = tomllib.loads(content)
    except Exception as exc:
        logger.debug("Skipping %s (invalid TOML): %s", file_path, exc)
        return []

    out: list[ParsedPackage] = []

    # PEP 621: [project].dependencies / [project.optional-dependencies]
    project = data.get("project") or {}
    for dep in project.get("dependencies") or []:
        if not isinstance(dep, str):
            continue
        name, version = _split_pep508(dep)
        if name:
            out.append(
                ParsedPackage(
                    name=name, kind="pip", version=version, file_path=file_path, is_dev=False
                )
            )
    optional = project.get("optional-dependencies") or {}
    for group, items in optional.items():
        if not isinstance(items, list):
            continue
        is_dev = group.lower() in ("dev", "test", "testing", "lint", "docs")
        for dep in items:
            if not isinstance(dep, str):
                continue
            name, version = _split_pep508(dep)
            if name:
                out.append(
                    ParsedPackage(
                        name=name, kind="pip", version=version, file_path=file_path, is_dev=is_dev
                    )
                )

    # Poetry: [tool.poetry.dependencies] / [tool.poetry.group.*.dependencies]
    poetry = (data.get("tool") or {}).get("poetry") or {}
    main_deps = poetry.get("dependencies") or {}
    if isinstance(main_deps, dict):
        for name, spec in main_deps.items():
            if name == "python" or not isinstance(name, str):
                continue
            version = (
                spec
                if isinstance(spec, str)
                else (spec.get("version", "") if isinstance(spec, dict) else "")
            )
            out.append(
                ParsedPackage(
                    name=name, kind="pip", version=str(version), file_path=file_path, is_dev=False
                )
            )
    groups = poetry.get("group") or {}
    if isinstance(groups, dict):
        for group_name, group_data in groups.items():
            is_dev = group_name.lower() in ("dev", "test", "lint", "docs")
            group_deps = (group_data or {}).get("dependencies") or {}
            if not isinstance(group_deps, dict):
                continue
            for name, spec in group_deps.items():
                version = (
                    spec
                    if isinstance(spec, str)
                    else (spec.get("version", "") if isinstance(spec, dict) else "")
                )
                out.append(
                    ParsedPackage(
                        name=name,
                        kind="pip",
                        version=str(version),
                        file_path=file_path,
                        is_dev=is_dev,
                    )
                )
    return out


def _split_pep508(spec: str) -> tuple[str, str]:
    """Split a PEP-508 spec like 'requests>=2.31.0; python_version>="3.8"'
    into (name, version_constraint)."""
    spec = spec.split(";", 1)[0].strip()
    spec = re.sub(r"\[[^\]]*\]", "", spec)
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9\-_.]*)\s*(.*)$", spec)
    if not m:
        return "", ""
    return m.group(1).strip(), m.group(2).strip()


# ── go.mod ──

_GO_REQUIRE = re.compile(r"^\s*([^\s]+)\s+([^\s]+)\s*$")


def parse_go_mod(content: str, file_path: str) -> list[ParsedPackage]:
    out: list[ParsedPackage] = []
    in_require_block = False
    for raw in content.splitlines():
        line = raw.strip()
        if line.startswith("//") or not line:
            continue
        if line.startswith("require ("):
            in_require_block = True
            continue
        if in_require_block and line == ")":
            in_require_block = False
            continue
        if line.startswith("require "):
            # Single-line form: require module/path v1.2.3
            m = _GO_REQUIRE.match(line[len("require ") :])
            if m:
                out.append(
                    ParsedPackage(
                        name=m.group(1),
                        kind="go",
                        version=m.group(2),
                        file_path=file_path,
                    )
                )
            continue
        if in_require_block:
            # Inside require (...) block: "path v1.2.3 // indirect"
            # Strip trailing comment first.
            clean = line.split("//", 1)[0].strip()
            m = _GO_REQUIRE.match(clean)
            if m:
                is_indirect = "// indirect" in line
                out.append(
                    ParsedPackage(
                        name=m.group(1),
                        kind="go",
                        version=m.group(2),
                        file_path=file_path,
                        is_dev=is_indirect,
                    )
                )
    return out


# ── Dockerfile ──

_DOCKER_FROM = re.compile(r"^\s*FROM\s+([^\s]+)(?:\s+AS\s+\S+)?\s*$", re.IGNORECASE)


def parse_dockerfile(content: str, file_path: str) -> list[ParsedPackage]:
    out: list[ParsedPackage] = []
    for raw in content.splitlines():
        m = _DOCKER_FROM.match(raw)
        if not m:
            continue
        image = m.group(1)
        if ":" in image and not image.startswith("$"):
            name, _, tag = image.rpartition(":")
            out.append(
                ParsedPackage(
                    name=name,
                    kind="docker",
                    version=tag,
                    file_path=file_path,
                )
            )
        else:
            out.append(
                ParsedPackage(
                    name=image,
                    kind="docker",
                    version="",
                    file_path=file_path,
                )
            )
    return out


# ── Dispatch ──

_PARSERS: list[tuple[re.Pattern[str], object]] = [
    (re.compile(r"(^|/)package\.json$"), parse_package_json),
    (re.compile(r"(^|/)requirements[^/]*\.txt$"), parse_requirements_txt),
    (re.compile(r"(^|/)pyproject\.toml$"), parse_pyproject_toml),
    (re.compile(r"(^|/)go\.mod$"), parse_go_mod),
    (re.compile(r"(^|/)(Dockerfile|[^/]+\.Dockerfile)$"), parse_dockerfile),
]


def is_manifest(path: str) -> bool:
    return any(p.search(path) for p, _ in _PARSERS)


def parse_manifest(path: str, content: str) -> list[ParsedPackage]:
    """Dispatch to the correct parser based on file path. Returns [] for
    unknown manifest types or parse failures."""
    for pattern, fn in _PARSERS:
        if pattern.search(path):
            try:
                return fn(content, path)  # type: ignore[misc]
            except Exception as exc:
                logger.warning("Parser failed on %s: %s", path, exc)
                return []
    return []
