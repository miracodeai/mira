"""Unit tests for the golden-comment converter. No network."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "import_golden_comments",
    Path(__file__).resolve().parent.parent / "scripts" / "import_golden_comments.py",
)
ig = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ig)


def test_map_severity():
    assert ig.map_severity("Critical") == "blocker"
    assert ig.map_severity("High") == "blocker"
    assert ig.map_severity("Medium") == "warning"
    assert ig.map_severity("Low") == "suggestion"
    assert ig.map_severity("") == "warning"  # unknown defaults to warning


def test_guess_category():
    assert ig.guess_category("SSRF via open(url) without validation") == "security"
    assert ig.guess_category("auth bypass on the admin route") == "security"
    assert ig.guess_category("N+1 query in the loop") == "performance"
    assert ig.guess_category("off-by-one in the slice bound") == "bug"


def test_make_slug():
    assert ig.make_slug("discourse", "4", 2) == "discourse-4-2"


def test_convert_shape_and_ids():
    golden = {
        "discourse.json": [
            {
                "pr_title": "T",
                "url": "https://github.com/ai-code-review-evaluation/discourse-graphite/pull/4",
                "comments": [
                    {
                        "comment": "X-Frame-Options ALLOWALL disables clickjacking protection",
                        "severity": "High",
                    },
                    {"comment": "minor style nit", "severity": "Low"},
                ],
            }
        ]
    }
    out = ig.convert(golden, {golden["discourse.json"][0]["url"]: "abc123"})
    assert len(out) == 1
    pr = out[0]
    assert pr["language"] == "ruby"
    assert pr["source"] == "martian-bench"
    assert pr["head_sha"] == "abc123"
    assert [f["id"] for f in pr["findings"]] == ["discourse-4-0", "discourse-4-1"]
    assert pr["findings"][0]["severity"] == "blocker"
    assert pr["findings"][1]["severity"] == "suggestion"


def test_convert_missing_sha_is_none():
    golden = {
        "grafana.json": [
            {"pr_title": "T", "url": "https://github.com/grafana/grafana/pull/1", "comments": []}
        ]
    }
    out = ig.convert(golden, {})
    assert out[0]["head_sha"] is None
    assert out[0]["language"] == "go"
    assert out[0]["findings"] == []
