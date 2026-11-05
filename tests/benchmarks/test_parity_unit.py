"""HAI-11607 parity harness: snapshots, blind judge, stats, runner. No network, no model."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from mira.config import LLMConfig
from mira.models import PRInfo, ReviewComment, Severity

from . import parity_runner
from .parity_export import GitHubReader, export_pr
from .parity_judge import (
    GREPTILE,
    KORY,
    PRIOR,
    SUBMIT_ISSUES_TOOL,
    build_pool,
    build_prompt,
    judge_unit,
    normalize_response,
    score_unit,
)
from .parity_snapshot import (
    PinnedGitHubProvider,
    later_evidence,
    load_units,
    neutral_text,
    units_for_fixture,
    window_survives,
)
from .parity_stats import model_family, parity_verdict, registry_ab, totals

HICKORY = Path(__file__).parent / "fixtures" / "hickory"
SHA_A, SHA_B, HEAD = "a" * 40, "b" * 40, "f" * 40


@pytest.fixture(autouse=True)
def _restore_registry_hooks(monkeypatch):
    """install_arm patches module globals; undo it so other test files see the shipped hooks."""
    import mira.core.engine as engine_module
    import mira.registry.prompt as registry_prompt

    monkeypatch.setattr(engine_module, "apply_registry", engine_module.apply_registry)
    monkeypatch.setattr(registry_prompt, "registry_rule", registry_prompt.registry_rule)
    monkeypatch.setattr(registry_prompt, "with_registry_tool", registry_prompt.with_registry_tool)


GREPTILE_BODY = (
    '<a href="#"><img alt="P1" src="https://greptile-static-assets.s3.amazonaws.com/badges/p1.svg"></a> '
    "**Null token crashes refresh**\n\n`refresh()` dereferences `token` before the check. Greptile thinks so."
    "\n\n<details><summary>Prompt To Fix With AI</summary>secret fix prompt</details>"
)


def _inline(sha: str, path: str, line: int, created: str) -> dict:
    return {
        "kind": "inline",
        "path": path,
        "original_line": line,
        "original_commit_id": sha,
        "commit_id": HEAD,
        "created_at": created,
        "body": GREPTILE_BODY,
        "thread_resolved": True,
        "thread_outdated": False,
        "url": "u",
    }


def _finding(n: int, pr: int, path: str, line: int) -> dict:
    return {
        "id": f"hickory-{pr}-{n}",
        "path": path,
        "line_start": line,
        "line_end": line,
        "description": GREPTILE_BODY,
        "category": "bug",
        "severity": "blocker",
    }


def _fixture(
    pr: int = 1, inline: list | None = None, findings: list | None = None, summary: bool = True
) -> dict:
    comments = list(inline or [])
    if summary:
        comments.insert(
            0,
            {
                "kind": "summary",
                "created_at": "2026-10-01T00:00:00Z",
                "body": f'Last reviewed commit: ["x"](https://github.com/o/r/commit/{SHA_B})',
            },
        )
    return {
        "repo": "o/r",
        "pr_url": f"https://github.com/o/r/pull/{pr}",
        "pr_number": pr,
        "pr_title": "t",
        "merged_at": "2026-10-02T00:00:00Z",
        "base_sha": "0" * 40,
        "head_sha": HEAD,
        "split": "held-out",
        "greptile_comments": comments,
        "findings": list(findings or []),
    }


# ------------------------------------------------------------------ snapshots


def test_units_follow_the_commit_greptile_reviewed():
    fx = _fixture(
        inline=[
            _inline(SHA_B, "b.py", 9, "2026-10-01T02:00:00Z"),
            _inline(SHA_A, "a.py", 3, "2026-10-01T01:00:00Z"),
        ],
        findings=[_finding(1, 1, "b.py", 9), _finding(2, 1, "a.py", 3)],
    )
    first, second = units_for_fixture(fx)
    assert (first.sha, second.sha) == (SHA_A, SHA_B)
    assert [g["id"] for g in first.golden] == ["hickory-1-2"]
    assert first.prior == ()
    assert [g["id"] for g in second.prior] == ["hickory-1-2"]
    assert second.final_head_sha == HEAD and first.greptile_reviewed


def test_clean_pr_uses_last_reviewed_commit_and_missing_greptile_is_flagged():
    (clean,) = units_for_fixture(_fixture())
    assert clean.sha == SHA_B and clean.golden == () and clean.greptile_reviewed
    (missing,) = units_for_fixture(_fixture(summary=False))
    assert missing.sha == HEAD and not missing.greptile_reviewed


def test_frozen_manifest_units_keep_every_finding():
    fixtures = json.loads((HICKORY / "manifest.json").read_text())
    units = load_units(fixtures)
    assert sum(len(u.golden) for u in units) == sum(len(f["findings"]) for f in fixtures) == 77
    assert len({u.unit_id for u in units}) == len(units) == 46
    assert len(load_units(fixtures, "held-out")) == 25
    by_pr = {u.pr_number: u for u in units if u.pr_number == 11918}
    assert by_pr[11918].sha.startswith("c90bde8e") and by_pr[11918].final_head_sha.startswith(
        "653db5a0"
    )


def test_neutral_text_strips_identity_and_badges():
    text = neutral_text(GREPTILE_BODY)
    assert "P1" not in text and "Prompt To Fix" not in text and "secret" not in text
    assert "greptile" not in text.lower() and "[reviewer]" in text
    assert "**Null token crashes refresh**" in text


def test_window_survives_shifts_but_not_edits():
    before = "\n".join(f"line {i}" for i in range(1, 21))
    shifted = "header\nheader\n" + before
    edited = before.replace("line 10", "line ten")
    assert window_survives(before, shifted, 10)
    assert not window_survives(before, edited, 10)
    assert window_survives(before, edited, 18)
    assert not window_survives(before, None, 3)
    assert window_survives(before, before, 0) and not window_survives(before, edited, 0)


async def test_later_evidence_reads_snapshot_and_merged_file():
    fx = _fixture(
        inline=[_inline(SHA_A, "a.py", 3, "2026-10-01T01:00:00Z")],
        findings=[_finding(1, 1, "a.py", 3)],
    )
    (unit,) = units_for_fixture(fx)
    files = {
        ("a.py", SHA_A): "a\nb\nbug\nc\nd\ne\nf\ng\nh",
        ("a.py", HEAD): "a\nb\nfixed\nc\nd\ne\nf\ng\nh",
    }

    class Files:
        async def get_file_content(self, info, path, ref):
            if (path, ref) not in files:
                raise RuntimeError("404")
            return files[(path, ref)]

    ev = await later_evidence(Files(), unit, {("a.py", 3), ("a.py", 8), ("gone.py", 1)})
    assert ev["changed_before_merge"] == {"a.py:3": True, "a.py:8": False}
    assert "gone.py" in ev["error"]


async def test_pinned_provider_diffs_the_snapshot_with_fixture_text_only(monkeypatch):
    (unit,) = units_for_fixture(_fixture())
    calls = {}

    async def fake_info(self, url):
        return PRInfo(
            title="Live title edited after review",
            description="Fix auth\nAddressed Greptile P1 on refresh\nAlso guards the null token",
            base_branch="main",
            head_branch="h",
            url=url,
            number=1,
            owner="o",
            repo="r",
            head_sha=HEAD,
        )

    async def fake_compare(self, info, base, head):
        calls["range"] = (base, head)
        return "diff"

    monkeypatch.setattr("mira.providers.github.GitHubProvider.get_pr_info", fake_info)
    monkeypatch.setattr("mira.providers.github.GitHubProvider.get_compare_diff", fake_compare)
    provider = PinnedGitHubProvider("tok", unit)
    info = await provider.get_pr_info(unit.pr_url)
    assert info.head_sha == SHA_B
    # HAI-12817: the fixture title, and no live description at all.
    assert info.title == "t" and info.description == ""
    assert await provider.get_pr_description(info) == ""
    assert await provider.get_pr_diff(info) == "diff"
    assert calls["range"] == (unit.base_sha, SHA_B)
    assert await provider.get_all_bot_threads(info, "bot") == []


async def test_pinned_provider_reads_source_tree_and_history_at_the_snapshot(monkeypatch):
    """The live branch has moved on; every reviewer read still sees ``unit.sha``."""
    (unit,) = units_for_fixture(_fixture())
    refs: list[tuple[str, str]] = []
    history_params: list[dict] = []

    async def fake_info(self, url):
        return PRInfo(
            title="t",
            description="",
            base_branch="main",
            head_branch="feature/moving",
            url=url,
            number=1,
            owner="o",
            repo="r",
            head_sha=HEAD,
        )

    async def fake_content(self, info, path, ref):
        refs.append(("content", ref))
        return "x"

    async def fake_tree(self, info, ref):
        refs.append(("tree", ref))
        return ["a.py"]

    async def fake_token(self):
        return "tok"

    def handler(request: httpx.Request) -> httpx.Response:
        history_params.append(dict(request.url.params))
        return httpx.Response(
            200,
            json=[
                {
                    "sha": SHA_A,
                    "commit": {
                        "message": "Add refresh\n\nbody",
                        "author": {"name": "dev", "date": "2026-09-30T00:00:00Z"},
                    },
                }
            ],
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.setattr("mira.providers.github.GitHubProvider.get_pr_info", fake_info)
    monkeypatch.setattr("mira.providers.github.GitHubProvider.get_file_content", fake_content)
    monkeypatch.setattr("mira.providers.github.GitHubProvider.get_repo_tree", fake_tree)
    monkeypatch.setattr("mira.providers.github.GitHubProvider._resolve_token", fake_token)

    provider = PinnedGitHubProvider("tok", unit)
    info = await provider.get_pr_info(unit.pr_url)

    from mira.index.context import ProviderSourceFetcher

    # Exactly what the engine does: fetch at pr_info.head_branch.
    await ProviderSourceFetcher(provider, info, info.head_branch).fetch("a.py")
    await provider.get_repo_tree(info, info.head_branch)
    # Explicit SHAs (later evidence reads the merged head) are left alone.
    await provider.get_file_content(info, "a.py", HEAD)
    assert refs == [("content", SHA_B), ("tree", SHA_B), ("content", HEAD)]

    history = await provider.get_file_history(info, ["a.py"], max_per_file=3)
    assert history_params == [{"path": "a.py", "sha": SHA_B, "per_page": "3"}]
    (entry,) = history["a.py"]
    assert entry.sha == SHA_A[:8] and entry.message == "Add refresh"


# ---------------------------------------------------------------------- judge


def _c(cid: str, path: str = "a.py", line: int = 3, title: str = "t") -> dict:
    return {"id": cid, "path": path, "line": line, "title": title, "body": "b"}


def test_pool_is_blind_and_reproducible():
    kory, grep, prior = [_c("k0"), _c("k1")], [_c("g0")], [_c("p0")]
    pool1, seed1 = build_pool("u1", "registry_on|r1", kory, grep, prior, 7)
    pool2, seed2 = build_pool("u1", "registry_on|r1", kory, grep, prior, 7)
    assert pool1 == pool2 and seed1 == seed2
    orders = {
        tuple(c["source_id"] for c in build_pool("u1", f"r{i}", kory, grep, prior, 7)[0])
        for i in range(12)
    }
    assert len(orders) > 1
    prompt = build_prompt("t", "diff --git a/a.py b/a.py\n+x", pool1)
    for word in (KORY, GREPTILE, PRIOR, "k0", "g0"):
        assert word not in prompt.lower()


def test_normalize_makes_every_comment_judged_once():
    norm = normalize_response(
        {
            "issues": [
                {"comment_indices": [0, 0, 9], "verdict": "real", "severity": "major"},
                {"comment_indices": [0, 1], "verdict": "bogus", "severity": "?"},
            ],
            "evidence": [{"comment_index": 1, "quality": 7}],
        },
        3,
    )
    members = sorted(i for issue in norm["issues"] for i in issue["members"])
    assert members == [0, 1, 2]
    assert norm["issues"][1]["verdict"] == "uncertain" and norm["issues"][1]["severity"] == "minor"
    assert norm["issues"][2]["judged"] is False
    assert norm["quality"] == {1: 3}


def _pool(owners: list[str]) -> list[dict]:
    return [
        {
            "index": i,
            "owner": o,
            "source_id": f"{o}{i}",
            "path": "a.py",
            "line": 10 + 20 * i,
            "title": "t",
            "body": "b",
        }
        for i, o in enumerate(owners)
    ]


def test_score_unit_maps_back_to_reviewers():
    pool = _pool([KORY, GREPTILE, KORY, GREPTILE, PRIOR, KORY])
    raw = {
        "issues": [
            {"comment_indices": [0, 1], "verdict": "real", "severity": "critical"},  # both
            {"comment_indices": [2], "verdict": "not_real", "severity": "minor"},  # kory FP
            {"comment_indices": [3], "verdict": "real", "severity": "major"},  # greptile only
            {"comment_indices": [4, 5], "verdict": "real", "severity": "major"},  # prior: excluded
        ],
        "evidence": [{"comment_index": 0, "quality": 3}, {"comment_index": 1, "quality": 1}],
    }
    s = score_unit(pool, raw, {"changed_before_merge": {"a.py:70": True, "a.py:10": False}})
    assert (s[KORY]["tp"], s[KORY]["fp"], s[KORY]["missed"]) == (1, 1, 1)
    assert (s[GREPTILE]["tp"], s[GREPTILE]["fp"], s[GREPTILE]["missed"]) == (2, 0, 0)
    assert s[KORY]["weighted_tp"] == 4 and s[GREPTILE]["weighted_tp"] == 6
    assert s[KORY]["evidence_sum"] == 3 and s[GREPTILE]["evidence_sum"] == 1
    assert s["golden"] == {"tp": 1, "fn": 1, "fp": 1}
    assert s["agreement"]["both_real"] == 1 and s["agreement"]["kory_only_not_real"] == 1
    assert s[GREPTILE]["corroborated_tp"] == 1  # line 70 changed before merge
    assert s["cross_check"]["real_fixed_later"] == 1


async def test_judge_unit_records_everything_needed_to_rescore():
    class Judge:
        async def complete_with_tools(self, messages, tools, temperature=None):
            return json.dumps(
                {
                    "issues": [{"comment_indices": [0, 1], "verdict": "real", "severity": "major"}],
                    "evidence": [],
                }
            )

    rec = await judge_unit(
        Judge(),
        unit_id="u",
        run_label="r",
        pr_title="t",
        diff_text="",
        kory=[_c("k0")],
        greptile=[_c("g0")],
        prior=[],
        seed=1,
    )
    assert rec["parsed"]["issues"] and rec["prompt_sha256"] and not rec["error"]
    assert score_unit(rec["pool"], rec["parsed"])[KORY]["tp"] == 1


async def test_judge_failure_is_recorded_not_raised():
    class Broken:
        async def complete_with_tools(self, messages, tools, temperature=None):
            raise RuntimeError("seat down")

    rec = await judge_unit(
        Broken(),
        unit_id="u",
        run_label="r",
        pr_title="t",
        diff_text="",
        kory=[_c("k0")],
        greptile=[],
        prior=[],
        seed=1,
    )
    assert (
        "seat down" in rec["error"]
        and score_unit(rec["pool"], rec["parsed"])["unjudged_comments"] == 1
    )


# ---------------------------------------------------------------------- stats


def test_registry_ab_two_sigma_rule():
    keep = registry_ab([40.0, 41.0, 42.0], [30.0, 31.0, 32.0], blocks_seen=5)
    assert keep["decision"] == "on" and keep["off"]["sigma"] == pytest.approx(0.8165, abs=1e-3)
    drop = registry_ab([31.5, 32.0, 32.5], [30.0, 31.0, 32.0], blocks_seen=5)
    assert drop["decision"] == "off"
    empty = registry_ab([40.0, 41.0, 42.0], [30.0, 31.0, 32.0], blocks_seen=0)
    assert empty["decision"] == "off" and "identical" in empty["why"]
    human = registry_ab([30.0], [31.0], blocks_seen=1, override="Zane Claes, 2026-10-29: keep on")
    assert human["decision"] == "on" and human["override"].startswith("Zane")


def _rows(deltas: list[tuple[float, float]]) -> list[dict]:
    return [
        {"pr_url": f"pr{i % 12}", "kory_weighted_tp": k, "greptile_weighted_tp": g}
        for i, (k, g) in enumerate(deltas)
    ]


def test_parity_verdicts():
    good = {"precision": 0.6}
    grep = {"precision": 0.55}
    better = parity_verdict(_rows([(5, 2)] * 24), good, grep, 1.0, False)
    assert better["verdict"] == "better"
    same = parity_verdict(_rows([(2, 2), (2.2, 2), (1.9, 2)] * 8), good, grep, 1.0, False)
    assert same["verdict"] == "as_good"
    worse = parity_verdict(_rows([(0, 4)] * 24), good, grep, 1.0, False)
    assert worse["verdict"] == "worse"
    noisy = parity_verdict(_rows([(5, 2)] * 24), {"precision": 0.2}, grep, 1.0, False)
    assert noisy["verdict"] == "worse" and not noisy["precision_ok"]
    few = parity_verdict(_rows([(5, 2)] * 5), good, grep, 1.0, False)
    assert few["verdict"] == "inconclusive"
    overlap = parity_verdict(_rows([(5, 2)] * 24), good, grep, 1.0, True)
    assert overlap["verdict"] == "inconclusive"
    low_cov = parity_verdict(_rows([(5, 2)] * 24), good, grep, 0.5, False)
    assert low_cov["verdict"] == "inconclusive"
    judge_down = parity_verdict(_rows([(5, 2)] * 24), good, grep, 1.0, False, judge_error_rate=0.25)
    assert judge_down["verdict"] == "inconclusive"
    assert "judge failed" in judge_down["inconclusive_reasons"][0]


def test_model_family():
    assert model_family("claude@22e78/opus") == "anthropic"
    assert model_family("codex/gpt-5.6-sol") == "openai"
    assert model_family("grok/grok-4.6") == "xai"
    assert model_family("auto") == "unknown"


def test_totals_empty_and_filled():
    s = score_unit(
        _pool([KORY]),
        {
            "issues": [{"comment_indices": [0], "verdict": "real", "severity": "nit"}],
            "evidence": [],
        },
    )
    t = totals([s], KORY)
    assert t["tp"] == 1 and t["weighted_tp"] == 0 and t["recall"] == 1.0


# ---------------------------------------------------------------- end to end


class FakeEngine:
    """Stands in for ReviewEngine: reads the registry block like the real one."""

    fail_on: set[str] = set()

    def __init__(self, config, llm, provider, dry_run):
        self.provider = provider

    async def review_pr(self, url):
        import mira.registry.prompt as rp

        unit = self.provider.unit
        if unit.unit_id in self.fail_on:
            raise RuntimeError("seat outage")
        info = SimpleNamespace(owner="o", repo="r")
        block = rp.registry_rule(info, ["a.py"])
        comments = [
            ReviewComment(
                path="a.py",
                line=3,
                end_line=None,
                severity=Severity.WARNING,
                category="bug",
                title="Null token",
                body="Mira says boom",
                confidence=0.9,
            )
        ]
        if block:
            comments.append(
                ReviewComment(
                    path="a.py",
                    line=30,
                    end_line=None,
                    severity=Severity.WARNING,
                    category="bug",
                    title="Violates reg rule",
                    body="b",
                    confidence=0.9,
                )
            )
        return SimpleNamespace(comments=comments)


class FakeJudge:
    """Marks every cluster real; groups comments that share a line."""

    def __init__(self):
        self.prompts = []

    async def complete_with_tools(self, messages, tools, temperature=None):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        import re

        by_line: dict[str, list[int]] = {}
        for idx, line in re.findall(r"^\[(\d+)\] a\.py:(\d+)", prompt, re.M):
            by_line.setdefault(line, []).append(int(idx))
        return json.dumps(
            {
                "issues": [
                    {"comment_indices": v, "verdict": "real", "severity": "major"}
                    for v in by_line.values()
                ],
                "evidence": [
                    {"comment_index": i, "quality": 2} for v in by_line.values() for i in v
                ],
            }
        )


class FakeProvider:
    def __init__(self, token, unit):
        self.unit = unit

    async def get_pr_diff(self, info):
        return "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,3 @@\n-x\n+y\n"

    async def get_file_content(self, info, path, ref):
        lines = [f"l{i}" for i in range(1, 40)]
        if ref == HEAD:
            lines[2] = "fixed"  # line 3 changed before merge, line 30 did not
        return "\n".join(lines)


class FakeRegistry:
    def context_for_paths(self, repo, paths, include_proposals=False):
        return [
            {
                "id": "reg_1",
                "kind": "rule",
                "status": "approved",
                "title": "No nulls",
                "summary": "",
                "body": "x",
                "confidence": 0.9,
                "anchors": [],
                "scope": "repo",
                "decided_at": 1.0,
                "created_at": 1.0,
            },
            {
                "id": "reg_2",
                "kind": "rule",
                "status": "approved",
                "title": "From the future",
                "summary": "",
                "body": "x",
                "confidence": 0.9,
                "anchors": [],
                "scope": "repo",
                "decided_at": 4e9,
                "created_at": 4e9,
            },
        ]


async def _artifact(monkeypatch, fail_on=frozenset(), engine=None):
    engine = engine or FakeEngine
    monkeypatch.setattr(parity_runner, "ReviewEngine", engine)
    monkeypatch.setattr(FakeEngine, "fail_on", set(fail_on))
    monkeypatch.setattr("mira.registry.get_registry", lambda create=True: FakeRegistry())
    judge = FakeJudge()

    def llm_factory(cfg):
        return judge if cfg.model == "grok/grok-4.6" else object()

    async def no_reverts(*a):
        return []

    fx = [
        _fixture(
            1, [_inline(SHA_A, "a.py", 3, "2026-10-01T01:00:00Z")], [_finding(1, 1, "a.py", 3)]
        ),
        _fixture(2, summary=False),
    ]
    units = load_units(fx)
    cfg = parity_runner.ParityConfig(
        reviewer_llm=LLMConfig(
            provider="router-acp", model="claude@x/opus", router_acp_token="secret"
        ),
        judge_llm=LLMConfig(provider="router-acp", model="grok/grok-4.6"),
        runs=2,
    )
    art = await parity_runner.run_parity(
        units,
        cfg,
        "tok",
        provider_factory=FakeProvider,
        llm_factory=llm_factory,
        revert_finder=no_reverts,
    )
    return art, judge


async def test_run_parity_end_to_end(monkeypatch):
    art, judge = await _artifact(monkeypatch)
    assert art["config"]["reviewer_llm"]["router_acp_token"] == ""
    # Only the Greptile-reviewed unit is reviewed: 2 arms x 2 runs.
    assert len(art["records"]) == 4 and {r["unit_id"] for r in art["records"]} == {
        f"1@{SHA_A[:12]}"
    }
    assert all("mira" not in p.lower() and "greptile" not in p.lower() for p in judge.prompts)

    on = [r for r in art["records"] if r["arm"] == "registry_on"]
    off = [r for r in art["records"] if r["arm"] == "registry_off"]
    assert all(r["review"]["registry_blocks"][0] for r in on)
    assert all(r["review"]["registry_blocks"] == [""] for r in off)
    assert all(len(r["review"]["comments"]) == 2 for r in on) and all(
        len(r["review"]["comments"]) == 1 for r in off
    )

    summary = json.loads(json.dumps(parity_runner.summarize(art)))
    assert summary["coverage"]["greptile_missing_units"] == [f"2@{HEAD[:12]}"]
    on_arm = summary["arms"]["registry_on"]
    assert on_arm["kory_totals"]["tp"] == 4 and on_arm["greptile_totals"]["tp"] == 2
    assert summary["arms"]["registry_off"]["golden_f1"]["mean"] == 100.0
    assert summary["registry_ab"]["blocks_seen"] == 2
    assert summary["judge"]["family_overlap"] == []
    assert on_arm["cross_check"]["real_fixed_later"] == 2  # the a.py:3 issue, once per run
    assert on_arm["kory_totals"]["corroborated_tp"] == 2
    assert summary["parity_verdict"] == "inconclusive"  # one unit is below the minimum sample
    # Reproducible: a JSON round trip of the artifact rescored gives the same summary.
    again = parity_runner.summarize(json.loads(json.dumps(art)))
    assert json.loads(json.dumps(again)) == summary
    md = parity_runner.render_markdown(summary)
    for section in ("Registry A/B", "## Attribution", "## Uncertainty", "## Seat usage"):
        assert section in md


async def test_registry_as_of_drops_later_approvals(monkeypatch):
    art, judge = await _artifact(monkeypatch)
    on_prompt = next(p for p in judge.prompts if "a.py:30" in p)
    assert "From the future" not in on_prompt  # the block itself never reaches the judge
    monkeypatch.setattr("mira.registry.get_registry", lambda create=True: FakeRegistry())
    token = parity_runner._as_of.set(2e9)
    try:
        block = parity_runner._registry_rule_as_of(SimpleNamespace(owner="o", repo="r"), ["a.py"])
    finally:
        parity_runner._as_of.reset(token)
    assert "No nulls" in block["content"] and "From the future" not in block["content"]


async def test_arm_owns_the_engine_registry_hook(monkeypatch):
    import mira.core.engine as engine_module

    monkeypatch.setattr("mira.registry.get_registry", lambda create=True: FakeRegistry())
    pr_info = SimpleNamespace(owner="o", repo="r")
    files = [SimpleNamespace(path="a.py")]
    for arm, expect_block in (("registry_off", False), ("registry_on", True)):
        parity_runner.install_arm(arm)
        engine, rules = SimpleNamespace(_registry_findings=["stale"]), []
        await engine_module.apply_registry(engine, pr_info, files, rules, "", "t", "b")
        assert engine._registry_findings == []  # no contradiction check in either arm
        assert bool(rules) is expect_block


async def test_registry_search_answers_nothing_in_both_arms(monkeypatch):
    import mira.registry.prompt as registry_prompt

    def no_registry(create=True):
        raise AssertionError("the harness must not search the live registry")

    monkeypatch.setattr("mira.registry.get_registry", no_registry)

    class Inner:
        call_log: list = []

        async def execute(self, name, args):
            return f"inner {name}"

    for arm in ("registry_off", "registry_on"):
        parity_runner.install_arm(arm)
        inner = Inner()
        inner.call_log = []
        executor = registry_prompt.with_registry_tool(inner, SimpleNamespace(owner="o", repo="r"))
        assert (
            await executor.execute("search_decisions", {"query": "q"}) == "[registry unavailable]"
        )
        assert await executor.execute("read_file", {"path": "a.py"}) == "inner read_file"
        assert executor.call_log == [{"tool": "search_decisions", "arg": "q"}]


class TrailEngine(FakeEngine):
    async def review_pr(self, url):
        result = await super().review_pr(url)
        result.audit = [
            {"stage": "agentic", "chunk": 0, "calls": [{"tool": "read_file", "arg": "b.py"}]},
            {"stage": "drafted", "chunk": 0, "count": 3},
            {"stage": "noise_filter", "path": "a.py", "line": 9, "title": "x"},
            {"stage": "critique", "path": "a.py", "line": 12, "title": "y"},
        ]
        result.reviewed_paths = ["a.py"]
        result.skipped_paths = ["big.lock"]
        result.skipped_reason = None
        return result


async def test_review_records_the_pipeline_trail_without_changing_scores(monkeypatch):
    plain, _ = await _artifact(monkeypatch)
    art, _ = await _artifact(monkeypatch, engine=TrailEngine)
    assert art["harness"] == parity_runner.HARNESS
    review = art["records"][0]["review"]
    assert review["reviewed_paths"] == ["a.py"] and review["skipped_paths"] == ["big.lock"]
    assert review["skipped_reason"] == "" and len(review["audit"]) == 4

    summary = json.loads(json.dumps(parity_runner.summarize(art)))
    pipe = summary["arms"]["registry_off"]["pipeline"]
    assert pipe == {
        "reviews_with_audit": 2,
        "drafted": 6,
        "dropped_by_stage": {"noise_filter": 2, "critique": 2},
        "tool_calls": {"read_file": 2},
        "reviews_using_tools": 2,
        "skipped_paths": 2,
    }
    assert "## Pipeline (informational)" in parity_runner.render_markdown(summary)
    base = json.loads(json.dumps(parity_runner.summarize(plain)))
    for arm in ("registry_on", "registry_off"):
        assert summary["arms"][arm]["kory_totals"] == base["arms"][arm]["kory_totals"]
        assert summary["arms"][arm]["parity"] == base["arms"][arm]["parity"]


async def test_artifact_from_before_the_trail_still_rescores(monkeypatch):
    art, _ = await _artifact(monkeypatch)
    new = parity_runner.summarize(json.loads(json.dumps(art)))
    old = json.loads(json.dumps(art))
    old.pop("harness")
    for rec in old["records"]:
        for key in ("audit", "reviewed_paths", "skipped_paths", "skipped_reason"):
            rec["review"].pop(key)
    summary = parity_runner.summarize(old)
    assert summary["harness"] == ""
    assert summary["arms"]["registry_off"]["pipeline"]["reviews_with_audit"] == 0
    for arm in ("registry_on", "registry_off"):
        assert summary["arms"][arm]["kory_totals"] == new["arms"][arm]["kory_totals"]
    parity_runner.render_markdown(summary)


async def test_seat_meter_refuses_past_the_cap():
    sent = []

    async def transport(request):
        sent.append(request)
        return {"ok": True}

    meter = parity_runner.SeatMeter(2)
    llm = meter.guard(SimpleNamespace(_transport=transport))
    await llm._transport({})
    await llm._transport({})
    with pytest.raises(parity_runner.SeatBudgetExhausted):
        await llm._transport({})
    assert len(sent) == 2 and meter.as_dict() == {
        "budget": 2,
        "used": 2,
        "refused": 1,
        "exhausted": True,
    }
    assert parity_runner.SeatMeter(0).exhausted is False  # 0 = no cap


def test_exhausted_budget_is_inconclusive():
    rows = [
        {"pr_url": f"p{i}", "kory_weighted_tp": 3, "greptile_weighted_tp": 1} for i in range(30)
    ]
    tot = {"precision": 1.0}
    assert parity_verdict(rows, tot, tot, 1.0, False)["verdict"] == "better"
    capped = parity_verdict(rows, tot, tot, 1.0, False, budget_exhausted=True)
    assert (
        capped["verdict"] == "inconclusive" and "seat budget" in capped["inconclusive_reasons"][0]
    )


def test_router_acp_llm_is_built_directly():
    from mira.llm.router_acp import RouterAcpProvider, UnconfiguredSeatProvider

    llm = parity_runner.make_llm(LLMConfig(provider="router-acp", model="grok-4"))
    assert type(llm) is RouterAcpProvider and not isinstance(llm, UnconfiguredSeatProvider)


async def test_judge_llm_has_no_reviewer_framing(monkeypatch):
    monkeypatch.setenv("MIRA_ROUTER_ACP_ALLOW_UNTRACED", "1")
    sent: list[dict] = []

    async def transport(request):
        sent.append(request)
        return {"text": '{"issues": []}', "actual_model": "grok@x/grok-4.7"}

    llm = parity_runner.make_judge_llm(
        LLMConfig(provider="router-acp", model="grok@x/grok-4.7", router_acp_url="http://x")
    )
    llm._transport = transport
    out = await llm.complete_with_tools(
        [{"role": "user", "content": "JUDGE BODY"}], tools=[SUBMIT_ISSUES_TOOL]
    )
    assert out == '{"issues": []}'
    prompt = json.dumps(sent[0])
    assert "JUDGE BODY" in prompt and "submit_issues" in prompt
    assert "Mira" not in prompt


async def test_kory_crash_counts_as_misses(monkeypatch):
    art, _ = await _artifact(monkeypatch, fail_on={f"1@{SHA_A[:12]}"})
    summary = parity_runner.summarize(art)
    arm = summary["arms"]["registry_off"]
    assert arm["coverage"]["kory_rate"] == 0.0
    assert arm["kory_totals"]["tp"] == 0 and arm["kory_totals"]["missed"] == 2
    assert arm["greptile_totals"]["tp"] == 2


def _scripted_reviewer(models: list[str], **cfg):
    from mira.llm.router_acp import RouterAcpProvider

    sent: list[dict] = []

    async def transport(request):
        sent.append(request)
        return {"text": '{"comments": []}', "actual_model": models[len(sent) - 1]}

    async def no_sleep(_):
        return None

    llm = RouterAcpProvider(
        LLMConfig(provider="router-acp", model="auto", router_acp_url="http://x", **cfg),
        transport=transport,
        sleep=no_sleep,
    )
    return llm, sent


@pytest.mark.parametrize("refused", ["grok@x/grok-4.7", "unknown", ""])
async def test_excluded_reviewer_reply_is_a_failed_call_and_retried(monkeypatch, refused):
    from mira.llm.router_acp import RouterAcpProvider, attribution_key

    monkeypatch.setenv("MIRA_ROUTER_ACP_ALLOW_UNTRACED", "1")
    monkeypatch.setattr(RouterAcpProvider, "call_log", type(RouterAcpProvider.call_log)())
    llm, sent = _scripted_reviewer([refused, "claude@x/opus"])
    meter = parity_runner.SeatMeter(10)
    llm = meter.guard(parity_runner.exclude_reviewer_families(llm, {"xai"}))
    token = attribution_key.set("review|t")
    try:
        assert await llm.review([{"role": "user", "content": "diff"}]) == '{"comments": []}'
    finally:
        attribution_key.reset(token)
    log = [(e["state"], e["actual_model"], e["attempts"]) for e in RouterAcpProvider.call_log]
    assert log == [
        (parity_runner.EXCLUDED_STATE, refused or "unknown", 1),
        ("ok", "claude@x/opus", 1),
    ]
    assert len(sent) == 2 and meter.used == 2  # the refused reply still used a seat session


async def test_excluded_reviewer_retries_stop_at_the_retry_budget(monkeypatch):
    from mira.exceptions import NonRetriableLLMError
    from mira.llm.router_acp import RouterAcpProvider

    monkeypatch.setenv("MIRA_ROUTER_ACP_ALLOW_UNTRACED", "1")
    monkeypatch.setattr(RouterAcpProvider, "call_log", type(RouterAcpProvider.call_log)())
    llm, sent = _scripted_reviewer(["grok@x/grok-4.7"] * 5, router_acp_max_retries=2)
    llm = parity_runner.exclude_reviewer_families(llm, {"xai"})
    with pytest.raises(NonRetriableLLMError):
        await llm.review([{"role": "user", "content": "diff"}])
    assert len(sent) == 3  # 1 call + router_acp_max_retries
    assert [e["state"] for e in RouterAcpProvider.call_log] == [parity_runner.EXCLUDED_STATE] * 3


async def test_run_parity_never_scores_a_judge_family_reviewer_reply(monkeypatch):
    from mira.llm.router_acp import RouterAcpProvider

    monkeypatch.setenv("MIRA_ROUTER_ACP_ALLOW_UNTRACED", "1")
    monkeypatch.setattr(RouterAcpProvider, "call_log", type(RouterAcpProvider.call_log)())

    class CallingEngine(FakeEngine):
        def __init__(self, config, llm, provider, dry_run):
            super().__init__(config, llm, provider, dry_run)
            self.llm = llm

        async def review_pr(self, url):
            await self.llm.review([{"role": "user", "content": "diff"}])
            return await super().review_pr(url)

    monkeypatch.setattr(parity_runner, "ReviewEngine", CallingEngine)
    monkeypatch.setattr("mira.registry.get_registry", lambda create=True: FakeRegistry())
    judge = FakeJudge()
    # Every review's first reply comes from the judge's family, the retry from Claude.
    replies = iter(["grok@x/grok-4.6", "claude@x/opus"] * 4)

    def llm_factory(cfg):
        if cfg.model == "grok/grok-4.6":
            return judge

        async def transport(request):
            return {"text": '{"comments": []}', "actual_model": next(replies)}

        return RouterAcpProvider(cfg, transport=transport)

    async def no_reverts(*a):
        return []

    units = load_units(
        [
            _fixture(
                1, [_inline(SHA_A, "a.py", 3, "2026-10-01T01:00:00Z")], [_finding(1, 1, "a.py", 3)]
            )
        ]
    )
    cfg = parity_runner.ParityConfig(
        reviewer_llm=LLMConfig(provider="router-acp", model="auto", router_acp_url="http://x"),
        judge_llm=LLMConfig(provider="router-acp", model="grok/grok-4.6"),
        runs=2,
        seat_budget=100,
    )
    art = await parity_runner.run_parity(
        units,
        cfg,
        "tok",
        provider_factory=FakeProvider,
        llm_factory=llm_factory,
        revert_finder=no_reverts,
    )
    assert all(not r["review"]["error"] for r in art["records"])
    assert art["seat_meter"]["used"] == 8  # 4 reviews x (refused + retry)
    summary = parity_runner.summarize(art)
    assert summary["judge"]["family_overlap"] == []
    review_usage = summary["seat_usage"]["review"]
    assert review_usage["failed"] == 4 and review_usage["seat_sessions"] == 8


async def test_same_family_judge_is_refused():
    cfg = parity_runner.ParityConfig(
        reviewer_llm=LLMConfig(provider="router-acp", model="claude@x/opus"),
        judge_llm=LLMConfig(provider="router-acp", model="claude@y/sonnet"),
    )
    with pytest.raises(SystemExit, match="anthropic"):
        await parity_runner.run_parity([], cfg, "tok")


# --------------------------------------------------------------- export + CLI


async def test_export_pr_builds_manifest_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/graphql"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "repository": {
                            "pullRequest": {
                                "reviewThreads": {
                                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                                    "nodes": [
                                        {
                                            "isResolved": True,
                                            "isOutdated": False,
                                            "comments": {"nodes": [{"databaseId": 11}]},
                                        }
                                    ],
                                }
                            }
                        }
                    }
                },
            )
        if path.endswith("/pulls/5"):
            return httpx.Response(
                200,
                json={
                    "html_url": "https://github.com/o/r/pull/5",
                    "title": "T",
                    "merged_at": "2026-10-01T00:00:00Z",
                    "base": {"sha": "0" * 40},
                    "head": {"sha": HEAD},
                },
            )
        if path.endswith("/issues/5/comments"):
            return httpx.Response(
                200,
                json=[
                    {
                        "user": {"login": "greptile-apps[bot]"},
                        "html_url": "s",
                        "created_at": "c",
                        "updated_at": "u",
                        "body": "summary",
                    }
                ],
            )
        if path.endswith("/pulls/5/comments"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 11,
                        "user": {"login": "greptile-apps[bot]"},
                        "html_url": "i",
                        "created_at": "2026-10-01T00:00:00Z",
                        "updated_at": "u",
                        "path": "a.py",
                        "line": None,
                        "original_line": 4,
                        "start_line": None,
                        "original_start_line": None,
                        "commit_id": HEAD,
                        "original_commit_id": SHA_A,
                        "body": GREPTILE_BODY,
                    },
                    {"id": 12, "user": {"login": "someone"}, "body": "human", "created_at": "c"},
                ],
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        pr = await export_pr(GitHubReader("tok", client), "o/r", 5, "nightly")
    assert pr["split"] == "nightly" and len(pr["findings"]) == 1
    assert pr["findings"][0]["severity"] == "blocker" and pr["findings"][0]["line_start"] == 4
    inline = [c for c in pr["greptile_comments"] if c["kind"] == "inline"]
    assert inline[0]["thread_resolved"] is True
    (unit,) = units_for_fixture(pr)
    assert unit.sha == SHA_A


def _cli(*args: str, **env: str) -> subprocess.CompletedProcess:
    root = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [sys.executable, str(HICKORY / "run_parity.py"), *args],
        cwd=root,
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=120,
    )


def test_cli_plan_is_offline():
    out = _cli("plan", "--split", "held-out")
    assert out.returncode == 0, out.stderr
    plan = json.loads(out.stdout)
    assert plan["units_scored"] == 25 and plan["reviews"] == 150 and plan["golden_findings"] == 43


def test_cli_run_refuses_unpinned_judge():
    out = _cli("run", "--judge-model", "auto", GITHUB_TOKEN="x")
    assert out.returncode != 0 and "pin one model" in out.stderr


async def test_cli_rescore_check(monkeypatch, tmp_path):
    art, _ = await _artifact(monkeypatch)
    art["summary"] = json.loads(json.dumps(parity_runner.summarize(art)))
    path = tmp_path / "art.json"
    path.write_text(json.dumps(art))
    ok = _cli("rescore", str(path), "--check")
    assert ok.returncode == 0, ok.stderr + ok.stdout
    art["records"][0]["judge"]["parsed"]["issues"][0]["verdict"] = "not_real"
    path.write_text(json.dumps(art))
    assert _cli("rescore", str(path), "--check").returncode == 1
