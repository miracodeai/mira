"""Org-wide SQLite aggregation must cover GitLab (namespaced) repos too."""

from __future__ import annotations

import pytest

from mira.index.store import (
    IndexStore,
    _iter_repo_dbs,
    search_packages_org_wide_sqlite,
)

_PKG = [{"name": "lodash", "kind": "npm", "version": "4.17.20", "file_path": "package.json"}]


@pytest.fixture
def index_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    return tmp_path


def test_iter_finds_github_and_gitlab(index_dir):
    IndexStore.open("acme", "web").close()
    IndexStore.open("group/sub", "web", platform="gitlab").close()
    found = {(p, o, r) for p, o, r, _ in _iter_repo_dbs(str(index_dir))}
    assert ("github", "acme", "web") in found
    assert ("gitlab", "group/sub", "web") in found  # nested-group owner preserved


def test_package_search_includes_gitlab(index_dir):
    gh = IndexStore.open("acme", "web")
    gh.replace_manifest_packages("package.json", _PKG)
    gh.close()
    gl = IndexStore.open("grp", "app", platform="gitlab")
    gl.replace_manifest_packages("package.json", _PKG)
    gl.close()

    hits = search_packages_org_wide_sqlite(name="lodash")
    by_platform = {(h["platform"], h["owner"], h["repo"]) for h in hits}
    assert ("github", "acme", "web") in by_platform
    assert ("gitlab", "grp", "app") in by_platform
