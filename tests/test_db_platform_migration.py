"""The platform-column migration must upgrade an old (owner, repo) DB in place."""

from __future__ import annotations

import sqlite3

from mira.dashboard.db import AppDatabase

# The pre-migration repos/pr_review_progress schema (no platform column,
# PK on owner/repo). Mirrors what shipped before GitLab support.
_OLD_SCHEMA = """
CREATE TABLE repos (
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
    PRIMARY KEY (owner, repo)
);
CREATE TABLE pr_review_progress (
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    total_paths TEXT NOT NULL DEFAULT '[]',
    reviewed_paths TEXT NOT NULL DEFAULT '[]',
    skipped_paths TEXT NOT NULL DEFAULT '[]',
    chunk_index INTEGER NOT NULL DEFAULT 0,
    last_reviewed_sha TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (owner, repo, pr_number)
);
"""


def test_migrates_old_db_preserving_rows(tmp_path):
    db_file = tmp_path / "app.db"
    # Seed an old-schema DB with one GitHub repo + one progress row.
    conn = sqlite3.connect(db_file)
    conn.executescript(_OLD_SCHEMA)
    conn.execute(
        "INSERT INTO repos (owner, repo, status, installation_id, private) "
        "VALUES ('acme', 'web', 'ready', 42, 1)"
    )
    conn.execute(
        "INSERT INTO pr_review_progress (owner, repo, pr_number, last_reviewed_sha) "
        "VALUES ('acme', 'web', 3, 'deadbeef')"
    )
    conn.commit()
    conn.close()

    # Opening through AppDatabase runs the migration.
    db = AppDatabase(url=f"sqlite:///{db_file}")

    # Existing row preserved and tagged github.
    rec = db.get_repo("acme", "web")
    assert rec is not None
    assert rec.platform == "github"
    assert rec.status == "ready"
    assert rec.installation_id == 42
    assert rec.private is True
    assert db.get_last_reviewed_sha("acme", "web", 3) == "deadbeef"

    # New PK allows a same-named repo on another platform to coexist.
    db.register_repo("acme", "web", platform="gitlab")
    assert db.get_repo("acme", "web", platform="gitlab") is not None
    assert db.get_repo("acme", "web").status == "ready"  # github row untouched

    # The composite PK is in force (not the old owner/repo one).
    cols = {
        r[1]
        for r in db._sqlite_conn.execute("PRAGMA table_info(repos)").fetchall()  # type: ignore[union-attr]
        if r[5]  # pk flag
    }
    assert cols == {"platform", "owner", "repo"}
