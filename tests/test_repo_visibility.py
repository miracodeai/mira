"""Repo visibility round-trips through the registry — backs the blast-radius
privacy filter, which relies on RepoRecord.private to keep private repo names
out of a public repo's review."""

from __future__ import annotations

from pathlib import Path

import pytest

from mira.core.engine import filter_blast_radius_for_visibility
from mira.dashboard.db import AppDatabase


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    return AppDatabase(url="", admin_password="admin")


def test_new_repo_visibility_is_unknown(db: AppDatabase):
    # Until a sync/PR/install records it, visibility is unknown (None), which
    # the blast-radius filter treats as private.
    db.register_repo("acme", "web", installation_id=1)
    assert db.get_repo("acme", "web").private is None


def test_set_visibility_public(db: AppDatabase):
    db.register_repo("acme", "web", installation_id=1)
    db.set_repo_visibility("acme", "web", False)
    assert db.get_repo("acme", "web").private is False


def test_set_visibility_private(db: AppDatabase):
    db.register_repo("acme", "secret", installation_id=1)
    db.set_repo_visibility("acme", "secret", True)
    assert db.get_repo("acme", "secret").private is True


def test_set_visibility_flips_back(db: AppDatabase):
    db.register_repo("acme", "web", installation_id=1)
    db.set_repo_visibility("acme", "web", True)
    db.set_repo_visibility("acme", "web", False)
    assert db.get_repo("acme", "web").private is False


def test_visibility_survives_register_repo(db: AppDatabase):
    # A later register_repo (e.g. re-sync) must not clobber a known visibility.
    db.register_repo("acme", "secret", installation_id=1)
    db.set_repo_visibility("acme", "secret", True)
    db.register_repo("acme", "secret", installation_id=2)
    assert db.get_repo("acme", "secret").private is True


def test_set_visibility_on_unknown_repo_is_noop(db: AppDatabase):
    db.set_repo_visibility("acme", "ghost", True)  # never registered
    assert db.get_repo("acme", "ghost") is None


def test_list_repos_carries_visibility(db: AppDatabase):
    db.register_repo("acme", "web", installation_id=1)
    db.register_repo("acme", "secret", installation_id=1)
    db.register_repo("acme", "unsynced", installation_id=1)
    db.set_repo_visibility("acme", "web", False)
    db.set_repo_visibility("acme", "secret", True)
    by_name = {r.repo: r.private for r in db.list_repos()}
    assert by_name == {"web": False, "secret": True, "unsynced": None}


# ── blast-radius visibility filter (the privacy-critical decision) ──

# visibility map: "owner/repo" -> True (private) / False (public) / None (unknown)
_VIS = {"acme/pub": False, "acme/priv": True}  # "acme/unknown" absent -> None


def _vis(name: str) -> bool | None:
    return _VIS.get(name)


def _deps(*names: str) -> list[dict]:
    return [{"repo": n, "files": []} for n in names]


def test_public_review_keeps_only_known_public():
    deps = _deps("acme/pub", "acme/priv", "acme/unknown")
    kept = filter_blast_radius_for_visibility(deps, reviewed_private=False, dependent_private=_vis)
    assert [d["repo"] for d in kept] == ["acme/pub"]


def test_unknown_reviewed_repo_is_treated_as_public_side():
    # Reviewed visibility unknown → still filter to known-public (safe).
    deps = _deps("acme/pub", "acme/priv")
    kept = filter_blast_radius_for_visibility(deps, reviewed_private=None, dependent_private=_vis)
    assert [d["repo"] for d in kept] == ["acme/pub"]


def test_private_reviewed_repo_keeps_all():
    deps = _deps("acme/pub", "acme/priv", "acme/unknown")
    kept = filter_blast_radius_for_visibility(deps, reviewed_private=True, dependent_private=_vis)
    assert kept == deps


def test_unknown_dependent_is_omitted_from_public_review():
    deps = _deps("acme/unknown")
    kept = filter_blast_radius_for_visibility(deps, reviewed_private=False, dependent_private=_vis)
    assert kept == []
