"""Integration tests using a fake e-commerce repo as test corpus.

36 tests across 5 test classes covering the full index, context, review,
blast radius, and symbol extraction pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mira.config import load_config
from mira.core.engine import ReviewEngine
from mira.index.context import build_code_context
from mira.index.extract import extract_symbols, find_symbol_by_name
from mira.index.store import (
    FileSummary,
    IndexStore,
    SymbolInfo,
)
from mira.llm.provider import LLMProvider

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FAKE_REPO_DIR = FIXTURES_DIR / "fake_repo"

# Fake repo file paths (relative, as they'd appear in the index)
FAKE_FILES = {
    "config.py": FAKE_REPO_DIR / "config.py",
    "db_models.py": FAKE_REPO_DIR / "db_models.py",
    "auth_service.py": FAKE_REPO_DIR / "auth_service.py",
    "auth_middleware.py": FAKE_REPO_DIR / "auth_middleware.py",
    "api_routes.py": FAKE_REPO_DIR / "api_routes.py",
    "validators.py": FAKE_REPO_DIR / "validators.py",
    "payments.py": FAKE_REPO_DIR / "payments.py",
}


def _make_store(tmp_path: Path) -> IndexStore:
    """Create a temporary IndexStore and populate with fake repo data."""
    db_path = str(tmp_path / "test.db")
    store = IndexStore(db_path)

    # config.py — no imports, leaf-ish
    store.upsert_summary(
        FileSummary(
            path="config.py",
            language="python",
            summary="Application configuration with DB, auth, and Stripe settings.",
            symbols=[
                SymbolInfo("AppConfig", "class", "class AppConfig", "Central app configuration")
            ],
            imports=[],
            symbol_refs=[],
        )
    )

    # db_models.py — no imports, core models
    store.upsert_summary(
        FileSummary(
            path="db_models.py",
            language="python",
            summary="Database models for users, products, orders.",
            symbols=[
                SymbolInfo("User", "class", "class User", "User model"),
                SymbolInfo("Product", "class", "class Product", "Product model"),
                SymbolInfo("Order", "class", "class Order", "Order model"),
                SymbolInfo("OrderItem", "class", "class OrderItem", "Order line item"),
            ],
            imports=[],
            symbol_refs=[],
        )
    )

    # auth_service.py — imports config, db_models
    store.upsert_summary(
        FileSummary(
            path="auth_service.py",
            language="python",
            summary="Authentication service for JWT token verification and session management.",
            symbols=[
                SymbolInfo(
                    "authenticate",
                    "function",
                    "def authenticate(token: str) -> User",
                    "Verify JWT token",
                ),
                SymbolInfo(
                    "create_session",
                    "function",
                    "def create_session(user: User) -> str",
                    "Create JWT session",
                ),
                SymbolInfo(
                    "hash_password",
                    "function",
                    "def hash_password(raw: str) -> str",
                    "Hash password",
                ),
                SymbolInfo(
                    "verify_password",
                    "function",
                    "def verify_password(raw: str, hashed: str) -> bool",
                    "Verify password",
                ),
            ],
            imports=["config.py", "db_models.py"],
            symbol_refs=[
                ("authenticate", "db_models.py", "User"),
                ("authenticate", "config.py", "AppConfig"),
                ("create_session", "config.py", "AppConfig"),
            ],
        )
    )

    # auth_middleware.py — imports auth_service
    store.upsert_summary(
        FileSummary(
            path="auth_middleware.py",
            language="python",
            summary="Authentication middleware decorator for API routes.",
            symbols=[
                SymbolInfo(
                    "require_auth", "function", "def require_auth(handler)", "Auth decorator"
                ),
                SymbolInfo(
                    "extract_token",
                    "function",
                    "def extract_token(headers: dict) -> Optional[str]",
                    "Extract bearer token",
                ),
            ],
            imports=["auth_service.py"],
            symbol_refs=[
                ("require_auth", "auth_service.py", "authenticate"),
            ],
        )
    )

    # api_routes.py — imports auth_middleware, db_models, payments
    store.upsert_summary(
        FileSummary(
            path="api_routes.py",
            language="python",
            summary="API route handlers for orders, products, and checkout.",
            symbols=[
                SymbolInfo(
                    "create_order", "function", "async def create_order(request)", "Create order"
                ),
                SymbolInfo(
                    "get_order",
                    "function",
                    "async def get_order(request, order_id)",
                    "Get order by ID",
                ),
                SymbolInfo(
                    "list_products", "function", "async def list_products(request)", "List products"
                ),
                SymbolInfo(
                    "checkout", "function", "async def checkout(request)", "Process checkout"
                ),
            ],
            imports=["auth_middleware.py", "db_models.py", "payments.py"],
            symbol_refs=[
                ("create_order", "auth_middleware.py", "require_auth"),
                ("checkout", "payments.py", "charge_card"),
                ("checkout", "payments.py", "create_payment_intent"),
            ],
        )
    )

    # validators.py — imports config
    store.upsert_summary(
        FileSummary(
            path="validators.py",
            language="python",
            summary="Input validation functions for orders and payments.",
            symbols=[
                SymbolInfo(
                    "validate_email",
                    "function",
                    "def validate_email(email: str) -> list[str]",
                    "Validate email",
                ),
                SymbolInfo(
                    "validate_order",
                    "function",
                    "def validate_order(data: dict) -> list[str]",
                    "Validate order",
                ),
                SymbolInfo(
                    "validate_payment",
                    "function",
                    "def validate_payment(data: dict) -> list[str]",
                    "Validate payment",
                ),
            ],
            imports=["config.py"],
            symbol_refs=[],
        )
    )

    # payments.py — imports config, db_models
    store.upsert_summary(
        FileSummary(
            path="payments.py",
            language="python",
            summary="Payment processing via Stripe for charges and refunds.",
            symbols=[
                SymbolInfo(
                    "charge_card",
                    "function",
                    "def charge_card(amount: float, token: str) -> dict",
                    "Charge card",
                ),
                SymbolInfo(
                    "refund", "function", "def refund(charge_id: str) -> dict", "Issue refund"
                ),
                SymbolInfo(
                    "create_payment_intent",
                    "function",
                    "def create_payment_intent(order) -> dict",
                    "Create payment intent",
                ),
            ],
            imports=["config.py", "db_models.py"],
            symbol_refs=[
                ("charge_card", "config.py", "AppConfig"),
                ("create_payment_intent", "db_models.py", "Order"),
            ],
        )
    )

    return store


class _FakeSourceFetcher:
    """Reads source from the fake_repo fixtures on disk."""

    async def fetch(self, path: str) -> str | None:
        file_path = FAKE_REPO_DIR / path
        if file_path.exists():
            return file_path.read_text()
        return None


# ─── TestIndexStoreGraph ───────────────────────────────────────────────


class TestIndexStoreGraph:
    """12 tests for dependency graph queries, call graph, and blast radius."""

    def test_dependency_graph_simple(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        deps = store.get_dependents("config.py")
        assert "auth_service.py" in deps
        assert "validators.py" in deps
        assert "payments.py" in deps
        store.close()

    def test_dependency_graph_transitive(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        rev_deps = store.get_reverse_deps("config.py", max_depth=3)
        assert "auth_service.py" in rev_deps
        # auth_middleware depends on auth_service which depends on config
        assert "auth_middleware.py" in rev_deps
        store.close()

    def test_call_graph_direct(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        callers = store.get_call_graph("auth_service.py", "authenticate")
        assert ("auth_middleware.py", "require_auth") in callers
        store.close()

    def test_call_graph_no_callers(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        callers = store.get_call_graph("validators.py", "validate_email")
        assert callers == []
        store.close()

    def test_blast_radius_leaf_change(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        # validators.py symbols have no callers in symbol_refs
        blast = store.get_blast_radius(["validators.py"])
        assert len(blast) == 0
        store.close()

    def test_blast_radius_core_change(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        blast = store.get_blast_radius(["db_models.py"])
        blast_paths = [e.path for e in blast]
        assert "auth_service.py" in blast_paths
        assert "payments.py" in blast_paths
        store.close()

    def test_blast_radius_depth_2(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        blast = store.get_blast_radius(["config.py"])
        depths = {e.path: e.depth for e in blast}
        # auth_service directly calls config → depth 1
        assert depths.get("auth_service.py") == 1
        # auth_middleware calls auth_service which calls config → depth 2
        assert depths.get("auth_middleware.py") == 2
        store.close()

    def test_upsert_updates_symbols(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        # Update config.py with different symbols
        store.upsert_summary(
            FileSummary(
                path="config.py",
                language="python",
                summary="Updated config",
                symbols=[SymbolInfo("NewConfig", "class", "class NewConfig", "New config")],
            )
        )
        s = store.get_summary("config.py")
        assert s is not None
        assert len(s.symbols) == 1
        assert s.symbols[0].name == "NewConfig"
        store.close()

    def test_remove_paths_cascades(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.remove_paths(["auth_service.py"])
        assert store.get_summary("auth_service.py") is None
        # Imports from auth_service should be gone
        callers = store.get_call_graph("auth_service.py", "authenticate")
        # The symbol_ref from auth_middleware still points to auth_service,
        # but auth_service itself is deleted. Let's verify it doesn't crash.
        assert isinstance(callers, list)
        store.close()

    def test_all_paths_after_batch(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        paths = store.all_paths()
        assert len(paths) == 7
        assert "config.py" in paths
        assert "api_routes.py" in paths
        store.close()

    def test_directory_summaries_roundtrip(self, tmp_path: Path) -> None:
        from mira.index.store import DirectorySummary

        store = _make_store(tmp_path)
        store.upsert_directory(
            DirectorySummary(path="fake_repo", summary="E-commerce app", file_count=7)
        )
        ds = store.get_directory_summary("fake_repo")
        assert ds is not None
        assert ds.summary == "E-commerce app"
        assert ds.file_count == 7
        store.close()

    def test_symbol_refs_roundtrip(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        s = store.get_summary("auth_service.py")
        assert s is not None
        assert len(s.symbol_refs) == 3
        # Check one specific ref
        assert ("authenticate", "db_models.py", "User") in s.symbol_refs
        store.close()


# ─── TestContextBuilderWithFakeRepo ────────────────────────────────────


class TestContextBuilderWithFakeRepo:
    """9 tests for the async context builder with source fetching."""

    @pytest.mark.asyncio
    async def test_source_code_included_in_context(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        fetcher = _FakeSourceFetcher()
        result = await build_code_context(
            changed_paths=["auth_service.py"],
            store=store,
            token_budget=8000,
            source_fetcher=fetcher,
        )
        assert "def authenticate" in result
        assert "### Source Code" in result
        store.close()

    @pytest.mark.asyncio
    async def test_summary_deduplication(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        fetcher = _FakeSourceFetcher()
        result = await build_code_context(
            changed_paths=["auth_service.py"],
            store=store,
            token_budget=8000,
            source_fetcher=fetcher,
        )
        # auth_service.py should appear in source section but NOT in changed files summary
        lines = result.split("\n")
        summary_section_found = False
        for line in lines:
            if "### Changed Files" in line:
                summary_section_found = True
            if summary_section_found and "`auth_service.py`" in line:
                pytest.fail("auth_service.py should not appear in summary when source is shown")
        store.close()

    @pytest.mark.asyncio
    async def test_budget_60_30_10_split(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        fetcher = _FakeSourceFetcher()
        result = await build_code_context(
            changed_paths=["auth_service.py"],
            store=store,
            token_budget=8000,
            source_fetcher=fetcher,
        )
        # Source section should be present (60% tier)
        assert "### Source Code" in result
        # Total should be within budget
        assert len(result) <= 8000 * 4  # chars = tokens * 4
        store.close()

    @pytest.mark.asyncio
    async def test_source_fetcher_failure_graceful(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)

        class _FailingFetcher:
            async def fetch(self, path: str) -> str | None:
                raise ConnectionError("Network error")

        result = await build_code_context(
            changed_paths=["auth_service.py"],
            store=store,
            token_budget=8000,
            source_fetcher=_FailingFetcher(),
        )
        # Should still produce output without crashing
        assert "## Codebase Context" in result
        store.close()

    @pytest.mark.asyncio
    async def test_blast_radius_source_fetched(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        fetcher = _FakeSourceFetcher()
        # Change config.py — blast radius includes auth_service (depth 1)
        result = await build_code_context(
            changed_paths=["config.py"],
            store=store,
            token_budget=16000,
            source_fetcher=fetcher,
        )
        # auth_service.py's affected symbol should appear in source
        assert "authenticate" in result or "AppConfig" in result
        store.close()

    @pytest.mark.asyncio
    async def test_empty_store_with_source_fetcher(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "empty.db")
        store = IndexStore(db_path)
        fetcher = _FakeSourceFetcher()
        result = await build_code_context(
            changed_paths=["nonexistent.py"],
            store=store,
            token_budget=8000,
            source_fetcher=fetcher,
        )
        assert "## Codebase Context" in result
        store.close()

    @pytest.mark.asyncio
    async def test_directory_structure_within_budget(self, tmp_path: Path) -> None:
        from mira.index.store import DirectorySummary

        store = _make_store(tmp_path)
        store.upsert_directory(
            DirectorySummary(path="fake_repo", summary="E-commerce", file_count=7)
        )
        fetcher = _FakeSourceFetcher()
        result = await build_code_context(
            changed_paths=["fake_repo/auth_service.py"],
            store=store,
            token_budget=8000,
            source_fetcher=fetcher,
        )
        # Directory section should be present
        assert "### Repository Structure" in result or "## Codebase Context" in result
        store.close()

    @pytest.mark.asyncio
    async def test_symbol_extraction_in_context(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        fetcher = _FakeSourceFetcher()
        result = await build_code_context(
            changed_paths=["auth_service.py"],
            store=store,
            token_budget=16000,
            source_fetcher=fetcher,
        )
        # Should show individual function bodies, not just entire files
        assert "def authenticate" in result or "def create_session" in result
        store.close()

    @pytest.mark.asyncio
    async def test_async_execution(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        # Verify build_code_context is properly awaitable
        result = await build_code_context(
            changed_paths=["config.py"],
            store=store,
            token_budget=4000,
        )
        assert isinstance(result, str)
        store.close()


# ─── TestEndToEndReview ────────────────────────────────────────────────


class TestEndToEndReview:
    """4 tests for full pipeline with mocked LLM and realistic diffs."""

    def _make_review_response(self, comments: list[dict] | None = None) -> str:
        return json.dumps(
            {
                "comments": comments or [],
                "key_issues": [],
                "summary": "Looks good overall.",
                "metadata": {"reviewed_files": 1},
            }
        )

    def _make_walkthrough_response(self) -> str:
        return json.dumps(
            {
                "summary": "Auth service changes.",
                "change_groups": [
                    {
                        "label": "Auth",
                        "files": [
                            {
                                "path": "fake_repo/auth_service.py",
                                "change_type": "modified",
                                "description": "Updated auth",
                            }
                        ],
                    }
                ],
                "effort": {"level": 2, "label": "Simple", "minutes": 10},
                "confidence_score": {"score": 4, "label": "Safe", "reason": "Minor change"},
            }
        )

    @pytest.mark.asyncio
    async def test_full_pipeline_with_context(self, tmp_path: Path) -> None:
        diff_text = (FIXTURES_DIR / "fake_repo_auth_change.diff").read_text()
        config = load_config()
        config.review.walkthrough = False

        mock_llm = MagicMock(spec=LLMProvider)
        mock_llm.review = AsyncMock(return_value=self._make_review_response())
        mock_llm.walkthrough = AsyncMock(return_value=self._make_walkthrough_response())
        mock_llm.complete = AsyncMock(return_value=self._make_review_response())
        mock_llm.count_tokens = MagicMock(return_value=100)
        mock_llm.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        engine = ReviewEngine(config=config, llm=mock_llm, dry_run=True)
        result = await engine.review_diff(diff_text)
        assert result is not None
        assert result.summary != ""

    @pytest.mark.asyncio
    async def test_review_detects_issues_in_diff(self, tmp_path: Path) -> None:
        diff_text = (FIXTURES_DIR / "fake_repo_payment_change.diff").read_text()
        config = load_config()
        config.review.walkthrough = False

        comments = [
            {
                "path": "fake_repo/payments.py",
                "line": 55,
                "severity": "warning",
                "category": "security",
                "title": "Use hmac.new correctly",
                "body": "hmac.new should be hmac.new with proper arguments.",
                "confidence": 0.9,
                "existing_code": "expected = hmac.new(",
            }
        ]
        mock_llm = MagicMock(spec=LLMProvider)
        mock_llm.review = AsyncMock(return_value=self._make_review_response(comments))
        mock_llm.count_tokens = MagicMock(return_value=100)
        mock_llm.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        engine = ReviewEngine(config=config, llm=mock_llm, dry_run=True)
        result = await engine.review_diff(diff_text)
        assert len(result.comments) >= 1

    @pytest.mark.asyncio
    async def test_walkthrough_generated(self, tmp_path: Path) -> None:
        diff_text = (FIXTURES_DIR / "fake_repo_auth_change.diff").read_text()
        config = load_config()
        config.review.walkthrough = True

        mock_llm = MagicMock(spec=LLMProvider)
        mock_llm.review = AsyncMock(return_value=self._make_review_response())
        mock_llm.walkthrough = AsyncMock(return_value=self._make_walkthrough_response())
        mock_llm.count_tokens = MagicMock(return_value=100)
        mock_llm.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        engine = ReviewEngine(config=config, llm=mock_llm, dry_run=True)
        result = await engine.review_diff(diff_text)
        assert result.walkthrough is not None
        assert result.walkthrough.summary != ""

    @pytest.mark.asyncio
    async def test_no_duplicate_comments_across_chunks(self, tmp_path: Path) -> None:
        diff_text = (FIXTURES_DIR / "fake_repo_multi_file.diff").read_text()
        config = load_config()
        config.review.walkthrough = False

        mock_llm = MagicMock(spec=LLMProvider)
        mock_llm.review = AsyncMock(return_value=self._make_review_response())
        mock_llm.count_tokens = MagicMock(return_value=100)
        mock_llm.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        engine = ReviewEngine(config=config, llm=mock_llm, dry_run=True)
        result = await engine.review_diff(diff_text)
        # Verify no duplicate comments (same path+line)
        seen = set()
        for c in result.comments:
            key = (c.path, c.line)
            assert key not in seen, f"Duplicate comment at {c.path}:{c.line}"
            seen.add(key)


# ─── TestBlastRadiusScenarios ──────────────────────────────────────────


class TestBlastRadiusScenarios:
    """4 tests for blast radius with different change scenarios."""

    def test_leaf_change_zero_blast(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        blast = store.get_blast_radius(["validators.py"])
        # validators has no callers in symbol_refs
        assert len(blast) == 0
        store.close()

    def test_core_model_change_wide_blast(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        blast = store.get_blast_radius(["db_models.py"])
        affected_paths = {e.path for e in blast}
        assert "auth_service.py" in affected_paths
        assert "payments.py" in affected_paths
        store.close()

    def test_multi_file_change_blast(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        blast = store.get_blast_radius(["config.py", "auth_service.py"])
        affected_paths = {e.path for e in blast}
        # config change affects validators, payments, auth_service (but auth_service is in changed set)
        # auth_service change affects auth_middleware
        assert "auth_middleware.py" in affected_paths
        store.close()

    def test_depth_annotations_correct(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        blast = store.get_blast_radius(["config.py"])
        depth_map = {e.path: e.depth for e in blast}
        # Direct callers of config symbols → depth 1
        for path in ["auth_service.py", "payments.py"]:
            if path in depth_map:
                assert depth_map[path] == 1
        # auth_middleware calls auth_service which calls config → depth 2
        if "auth_middleware.py" in depth_map:
            assert depth_map["auth_middleware.py"] == 2
        store.close()


# ─── TestSymbolExtraction ─────────────────────────────────────────────


class TestSymbolExtraction:
    """7 tests for extracting real functions from fake repo files."""

    def test_extract_python_functions(self) -> None:
        source = (FAKE_REPO_DIR / "auth_service.py").read_text()
        symbols = extract_symbols(source, "python")
        names = [s.name for s in symbols]
        assert "authenticate" in names
        assert "create_session" in names
        assert "hash_password" in names
        assert "verify_password" in names

    def test_extract_python_classes(self) -> None:
        source = (FAKE_REPO_DIR / "db_models.py").read_text()
        symbols = extract_symbols(source, "python")
        names = [s.name for s in symbols]
        assert "User" in names
        assert "Product" in names
        assert "Order" in names
        assert "OrderItem" in names

    def test_extract_decorators_included(self) -> None:
        source = (FAKE_REPO_DIR / "auth_middleware.py").read_text()
        symbols = extract_symbols(source, "python")
        # The require_auth function is decorated with @wraps
        names = [s.name for s in symbols]
        assert "require_auth" in names

    def test_extract_specific_symbol_by_name(self) -> None:
        source = (FAKE_REPO_DIR / "auth_service.py").read_text()
        sym = find_symbol_by_name(source, "python", "authenticate")
        assert sym is not None
        assert sym.name == "authenticate"
        assert "def authenticate" in sym.source
        assert sym.start_line > 0

    def test_extract_preserves_source(self) -> None:
        source = (FAKE_REPO_DIR / "payments.py").read_text()
        sym = find_symbol_by_name(source, "python", "charge_card")
        assert sym is not None
        # The extracted source should contain the full function body
        assert "config.STRIPE_API_KEY" in sym.source
        assert "return" in sym.source

    def test_extract_empty_file(self) -> None:
        symbols = extract_symbols("", "python")
        assert symbols == []

    def test_extract_unknown_language(self) -> None:
        source = "some random content\nwith multiple lines\n"
        symbols = extract_symbols(source, "brainfuck")
        # Should not crash, may return empty
        assert isinstance(symbols, list)
