"""Application database for auth (users, sessions).

Supports PostgreSQL via DATABASE_URL or SQLite fallback for local dev.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from typing import Any

logger = logging.getLogger(__name__)

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    theme TEXT NOT NULL DEFAULT 'dark',
    created_at REAL NOT NULL DEFAULT 0,
    last_login_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL DEFAULT 0,
    expires_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS repos (
    -- Code-hosting platform ('github' / 'gitlab'). Part of the key so the same
    -- owner/repo can exist on more than one platform.
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
    -- Distinct from updated_at: only set when a real indexing run finishes.
    -- Container restarts and reconciliation passes touch updated_at without
    -- last_indexed_at, so the dashboard's "Indexed N ago" reflects actual
    -- indexing, not housekeeping.
    last_indexed_at REAL NOT NULL DEFAULT 0,
    -- Team coding conventions extracted from CONTRIBUTING.md / AGENTS.md /
    -- etc. at indexing time; injected into review prompts so Mira flags
    -- team-specific violations (not just generic best-practices).
    conventions TEXT NOT NULL DEFAULT '',
    -- Repo visibility; keeps private repo names out of the blast-radius
    -- section of a public repo's review. NULL = not yet known (treated as
    -- private until a sync/PR/install event records the real value).
    private INTEGER,
    PRIMARY KEY (platform, owner, repo)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS pending_uninstalls (
    installation_id INTEGER PRIMARY KEY,
    owner TEXT NOT NULL,
    created_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS global_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pr_review_progress (
    platform TEXT NOT NULL DEFAULT 'github',
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    total_paths TEXT NOT NULL DEFAULT '[]',     -- JSON array of all paths in PR
    reviewed_paths TEXT NOT NULL DEFAULT '[]',  -- JSON array of paths reviewed so far
    skipped_paths TEXT NOT NULL DEFAULT '[]',   -- JSON array of paths intentionally skipped (low priority)
    chunk_index INTEGER NOT NULL DEFAULT 0,
    -- Head SHA of the most recent review on this PR. Round 2+ uses this
    -- as the base for an incremental diff so unchanged files aren't
    -- re-flagged after a new push.
    last_reviewed_sha TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (platform, owner, repo, pr_number)
);

-- ── Contributor analytics ──
-- People who contribute to indexed repos, keyed provider-agnostically so a
-- future non-GitHub provider slots in without a schema change.
CREATE TABLE IF NOT EXISTS contributors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL DEFAULT 'github',
    external_login TEXT NOT NULL,
    -- Stable numeric id from the provider (GitHub user id). Survives a login
    -- rename, so a later migration can merge two logins that share an id.
    external_id INTEGER NOT NULL DEFAULT 0,
    display_name TEXT NOT NULL DEFAULT '',
    avatar_url TEXT NOT NULL DEFAULT '',
    is_bot INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    UNIQUE (provider, external_login)
);

-- One row per atomic contribution event (PR opened/merged, commit, review).
-- The UNIQUE key makes both backfill and live webhooks idempotent: a re-run
-- or an overlapping webhook updates the row instead of double-counting.
CREATE TABLE IF NOT EXISTS contributions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contributor_id INTEGER NOT NULL REFERENCES contributors(id) ON DELETE CASCADE,
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    kind TEXT NOT NULL,            -- 'pr_opened' | 'pr_merged' | 'commit' | 'review'
    external_key TEXT NOT NULL,    -- 'pr:123' | 'prm:123' | 'commit:<sha>' | 'review:<id>'
    pr_number INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    additions INTEGER NOT NULL DEFAULT 0,
    deletions INTEGER NOT NULL DEFAULT 0,
    changed_files INTEGER NOT NULL DEFAULT 0,
    merged INTEGER NOT NULL DEFAULT 0,
    -- Epoch of when the event actually happened (authored/merged/committed),
    -- NOT ingest time — the heatmap buckets on this.
    event_at REAL NOT NULL DEFAULT 0,
    event_day TEXT NOT NULL DEFAULT '',  -- 'YYYY-MM-DD' UTC, denormalized from event_at
    created_at REAL NOT NULL DEFAULT 0,
    UNIQUE (owner, repo, kind, external_key)
);
CREATE INDEX IF NOT EXISTS idx_contrib_contributor ON contributions(contributor_id, event_at);
CREATE INDEX IF NOT EXISTS idx_contrib_repo ON contributions(owner, repo, event_at);

-- Pre-aggregated per-contributor/day rollup so the 365-day heatmap is one
-- indexed range scan rather than a GROUP BY over every commit row. Maintained
-- incrementally — only bumped when a contributions row is genuinely inserted.
CREATE TABLE IF NOT EXISTS contribution_days (
    contributor_id INTEGER NOT NULL REFERENCES contributors(id) ON DELETE CASCADE,
    day TEXT NOT NULL,            -- 'YYYY-MM-DD' UTC
    commits INTEGER NOT NULL DEFAULT 0,
    prs_opened INTEGER NOT NULL DEFAULT 0,
    prs_merged INTEGER NOT NULL DEFAULT 0,
    reviews INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (contributor_id, day)
);

-- ── Review insights ──
-- One row per PR, tracking its review lifecycle for stale/throughput insights.
CREATE TABLE IF NOT EXISTS pull_requests (
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'open',   -- open | closed | merged
    draft INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,   -- last activity; drives "stale"
    first_review_at REAL NOT NULL DEFAULT 0,  -- 0 = not yet reviewed
    merged_at REAL NOT NULL DEFAULT 0,
    closed_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (owner, repo, number)
);
CREATE INDEX IF NOT EXISTS idx_pr_state ON pull_requests(state);

-- One row per (PR, reviewer): when they were asked, when they first responded,
-- and their latest review state. Powers reviewer responsiveness + waiting-on.
CREATE TABLE IF NOT EXISTS pr_reviewers (
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    reviewer TEXT NOT NULL,
    requested_at REAL NOT NULL DEFAULT 0,   -- 0 = not formally requested
    responded_at REAL NOT NULL DEFAULT 0,   -- 0 = hasn't reviewed yet
    state TEXT NOT NULL DEFAULT '',          -- approved | changes_requested | commented
    -- 1 = "rubber-stamp": an approval with no substantive body or inline comments
    bare_approval INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (owner, repo, pr_number, reviewer)
);
"""

