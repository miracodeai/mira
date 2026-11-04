"""Review-without-post API (HAI-12286).

These tests boot the real app (``create_app`` + lifespan), the real job
executor, and the real review engine. GitHub is an ``httpx.MockTransport``
that fails any non-GET request. Model calls are answered the way the Kory
Code client answers them (HAI-12330): poll ``pending_work`` and post each
completion to ``/api/reviews/{id}/work/{work_id}``. The relay HTTP transport
must never run. The database is SQLite under a temporary ``MIRA_INDEX_DIR``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import mira.config
import mira.dashboard.api as dashboard_api
from mira.dashboard.db import AppDatabase
from mira.llm.router_acp import RouterAcpProvider, RouterTransportError
from mira.models import PRInfo, ReviewResult
from mira.review_api import runner as review_runner
from mira.review_api import service as review_service
from mira.review_api.executor import ReviewExecutor
from mira.review_api.runner import JobOutcome, run_job
from mira.review_api.settings import ReviewApiSettings
from mira.review_api.snapshot import (
    FrozenSnapshot,
    PullMeta,
    ReadOnlySnapshotProvider,
    ReadOnlyViolation,
    SnapshotReader,
)
from mira.review_api.store import ReviewJobStore

OWNER, REPO = "hickoryai", "widget"
HEAD = "a" * 40
NEW_HEAD = "c" * 40
BASE = "b" * 40
MERGE_BASE = "d" * 40
ENGINEER_TOKEN = "kst_engineer_secret"
RELAY_TOKEN = "kst_relay_secret"
OTHER_TOKEN = "kst_other_repo_secret"

PATCH = "@@ -1,2 +1,3 @@\n def handle(x):\n-    return x\n+    y = x / 0\n+    return y\n"
FINDING = {
    "path": "app/handler.py",
    "line": 2,
    "end_line": None,
    "severity": "blocker",
    "category": "bug",
    "title": "Division by zero",
    "body": "`x / 0` always raises `ZeroDivisionError`.",
    "confidence": 0.95,
    "existing_code": "    y = x / 0",
}


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ── fakes ──────────────────────────────────────────────────────────


class FakeGitHub:
    """Serves one repo's PRs by SHA and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.writes: list[str] = []
        self.pulls: dict[int, dict[str, Any]] = {}
        # Compare results are keyed by head SHA: commits are immutable.
        self.compares: dict[str, list[dict[str, Any]]] = {}
        self.add_pull(7, title="Divide things", head=HEAD)

    def add_pull(self, number: int, *, title: str, head: str, **extra: Any) -> None:
        self.pulls[number] = {
            "number": number,
            "title": title,
            "body": "PR body",
            "state": "open",
            "head": {"sha": head, "ref": "feature", "repo": {"full_name": f"{OWNER}/{REPO}"}},
            "base": {"sha": BASE, "ref": "main"},
            "html_url": f"https://github.com/{OWNER}/{REPO}/pull/{number}",
            "user": {"login": "dev"},
            "changed_files": 2,
            **extra,
        }
        self.compares[head] = [
            {"filename": "app/handler.py", "status": "modified", "patch": PATCH},
            {"filename": "assets/logo.png", "status": "added"},
        ]

    def move_head(self, number: int, sha: str) -> None:
        self.pulls[number]["head"]["sha"] = sha

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method != "GET":
            self.writes.append(f"{request.method} {request.url.path}")
            return httpx.Response(500, json={"message": "write attempted"})
        path = request.url.path
        prefix = f"/repos/{OWNER}/{REPO}"
        if not path.startswith(prefix):
            return httpx.Response(404, json={})
        rest = path[len(prefix) :]
        if rest.startswith("/pulls/"):
            pull = self.pulls.get(int(rest.split("/")[2]))
            if pull is None:
                return httpx.Response(404, json={})
            return httpx.Response(200, json=pull)
        if rest.startswith("/compare/"):
            base, _, head = rest[len("/compare/") :].partition("...")
            files = self.compares.get(head) if base == BASE else None
            if files is None:
                return httpx.Response(404, json={})
            page = int(request.url.params.get("page", "1"))
            return httpx.Response(
                200,
                json={
                    "merge_base_commit": {"sha": MERGE_BASE},
                    "files": files if page == 1 else [],
                },
            )
        if rest.startswith("/contents/"):
            content = base64.b64encode(b"def handle(x):\n    y = x / 0\n    return y\n").decode()
            return httpx.Response(200, json={"content": content})
        if rest.startswith("/git/trees/"):
            return httpx.Response(200, json={"tree": [{"path": "app/handler.py", "type": "blob"}]})
        if rest == "/commits":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class FakeSeat:
    """Stands in for the requester's seat: one prompt in, one completion out."""

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.findings: list[dict[str, Any]] = [FINDING]
        self.actual_model = "grok/grok-4.6"
        self.fail_main: list[str] = []  # router states for successive main-review calls
        self.fail_security: str | None = None

    def respond(self, prompt: str) -> dict[str, Any]:
        self.prompts.append(prompt)
        actual = self.actual_model
        if "Title: Other PR" in prompt or "Other PR" in prompt:
            actual = "claude/sonnet-5.5"
        if "`submit_walkthrough`" in prompt:
            text = {
                "summary": "Adds a division.",
                "change_groups": [
                    {
                        "label": "Core",
                        "files": [
                            {
                                "path": "app/handler.py",
                                "change_type": "modified",
                                "description": "divides",
                            }
                        ],
                    }
                ],
            }
        elif "`submit_review`" in prompt and "dedicated security reviewer" in prompt:
            if self.fail_security:
                raise RouterTransportError("busy", kind=self.fail_security)
            text = {"comments": [], "summary": "No security issues."}
        elif "`submit_review`" in prompt:
            if self.fail_main:
                raise RouterTransportError("busy", kind=self.fail_main.pop(0))
            text = {"comments": self.findings, "summary": "One problem."}
        else:
            return {"text": "Summary of the review.", "actual_model": actual}
        return {"text": json.dumps(text), "actual_model": actual}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path / "idx"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("LLM_OTLP_ENDPOINT", raising=False)
    # Set to values that would fail if read: the review API has no relay.
    monkeypatch.setenv("MIRA_ROUTER_ACP_URL", "http://relay.example.com")
    monkeypatch.setenv("MIRA_ROUTER_ACP_TOKEN", "must-not-be-sent")
    monkeypatch.setenv("MIRA_ROUTER_ACP_SESSION", "must-not-be-sent")
    monkeypatch.setattr(review_runner, "WORK_POLL_SECONDS", 0.01)
    monkeypatch.setenv("MIRA_ROUTER_ACP_ALLOW_UNTRACED", "1")
    monkeypatch.setenv("MIRA_REVIEW_API_ENABLED", "1")
    monkeypatch.setenv(
        "MIRA_REVIEW_API_TOKENS",
        json.dumps(
            [
                {
                    "principal": "zane-laptop",
                    "kind": "engineer",
                    "sha256": _digest(ENGINEER_TOKEN),
                    "repos": [f"{OWNER}/{REPO}"],
                },
                {
                    "principal": "kory-relay",
                    "kind": "relay",
                    "sha256": _digest(RELAY_TOKEN),
                    "repos": [f"{OWNER}/*"],
                },
                {
                    "principal": "elsewhere",
                    "kind": "engineer",
                    "sha256": _digest(OTHER_TOKEN),
                    "repos": ["someone/else"],
                },
            ]
        ),
    )
    db = AppDatabase(url=str(tmp_path / "app.db"), admin_password="pw")
    db.register_repo(OWNER, REPO, installation_id=42)
    monkeypatch.setattr(dashboard_api, "_app_db", db)
    monkeypatch.setattr(
        mira.config,
        "_global_defaults",
        {
            "llm": {"provider": "router-acp", "model": "auto", "router_acp_max_retries": 1},
            "review": {"self_critique": False},
        },
    )

    github = FakeGitHub()
    seat = FakeSeat()

    relay_calls: list[dict[str, Any]] = []

    async def relay_spy(self, request):  # type: ignore[no-untyped-def]
        relay_calls.append(request)
        raise AssertionError("the review API dialed the relay")

    monkeypatch.setattr(RouterAcpProvider, "_http_transport", relay_spy)
    monkeypatch.setattr(sys.modules[__name__], "_SEAT", seat)
    RouterAcpProvider.call_log.clear()

    original_factory = review_service.github_reader_factory
    monkeypatch.setattr(
        review_service,
        "github_reader_factory",
        lambda app_auth, transport=None: original_factory(app_auth, transport=github.transport()),
    )

    async def no_backfill(_auth):  # type: ignore[no-untyped-def]
        return None

    async def no_poller():  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr("mira.platforms.server.backfill_missing_indexes", no_backfill)
    monkeypatch.setattr("mira.security.poller.run_forever", no_poller)
    monkeypatch.setattr(review_runner, "custom_rules_providers", [])
    return {"db": db, "github": github, "seat": seat, "tmp": tmp_path, "relay_calls": relay_calls}


