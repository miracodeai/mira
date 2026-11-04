"""Review-without-post API routes (``/api/reviews``).

Bearer-token callers (the goose CLI, the Kory Code relay) submit and poll
their own jobs, and answer the job's ``pending_work`` on their own seats.
Logged-in dashboard users can read any job and open its report. Nothing here
writes to GitHub. Contract: ``docs/review-api.md``.
"""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Annotated, Any, Literal

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from mira.dashboard.api import router
from mira.review_api import service
from mira.review_api.snapshot import SnapshotError
from mira.review_api.store import ReviewJob, ReviewJobStore
from mira.review_api.tokens import ReviewPrincipal

logger = logging.getLogger(__name__)

_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


class SubmitReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: Annotated[StrictStr, Field(pattern=_REPO.pattern)]
    pr_number: Annotated[StrictInt, Field(gt=0)]
    head_sha: Annotated[StrictStr, Field(pattern=_SHA.pattern)]


class WorkAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[StrictStr, Field(min_length=1, pattern=r"\S")]
    actual_model: Annotated[StrictStr, Field(min_length=1, max_length=200, pattern=r"\S")]


class WorkFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: Literal["seat_exhausted", "workstation_outage", "model_mismatch", "timeout"]
    actual_model: Annotated[StrictStr, Field(min_length=1, max_length=200)] = "unknown"


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message, **extra}}
    )


def _principal(request: Request) -> ReviewPrincipal | None:
    return getattr(request.state, "review_principal", None)


def _base_url(request: Request, svc: service.ReviewService) -> str:
    return svc.settings.public_url or str(request.base_url).rstrip("/")


def _models(calls: list[dict[str, Any]]) -> dict[str, list[str]]:
    requested = sorted({str(c.get("requested_model") or "") for c in calls} - {""})
    actual = sorted({str(c.get("actual_model") or "") for c in calls} - {"", "unknown"})
    return {"requested": requested, "actual": actual}


def _pending_work(job: ReviewJob, store: ReviewJobStore | None) -> list[dict[str, Any]]:
    if store is None or job.state != "running":
        return []
    return [
        {
            "work_id": w.id,
            "prompt": w.prompt,
            "model": w.model,
            "timeout_seconds": w.timeout_seconds,
        }
        for w in store.pending_work(job.id)
    ]


def _job_view(job: ReviewJob, base: str, store: ReviewJobStore | None = None) -> dict[str, Any]:
    result = job.result if job.state == "succeeded" else None
    calls = job.model_calls
    return {
        "id": job.id,
        "state": job.state,
        "stage": job.stage,
        "repo": f"{job.owner}/{job.repo}",
        "pr_number": job.pr_number,
        "requested_head_sha": job.requested_head_sha,
        "reviewed_head_sha": job.reviewed_head_sha or None,
        "base_sha": job.base_sha or None,
        "merge_base_sha": job.merge_base_sha or None,
        "principal": job.principal,
        "created_at": job.created_at,
        "started_at": job.started_at or None,
        "updated_at": job.updated_at,
        "finished_at": job.finished_at or None,
        "deadline_at": job.deadline_at or None,
        # null until the review succeeded; [] means a clean review.
        "findings": result["findings"] if result else None,
        "key_issues": result["key_issues"] if result else None,
        "summary": result["summary"] if result else None,
        "walkthrough": result.get("walkthrough") if result else None,
        "coverage": result["coverage"] if result else None,
        "models": _models(calls),
        "model_calls": calls,
        "error": {"code": job.error_code, "message": job.error_message} if job.error_code else None,
        # Model calls waiting for the submitting client to run them.
        "pending_work": _pending_work(job, store),
        "status_url": f"{base}/api/reviews/{job.id}",
        "report_url": f"{base}/api/reviews/{job.id}/report",
    }


def _visible(request: Request, job: ReviewJob) -> bool:
    principal = _principal(request)
    if principal is not None:
        return job.principal == principal.name and principal.allows(job.owner, job.repo)
    return getattr(request.state, "user", None) is not None


