"""Contradiction gate (HAI-12196).

The judge is faked at the seat-provider boundary: ``llm.complete``, not a
hand-rolled verdict. A review-time hit does not write a registry object.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mira.config import MiraConfig
from mira.core.engine import ReviewEngine
from mira.models import PRInfo, Severity
from mira.registry import set_registry
from mira.registry.conflicts import seat_judge
from mira.registry.db import RegistryDB
from mira.registry.model import Actor
from mira.registry.realization import observe_github
from mira.registry.service import Registry

REPO = "hickoryai/hickory-ai"
PATH = "src/apps/perm.py"
SYMBOL = "can_edit"
APPROVED = (
    "Engineers hold full collaborator rights on every Interactive App, "
    "including Apps made from an Artifact."
)
NARROW = "The maintain capability does not grant engineers edit on Artifact-made Apps."
NEW_CLAUSE = "does not grant engineers edit on Artifact-made Apps"
OLD_CLAUSE = "engineers hold full collaborator rights on every Interactive App"
OPERATOR = Actor("human", "operator")
SECRET = "test-secret-of-adequate-length-123"

DIFF = """diff --git a/src/apps/perm.py b/src/apps/perm.py
index 1111111..2222222 100644
--- a/src/apps/perm.py
+++ b/src/apps/perm.py
@@ -1,2 +1,5 @@
 def can_edit(user, app):
-    return user.is_engineer
+    # the maintain capability does not grant engineers edit on Artifact-made Apps
+    if app.made_from_artifact:
+        return False
+    return user.is_engineer
"""


class SeatLLM:
    """The seat provider. ``complete`` is the boundary the registry judge calls."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]], json_mode: bool = True) -> str:
        self.calls.append(messages)
        user = messages[-1]["content"]
        if NARROW.lower() in user.lower() and "full collaborator rights" in user.lower():
            return json.dumps(
                {
                    "relation": "narrows",
                    "new_clause": NEW_CLAUSE,
                    "existing_clause": OLD_CLAUSE,
                }
            )
        return json.dumps({"relation": "none", "new_clause": "", "existing_clause": ""})


def _agent(name: str) -> Actor:
    return Actor("agent", name, "claude-opus")


def _claim(session: str, body: str, *, branch: str = "feat/apps") -> dict:
    return {
        "kind": "decision",
        "title": "App edit rights",
        "body": body,
        "repo": REPO,
        "session": session,
        "branch": branch,
        "anchors": [{"path": PATH, "qualified_name": SYMBOL, "anchor_id": "anc_app_edit"}],
    }


@pytest.fixture
def seat() -> SeatLLM:
    return SeatLLM()


@pytest.fixture
def gate(tmp_path, seat, monkeypatch):
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path / "indexes"))
    monkeypatch.setenv("REGISTRY_JWT_SECRET", SECRET)
    registry = Registry(RegistryDB(path=str(tmp_path / "registry.db")), judge=seat_judge(seat))
    set_registry(registry)
    yield registry
    set_registry(None)


async def _approve(reg: Registry) -> dict:
    out = await reg.put_object(_claim("human-session", APPROVED, branch="feat/policy"), OPERATOR)
    return reg.approve(out["object"]["id"], Actor("human", "pat"))


def _merge(branch: str = "feat/apps") -> dict:
    return {
        "action": "closed",
        "repository": {"full_name": REPO},
        "pull_request": {
            "merged": True,
            "html_url": "https://github.com/hickoryai/hickory-ai/pull/7",
            "merge_commit_sha": "abc123",
            "head": {"ref": branch, "repo": {"full_name": REPO}},
        },
    }


def _token(kind: str, sub: str, model: str = "") -> str:
    claims: dict = {"kind": kind, "sub": sub, "exp": 2_000_000_000}
    if model:
        claims["model"] = model
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _rpc(client: TestClient, tok: str, name: str, arguments: dict) -> dict:
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={"Authorization": f"Bearer {tok}"},
    ).json()["result"]


def _quiet_engine() -> ReviewEngine:
    config = MiraConfig()
    config.review.walkthrough = False
    config.review.self_critique = False
    config.review.security_pass = False
    config.review.osv_scan = False
    config.review.secrets_scan = False
    config.review.agentic_tools = False
    config.review.code_context = False
    config.review.dependency_overlap = False
    config.review.include_summary = False
    config.review.blast_radius = False
    llm = MagicMock()
    llm.review = AsyncMock(return_value='{"comments":[],"key_issues":[],"summary":""}')
    llm.count_tokens = MagicMock(return_value=10)
    llm.usage = {}
    return ReviewEngine(config=config, llm=llm)


def _pr(description: str = NARROW) -> PRInfo:
    return PRInfo(
        title="Narrow artifact app edits",
        description=description,
        base_branch="main",
        head_branch="feat/apps",
        url="https://github.com/hickoryai/hickory-ai/pull/7",
        number=7,
        owner="hickoryai",
        repo="hickory-ai",
    )