class FakeAppAuth:
    async def get_installation_token(self, installation_id: int) -> str:
        assert installation_id == 42
        return "ghs_installation"


def _client(base_url: str = "https://testserver") -> TestClient:
    from mira.platforms.server import create_app

    app = create_app(app_auth=FakeAppAuth(), webhook_secret="s", bot_name="kory")  # type: ignore[arg-type]
    return TestClient(app, base_url=base_url)


def _auth(token: str = ENGINEER_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _submit(client: TestClient, token: str = ENGINEER_TOKEN, **body: Any) -> httpx.Response:
    payload = {"repo": f"{OWNER}/{REPO}", "pr_number": 7, "head_sha": HEAD, **body}
    return client.post("/api/reviews", json=payload, headers=_auth(token))


_SEAT: FakeSeat | None = None


def _seat_payload(seat: FakeSeat, prompt: str) -> dict[str, Any]:
    try:
        return seat.respond(prompt)
    except RouterTransportError as exc:
        return {"error": exc.kind}


def _answer(
    client: TestClient, job_id: str, item: dict[str, Any], token: str = ENGINEER_TOKEN
) -> httpx.Response:
    assert _SEAT is not None
    return client.post(
        f"/api/reviews/{job_id}/work/{item['work_id']}",
        json=_seat_payload(_SEAT, item["prompt"]),
        headers=_auth(token),
    )


def _wait(
    client: TestClient, job_id: str, token: str = ENGINEER_TOKEN, *, answer: bool = True
) -> dict[str, Any]:
    """Poll like the Kory Code client: run each pending prompt, post it back."""
    deadline = time.time() + 20
    while time.time() < deadline:
        body = client.get(f"/api/reviews/{job_id}", headers=_auth(token)).json()
        if body["state"] in {"succeeded", "failed", "interrupted"}:
            assert body["pending_work"] == []
            return body
        for item in body["pending_work"] if answer else []:
            resp = _answer(client, job_id, item, token)
            assert resp.status_code in {200, 409}, resp.text
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


def _assert_no_side_effects(env: dict[str, Any]) -> None:
    assert env["github"].writes == []
    assert all(r.method == "GET" for r in env["github"].requests)
    db: AppDatabase = env["db"]
    assert db._sqlite_conn is not None
    assert db._sqlite_conn.execute("SELECT COUNT(*) FROM pr_review_progress").fetchone()[0] == 0


# ── submission, status, report ─────────────────────────────────────


def test_review_succeeds_without_touching_github(env):
    with _client() as client:
        resp = _submit(client)
        assert resp.status_code == 202, resp.text
        created = resp.json()
        assert created["state"] == "queued"
        assert created["requested_head_sha"] == HEAD
        assert created["findings"] is None
        assert created["status_url"] == f"https://testserver/api/reviews/{created['id']}"
        assert created["report_url"].endswith(f"/api/reviews/{created['id']}/report")

        done = _wait(client, created["id"])
        assert done["state"] == "succeeded", (done["error"], done["model_calls"])
        assert done["error"] is None
        assert done["reviewed_head_sha"] == HEAD
        assert done["base_sha"] == BASE
        assert done["merge_base_sha"] == MERGE_BASE
        assert [f["title"] for f in done["findings"]] == ["Division by zero"]
        finding = done["findings"][0]
        assert finding["severity"] == "blocker"
        assert finding["registry_ids"] == []
        assert done["models"] == {"requested": ["auto"], "actual": ["grok/grok-4.6"]}
        assert all(c["state"] == "ok" for c in done["model_calls"])
        skipped = {s["path"]: s["reason"] for s in done["coverage"]["skipped"]}
        assert skipped == {"assets/logo.png": "patch_unavailable"}
        assert done["coverage"]["reviewed_paths"] == ["app/handler.py"]

        report = client.get(done["report_url"].replace("https://testserver", ""), headers=_auth())
        assert report.status_code == 200
        assert "Division by zero" in report.text
        assert "Nothing was posted to GitHub." in report.text

    _assert_no_side_effects(env)
    assert env["relay_calls"] == []
    reads = env["github"].requests
    for r in reads:
        path = r.url.path
        if "/contents/" in path:
            assert r.url.params["ref"] == HEAD
        if "/git/trees/" in path:
            assert path.endswith(HEAD)
        if path.endswith("/commits"):
            assert r.url.params["sha"] == HEAD
        if "/compare/" in path:
            assert path.endswith(f"{BASE}...{HEAD}")


def test_zero_findings_is_an_empty_list(env):
    env["seat"].findings = []
    with _client() as client:
        done = _wait(client, _submit(client).json()["id"])
    assert done["state"] == "succeeded"
    assert done["findings"] == []


def test_stale_head_is_rejected_before_any_model_call(env):
    env["github"].move_head(7, NEW_HEAD)
    with _client() as client:
        resp = _submit(client)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "head_sha_mismatch"
    assert resp.json()["error"]["current_head_sha"] == NEW_HEAD
    assert env["seat"].prompts == []
    db = env["db"]
    assert db._sqlite_conn.execute("SELECT COUNT(*) FROM review_api_jobs").fetchone()[0] == 0


@pytest.mark.parametrize(
    "body",
    [
        {"repo": f"{OWNER}/{REPO}", "pr_number": 7, "head_sha": HEAD, "post": True},
        {"repo": f"{OWNER}/{REPO}", "pr_number": 7, "head_sha": HEAD[:12]},
        {"repo": f"{OWNER}/{REPO}", "pr_number": 7, "head_sha": HEAD.upper()},
        {"repo": f"{OWNER}/{REPO}", "pr_number": "7", "head_sha": HEAD},
        {"repo": f"{OWNER}/{REPO}", "pr_number": True, "head_sha": HEAD},
        {"repo": f"{OWNER}/{REPO}", "pr_number": 0, "head_sha": HEAD},
        {"repo": "not-a-repo", "pr_number": 7, "head_sha": HEAD},
        {"pr_number": 7, "head_sha": HEAD},
    ],
)
def test_invalid_bodies_are_rejected(env, body):
    with _client() as client:
        resp = client.post("/api/reviews", json=body, headers=_auth())
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"
    assert env["github"].requests == []


def test_authentication_and_repo_isolation(env):
    with _client() as client:
        assert _submit(client, token="wrong").status_code == 401
        assert client.post("/api/reviews", json={}).status_code == 401
        # A review token is not a dashboard credential.
        assert client.get("/api/repos", headers=_auth()).status_code == 401
        assert client.get("/api/admin/settings", headers=_auth()).status_code == 401

        denied = _submit(client, token=OTHER_TOKEN)
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "repo_not_allowed"
        assert env["github"].requests == []

        job = _submit(client).json()
        _wait(client, job["id"])
        # Another principal cannot see the job, even one allowed on the repo.
        assert (
            client.get(f"/api/reviews/{job['id']}", headers=_auth(RELAY_TOKEN)).status_code == 404
        )
        assert (
            client.get(f"/api/reviews/{job['id']}", headers=_auth(OTHER_TOKEN)).status_code == 404
        )

        # Dashboard users can read any job and its report, but cannot submit.
        login = client.post("/api/auth/login", json={"username": "admin", "password": "pw"})
        assert login.status_code == 200
        assert client.get(f"/api/reviews/{job['id']}").json()["state"] == "succeeded"
        assert client.get(f"/api/reviews/{job['id']}/report").status_code == 200
        session_submit = client.post(
            "/api/reviews", json={"repo": f"{OWNER}/{REPO}", "pr_number": 7, "head_sha": HEAD}
        )
        assert session_submit.status_code == 403
        assert session_submit.json()["error"]["code"] == "token_required"


def test_bearer_tokens_require_https_off_loopback(env):
    with _client(base_url="http://kory-source.example.com") as client:
        assert _submit(client).status_code == 403
        forwarded = client.post(
            "/api/reviews",
            json={"repo": f"{OWNER}/{REPO}", "pr_number": 7, "head_sha": NEW_HEAD},
            headers={**_auth(), "X-Forwarded-Proto": "https"},
        )
        assert forwarded.status_code == 409  # authenticated; head check ran
    with _client(base_url="http://127.0.0.1:8765") as client:
        assert _submit(client).status_code == 202


def test_disabled_executor_returns_503_before_github(env, monkeypatch):
    monkeypatch.delenv("MIRA_REVIEW_API_ENABLED")
    with _client() as client:
        resp = _submit(client)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "review_execution_unavailable"
    assert env["github"].requests == []


def test_duplicate_submission_returns_the_active_job(env, monkeypatch):
    monkeypatch.setenv("MIRA_REVIEW_API_MAX_RUNNING", "1")
    started = asyncio.Event()

    with _client() as client:
        svc = review_service.current()
        assert svc is not None and svc.executor is not None
        release: asyncio.Future[None] | None = None

        async def blocked(job, reader_factory, set_stage):  # type: ignore[no-untyped-def]
            nonlocal release
            release = asyncio.get_running_loop().create_future()
            started.set()
            await release
            return JobOutcome(
                "succeeded",
                result={"summary": "", "findings": [], "key_issues": [], "coverage": {}},
            )

        svc.executor._runner = blocked
        first = _submit(client).json()
        second = _submit(client).json()
        assert second["id"] == first["id"]
        assert second["deduplicated"] is True
        deadline = time.time() + 5
        while release is None and time.time() < deadline:
            time.sleep(0.02)
        assert release is not None
        client.portal.call(lambda: release.set_result(None))  # type: ignore[union-attr]
        assert _wait(client, first["id"])["state"] == "succeeded"


# ── capture: forks, races, incomplete diffs ────────────────────────


def test_fork_head_is_compared_in_the_base_repo(env):
    env["github"].pulls[7]["head"]["repo"] = {"full_name": "contributor/widget"}
    with _client() as client:
        done = _wait(client, _submit(client).json()["id"])
    assert done["state"] == "succeeded"
    compares = [r.url.path for r in env["github"].requests if "/compare/" in r.url.path]
    assert compares == [f"/repos/{OWNER}/{REPO}/compare/{BASE}...{HEAD}"]


def test_incomplete_diff_fails_without_model_calls(env):
    env["github"].pulls[7]["changed_files"] = 500
    with _client() as client:
        done = _wait(client, _submit(client).json()["id"])
    assert done["state"] == "failed"
    assert done["error"]["code"] == "diff_incomplete"
    assert done["findings"] is None
    assert env["seat"].prompts == []


def _job_row(store: ReviewJobStore, head: str = HEAD, number: int = 7):
    job = store.create(
        principal="zane-laptop",
        owner=OWNER,
        repo=REPO,
        pr_number=number,
        head_sha=head,
        base_sha=BASE,
        max_queued=10,
    )
    assert job is not None
    return job


def _reader_factory(github: FakeGitHub):
    async def token() -> str:
        return "ghs"

    async def factory(owner: str, repo: str) -> SnapshotReader:
        return SnapshotReader(owner, repo, token, transport=github.transport())

    return factory


async def _run_answered(env: dict[str, Any], job: Any, set_stage: Any = None) -> JobOutcome:
    """Run one job in-process while a fake requester answers its work rows."""
    store = ReviewJobStore(env["db"])
    claimed = store.claim_next("w_test", lease_seconds=60, deadline_seconds=60, max_running=10)
    assert claimed is not None and claimed.id == job.id

    async def requester() -> None:
        while True:
            for work in store.pending_work(job.id):
                payload = _seat_payload(env["seat"], work.prompt)
                store.answer_work(
                    job.id,
                    work.id,
                    text=payload.get("text", ""),
                    actual_model=payload.get("actual_model", "unknown"),
                    error_code=payload.get("error", ""),
                )
            await asyncio.sleep(0.01)

    answering = asyncio.create_task(requester())
    try:
        return await run_job(
            claimed, _reader_factory(env["github"]), set_stage or (lambda *a, **k: None)
        )
    finally:
        answering.cancel()


async def test_head_moving_before_capture_fails_cleanly(env):
    store = ReviewJobStore(env["db"])
    job = _job_row(store)
    env["github"].move_head(7, NEW_HEAD)
    outcome = await run_job(job, _reader_factory(env["github"]), lambda *a, **k: None)
    assert outcome.state == "failed"
    assert outcome.error_code == "head_sha_mismatch"
    assert env["seat"].prompts == []


async def test_push_after_capture_does_not_change_the_review(env):
    store = ReviewJobStore(env["db"])
    job = _job_row(store)
    github, seat = env["github"], env["seat"]
    original = seat.respond

    def push_then_respond(prompt: str) -> dict[str, Any]:
        github.move_head(7, NEW_HEAD)
        github.compares[NEW_HEAD] = [
            {"filename": "app/other.py", "status": "added", "patch": "@@ -0,0 +1 @@\n+x = 1\n"}
        ]
        return original(prompt)

    seat.respond = push_then_respond  # type: ignore[method-assign]
    stages: list[tuple[str, dict]] = []
    outcome = await _run_answered(env, job, lambda stage, **f: stages.append((stage, f)))
    assert outcome.state == "succeeded"
    assert stages[0] == (
        "reviewing",
        {"reviewed_head_sha": HEAD, "base_sha": BASE, "merge_base_sha": MERGE_BASE},
    )
    assert outcome.result is not None
    assert outcome.result["coverage"]["reviewed_paths"] == ["app/handler.py"]
    for r in github.requests:
        if "/contents/" in r.url.path:
            assert r.url.params["ref"] == HEAD
        if "/git/trees/" in r.url.path:
            assert r.url.path.endswith(HEAD)
    assert github.writes == []


# ── seats: exhaustion, outage, swallowed optional failures ─────────


@pytest.mark.parametrize(
    ("states", "code"),
    [
        (["seat_exhausted", "seat_exhausted"], "seat_exhausted"),
        (["workstation_outage", "workstation_outage"], "workstation_outage"),
        (["model_mismatch"], "model_mismatch"),
        (["timeout"], "model_timeout"),
    ],
)
def test_seat_failures_are_distinct(env, states, code):
    env["seat"].fail_main = list(states)
    with _client() as client:
        done = _wait(client, _submit(client).json()["id"])
    assert done["state"] == "failed"
    assert done["error"]["code"] == code
    assert done["findings"] is None
    assert any(c["state"] != "ok" for c in done["model_calls"])
    _assert_no_side_effects(env)


def test_retry_within_budget_succeeds_and_reports_attempts(env):
    env["seat"].fail_main = ["seat_exhausted"]
    with _client() as client:
        done = _wait(client, _submit(client).json()["id"])
    assert done["state"] == "succeeded"
    assert max(c["attempts"] for c in done["model_calls"]) == 2


def test_swallowed_optional_pass_failure_still_fails_the_job(env):
    # The engine tolerates a failed security pass; the API must not.
    env["seat"].fail_security = "seat_exhausted"
    with _client() as client:
        done = _wait(client, _submit(client).json()["id"])
    assert done["state"] == "failed"
    assert done["error"]["code"] == "seat_exhausted"
    assert done["findings"] is None


async def test_non_seat_provider_is_refused(env, monkeypatch):
    monkeypatch.setattr(
        mira.config, "_global_defaults", {"llm": {"provider": "openrouter", "model": "x/y"}}
    )
    store = ReviewJobStore(env["db"])
    outcome = await run_job(_job_row(store), _reader_factory(env["github"]), lambda *a, **k: None)
    assert outcome.state == "failed"
    assert outcome.error_code == "seat_provider_unconfigured"
    assert env["seat"].prompts == []


def test_concurrent_jobs_keep_their_own_model_calls(env):
    env["github"].add_pull(8, title="Other PR", head=NEW_HEAD)
    with _client() as client:
        a = _submit(client).json()
        b = _submit(client, pr_number=8, head_sha=NEW_HEAD).json()
        done_a, done_b = _wait(client, a["id"]), _wait(client, b["id"])
    assert done_a["state"] == done_b["state"] == "succeeded"
    assert done_a["models"]["actual"] == ["grok/grok-4.6"]
    assert done_b["models"]["actual"] == ["claude/sonnet-5.5"]


async def test_custom_rules_hook_reaches_the_prompt(env, monkeypatch):
    async def registry_rules(snapshot: FrozenSnapshot) -> list[dict[str, str]]:
        assert snapshot.meta.head_sha == HEAD
        return [{"title": "Registry rule", "content": "Never divide by a literal zero."}]

    monkeypatch.setattr(review_runner, "custom_rules_providers", [registry_rules])
    store = ReviewJobStore(env["db"])
    outcome = await _run_answered(env, _job_row(store))
    assert outcome.state == "succeeded"
    assert any("Never divide by a literal zero." in p for p in env["seat"].prompts)


# ── requester work: protocol, auth, clocks ─────────────────────────


def _first_pending(client: TestClient, job_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    deadline = time.time() + 10
    while time.time() < deadline:
        body = client.get(f"/api/reviews/{job_id}", headers=_auth()).json()
        if body["pending_work"]:
            return body, body["pending_work"][0]
        assert body["state"] in {"queued", "running"}, body["error"]
        time.sleep(0.02)
    raise AssertionError("no work became pending")


def test_pending_work_shape_and_relay_never_dialed(env):
    with _client() as client:
        created = _submit(client).json()
        assert created["pending_work"] == []
        view, item = _first_pending(client, created["id"])
        assert (view["state"], view["stage"]) == ("running", "reviewing")
        assert set(item) == {"work_id", "prompt", "model", "timeout_seconds"}
        assert item["work_id"].startswith("wrk_") and len(item["work_id"]) == 36
        assert item["model"] == "auto"
        assert item["timeout_seconds"] == 900
        assert "Mira" in item["prompt"]
        report = client.get(f"/api/reviews/{created['id']}/report", headers=_auth())
        assert item["prompt"] not in report.text
        done = _wait(client, created["id"])
    assert done["state"] == "succeeded"
    assert done["models"]["actual"] == ["grok/grok-4.6"]
    assert env["relay_calls"] == []


def test_only_the_submitting_token_can_answer(env):
    with _client() as client:
        job_id = _submit(client).json()["id"]
        _, item = _first_pending(client, job_id)
        url = f"/api/reviews/{job_id}/work/{item['work_id']}"
        good = {"text": "x", "actual_model": "grok/grok-4.6"}

        other = client.post(url, json=good, headers=_auth(RELAY_TOKEN))
        assert other.status_code == 404
        assert other.json()["error"]["code"] == "review_not_found"
        assert client.post(url, json=good).status_code == 401
        assert client.post(url, json=good, headers=_auth("wrong")).status_code == 401
        still = client.get(f"/api/reviews/{job_id}", headers=_auth()).json()["pending_work"]
        assert item["work_id"] in {w["work_id"] for w in still}

        missing = client.post(f"/api/reviews/{job_id}/work/wrk_nope", json=good, headers=_auth())
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "work_not_found"

        for bad in (
            {"text": "x", "actual_model": "m", "error": "timeout"},
            {"text": "   ", "actual_model": "m"},
            {"text": "x"},
            {"text": "x", "actual_model": "m" * 201},
            {"error": "on_fire"},
            {"text": "x", "actual_model": "m", "extra": 1},
        ):
            resp = client.post(url, json=bad, headers=_auth())
            assert resp.status_code == 422, bad
            assert resp.json()["error"]["code"] == "invalid_request"
        still = client.get(f"/api/reviews/{job_id}", headers=_auth()).json()["pending_work"]
        assert item["work_id"] in {w["work_id"] for w in still}

        login = client.post("/api/auth/login", json={"username": "admin", "password": "pw"})
        assert login.status_code == 200
        session = client.post(url, json=good)
        assert session.status_code == 403
        assert session.json()["error"]["code"] == "token_required"
        client.cookies.clear()

        first = _answer(client, job_id, item)
        assert first.status_code == 200
        assert first.json() == {"work_id": item["work_id"], "state": "answered"}
        again = _answer(client, job_id, item)
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "work_not_pending"
        done = _wait(client, job_id)
        assert done["state"] == "succeeded"
        late = _answer(client, job_id, item)
        assert late.json()["error"]["code"] == "work_not_pending"


def test_bearer_reaches_the_work_route_only(env):
    with _client() as client:
        job_id = _submit(client).json()["id"]
        _, item = _first_pending(client, job_id)
        for path in (
            f"/api/reviews/{job_id}/work",
            f"/api/reviews/{job_id}/work/{item['work_id']}/x",
            f"/api/reviews/{job_id}/other/{item['work_id']}",
        ):
            assert client.post(path, json={}, headers=_auth()).status_code == 401, path
        _wait(client, job_id)


def test_unanswered_work_times_out_without_findings(env, monkeypatch):
    monkeypatch.setattr(
        mira.config,
        "_global_defaults",
        {
            "llm": {"provider": "router-acp", "model": "auto", "router_acp_timeout_seconds": 1},
            "review": {"self_critique": False},
        },
    )
    with _client() as client:
        job_id = _submit(client).json()["id"]
        _, item = _first_pending(client, job_id)
        assert item["timeout_seconds"] == 1
        done = _wait(client, job_id, answer=False)
        late = _answer(client, job_id, item)
    assert done["state"] == "failed"
    assert done["error"]["code"] == "model_timeout"
    assert done["findings"] is None
    assert any(c["state"] == "timeout" for c in done["model_calls"])
    assert late.status_code == 409
    rows = (
        env["db"]
        ._sqlite_conn.execute("SELECT state FROM review_api_work WHERE job_id = ?", (job_id,))
        .fetchall()
    )
    assert {r[0] for r in rows} <= {"failed", "answered", "expired"}


def test_pinned_model_answered_by_another_fails(env, monkeypatch):
    monkeypatch.setattr(
        mira.config,
        "_global_defaults",
        {
            "llm": {"provider": "router-acp", "model": "grok/grok-4.6"},
            "review": {"self_critique": False},
        },
    )
    env["seat"].actual_model = "claude/sonnet-5.5"
    with _client() as client:
        done = _wait(client, _submit(client).json()["id"])
    assert done["state"] == "failed"
    assert done["error"]["code"] == "model_mismatch"
    assert done["findings"] is None
    assert done["models"]["requested"] == ["grok/grok-4.6"]


def test_retry_opens_a_new_work_item(env):
    env["seat"].fail_main = ["workstation_outage"]
    with _client() as client:
        job_id = _submit(client).json()["id"]
        done = _wait(client, job_id)
    assert done["state"] == "succeeded"
    rows = (
        env["db"]
        ._sqlite_conn.execute(
            "SELECT state, error_code FROM review_api_work WHERE job_id = ?", (job_id,)
        )
        .fetchall()
    )
    assert ("failed", "workstation_outage") in rows
    assert sum(1 for r in rows if r[0] == "answered") >= 2


async def test_finishing_a_job_expires_its_pending_work(env):
    store = ReviewJobStore(env["db"])
    job = _job_row(store)
    claimed = store.claim_next("w1", lease_seconds=30, deadline_seconds=60, max_running=2)
    assert claimed is not None
    work_id = store.create_work(job.id, "p", "auto", 900)
    assert [w.id for w in store.pending_work(job.id)] == [work_id]
    assert store.finish(job.id, "w1", state="failed", error_code="deadline_exceeded")
    assert store.get_work(work_id).state == "expired"  # type: ignore[union-attr]
    assert store.pending_work(job.id) == []
    assert not store.answer_work(job.id, work_id, text="x", actual_model="m")


async def test_cancelled_waiter_expires_its_item_without_a_failure(env):
    store = ReviewJobStore(env["db"])
    job = _job_row(store)
    store.claim_next("w1", lease_seconds=30, deadline_seconds=60, max_running=2)
    transport = review_runner.requester_transport(store, job.id)
    task = asyncio.create_task(
        transport({"body": {"prompt": "p", "model": "auto", "timeout_seconds": 900}})
    )
    await asyncio.sleep(0.05)
    (work,) = store.pending_work(job.id)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.get_work(work.id).state == "expired"  # type: ignore[union-attr]


async def test_registry_judge_uses_the_job_requester(env):
    from mira.exceptions import LLMError
    from mira.llm.router_acp import job_llm
    from mira.registry.conflicts import default_judge

    judge = default_judge()
    assert judge is not None
    new = {"id": "", "kind": "rule", "title": "New", "summary": "", "body": ""}
    old = {"id": "dec_1", "kind": "rule", "title": "Old", "summary": "", "body": ""}
    with pytest.raises(LLMError) as exc:
        await judge(new, old)
    assert exc.value.code == "seat_provider_unconfigured"
    assert env["relay_calls"] == []

    class Seat:
        async def complete(self, messages, json_mode=True):  # type: ignore[no-untyped-def]
            return json.dumps({"relation": "none", "new_clause": "", "existing_clause": ""})

    token = job_llm.set(Seat())
    try:
        assert (await judge(new, old))["relation"] == "none"
    finally:
        job_llm.reset(token)


async def test_webhook_review_fails_before_any_github_write(env, monkeypatch):
    from mira.core.review_status import tracker
    from mira.exceptions import LLMError
    from mira.platforms import handlers

    entered: list[str] = []

    async def review_pr(self, pr_url):  # type: ignore[no-untyped-def]
        entered.append(pr_url)

    monkeypatch.setattr(handlers.ReviewEngine, "review_pr", review_pr)

    class NoWrites:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"provider.{name} was used")

    with pytest.raises(LLMError) as exc:
        await handlers.run_pr_review(
            NoWrites(), OWNER, REPO, 7, "https://github.com/hickoryai/widget/pull/7", False, "kory"
        )
    assert exc.value.code == "seat_provider_unconfigured"
    assert entered == []
    assert tracker.try_start(f"{OWNER}/{REPO}", 7, "", "u")  # the slot was released
    tracker.complete(f"{OWNER}/{REPO}", 7)


# ── read-only provider ─────────────────────────────────────────────


async def test_snapshot_provider_refuses_every_write(env):
    meta = PullMeta(OWNER, REPO, 7, "t", "d", "open", HEAD, "f", "", BASE, "main", "u", "dev", 1)
    snap = FrozenSnapshot(meta, MERGE_BASE, "", [])
    reader = SnapshotReader(OWNER, REPO, lambda: None, transport=env["github"].transport())  # type: ignore[arg-type]
    provider = ReadOnlySnapshotProvider(reader, snap)
    info = snap.pr_info()
    calls = [
        provider.post_review(info, ReviewResult()),
        provider.post_comment(info, "x"),
        provider.find_bot_comment(info, "m"),
        provider.update_comment(info, 1, "x"),
        provider.resolve_outdated_review_threads(info),
        provider.resolve_threads(info, ["t"]),
        provider.update_pr_description(info, "x"),
        provider.add_label(info, "l"),
        provider.remove_label(info, "l"),
        provider.reply_to_review_comment(info, 1, "x"),
    ]
    for call in calls:
        with pytest.raises(ReadOnlyViolation):
            await call
    assert len(provider.write_attempts) == len(calls)
    assert env["github"].requests == []
    assert isinstance(info, PRInfo) and info.head_branch == HEAD


# ── store and executor lifecycle ───────────────────────────────────


def _settings(**overrides: Any) -> ReviewApiSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "max_running": 2,
        "max_queued": 10,
        "deadline_seconds": 60,
        "lease_seconds": 30,
        "poll_seconds": 0.05,
        "public_url": "",
    }
    base.update(overrides)
    return ReviewApiSettings(**base)  # type: ignore[arg-type]


