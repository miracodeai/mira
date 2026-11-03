"""Kory Source work and decision registry (HAI-11605)."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import jwt
import pytest

from mira.registry import Actor, Forbidden, RegistryError, set_registry
from mira.registry.conflicts import parse_judgment
from mira.registry.db import RegistryDB
from mira.registry.learned import SOURCE_SYSTEM, import_learned_rules
from mira.registry.model import compute_confidence
from mira.registry.prompt import RegistryToolExecutor, registry_rule, render_block
from mira.registry.realization import observe_github
from mira.registry.service import Registry

REPO = "hickoryai/hickory-ai"
AGENT = Actor("agent", "claude-session-1", "claude-opus-5-5")
HUMAN = Actor("human", "zane")
OTHER_HUMAN = Actor("human", "pat")
DAY = 86400.0
ROOT = Path(__file__).resolve().parents[1]
FORK_BASE = "567ed7e98b0155448e3449477a58f58b453fcc2e"


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


class ScriptedJudge:
    """Narrows when the new statement mentions 'only admins', else none."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    async def __call__(self, new: dict, existing: dict) -> dict:
        self.calls.append((new["id"], existing["id"]))
        if self.fail:
            raise RuntimeError("seat unavailable")
        if "only admins" in new["body"]:
            return parse_judgment(
                json.dumps(
                    {
                        "relation": "narrows",
                        "new_clause": "only admins may edit",
                        "existing_clause": "engineers may edit",
                    }
                )
            )
        return parse_judgment('{"relation": "none", "new_clause": "", "existing_clause": ""}')


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def judge() -> ScriptedJudge:
    return ScriptedJudge()


@pytest.fixture
def reg(tmp_path, clock, judge) -> Registry:
    registry = Registry(RegistryDB(path=str(tmp_path / "registry.db")), judge=judge, clock=clock)
    set_registry(registry)
    yield registry
    set_registry(None)


def claim(**extra) -> dict:
    fields = {
        "kind": "decision",
        "title": "Engineers can edit Apps",
        "body": "Engineers may edit Artifact-made Apps.",
        "repo": REPO,
        "session": "sess-1",
        "branch": "feat/apps",
        "anchors": [{"path": "src/apps/perm.py", "qualified_name": "can_edit"}],
    }
    fields.update(extra)
    return fields


async def approved(reg: Registry, **extra) -> dict:
    out = await reg.put_object(claim(**extra), HUMAN)
    return reg.approve(out["object"]["id"], OTHER_HUMAN)


def merge_event(branch: str, *, merged: bool = True, action: str = "closed") -> dict:
    return {
        "action": action,
        "repository": {"full_name": REPO},
        "pull_request": {
            "merged": merged,
            "html_url": "https://github.com/hickoryai/hickory-ai/pull/7",
            "merge_commit_sha": "abc123" if merged else None,
            "head": {"ref": branch, "repo": {"full_name": REPO}},
        },
    }


# ── confidence ──────────────────────────────────────────────────────────


async def test_caller_confidence_and_provenance_are_ignored(reg):
    out = await reg.put_object(
        claim(confidence=0.99, status="approved", decided_by="ceo", pr="x", realization="merged"),
        AGENT,
    )
    obj = out["object"]
    assert obj["confidence"] == pytest.approx(0.40)
    assert obj["status"] == "proposed"
    assert obj["realization"] == "stated"
    assert obj["decided_by"] == AGENT.sub
    assert obj["model"] == AGENT.model
    assert obj["pr"] == ""
    assert set(out["ignored_fields"]) == {"confidence", "status", "decided_by", "pr", "realization"}


def test_every_merged_approval_outranks_every_stated_proposal():
    now = 100 * DAY
    lowest_merged = min(
        compute_confidence(
            status="approved", author_kind=a, realization=r, created_at=c, restored_at=0, now=now
        )
        for a in ("human", "agent")
        for r in ("merged", "merged_with_conflict")
        for c in (0, now - 10 * DAY, now)
    )
    highest_stated = max(
        compute_confidence(
            status="proposed",
            author_kind=a,
            realization="stated",
            created_at=c,
            restored_at=0,
            now=now,
        )
        for a in ("human", "agent")
        for c in (0, now - 10 * DAY, now)
    )
    assert lowest_merged > highest_stated


async def test_claims_need_session_and_branch(reg):
    with pytest.raises(RegistryError, match="session and branch"):
        await reg.put_object(claim(session=""), AGENT)


