"""Linear import into the registry (HAI-11606): client, mapping, and resumable walks.

A stateful fake Linear GraphQL server stands in for api.linear.app. It pages,
filters on team and ``updatedAt``, and can fail requests on demand.
"""

from __future__ import annotations

import gzip
import json
import os
import threading
from datetime import UTC, datetime

import httpx
import pytest

from mira.registry import Actor, RegistryError, set_registry
from mira.registry import linear as lin
from mira.registry.db import RegistryDB
from mira.registry.linear import LinearClient, LinearError, LinearImporter
from mira.registry.service import Registry

HUMAN = Actor("human", "zane")
AGENT = Actor("agent", "claude-session-1", "claude-opus-5-5")
T0 = 1_800_000_000.0


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


class FakeLinear:
    """In-memory Linear: teams, issues, comments and documents, cursor paging, fault injection."""

    def __init__(self) -> None:
        self.teams: dict[str, dict] = {}
        self.issues: dict[str, dict] = {}
        self.comments: dict[str, dict] = {}
        self.documents: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.fail_next: list[int] = []  # HTTP statuses to return before serving
        self.crash_after_pages: int | None = None
        self.pages_served = 0

    def issue(self, key: str, *, t: float, **fields) -> dict:
        node = {
            "id": f"uuid-{key}",
            "identifier": key,
            "title": fields.pop("title", key),
            "description": fields.pop("description", ""),
            "url": f"https://linear.app/hickoryai/issue/{key}",
            "updatedAt": iso(t),
            "archivedAt": None,
            "trashed": False,
            "team": {"key": key.split("-")[0]},
            "state": {"type": fields.pop("state", "unstarted")},
            "assignee": None,
            "project": None,
            "parent": None,
            "relations": {"pageInfo": {"hasNextPage": False}, "nodes": []},
        }
        node.update(fields)
        self.issues[node["id"]] = node
        team = node["team"]["key"]
        self.teams.setdefault(
            team,
            {"id": f"team-{team}", "key": team, "name": team, "archivedAt": None, "updatedAt": ""},
        )
        return node

    def comment(self, cid: str, issue_key: str, body: str, *, t: float, **fields) -> dict:
        issue = self.issues[f"uuid-{issue_key}"]
        node = {
            "id": cid,
            "body": body,
            "url": f"{issue['url']}#comment-{cid}",
            "createdAt": iso(t),
            "updatedAt": iso(t),
            "archivedAt": None,
            "user": {"displayName": "Zane", "email": "zane@example.com"},
            "issue": {"id": issue["id"], "team": issue["team"]},
        }
        node.update(fields)
        self.comments[cid] = node
        return node

    def document(self, did: str, issue_key: str | None, *, t: float, **fields) -> dict:
        issue = self.issues[f"uuid-{issue_key}"] if issue_key else None
        node = {
            "id": did,
            "title": fields.pop("title", did),
            "url": f"https://linear.app/hickoryai/document/{did}",
            "updatedAt": iso(t),
            "archivedAt": None,
            "issue": (
                {"id": issue["id"], "identifier": issue["identifier"], "team": issue["team"]}
                if issue
                else None
            ),
            "project": None,
            "initiative": None,
        }
        node.update(fields)
        self.documents[did] = node
        return node

    @staticmethod
    def _match(node: dict, f: dict) -> bool:
        for key, cond in f.items():
            if key == "and" and not all(FakeLinear._match(node, c) for c in cond):
                return False
            if key == "or" and not any(FakeLinear._match(node, c) for c in cond):
                return False
            if key == "team" and node["team"]["key"] != cond["key"]["eq"]:
                return False
            if key == "id" and node["id"] not in cond["in"]:
                return False
            if key == "issue" and (
                node["issue"] is None or not FakeLinear._match(node["issue"], cond)
            ):
                return False
            if key == "updatedAt" and lin.to_epoch(node["updatedAt"]) < lin.to_epoch(cond["gte"]):
                return False
        return True

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(body)
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), headers={"retry-after": "1"})
        if self.crash_after_pages is not None and self.pages_served >= self.crash_after_pages:
            raise httpx.ConnectError("linear went away")
        q, v = body["query"], body["variables"]
        name = next(
            n for n in ("issues", "comments", "teams", "documents") if f"Registry{n.title()}" in q
        )
        store = getattr(self, name)
        nodes = sorted(
            (n for n in store.values() if self._match(n, v["filter"])),
            key=lambda n: (n["updatedAt"], n["id"]),
        )
        after = v.get("after")
        if after is not None and not after.startswith("c"):
            return httpx.Response(200, json={"errors": [{"message": "invalid cursor"}]})
        start = int(after[1:]) if after else 0
        page = nodes[start : start + v["first"]]
        more = start + v["first"] < len(nodes)
        self.pages_served += 1
        return httpx.Response(
            200,
            json={
                "data": {
                    name: {
                        "pageInfo": {
                            "hasNextPage": more,
                            "endCursor": f"c{start + v['first']}" if more else None,
                        },
                        "nodes": page,
                    }
                }
            },
        )