def test_store_claims_within_cap_and_never_requeues(env):
    store = ReviewJobStore(env["db"])
    jobs = [_job_row(store, number=n) for n in (1, 2, 3)]
    first = store.claim_next("w1", lease_seconds=30, deadline_seconds=60, max_running=2)
    second = store.claim_next("w2", lease_seconds=30, deadline_seconds=60, max_running=2)
    assert {first.id, second.id} == {jobs[0].id, jobs[1].id}  # type: ignore[union-attr]
    assert store.claim_next("w1", lease_seconds=30, deadline_seconds=60, max_running=2) is None
    assert store.running_count() == 2

    assert store.finish(first.id, "w2", state="succeeded", result={}) is False  # type: ignore[union-attr]
    assert store.heartbeat(first.id, "w1", 30)  # type: ignore[union-attr]

    assert store.interrupt_expired(now=time.time() + 3600) == 2
    interrupted = store.get(first.id)  # type: ignore[union-attr]
    assert interrupted is not None
    assert interrupted.state == "interrupted"
    assert interrupted.error_code == "executor_interrupted"
    # Interrupted jobs are terminal: the next claim takes the queued job only.
    third = store.claim_next("w1", lease_seconds=30, deadline_seconds=60, max_running=2)
    assert third is not None and third.id == jobs[2].id
    assert store.claim_next("w1", lease_seconds=30, deadline_seconds=60, max_running=2) is None