_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    theme TEXT NOT NULL DEFAULT 'dark',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_login_at DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS repos (
    platform TEXT NOT NULL DEFAULT 'github',
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    index_mode TEXT NOT NULL DEFAULT 'full',
    files_indexed INTEGER NOT NULL DEFAULT 0,
    file_count_estimate INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    installation_id INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    -- Distinct from updated_at: only set when a real indexing run finishes.
    last_indexed_at TIMESTAMPTZ,
    -- Team coding conventions extracted at indexing time.
    conventions TEXT NOT NULL DEFAULT '',
    -- Repo visibility; keeps private repo names out of a public review.
    -- NULL = not yet known (treated as private until a sync records it).
    private BOOLEAN,
    PRIMARY KEY (platform, owner, repo)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS pending_uninstalls (
    installation_id INTEGER PRIMARY KEY,
    owner TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS global_rules (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pr_review_progress (
    platform TEXT NOT NULL DEFAULT 'github',
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    total_paths TEXT NOT NULL DEFAULT '[]',
    reviewed_paths TEXT NOT NULL DEFAULT '[]',
    skipped_paths TEXT NOT NULL DEFAULT '[]',
    chunk_index INTEGER NOT NULL DEFAULT 0,
    last_reviewed_sha TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (platform, owner, repo, pr_number)
);

-- ── Contributor analytics ── (see SQLite schema above for column rationale)
CREATE TABLE IF NOT EXISTS contributors (
    id SERIAL PRIMARY KEY,
    provider TEXT NOT NULL DEFAULT 'github',
    external_login TEXT NOT NULL,
    external_id BIGINT NOT NULL DEFAULT 0,
    display_name TEXT NOT NULL DEFAULT '',
    avatar_url TEXT NOT NULL DEFAULT '',
    is_bot BOOLEAN NOT NULL DEFAULT FALSE,
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    UNIQUE (provider, external_login)
);

CREATE TABLE IF NOT EXISTS contributions (
    id SERIAL PRIMARY KEY,
    contributor_id INTEGER NOT NULL REFERENCES contributors(id) ON DELETE CASCADE,
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    kind TEXT NOT NULL,
    external_key TEXT NOT NULL,
    pr_number INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    additions INTEGER NOT NULL DEFAULT 0,
    deletions INTEGER NOT NULL DEFAULT 0,
    changed_files INTEGER NOT NULL DEFAULT 0,
    merged BOOLEAN NOT NULL DEFAULT FALSE,
    event_at REAL NOT NULL DEFAULT 0,
    event_day TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL DEFAULT 0,
    UNIQUE (owner, repo, kind, external_key)
);
CREATE INDEX IF NOT EXISTS idx_contrib_contributor ON contributions(contributor_id, event_at);
CREATE INDEX IF NOT EXISTS idx_contrib_repo ON contributions(owner, repo, event_at);

CREATE TABLE IF NOT EXISTS contribution_days (
    contributor_id INTEGER NOT NULL REFERENCES contributors(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    commits INTEGER NOT NULL DEFAULT 0,
    prs_opened INTEGER NOT NULL DEFAULT 0,
    prs_merged INTEGER NOT NULL DEFAULT 0,
    reviews INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (contributor_id, day)
);

-- ── Review insights ── (see SQLite schema above for column rationale)
CREATE TABLE IF NOT EXISTS pull_requests (
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    number INTEGER NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'open',
    draft INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    first_review_at REAL NOT NULL DEFAULT 0,
    merged_at REAL NOT NULL DEFAULT 0,
    closed_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (owner, repo, number)
);
CREATE INDEX IF NOT EXISTS idx_pr_state ON pull_requests(state);

CREATE TABLE IF NOT EXISTS pr_reviewers (
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    reviewer TEXT NOT NULL,
    requested_at REAL NOT NULL DEFAULT 0,
    responded_at REAL NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (owner, repo, pr_number, reviewer)
);
"""

SESSION_DURATION = 86400 * 7  # 7 days


@dataclass
class User:
    id: int
    username: str
    is_admin: bool = False
    theme: str = "dark"
    created_at: float = 0.0
    last_login_at: float = 0.0


@dataclass
class GlobalRule:
    """A global custom rule that applies to all repos in the org."""

    id: int
    title: str
    content: str
    enabled: bool = True
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class PRReviewProgress:
    """Tracks which files in a large PR have been reviewed across one or more
    review passes. Powers `@mira-bot review-rest` and (eventually) the
    auto-advance progressive flow."""

    owner: str
    repo: str
    pr_number: int
    total_paths: list[str]  # all paths in PR diff at last review
    reviewed_paths: list[str]  # paths reviewed so far
    skipped_paths: list[str]  # paths intentionally not reviewed (low priority)
    chunk_index: int = 0  # how many review passes have run for this PR
    updated_at: float = 0.0

    @property
    def is_complete(self) -> bool:
        return set(self.total_paths) == set(self.reviewed_paths) | set(self.skipped_paths)

    @property
    def remaining_paths(self) -> list[str]:
        done = set(self.reviewed_paths) | set(self.skipped_paths)
        return [p for p in self.total_paths if p not in done]


@dataclass
class RepoRecord:
    owner: str
    repo: str
    platform: str = "github"
    status: str = "pending"  # pending, indexing, ready, failed
    index_mode: str = "full"  # full, light, none
    files_indexed: int = 0
    file_count_estimate: int = 0
    error: str = ""
    installation_id: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    last_indexed_at: float = 0.0  # 0.0 means never
    conventions: str = ""
    private: bool | None = None  # None = visibility not yet known


@dataclass
class Contributor:
    """A person who contributes to indexed repos, keyed by (provider, login)."""

    id: int
    provider: str
    external_login: str
    external_id: int = 0
    display_name: str = ""
    avatar_url: str = ""
    is_bot: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass
class ContributionDay:
    """One day's contribution counts for a contributor (heatmap cell)."""

    day: str  # 'YYYY-MM-DD' UTC
    commits: int = 0
    prs_opened: int = 0
    prs_merged: int = 0
    reviews: int = 0
    total: int = 0


# Valid contribution kinds and the contribution_days column each one rolls up.
_CONTRIB_KIND_COLUMNS = {
    "commit": "commits",
    "pr_opened": "prs_opened",
    "pr_merged": "prs_merged",
    "review": "reviews",
}


def _epoch_to_day(event_at: float) -> str:
    """UTC 'YYYY-MM-DD' for an epoch timestamp — the heatmap bucket key."""
    from datetime import datetime

    return datetime.fromtimestamp(event_at, tz=UTC).strftime("%Y-%m-%d")


def _hash_password(password: str) -> str:
    """Hash password with salt using SHA-256. Simple and dependency-free."""
    salt = "mira_salt_v1"
    return hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()


class AppDatabase:
    """Application database for users and sessions."""

    def __init__(self, url: str = "", admin_password: str = "admin") -> None:
        self._url = url
        self._admin_password = admin_password
        self._pg_conn = None
        self._pg_lock = threading.Lock()
        self._sqlite_conn: sqlite3.Connection | None = None

        if url.startswith("postgresql://") or url.startswith("postgres://"):
            self._init_postgres(url)
        else:
            self._init_sqlite(url)

        self._ensure_default_admin()

    def _init_sqlite(self, url: str) -> None:
        self._backend = "sqlite"
        if url.startswith("sqlite:///"):
            db_path = url[len("sqlite:///") :]
        elif url:
            db_path = url
        else:
            index_dir = os.environ.get("MIRA_INDEX_DIR", "./data/indexes")
            db_path = os.path.join(index_dir, "_app.db")
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._sqlite_conn = sqlite3.connect(db_path, check_same_thread=False)
        self._sqlite_conn.execute("PRAGMA journal_mode=WAL")
        self._sqlite_conn.execute("PRAGMA foreign_keys=ON")
        self._sqlite_conn.executescript(_SQLITE_SCHEMA)
        # Lightweight migrations for columns added after the original schema.
        # SQLite has no "IF NOT EXISTS" on ALTER, so we probe pragma_table_info.
        user_cols = {r[1] for r in self._sqlite_conn.execute("PRAGMA table_info(users)").fetchall()}
        if "last_login_at" not in user_cols:
            self._sqlite_conn.execute(
                "ALTER TABLE users ADD COLUMN last_login_at REAL NOT NULL DEFAULT 0"
            )
        cols = {r[1] for r in self._sqlite_conn.execute("PRAGMA table_info(repos)").fetchall()}
        if "last_indexed_at" not in cols:
            self._sqlite_conn.execute(
                "ALTER TABLE repos ADD COLUMN last_indexed_at REAL NOT NULL DEFAULT 0"
            )
        if "conventions" not in cols:
            self._sqlite_conn.execute(
                "ALTER TABLE repos ADD COLUMN conventions TEXT NOT NULL DEFAULT ''"
            )
        if "private" not in cols:
            # Nullable, no default — existing rows become NULL ("unknown"),
            # which the blast-radius filter treats as private until a sync
            # records the real visibility.
            self._sqlite_conn.execute("ALTER TABLE repos ADD COLUMN private INTEGER")
        progress_cols = {
            r[1]
            for r in self._sqlite_conn.execute("PRAGMA table_info(pr_review_progress)").fetchall()
        }
        if "last_reviewed_sha" not in progress_cols:
            self._sqlite_conn.execute(
                "ALTER TABLE pr_review_progress ADD COLUMN last_reviewed_sha TEXT NOT NULL DEFAULT ''"
            )
        # bare_approval (rubber-stamp flag) added to pr_reviewers post-launch.
        reviewer_cols = {
            r[1] for r in self._sqlite_conn.execute("PRAGMA table_info(pr_reviewers)").fetchall()
        }
        if reviewer_cols and "bare_approval" not in reviewer_cols:
            self._sqlite_conn.execute(
                "ALTER TABLE pr_reviewers ADD COLUMN bare_approval INTEGER NOT NULL DEFAULT 0"
            )
        # Adding `platform` to the primary key requires a table rebuild (SQLite
        # can't alter a PK in place). Rename the old table, recreate it from the
        # current schema, and copy rows in as 'github'.
        repos_needs_platform = "platform" not in cols
        progress_needs_platform = "platform" not in progress_cols
        if repos_needs_platform:
            self._sqlite_conn.execute("ALTER TABLE repos RENAME TO repos_old")
        if progress_needs_platform:
            self._sqlite_conn.execute(
                "ALTER TABLE pr_review_progress RENAME TO pr_review_progress_old"
            )
        if repos_needs_platform or progress_needs_platform:
            self._sqlite_conn.executescript(_SQLITE_SCHEMA)  # recreates the renamed tables
        if repos_needs_platform:
            self._sqlite_conn.execute(
                "INSERT INTO repos (platform, owner, repo, status, index_mode, files_indexed, "
                "file_count_estimate, error, installation_id, created_at, updated_at, "
                "last_indexed_at, conventions, private) "
                "SELECT 'github', owner, repo, status, index_mode, files_indexed, "
                "file_count_estimate, error, installation_id, created_at, updated_at, "
                "last_indexed_at, conventions, private FROM repos_old"
            )
            self._sqlite_conn.execute("DROP TABLE repos_old")
        if progress_needs_platform:
            self._sqlite_conn.execute(
                "INSERT INTO pr_review_progress (platform, owner, repo, pr_number, total_paths, "
                "reviewed_paths, skipped_paths, chunk_index, last_reviewed_sha, updated_at) "
                "SELECT 'github', owner, repo, pr_number, total_paths, reviewed_paths, "
                "skipped_paths, chunk_index, last_reviewed_sha, updated_at FROM pr_review_progress_old"
            )
            self._sqlite_conn.execute("DROP TABLE pr_review_progress_old")
        self._sqlite_conn.commit()
        logger.info("App database: SQLite at %s", db_path)

    def _init_postgres(self, url: str) -> None:
        self._backend = "postgres"
        try:
            from mira.db.postgres import connect

            conn = connect(url)
            self._pg_conn = conn
            with conn.cursor() as cur:
                cur.execute(_PG_SCHEMA)
                # Lightweight migration for columns added after launch.
                cur.execute(
                    "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
                    "last_login_at DOUBLE PRECISION NOT NULL DEFAULT 0"
                )
                cur.execute(
                    "ALTER TABLE repos ADD COLUMN IF NOT EXISTS last_indexed_at TIMESTAMPTZ"
                )
                cur.execute(
                    "ALTER TABLE repos ADD COLUMN IF NOT EXISTS conventions TEXT NOT NULL DEFAULT ''"
                )
                cur.execute("ALTER TABLE repos ADD COLUMN IF NOT EXISTS private BOOLEAN")
                cur.execute(
                    "ALTER TABLE pr_review_progress ADD COLUMN IF NOT EXISTS "
                    "last_reviewed_sha TEXT NOT NULL DEFAULT ''"
                )
                cur.execute(
                    "ALTER TABLE pr_reviewers ADD COLUMN IF NOT EXISTS "
                    "bare_approval INTEGER NOT NULL DEFAULT 0"
                )
                # Add `platform` to the key on existing DBs. Postgres can swap
                # the PK in place; guard so it only rebuilds when needed.
                cur.execute(
                    "ALTER TABLE repos ADD COLUMN IF NOT EXISTS "
                    "platform TEXT NOT NULL DEFAULT 'github'"
                )
                cur.execute(
                    "ALTER TABLE pr_review_progress ADD COLUMN IF NOT EXISTS "
                    "platform TEXT NOT NULL DEFAULT 'github'"
                )
                cur.execute(
                    "DO $$ BEGIN "
                    "IF NOT EXISTS (SELECT 1 FROM information_schema.key_column_usage "
                    "WHERE table_name='repos' AND column_name='platform' "
                    "AND constraint_name='repos_pkey') THEN "
                    "ALTER TABLE repos DROP CONSTRAINT IF EXISTS repos_pkey; "
                    "ALTER TABLE repos ADD CONSTRAINT repos_pkey PRIMARY KEY (platform, owner, repo); "
                    "END IF; END $$;"
                )
                cur.execute(
                    "DO $$ BEGIN "
                    "IF NOT EXISTS (SELECT 1 FROM information_schema.key_column_usage "
                    "WHERE table_name='pr_review_progress' AND column_name='platform' "
                    "AND constraint_name='pr_review_progress_pkey') THEN "
                    "ALTER TABLE pr_review_progress DROP CONSTRAINT IF EXISTS pr_review_progress_pkey; "
                    "ALTER TABLE pr_review_progress ADD CONSTRAINT pr_review_progress_pkey "
                    "PRIMARY KEY (platform, owner, repo, pr_number); END IF; END $$;"
                )
            logger.info("App database: PostgreSQL")
        except ImportError:
            logger.warning(
                "psycopg not installed, falling back to SQLite. "
                "Install with: pip install 'psycopg[binary]>=3.1'"
            )
            self._init_sqlite("")
        except Exception as exc:
            logger.warning("PostgreSQL connection failed (%s), falling back to SQLite", exc)
            self._init_sqlite("")

    @contextmanager
    def _pg_cursor(self) -> Iterator[Any]:
        """Yield a Postgres cursor that reconnects once on stale-connection errors."""
        from mira.db.postgres import ReconnectingCursor, connect, reconnect

        def refresh() -> Any:
            with self._pg_lock:
                self._pg_conn = reconnect(self._url, self._pg_conn)
                return self._pg_conn

        with self._pg_lock:
            if self._pg_conn is None:
                self._pg_conn = connect(self._url)
            conn = self._pg_conn
        with ReconnectingCursor(conn.cursor(), on_reconnect=refresh) as cur:
            yield cur

    def _pg_commit(self) -> None:
        with self._pg_lock:
            if self._pg_conn is not None:
                self._pg_conn.commit()

    def _ensure_default_admin(self) -> None:
        """Create default admin user if none exists. Uses configured admin_password."""
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            row = self._sqlite_conn.execute(
                "SELECT COUNT(*) FROM users WHERE is_admin = 1"
            ).fetchone()
            if row[0] == 0:
                self.create_user("admin", self._admin_password, is_admin=True)
                logger.info("Created default admin user (password from config)")
        else:
            with self._pg_cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM users WHERE is_admin = TRUE")
                row = cur.fetchone()
                if row and row[0] == 0:
                    self.create_user("admin", self._admin_password, is_admin=True)
                    logger.info("Created default admin user (password from config)")

    def create_user(self, username: str, password: str, is_admin: bool = False) -> User:
        pw_hash = _hash_password(password)
        now = time.time()
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "INSERT INTO users (username, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?)",
                (username, pw_hash, int(is_admin), now),
            )
            self._sqlite_conn.commit()
            row_id = self._sqlite_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            return User(id=row_id, username=username, is_admin=is_admin, created_at=now)
        with self._pg_cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES (%s, %s, %s) RETURNING id",
                (username, pw_hash, is_admin),
            )
            row = cur.fetchone()
            return User(id=row[0], username=username, is_admin=is_admin, created_at=now)

    def authenticate(self, username: str, password: str) -> User | None:
        pw_hash = _hash_password(password)
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            row = self._sqlite_conn.execute(
                "SELECT id, username, is_admin, theme, created_at FROM users WHERE username = ? AND password_hash = ?",
                (username, pw_hash),
            ).fetchone()
            if row:
                return User(
                    id=row[0],
                    username=row[1],
                    is_admin=bool(row[2]),
                    theme=row[3],
                    created_at=row[4],
                )
            return None
        with self._pg_cursor() as cur:
            cur.execute(
                "SELECT id, username, is_admin, theme FROM users WHERE username = %s AND password_hash = %s",
                (username, pw_hash),
            )
            row = cur.fetchone()
            if row:
                return User(id=row[0], username=row[1], is_admin=bool(row[2]), theme=row[3])
            return None

    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        expires = now + SESSION_DURATION
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (token, user_id, now, expires),
            )
            self._sqlite_conn.commit()
        else:
            with self._pg_cursor() as cur:
                cur.execute(
                    "INSERT INTO sessions (token, user_id, expires_at) VALUES (%s, %s, to_timestamp(%s))",
                    (token, user_id, expires),
                )
        return token

    def validate_session(self, token: str) -> User | None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            row = self._sqlite_conn.execute(
                "SELECT u.id, u.username, u.is_admin, u.theme, u.created_at FROM sessions s "
                "JOIN users u ON s.user_id = u.id "
                "WHERE s.token = ? AND s.expires_at > ?",
                (token, time.time()),
            ).fetchone()
            if row:
                return User(
                    id=row[0],
                    username=row[1],
                    is_admin=bool(row[2]),
                    theme=row[3],
                    created_at=row[4],
                )
            return None
        with self._pg_cursor() as cur:
            cur.execute(
                "SELECT u.id, u.username, u.is_admin, u.theme FROM sessions s "
                "JOIN users u ON s.user_id = u.id "
                "WHERE s.token = %s AND s.expires_at > NOW()",
                (token,),
            )
            row = cur.fetchone()
            if row:
                return User(id=row[0], username=row[1], is_admin=bool(row[2]), theme=row[3])
            return None

    def set_user_theme(self, user_id: int, theme: str) -> None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute("UPDATE users SET theme = ? WHERE id = ?", (theme, user_id))
            self._sqlite_conn.commit()
        else:
            with self._pg_cursor() as cur:
                cur.execute("UPDATE users SET theme = %s WHERE id = %s", (theme, user_id))

    def update_password(self, user_id: int, new_password: str) -> None:
        """Set a user's password to a new value (already-verified by caller)."""
        pw_hash = _hash_password(new_password)
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?", (pw_hash, user_id)
            )
            self._sqlite_conn.commit()
        else:
            with self._pg_cursor() as cur:
                cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (pw_hash, user_id))
            # autocommit today, but this keeps the write safe if that changes.
            self._pg_commit()

    def record_login(self, user_id: int) -> None:
        """Stamp a user's last-login time (called on each successful login)."""
        now = time.time()
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?", (now, user_id)
            )
            self._sqlite_conn.commit()
        else:
            with self._pg_cursor() as cur:
                cur.execute("UPDATE users SET last_login_at = %s WHERE id = %s", (now, user_id))
            # autocommit today, but this keeps the write safe if that changes.
            self._pg_commit()

    def list_users(self) -> list[User]:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            rows = self._sqlite_conn.execute(
                "SELECT id, username, is_admin, created_at, last_login_at FROM users ORDER BY id"
            ).fetchall()
            return [
                User(
                    id=r[0],
                    username=r[1],
                    is_admin=bool(r[2]),
                    created_at=r[3],
                    last_login_at=r[4],
                )
                for r in rows
            ]
        with self._pg_cursor() as cur:
            cur.execute("SELECT id, username, is_admin, last_login_at FROM users ORDER BY id")
            return [
                User(id=r[0], username=r[1], is_admin=bool(r[2]), last_login_at=r[3])
                for r in cur.fetchall()
            ]

    def delete_user(self, user_id: int) -> None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            self._sqlite_conn.commit()
        else:
            with self._pg_cursor() as cur:
                cur.execute("DELETE FROM users WHERE id = %s", (user_id,))

    def delete_session(self, token: str) -> None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            self._sqlite_conn.commit()
        else:
            with self._pg_cursor() as cur:
                cur.execute("DELETE FROM sessions WHERE token = %s", (token,))

    # ── Repos ──

    def register_repo(
        self, owner: str, repo: str, installation_id: int = 0, platform: str = "github"
    ) -> RepoRecord:
        now = time.time()
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "INSERT INTO repos (platform, owner, repo, installation_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(platform, owner, repo) DO UPDATE SET "
                "installation_id=excluded.installation_id, updated_at=excluded.updated_at",
                (platform, owner, repo, installation_id, now, now),
            )
            self._sqlite_conn.commit()
        else:
            with self._pg_cursor() as cur:
                cur.execute(
                    "INSERT INTO repos (platform, owner, repo, installation_id) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT(platform, owner, repo) DO UPDATE SET "
                    "installation_id=EXCLUDED.installation_id, updated_at=NOW()",
                    (platform, owner, repo, installation_id),
                )
        return RepoRecord(
            owner=owner,
            repo=repo,
            platform=platform,
            installation_id=installation_id,
            created_at=now,
            updated_at=now,
        )

    def set_repo_status(
        self,
        owner: str,
        repo: str,
        status: str,
        files_indexed: int = 0,
        error: str = "",
        bump_last_indexed: bool = False,
        platform: str = "github",
    ) -> None:
        """Update a repo's status row.

        ``bump_last_indexed=True`` is reserved for callers that just
        completed a real indexing run — it sets ``last_indexed_at=NOW()``,
        which the dashboard surfaces as "Indexed N ago". Status-only
        updates (reconciliation, in-progress flips, error states) leave
        that timestamp untouched so the UI doesn't lie.
        """
        now = time.time()
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            if bump_last_indexed:
                self._sqlite_conn.execute(
                    "UPDATE repos SET status=?, files_indexed=?, error=?, "
                    "updated_at=?, last_indexed_at=? WHERE platform=? AND owner=? AND repo=?",
                    (status, files_indexed, error, now, now, platform, owner, repo),
                )
            else:
                self._sqlite_conn.execute(
                    "UPDATE repos SET status=?, files_indexed=?, error=?, "
                    "updated_at=? WHERE platform=? AND owner=? AND repo=?",
                    (status, files_indexed, error, now, platform, owner, repo),
                )
            self._sqlite_conn.commit()
        else:
            with self._pg_cursor() as cur:
                if bump_last_indexed:
                    cur.execute(
                        "UPDATE repos SET status=%s, files_indexed=%s, error=%s, "
                        "updated_at=NOW(), last_indexed_at=NOW() "
                        "WHERE platform=%s AND owner=%s AND repo=%s",
                        (status, files_indexed, error, platform, owner, repo),
                    )
                else:
                    cur.execute(
                        "UPDATE repos SET status=%s, files_indexed=%s, error=%s, "
                        "updated_at=NOW() WHERE platform=%s AND owner=%s AND repo=%s",
                        (status, files_indexed, error, platform, owner, repo),
                    )

    # ── Pending uninstalls ──

    def add_pending_uninstall(self, installation_id: int, owner: str) -> None:
        now = time.time()
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "INSERT INTO pending_uninstalls (installation_id, owner, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(installation_id) DO NOTHING",
                (installation_id, owner, now),
            )
            self._sqlite_conn.commit()
        else:
            with self._pg_cursor() as cur:
                cur.execute(
                    "INSERT INTO pending_uninstalls (installation_id, owner) VALUES (%s, %s) "
                    "ON CONFLICT(installation_id) DO NOTHING",
                    (installation_id, owner),
                )

    def list_pending_uninstalls(self) -> list[tuple[int, str]]:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            rows = self._sqlite_conn.execute(
                "SELECT installation_id, owner FROM pending_uninstalls"
            ).fetchall()
            return [(r[0], r[1]) for r in rows]
        with self._pg_cursor() as cur:
            cur.execute("SELECT installation_id, owner FROM pending_uninstalls")
            return [(r[0], r[1]) for r in cur.fetchall()]

    def remove_pending_uninstall(self, installation_id: int) -> None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "DELETE FROM pending_uninstalls WHERE installation_id=?",
                (installation_id,),
            )
            self._sqlite_conn.commit()
        else:
            with self._pg_cursor() as cur:
                cur.execute(
                    "DELETE FROM pending_uninstalls WHERE installation_id=%s",
                    (installation_id,),
                )

    def delete_repos_by_installation(self, installation_id: int) -> int:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            cur = self._sqlite_conn.execute(
                "DELETE FROM repos WHERE installation_id=?",
                (installation_id,),
            )
            self._sqlite_conn.commit()
            return cur.rowcount
        with self._pg_cursor() as cur:
            cur.execute(
                "DELETE FROM repos WHERE installation_id=%s",
                (installation_id,),
            )
            return cur.rowcount

    def delete_repo(self, owner: str, repo: str, platform: str = "github") -> None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "DELETE FROM repos WHERE platform=? AND owner=? AND repo=?",
                (platform, owner, repo),
            )
            self._sqlite_conn.commit()
        else:
            with self._pg_cursor() as cur:
                cur.execute(
                    "DELETE FROM repos WHERE platform=%s AND owner=%s AND repo=%s",
                    (platform, owner, repo),
                )

    def set_repo_file_count(
        self, owner: str, repo: str, count: int, platform: str = "github"
    ) -> None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "UPDATE repos SET file_count_estimate=? WHERE platform=? AND owner=? AND repo=?",
                (count, platform, owner, repo),
            )
            self._sqlite_conn.commit()
        else:
            with self._pg_cursor() as cur:
                cur.execute(
                    "UPDATE repos SET file_count_estimate=%s WHERE platform=%s AND owner=%s AND repo=%s",
                    (count, platform, owner, repo),
                )

    def set_repo_index_mode(
        self, owner: str, repo: str, mode: str, platform: str = "github"
    ) -> None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "UPDATE repos SET index_mode=? WHERE platform=? AND owner=? AND repo=?",
                (mode, platform, owner, repo),
            )
            self._sqlite_conn.commit()
        else:
            with self._pg_cursor() as cur:
                cur.execute(
                    "UPDATE repos SET index_mode=%s WHERE platform=%s AND owner=%s AND repo=%s",
                    (mode, platform, owner, repo),
                )

    @staticmethod
    def _sqlite_row_to_repo(row) -> RepoRecord:
        """Map a SELECT row (sqlite backend) to a RepoRecord."""
        return RepoRecord(
            owner=row[0],
            repo=row[1],
            status=row[2],
            index_mode=row[3],
            files_indexed=row[4],
            file_count_estimate=row[5],
            error=row[6],
            installation_id=row[7],
            created_at=row[8],
            updated_at=row[9],
            last_indexed_at=row[10] or 0.0,
            conventions=row[11] or "",
            private=(None if row[12] is None else bool(row[12])),
            platform=row[13],
        )

    @staticmethod
    def _pg_row_to_repo(row) -> RepoRecord:
        """Map a SELECT row (postgres backend) to a RepoRecord.

        Postgres returns timestamptz as datetime; downstream code
        expects epoch float. Coerce here.
        """
        return RepoRecord(
            owner=row[0],
            repo=row[1],
            status=row[2],
            index_mode=row[3],
            files_indexed=row[4],
            file_count_estimate=row[5],
            error=row[6],
            installation_id=row[7],
            created_at=row[8].timestamp() if row[8] else 0.0,
            updated_at=row[9].timestamp() if row[9] else 0.0,
            last_indexed_at=row[10].timestamp() if row[10] else 0.0,
            conventions=row[11] or "",
            private=(None if row[12] is None else bool(row[12])),
            platform=row[13],
        )

    def list_repos(self) -> list[RepoRecord]:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            rows = self._sqlite_conn.execute(
                "SELECT owner, repo, status, index_mode, files_indexed, file_count_estimate, "
                "error, installation_id, created_at, updated_at, last_indexed_at, conventions, private, "
                "platform FROM repos ORDER BY owner, repo"
            ).fetchall()
            return [self._sqlite_row_to_repo(r) for r in rows]
        with self._pg_cursor() as cur:
            cur.execute(
                "SELECT owner, repo, status, index_mode, files_indexed, file_count_estimate, "
                "error, installation_id, created_at, updated_at, last_indexed_at, conventions, private, "
                "platform FROM repos ORDER BY owner, repo"
            )
            return [self._pg_row_to_repo(r) for r in cur.fetchall()]

    def get_repo(self, owner: str, repo: str, platform: str = "github") -> RepoRecord | None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            row = self._sqlite_conn.execute(
                "SELECT owner, repo, status, index_mode, files_indexed, file_count_estimate, "
                "error, installation_id, created_at, updated_at, last_indexed_at, conventions, private, "
                "platform FROM repos WHERE platform=? AND owner=? AND repo=?",
                (platform, owner, repo),
            ).fetchone()
            if row:
                return self._sqlite_row_to_repo(row)
            return None
        with self._pg_cursor() as cur:
            cur.execute(
                "SELECT owner, repo, status, index_mode, files_indexed, file_count_estimate, "
                "error, installation_id, created_at, updated_at, last_indexed_at, conventions, private, "
                "platform FROM repos WHERE platform=%s AND owner=%s AND repo=%s",
                (platform, owner, repo),
            )
            row = cur.fetchone()
            if row:
                return self._pg_row_to_repo(row)
            return None

    def get_repo_any_platform(self, owner: str, repo: str) -> list[RepoRecord]:
        """Look up every repo matching (owner, repo) across all platforms.

        Returns one ``RepoRecord`` per platform that hosts the given
        owner/repo (typically 0 or 1 entries; at most one per platform).
        When more than one is returned the caller decides how to
        disambiguate — see ``_platform_pref`` callers in the dashboard
        routers, which prefer github → gitlab → forgejo.

        Single query replacing the former triple ``get_repo`` fallback
        (github → gitlab → forgejo). That pattern made 3 sequential SQLite
        calls on a threadpool worker, and the widened window collided with
        event-loop-thread access — producing ``InterfaceError`` on Forgejo
        repos (which always miss the github lookup and hit all three).
        """
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            rows = self._sqlite_conn.execute(
                "SELECT owner, repo, status, index_mode, files_indexed, file_count_estimate, "
                "error, installation_id, created_at, updated_at, last_indexed_at, conventions, private, "
                "platform FROM repos WHERE owner=? AND repo=?",
                (owner, repo),
            ).fetchall()
            return [self._sqlite_row_to_repo(r) for r in rows]
        with self._pg_cursor() as cur:
            cur.execute(
                "SELECT owner, repo, status, index_mode, files_indexed, file_count_estimate, "
                "error, installation_id, created_at, updated_at, last_indexed_at, conventions, private, "
                "platform FROM repos WHERE owner=%s AND repo=%s",
                (owner, repo),
            )
            return [self._pg_row_to_repo(r) for r in cur.fetchall()]

    def set_repo_conventions(
        self, owner: str, repo: str, conventions: str, platform: str = "github"
    ) -> None:
        """Store the team-conventions string for a repo. Called by the
        indexer after extracting from CONTRIBUTING.md / AGENTS.md / etc."""
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "UPDATE repos SET conventions=? WHERE platform=? AND owner=? AND repo=?",
                (conventions, platform, owner, repo),
            )
            self._sqlite_conn.commit()
        else:
            with self._pg_cursor() as cur:
                cur.execute(
                    "UPDATE repos SET conventions=%s WHERE platform=%s AND owner=%s AND repo=%s",
                    (conventions, platform, owner, repo),
                )

    def set_repo_visibility(
        self, owner: str, repo: str, private: bool, platform: str = "github"
    ) -> None:
        """Record a repo's visibility. Updates the existing row only;
        no-op if the repo isn't registered yet (it'll be set on next sync)."""
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "UPDATE repos SET private=? WHERE platform=? AND owner=? AND repo=?",
                (1 if private else 0, platform, owner, repo),
            )
            self._sqlite_conn.commit()
        else:
            with self._pg_cursor() as cur:
                cur.execute(
                    "UPDATE repos SET private=%s WHERE platform=%s AND owner=%s AND repo=%s",
                    (private, platform, owner, repo),
                )
            # Explicit commit mirrors set_last_reviewed_sha — the connection is
            # autocommit today, but this keeps the write safe if that changes.
            self._pg_commit()

    # ── Settings ──

    def get_setting(self, key: str) -> str | None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            row = self._sqlite_conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else None
        with self._pg_cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
            row = cur.fetchone()
            return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._sqlite_conn.commit()
        else:
            with self._pg_cursor() as cur:
                cur.execute(
                    "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                    (key, value),
                )

    @property
    def setup_complete(self) -> bool:
        return self.get_setting("setup_complete") == "true"

    # JSON-blobbed under one settings row — schema doesn't churn when
    # ReviewConfig / FilterConfig grow new fields.
    _GLOBAL_OVERRIDES_KEY = "global_review_overrides"

    def get_global_review_overrides(self) -> dict[str, Any]:
        """Return the admin-set runtime overrides, or {} if none."""
        raw = self.get_setting(self._GLOBAL_OVERRIDES_KEY)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def set_global_review_overrides(self, overrides: dict[str, Any]) -> None:
        """Replace the admin-set runtime overrides. Pass `{}` to clear."""
        self.set_setting(self._GLOBAL_OVERRIDES_KEY, json.dumps(overrides))

    # Outbound webhooks live in their own settings row (not in the review
    # overrides blob) so their secret URLs never leak into the effective-config
    # dump returned by GET /api/admin/settings.
    _WEBHOOKS_KEY = "webhooks"

    def get_webhooks(self) -> list[dict[str, Any]]:
        """Return the configured outbound webhooks, or [] if none."""
        raw = self.get_setting(self._WEBHOOKS_KEY)
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []

    def set_webhooks(self, webhooks: list[dict[str, Any]]) -> None:
        """Replace the configured outbound webhooks. Pass `[]` to clear."""
        self.set_setting(self._WEBHOOKS_KEY, json.dumps(webhooks))

    def mark_setup_complete(self) -> None:
        self.set_setting("setup_complete", "true")

    # ── Global rules ──

    def list_global_rules(self) -> list[GlobalRule]:
        """List all global rules."""
        if self._backend == "sqlite":
            rows = self._sqlite_conn.execute(
                "SELECT id, title, content, enabled, created_at, updated_at "
                "FROM global_rules ORDER BY updated_at DESC"
            ).fetchall()
        else:
            with self._pg_cursor() as cur:
                cur.execute(
                    "SELECT id, title, content, enabled, created_at, updated_at "
                    "FROM global_rules ORDER BY updated_at DESC"
                )
                rows = cur.fetchall()
        return [
            GlobalRule(
                id=r[0],
                title=r[1],
                content=r[2],
                enabled=bool(r[3]),
                created_at=r[4] if isinstance(r[4], float) else r[4].timestamp() if r[4] else 0.0,
                updated_at=r[5] if isinstance(r[5], float) else r[5].timestamp() if r[5] else 0.0,
            )
            for r in rows
        ]

    def get_global_rule(self, rule_id: int) -> GlobalRule | None:
        if self._backend == "sqlite":
            row = self._sqlite_conn.execute(
                "SELECT id, title, content, enabled, created_at, updated_at "
                "FROM global_rules WHERE id = ?",
                (rule_id,),
            ).fetchone()
        else:
            with self._pg_cursor() as cur:
                cur.execute(
                    "SELECT id, title, content, enabled, created_at, updated_at "
                    "FROM global_rules WHERE id = %s",
                    (rule_id,),
                )
                row = cur.fetchone()
        if not row:
            return None
        return GlobalRule(
            id=row[0],
            title=row[1],
            content=row[2],
            enabled=bool(row[3]),
            created_at=row[4]
            if isinstance(row[4], float)
            else row[4].timestamp()
            if row[4]
            else 0.0,
            updated_at=row[5]
            if isinstance(row[5], float)
            else row[5].timestamp()
            if row[5]
            else 0.0,
        )

    def upsert_global_rule(
        self,
        title: str,
        content: str,
        rule_id: int | None = None,
    ) -> GlobalRule:
        import time

        now = time.time()
        if self._backend == "sqlite":
            if rule_id:
                self._sqlite_conn.execute(
                    "UPDATE global_rules SET title=?, content=?, updated_at=? WHERE id=?",
                    (title, content, now, rule_id),
                )
                self._sqlite_conn.commit()
            else:
                cur = self._sqlite_conn.execute(
                    "INSERT INTO global_rules (title, content, enabled, created_at, updated_at) "
                    "VALUES (?, ?, 1, ?, ?)",
                    (title, content, now, now),
                )
                self._sqlite_conn.commit()
                rule_id = cur.lastrowid
        else:
            if rule_id:
                with self._pg_cursor() as cur:
                    cur.execute(
                        "UPDATE global_rules SET title=%s, content=%s, updated_at=NOW() WHERE id=%s",
                        (title, content, rule_id),
                    )
            else:
                with self._pg_cursor() as cur:
                    cur.execute(
                        "INSERT INTO global_rules (title, content, enabled) "
                        "VALUES (%s, %s, TRUE) RETURNING id",
                        (title, content),
                    )
                    rule_id = cur.fetchone()[0]
        return self.get_global_rule(rule_id)  # type: ignore[return-value]

    def delete_global_rule(self, rule_id: int) -> None:
        if self._backend == "sqlite":
            self._sqlite_conn.execute("DELETE FROM global_rules WHERE id=?", (rule_id,))
            self._sqlite_conn.commit()
        else:
            with self._pg_cursor() as cur:
                cur.execute("DELETE FROM global_rules WHERE id=%s", (rule_id,))

    def toggle_global_rule(self, rule_id: int) -> GlobalRule | None:
        if self._backend == "sqlite":
            self._sqlite_conn.execute(
                "UPDATE global_rules SET enabled = NOT enabled, updated_at = ? WHERE id = ?",
                ((__import__("time")).time(), rule_id),
            )
            self._sqlite_conn.commit()
        else:
            with self._pg_cursor() as cur:
                cur.execute(
                    "UPDATE global_rules SET enabled = NOT enabled, updated_at = NOW() WHERE id = %s",
                    (rule_id,),
                )
        return self.get_global_rule(rule_id)

    def get_global_rules_text(self) -> list[str]:
        """Get enabled global rules as a list of formatted strings for prompt injection."""
        rules = self.list_global_rules()
        return [f"{r.title}: {r.content}" for r in rules if r.enabled][:20]

    # ── PR review progress ──

    def upsert_pr_review_progress(
        self, progress: PRReviewProgress, platform: str = "github"
    ) -> None:
        """Insert or update progress for a single PR. Idempotent."""
        now = time.time()
        total = json.dumps(progress.total_paths)
        reviewed = json.dumps(progress.reviewed_paths)
        skipped = json.dumps(progress.skipped_paths)
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "INSERT INTO pr_review_progress "
                "(platform, owner, repo, pr_number, total_paths, reviewed_paths, "
                "skipped_paths, chunk_index, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(platform, owner, repo, pr_number) DO UPDATE SET "
                "total_paths=excluded.total_paths, "
                "reviewed_paths=excluded.reviewed_paths, "
                "skipped_paths=excluded.skipped_paths, "
                "chunk_index=excluded.chunk_index, "
                "updated_at=excluded.updated_at",
                (
                    platform,
                    progress.owner,
                    progress.repo,
                    progress.pr_number,
                    total,
                    reviewed,
                    skipped,
                    progress.chunk_index,
                    now,
                ),
            )
            self._sqlite_conn.commit()
        else:
            with self._pg_cursor() as cur:
                cur.execute(
                    "INSERT INTO pr_review_progress "
                    "(platform, owner, repo, pr_number, total_paths, reviewed_paths, "
                    "skipped_paths, chunk_index, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW()) "
                    "ON CONFLICT (platform, owner, repo, pr_number) DO UPDATE SET "
                    "total_paths=EXCLUDED.total_paths, "
                    "reviewed_paths=EXCLUDED.reviewed_paths, "
                    "skipped_paths=EXCLUDED.skipped_paths, "
                    "chunk_index=EXCLUDED.chunk_index, "
                    "updated_at=NOW()",
                    (
                        platform,
                        progress.owner,
                        progress.repo,
                        progress.pr_number,
                        total,
                        reviewed,
                        skipped,
                        progress.chunk_index,
                    ),
                )

    def get_pr_review_progress(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        platform: str = "github",
    ) -> PRReviewProgress | None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            row = self._sqlite_conn.execute(
                "SELECT total_paths, reviewed_paths, skipped_paths, chunk_index, updated_at "
                "FROM pr_review_progress WHERE platform=? AND owner=? AND repo=? AND pr_number=?",
                (platform, owner, repo, pr_number),
            ).fetchone()
        else:
            with self._pg_cursor() as cur:
                cur.execute(
                    "SELECT total_paths, reviewed_paths, skipped_paths, chunk_index, "
                    "EXTRACT(EPOCH FROM updated_at) "
                    "FROM pr_review_progress WHERE platform=%s AND owner=%s AND repo=%s AND pr_number=%s",
                    (platform, owner, repo, pr_number),
                )
                row = cur.fetchone()
        if not row:
            return None
        return PRReviewProgress(
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            total_paths=json.loads(row[0]),
            reviewed_paths=json.loads(row[1]),
            skipped_paths=json.loads(row[2]),
            chunk_index=int(row[3]),
            updated_at=float(row[4] or 0),
        )

    def get_last_reviewed_sha(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        platform: str = "github",
    ) -> str:
        """Return the head SHA at the time of the last review on this PR.

        Empty string if there is no prior review (first round, or progress
        row never written) — callers should treat empty as "no previous SHA,
        do a full review."
        """
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            row = self._sqlite_conn.execute(
                "SELECT last_reviewed_sha FROM pr_review_progress "
                "WHERE platform=? AND owner=? AND repo=? AND pr_number=?",
                (platform, owner, repo, pr_number),
            ).fetchone()
        else:
            with self._pg_cursor() as cur:
                cur.execute(
                    "SELECT last_reviewed_sha FROM pr_review_progress "
                    "WHERE platform=%s AND owner=%s AND repo=%s AND pr_number=%s",
                    (platform, owner, repo, pr_number),
                )
                row = cur.fetchone()
        return str(row[0]) if row and row[0] else ""

    def set_last_reviewed_sha(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        sha: str,
        platform: str = "github",
    ) -> None:
        """Record the head SHA we just reviewed against. Round 2+ uses this
        as the base for the incremental diff.
        """
        if not sha:
            return
        now = time.time()
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "INSERT INTO pr_review_progress "
                "(platform, owner, repo, pr_number, last_reviewed_sha, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(platform, owner, repo, pr_number) DO UPDATE SET "
                "last_reviewed_sha=excluded.last_reviewed_sha, "
                "updated_at=excluded.updated_at",
                (platform, owner, repo, pr_number, sha, now),
            )
            self._sqlite_conn.commit()
        else:
            with self._pg_cursor() as cur:
                cur.execute(
                    "INSERT INTO pr_review_progress "
                    "(platform, owner, repo, pr_number, last_reviewed_sha, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, NOW()) "
                    "ON CONFLICT (platform, owner, repo, pr_number) DO UPDATE SET "
                    "last_reviewed_sha=EXCLUDED.last_reviewed_sha, "
                    "updated_at=NOW()",
                    (platform, owner, repo, pr_number, sha),
                )
            self._pg_commit()

    def delete_pr_review_progress(
        self, owner: str, repo: str, pr_number: int, platform: str = "github"
    ) -> None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "DELETE FROM pr_review_progress WHERE platform=? AND owner=? AND repo=? AND pr_number=?",
                (platform, owner, repo, pr_number),
            )
            self._sqlite_conn.commit()
        else:
            with self._pg_cursor() as cur:
                cur.execute(
                    "DELETE FROM pr_review_progress WHERE platform=%s AND owner=%s AND repo=%s AND pr_number=%s",
                    (platform, owner, repo, pr_number),
                )

    # ── Contributors ──

    @property
    def _false_sql(self) -> str:
        """Boolean false literal for the active backend (SQLite stores 0/1)."""
        return "0" if self._backend == "sqlite" else "FALSE"

    def _rows(self, sql: str, params: tuple = ()) -> list[tuple]:
        """Run a read-only SELECT written with ``?`` placeholders against
        either backend (``?`` → ``%s`` for Postgres). Read path only."""
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            return self._sqlite_conn.execute(sql, params).fetchall()
        assert self._pg_conn is not None
        with self._pg_conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), params)
            return cur.fetchall()

    def _exec(self, sql: str, params: tuple = ()) -> None:
        """Run a write written with ``?`` placeholders + lowercase ``excluded``
        (both work on either backend) against the active backend."""
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(sql, params)
            self._sqlite_conn.commit()
            return
        assert self._pg_conn is not None
        with self._pg_conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), params)
        self._pg_conn.commit()

    def upsert_contributor(
        self,
        provider: str,
        external_login: str,
        *,
        external_id: int = 0,
        display_name: str = "",
        avatar_url: str = "",
        is_bot: bool = False,
    ) -> Contributor:
        """Insert or update a contributor keyed by (provider, login).

        Non-empty metadata wins on conflict so a sparse webhook update (no
        avatar/display name) never clobbers a richer backfilled row.
        """
        now = time.time()
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "INSERT INTO contributors "
                "(provider, external_login, external_id, display_name, avatar_url, is_bot, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(provider, external_login) DO UPDATE SET "
                "external_id=CASE WHEN excluded.external_id>0 THEN excluded.external_id ELSE contributors.external_id END, "
                "display_name=CASE WHEN excluded.display_name!='' THEN excluded.display_name ELSE contributors.display_name END, "
                "avatar_url=CASE WHEN excluded.avatar_url!='' THEN excluded.avatar_url ELSE contributors.avatar_url END, "
                "is_bot=excluded.is_bot, updated_at=excluded.updated_at",
                (
                    provider,
                    external_login,
                    external_id,
                    display_name,
                    avatar_url,
                    int(is_bot),
                    now,
                    now,
                ),
            )
            self._sqlite_conn.commit()
        else:
            assert self._pg_conn is not None
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO contributors "
                    "(provider, external_login, external_id, display_name, avatar_url, is_bot, created_at, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT(provider, external_login) DO UPDATE SET "
                    "external_id=CASE WHEN EXCLUDED.external_id>0 THEN EXCLUDED.external_id ELSE contributors.external_id END, "
                    "display_name=CASE WHEN EXCLUDED.display_name!='' THEN EXCLUDED.display_name ELSE contributors.display_name END, "
                    "avatar_url=CASE WHEN EXCLUDED.avatar_url!='' THEN EXCLUDED.avatar_url ELSE contributors.avatar_url END, "
                    "is_bot=EXCLUDED.is_bot, updated_at=EXCLUDED.updated_at",
                    (
                        provider,
                        external_login,
                        external_id,
                        display_name,
                        avatar_url,
                        is_bot,
                        now,
                        now,
                    ),
                )
        contributor = self.get_contributor_by_login(provider, external_login)
        assert contributor is not None  # just upserted
        return contributor

    def get_contributor_by_login(self, provider: str, external_login: str) -> Contributor | None:
        rows = self._rows(
            "SELECT id, provider, external_login, external_id, display_name, avatar_url, "
            "is_bot, created_at, updated_at FROM contributors WHERE provider=? AND external_login=?",
            (provider, external_login),
        )
        if not rows:
            return None
        r = rows[0]
        return Contributor(
            id=r[0],
            provider=r[1],
            external_login=r[2],
            external_id=r[3] or 0,
            display_name=r[4] or "",
            avatar_url=r[5] or "",
            is_bot=bool(r[6]),
            created_at=r[7] or 0.0,
            updated_at=r[8] or 0.0,
        )

    def record_contribution(
        self,
        contributor_id: int,
        owner: str,
        repo: str,
        kind: str,
        external_key: str,
        *,
        event_at: float,
        pr_number: int = 0,
        title: str = "",
        additions: int = 0,
        deletions: int = 0,
        changed_files: int = 0,
        merged: bool = False,
    ) -> bool:
        """Record one contribution event idempotently.

        Returns True only when a new row was inserted (the daily rollup is
        bumped exactly once per unique event, so backfill re-runs and
        overlapping webhooks never inflate the heatmap). On a duplicate the
        mutable metadata (title/additions/...) is refreshed in place — this is
        how a PR ``synchronize`` that grows the diff updates without
        double-counting.
        """
        if kind not in _CONTRIB_KIND_COLUMNS:
            raise ValueError(f"unknown contribution kind: {kind!r}")
        col = _CONTRIB_KIND_COLUMNS[kind]
        day = _epoch_to_day(event_at)
        now = time.time()
        insert_sql = (
            "INSERT INTO contributions "
            "(contributor_id, owner, repo, kind, external_key, pr_number, title, "
            "additions, deletions, changed_files, merged, event_at, event_day, created_at) "
            "VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}) "
            "ON CONFLICT(owner, repo, kind, external_key) DO NOTHING"
        )
        update_sql = (
            "UPDATE contributions SET title={p}, additions={p}, deletions={p}, "
            "changed_files={p}, merged={p} "
            "WHERE owner={p} AND repo={p} AND kind={p} AND external_key={p}"
        )
        rollup_sql = (
            f"INSERT INTO contribution_days (contributor_id, day, {col}, total) "
            "VALUES ({p}, {p}, 1, 1) "
            f"ON CONFLICT(contributor_id, day) DO UPDATE SET {col}=contribution_days.{col}+1, "
            "total=contribution_days.total+1"
        )
        insert_params = (
            contributor_id,
            owner,
            repo,
            kind,
            external_key,
            pr_number,
            title,
            additions,
            deletions,
            changed_files,
            merged,
            event_at,
            day,
            now,
        )
        update_params = (
            title,
            additions,
            deletions,
            changed_files,
            merged,
            owner,
            repo,
            kind,
            external_key,
        )
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            # SQLite stores booleans as 0/1.
            ins = (*insert_params[:10], int(merged), *insert_params[11:])
            upd = (
                title,
                additions,
                deletions,
                changed_files,
                int(merged),
                owner,
                repo,
                kind,
                external_key,
            )
            cur = self._sqlite_conn.execute(insert_sql.format(p="?"), ins)
            inserted = cur.rowcount == 1
            if inserted:
                self._sqlite_conn.execute(rollup_sql.format(p="?"), (contributor_id, day))
            else:
                self._sqlite_conn.execute(update_sql.format(p="?"), upd)
            self._sqlite_conn.commit()
            return inserted
        assert self._pg_conn is not None
        with self._pg_conn.cursor() as cur:
            cur.execute(insert_sql.format(p="%s"), insert_params)
            inserted = cur.rowcount == 1
            if inserted:
                cur.execute(rollup_sql.format(p="%s"), (contributor_id, day))
            else:
                cur.execute(update_sql.format(p="%s"), update_params)
        self._pg_conn.commit()
        return inserted

    def record_contribution_for_login(
        self,
        provider: str,
        login: str,
        owner: str,
        repo: str,
        kind: str,
        external_key: str,
        *,
        event_at: float,
        external_id: int = 0,
        display_name: str = "",
        avatar_url: str = "",
        is_bot: bool = False,
        pr_number: int = 0,
        title: str = "",
        additions: int = 0,
        deletions: int = 0,
        changed_files: int = 0,
        merged: bool = False,
    ) -> bool:
        """Upsert the contributor then record the event. Single call site shared
        by the webhook handlers and the backfill, which is what keeps the two
        paths idempotent against each other."""
        contributor = self.upsert_contributor(
            provider,
            login,
            external_id=external_id,
            display_name=display_name,
            avatar_url=avatar_url,
            is_bot=is_bot,
        )
        return self.record_contribution(
            contributor.id,
            owner,
            repo,
            kind,
            external_key,
            event_at=event_at,
            pr_number=pr_number,
            title=title,
            additions=additions,
            deletions=deletions,
            changed_files=changed_files,
            merged=merged,
        )

    # Whitelisted sort -> aggregate ORDER BY expression. Full expressions
    # (not output aliases) so it works on both SQLite and Postgres.
    _CONTRIB_SORTS = {
        "commits": "SUM(CASE WHEN co.kind='commit' THEN 1 ELSE 0 END) DESC",
        "prs": "(SUM(CASE WHEN co.kind='pr_opened' THEN 1 ELSE 0 END) "
        "+ SUM(CASE WHEN co.kind='pr_merged' THEN 1 ELSE 0 END)) DESC",
        "reviews": "SUM(CASE WHEN co.kind='review' THEN 1 ELSE 0 END) DESC",
        "recent": "MAX(co.event_at) DESC",
        "additions": "SUM(co.additions) DESC",
    }

    def list_contributors(
        self,
        sort: str = "commits",
        since: float | None = None,
        include_bots: bool = False,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Cross-repo leaderboard: one aggregated row per contributor."""
        order = self._CONTRIB_SORTS.get(sort, self._CONTRIB_SORTS["commits"])
        params: list[Any] = []
        join_cond = "co.contributor_id = c.id"
        if since is not None:
            join_cond += " AND co.event_at >= ?"
            params.append(since)
        where = "" if include_bots else f"WHERE c.is_bot = {self._false_sql}"
        having = "HAVING COUNT(co.id) > 0" if since is not None else ""
        params.append(limit)
        sql = (
            "SELECT c.id, c.provider, c.external_login, c.display_name, c.avatar_url, c.is_bot, "
            "SUM(CASE WHEN co.kind='pr_opened' THEN 1 ELSE 0 END) AS prs_opened, "
            "SUM(CASE WHEN co.kind='pr_merged' THEN 1 ELSE 0 END) AS prs_merged, "
            "SUM(CASE WHEN co.kind='commit' THEN 1 ELSE 0 END) AS commits, "
            "SUM(CASE WHEN co.kind='review' THEN 1 ELSE 0 END) AS reviews, "
            "COALESCE(SUM(co.additions), 0) AS additions, "
            "COALESCE(SUM(co.deletions), 0) AS deletions, "
            "MAX(co.event_at) AS last_active, "
            "COUNT(DISTINCT co.owner || '/' || co.repo) AS repos_touched "
            f"FROM contributors c LEFT JOIN contributions co ON {join_cond} "
            f"{where} "
            "GROUP BY c.id, c.provider, c.external_login, c.display_name, c.avatar_url, c.is_bot "
            f"{having} ORDER BY {order} LIMIT ?"
        )
        rows = self._rows(sql, tuple(params))
        return [
            {
                "id": r[0],
                "provider": r[1],
                "login": r[2],
                "display_name": r[3] or "",
                "avatar_url": r[4] or "",
                "is_bot": bool(r[5]),
                "prs_opened": int(r[6] or 0),
                "prs_merged": int(r[7] or 0),
                "commits": int(r[8] or 0),
                "reviews": int(r[9] or 0),
                "additions": int(r[10] or 0),
                "deletions": int(r[11] or 0),
                "last_active": (float(r[12]) if r[12] else None),
                "repos_touched": int(r[13] or 0),
            }
            for r in rows
        ]

    def aggregate_contributions(
        self, start: float, end: float, include_bots: bool = False
    ) -> dict[str, Any]:
        """Org-wide contribution counts in the half-open window [start, end).

        Powers period-over-period trend widgets (this week vs last week)."""
        where = "WHERE co.event_at >= ? AND co.event_at < ?"
        if not include_bots:
            where += f" AND c.is_bot = {self._false_sql}"
        rows = self._rows(
            "SELECT "
            "SUM(CASE WHEN co.kind='commit' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN co.kind='pr_opened' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN co.kind='pr_merged' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN co.kind='review' THEN 1 ELSE 0 END), "
            "COALESCE(SUM(co.additions), 0), "
            "COUNT(DISTINCT co.contributor_id) "
            "FROM contributions co JOIN contributors c ON c.id = co.contributor_id "
            f"{where}",
            (start, end),
        )
        r = rows[0] if rows else (0, 0, 0, 0, 0, 0)
        return {
            "commits": int(r[0] or 0),
            "prs_opened": int(r[1] or 0),
            "prs_merged": int(r[2] or 0),
            "reviews": int(r[3] or 0),
            "additions": int(r[4] or 0),
            "contributors": int(r[5] or 0),
        }

    def get_contributor_days(
        self, contributor_id: int, start_day: str, end_day: str
    ) -> list[ContributionDay]:
        """Daily rollup rows in [start_day, end_day] (inclusive) — the heatmap."""
        rows = self._rows(
            "SELECT day, commits, prs_opened, prs_merged, reviews, total "
            "FROM contribution_days WHERE contributor_id=? AND day>=? AND day<=? ORDER BY day",
            (contributor_id, start_day, end_day),
        )
        return [
            ContributionDay(
                day=r[0],
                commits=int(r[1] or 0),
                prs_opened=int(r[2] or 0),
                prs_merged=int(r[3] or 0),
                reviews=int(r[4] or 0),
                total=int(r[5] or 0),
            )
            for r in rows
        ]

    def get_contributor_repo_breakdown(
        self, contributor_id: int, since: float | None = None
    ) -> list[dict[str, Any]]:
        """Per-repo activity counts for one contributor (where they contribute)."""
        params: list[Any] = [contributor_id]
        since_clause = ""
        if since is not None:
            since_clause = " AND event_at >= ?"
            params.append(since)
        rows = self._rows(
            "SELECT owner, repo, "
            "SUM(CASE WHEN kind='commit' THEN 1 ELSE 0 END) AS commits, "
            "SUM(CASE WHEN kind='pr_opened' THEN 1 ELSE 0 END) AS prs_opened, "
            "SUM(CASE WHEN kind='pr_merged' THEN 1 ELSE 0 END) AS prs_merged, "
            "SUM(CASE WHEN kind='review' THEN 1 ELSE 0 END) AS reviews "
            f"FROM contributions WHERE contributor_id=?{since_clause} "
            "GROUP BY owner, repo ORDER BY commits DESC, prs_merged DESC",
            tuple(params),
        )
        return [
            {
                "owner": r[0],
                "repo": r[1],
                "commits": int(r[2] or 0),
                "prs_opened": int(r[3] or 0),
                "prs_merged": int(r[4] or 0),
                "reviews": int(r[5] or 0),
            }
            for r in rows
        ]

    def get_contributor_totals(
        self, contributor_id: int, since: float | None = None
    ) -> dict[str, Any]:
        """Aggregate totals for one contributor across all repos."""
        params: list[Any] = [contributor_id]
        since_clause = ""
        if since is not None:
            since_clause = " AND event_at >= ?"
            params.append(since)
        rows = self._rows(
            "SELECT "
            "SUM(CASE WHEN kind='pr_opened' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN kind='pr_merged' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN kind='commit' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN kind='review' THEN 1 ELSE 0 END), "
            "COALESCE(SUM(additions), 0), COALESCE(SUM(deletions), 0), "
            "MAX(event_at), COUNT(DISTINCT owner || '/' || repo) "
            f"FROM contributions WHERE contributor_id=?{since_clause}",
            tuple(params),
        )
        r = rows[0] if rows else (0, 0, 0, 0, 0, 0, None, 0)
        return {
            "prs_opened": int(r[0] or 0),
            "prs_merged": int(r[1] or 0),
            "commits": int(r[2] or 0),
            "reviews": int(r[3] or 0),
            "additions": int(r[4] or 0),
            "deletions": int(r[5] or 0),
            "last_active": (float(r[6]) if r[6] else None),
            "repos_touched": int(r[7] or 0),
        }

    # ── Review insights (pull_requests + pr_reviewers) ──

    def upsert_pull_request(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        author: str = "",
        title: str = "",
        url: str = "",
        state: str = "open",
        draft: bool = False,
        created_at: float = 0.0,
        updated_at: float = 0.0,
        merged_at: float = 0.0,
        closed_at: float = 0.0,
    ) -> None:
        """Insert/update a PR's lifecycle row. Never clears first_review_at
        (set separately) or an earlier created_at."""
        self._exec(
            "INSERT INTO pull_requests "
            "(owner, repo, number, author, title, url, state, draft, "
            "created_at, updated_at, merged_at, closed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(owner, repo, number) DO UPDATE SET "
            "author=CASE WHEN excluded.author!='' THEN excluded.author ELSE pull_requests.author END, "
            "title=CASE WHEN excluded.title!='' THEN excluded.title ELSE pull_requests.title END, "
            "url=CASE WHEN excluded.url!='' THEN excluded.url ELSE pull_requests.url END, "
            "state=excluded.state, draft=excluded.draft, updated_at=excluded.updated_at, "
            "created_at=CASE WHEN pull_requests.created_at=0 THEN excluded.created_at ELSE pull_requests.created_at END, "
            "merged_at=CASE WHEN excluded.merged_at>0 THEN excluded.merged_at ELSE pull_requests.merged_at END, "
            "closed_at=CASE WHEN excluded.closed_at>0 THEN excluded.closed_at ELSE pull_requests.closed_at END",
            (
                owner,
                repo,
                number,
                author,
                title,
                url,
                state,
                int(draft),
                created_at,
                updated_at,
                merged_at,
                closed_at,
            ),
        )

    def set_pr_first_review(self, owner: str, repo: str, number: int, ts: float) -> None:
        """Record the earliest review time on a PR (no-op if an earlier one exists)."""
        if ts <= 0:
            return
        self._exec(
            "UPDATE pull_requests SET first_review_at=? "
            "WHERE owner=? AND repo=? AND number=? AND (first_review_at=0 OR ?<first_review_at)",
            (ts, owner, repo, number, ts),
        )

    def upsert_pr_reviewer(
        self,
        owner: str,
        repo: str,
        number: int,
        reviewer: str,
        *,
        requested_at: float = 0.0,
        responded_at: float = 0.0,
        state: str = "",
        bare_approval: int = 0,
    ) -> None:
        """Merge a review request and/or a review into the (PR, reviewer) row:
        keeps the earliest request + earliest response, and the latest state.

        ``bare_approval`` (rubber-stamp flag) is only updated on a review-
        recording call (``responded_at>0``), so a later bare review-*request*
        upsert never clobbers a recorded classification."""
        self._exec(
            "INSERT INTO pr_reviewers "
            "(owner, repo, pr_number, reviewer, requested_at, responded_at, state, bare_approval) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(owner, repo, pr_number, reviewer) DO UPDATE SET "
            "requested_at=CASE "
            "  WHEN pr_reviewers.requested_at=0 THEN excluded.requested_at "
            "  WHEN excluded.requested_at>0 AND excluded.requested_at<pr_reviewers.requested_at THEN excluded.requested_at "
            "  ELSE pr_reviewers.requested_at END, "
            "responded_at=CASE "
            "  WHEN excluded.responded_at=0 THEN pr_reviewers.responded_at "
            "  WHEN pr_reviewers.responded_at=0 OR excluded.responded_at<pr_reviewers.responded_at THEN excluded.responded_at "
            "  ELSE pr_reviewers.responded_at END, "
            "state=CASE WHEN excluded.state!='' THEN excluded.state ELSE pr_reviewers.state END, "
            "bare_approval=CASE WHEN excluded.responded_at>0 THEN excluded.bare_approval ELSE pr_reviewers.bare_approval END",
            (owner, repo, number, reviewer, requested_at, responded_at, state, bare_approval),
        )

    def remove_pr_reviewer(self, owner: str, repo: str, number: int, reviewer: str) -> None:
        """Drop a review request (review_request_removed) only if unanswered."""
        self._exec(
            "DELETE FROM pr_reviewers WHERE owner=? AND repo=? AND pr_number=? "
            "AND reviewer=? AND responded_at=0",
            (owner, repo, number, reviewer),
        )

    def get_open_pull_requests(self) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT owner, repo, number, author, title, url, draft, "
            "created_at, updated_at, first_review_at "
            "FROM pull_requests WHERE state='open' ORDER BY created_at ASC"
        )
        return [
            {
                "owner": r[0],
                "repo": r[1],
                "number": r[2],
                "author": r[3],
                "title": r[4],
                "url": r[5],
                "draft": bool(r[6]),
                "created_at": r[7] or 0.0,
                "updated_at": r[8] or 0.0,
                "first_review_at": r[9] or 0.0,
            }
            for r in rows
        ]

    def get_open_pr_reviewers(self) -> list[dict[str, Any]]:
        rows = self._rows(
            "SELECT r.owner, r.repo, r.pr_number, r.reviewer, r.requested_at, r.responded_at, r.state "
            "FROM pr_reviewers r JOIN pull_requests p "
            "ON p.owner=r.owner AND p.repo=r.repo AND p.number=r.pr_number "
            "WHERE p.state='open'"
        )
        return [
            {
                "owner": r[0],
                "repo": r[1],
                "number": r[2],
                "reviewer": r[3],
                "requested_at": r[4] or 0.0,
                "responded_at": r[5] or 0.0,
                "state": r[6] or "",
            }
            for r in rows
        ]

    def get_reviewer_activity_rows(self) -> list[dict[str, Any]]:
        """All (reviewer, request/response times, PR state) rows — for the
        responsiveness/bottleneck stats, aggregated in Python."""
        rows = self._rows(
            "SELECT r.reviewer, r.requested_at, r.responded_at, p.state, r.state, r.bare_approval "
            "FROM pr_reviewers r JOIN pull_requests p "
            "ON p.owner=r.owner AND p.repo=r.repo AND p.number=r.pr_number"
        )
        return [
            {
                "reviewer": r[0],
                "requested_at": r[1] or 0.0,
                "responded_at": r[2] or 0.0,
                "pr_state": r[3],
                "review_state": r[4] or "",
                "bare_approval": int(r[5] or 0),
            }
            for r in rows
        ]

    def get_throughput_rows(self) -> list[dict[str, Any]]:
        """(created_at, first_review_at, merged_at) for every PR — medians
        computed in Python so it works the same on SQLite and Postgres."""
        rows = self._rows("SELECT created_at, first_review_at, merged_at FROM pull_requests")
        return [
            {"created_at": r[0] or 0.0, "first_review_at": r[1] or 0.0, "merged_at": r[2] or 0.0}
            for r in rows
        ]

    def get_merged_pr_quality(self, start: float, end: float) -> list[dict[str, Any]]:
        """For PRs merged in [start, end): whether they were reviewed and whether
        a human approved — for the 'humans approve + merge' health signal."""
        rows = self._rows(
            "SELECT p.created_at, p.merged_at, p.first_review_at, "
            "CASE WHEN EXISTS (SELECT 1 FROM pr_reviewers r "
            "  WHERE r.owner=p.owner AND r.repo=p.repo AND r.pr_number=p.number "
            "  AND r.state='approved') THEN 1 ELSE 0 END "
            "FROM pull_requests p WHERE p.merged_at >= ? AND p.merged_at < ?",
            (start, end),
        )
        return [
            {
                "created_at": r[0] or 0.0,
                "merged_at": r[1] or 0.0,
                "first_review_at": r[2] or 0.0,
                "approved": bool(r[3]),
            }
            for r in rows
        ]

    def close(self) -> None:
        if self._sqlite_conn:
            self._sqlite_conn.close()
        if self._pg_conn:
            self._pg_conn.close()