# ── prompt block and search tool ────────────────────────────────────────


async def test_proposed_objects_never_reach_the_prompt(reg):
    proposal = (await reg.put_object(claim(title="Proposed rule", body="x"), AGENT))["object"]
    decision = await approved(reg, title="Approved rule", body="y")

    class PR:
        owner, repo = REPO.split("/")

    block = registry_rule(PR, ["src/apps/perm.py"])
    assert block is not None
    assert decision["id"] in block["content"]
    assert f"(registry: {decision['id']})" in block["content"]
    assert proposal["id"] not in block["content"]
    assert registry_rule(PR, ["unrelated.py"]) is None


async def test_org_scope_rules_join_the_block_and_proposals_stay_out():
    objs = [
        {
            "id": "reg_1",
            "kind": "rule",
            "status": "proposed",
            "title": "p",
            "summary": "",
            "body": "",
            "scope": "org",
            "confidence": 0.4,
        },
    ]
    assert render_block(objs) is None


async def test_search_decisions_tool_returns_only_approved(reg):
    await reg.put_object(claim(title="Proposal on perm"), AGENT)
    decision = await approved(reg, title="Approved on perm")

    class Inner:
        call_log: list = []

        async def execute(self, name, args):
            return "inner"

    executor = RegistryToolExecutor(Inner(), REPO)
    out = await executor.execute("search_decisions", {"path": "src/apps/perm.py"})
    assert decision["id"] in out
    assert "Proposal on perm" not in out
    assert await executor.execute("read_file", {"path": "a"}) == "inner"


# ── import idempotency (HAI-11606 seam) ─────────────────────────────────


async def test_import_replay_is_idempotent_and_versioned(reg):
    items = [
        {"source_id": "HAI-1", "source_version": 1, "kind": "work", "title": "Parent"},
        {
            "source_id": "HAI-2",
            "source_version": 1,
            "kind": "work",
            "title": "Child",
            "links": [{"type": "blocks", "target_source_id": "HAI-3"}],
        },
    ]
    first = reg.import_items("linear", items, HUMAN)
    assert [r["outcome"] for r in first] == ["created", "created"]
    assert first[1]["sync_status"] == "pending_links"

    # Replay with the missing target: same versions no-op but the pending link resolves.
    items.append({"source_id": "HAI-3", "source_version": 1, "kind": "work", "title": "Dep"})
    second = reg.import_items("linear", items, HUMAN)
    assert [r["outcome"] for r in second] == ["unchanged", "unchanged", "created"]
    assert second[1]["sync_status"] == "synced"
    child = reg.by_source("linear", "HAI-2")
    assert len(reg.relationships(child["id"], direction="out", type="blocks")) == 1

    # Exact replay: nothing changes, no duplicate objects or links.
    third = reg.import_items("linear", items, HUMAN)
    assert {r["outcome"] for r in third} == {"unchanged"}
    assert len(reg.works()) == 3
    assert len(reg.relationships(child["id"], direction="out", type="blocks")) == 1

    # Older version ignored; newer updates the row and replaces its links.
    stale = reg.import_items("linear", [{**items[1], "source_version": 0, "title": "Old"}], HUMAN)
    assert stale[0]["outcome"] == "stale"
    newer = {
        **items[1],
        "source_version": 2,
        "title": "Child v2",
        "links": [{"type": "relates", "target_source_id": "HAI-1"}],
    }
    assert reg.import_items("linear", [newer], HUMAN)[0]["outcome"] == "updated"
    child = reg.get(child["id"])
    assert child["title"] == "Child v2"
    assert reg.relationships(child["id"], direction="out", type="blocks") == []
    assert len(reg.relationships(child["id"], direction="out", type="relates")) == 1
    assert reg.sync_status("linear") == {"synced": 3}


async def test_imported_claims_are_proposed_and_never_expire(reg, clock):
    reg.import_items(
        "linear",
        [
            {
                "source_id": "HAI-9#d1",
                "source_version": 1,
                "kind": "decision",
                "title": "Imported",
                "status": "approved",
            }
        ],
        AGENT,
    )
    obj = reg.by_source("linear", "HAI-9#d1")
    assert obj["status"] == "proposed"
    clock.now += 30 * DAY
    assert reg.expire() == []


# ── realization and merge ───────────────────────────────────────────────