def test_store_queue_limit(env):
    store = ReviewJobStore(env["db"])
    assert store.create(
        principal="p",
        owner=OWNER,
        repo=REPO,
        pr_number=1,
        head_sha=HEAD,
        base_sha=BASE,
        max_queued=1,
    )
    assert (
        store.create(
            principal="p",
            owner=OWNER,
            repo=REPO,
            pr_number=2,
            head_sha=HEAD,
            base_sha=BASE,
            max_queued=1,
        )
        is None
    )


def test_schema_upgrade_on_existing_database_is_additive(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    path = tmp_path / "old.db"
    old = AppDatabase(url=str(path), admin_password="pw")
    old.register_repo(OWNER, REPO, installation_id=42)
    old._sqlite_conn.close()  # type: ignore[union-attr]

    upgraded = AppDatabase(url=str(path), admin_password="pw")
    ReviewJobStore(upgraded)
    ReviewJobStore(upgraded)  # idempotent
    record = upgraded.get_repo(OWNER, REPO)
    assert record is not None and record.installation_id == 42
    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"review_api_jobs", "repos", "users", "pr_review_progress"} <= tables


async def test_executor_deadline_marks_job_failed(env):
    store = ReviewJobStore(env["db"])
    job = _job_row(store)

    async def slow(job, reader_factory, set_stage):  # type: ignore[no-untyped-def]
        await asyncio.sleep(30)

    executor = ReviewExecutor(
        store, _settings(deadline_seconds=1), _reader_factory(env["github"]), runner=slow
    )
    await executor.start()
    await asyncio.sleep(0.2)
    await executor.drain()
    # stop() must return without waiting out the runner. On 3.11 a stuck
    # poll task used to block here until the suite was killed.
    await asyncio.wait_for(executor.stop(), timeout=2)
    final = store.get(job.id)
    assert final is not None
    assert (final.state, final.error_code) == ("failed", "deadline_exceeded")


