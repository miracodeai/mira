"""Run one review-API job: capture the snapshot, review it, classify the outcome.

Every model role uses the router-acp seat provider. Kory Source runs no
inference for a job: each logical model call becomes a ``review_api_work``
row that the client who submitted the job runs on its own seats and posts
back (HAI-12330). The relay URL, token, and session are never read here.
There is no cloud-key or local fallback, ensemble sampling and tool calling
stay off, and cross-PR overlap detection is skipped because it reads other
PRs. Each job observes
its own model calls through ``router_acp.call_observer``. A failed call in
any pass, including the optional passes the engine tolerates, fails the job
with a code that names the cause, so a seat failure never reads as a clean
review.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from mira.exceptions import LLMError, MiraError
from mira.registry.uncited import advisory_findings
from mira.review_api.snapshot import (
    FrozenSnapshot,
    ReadOnlySnapshotProvider,
    ReadOnlyViolation,
    SnapshotError,
    SnapshotReader,
)
from mira.review_api.store import ReviewJob, ReviewJobStore

logger = logging.getLogger(__name__)

# Router failure state -> job error code. Seat exhaustion and workstation
# outage stay distinct so a client can tell "wait for a seat" from "the
# workstation is down".
_STATE_ERRORS = {
    "seat_exhausted": "seat_exhausted",
    "workstation_outage": "workstation_outage",
    "timeout": "model_timeout",
    "model_mismatch": "model_mismatch",
}
_CODE_ERRORS = {
    "router_acp_seat_exhausted": "seat_exhausted",
    "router_acp_workstation_outage": "workstation_outage",
    "router_acp_timeout": "model_timeout",
    "router_acp_model_mismatch": "model_mismatch",
}

# Hook for registry-supplied review rules (HAI-11605). Each provider takes
# the frozen snapshot and returns ``{"title", "content"}`` rules that are
# appended to the custom rules the engine already loads.
CustomRulesProvider = Callable[[FrozenSnapshot], Awaitable[list[dict[str, str]]]]
custom_rules_providers: list[CustomRulesProvider] = []

# How often a waiting call re-reads its work row. The answer may land on any
# instance, so the database is polled; 500 ms keeps a small Cloud SQL quiet.
WORK_POLL_SECONDS = 0.5


class JobFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class JobOutcome:
    state: str
    error_code: str = ""
    error_message: str = ""
    result: dict[str, Any] | None = None
    model_calls: list[dict[str, Any]] = field(default_factory=list)


StageCallback = Callable[..., None]
ReaderFactory = Callable[[str, str], Awaitable[SnapshotReader]]


def _work_store() -> ReviewJobStore:
    from mira.review_api import service

    svc = service.current()
    if svc is not None:
        return svc.store
    from mira.dashboard.api import _app_db

    return ReviewJobStore(_app_db)


def requester_transport(
    store: ReviewJobStore, job_id: str
) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    """A ``RouterAcpProvider`` transport that parks each attempt as a work row.

    It returns ``{text, actual_model}`` once the requester answers, or raises
    ``RouterTransportError`` with the same ``kind`` values the HTTP transport
    uses, so retries and the pin check stay in the provider.
    """
    from mira.llm.router_acp import RouterTransportError

    async def transport(request: dict[str, Any]) -> dict[str, Any]:
        body = request["body"]
        work_id = store.create_work(
            job_id, body["prompt"], body["model"], int(body["timeout_seconds"])
        )
        try:
            while True:
                work = store.get_work(work_id)
                if work is None or work.state == "expired":
                    raise RouterTransportError("work item expired", kind="router_failed")
                if work.state == "answered":
                    return {"text": work.text, "actual_model": work.actual_model}
                if work.state == "failed":
                    raise RouterTransportError(
                        f"requester reported {work.error_code}",
                        kind=work.error_code,
                        body={"actual_model": work.actual_model or "unknown"},
                    )
                if time.time() > work.deadline_at and store.time_out_work(work_id):
                    raise RouterTransportError("no answer before the work deadline", kind="timeout")
                await asyncio.sleep(WORK_POLL_SECONDS)
        except asyncio.CancelledError:
            # A sibling failed or the job deadline fired. Not a second failure.
            store.expire_work(work_id=work_id)
            raise

    return transport


def _build_engine(provider: ReadOnlySnapshotProvider, job_id: str):  # type: ignore[no-untyped-def]
    from mira.config import load_config
    from mira.core.engine import ReviewEngine
    from mira.dashboard.models_config import llm_config_for
    from mira.llm.router_acp import RouterAcpProvider

    config = load_config()
    if config.llm.provider not in {"router-acp", "router_acp"}:
        raise JobFailure(
            "seat_provider_unconfigured",
            "The review API runs only on router-acp seats; llm.provider is not router-acp.",
        )
    review = config.review.model_copy(
        update={
            "ensemble_runs": 1,
            "agentic_tools": False,
            "security_agentic": False,
            "overlap": config.review.overlap.model_copy(update={"enabled": False}),
        }
    )
    config = config.model_copy(update={"review": review})
    transport = requester_transport(_work_store(), job_id)
    llms = {}
    for role in ("review", "indexing", "security"):
        role_config = llm_config_for(role, config.llm)
        if role_config.provider not in {"router-acp", "router_acp"}:
            raise JobFailure(
                "seat_provider_unconfigured", f"The {role} model is not a router-acp seat."
            )
        llms[role] = RouterAcpProvider(role_config, transport=transport, relay=False)
    return ReviewEngine(
        config=config,
        llm=llms["review"],
        provider=provider,
        bot_name="",
        dry_run=True,
        indexing_llm=llms["indexing"],
        security_llm=llms["security"],
    )


def _team_conventions(owner: str, repo: str) -> str:
    try:
        from mira.dashboard.api import _app_db

        record = _app_db.get_repo(owner, repo)
        return record.conventions if record and record.conventions else ""
    except Exception:
        return ""


def _finding(index: int, c: Any) -> dict[str, Any]:
    return {
        "id": f"f{index + 1}",
        "path": c.path,
        "line": c.line,
        "end_line": c.end_line,
        "severity": c.severity.name.lower(),
        "category": c.category,
        "title": c.title,
        "body": c.body,
        "confidence": c.confidence,
        "suggestion": c.suggestion,
        "source_pass": c.source_pass,
        # Validated registry citations land with HAI-11605. Never guessed here.
        "registry_ids": [],
    }


def _coverage(snapshot: FrozenSnapshot, result: Any) -> dict[str, Any]:
    reviewed = list(result.reviewed_paths)
    considered = set(result.total_paths)
    skipped: list[dict[str, str]] = []
    unavailable = set(snapshot.unavailable_paths)
    for path in snapshot.changed_paths:
        if path in unavailable:
            skipped.append({"path": path, "reason": "patch_unavailable"})
        elif path not in considered:
            skipped.append({"path": path, "reason": "excluded_by_filter"})
    for path in result.skipped_paths:
        skipped.append({"path": path, "reason": "size_or_priority_cap"})
    return {
        "changed_files": len(snapshot.changed_paths),
        "reviewed_files": len(reviewed),
        "reviewed_paths": reviewed,
        "skipped": skipped,
        "note": result.skipped_reason or "",
    }


def _failed_call(calls: list[dict[str, Any]]) -> JobFailure | None:
    for call in calls:
        state = call.get("state")
        if state and state != "ok":
            code = _STATE_ERRORS.get(state, "model_call_failed")
            return JobFailure(
                code,
                f"A model call ended in state {state!r} "
                f"(requested {call.get('requested_model')}, actual {call.get('actual_model')}).",
            )
    return None


async def run_job(
    job: ReviewJob, reader_factory: ReaderFactory, set_stage: StageCallback
) -> JobOutcome:
    from mira.llm.router_acp import call_observer, job_llm

    calls: list[dict[str, Any]] = []
    try:
        reader = await reader_factory(job.owner, job.repo)
        meta = await reader.get_pull(job.pr_number)
        if meta.head_sha != job.requested_head_sha:
            raise JobFailure(
                "head_sha_mismatch",
                f"PR head moved to {meta.head_sha} before capture; requested "
                f"{job.requested_head_sha}. Nothing was reviewed.",
            )
        snapshot = await reader.freeze(meta)
        set_stage(
            "reviewing",
            reviewed_head_sha=meta.head_sha,
            base_sha=meta.base_sha,
            merge_base_sha=snapshot.merge_base_sha,
        )
        provider = ReadOnlySnapshotProvider(reader, snapshot)
        engine = _build_engine(provider, job.id)
        extra_rules: list[dict[str, str]] = []
        for rules_provider in custom_rules_providers:
            extra_rules.extend(await rules_provider(snapshot))

        token = call_observer.set(calls.append)
        # The registry judge reads this, so its calls are this job's work too.
        judge_token = job_llm.set(engine.llm)
        try:
            result = await engine.review_snapshot(
                snapshot.pr_info(),
                snapshot.diff_text,
                team_conventions=_team_conventions(job.owner, job.repo),
                extra_custom_rules=extra_rules or None,
            )
        finally:
            job_llm.reset(judge_token)
            call_observer.reset(token)

        if provider.write_attempts:
            raise JobFailure(
                "write_blocked", f"Blocked GitHub writes: {', '.join(provider.write_attempts)}"
            )
        failure = _failed_call(calls)
        if failure is not None:
            raise failure
        set_stage("persisting")
        # Advisory only, and appended here so a PR with no reviewable file
        # still gets them. A GitHub-posting review gets them as one general PR
        # comment instead (registry.review_gate.finish).
        result.comments.extend(advisory_findings(snapshot.pr_info()))
        findings = [_finding(i, c) for i, c in enumerate(result.comments)]
        return JobOutcome(
            state="succeeded",
            result={
                "summary": result.summary,
                "walkthrough": result.walkthrough.summary if result.walkthrough else None,
                "findings": findings,
                "key_issues": [
                    {"issue": k.issue, "path": k.path, "line": k.line} for k in result.key_issues
                ],
                "coverage": _coverage(snapshot, result),
            },
            model_calls=calls,
        )
    except JobFailure as exc:
        return JobOutcome("failed", exc.code, exc.message, model_calls=calls)
    except SnapshotError as exc:
        return JobOutcome("failed", exc.code, str(exc), model_calls=calls)
    except ReadOnlyViolation as exc:
        return JobOutcome("failed", "write_blocked", str(exc), model_calls=calls)
    except LLMError as exc:
        failure = _failed_call(calls)
        code = _CODE_ERRORS.get(exc.code) or (failure.code if failure else "model_call_failed")
        return JobOutcome("failed", code, exc.safe_message, model_calls=calls)
    except MiraError as exc:
        failure = _failed_call(calls)
        if failure is not None:
            return JobOutcome("failed", failure.code, failure.message, model_calls=calls)
        return JobOutcome("failed", "review_failed", exc.safe_message, model_calls=calls)