async def test_replay_quotes_clauses_and_answers_do_not_cross_sessions(
    gate: Registry, seat: SeatLLM
):
    approved = await _approve(gate)
    before = len(seat.calls)
    out = await gate.put_object(_claim("sess-a", NARROW), _agent("agent-a"))
    assert len(seat.calls) == before + 1
    assert "Inferred narrowing counts" in seat.calls[-1][0]["content"]
    assert out["conflicts"] == [
        {
            "existing_id": approved["id"],
            "existing_title": approved["title"],
            "relation": "narrows",
            "new_clause": NEW_CLAUSE,
            "existing_clause": OLD_CLAUSE,
        }
    ]
    held = gate.session_conflicts("sess-a")
    assert held["unresolved"] is True
    assert held["conflicts"][0]["existing_id"] == approved["id"]
    assert held["conflicts"][0]["new_clause"] == NEW_CLAUSE

    unrelated = await gate.put_object(
        _claim("sess-log", "Log every edit of an App.", branch="feat/log"), _agent("agent-log")
    )
    assert unrelated["conflicts"] == []
    assert gate.session_conflicts("sess-log")["unresolved"] is False

    kept = gate.resolve_conflict(out["object"]["id"], "keep", OPERATOR)
    assert (kept["status"], kept["status_reason"]) == ("rejected", "conflicts_with_approved")
    assert gate.get(approved["id"])["status"] == "approved"
    assert gate.session_conflicts("sess-a")["unresolved"] is False

    second = await gate.put_object(_claim("sess-b", NARROW, branch="feat/b"), _agent("agent-b"))
    assert gate.session_conflicts("sess-b")["unresolved"] is True
    assert gate.get(second["object"]["id"])["status"] == "proposed"
    assert gate.session_conflicts("sess-a")["unresolved"] is False

    deferred = await gate.put_object(_claim("sess-c", NARROW, branch="feat/c"), _agent("agent-c"))
    gate.resolve_conflict(deferred["object"]["id"], "defer", OPERATOR)
    assert gate.get(deferred["object"]["id"])["status"] == "proposed"
    deferred_hold = gate.session_conflicts("sess-c")
    assert deferred_hold["unresolved"] is True
    assert deferred_hold["conflicts"][0]["answer"] == "defer"
    assert gate.session_conflicts("sess-b")["unresolved"] is True
    view = gate.conflict_views(gate.get(deferred["object"]["id"]))[0]
    assert (view["answer"], view["answered_by"], view["session"]) == ("defer", "operator", "sess-c")

    won = gate.resolve_conflict(second["object"]["id"], "supersede", OPERATOR)
    assert (won["status"], won["decided_by"]) == ("approved", "operator")
    assert gate.get(approved["id"])["status"] == "superseded"
    chain = gate.chain(won["id"])
    assert [node["id"] for node in chain] == [approved["id"], won["id"]]
    assert chain[-1]["conflicts"][0]["answer"] == "supersede"
    assert chain[-1]["conflicts"][0]["session"] == "sess-b"

    counts = gate.conflict_prompt_counts()
    assert counts["prompts"] >= 4
    assert counts["answers"] == {"keep": 1, "supersede": 1, "defer": 1}
    assert {row["session"] for row in counts["by_session"]} >= {"sess-a", "sess-b", "sess-c"}


async def test_unanswered_merge_stays_proposed_and_review_names_both_ids(gate: Registry):
    approved = await _approve(gate)
    out = await gate.put_object(_claim("sess-a", NARROW), _agent("agent-a"))
    proposal_id = out["object"]["id"]
    observe_github("pull_request", _merge(), gate)
    stored = gate.get(proposal_id)
    assert stored["status"] == "proposed"
    assert stored["realization"] == "merged_with_conflict"
    assert [row["id"] for row in gate.queue()][0] == proposal_id

    before = len(gate._select("1=1"))
    hits = await gate.review_findings(
        repo=REPO,
        hunks={PATH: {"text": DIFF, "line": 2}},
        pr_title="Narrow artifact app edits",
        pr_body=NARROW,
        branch="feat/apps",
        pr_number=7,
    )
    assert len(gate._select("1=1")) == before
    assert len(hits) == 1
    assert hits[0]["approved_id"] == approved["id"]
    assert hits[0]["proposal_id"] == proposal_id
    assert hits[0]["new_clause"] == NEW_CLAUSE
    assert hits[0]["existing_clause"] == OLD_CLAUSE

    result = await _quiet_engine().review_snapshot(_pr(), DIFF)
    blockers = [c for c in result.comments if c.source_pass == "registry"]
    assert len(blockers) == 1
    comment = blockers[0]
    assert comment.severity == Severity.BLOCKER
    assert approved["id"] in comment.body
    assert proposal_id in comment.body
    assert NEW_CLAUSE in comment.body
    assert OLD_CLAUSE in comment.body
    assert len(gate._select("1=1")) == before