async def test_branch_events_drive_realization_to_approval(reg):
    obj = (await reg.put_object(claim(title="Unrelated", body="Cache the list."), AGENT))["object"]
    assert obj["conflict_check"] == "done"
    observe_github("push", {"ref": "refs/heads/feat/apps", "repository": {"full_name": REPO}}, reg)
    assert reg.get(obj["id"])["realization"] == "in_progress"
    observe_github("pull_request", merge_event("feat/apps", merged=False, action="opened"), reg)
    assert reg.get(obj["id"])["realization"] == "submitted"
    observe_github("pull_request", merge_event("feat/apps"), reg)
    done = reg.get(obj["id"])
    assert (done["status"], done["realization"], done["basis"]) == (
        "approved",
        "merged",
        "merged_pr",
    )
    assert done["decided_by"] == AGENT.sub
    assert done["merge_commit"] == "abc123"
    assert done["pr"].endswith("/pull/7")


async def test_conflicted_merge_stays_proposed_and_sorts_first(reg):
    await approved(reg, branch="feat/old")
    clean = (
        await reg.put_object(claim(title="Other", body="Log edits.", branch="feat/other"), AGENT)
    )["object"]
    conflicted = await reg.put_object(
        claim(title="Admins only", body="From now on only admins may edit Apps."), AGENT
    )
    observe_github("pull_request", merge_event("feat/apps"), reg)
    obj = reg.get(conflicted["object"]["id"])
    assert obj["status"] == "proposed"
    assert obj["realization"] == "merged_with_conflict"
    assert [o["id"] for o in reg.queue()][0] == obj["id"]
    assert clean["id"] in [o["id"] for o in reg.queue()]


async def test_human_proposal_does_not_auto_approve_on_merge(reg):
    obj = (await reg.put_object(claim(body="Cache the list."), HUMAN))["object"]
    observe_github("pull_request", merge_event("feat/apps"), reg)
    after = reg.get(obj["id"])
    assert (after["status"], after["realization"]) == ("proposed", "merged")


async def test_closed_unmerged_pr_abandons(reg):
    obj = (await reg.put_object(claim(body="Cache the list."), AGENT))["object"]
    observe_github("pull_request", merge_event("feat/apps", merged=False), reg)
    assert reg.get(obj["id"])["realization"] == "abandoned"


async def test_failed_judge_blocks_auto_approval(reg, judge):
    await approved(reg, branch="feat/old")
    judge.fail = True
    out = await reg.put_object(claim(body="Cache the list."), AGENT)
    assert out["conflict_check"] == "failed"
    observe_github("pull_request", merge_event("feat/apps"), reg)
    assert reg.get(out["object"]["id"])["realization"] == "merged_with_conflict"


# ── expiry and restore ──────────────────────────────────────────────────


async def test_fourteen_day_expiry_and_restore(reg, clock):
    obj = (await reg.put_object(claim(), AGENT))["object"]
    clock.now += 13 * DAY
    assert reg.expire() == []
    reg.refresh_confidence()
    assert reg.get(obj["id"])["confidence"] == pytest.approx(0.35)
    clock.now += 1 * DAY
    assert reg.expire() == [obj["id"]]
    expired = reg.get(obj["id"])
    assert (expired["status"], expired["status_reason"]) == ("rejected", "expired")
    assert [o["id"] for o in reg.expired()] == [obj["id"]]

    with pytest.raises(Forbidden):
        reg.restore(obj["id"], AGENT)
    restored = reg.restore(obj["id"], HUMAN)
    assert restored["status"] == "proposed"
    assert restored["restored_at"] == clock.now
    clock.now += 13 * DAY
    assert reg.expire() == []
    clock.now += 1 * DAY
    assert reg.expire() == [obj["id"]]


async def test_submitted_proposal_does_not_expire(reg, clock):
    obj = (await reg.put_object(claim(), AGENT))["object"]
    observe_github("pull_request", merge_event("feat/apps", merged=False, action="opened"), reg)
    clock.now += 20 * DAY
    assert reg.expire() == []
    assert reg.get(obj["id"])["status"] == "proposed"


# ── contradiction contract ──────────────────────────────────────────────