@pytest.fixture
def fake() -> FakeLinear:
    return FakeLinear()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(fake, sleeps) -> LinearClient:
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return LinearClient(
        "lin_sync_test", endpoint="https://linear.test/graphql", http=http, sleep=sleeps.append
    )


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def reg(tmp_path, clock) -> Registry:
    registry = Registry(RegistryDB(path=str(tmp_path / "registry.db")), clock=clock)
    set_registry(registry)
    yield registry
    set_registry(None)


@pytest.fixture(autouse=True)
def small_pages(monkeypatch):
    monkeypatch.setattr(lin, "PAGE_SIZE", 2)


def importer(reg, client, clock) -> LinearImporter:
    return LinearImporter(reg, client, ["HAI"], clock=clock)


def snapshot(reg: Registry) -> dict:
    """Everything the import owns, with generated ids replaced by source ids."""
    by_id = {o["id"]: o["source_id"] for o in reg._select()}
    objects = sorted(
        (
            o["source_id"],
            o["title"],
            o["ticket"],
            o["status"],
            o["delivery_state"],
            o["source_version"],
            o["sync_status"],
        )
        for o in reg._select()
    )
    rels = sorted(
        (by_id[r[0]], by_id[r[1]], r[2])
        for r in reg.db.execute("SELECT source_id, target_id, type FROM registry_relationships")
    )
    comments = sorted(
        reg.db.execute(
            "SELECT source_id, parent_source_id, body, source_version, archived "
            "FROM registry_comments"
        )
    )
    return {"objects": objects, "rels": rels, "comments": comments}


def seed(fake: FakeLinear) -> None:
    fake.issue("HAI-1", t=T0 - 50, title="Epic")
    fake.issue("HAI-2", t=T0 - 40, parent={"id": "uuid-HAI-1"}, state="started")
    fake.issue(
        "HAI-3",
        t=T0 - 30,
        relations={
            "pageInfo": {"hasNextPage": False},
            "nodes": [
                {"type": "blocks", "relatedIssue": {"id": "uuid-HAI-2"}},
                {"type": "similar", "relatedIssue": {"id": "uuid-HAI-1"}},
            ],
        },
    )
    fake.issue("HAI-4", t=T0 - 20, state="completed")
    fake.issue("OPS-1", t=T0 - 10)
    fake.comment("cm-1", "HAI-2", "Plan: do the thing", t=T0 - 35)
    fake.comment("cm-2", "HAI-2", "Binding terms", t=T0 - 25)
    fake.comment("cm-3", "OPS-1", "other team", t=T0 - 5)


# ── client ──────────────────────────────────────────────────────────────


def test_client_refuses_mutations(client, fake):
    with pytest.raises(LinearError, match="read-only"):
        client.query('  mutation { issueUpdate(id: "x") { success } }', {})
    assert fake.requests == []


def test_client_requires_the_sync_key():
    with pytest.raises(LinearError, match="LINEAR_SYNC_API_KEY"):
        LinearClient("")


def test_client_retries_rate_limits_and_server_errors(client, fake, sleeps):
    fake.issue("HAI-1", t=T0)
    fake.fail_next = [429, 502]
    data = client.query(lin.ISSUES_QUERY, {"filter": {}, "after": None, "first": 2})
    assert [n["identifier"] for n in data["issues"]["nodes"]] == ["HAI-1"]
    assert sleeps == [1.0, 1.0]  # Retry-After honoured both times


def test_client_gives_up_after_bounded_retries(client, fake, sleeps):
    fake.fail_next = [503] * lin.MAX_ATTEMPTS
    with pytest.raises(LinearError, match="after 5 attempts: HTTP 503"):
        client.query(lin.ISSUES_QUERY, {"filter": {}, "after": None, "first": 2})
    assert len(fake.requests) == lin.MAX_ATTEMPTS


