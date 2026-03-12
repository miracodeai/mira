"""Tests for the review-time context builder."""

from __future__ import annotations

import pytest

from mira.index.context import build_code_context
from mira.index.store import (
    DirectorySummary,
    FileSummary,
    IndexStore,
    SymbolInfo,
)


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "ctx_test.db")
    s = IndexStore(db_path)
    yield s
    s.close()


@pytest.fixture
def populated_store(store):
    """Store with auth service, db models, and API routes."""
    auth = FileSummary(
        path="src/auth/service.py",
        language="python",
        summary="Handles user authentication and session management.",
        symbols=[
            SymbolInfo("authenticate", "function", "def authenticate(token: str) -> Session", "Validates JWT"),
            SymbolInfo("revoke_session", "function", "def revoke_session(session_id: str) -> None", "Invalidates session"),
        ],
        imports=["src/db/models.py", "src/config.py"],
        content_hash="h1",
    )
    models = FileSummary(
        path="src/db/models.py",
        language="python",
        summary="SQLAlchemy ORM models for users, sessions, and permissions.",
        symbols=[
            SymbolInfo("Session", "class", "class Session", "ORM model for sessions"),
            SymbolInfo("User", "class", "class User", "ORM model for users"),
        ],
        content_hash="h2",
    )
    routes = FileSummary(
        path="src/api/routes.py",
        language="python",
        summary="API route handlers.",
        symbols=[
            SymbolInfo("handle_request", "function", "def handle_request()", "Main request handler"),
        ],
        symbol_refs=[("handle_request", "src/auth/service.py", "authenticate")],
        content_hash="h3",
    )
    store.upsert_batch([auth, models, routes])
    store.upsert_directory(DirectorySummary(path="src/auth", summary="Authentication middleware and session management.", file_count=2))
    store.upsert_directory(DirectorySummary(path="src/db", summary="Database models and connection management.", file_count=4))
    return store


class TestBuildCodeContext:
    def test_includes_changed_file_summaries(self, populated_store):
        ctx = build_code_context(["src/auth/service.py"], populated_store)
        assert "src/auth/service.py" in ctx
        assert "Handles user authentication" in ctx
        assert "authenticate" in ctx

    def test_includes_directory_summaries(self, populated_store):
        ctx = build_code_context(["src/auth/service.py"], populated_store)
        assert "Repository Structure" in ctx
        assert "Authentication middleware" in ctx

    def test_includes_related_files(self, populated_store):
        ctx = build_code_context(["src/auth/service.py"], populated_store)
        assert "Related Files" in ctx
        assert "src/db/models.py" in ctx

    def test_includes_blast_radius(self, populated_store):
        ctx = build_code_context(["src/auth/service.py"], populated_store)
        assert "Blast Radius" in ctx
        assert "src/api/routes.py" in ctx
        assert "handle_request" in ctx

    def test_empty_index_returns_header_only(self, store):
        ctx = build_code_context(["nonexistent.py"], store)
        assert "Codebase Context" in ctx
        # Should not crash, just have minimal content
        assert "Changed Files" not in ctx

    def test_respects_token_budget(self, populated_store):
        # Very small budget should truncate
        ctx = build_code_context(["src/auth/service.py"], populated_store, token_budget=50)
        assert "truncated" in ctx

    def test_no_self_reference_in_related(self, populated_store):
        """Changed files should not appear in the 'related files' section."""
        ctx = build_code_context(["src/auth/service.py", "src/db/models.py"], populated_store)
        # models.py is imported by service.py but since it's also changed, it shouldn't be in "Related Files"
        lines = ctx.split("\n")
        in_related = False
        for line in lines:
            if "Related Files" in line:
                in_related = True
            elif line.startswith("###"):
                in_related = False
            if in_related and "src/db/models.py" in line:
                pytest.fail("Changed file src/db/models.py should not appear in Related Files section")
