"""Tests for the OSV.dev client + vulnerability storage."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from mira.index.store import IndexStore
from mira.security.osv import (
    PackageQuery,
    normalize_version,
    osv_ecosystem,
    parse_advisory,
    query_batch,
)

# ── Pure helpers ──


class TestVersionNormalization:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("4.18.0", "4.18.0"),
            ("^4.18.0", "4.18.0"),
            ("~4.18", "4.18"),
            (">=2.31.0", "2.31.0"),
            ("==1.2.3", "1.2.3"),
            (">=2.31.0,<3.0", "2.31.0"),
            ("", ""),
            ("  ^1.0  ", "1.0"),
        ],
    )
    def test_strips_constraint_operators(self, raw, expected):
        assert normalize_version(raw) == expected


class TestEcosystemMapping:
    @pytest.mark.parametrize(
        "kind,expected",
        [
            ("npm", "npm"),
            ("pip", "PyPI"),
            ("go", "Go"),
            ("rust", "crates.io"),
            ("docker", None),
            ("unknown", None),
        ],
    )
    def test_mapping(self, kind, expected):
        assert osv_ecosystem(kind) == expected


class TestParseAdvisory:
    def test_basic_parse(self):
        adv = {
            "id": "GHSA-abcd-1234",
            "summary": "Prototype Pollution in lodash",
            "details": "longer details",
            "database_specific": {"severity": "HIGH"},
            "references": [
                {"type": "ADVISORY", "url": "https://github.com/advisories/GHSA-abcd-1234"},
            ],
            "affected": [
                {
                    "ranges": [
                        {
                            "events": [
                                {"introduced": "0"},
                                {"fixed": "4.17.21"},
                            ],
                        },
                    ],
                },
            ],
        }
        entry = parse_advisory(adv)
        assert entry.cve_id == "GHSA-abcd-1234"
        assert entry.summary == "Prototype Pollution in lodash"
        assert entry.severity == "high"
        assert entry.advisory_url == "https://github.com/advisories/GHSA-abcd-1234"
        assert entry.fixed_in == "4.17.21"

    def test_normalizes_severity_from_cvss(self):
        # No database_specific.severity; falls back to CVSS.
        adv = {
            "id": "CVE-2024-1234",
            "summary": "x",
            "severity": [{"type": "CVSS_V3", "score": "9.8"}],
        }
        assert parse_advisory(adv).severity == "critical"

    def test_unknown_severity_when_missing(self):
        adv = {"id": "CVE-X", "summary": "x"}
        assert parse_advisory(adv).severity == "unknown"

    def test_advisory_url_falls_back_to_osv(self):
        adv = {"id": "OSV-2024-1", "summary": "x"}
        assert parse_advisory(adv).advisory_url.startswith("https://osv.dev/")


# ── query_batch ──


@pytest.mark.asyncio
async def test_query_batch_skips_unmappable_ecosystems():
    """Docker packages should be filtered before the API call."""
    with patch("mira.security.osv.httpx.AsyncClient") as mock_client_cls:
        instance = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = instance
        # Empty results — no real packages to query.
        instance.post.return_value.status_code = 200
        instance.post.return_value.json = lambda: {"results": []}
        instance.post.return_value.raise_for_status = lambda: None

        results = await query_batch(
            [PackageQuery(ecosystem="docker", name="nginx", version="1.25")]
        )
        # Docker queries should never have hit the wire.
        assert results == {}
        instance.post.assert_not_called()


@pytest.mark.asyncio
async def test_query_batch_returns_vulns_per_package():
    """Mock OSV response with one vulnerable package; verify mapping back."""
    osv_response = {
        "results": [
            {"vulns": [{"id": "GHSA-aaaa-1111"}]},
            {"vulns": []},
        ]
    }
    detail_response = {
        "id": "GHSA-aaaa-1111",
        "summary": "Test vuln",
        "database_specific": {"severity": "HIGH"},
        "references": [{"type": "ADVISORY", "url": "https://example.com/x"}],
    }

    with patch("mira.security.osv.httpx.AsyncClient") as mock_client_cls:
        instance = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = instance

        async def post_side(*args, **kwargs):
            r = AsyncMock()
            r.status_code = 200
            r.json = lambda: osv_response
            r.raise_for_status = lambda: None
            return r

        async def get_side(url):
            r = AsyncMock()
            r.status_code = 200
            r.json = lambda: detail_response
            return r

        instance.post.side_effect = post_side
        instance.get.side_effect = get_side

        results = await query_batch(
            [
                PackageQuery(ecosystem="npm", name="lodash", version="4.17.20"),
                PackageQuery(ecosystem="npm", name="express", version="4.18.2"),
            ]
        )

    lodash = results[("npm", "lodash", "4.17.20")]
    assert len(lodash) == 1
    assert lodash[0].cve_id == "GHSA-aaaa-1111"
    assert lodash[0].severity == "high"

    express = results[("npm", "express", "4.18.2")]
    assert express == []


# ── Store CRUD ──


@pytest.fixture
def store(tmp_path):
    s = IndexStore(str(tmp_path / "t.db"))
    yield s
    s.close()


class TestVulnerabilityCRUD:
    def test_replace_then_list(self, store):
        store.replace_vulnerabilities_for_package(
            "lodash",
            "npm",
            "4.17.20",
            [
                {
                    "cve_id": "GHSA-aaaa-1111",
                    "summary": "Prototype Pollution",
                    "severity": "high",
                    "advisory_url": "https://x",
                    "fixed_in": "4.17.21",
                },
            ],
        )
        rows = store.list_vulnerabilities()
        assert len(rows) == 1
        assert rows[0].cve_id == "GHSA-aaaa-1111"
        assert rows[0].severity == "high"

    def test_replace_atomically_drops_resolved(self, store):
        # First scan finds 2 vulns
        store.replace_vulnerabilities_for_package(
            "lodash",
            "npm",
            "4.17.20",
            [
                {"cve_id": "GHSA-1", "severity": "high"},
                {"cve_id": "GHSA-2", "severity": "moderate"},
            ],
        )
        assert len(store.list_vulnerabilities()) == 2
        # Second scan: only one remains (other was withdrawn)
        store.replace_vulnerabilities_for_package(
            "lodash",
            "npm",
            "4.17.20",
            [{"cve_id": "GHSA-1", "severity": "high"}],
        )
        rows = store.list_vulnerabilities()
        assert len(rows) == 1
        assert rows[0].cve_id == "GHSA-1"

    def test_replace_with_empty_clears_all(self, store):
        store.replace_vulnerabilities_for_package(
            "lodash",
            "npm",
            "4.17.20",
            [{"cve_id": "GHSA-X", "severity": "high"}],
        )
        store.replace_vulnerabilities_for_package(
            "lodash",
            "npm",
            "4.17.20",
            [],
        )
        assert store.list_vulnerabilities() == []

    def test_list_orders_by_severity(self, store):
        store.replace_vulnerabilities_for_package(
            "pkg",
            "npm",
            "1.0",
            [
                {"cve_id": "A", "severity": "low"},
                {"cve_id": "B", "severity": "critical"},
                {"cve_id": "C", "severity": "moderate"},
            ],
        )
        rows = store.list_vulnerabilities()
        severities = [r.severity for r in rows]
        assert severities == ["critical", "moderate", "low"]

    def test_count_by_severity(self, store):
        store.replace_vulnerabilities_for_package(
            "pkg",
            "npm",
            "1.0",
            [
                {"cve_id": "A", "severity": "high"},
                {"cve_id": "B", "severity": "high"},
                {"cve_id": "C", "severity": "low"},
            ],
        )
        counts = store.count_vulnerabilities_by_severity()
        assert counts == {"high": 2, "low": 1}

    def test_first_seen_at_preserved_across_rescan(self, store):
        store.replace_vulnerabilities_for_package(
            "pkg",
            "npm",
            "1.0",
            [{"cve_id": "GHSA-X", "severity": "high"}],
        )
        first_seen = store.list_vulnerabilities()[0].first_seen_at
        # Re-scan with same advisory — first_seen_at should be the same,
        # last_seen_at should advance.
        import time as _t

        _t.sleep(0.01)
        store.replace_vulnerabilities_for_package(
            "pkg",
            "npm",
            "1.0",
            [{"cve_id": "GHSA-X", "severity": "high"}],
        )
        row = store.list_vulnerabilities()[0]
        assert row.first_seen_at == pytest.approx(first_seen, abs=0.001)
        assert row.last_seen_at >= first_seen