def test_graphql_errors_are_not_retried(client, fake):
    with pytest.raises(LinearError, match="invalid cursor"):
        client.query(lin.ISSUES_QUERY, {"filter": {}, "after": "bad", "first": 2})
    assert len(fake.requests) == 1


# ── mapping ─────────────────────────────────────────────────────────────


def test_issue_mapping(fake):
    node = fake.issue(
        "HAI-7",
        t=T0,
        description="body",
        state="started",
        assignee={"displayName": "Pat", "email": "pat@example.com"},
        parent={"id": "uuid-HAI-1"},
        relations={
            "pageInfo": {"hasNextPage": False},
            "nodes": [
                {"type": "related", "relatedIssue": {"id": "uuid-HAI-2"}},
                {"type": "duplicate", "relatedIssue": {"id": "uuid-HAI-3"}},
                {"type": "similar", "relatedIssue": {"id": "uuid-HAI-4"}},
            ],
        },
    )
    item = lin.issue_item(node)
    assert item["source_id"] == "uuid-HAI-7"
    assert item["source_version"] == int(T0 * 1000)
    assert item["ticket"] == "HAI-7"
    assert item["owner"] == "pat@example.com"
    assert item["delivery_state"] == "active"
    assert item["status"] == "approved"
    assert item["links"] == [
        {"type": "parent_of", "target_source_id": "uuid-HAI-1", "reverse": True},
        {"type": "relates", "target_source_id": "uuid-HAI-2"},
        {"type": "relates", "target_source_id": "uuid-HAI-3"},
    ]
    archived = lin.issue_item({**node, "archivedAt": iso(T0)})
    assert archived["delivery_state"] == "archived"
    trashed = lin.issue_item({**node, "trashed": True})
    assert (trashed["status"], trashed["status_reason"]) == ("retired", "source_deleted")


@pytest.mark.parametrize(
    ("state", "delivery"),
    [
        ("triage", "planned"),
        ("backlog", "planned"),
        ("unstarted", "planned"),
        ("started", "active"),
        ("completed", "shipped"),
        ("canceled", "canceled"),
        ("duplicate", "canceled"),
    ],
)
def test_every_linear_state_type_maps(fake, state, delivery):
    # The state types WorkflowState.type documents in Linear's schema.
    assert lin.issue_item(fake.issue("HAI-8", t=T0, state=state))["delivery_state"] == delivery


# ── import walks ────────────────────────────────────────────────────────


def test_import_creates_work_links_and_comments(reg, client, fake, clock):
    seed(fake)
    out = importer(reg, client, clock).run(apply=True)
    assert out["issues"]["totals"]["created"] == 4  # OPS-1 is another team
    assert out["comments"]["totals"]["created"] == 2
    assert out["sync_status"] == {"synced": 4}

    ctx = reg.work_context(reg.work_by_ticket("HAI-2")["id"])
    assert ctx["dependencies"]["parent"]["ticket"] == "HAI-1"
    assert [o["ticket"] for o in ctx["dependencies"]["blocked_by"]] == ["HAI-3"]
    assert ctx["work"]["delivery_state"] == "active"
    assert [c["body"] for c in ctx["comments"]] == ["Plan: do the thing", "Binding terms"]
    # "similar" is not a registry relationship.
    assert reg.work_context(reg.work_by_ticket("HAI-3")["id"])["dependencies"]["relates"] == []


def test_walks_ask_linear_only_for_the_team(reg, client, fake, clock):
    seed(fake)
    out = importer(reg, client, clock).run(apply=True)
    assert out["comments"]["totals"]["read"] == 2  # OPS-1's comment is never sent
    assert out["comments"]["totals"]["skipped_other_team_or_not_on_issue"] == 0
    filters = [r["variables"]["filter"] for r in fake.requests if "RegistryComments" in r["query"]]
    assert filters and all(f["and"][0] == {"issue": lin._team_filter(["HAI"])} for f in filters)


def test_default_scope_is_every_team(reg, client, fake, clock):
    seed(fake)
    fake.issue("HAI-6", t=T0 - 15, state="duplicate", archivedAt=iso(T0 - 15))
    out = LinearImporter(reg, client, clock=clock).run(apply=True)
    assert out["issues"]["totals"]["created"] == 6
    assert out["comments"]["totals"]["created"] == 3
    assert reg.work_by_ticket("OPS-1")["delivery_state"] == "planned"
    assert reg.work_by_ticket("HAI-6")["delivery_state"] == "archived"
    walks = [r for r in fake.requests if "RegistryIssues" in r["query"]]
    assert walks and all("team" not in json.dumps(r["variables"]["filter"]) for r in walks)
    assert all("includeArchived: true" in q for q in lin.QUERIES)