async def test_executor_shutdown_interrupts_running_jobs(env):
    store = ReviewJobStore(env["db"])
    job = _job_row(store)
    started = asyncio.Event()

    async def forever(job, reader_factory, set_stage):  # type: ignore[no-untyped-def]
        started.set()
        await asyncio.sleep(3600)

    executor = ReviewExecutor(store, _settings(), _reader_factory(env["github"]), runner=forever)
    await executor.start()
    await asyncio.wait_for(started.wait(), timeout=5)
    await asyncio.wait_for(executor.stop(), timeout=2)
    final = store.get(job.id)
    assert final is not None
    assert (final.state, final.error_code) == ("interrupted", "executor_shutdown")


async def test_restart_interrupts_orphaned_jobs_without_rerunning(env):
    store = ReviewJobStore(env["db"])
    job = _job_row(store)
    claimed = store.claim_next(
        "dead-worker", lease_seconds=0.01, deadline_seconds=60, max_running=2
    )
    assert claimed is not None
    await asyncio.sleep(0.05)
    ran: list[str] = []

    async def runner(job, reader_factory, set_stage):  # type: ignore[no-untyped-def]
        ran.append(job.id)
        return JobOutcome("succeeded", result={})

    executor = ReviewExecutor(store, _settings(), _reader_factory(env["github"]), runner=runner)
    await executor.start()
    await asyncio.sleep(0.2)
    await executor.stop()
    final = store.get(job.id)
    assert final is not None
    assert (final.state, final.error_code) == ("interrupted", "executor_interrupted")
    assert ran == []


