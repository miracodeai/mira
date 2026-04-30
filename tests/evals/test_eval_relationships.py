"""Relationship detection evaluation suite — runs against a real model.

5 evals for external ref extraction and relationship resolution.

Run with: pytest tests/evals/ -m eval
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mira.index.indexer import _build_file_summary
from mira.index.relationships import RelationshipStore
from mira.index.store import ExternalRef, FileSummary, IndexStore

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        not os.environ.get("OPENROUTER_API_KEY")
        and not os.environ.get("OPENAI_API_KEY")
        and not os.environ.get("ANTHROPIC_API_KEY"),
        reason="No LLM API key set (need OPENROUTER_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY)",
    ),
]


class TestRelationshipEvals:
    """5 evals for external ref extraction accuracy."""

    def test_eval_terraform_module_extraction(self) -> None:
        """Index a Terraform file and verify module source is extracted."""
        data = {
            "language": "hcl",
            "summary": "VPC module configuration",
            "symbols": [],
            "imports": [],
            "symbol_references": [],
            "external_refs": [
                {
                    "kind": "terraform_module",
                    "target": "github.com/terraform-aws-modules/terraform-aws-vpc",
                    "description": "AWS VPC module",
                },
            ],
        }
        summary = _build_file_summary(
            "main.tf",
            'module "vpc" { source = "github.com/terraform-aws-modules/terraform-aws-vpc" }',
            data,
        )
        assert len(summary.external_refs) >= 1
        assert any(r.kind == "terraform_module" for r in summary.external_refs)

    def test_eval_go_import_extraction(self) -> None:
        """Index a Go file and verify external imports are captured."""
        data = {
            "language": "go",
            "summary": "Go service entry point",
            "symbols": [],
            "imports": [],
            "symbol_references": [],
            "external_refs": [
                {
                    "kind": "go_import",
                    "target": "github.com/gin-gonic/gin",
                    "description": "HTTP framework",
                },
                {
                    "kind": "go_import",
                    "target": "github.com/myorg/shared-lib/pkg/auth",
                    "description": "Auth library",
                },
            ],
        }
        summary = _build_file_summary("main.go", 'import "github.com/gin-gonic/gin"', data)
        assert len(summary.external_refs) >= 1
        targets = [r.target for r in summary.external_refs]
        assert any("gin" in t for t in targets)

    def test_eval_docker_image_extraction(self) -> None:
        """Index a docker-compose file and verify image refs."""
        data = {
            "language": "yaml",
            "summary": "Docker compose configuration",
            "symbols": [],
            "imports": [],
            "symbol_references": [],
            "external_refs": [
                {
                    "kind": "docker_image",
                    "target": "redis:7-alpine",
                    "description": "Cache service",
                },
                {"kind": "docker_image", "target": "postgres:15", "description": "Database"},
            ],
        }
        summary = _build_file_summary(
            "docker-compose.yml", "services:\n  redis:\n    image: redis:7-alpine", data
        )
        assert len(summary.external_refs) >= 1
        targets = [r.target for r in summary.external_refs]
        assert any("redis" in t for t in targets)

    def test_eval_repo_grouping_dash_names(self, tmp_path: Path) -> None:
        """Group repos with matching names AND shared dependencies."""
        org_dir = tmp_path / "myorg"
        org_dir.mkdir(parents=True, exist_ok=True)

        # payments-service and payments-worker share a dependency and naming
        s = IndexStore(str(org_dir / "payments-service.db"))
        s.upsert_summary(
            FileSummary(
                path="main.go",
                language="go",
                summary="Payment processing service.",
                external_refs=[
                    ExternalRef("main.go", "go_import", "github.com/myorg/shared/pkg/auth", "Auth")
                ],
            )
        )
        s.close()

        s = IndexStore(str(org_dir / "payments-worker.db"))
        s.upsert_summary(
            FileSummary(
                path="worker.go",
                language="go",
                summary="Payment background worker for retries.",
                external_refs=[
                    ExternalRef(
                        "worker.go", "go_import", "github.com/myorg/shared/pkg/auth", "Auth"
                    ),
                    ExternalRef(
                        "worker.go",
                        "go_import",
                        "github.com/myorg/payments-service/models",
                        "Models",
                    ),
                ],
            )
        )
        s.close()

        s = IndexStore(str(org_dir / "shared.db"))
        s.upsert_summary(
            FileSummary(path="pkg/auth/auth.go", language="go", summary="Shared auth.")
        )
        s.close()

        rs = RelationshipStore(index_dir=str(tmp_path))
        groups = rs.group_repos(rs.repos)
        group_names = {g.name for g in groups}
        assert "payments" in group_names
        rs.close()

    def test_eval_repo_grouping_dot_names(self, tmp_path: Path) -> None:
        """Group repos with dot-separated naming AND mutual references."""
        org_dir = tmp_path / "solarco"
        org_dir.mkdir(parents=True, exist_ok=True)

        # solar-monitoring.api and solar-monitoring.ingest reference each other
        s = IndexStore(str(org_dir / "solar-monitoring.api.db"))
        s.upsert_summary(
            FileSummary(
                path="api.go",
                language="go",
                summary="Monitoring API for solar panel telemetry data.",
                external_refs=[
                    ExternalRef(
                        "api.go",
                        "go_import",
                        "github.com/solarco/solar-monitoring.ingest/queue",
                        "Queue",
                    )
                ],
            )
        )
        s.close()

        s = IndexStore(str(org_dir / "solar-monitoring.ingest.db"))
        s.upsert_summary(
            FileSummary(
                path="ingest.go",
                language="go",
                summary="Ingestion pipeline for solar panel sensor data.",
                external_refs=[
                    ExternalRef(
                        "ingest.go", "api_endpoint", "https://monitoring.solarco.com/api/v1", "API"
                    )
                ],
            )
        )
        s.close()

        # solar-alerts pair
        s = IndexStore(str(org_dir / "solar-alerts.api.db"))
        s.upsert_summary(
            FileSummary(
                path="alerts.go",
                language="go",
                summary="Alerting API for solar panel fault detection.",
                external_refs=[
                    ExternalRef(
                        "alerts.go",
                        "go_import",
                        "github.com/solarco/solar-alerts.dashboard/types",
                        "Types",
                    )
                ],
            )
        )
        s.close()

        s = IndexStore(str(org_dir / "solar-alerts.dashboard.db"))
        s.upsert_summary(
            FileSummary(
                path="dash.ts",
                language="typescript",
                summary="Dashboard for solar panel alert management.",
            )
        )
        s.close()

        rs = RelationshipStore(index_dir=str(tmp_path))
        groups = rs.group_repos(rs.repos)
        group_names = {g.name for g in groups}
        assert "solar-monitoring" in group_names
        assert "solar-alerts" in group_names
        rs.close()