def test_cli_defaults_to_every_team(monkeypatch):
    seen: list = []
    monkeypatch.setattr(lin.LinearClient, "from_env", classmethod(lambda cls: None))
    monkeypatch.setattr(lin, "inventory", lambda client, teams: seen.append(teams) or {})
    lin.main(["inventory"])
    lin.main(["inventory", "--team", "HAI"])
    assert seen == [None, ["HAI"]]


def test_several_teams_filter_with_or(reg, client, fake, clock):
    seed(fake)
    out = LinearImporter(reg, client, ["HAI", "OPS"], clock=clock).run(apply=True)
    assert out["issues"]["totals"]["created"] == 5
    assert out["comments"]["totals"]["created"] == 3


async def test_get_context_takes_a_ticket_key(reg, client, fake, clock):
    from mira.registry.mcp import call_tool

    seed(fake)
    importer(reg, client, clock).run(apply=True)
    ctx = await call_tool("get_context", {"ticket": "HAI-2"}, AGENT)
    assert ctx["work"]["ticket"] == "HAI-2"
    assert len(ctx["comments"]) == 2


def test_dry_run_writes_nothing(reg, client, fake, clock):
    seed(fake)
    out = importer(reg, client, clock).run(apply=False)
    assert out["issues"]["totals"]["read"] == 4
    assert reg._select() == []
    assert reg.db.execute("SELECT COUNT(*) FROM registry_sync_state") == [(0,)]


def test_replay_is_a_no_op(reg, client, fake, clock):
    seed(fake)
    importer(reg, client, clock).run(apply=True)
    before = snapshot(reg)
    # Force a full re-read: the same versions must change nothing.
    reg.db.execute("DELETE FROM registry_sync_state")
    out = importer(reg, client, clock).run(apply=True)
    assert set(out["issues"]["totals"]) == {"read", "unchanged"}
    assert set(out["comments"]["totals"]) == {
        "read",
        "unchanged",
        "skipped_other_team_or_not_on_issue",
    }
    assert snapshot(reg) == before


def test_incremental_walk_reads_from_the_watermark(reg, client, fake, clock):
    seed(fake)
    importer(reg, client, clock).run(apply=True)
    assert importer(reg, client, clock).state("issues")["watermark"] == T0
    clock.now = T0 + 3600
    fake.issue("HAI-4", t=T0 + 1800, title="Renamed", state="completed")
    fake.requests.clear()
    out = importer(reg, client, clock).run(apply=True)
    # Issues inside the overlap are read again and are no-ops.
    assert out["issues"]["totals"] == {"read": 4, "updated": 1, "unchanged": 3}
    issues_filter = fake.requests[0]["variables"]["filter"]["and"][1]
    assert lin.to_epoch(issues_filter["updatedAt"]["gte"]) == T0 - lin.WATERMARK_OVERLAP
    assert reg.work_by_ticket("HAI-4")["title"] == "Renamed"


def test_restart_after_a_crash_resumes_and_matches_a_clean_run(tmp_path, client, fake, clock):
    seed(fake)
    for i in range(5, 12):
        fake.issue(f"HAI-{i}", t=T0 - 15 + i, parent={"id": "uuid-HAI-1"})

    clean = Registry(RegistryDB(path=str(tmp_path / "clean.db")), clock=clock)
    importer(clean, client, clock).run(apply=True)

    crashy = Registry(RegistryDB(path=str(tmp_path / "crashy.db")), clock=clock)
    fake.crash_after_pages = fake.pages_served + 2
    with pytest.raises(LinearError):
        importer(crashy, client, clock).run(apply=True)
    st = importer(crashy, client, clock).state("issues")
    assert st["after_cursor"] == "c4"  # two pages imported and checkpointed
    assert st["watermark"] == 0.0  # the walk did not complete

    fake.crash_after_pages = None
    fake.requests.clear()
    importer(crashy, client, clock).run(apply=True)
    assert fake.requests[0]["variables"]["after"] == "c4"
    assert snapshot(crashy) == snapshot(clean)


def test_rejected_cursor_restarts_the_walk(reg, client, fake, clock):
    seed(fake)
    imp = importer(reg, client, clock)
    imp._save(
        "issues",
        {"watermark": 0.0, "walk_since": 0.0, "walk_started_at": T0, "after_cursor": "expired"},
    )
    out = imp.run(apply=True)
    assert out["issues"]["totals"]["walk_restarted"] == 1
    assert len(reg.works()) == 4


