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
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    theme TEXT NOT NULL DEFAULT 'dark',
    created_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL DEFAULT 0,
    expires_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS repos (
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
    PRIMARY KEY (owner, repo)
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
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    total_paths TEXT NOT NULL DEFAULT '[]',     -- JSON array of all paths in PR
    reviewed_paths TEXT NOT NULL DEFAULT '[]',  -- JSON array of paths reviewed so far
    skipped_paths TEXT NOT NULL DEFAULT '[]',   -- JSON array of paths intentionally skipped (low priority)
    chunk_index INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (owner, repo, pr_number)
);
"""

_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    theme TEXT NOT NULL DEFAULT 'dark',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS repos (
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
    PRIMARY KEY (owner, repo)
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
    owner TEXT NOT NULL,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    total_paths TEXT NOT NULL DEFAULT '[]',
    reviewed_paths TEXT NOT NULL DEFAULT '[]',
    skipped_paths TEXT NOT NULL DEFAULT '[]',
    chunk_index INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (owner, repo, pr_number)
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
    status: str = "pending"  # pending, indexing, ready, failed
    index_mode: str = "full"  # full, light, none
    files_indexed: int = 0
    file_count_estimate: int = 0
    error: str = ""
    installation_id: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0


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
        self._sqlite_conn.commit()
        logger.info("App database: SQLite at %s", db_path)

    def _init_postgres(self, url: str) -> None:
        self._backend = "postgres"
        try:
            import psycopg

            self._pg_conn = psycopg.connect(url)
            self._pg_conn.autocommit = True
            with self._pg_conn.cursor() as cur:
                cur.execute(_PG_SCHEMA)
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
            assert self._pg_conn is not None
            with self._pg_conn.cursor() as cur:
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
        assert self._pg_conn is not None
        with self._pg_conn.cursor() as cur:
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
        assert self._pg_conn is not None
        with self._pg_conn.cursor() as cur:
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
            assert self._pg_conn is not None
            with self._pg_conn.cursor() as cur:
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
        assert self._pg_conn is not None
        with self._pg_conn.cursor() as cur:
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
            assert self._pg_conn is not None
            with self._pg_conn.cursor() as cur:
                cur.execute("UPDATE users SET theme = %s WHERE id = %s", (theme, user_id))

    def list_users(self) -> list[User]:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            rows = self._sqlite_conn.execute(
                "SELECT id, username, is_admin, created_at FROM users ORDER BY id"
            ).fetchall()
            return [
                User(id=r[0], username=r[1], is_admin=bool(r[2]), created_at=r[3]) for r in rows
            ]
        assert self._pg_conn is not None
        with self._pg_conn.cursor() as cur:
            cur.execute("SELECT id, username, is_admin FROM users ORDER BY id")
            return [User(id=r[0], username=r[1], is_admin=bool(r[2])) for r in cur.fetchall()]

    def delete_user(self, user_id: int) -> None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            self._sqlite_conn.commit()
        else:
            assert self._pg_conn is not None
            with self._pg_conn.cursor() as cur:
                cur.execute("DELETE FROM users WHERE id = %s", (user_id,))

    def delete_session(self, token: str) -> None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            self._sqlite_conn.commit()
        else:
            assert self._pg_conn is not None
            with self._pg_conn.cursor() as cur:
                cur.execute("DELETE FROM sessions WHERE token = %s", (token,))

    # ── Repos ──

    def register_repo(self, owner: str, repo: str, installation_id: int = 0) -> RepoRecord:
        now = time.time()
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "INSERT INTO repos (owner, repo, installation_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(owner, repo) DO UPDATE SET "
                "installation_id=excluded.installation_id, updated_at=excluded.updated_at",
                (owner, repo, installation_id, now, now),
            )
            self._sqlite_conn.commit()
        else:
            assert self._pg_conn is not None
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO repos (owner, repo, installation_id) VALUES (%s, %s, %s) "
                    "ON CONFLICT(owner, repo) DO UPDATE SET installation_id=EXCLUDED.installation_id, updated_at=NOW()",
                    (owner, repo, installation_id),
                )
        return RepoRecord(
            owner=owner, repo=repo, installation_id=installation_id, created_at=now, updated_at=now
        )

    def set_repo_status(
        self, owner: str, repo: str, status: str, files_indexed: int = 0, error: str = ""
    ) -> None:
        now = time.time()
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "UPDATE repos SET status=?, files_indexed=?, error=?, updated_at=? WHERE owner=? AND repo=?",
                (status, files_indexed, error, now, owner, repo),
            )
            self._sqlite_conn.commit()
        else:
            assert self._pg_conn is not None
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    "UPDATE repos SET status=%s, files_indexed=%s, error=%s, updated_at=NOW() WHERE owner=%s AND repo=%s",
                    (status, files_indexed, error, owner, repo),
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
            assert self._pg_conn is not None
            with self._pg_conn.cursor() as cur:
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
        assert self._pg_conn is not None
        with self._pg_conn.cursor() as cur:
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
            assert self._pg_conn is not None
            with self._pg_conn.cursor() as cur:
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
        assert self._pg_conn is not None
        with self._pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM repos WHERE installation_id=%s",
                (installation_id,),
            )
            return cur.rowcount

    def delete_repo(self, owner: str, repo: str) -> None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "DELETE FROM repos WHERE owner=? AND repo=?",
                (owner, repo),
            )
            self._sqlite_conn.commit()
        else:
            assert self._pg_conn is not None
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM repos WHERE owner=%s AND repo=%s",
                    (owner, repo),
                )

    def set_repo_file_count(self, owner: str, repo: str, count: int) -> None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "UPDATE repos SET file_count_estimate=? WHERE owner=? AND repo=?",
                (count, owner, repo),
            )
            self._sqlite_conn.commit()
        else:
            assert self._pg_conn is not None
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    "UPDATE repos SET file_count_estimate=%s WHERE owner=%s AND repo=%s",
                    (count, owner, repo),
                )

    def set_repo_index_mode(self, owner: str, repo: str, mode: str) -> None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "UPDATE repos SET index_mode=? WHERE owner=? AND repo=?",
                (mode, owner, repo),
            )
            self._sqlite_conn.commit()
        else:
            assert self._pg_conn is not None
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    "UPDATE repos SET index_mode=%s WHERE owner=%s AND repo=%s",
                    (mode, owner, repo),
                )

    def list_repos(self) -> list[RepoRecord]:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            rows = self._sqlite_conn.execute(
                "SELECT owner, repo, status, index_mode, files_indexed, file_count_estimate, error, installation_id, created_at, updated_at "
                "FROM repos ORDER BY owner, repo"
            ).fetchall()
            return [
                RepoRecord(
                    owner=r[0],
                    repo=r[1],
                    status=r[2],
                    index_mode=r[3],
                    files_indexed=r[4],
                    file_count_estimate=r[5],
                    error=r[6],
                    installation_id=r[7],
                    created_at=r[8],
                    updated_at=r[9],
                )
                for r in rows
            ]
        assert self._pg_conn is not None
        with self._pg_conn.cursor() as cur:
            cur.execute(
                "SELECT owner, repo, status, index_mode, files_indexed, file_count_estimate, error, installation_id "
                "FROM repos ORDER BY owner, repo"
            )
            return [
                RepoRecord(
                    owner=r[0],
                    repo=r[1],
                    status=r[2],
                    index_mode=r[3],
                    files_indexed=r[4],
                    file_count_estimate=r[5],
                    error=r[6],
                    installation_id=r[7],
                )
                for r in cur.fetchall()
            ]

    def get_repo(self, owner: str, repo: str) -> RepoRecord | None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            row = self._sqlite_conn.execute(
                "SELECT owner, repo, status, index_mode, files_indexed, file_count_estimate, error, installation_id, created_at, updated_at "
                "FROM repos WHERE owner=? AND repo=?",
                (owner, repo),
            ).fetchone()
            if row:
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
                )
            return None
        assert self._pg_conn is not None
        with self._pg_conn.cursor() as cur:
            cur.execute(
                "SELECT owner, repo, status, index_mode, files_indexed, file_count_estimate, error, installation_id "
                "FROM repos WHERE owner=%s AND repo=%s",
                (owner, repo),
            )
            row = cur.fetchone()
            if row:
                return RepoRecord(
                    owner=row[0],
                    repo=row[1],
                    status=row[2],
                    index_mode=row[3],
                    files_indexed=row[4],
                    file_count_estimate=row[5],
                    error=row[6],
                    installation_id=row[7],
                )
            return None

    # ── Settings ──

    def get_setting(self, key: str) -> str | None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            row = self._sqlite_conn.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else None
        assert self._pg_conn is not None
        with self._pg_conn.cursor() as cur:
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
            assert self._pg_conn is not None
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                    (key, value),
                )

    @property
    def setup_complete(self) -> bool:
        return self.get_setting("setup_complete") == "true"

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
            with self._pg_conn.cursor() as cur:
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
            with self._pg_conn.cursor() as cur:
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
                with self._pg_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE global_rules SET title=%s, content=%s, updated_at=NOW() WHERE id=%s",
                        (title, content, rule_id),
                    )
            else:
                with self._pg_conn.cursor() as cur:
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
            with self._pg_conn.cursor() as cur:
                cur.execute("DELETE FROM global_rules WHERE id=%s", (rule_id,))

    def toggle_global_rule(self, rule_id: int) -> GlobalRule | None:
        if self._backend == "sqlite":
            self._sqlite_conn.execute(
                "UPDATE global_rules SET enabled = NOT enabled, updated_at = ? WHERE id = ?",
                ((__import__("time")).time(), rule_id),
            )
            self._sqlite_conn.commit()
        else:
            with self._pg_conn.cursor() as cur:
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

    def upsert_pr_review_progress(self, progress: PRReviewProgress) -> None:
        """Insert or update progress for a single PR. Idempotent."""
        now = time.time()
        total = json.dumps(progress.total_paths)
        reviewed = json.dumps(progress.reviewed_paths)
        skipped = json.dumps(progress.skipped_paths)
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "INSERT INTO pr_review_progress "
                "(owner, repo, pr_number, total_paths, reviewed_paths, "
                "skipped_paths, chunk_index, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(owner, repo, pr_number) DO UPDATE SET "
                "total_paths=excluded.total_paths, "
                "reviewed_paths=excluded.reviewed_paths, "
                "skipped_paths=excluded.skipped_paths, "
                "chunk_index=excluded.chunk_index, "
                "updated_at=excluded.updated_at",
                (
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
            assert self._pg_conn is not None
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pr_review_progress "
                    "(owner, repo, pr_number, total_paths, reviewed_paths, "
                    "skipped_paths, chunk_index, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, NOW()) "
                    "ON CONFLICT (owner, repo, pr_number) DO UPDATE SET "
                    "total_paths=EXCLUDED.total_paths, "
                    "reviewed_paths=EXCLUDED.reviewed_paths, "
                    "skipped_paths=EXCLUDED.skipped_paths, "
                    "chunk_index=EXCLUDED.chunk_index, "
                    "updated_at=NOW()",
                    (
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
    ) -> PRReviewProgress | None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            row = self._sqlite_conn.execute(
                "SELECT total_paths, reviewed_paths, skipped_paths, chunk_index, updated_at "
                "FROM pr_review_progress WHERE owner=? AND repo=? AND pr_number=?",
                (owner, repo, pr_number),
            ).fetchone()
        else:
            assert self._pg_conn is not None
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    "SELECT total_paths, reviewed_paths, skipped_paths, chunk_index, "
                    "EXTRACT(EPOCH FROM updated_at) "
                    "FROM pr_review_progress WHERE owner=%s AND repo=%s AND pr_number=%s",
                    (owner, repo, pr_number),
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

    def delete_pr_review_progress(self, owner: str, repo: str, pr_number: int) -> None:
        if self._backend == "sqlite":
            assert self._sqlite_conn is not None
            self._sqlite_conn.execute(
                "DELETE FROM pr_review_progress WHERE owner=? AND repo=? AND pr_number=?",
                (owner, repo, pr_number),
            )
            self._sqlite_conn.commit()
        else:
            assert self._pg_conn is not None
            with self._pg_conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM pr_review_progress WHERE owner=%s AND repo=%s AND pr_number=%s",
                    (owner, repo, pr_number),
                )

    def close(self) -> None:
        if self._sqlite_conn:
            self._sqlite_conn.close()
        if self._pg_conn:
            self._pg_conn.close()
