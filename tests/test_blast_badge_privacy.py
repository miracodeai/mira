"""The blast-radius badge is public (GitHub embeds it in READMEs), so it must
not reveal a private repo's structure, or a private repo's name, to the
anonymous web — while a signed-in dashboard user still sees everything."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mira.dashboard import api
from mira.dashboard.db import AppDatabase, User
from mira.dashboard.routers import repos


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    db = AppDatabase(url="", admin_password="admin")
    monkeypatch.setattr(api, "_app_db", db)
    db.register_repo("acme", "web", installation_id=1)  # visibility unknown until set
    db.register_repo("acme", "public-client", installation_id=1)
    db.set_repo_visibility("acme", "public-client", False)
    db.register_repo("acme", "secret-client", installation_id=1)
    db.set_repo_visibility("acme", "secret-client", True)
    return db


class _Store:
    """Two indexed files; core.py is called from helper.py so it ranks as core."""

    def all_paths(self) -> list[str]:
        return ["src/core.py", "src/helper.py"]

    def get_summary(self, path: str) -> SimpleNamespace:
        return SimpleNamespace(symbols=[SimpleNamespace(name="run")])

    def get_call_graph(self, path: str, name: str) -> list[tuple[str, int]]:
        return [("src/helper.py", 3)] if path == "src/core.py" else []


class _Relationships:
    def resolve_edges(self) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(source_repo="acme/public-client", target_repo="acme/web"),
            SimpleNamespace(source_repo="acme/secret-client", target_repo="acme/web"),
        ]


@pytest.fixture(autouse=True)
def _stub_index(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def open_store(owner: str, repo: str):
        yield _Store()

    @contextmanager
    def open_relationships():
        yield _Relationships()

    monkeypatch.setattr(repos, "_open_store", open_store)
    monkeypatch.setattr(repos, "_open_relationships", open_relationships)


def _request(user: User | None = None) -> SimpleNamespace:
    state = SimpleNamespace(user=user) if user else SimpleNamespace()
    return SimpleNamespace(state=state)


def _badge(owner: str, repo: str, user: User | None = None) -> tuple[str, dict]:
    response = repos.get_blast_radius_svg(owner, repo, _request(user))
    assert response.media_type == "image/svg+xml"
    return response.body.decode(), response.headers


def test_anonymous_public_repo_shows_files_and_only_public_dependents(db: AppDatabase) -> None:
    db.set_repo_visibility("acme", "web", False)
    svg, headers = _badge("acme", "web")
    assert "core.py" in svg
    assert "public-client" in svg
    assert "secret-client" not in svg
    assert headers["cache-control"] == "public, max-age=300"


def test_anonymous_private_repo_is_redacted(db: AppDatabase) -> None:
    db.set_repo_visibility("acme", "web", True)
    svg, headers = _badge("acme", "web")
    assert "core.py" not in svg
    assert "client" not in svg
    assert "Private repository" in svg
    assert headers["cache-control"] == "public, max-age=300"


def test_unknown_visibility_counts_as_private(db: AppDatabase) -> None:
    svg, _ = _badge("acme", "web")  # never synced → visibility None
    assert "core.py" not in svg
    assert "Private repository" in svg


def test_signed_in_viewer_sees_everything(db: AppDatabase) -> None:
    db.set_repo_visibility("acme", "web", True)
    viewer = User(id=2, username="bob", is_admin=False)
    svg, headers = _badge("acme", "web", viewer)
    assert "core.py" in svg
    assert "public-client" in svg
    assert "secret-client" in svg
    # Never from a shared cache to an anonymous viewer.
    assert headers["cache-control"] == "private, max-age=300"
    assert headers["vary"] == "Cookie"


def test_unknown_repo_is_404_for_everyone(db: AppDatabase) -> None:
    with pytest.raises(HTTPException) as exc:
        repos.get_blast_radius_svg("acme", "ghost", _request())
    assert exc.value.status_code == 404