async def test_narrows_returns_both_clauses_and_none_returns_nothing(reg):
    existing = await approved(reg, branch="feat/old")
    narrows = await reg.put_object(claim(body="From now on only admins may edit Apps."), AGENT)
    assert narrows["conflicts"] == [
        {
            "existing_id": existing["id"],
            "existing_title": existing["title"],
            "relation": "narrows",
            "new_clause": "only admins may edit",
            "existing_clause": "engineers may edit",
        }
    ]
    rel = reg.relationships(narrows["object"]["id"], direction="out", type="conflicts_with")[0]
    assert (rel["status"], rel["new_clause"], rel["existing_clause"]) == (
        "proposed",
        "only admins may edit",
        "engineers may edit",
    )

    unrelated = await reg.put_object(claim(title="Log", body="Log every edit."), AGENT)
    assert unrelated["conflicts"] == []
    assert unrelated["judgments"][0]["relation"] == "none"
    assert unrelated["conflict_check"] == "done"


async def test_resolve_conflict_keep_and_supersede(reg):
    existing = await approved(reg, branch="feat/old")
    a = (await reg.put_object(claim(body="only admins may edit"), AGENT))["object"]
    with pytest.raises(Forbidden):
        reg.resolve_conflict(a["id"], "supersede", AGENT)
    kept = reg.resolve_conflict(a["id"], "keep", HUMAN)
    assert (kept["status"], kept["status_reason"]) == ("rejected", "conflicts_with_approved")
    assert reg.get(existing["id"])["status"] == "approved"

    b = (await reg.put_object(claim(body="only admins may edit", branch="feat/b"), HUMAN))["object"]
    with pytest.raises(Forbidden, match="author"):
        reg.resolve_conflict(b["id"], "supersede", HUMAN)
    won = reg.resolve_conflict(b["id"], "supersede", OTHER_HUMAN)
    assert (won["status"], won["decided_by"]) == ("approved", OTHER_HUMAN.sub)
    assert reg.get(existing["id"])["status"] == "superseded"
    assert [o["id"] for o in reg.chain(b["id"])] == [existing["id"], b["id"]]


async def test_supersede_before_realization_retires(reg):
    old = (await reg.put_object(claim(body="v1"), AGENT))["object"]
    new = (await reg.put_object(claim(body="v2", branch="feat/v2"), AGENT))["object"]
    out = reg.supersede(new["id"], old["id"], AGENT)
    assert out["old"]["status"] == "retired"
    assert [o["id"] for o in reg.chain(old["id"])] == [old["id"], new["id"]]


async def test_agent_supersede_of_approved_waits_for_approval(reg):
    old = await approved(reg, branch="feat/old")
    new = (await reg.put_object(claim(body="Cache.", branch="feat/new"), AGENT))["object"]
    out = reg.supersede(new["id"], old["id"], AGENT)
    assert out["applied"] is False
    assert reg.get(old["id"])["status"] == "approved"
    assert any(r.startswith("supersedes") for r in reg.unresolved(reg.get(new["id"])))
    reg.approve(new["id"], HUMAN)
    assert reg.get(old["id"])["status"] == "superseded"


# ── work, evidence, context ─────────────────────────────────────────────


async def test_work_context_order_and_view(reg):
    work = await reg.put_work(
        {"title": "Registry", "goal": "One record", "plan": "1. schema", "scope": "org"}, HUMAN
    )
    child = await reg.put_work({"title": "Child", "scope": "org"}, HUMAN)
    dep = await reg.put_work({"title": "Dep", "scope": "org"}, HUMAN)
    req = (
        await reg.put_object(claim(kind="requirement", title="Edits are logged", body="log"), AGENT)
    )["object"]
    dec = (await reg.put_object(claim(title="Use JSON", body="json", branch="feat/json"), AGENT))[
        "object"
    ]
    reg.link_work(work["id"], req["id"], "addresses", HUMAN)
    reg.link_work(work["id"], dec["id"], "records", HUMAN)
    reg.link_work(work["id"], child["id"], "parent_of", HUMAN)
    reg.link_work(dep["id"], work["id"], "blocks", HUMAN)

    with pytest.raises(RegistryError, match="parent"):
        reg.link_work(dep["id"], child["id"], "parent_of", HUMAN)
    with pytest.raises(RegistryError, match="cycle"):
        reg.link_work(work["id"], dep["id"], "blocks", HUMAN)
    with pytest.raises(RegistryError):
        reg.link_work(work["id"], dec["id"], "addresses", HUMAN)

    observe_github("pull_request", merge_event("feat/apps"), reg)
    view = reg.work_view(work["id"])
    assert view["verification"][0]["latest_result"] == "unverified"  # a merge is not evidence
    assert view["evidence"] == []
    assert view["delivery_state"] == "planned"
    assert view["implementation"]["realization_counts"] == {"merged": 1, "stated": 1}

    ev = reg.put_evidence(
        {
            "requirement_ids": [req["id"]],
            "tested_revision": "abc123",
            "method": "pytest",
            "result": "pass",
        },
        AGENT,
    )
    ctx = reg.work_context(work["id"])
    assert list(ctx) == [
        "work",
        "requirements",
        "decisions",
        "children",
        "dependencies",
        "evidence",
        "comments",
    ]
    assert ctx["work"]["plan"] == "1. schema"
    assert [o["id"] for o in ctx["requirements"]] == [req["id"]]
    assert [o["id"] for o in ctx["decisions"]] == [dec["id"]]
    assert [o["id"] for o in ctx["children"]] == [child["id"]]
    assert [o["id"] for o in ctx["dependencies"]["blocked_by"]] == [dep["id"]]
    assert [o["id"] for o in ctx["evidence"]] == [ev["id"]]
    assert reg.work_view(work["id"])["verification"][0]["latest_revision"] == "abc123"


