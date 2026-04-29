"""Tests for deterministic manifest parsers."""

from __future__ import annotations

import json

import pytest

from mira.index.manifests import (
    is_manifest,
    parse_dockerfile,
    parse_go_mod,
    parse_manifest,
    parse_package_json,
    parse_pyproject_toml,
    parse_requirements_txt,
)


class TestPackageJson:
    def test_basic_dependencies(self):
        content = json.dumps(
            {
                "name": "my-app",
                "dependencies": {"express": "^4.18.0", "react": "18.2.0"},
                "devDependencies": {"jest": "^29.0.0"},
            }
        )
        pkgs = parse_package_json(content, "package.json")
        by_name = {p.name: p for p in pkgs}
        assert by_name["express"].version == "^4.18.0"
        assert by_name["express"].is_dev is False
        assert by_name["react"].version == "18.2.0"
        assert by_name["jest"].is_dev is True
        assert all(p.kind == "npm" for p in pkgs)

    def test_peer_and_optional(self):
        content = json.dumps(
            {
                "peerDependencies": {"react": ">=16"},
                "optionalDependencies": {"fsevents": "2.3.3"},
            }
        )
        pkgs = parse_package_json(content, "package.json")
        names = {p.name for p in pkgs}
        assert names == {"react", "fsevents"}

    def test_invalid_json_returns_empty(self):
        assert parse_package_json("{not-json", "package.json") == []

    def test_missing_dependency_blocks(self):
        content = json.dumps({"name": "x", "version": "1.0"})
        assert parse_package_json(content, "package.json") == []


class TestRequirementsTxt:
    def test_pinned_versions(self):
        content = "requests==2.31.0\ndjango>=4.2,<5.0\nnumpy ~= 1.26"
        pkgs = parse_requirements_txt(content, "requirements.txt")
        by_name = {p.name: p for p in pkgs}
        assert "requests" in by_name
        assert by_name["requests"].version == "==2.31.0"
        assert by_name["django"].version.startswith(">=")
        assert all(p.kind == "pip" for p in pkgs)

    def test_ignores_comments_and_options(self):
        content = (
            "# comment\n"
            "-r other.txt\n"
            "-e git+https://github.com/foo/bar.git#egg=bar\n"
            "./local-dep\n"
            "requests==2.31.0\n"
        )
        pkgs = parse_requirements_txt(content, "requirements.txt")
        assert len(pkgs) == 1
        assert pkgs[0].name == "requests"

    def test_strips_extras(self):
        content = "requests[security]==2.31.0\n"
        pkgs = parse_requirements_txt(content, "requirements.txt")
        assert pkgs[0].name == "requests"
        assert pkgs[0].version == "==2.31.0"

    def test_dev_file_marked_is_dev(self):
        content = "pytest==7.0\n"
        pkgs = parse_requirements_txt(content, "requirements-dev.txt")
        assert pkgs[0].is_dev is True


class TestPyprojectToml:
    def test_pep621_dependencies(self):
        content = """
[project]
name = "mira"
dependencies = ["requests>=2.31", "click==8.1.7"]

[project.optional-dependencies]
dev = ["pytest>=7.0"]
"""
        pkgs = parse_pyproject_toml(content, "pyproject.toml")
        by_name = {p.name: p for p in pkgs}
        assert by_name["requests"].version.startswith(">=")
        assert by_name["click"].version == "==8.1.7"
        assert by_name["pytest"].is_dev is True

    def test_poetry_dependencies(self):
        content = """
[tool.poetry.dependencies]
python = "^3.11"
requests = "^2.31.0"

[tool.poetry.group.dev.dependencies]
pytest = "^7.0"
"""
        pkgs = parse_pyproject_toml(content, "pyproject.toml")
        names = {p.name for p in pkgs}
        # python itself is filtered out
        assert "python" not in names
        assert "requests" in names
        dev_pkg = next(p for p in pkgs if p.name == "pytest")
        assert dev_pkg.is_dev is True


class TestGoMod:
    def test_require_block(self):
        content = """
module github.com/example/app

go 1.21

require (
    github.com/gin-gonic/gin v1.9.1
    github.com/spf13/cobra v1.7.0 // indirect
)
"""
        pkgs = parse_go_mod(content, "go.mod")
        by_name = {p.name: p for p in pkgs}
        assert by_name["github.com/gin-gonic/gin"].version == "v1.9.1"
        assert by_name["github.com/spf13/cobra"].is_dev is True  # indirect

    def test_single_line_require(self):
        content = "require github.com/foo/bar v0.1.0\n"
        pkgs = parse_go_mod(content, "go.mod")
        assert len(pkgs) == 1
        assert pkgs[0].name == "github.com/foo/bar"


class TestDockerfile:
    def test_simple_from(self):
        content = "FROM node:20.10-alpine\nRUN npm install"
        pkgs = parse_dockerfile(content, "Dockerfile")
        assert len(pkgs) == 1
        assert pkgs[0].name == "node"
        assert pkgs[0].version == "20.10-alpine"
        assert pkgs[0].kind == "docker"

    def test_multistage_from_as(self):
        content = "FROM python:3.11 AS builder\nFROM alpine:3.18\n"
        pkgs = parse_dockerfile(content, "Dockerfile")
        names = [p.name for p in pkgs]
        assert names == ["python", "alpine"]

    def test_unversioned_from(self):
        content = "FROM scratch\n"
        pkgs = parse_dockerfile(content, "Dockerfile")
        assert pkgs[0].name == "scratch"
        assert pkgs[0].version == ""


class TestDispatcher:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("package.json", True),
            ("frontend/package.json", True),
            ("requirements.txt", True),
            ("requirements-dev.txt", True),
            ("pyproject.toml", True),
            ("go.mod", True),
            ("Dockerfile", True),
            ("src/web.Dockerfile", True),
            ("README.md", False),
            ("src/main.py", False),
            ("package-lock.json", False),
        ],
    )
    def test_is_manifest(self, path, expected):
        assert is_manifest(path) is expected

    def test_parse_manifest_dispatches(self):
        pkgs = parse_manifest(
            "package.json",
            json.dumps({"dependencies": {"x": "1.0"}}),
        )
        assert len(pkgs) == 1
        assert pkgs[0].kind == "npm"

    def test_parse_manifest_unknown_returns_empty(self):
        assert parse_manifest("README.md", "# hi") == []

    def test_parse_manifest_swallows_parser_errors(self):
        # Pass invalid JSON to package.json parser
        assert parse_manifest("package.json", "this is not json") == []