async def test_crashed_runner_is_reported_as_internal_error(env):
    store = ReviewJobStore(env["db"])
    job = _job_row(store)

    async def boom(job, reader_factory, set_stage):  # type: ignore[no-untyped-def]
        raise RuntimeError("bug")

    executor = ReviewExecutor(store, _settings(), _reader_factory(env["github"]), runner=boom)
    await executor.start()
    await asyncio.sleep(0.2)
    await executor.drain()
    await executor.stop()
    final = store.get(job.id)
    assert final is not None
    assert (final.state, final.error_code, final.result_json) == ("failed", "internal_error", "")


# ── uncited-decision advisories (HAI-12775) ────────────────────────


ADVISORY_BODY = (
    "Adds a division.\n\n"
    "## Decisions I made\n"
    "- Divide before returning\n"
    "- Keep the handler sync (reg_0123456789ab)\n"
)


def _advisories(done: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in done["findings"] if f["source_pass"] == "registry_uncited_decision"]


def test_uncited_decision_is_an_advisory_finding(env):
    env["seat"].findings = []
    env["github"].pulls[7]["body"] = ADVISORY_BODY
    with _client() as client:
        done = _wait(client, _submit(client).json()["id"])
        assert done["state"] == "succeeded", done["error"]
        [finding] = done["findings"]
        assert finding == {
            "id": "f1",
            "path": "",
            "line": 1,
            "end_line": None,
            "severity": "suggestion",
            "category": "registry",
            "title": "Decision lacks a registry citation",
            "body": finding["body"],
            "confidence": finding["confidence"],
            "suggestion": None,
            "source_pass": "registry_uncited_decision",
            "registry_ids": [],
        }
        assert finding["body"].startswith("The pull request body, line 4, ")
        assert '"Divide before returning"' in finding["body"]
        assert "handler sync" not in finding["body"]
        report = client.get(done["report_url"].replace("https://testserver", ""), headers=_auth())
        assert "<i>pull request</i>" in report.text
        assert "<code>:1</code>" not in report.text
    _assert_no_side_effects(env)