def test_retries_running_out_on_a_resumed_walk_keep_the_cursor(reg, client, fake, clock):
    seed(fake)
    imp = importer(reg, client, clock)
    imp._save(
        "issues",
        {"watermark": 0.0, "walk_since": 0.0, "walk_started_at": T0, "after_cursor": "c2"},
    )
    fake.fail_next = [503] * lin.MAX_ATTEMPTS
    fake.requests.clear()
    with pytest.raises(LinearError, match="after 5 attempts"):
        imp.run(apply=True)
    # Not a rejection, so no restart from the top: every attempt resumed at c2.
    assert {r["variables"]["after"] for r in fake.requests} == {"c2"}
    assert imp.state("issues")["after_cursor"] == "c2"
    assert reg.works() == []


def test_crash_between_object_and_links_is_retried(reg, client, fake, clock, monkeypatch):
    seed(fake)
    real = Registry._import_links

    def boom(self, *a, **k):
        raise RuntimeError("process killed")

    monkeypatch.setattr(Registry, "_import_links", boom)
    with pytest.raises(RuntimeError):
        importer(reg, client, clock).run(apply=True)
    child = reg.work_by_ticket("HAI-2")
    # Written without its links, so it is not marked synced.
    assert child["sync_status"] == "pending_links"

    monkeypatch.setattr(Registry, "_import_links", real)
    importer(reg, client, clock).run(apply=True)
    ctx = reg.work_context(child["id"])
    assert ctx["dependencies"]["parent"]["ticket"] == "HAI-1"
    assert reg.sync_status("linear") == {"synced": 4}


def test_child_read_before_its_parent_gets_its_parent_link(reg, client, fake, clock):
    # The epic was edited after its child, so the child is on an earlier page.
    fake.issue("HAI-2", t=T0 - 5000, parent={"id": "uuid-HAI-1"})
    fake.issue("HAI-9", t=T0 - 4500)
    fake.issue("HAI-1", t=T0 - 4000, title="Epic")
    out = importer(reg, client, clock).run(apply=True)
    child = reg.work_by_ticket("HAI-2")
    assert child["sync_status"] == "synced"
    assert reg.work_context(child["id"])["dependencies"]["parent"]["ticket"] == "HAI-1"
    assert out["issues"]["retried"] == {"read": 1, "resolved": 1}


def test_concurrent_insert_of_the_same_ticket_keeps_one_row(reg, monkeypatch):
    item = {"source_id": "uuid-HAI-1", "source_version": 5, "kind": "work", "title": "A"}
    reg.import_items("linear", [item], HUMAN)
    real = Registry.by_source
    calls = {"n": 0}

    def racing(self, system, source_id):
        # The first lookup misses, as if another importer inserted just after it.
        calls["n"] += 1
        return None if calls["n"] == 1 else real(self, system, source_id)

    monkeypatch.setattr(Registry, "by_source", racing)
    [res] = reg.import_items("linear", [item], HUMAN)
    assert res["outcome"] == "unchanged"
    assert len(reg.works()) == 1


def test_comment_insert_lost_to_a_concurrent_importer_reports_the_winner(reg, monkeypatch):
    comment = {
        "source_id": "cm-1",
        "parent_source_id": "uuid-HAI-1",
        "source_version": 5,
        "body": "A",
    }
    reg.import_comments("linear", [comment])
    real = reg.db.execute
    calls = {"n": 0}

    def racing(sql, params=()):
        # The first lookup misses, as if another importer inserted just after it.
        if sql.startswith("SELECT source_version FROM registry_comments"):
            calls["n"] += 1
            if calls["n"] == 1:
                return []
        return real(sql, params)

    monkeypatch.setattr(reg.db, "execute", racing)
    [res] = reg.import_comments("linear", [comment])
    assert res["outcome"] == "unchanged"
    assert reg.db.execute("SELECT COUNT(*) FROM registry_comments") == [(1,)]


def test_duplicate_deliveries_of_an_issue_in_one_batch(reg):
    item = {"source_id": "uuid-HAI-1", "source_version": 5, "kind": "work", "title": "A"}
    out = reg.import_items("linear", [item, dict(item), {**item, "source_version": 4}], HUMAN)
    assert [r["outcome"] for r in out] == ["created", "unchanged", "stale"]
    assert len(reg.works()) == 1