async def test_review_without_put_decision_quotes_the_pr_clause(gate: Registry, seat: SeatLLM):
    approved = await _approve(gate)
    before = len(gate._select("1=1"))
    result = await _quiet_engine().review_snapshot(_pr(), DIFF)
    assert len(gate._select("1=1")) == before
    [comment] = [c for c in result.comments if c.source_pass == "registry"]
    assert comment.severity == Severity.BLOCKER
    assert approved["id"] in comment.body
    assert NEW_CLAUSE in comment.body
    assert OLD_CLAUSE in comment.body
    assert "Proposal (" not in comment.body

    calls = len(seat.calls)
    quiet = await gate.review_findings(
        repo=REPO,
        hunks={PATH: {"text": "def other_func():\n    return 1\n", "line": 20}},
        pr_title="Touch a different function",
        pr_body="No change to who can edit.",
        branch="feat/other",
        pr_number=8,
    )
    assert quiet == []
    assert len(seat.calls) == calls


async def test_defer_keeps_push_blocked_until_keep_or_supersede(gate: Registry):
    await _approve(gate)
    first = await gate.put_object(_claim("sess-a", NARROW), _agent("agent-a"))
    other = await gate.put_object(_claim("sess-b", NARROW, branch="feat/b"), _agent("agent-b"))
    gate.resolve_conflict(first["object"]["id"], "defer", OPERATOR)

    held = gate.session_conflicts("sess-a")
    assert held["unresolved"] is True
    assert held["conflicts"][0]["answer"] == "defer"
    assert held["conflicts"][0]["object_id"] == first["object"]["id"]
    assert gate.session_conflicts("sess-b")["unresolved"] is True
    assert gate.session_conflicts("sess-b")["conflicts"][0]["answer"] is None

    gate.resolve_conflict(first["object"]["id"], "keep", OPERATOR)
    assert gate.session_conflicts("sess-a")["unresolved"] is False
    assert gate.get(other["object"]["id"])["status"] == "proposed"
    assert gate.session_conflicts("sess-b")["unresolved"] is True


async def test_deferred_merge_does_not_auto_approve(gate: Registry):
    await _approve(gate)
    out = await gate.put_object(_claim("sess-a", NARROW), _agent("agent-a"))
    gate.resolve_conflict(out["object"]["id"], "defer", OPERATOR)
    observe_github("pull_request", _merge(), gate)
    stored = gate.get(out["object"]["id"])
    assert stored["status"] == "proposed"
    assert stored["realization"] == "merged_with_conflict"
    held = gate.session_conflicts("sess-a")
    assert held["unresolved"] is True
    assert held["conflicts"][0]["answer"] == "defer"


async def test_http_and_mcp_session_query(gate: Registry):
    from mira.registry import install

    app = FastAPI()
    install(app)
    client = TestClient(app)

    approved_task = await _approve(gate)
    assert approved_task["status"] == "approved"
    await gate.put_object(_claim("sess-a", NARROW), _agent("agent-a"))

    http = client.get("/api/registry/session-conflicts", params={"session": "sess-a"})
    assert http.status_code == 200
    assert http.json()["unresolved"] is True
    assert http.json()["conflicts"][0]["new_clause"] == NEW_CLAUSE

    other = client.get("/api/registry/session-conflicts", params={"session": "sess-b"})
    assert other.json()["unresolved"] is False

    counts = client.get("/api/registry/conflict-prompts")
    assert counts.status_code == 200
    body = counts.json()
    assert body["prompts"] >= 1
    assert set(body["answers"]) == {"keep", "supersede", "defer"}

    agent = _token("agent", "agent-a", "claude-opus")
    mcp = _rpc(client, agent, "session_conflicts", {"session": "sess-a"})
    assert mcp["isError"] is False
    assert mcp["structuredContent"]["unresolved"] is True
    prompts = _rpc(client, agent, "conflict_prompt_counts", {})
    assert prompts["structuredContent"]["prompts"] == body["prompts"]
    refused = _rpc(client, agent, "resolve_conflict", {"id": "reg_missing", "choice": "defer"})
    assert refused["isError"] is True


async def test_posting_review_keeps_the_blocker_and_posts_the_advisory(gate: Registry):
    """HAI-12775: an uncited decision is a general comment beside the inline blocker."""
    from mira.registry.uncited import COMMENT_MARKER

    class Provider:
        def __init__(self) -> None:
            self.posted: list[str] = []

        async def find_bot_comment(self, pr_info, marker):  # type: ignore[no-untyped-def]
            return None

        async def post_comment(self, pr_info, body):  # type: ignore[no-untyped-def]
            self.posted.append(body)

    approved = await _approve(gate)
    before = len(gate._select("1=1"))
    engine = _quiet_engine()
    engine.provider = Provider()  # type: ignore[assignment]
    engine.dry_run = False
    body = NARROW + "\n\nDecisions I made:\n- Artifact Apps get a separate edit check\n"
    result = await engine.review_snapshot(_pr(body), DIFF)
    [blocker] = [c for c in result.comments if c.source_pass == "registry"]
    assert blocker.severity == Severity.BLOCKER
    assert approved["id"] in blocker.body
    assert all(c.path for c in result.comments)
    [posted] = engine.provider.posted  # type: ignore[attr-defined]
    assert posted.startswith(COMMENT_MARKER)
    assert '"Artifact Apps get a separate edit check"' in posted
    assert len(gate._select("1=1")) == before