def test_advisories_survive_a_diff_with_no_reviewable_file(env):
    github = env["github"]
    github.pulls[7]["body"] = ADVISORY_BODY
    github.pulls[7]["changed_files"] = 1
    github.compares[HEAD] = [{"filename": "assets/logo.png", "status": "added"}]
    with _client() as client:
        done = _wait(client, _submit(client).json()["id"])
    assert done["state"] == "succeeded", done["error"]
    assert [f["source_pass"] for f in done["findings"]] == ["registry_uncited_decision"]
    assert done["coverage"]["reviewed_files"] == 0
    _assert_no_side_effects(env)


def test_advisory_sits_beside_a_contradiction_blocker(env, monkeypatch):
    from mira.registry import set_registry
    from mira.registry.db import RegistryDB
    from mira.registry.service import Registry

    registry = Registry(RegistryDB(path=str(env["tmp"] / "registry.db")))

    async def contradiction(**_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "relation": "narrows",
                "approved_id": "reg_aaaaaaaaaaaa",
                "existing_clause": "handlers stay sync",
                "new_clause": "divide before returning",
                "path": "app/handler.py",
                "line": 2,
            }
        ]

    monkeypatch.setattr(registry, "review_findings", contradiction)
    set_registry(registry)
    env["seat"].findings = []
    env["github"].pulls[7]["body"] = ADVISORY_BODY
    try:
        rows = len(registry._select("1=1"))
        with _client() as client:
            done = _wait(client, _submit(client).json()["id"])
        assert len(registry._select("1=1")) == rows
    finally:
        set_registry(None)
    assert done["state"] == "succeeded", done["error"]
    by_pass = {f["source_pass"]: f for f in done["findings"]}
    assert by_pass["registry"]["severity"] == "blocker"
    assert "reg_aaaaaaaaaaaa" in by_pass["registry"]["body"]
    assert by_pass["registry_uncited_decision"]["severity"] == "suggestion"