def test_archive_and_trash_round_trip(reg, client, fake, clock):
    seed(fake)
    importer(reg, client, clock).run(apply=True)
    imp = importer(reg, client, clock)

    clock.now = T0 + 100
    fake.issue("HAI-4", t=T0 + 90, state="completed", archivedAt=iso(T0 + 90))
    imp.run(apply=True)
    assert reg.work_by_ticket("HAI-4")["delivery_state"] == "archived"

    clock.now = T0 + 200
    fake.issue("HAI-4", t=T0 + 190, state="completed")
    imp.run(apply=True)
    assert reg.work_by_ticket("HAI-4")["delivery_state"] == "shipped"

    clock.now = T0 + 300
    fake.issue("HAI-4", t=T0 + 290, state="completed", trashed=True)
    imp.run(apply=True)
    work = reg.work_by_ticket("HAI-4")
    assert (work["status"], work["status_reason"]) == ("retired", "source_deleted")

    clock.now = T0 + 400
    fake.issue("HAI-4", t=T0 + 390, state="completed")
    imp.run(apply=True)
    work = reg.work_by_ticket("HAI-4")
    assert (work["status"], work["status_reason"]) == ("approved", "")


def test_a_person_retiring_work_is_not_undone_by_import(reg):
    item = {"source_id": "uuid-HAI-1", "source_version": 1, "kind": "work", "title": "A"}
    [res] = reg.import_items("linear", [item], HUMAN)
    reg.retire(res["object_id"], HUMAN)
    reg.import_items("linear", [{**item, "source_version": 2, "status": "approved"}], HUMAN)
    assert reg.get(res["object_id"])["status"] == "retired"


def test_reparenting_moves_the_parent_link(reg, client, fake, clock):
    seed(fake)
    importer(reg, client, clock).run(apply=True)
    clock.now = T0 + 100
    fake.issue("HAI-2", t=T0 + 90, parent={"id": "uuid-HAI-4"}, state="started")
    importer(reg, client, clock).run(apply=True)
    ctx = reg.work_context(reg.work_by_ticket("HAI-2")["id"])
    assert ctx["dependencies"]["parent"]["ticket"] == "HAI-4"
    assert reg.work_context(reg.work_by_ticket("HAI-1")["id"])["children"] == []


def test_comment_versions_and_archive(reg, client, fake, clock):
    seed(fake)
    importer(reg, client, clock).run(apply=True)
    clock.now = T0 + 100
    fake.comment("cm-1", "HAI-2", "Plan v2", t=T0 - 35, updatedAt=iso(T0 + 50))
    fake.comment(
        "cm-2", "HAI-2", "Binding terms", t=T0 - 25, updatedAt=iso(T0 + 60), archivedAt=iso(T0 + 60)
    )
    importer(reg, client, clock).run(apply=True)
    ctx = reg.work_context(reg.work_by_ticket("HAI-2")["id"])
    assert [c["body"] for c in ctx["comments"]] == ["Plan v2"]
    stale = reg.import_comments(
        "linear",
        [
            {
                "source_id": "cm-1",
                "parent_source_id": "uuid-HAI-2",
                "source_version": 1,
                "body": "old",
            }
        ],
    )
    assert stale[0]["outcome"] == "stale"


def test_a_comment_before_its_issue_attaches_later(reg):
    reg.import_comments(
        "linear",
        [
            {
                "source_id": "cm-9",
                "parent_source_id": "uuid-HAI-9",
                "source_version": 1,
                "body": "early",
            }
        ],
    )
    reg.import_items(
        "linear",
        [
            {
                "source_id": "uuid-HAI-9",
                "source_version": 1,
                "kind": "work",
                "title": "Late",
                "ticket": "HAI-9",
            }
        ],
        HUMAN,
    )
    ctx = reg.work_context(reg.work_by_ticket("HAI-9")["id"])
    assert [c["body"] for c in ctx["comments"]] == ["early"]


# ── Linear stays authoritative ──────────────────────────────────────────