@router.post("/api/reviews")
async def submit_review(request: Request) -> JSONResponse:
    principal = _principal(request)
    if principal is None:
        return _error(403, "token_required", "Submit reviews with a review API bearer token.")
    try:
        body = SubmitReview.model_validate(await request.json())
    except (ValidationError, ValueError) as exc:
        detail = (
            [{"field": ".".join(map(str, e["loc"])), "problem": e["msg"]} for e in exc.errors()]
            if isinstance(exc, ValidationError)
            else []
        )
        return _error(
            422,
            "invalid_request",
            "Body must be exactly {repo, pr_number, head_sha} with a full 40-character "
            "lowercase SHA.",
            fields=detail,
        )
    owner, repo = body.repo.split("/", 1)
    if not principal.allows(owner, repo):
        return _error(403, "repo_not_allowed", f"This token cannot review {body.repo}.")

    svc = service.current()
    if svc is None or svc.executor is None or svc.reader_factory is None:
        return _error(
            503,
            "review_execution_unavailable",
            "Review execution is not enabled on this deployment.",
        )

    existing = svc.store.find_active(principal.name, owner, repo, body.pr_number, body.head_sha)
    if existing is not None:
        view = _job_view(existing, _base_url(request, svc), svc.store)
        return JSONResponse(status_code=202, content={**view, "deduplicated": True})

    try:
        reader = await svc.reader_factory(owner, repo)
        meta = await reader.get_pull(body.pr_number)
    except SnapshotError as exc:
        status = {"repo_not_installed": 404, "pr_not_found": 404}.get(exc.code, 502)
        return _error(status, exc.code, str(exc))
    except Exception as exc:
        logger.warning("review API: GitHub lookup for %s failed: %s", body.repo, exc)
        return _error(502, "github_unavailable", "GitHub could not be reached.")
    if meta.state != "open":
        return _error(409, "pr_not_open", f"PR is {meta.state or 'not open'}.")
    if meta.head_sha != body.head_sha:
        return _error(
            409,
            "head_sha_mismatch",
            "The PR head is not the requested SHA. Push or fetch, then submit the current head.",
            current_head_sha=meta.head_sha,
        )

    job = svc.store.create(
        principal=principal.name,
        owner=owner,
        repo=repo,
        pr_number=body.pr_number,
        head_sha=body.head_sha,
        base_sha=meta.base_sha,
        max_queued=svc.settings.max_queued,
    )
    if job is None:
        return _error(429, "review_queue_full", "Too many reviews are queued. Retry later.")
    svc.executor.wake()
    logger.info(
        "review API: %s queued %s/%s#%d@%s as %s",
        principal.name,
        owner,
        repo,
        body.pr_number,
        body.head_sha[:12],
        job.id,
    )
    return JSONResponse(status_code=202, content=_job_view(job, _base_url(request, svc), svc.store))


def _load(request: Request, job_id: str) -> tuple[service.ReviewService, ReviewJob] | JSONResponse:
    svc = service.current()
    if svc is None:
        return _error(503, "review_execution_unavailable", "The review API is not running.")
    job = svc.store.get(job_id)
    # A job another principal owns is reported as missing, not forbidden.
    if job is None or not _visible(request, job):
        return _error(404, "review_not_found", "No such review.")
    return svc, job


@router.get("/api/reviews/{job_id}")
def get_review(job_id: str, request: Request) -> JSONResponse:
    loaded = _load(request, job_id)
    if isinstance(loaded, JSONResponse):
        return loaded
    svc, job = loaded
    return JSONResponse(content=_job_view(job, _base_url(request, svc), svc.store))


