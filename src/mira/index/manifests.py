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


# ── Lockfile parsers ──
#
# Lockfiles record the *resolved* version of every transitive dependency,
# unlike manifests which only record the constraint declared by the human.
# Resolved versions are what OSV.dev should match against to know if the
# code is actually vulnerable — `^4.17.20` plus the lockfile resolving to
# `4.17.21` (patched) is a very different security posture than just
# trusting the constraint.


def parse_uv_lock(content: str, file_path: str) -> list[ParsedPackage]:
    """Parse a `uv.lock` (TOML) file. Records resolved versions for pip.

    Schema:
      [[package]]
      name = "..."
      version = "..."
    """
    if tomllib is None:
        return []
    try:
        data = tomllib.loads(content)
    except Exception as exc:
        logger.debug("Skipping %s (invalid TOML): %s", file_path, exc)
        return []
    out: list[ParsedPackage] = []
    for pkg in data.get("package") or []:
        if not isinstance(pkg, dict):
            continue
        name = pkg.get("name")
        version = pkg.get("version")
        if isinstance(name, str) and isinstance(version, str) and name:
            out.append(
                ParsedPackage(name=name, kind="pip", version=version, file_path=file_path)
            )
    return out


def parse_poetry_lock(content: str, file_path: str) -> list[ParsedPackage]:
    """Parse a `poetry.lock` (TOML) file. Same shape as uv.lock."""
    return parse_uv_lock(content, file_path)


def parse_package_lock_json(content: str, file_path: str) -> list[ParsedPackage]:
    """Parse a `package-lock.json` (npm v2/v3 lockfile).

    Modern npm lockfiles use a flat ``packages`` map keyed by relative path
    (e.g. ``"node_modules/lodash"``). Each entry has a concrete ``version``.
    The root entry has key ``""`` and we skip it (it's the project itself).
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        logger.debug("Skipping %s (invalid JSON): %s", file_path, exc)
        return []

    out: list[ParsedPackage] = []
    packages = data.get("packages")
    if isinstance(packages, dict):
        for path, info in packages.items():
            if not path or not isinstance(info, dict):
                continue
            # path is "node_modules/x" or "node_modules/x/node_modules/y"
            # — the package name is the last segment, accounting for scopes.
            parts = path.split("node_modules/")
            tail = parts[-1] if parts else ""
            if not tail:
                continue
            # Scoped packages: "@scope/name"
            if tail.startswith("@") and "/" in tail:
                segs = tail.split("/", 2)
                name = "/".join(segs[:2])
            else:
                name = tail.split("/", 1)[0]
            version = info.get("version")
            if not isinstance(version, str) or not version:
                continue
            is_dev = bool(info.get("dev"))
            out.append(
                ParsedPackage(
                    name=name, kind="npm", version=version, file_path=file_path, is_dev=is_dev,
                )
            )
        return out

    # Legacy npm v1 lockfile uses nested "dependencies" tree.
    deps = data.get("dependencies")
    if isinstance(deps, dict):
        for name, info in deps.items():
            if not isinstance(info, dict):
                continue
            version = info.get("version")
            if isinstance(version, str) and version:
                out.append(
                    ParsedPackage(name=name, kind="npm", version=version, file_path=file_path)
                )
    return out


# ── Dispatch ──

_PARSERS: list[tuple[re.Pattern[str], object]] = [
    # Lockfiles first — when both a manifest and a lockfile exist, the
    # lockfile entry's resolved version is what we want for vuln matching.
    (re.compile(r"(^|/)uv\.lock$"), parse_uv_lock),
    (re.compile(r"(^|/)poetry\.lock$"), parse_poetry_lock),
    (re.compile(r"(^|/)package-lock\.json$"), parse_package_lock_json),
    (re.compile(r"(^|/)package\.json$"), parse_package_json),
    (re.compile(r"(^|/)requirements[^/]*\.txt$"), parse_requirements_txt),
    (re.compile(r"(^|/)pyproject\.toml$"), parse_pyproject_toml),
    (re.compile(r"(^|/)go\.mod$"), parse_go_mod),
    (re.compile(r"(^|/)(Dockerfile|[^/]+\.Dockerfile)$"), parse_dockerfile),
]


def _is_lockfile_path(path: str) -> bool:
    """Heuristic — does this path look like a lockfile (resolved versions)?"""
    return bool(re.search(r"(^|/)(uv|poetry)\.lock$|(^|/)package-lock\.json$", path))


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
