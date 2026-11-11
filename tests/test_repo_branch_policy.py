"""Per-repo auto-review branch policy: DB column, dashboard API, migration."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from mira.dashboard.db import AppDatabase
from mira.dashboard.routers.repos import (
    _parse_base_branch_policy,
    get_branch_policy,
    set_branch_policy,
)


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    return AppDatabase(url="", admin_password="admin")


def _admin_request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(is_admin=True)))


def _non_admin_request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(is_admin=False)))


class TestDBColumn:
    def test_new_column_defaults_to_inherit(self, db: AppDatabase):
        db.register_repo("o", "r")
        record = db.get_repo("o", "r")
        assert record is not None
        assert record.base_branch_policy == ""

    def test_set_and_read_back(self, db: AppDatabase):
        db.register_repo("o", "r")
        blob = json.dumps({"mode": "override", "branches": ["main", "dev*"]})
        db.set_repo_base_branch_policy("o", "r", blob)
        record = db.get_repo("o", "r")
        assert record is not None
        assert json.loads(record.base_branch_policy) == {
            "mode": "override",
            "branches": ["main", "dev*"],
        }

    def test_unregistered_repo_is_noop(self, db: AppDatabase):
        # A phantom repo must not gain a row just by saving a policy.
        db.set_repo_base_branch_policy("ghost", "repo", '{"mode": "disable"}')
        assert db.get_repo("ghost", "repo") is None

    def test_migration_adds_column_to_existing_db(self, tmp_path: Path):
        # A DB created before the column existed (plain sqlite3, schema from
        # before the feature) must gain the column on open.
        raw = tmp_path / "legacy.db"
        conn = sqlite3.connect(raw)
        conn.executescript(
            """
            CREATE TABLE repos (
                platform TEXT NOT NULL DEFAULT 'github',
                owner TEXT NOT NULL,
                repo TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                index_mode TEXT NOT NULL DEFAULT 'full',
                files_indexed INTEGER NOT NULL DEFAULT 0,
                file_count_estimate INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                installation_id INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0,
                last_indexed_at REAL NOT NULL DEFAULT 0,
                conventions TEXT NOT NULL DEFAULT '',
                private INTEGER,
                PRIMARY KEY (platform, owner, repo)
            );
            INSERT INTO repos (owner, repo) VALUES ('legacy', 'repo');
            """
        )
        conn.commit()
        conn.close()

        db = AppDatabase(url=str(raw), admin_password="admin")
        cols = {r[1] for r in db._sqlite_conn.execute("PRAGMA table_info(repos)")}
        assert "base_branch_policy" in cols
        record = db.get_repo("legacy", "repo")
        assert record is not None
        assert record.base_branch_policy == ""


class TestParsePolicy:
    def test_empty_is_unset(self):
        # Unset column parses to None; the GET endpoint maps that to inherit.
        assert _parse_base_branch_policy("") is None

    def test_override_round_trip(self):
        p = _parse_base_branch_policy('{"mode": "override", "branches": ["main"]}')
        assert p is not None
        assert p.mode == "override"
        assert p.branches == ["main"]

    def test_disable(self):
        p = _parse_base_branch_policy('{"mode": "disable"}')
        assert p is not None
        assert p.mode == "disable"

    def test_malformed_returns_none(self):
        assert _parse_base_branch_policy("{not json") is None
        assert _parse_base_branch_policy('{"mode": "unknown"}') is None
        # override with a bad pattern list must not surface as a valid policy
        assert _parse_base_branch_policy('{"mode": "override", "branches": ""}') is None
        assert _parse_base_branch_policy('{"mode": "override"}') is None
        assert _parse_base_branch_policy('{"mode": "override", "branches": [""]}') is None


class TestEndpoints:
    def test_put_persists_normalized_patterns(self, db: AppDatabase, monkeypatch):
        db.register_repo("o", "r")
        monkeypatch.setattr("mira.dashboard.api._app_db", db)
        body = SimpleNamespace(mode="override", branches=[" main", "release/*\t "])
        saved = set_branch_policy("o", "r", body, _admin_request())
        assert saved.branches == ["main", "release/*"]
        assert get_branch_policy("o", "r").branches == saved.branches
        record = db.get_repo("o", "r")
        assert json.loads(record.base_branch_policy)["branches"] == saved.branches

    def test_get_default_is_inherit(self, db: AppDatabase, monkeypatch):
        db.register_repo("o", "r")
        monkeypatch.setattr("mira.dashboard.api._app_db", db)
        policy = get_branch_policy("o", "r")
        assert policy.mode == "inherit"

    def test_put_then_get(self, db: AppDatabase, monkeypatch):
        db.register_repo("o", "r")
        monkeypatch.setattr("mira.dashboard.api._app_db", db)
        body = SimpleNamespace(mode="override", branches=["main", "dev*"])
        saved = set_branch_policy("o", "r", body, _admin_request())  # noqa: SLF001
        assert saved.mode == "override"
        assert saved.branches == ["main", "dev*"]
        assert get_branch_policy("o", "r").mode == "override"

    def test_put_drops_branches_for_inherit_and_disable(self, db: AppDatabase, monkeypatch):
        # Stale patterns must not persist for modes that ignore them.
        db.register_repo("o", "r")
        monkeypatch.setattr("mira.dashboard.api._app_db", db)
        body = SimpleNamespace(mode="disable", branches=["stale"])
        saved = set_branch_policy("o", "r", body, _admin_request())  # noqa: SLF001
        assert saved.branches == []
        record = db.get_repo("o", "r")
        assert json.loads(record.base_branch_policy) == {"mode": "disable", "branches": []}

    def test_put_requires_admin(self, db: AppDatabase, monkeypatch):
        db.register_repo("o", "r")
        monkeypatch.setattr("mira.dashboard.api._app_db", db)
        body = SimpleNamespace(mode="disable", branches=[])
        with pytest.raises(HTTPException) as exc:
            set_branch_policy("o", "r", body, _non_admin_request())  # noqa: SLF001
        assert exc.value.status_code == 403

    def test_put_rejects_empty_override(self, db: AppDatabase, monkeypatch):
        db.register_repo("o", "r")
        monkeypatch.setattr("mira.dashboard.api._app_db", db)
        body = SimpleNamespace(mode="override", branches=[])
        with pytest.raises(HTTPException) as exc:
            set_branch_policy("o", "r", body, _admin_request())  # noqa: SLF001
        assert exc.value.status_code == 400

    def test_put_unknown_repo_404s(self, db: AppDatabase, monkeypatch):
        monkeypatch.setattr("mira.dashboard.api._app_db", db)
        body = SimpleNamespace(mode="disable", branches=[])
        with pytest.raises(HTTPException) as exc:
            set_branch_policy("ghost", "repo", body, _admin_request())  # noqa: SLF001
        assert exc.value.status_code == 404