@router.post("/api/reviews/{job_id}/work/{work_id}")
async def answer_work(job_id: str, work_id: str, request: Request) -> JSONResponse:
    """The submitting client posts one model completion, or why it has none."""
    loaded = _load(request, job_id)
    if isinstance(loaded, JSONResponse):
        return loaded
    svc, job = loaded
    if _principal(request) is None:
        return _error(403, "token_required", "Answer work with the submitting review token.")
    work = svc.store.get_work(work_id)
    if work is None or work.job_id != job.id:
        return _error(404, "work_not_found", "This review has no such work item.")
    try:
        raw = await request.json()
        answer: WorkAnswer | WorkFailure = (
            WorkFailure.model_validate(raw)
            if isinstance(raw, dict) and "error" in raw
            else WorkAnswer.model_validate(raw)
        )
    except (ValidationError, ValueError) as exc:
        detail = (
            [{"field": ".".join(map(str, e["loc"])), "problem": e["msg"]} for e in exc.errors()]
            if isinstance(exc, ValidationError)
            else []
        )
        return _error(
            422,
            "invalid_request",
            "Body must be exactly {text, actual_model} or {error, actual_model?}.",
            fields=detail,
        )
    if isinstance(answer, WorkFailure):
        accepted = svc.store.answer_work(
            job.id, work_id, actual_model=answer.actual_model, error_code=answer.error
        )
        content: dict[str, Any] = {"work_id": work_id, "state": "failed", "error": answer.error}
    else:
        accepted = svc.store.answer_work(
            job.id, work_id, text=answer.text, actual_model=answer.actual_model
        )
        content = {"work_id": work_id, "state": "answered"}
    if not accepted:
        return _error(409, "work_not_pending", "This work item is no longer waiting for an answer.")
    return JSONResponse(content=content)


_SEVERITY_ORDER = {"blocker": 0, "warning": 1, "suggestion": 2, "nitpick": 3}


@router.get("/api/reviews/{job_id}/report", response_class=HTMLResponse)
def review_report(job_id: str, request: Request) -> HTMLResponse:
    loaded = _load(request, job_id)
    if isinstance(loaded, JSONResponse):
        return loaded  # type: ignore[return-value]
    svc, job = loaded
    view = _job_view(job, _base_url(request, svc))
    e = html.escape
    rows: list[str] = []
    for f in sorted(view["findings"] or [], key=lambda f: _SEVERITY_ORDER.get(f["severity"], 9)):
        # An empty path is a finding on the PR as a whole, such as its description.
        where = f"<code>{e(f['path'])}:{f['line']}</code>" if f["path"] else "<i>pull request</i>"
        rows.append(
            f"<li><b>[{e(f['severity'])}] {e(f['title'])}</b> {where}<p>{e(f['body'])}</p></li>"
        )
    if view["findings"] is None:
        findings_html = "<p><i>No findings: the review has not succeeded.</i></p>"
    elif not rows:
        findings_html = "<p>The review finished with no findings.</p>"
    else:
        findings_html = "<ol>" + "".join(rows) + "</ol>"
    error = view["error"]
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Review {e(job.id)}</title></head><body>
<h1>{e(view["repo"])}#{job.pr_number} review</h1>
<p>State: <b>{e(job.state)}</b> (stage {e(job.stage)})</p>
<p>Requested head <code>{e(job.requested_head_sha)}</code>;
reviewed head <code>{e(job.reviewed_head_sha or "-")}</code>;
base <code>{e(job.base_sha or "-")}</code></p>
<p>Models requested: {e(", ".join(view["models"]["requested"]) or "-")};
actual: {e(", ".join(view["models"]["actual"]) or "-")}</p>
{f"<p>Error <code>{e(error['code'])}</code>: {e(error['message'])}</p>" if error else ""}
<h2>Summary</h2><p>{e(view["summary"] or "")}</p>
<h2>Findings</h2>{findings_html}
<h2>Coverage</h2><pre>{e(json.dumps(view["coverage"], indent=2))}</pre>
<p>Nothing was posted to GitHub.</p>
</body></html>"""
    return HTMLResponse(page)
