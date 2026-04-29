"""Tests for cross-repo relationship detection.

22 tests across 4 test classes covering external ref storage,
relationship resolution, repo grouping, and template integration.
"""

from __future__ import annotations

from pathlib import Path

from mira.index.relationships import RelationshipStore
from mira.index.store import ExternalRef, FileSummary, IndexStore, SymbolInfo


def _make_store_with_refs(
    tmp_path: Path, owner: str, repo: str, refs: list[ExternalRef]
) -> IndexStore:
    """Create a store with files that have external refs."""
    repo_dir = tmp_path / owner
    repo_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(repo_dir / f"{repo}.db")
    store = IndexStore(db_path)

    # Group refs by file_path
    files: dict[str, list[ExternalRef]] = {}
    for ref in refs:
        files.setdefault(ref.file_path, []).append(ref)

    for path, file_refs in files.items():
        store.upsert_summary(
            FileSummary(
                path=path,
                language="python",
                summary=f"File {path}",
                symbols=[],
                imports=[],
                symbol_refs=[],
                external_refs=file_refs,
            )
        )

    return store


# ─── TestExternalRefsStorage ──────────────────────────────────────────


class TestExternalRefsStorage:
    """5 tests for external refs CRUD operations."""

    def test_upsert_and_load_external_refs(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        store = IndexStore(db_path)
        ref = ExternalRef(
            file_path="main.py",
            kind="docker_image",
            target="nginx:latest",
            description="Web server",
        )
        store.upsert_summary(
            FileSummary(
                path="main.py",
                language="python",
                summary="Main app",
                external_refs=[ref],
            )
        )
        s = store.get_summary("main.py")
        assert s is not None
        assert len(s.external_refs) == 1
        assert s.external_refs[0].kind == "docker_image"
        assert s.external_refs[0].target == "nginx:latest"
        store.close()

    def test_external_refs_cascade_on_delete(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        store = IndexStore(db_path)
        store.upsert_summary(
            FileSummary(
                path="main.py",
                language="python",
                summary="Main",
                external_refs=[ExternalRef("main.py", "docker_image", "nginx:latest")],
            )
        )
        store.remove_paths(["main.py"])
        refs = store.get_external_refs_for_paths(["main.py"])
        assert len(refs) == 0
        store.close()

    def test_get_refs_by_target(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        store = IndexStore(db_path)
        store.upsert_summary(
            FileSummary(
                path="a.py",
                language="python",
                summary="A",
                external_refs=[ExternalRef("a.py", "go_import", "github.com/org/shared-lib")],
            )
        )
        store.upsert_summary(
            FileSummary(
                path="b.py",
                language="python",
                summary="B",
                external_refs=[ExternalRef("b.py", "go_import", "github.com/org/shared-lib")],
            )
        )
        refs = store.get_files_referencing("shared-lib")
        assert len(refs) == 2
        store.close()

    def test_get_all_external_targets(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        store = IndexStore(db_path)
        store.upsert_summary(
            FileSummary(
                path="a.py",
                language="python",
                summary="A",
                external_refs=[
                    ExternalRef("a.py", "docker_image", "redis:7"),
                    ExternalRef("a.py", "npm_package", "lodash"),
                ],
            )
        )
        targets = store.get_all_external_targets()
        assert "redis:7" in targets
        assert "lodash" in targets
        store.close()

    def test_empty_external_refs(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "test.db")
        store = IndexStore(db_path)
        store.upsert_summary(
            FileSummary(
                path="a.py",
                language="python",
                summary="A",
            )
        )
        s = store.get_summary("a.py")
        assert s is not None
        assert s.external_refs == []
        store.close()


# ─── TestRelationshipResolution ───────────────────────────────────────


class TestRelationshipResolution:
    """8 tests for resolving external refs into repo edges."""

    def _setup_two_repos(self, tmp_path: Path, source_target: str) -> RelationshipStore:
        """Create two repos: source depends on target via external ref."""
        # Create target repo
        target_dir = tmp_path / "org"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_store = IndexStore(str(target_dir / "target-repo.db"))
        target_store.upsert_summary(
            FileSummary(
                path="lib.py",
                language="python",
                summary="Library",
            )
        )
        target_store.close()

        # Create source repo
        source_store = IndexStore(str(target_dir / "source-repo.db"))
        source_store.upsert_summary(
            FileSummary(
                path="main.py",
                language="python",
                summary="Main app",
                external_refs=[ExternalRef("main.py", "git_url", source_target)],
            )
        )
        source_store.close()

        return RelationshipStore(index_dir=str(tmp_path))

    def test_docker_image_matches_repo(self, tmp_path: Path) -> None:
        # Create repos
        org_dir = tmp_path / "myorg"
        org_dir.mkdir(parents=True, exist_ok=True)

        s1 = IndexStore(str(org_dir / "frontend.db"))
        s1.upsert_summary(
            FileSummary(
                path="docker-compose.yml",
                language="yaml",
                summary="Docker compose",
                external_refs=[
                    ExternalRef(
                        "docker-compose.yml", "docker_image", "ghcr.io/myorg/backend", "API service"
                    )
                ],
            )
        )
        s1.close()

        s2 = IndexStore(str(org_dir / "backend.db"))
        s2.upsert_summary(FileSummary(path="app.py", language="python", summary="Backend"))
        s2.close()

        rs = RelationshipStore(index_dir=str(tmp_path))
        edges = rs.resolve_edges()
        assert len(edges) >= 1
        assert any(e.target_repo == "myorg/backend" for e in edges)
        rs.close()

    def test_terraform_module_matches_repo(self, tmp_path: Path) -> None:
        rs = self._setup_two_repos(tmp_path, "github.com/org/target-repo//modules/vpc")
        edges = rs.resolve_edges()
        assert len(edges) >= 1
        assert any(e.target_repo == "org/target-repo" for e in edges)
        rs.close()

    def test_go_import_matches_repo(self, tmp_path: Path) -> None:
        rs = self._setup_two_repos(tmp_path, "github.com/org/target-repo/pkg/utils")
        edges = rs.resolve_edges()
        assert len(edges) >= 1
        rs.close()

    def test_api_endpoint_no_match(self, tmp_path: Path) -> None:
        """API endpoints typically don't match repo names."""
        org_dir = tmp_path / "org"
        org_dir.mkdir(parents=True, exist_ok=True)

        s1 = IndexStore(str(org_dir / "client.db"))
        s1.upsert_summary(
            FileSummary(
                path="api.py",
                language="python",
                summary="API client",
                external_refs=[
                    ExternalRef("api.py", "api_endpoint", "https://api.stripe.com/v1/charges")
                ],
            )
        )
        s1.close()

        rs = RelationshipStore(index_dir=str(tmp_path))
        edges = rs.resolve_edges()
        # Stripe isn't an indexed repo
        assert len(edges) == 0
        rs.close()

    def test_npm_package_matches_repo(self, tmp_path: Path) -> None:
        org_dir = tmp_path / "myorg"
        org_dir.mkdir(parents=True, exist_ok=True)

        s1 = IndexStore(str(org_dir / "webapp.db"))
        s1.upsert_summary(
            FileSummary(
                path="package.json",
                language="json",
                summary="Package",
                external_refs=[ExternalRef("package.json", "npm_package", "@myorg/shared-utils")],
            )
        )
        s1.close()

        s2 = IndexStore(str(org_dir / "shared-utils.db"))
        s2.upsert_summary(FileSummary(path="index.js", language="javascript", summary="Utils"))
        s2.close()

        rs = RelationshipStore(index_dir=str(tmp_path))
        edges = rs.resolve_edges()
        assert any(e.target_repo == "myorg/shared-utils" for e in edges)
        rs.close()

    def test_no_false_positives(self, tmp_path: Path) -> None:
        org_dir = tmp_path / "org"
        org_dir.mkdir(parents=True, exist_ok=True)

        s1 = IndexStore(str(org_dir / "app.db"))
        s1.upsert_summary(
            FileSummary(
                path="main.py",
                language="python",
                summary="App",
                external_refs=[ExternalRef("main.py", "pip_package", "requests")],
            )
        )
        s1.close()

        rs = RelationshipStore(index_dir=str(tmp_path))
        edges = rs.resolve_edges()
        assert len(edges) == 0  # "requests" isn't an indexed repo
        rs.close()

    def test_bidirectional_edges(self, tmp_path: Path) -> None:
        org_dir = tmp_path / "org"
        org_dir.mkdir(parents=True, exist_ok=True)

        s1 = IndexStore(str(org_dir / "repo-a.db"))
        s1.upsert_summary(
            FileSummary(
                path="main.py",
                language="python",
                summary="A",
                external_refs=[ExternalRef("main.py", "git_url", "github.com/org/repo-b")],
            )
        )
        s1.close()

        s2 = IndexStore(str(org_dir / "repo-b.db"))
        s2.upsert_summary(
            FileSummary(
                path="main.py",
                language="python",
                summary="B",
                external_refs=[ExternalRef("main.py", "git_url", "github.com/org/repo-a")],
            )
        )
        s2.close()

        rs = RelationshipStore(index_dir=str(tmp_path))
        edges = rs.resolve_edges()
        source_target_pairs = {(e.source_repo, e.target_repo) for e in edges}
        assert ("org/repo-a", "org/repo-b") in source_target_pairs
        assert ("org/repo-b", "org/repo-a") in source_target_pairs
        rs.close()

    def test_get_related_repos(self, tmp_path: Path) -> None:
        org_dir = tmp_path / "org"
        org_dir.mkdir(parents=True, exist_ok=True)

        s1 = IndexStore(str(org_dir / "frontend.db"))
        s1.upsert_summary(
            FileSummary(
                path="main.ts",
                language="typescript",
                summary="Frontend",
                external_refs=[ExternalRef("main.ts", "git_url", "github.com/org/backend")],
            )
        )
        s1.close()

        s2 = IndexStore(str(org_dir / "backend.db"))
        s2.upsert_summary(FileSummary(path="app.py", language="python", summary="Backend"))
        s2.close()

        rs = RelationshipStore(index_dir=str(tmp_path))
        related = rs.get_related_repos("org", "frontend")
        assert len(related) >= 1
        related_repos = [r[0] for r in related]
        assert "org/backend" in related_repos
        rs.close()


# ─── TestRepoGrouping ────────────────────────────────────────────────


def _setup_grouped_repos(tmp_path: Path) -> RelationshipStore:
    """Create repos with content that should be grouped together."""
    org_dir = tmp_path / "org"
    org_dir.mkdir(parents=True, exist_ok=True)

    # payments-service: processes payments, imports shared-lib
    s = IndexStore(str(org_dir / "payments-service.db"))
    s.upsert_summary(
        FileSummary(
            path="main.go",
            language="go",
            summary="Payment processing service handling charges and refunds.",
            symbols=[
                SymbolInfo(
                    "ProcessPayment", "function", "func ProcessPayment()", "Process a payment"
                )
            ],
            external_refs=[
                ExternalRef("main.go", "go_import", "github.com/org/shared-lib/pkg/auth", "Auth")
            ],
        )
    )
    s.close()

    # payments-worker: background payment jobs, imports payments-service models
    s = IndexStore(str(org_dir / "payments-worker.db"))
    s.upsert_summary(
        FileSummary(
            path="worker.go",
            language="go",
            summary="Background worker for payment reconciliation and retries.",
            symbols=[SymbolInfo("RunWorker", "function", "func RunWorker()", "Run payment worker")],
            external_refs=[
                ExternalRef(
                    "worker.go",
                    "go_import",
                    "github.com/org/payments-service/pkg/models",
                    "Payment models",
                ),
                ExternalRef("worker.go", "go_import", "github.com/org/shared-lib/pkg/auth", "Auth"),
            ],
        )
    )
    s.close()

    # shared-lib: utility library (not part of payments group)
    s = IndexStore(str(org_dir / "shared-lib.db"))
    s.upsert_summary(
        FileSummary(
            path="pkg/auth/auth.go",
            language="go",
            summary="Shared authentication middleware for all services.",
        )
    )
    s.close()

    # unrelated: no connection to payments
    s = IndexStore(str(org_dir / "unrelated.db"))
    s.upsert_summary(
        FileSummary(
            path="main.py",
            language="python",
            summary="Documentation site generator for the company blog.",
        )
    )
    s.close()

    return RelationshipStore(index_dir=str(tmp_path))


class TestRepoGrouping:
    """5 tests for content-aware repo grouping."""

    def test_groups_by_edges_and_naming(self, tmp_path: Path) -> None:
        """Repos with mutual edges AND matching naming are grouped."""
        rs = _setup_grouped_repos(tmp_path)
        groups = rs.group_repos(rs.repos)
        payment_group = [g for g in groups if "org/payments-service" in g.repos]
        assert len(payment_group) == 1
        assert "org/payments-worker" in payment_group[0].repos
        assert "org/unrelated" not in payment_group[0].repos
        assert payment_group[0].confidence > 0.3
        rs.close()

    def test_group_has_evidence(self, tmp_path: Path) -> None:
        """Groups include evidence explaining why repos are related."""
        rs = _setup_grouped_repos(tmp_path)
        groups = rs.group_repos(rs.repos)
        payment_group = [g for g in groups if "org/payments-service" in g.repos]
        assert len(payment_group) == 1
        evidence = " ".join(payment_group[0].evidence)
        # Should have multiple signals, not just naming
        assert len(payment_group[0].evidence) >= 2
        rs.close()

    def test_no_grouping_for_unrelated(self, tmp_path: Path) -> None:
        """Repos with no edges, shared deps, or content overlap are not grouped."""
        org_dir = tmp_path / "org"
        org_dir.mkdir(parents=True, exist_ok=True)

        for name, summary in [
            ("alpha", "Image processing pipeline"),
            ("beta", "Email notification service"),
            ("gamma", "Inventory tracking system"),
        ]:
            s = IndexStore(str(org_dir / f"{name}.db"))
            s.upsert_summary(FileSummary(path="main.py", language="python", summary=summary))
            s.close()

        rs = RelationshipStore(index_dir=str(tmp_path))
        groups = rs.group_repos(rs.repos)
        assert len(groups) == 0
        rs.close()

    def test_single_repo_no_group(self, tmp_path: Path) -> None:
        org_dir = tmp_path / "org"
        org_dir.mkdir(parents=True, exist_ok=True)
        s = IndexStore(str(org_dir / "payments-service.db"))
        s.upsert_summary(FileSummary(path="main.go", language="go", summary="Payments"))
        s.close()

        rs = RelationshipStore(index_dir=str(tmp_path))
        groups = rs.group_repos(rs.repos)
        assert len(groups) == 0
        rs.close()

    def test_content_similarity_groups_differently_named_repos(self, tmp_path: Path) -> None:
        """Repos with different names but shared deps + content overlap get grouped."""
        org_dir = tmp_path / "org"
        org_dir.mkdir(parents=True, exist_ok=True)

        # Two repos with different names but both work with payments and share a dependency
        s = IndexStore(str(org_dir / "checkout-flow.db"))
        s.upsert_summary(
            FileSummary(
                path="checkout.py",
                language="python",
                summary="Checkout flow handling payment processing and order creation for customers.",
                symbols=[
                    SymbolInfo(
                        "process_checkout", "function", "def process_checkout()", "Process checkout"
                    )
                ],
                external_refs=[
                    ExternalRef(
                        "checkout.py", "go_import", "github.com/org/stripe-gateway/pkg", "Stripe"
                    )
                ],
            )
        )
        s.close()

        s = IndexStore(str(org_dir / "stripe-gateway.db"))
        s.upsert_summary(
            FileSummary(
                path="gateway.py",
                language="python",
                summary="Stripe payment gateway handling charges, refunds and payment intents for checkout.",
                symbols=[SymbolInfo("charge_card", "function", "def charge_card()", "Charge card")],
            )
        )
        s.close()

        rs = RelationshipStore(index_dir=str(tmp_path))
        groups = rs.group_repos(rs.repos)
        # Should find a group because checkout-flow directly depends on stripe-gateway
        # AND they share content keywords (payment, checkout)
        assert len(groups) >= 1
        group_repos = groups[0].repos
        assert "org/checkout-flow" in group_repos
        assert "org/stripe-gateway" in group_repos
        rs.close()


# ─── TestSummarizeTemplateExternalRefs ────────────────────────────────


class TestSummarizeTemplateExternalRefs:
    """4 tests for external refs in the summarize template and indexer pipeline."""

    def test_template_includes_external_refs_schema(self) -> None:
        from jinja2 import Environment, FileSystemLoader

        template_dir = (
            Path(__file__).parent.parent / "src" / "mira" / "llm" / "prompts" / "templates"
        )
        env = Environment(loader=FileSystemLoader(str(template_dir)))
        template = env.get_template("summarize.jinja2")
        rendered = template.render(files=[{"path": "test.py", "content": "pass"}])
        assert "external_refs" in rendered

    def test_indexer_parses_external_refs(self) -> None:
        from mira.index.indexer import _build_file_summary

        data = {
            "language": "python",
            "summary": "Test file",
            "symbols": [],
            "imports": [],
            "symbol_references": [],
            "external_refs": [
                {"kind": "docker_image", "target": "redis:7", "description": "Cache"},
            ],
        }
        summary = _build_file_summary("test.py", "pass", data)
        assert len(summary.external_refs) == 1
        assert summary.external_refs[0].kind == "docker_image"
        assert summary.external_refs[0].target == "redis:7"

    def test_external_refs_persisted_through_pipeline(self, tmp_path: Path) -> None:
        from mira.index.indexer import _build_file_summary

        db_path = str(tmp_path / "test.db")
        store = IndexStore(db_path)

        data = {
            "language": "go",
            "summary": "Go service",
            "symbols": [],
            "imports": [],
            "symbol_references": [],
            "external_refs": [
                {
                    "kind": "go_import",
                    "target": "github.com/org/shared-lib/pkg",
                    "description": "Shared utils",
                },
            ],
        }
        summary = _build_file_summary("main.go", "package main", data)
        store.upsert_summary(summary)

        loaded = store.get_summary("main.go")
        assert loaded is not None
        assert len(loaded.external_refs) == 1
        assert loaded.external_refs[0].target == "github.com/org/shared-lib/pkg"
        store.close()

    def test_missing_external_refs_key_handled(self) -> None:
        from mira.index.indexer import _build_file_summary

        data = {
            "language": "python",
            "summary": "No refs",
            "symbols": [],
            "imports": [],
            "symbol_references": [],
            # no "external_refs" key
        }
        summary = _build_file_summary("test.py", "pass", data)
        assert summary.external_refs == []
