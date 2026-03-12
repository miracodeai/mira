"""Tests for the IndexStore SQLite storage layer."""

from __future__ import annotations

import os
import tempfile

import pytest

from mira.index.store import (
    BlastRadiusEntry,
    DirectorySummary,
    FileSummary,
    IndexStore,
    SymbolInfo,
)


@pytest.fixture
def store(tmp_path):
    """Create a temporary IndexStore."""
    db_path = str(tmp_path / "test.db")
    s = IndexStore(db_path)
    yield s
    s.close()


@pytest.fixture
def sample_summary() -> FileSummary:
    return FileSummary(
        path="src/auth/service.py",
        language="python",
        summary="Handles user authentication and session management.",
        symbols=[
            SymbolInfo(
                name="authenticate",
                kind="function",
                signature="def authenticate(token: str) -> Session",
                description="Validates JWT and returns active session",
            ),
            SymbolInfo(
                name="revoke_session",
                kind="function",
                signature="def revoke_session(session_id: str) -> None",
                description="Invalidates a session",
            ),
        ],
        imports=["src/db/models.py", "src/config.py"],
        symbol_refs=[
            ("authenticate", "src/db/models.py", "get_user"),
            ("authenticate", "src/utils/jwt.py", "decode"),
        ],
        content_hash="abc123",
    )


class TestIndexStoreBasic:
    def test_upsert_and_get_summary(self, store, sample_summary):
        store.upsert_summary(sample_summary)
        result = store.get_summary("src/auth/service.py")

        assert result is not None
        assert result.path == "src/auth/service.py"
        assert result.language == "python"
        assert result.summary == "Handles user authentication and session management."
        assert len(result.symbols) == 2
        assert result.symbols[0].name == "authenticate"
        assert result.symbols[1].name == "revoke_session"
        assert sorted(result.imports) == ["src/config.py", "src/db/models.py"]
        assert result.content_hash == "abc123"

    def test_get_summary_not_found(self, store):
        assert store.get_summary("nonexistent.py") is None

    def test_get_summaries_multiple(self, store, sample_summary):
        store.upsert_summary(sample_summary)
        other = FileSummary(
            path="src/config.py", language="python",
            summary="Configuration loader.", content_hash="def456",
        )
        store.upsert_summary(other)

        result = store.get_summaries(["src/auth/service.py", "src/config.py", "missing.py"])
        assert len(result) == 2
        assert "src/auth/service.py" in result
        assert "src/config.py" in result

    def test_upsert_updates_existing(self, store, sample_summary):
        store.upsert_summary(sample_summary)
        updated = FileSummary(
            path="src/auth/service.py", language="python",
            summary="Updated summary.", content_hash="new_hash",
        )
        store.upsert_summary(updated)

        result = store.get_summary("src/auth/service.py")
        assert result is not None
        assert result.summary == "Updated summary."
        assert result.content_hash == "new_hash"
        assert result.symbols == []  # symbols replaced with empty

    def test_upsert_batch(self, store):
        summaries = [
            FileSummary(path=f"file{i}.py", language="python", summary=f"File {i}.", content_hash=f"hash{i}")
            for i in range(5)
        ]
        store.upsert_batch(summaries)
        assert len(store.all_paths()) == 5

    def test_remove_paths(self, store, sample_summary):
        store.upsert_summary(sample_summary)
        assert store.get_summary("src/auth/service.py") is not None

        store.remove_paths(["src/auth/service.py"])
        assert store.get_summary("src/auth/service.py") is None

    def test_all_paths(self, store):
        for i in range(3):
            store.upsert_summary(
                FileSummary(path=f"file{i}.py", language="python", summary="", content_hash=f"h{i}")
            )
        paths = store.all_paths()
        assert paths == {"file0.py", "file1.py", "file2.py"}


class TestIndexStoreDependencies:
    def test_get_dependents(self, store, sample_summary):
        store.upsert_summary(sample_summary)
        deps = store.get_dependents("src/db/models.py")
        assert "src/auth/service.py" in deps

    def test_get_dependents_empty(self, store):
        assert store.get_dependents("nonexistent.py") == []

    def test_get_call_graph(self, store, sample_summary):
        store.upsert_summary(sample_summary)
        callers = store.get_call_graph("src/db/models.py", "get_user")
        assert ("src/auth/service.py", "authenticate") in callers

    def test_get_reverse_deps(self, store):
        # A -> B -> C (import chain)
        a = FileSummary(path="a.py", language="python", summary="", content_hash="h1", imports=["b.py"])
        b = FileSummary(path="b.py", language="python", summary="", content_hash="h2", imports=["c.py"])
        c = FileSummary(path="c.py", language="python", summary="", content_hash="h3")
        store.upsert_batch([a, b, c])

        # Reverse deps of c.py: b.py imports c.py, a.py imports b.py
        rdeps = store.get_reverse_deps("c.py", max_depth=3)
        assert "b.py" in rdeps
        assert "a.py" in rdeps


class TestIndexStoreBlastRadius:
    def test_blast_radius(self, store):
        # Setup: routes.py calls authenticate() from auth/service.py
        auth = FileSummary(
            path="src/auth/service.py", language="python",
            summary="Auth service.", content_hash="h1",
            symbols=[SymbolInfo("authenticate", "function", "def authenticate()", "Auth")],
        )
        routes = FileSummary(
            path="src/api/routes.py", language="python",
            summary="API routes.", content_hash="h2",
            symbols=[SymbolInfo("handle_request", "function", "def handle_request()", "Handler")],
            symbol_refs=[("handle_request", "src/auth/service.py", "authenticate")],
        )
        store.upsert_batch([auth, routes])

        radius = store.get_blast_radius(["src/auth/service.py"])
        assert len(radius) == 1
        assert radius[0].path == "src/api/routes.py"
        assert "handle_request" in radius[0].affected_symbols
        assert radius[0].depth == 1

    def test_blast_radius_empty(self, store):
        store.upsert_summary(
            FileSummary(path="solo.py", language="python", summary="Isolated.", content_hash="h1")
        )
        assert store.get_blast_radius(["solo.py"]) == []


class TestIndexStoreDirectories:
    def test_upsert_and_get_directory(self, store):
        ds = DirectorySummary(path="src/auth", summary="Auth middleware.", file_count=3)
        store.upsert_directory(ds)

        result = store.get_directory_summary("src/auth")
        assert result is not None
        assert result.path == "src/auth"
        assert result.summary == "Auth middleware."
        assert result.file_count == 3

    def test_get_directory_summaries(self, store):
        store.upsert_directory(DirectorySummary(path="src/auth", summary="Auth.", file_count=3))
        store.upsert_directory(DirectorySummary(path="src/db", summary="Database.", file_count=4))

        result = store.get_directory_summaries(["src/auth", "src/db", "missing"])
        assert len(result) == 2


class TestIndexStoreOpen:
    def test_open_creates_db(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
        store = IndexStore.open("testowner", "testrepo")
        store.upsert_summary(
            FileSummary(path="test.py", language="python", summary="Test.", content_hash="h")
        )
        store.close()

        # Re-open and verify data persisted
        store2 = IndexStore.open("testowner", "testrepo")
        assert store2.get_summary("test.py") is not None
        store2.close()