async def test_get_context_ranks_merged_approval_above_stated_proposal(reg):
    merged = (await reg.put_object(claim(title="Merged", body="Cache."), AGENT))["object"]
    observe_github("pull_request", merge_event("feat/apps"), reg)
    stated = (await reg.put_object(claim(title="Stated", body="Log.", branch="feat/2"), HUMAN))[
        "object"
    ]
    ctx = reg.context_for_paths(REPO, ["src/apps/perm.py"])
    assert [o["id"] for o in ctx] == [merged["id"], stated["id"]]
    assert ctx[0]["proposal"] is False and ctx[1]["proposal"] is True
    by_symbol = reg.context_for_paths(REPO, symbols=["can_edit"])
    assert [o["id"] for o in by_symbol] == [merged["id"], stated["id"]]


async def test_search_by_concept_and_symbol_without_prose(reg):
    obj = (
        await reg.put_object(claim(title="Zzz", body="Qqq", concepts=["App permissions"]), AGENT)
    )["object"]
    assert [o["id"] for o in reg.search(concept="app permissions")] == [obj["id"]]
    assert [o["id"] for o in reg.search(repo=REPO, symbol="can_edit")] == [obj["id"]]
    assert reg.search(repo=REPO, symbol="other") == []


async def test_anchor_follows_rename_through_index(reg):
    reg.anchor_lookup = lambda repo, aid: (
        {
            "path": "src/apps/renamed.py",
            "qualified_name": "can_edit",
            "resolution_status": "resolved",
        }
        if aid == "anc_1"
        else None
    )
    obj = await approved(
        reg,
        anchors=[{"path": "src/apps/perm.py", "qualified_name": "can_edit", "anchor_id": "anc_1"}],
    )
    assert [
        o["id"]
        for o in reg.context_for_paths(REPO, ["src/apps/renamed.py"], include_proposals=False)
    ] == [obj["id"]]


# ── learned rules ───────────────────────────────────────────────────────


def test_learned_rule_import_is_read_only_and_idempotent(reg, tmp_path, monkeypatch):
    from mira.index.store import IndexStore

    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path / "indexes"))
    store = IndexStore.open("hickoryai", "hickory-ai", platform="github")
    store.create_learned_rule("Prefer early returns", "style", "src/**", created_by="zane")
    store.create_learned_rule("Pending rule", "style", status="pending")
    before = store._conn.execute("SELECT * FROM learned_rules ORDER BY id").fetchall()
    store.close()

    repos = [("github", "hickoryai", "hickory-ai")]
    assert import_learned_rules(reg, repos)["created"] == 1
    assert import_learned_rules(reg, repos)["unchanged"] == 1

    [rule] = reg._select("source_system = ?", (SOURCE_SYSTEM,))
    assert (rule["kind"], rule["basis"], rule["status"]) == ("rule", "review_learning", "approved")
    assert rule["source_url"].startswith("mira://github/hickoryai/hickory-ai/learned-rules/")
    assert rule["body"] == "Prefer early returns"

    store = IndexStore.open("hickoryai", "hickory-ai", platform="github")
    after = store._conn.execute("SELECT * FROM learned_rules ORDER BY id").fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]
    for row in store.list_learned_rules():
        store.set_learned_rule_active(row.id, False)
    store.close()
    assert import_learned_rules(reg, repos)["retired"] == 1
    assert reg.get(rule["id"])["status"] == "retired"


# ── MCP and JWT ─────────────────────────────────────────────────────────