async def test_registry_edits_of_linear_fields_are_refused(reg, client, fake, clock):
    seed(fake)
    importer(reg, client, clock).run(apply=True)
    work = reg.work_by_ticket("HAI-2")
    with pytest.raises(RegistryError, match="Linear is authoritative for title"):
        reg.update_work(work["id"], {"title": "Agent rename"}, AGENT)
    with pytest.raises(RegistryError, match="delivery_state"):
        reg.update_work(work["id"], {"delivery_state": "shipped"}, HUMAN)
    # Registry-only fields, and an unchanged mirrored value, are allowed.
    updated = reg.update_work(work["id"], {"plan": "1. import", "title": work["title"]}, AGENT)
    assert updated["plan"] == "1. import"
    # ...and survive the next import of a newer Linear version.
    clock.now = T0 + 100
    fake.issue("HAI-2", t=T0 + 90, parent={"id": "uuid-HAI-1"}, state="started", title="New")
    importer(reg, client, clock).run(apply=True)
    assert reg.get(work["id"])["plan"] == "1. import"
    assert reg.get(work["id"])["title"] == "New"


async def test_links_between_linear_tickets_are_refused(reg, client, fake, clock):
    seed(fake)
    importer(reg, client, clock).run(apply=True)
    a, b = reg.work_by_ticket("HAI-4"), reg.work_by_ticket("HAI-1")
    with pytest.raises(RegistryError, match="link them in Linear"):
        reg.link_work(a["id"], b["id"], "blocks", AGENT)
    out = await reg.put_object(
        {
            "kind": "decision",
            "title": "Use the registry",
            "body": "Agents read it.",
            "repo": "hickoryai/hickory-ai",
            "session": "s",
            "branch": "b",
        },
        AGENT,
    )
    # A registry decision recorded against a Linear ticket has no Linear twin.
    reg.link_work(a["id"], out["object"]["id"], "records", AGENT)
    assert [d["id"] for d in reg.work_context(a["id"])["decisions"]] == [out["object"]["id"]]


# ── inventory ───────────────────────────────────────────────────────────


def seed_inventory(fake: FakeLinear) -> None:
    seed(fake)
    fake.issue("HAI-5", t=T0, project={"id": "proj-1"}, archivedAt=iso(T0), trashed=True)
    fake.comments["cm-doc"] = {**fake.comments["cm-1"], "id": "cm-doc", "issue": None}
    fake.document("doc-1", "HAI-2", t=T0 - 30, title="HAI-2 design")
    fake.document("doc-2", "OPS-1", t=T0 - 20, archivedAt=iso(T0 - 20))
    fake.document("doc-3", None, t=T0 - 10, project={"id": "proj-1"})


def test_inventory_counts_the_whole_workspace(client, fake):
    seed_inventory(fake)
    out = lin.inventory(client)
    assert [t["key"] for t in out["teams"]] == ["HAI", "OPS"]
    assert out["issues"]["total"] == 6
    assert (out["issues"]["team:HAI"], out["issues"]["team:OPS"]) == (5, 1)
    assert (out["issues"]["archived"], out["issues"]["trashed"]) == (1, 1)
    assert out["comments"] == {"on_issues": 3, "archived": 0, "not_on_issue": 1}
    assert out["documents"] == {
        "total": 3,
        "archived": 1,
        "on_issue": 2,
        "on_project": 1,
        "on_initiative": 0,
    }
    assert out["documents_linked_to_issues"] == ["HAI-2: HAI-2 design", "OPS-1: doc-2"]
    assert out["projects_referenced"] == 1
    joined = "\n".join(out["exceptions"])
    assert "projects: 1 referenced" in joined
    assert "of type 'similar'" in joined
    assert "documents: 3 (2 linked to an issue)" in joined
    assert "comments: 1 on documents, projects or updates" in joined
    assert "team filter" not in joined


def test_inventory_narrowed_to_a_team(client, fake):
    seed_inventory(fake)
    out = lin.inventory(client, ["HAI"])
    assert [t["key"] for t in out["teams"]] == ["HAI"]
    assert out["issues"]["total"] == 5
    assert out["comments"] == {"on_issues": 2, "archived": 0}
    assert out["documents"]["total"] == 1
    assert out["documents_linked_to_issues"] == ["HAI-2: HAI-2 design"]
    assert "team filter: documents and comments" in "\n".join(out["exceptions"])


# ── schema ──────────────────────────────────────────────────────────────
# tests/fixtures/linear_schema.graphql.gz is Linear's published SDL
# (github.com/linear/linear packages/sdk/src/schema.graphql at 3ce13b6, fetched
# 2026-11-02). It checks query shape and filter inputs offline; it is not a live run.


@pytest.fixture(scope="module")
def linear_schema():
    graphql = pytest.importorskip("graphql")
    path = os.path.join(os.path.dirname(__file__), "fixtures", "linear_schema.graphql.gz")
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return graphql.build_schema(fh.read())


def check_variables(schema, document: str, variables: dict) -> list[str]:
    from graphql import parse
    from graphql.execution.values import get_variable_values

    op = parse(document).definitions[0]
    coerced = get_variable_values(schema, op.variable_definitions, variables)
    return [e.message for e in coerced] if isinstance(coerced, list) else []


@pytest.mark.parametrize("document", lin.QUERIES)
def test_queries_validate_against_linear_schema(linear_schema, document):
    from graphql import parse, validate

    assert [e.message for e in validate(linear_schema, parse(document))] == []


def test_sent_filters_are_valid_linear_inputs(linear_schema, reg, client, fake, clock):
    seed_inventory(fake)
    reg.import_items(
        "linear",
        [
            {
                "source_id": "uuid-HAI-9",
                "source_version": 1,
                "kind": "work",
                "title": "blocked by a ticket that never arrives",
                "links": [{"type": "blocks", "target_source_id": "uuid-NOPE-1"}],
            }
        ],
        HUMAN,
    )
    lin.inventory(client)
    lin.inventory(client, ["HAI", "OPS"])
    LinearImporter(reg, client, clock=clock).run(apply=True)
    LinearImporter(reg, client, ["HAI"], clock=clock).run(apply=True)
    clock.now += 3600
    LinearImporter(reg, client, ["HAI"], clock=clock).run(apply=True)
    sent = {json.dumps(r, sort_keys=True): r for r in fake.requests}.values()
    assert {
        next(n for n in ("Issues", "Comments", "Teams", "Documents") if n in r["query"])
        for r in sent
    } == {"Issues", "Comments", "Teams", "Documents"}
    assert any('"in"' in json.dumps(r["variables"]) for r in sent)  # the pending re-read
    for r in sent:
        assert check_variables(linear_schema, r["query"], r["variables"]) == [], r["variables"]


def test_schema_check_catches_a_bad_filter(linear_schema):
    bad = {"filter": {"issue": {"team": {"key": "HAI"}}}, "after": None, "first": 2}
    assert check_variables(linear_schema, lin.COMMENTS_QUERY, bad)
    with pytest.raises(AssertionError):
        test_queries_validate_against_linear_schema(
            linear_schema, lin.COMMENTS_QUERY.replace("archivedAt", "archivedOn")
        )


# ── transactions ────────────────────────────────────────────────────────


def test_transaction_rolls_back_on_error(reg):
    with pytest.raises(RuntimeError), reg.db.transaction():
        reg.import_items(
            "linear", [{"source_id": "uuid-HAI-1", "source_version": 1, "kind": "work"}], HUMAN
        )
        raise RuntimeError("abort")
    assert reg.works() == []


# ── Postgres (opt-in) ───────────────────────────────────────────────────
# Set REGISTRY_TEST_POSTGRES_URL to a throwaway database to run these. They
# create the registry tables there and drop them afterwards.

PG_URL = os.environ.get("REGISTRY_TEST_POSTGRES_URL", "")
pg_only = pytest.mark.skipif(not PG_URL, reason="REGISTRY_TEST_POSTGRES_URL not set")


@pytest.fixture
def pg_regs(clock):
    regs = [Registry(RegistryDB(url=PG_URL), clock=clock) for _ in range(2)]
    yield regs
    for table in (
        "registry_objects",
        "registry_relationships",
        "registry_anchors",
        "registry_conflict_answers",
        "registry_conflict_prompts",
        "registry_comments",
        "registry_sync_state",
    ):
        regs[0].db.execute(f"DROP TABLE IF EXISTS {table}")
    for r in regs:
        r.db.close()


@pg_only
def test_postgres_concurrent_importers_keep_one_row(pg_regs):
    item = {"source_id": "uuid-HAI-1", "source_version": 5, "kind": "work", "title": "A"}
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def run(reg: Registry) -> None:
        barrier.wait()
        outcomes.extend(r["outcome"] for r in reg.import_items("linear", [item], HUMAN))

    threads = [threading.Thread(target=run, args=(r,)) for r in pg_regs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["created", "unchanged"]
    assert len(pg_regs[0].works()) == 1


@pg_only
def test_postgres_transaction_rolls_back(pg_regs):
    reg = pg_regs[0]
    with pytest.raises(RuntimeError), reg.db.transaction():
        reg.import_items(
            "linear",
            [{"source_id": "uuid-HAI-2", "source_version": 1, "kind": "work", "title": "B"}],
            HUMAN,
        )
        raise RuntimeError("abort")
    assert reg.by_source("linear", "uuid-HAI-2") is None