@pytest.fixture
def mcp_client(reg, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mira.registry import install

    monkeypatch.setenv("REGISTRY_JWT_SECRET", "test-secret-of-adequate-length-123")
    app = FastAPI()
    install(app)
    return TestClient(app)


def token(kind: str, sub: str, model: str = "", exp: float | None = None) -> str:
    claims = {"kind": kind, "sub": sub, "exp": int(exp or time.time() + 600)}
    if model:
        claims["model"] = model
    return jwt.encode(claims, "test-secret-of-adequate-length-123", algorithm="HS256")


def rpc(client, tok: str, method: str, params: dict | None = None, msg_id: int = 1):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}},
        headers={"Authorization": f"Bearer {tok}"},
    )


def test_mcp_requires_a_valid_jwt(mcp_client):
    assert mcp_client.post("/mcp", json={}).status_code == 401
    expired = token("agent", "a", "m", exp=time.time() - 10)
    assert rpc(mcp_client, expired, "ping").status_code == 401
    assert rpc(mcp_client, token("agent", "a"), "ping").status_code == 401  # agent needs model
    assert mcp_client.get("/mcp").status_code == 405


def test_mcp_tools_and_put_decision(mcp_client, reg):
    agent = token("agent", "codex-session-9", "gpt-5.5")
    init = rpc(mcp_client, agent, "initialize", {"protocolVersion": "2025-06-18"}).json()
    assert init["result"]["serverInfo"]["name"] == "kory-source"
    names = {t["name"] for t in rpc(mcp_client, agent, "tools/list").json()["result"]["tools"]}
    assert names == {
        "put_decision",
        "search_decisions",
        "supersede_decision",
        "get_decision_chain",
        "link_anchor",
        "get_context",
        "resolve_conflict",
        "session_conflicts",
        "conflict_prompt_counts",
        "put_work",
        "link_work",
        "put_evidence",
    }
    res = rpc(
        mcp_client,
        agent,
        "tools/call",
        {
            "name": "put_decision",
            "arguments": {**claim(), "decided_by": "someone-else", "confidence": 1},
        },
    ).json()["result"]
    obj = res["structuredContent"]["object"]
    assert res["isError"] is False
    assert (obj["decided_by"], obj["model"], obj["author_kind"]) == (
        "codex-session-9",
        "gpt-5.5",
        "agent",
    )
    assert obj["confidence"] == pytest.approx(0.40)

    refused = rpc(
        mcp_client,
        agent,
        "tools/call",
        {"name": "resolve_conflict", "arguments": {"id": obj["id"], "choice": "supersede"}},
    ).json()["result"]
    assert refused["isError"] is True

    missing = rpc(
        mcp_client,
        agent,
        "tools/call",
        {"name": "put_decision", "arguments": {**claim(), "branch": ""}},
    ).json()["result"]
    assert missing["isError"] is True
    assert "session and branch" in missing["content"][0]["text"]

    ctx = rpc(
        mcp_client,
        agent,
        "tools/call",
        {"name": "get_context", "arguments": {"repo": REPO, "paths": ["src/apps/perm.py"]}},
    ).json()["result"]["structuredContent"]["items"]
    assert ctx[0]["id"] == obj["id"] and ctx[0]["proposal"] is True


def test_mcp_notification_gets_202(mcp_client):
    tok = token("human", "zane")
    res = mcp_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert res.status_code == 202


# ── upstream drift caps ─────────────────────────────────────────────────


def _diff_lines(path: str) -> int | None:
    if shutil.which("git") is None:
        return None
    probe = subprocess.run(
        ["git", "cat-file", "-e", f"{FORK_BASE}^{{commit}}"], cwd=ROOT, capture_output=True
    )
    if probe.returncode != 0:
        return None
    out = subprocess.run(
        ["git", "diff", "--numstat", FORK_BASE, "--", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return int(out[0]) + int(out[1]) if out else 0


# Measured against 567ed7e: engine.py is 45. HAI-12286 set the cap at 47 when
# review_snapshot (28) sat beside the registry block. HAI-12196 moved the
# contradiction check into that same block and the measured total is 45.
# 46 is the next integer, so another edit still fails. server.py is 15.
@pytest.mark.parametrize(
    ("path", "cap"), [("src/mira/core/engine.py", 46), ("src/mira/platforms/server.py", 25)]
)
def test_upstream_edit_stays_under_cap(path, cap):
    changed = _diff_lines(path)
    if changed is None:
        pytest.skip("fork base commit not available in this checkout")
    assert changed < cap
