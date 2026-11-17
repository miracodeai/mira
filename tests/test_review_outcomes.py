"""Review delivery acknowledgements are independent of comment-ID lookup."""

import asyncio
import json
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from github import GithubException

from mira.config import MiraConfig
from mira.core.engine import ReviewEngine
from mira.exceptions import ProviderError
from mira.index.store import IndexStore
from mira.models import PRInfo, ReviewComment, ReviewResult, Severity
from mira.providers.bitbucket import BitbucketProvider
from mira.providers.forgejo import ForgejoProvider
from mira.providers.github import GitHubProvider
from mira.providers.gitlab import GitLabProvider
from mira.review_outcome import ReviewOutcome


def _result() -> ReviewResult:
    return ReviewResult(
        comments=[
            ReviewComment("app.py", 1, None, Severity.WARNING, "bug", "Finding", "Details", 1.0)
        ]
    )


@pytest.mark.asyncio
async def test_successful_batch_is_delivered_even_when_comment_ids_cannot_be_read(monkeypatch):
    provider = GitHubProvider("synthetic-token")
    client = MagicMock()
    pr = client.get_repo.return_value.get_pull.return_value
    pr.get_commits.return_value = [SimpleNamespace(sha="a" * 40)]
    pr.create_review.return_value.get_comments.side_effect = RuntimeError("ID lookup failed")
    monkeypatch.setattr(provider, "_make_client", lambda _token: client)
    info = PRInfo(
        "Test", "", "main", "change", "https://github.com/acme/app/pull/1", 1, "acme", "app"
    )
    result = _result()

    ids = await provider.post_review(info, result)

    assert ids == [0]
    receipt = getattr(result, "delivery", None)
    assert receipt is not None, "A successful submission needs a receipt even without comment IDs"
    assert receipt.delivered == 1
    assert receipt.unknown == 0
    assert receipt.status == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("second_status", [422, 401])
async def test_individual_fallback_retains_partial_acknowledgements(monkeypatch, second_status):
    monkeypatch.setattr("time.sleep", lambda _: None)
    provider = GitHubProvider("synthetic-token")
    client = MagicMock()
    pr = client.get_repo.return_value.get_pull.return_value
    pr.get_commits.return_value = [SimpleNamespace(sha="a" * 40)]
    pr.create_review.side_effect = GithubException(422, {"message": "batch rejected"}, {})
    pr.create_review_comment.side_effect = [
        SimpleNamespace(id=101),
        GithubException(second_status, {"message": "rejected"}, {}),
    ]
    monkeypatch.setattr(provider, "_make_client", lambda _: client)
    info = PRInfo(
        "Test", "", "main", "change", "https://github.com/acme/app/pull/1", 1, "acme", "app"
    )
    result = _result()
    result.comments.append(
        ReviewComment("other.py", 2, None, Severity.WARNING, "bug", "Other", "Details", 1.0)
    )

    if second_status == 401:
        with pytest.raises(ProviderError):
            await provider.post_review(info, result)
    else:
        assert await provider.post_review(info, result) == [101, 0]

    assert result.delivery is not None
    assert result.delivery.delivered == 1
    assert result.delivery.failed == 1
    assert result.delivery.unknown == 0
    assert result.delivery.status == "partial"
    assert result.delivery.comment_ids == [101, 0]


def _engine(tmp_path, monkeypatch, post_review):
    """Script model generation and the hosting boundary; use real isolated stores."""
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    monkeypatch.setenv("ADMIN_PASSWORD", "synthetic-review-test")
    from mira.dashboard import api
    from mira.dashboard.db import AppDatabase

    app = AppDatabase(url="", admin_password="synthetic")
    app.register_repo("acme", "app")
    monkeypatch.setattr(api, "_app_db", app)
    provider = MagicMock()
    info = PRInfo(
        "Test",
        "",
        "main",
        "change",
        "https://github.com/acme/app/pull/1",
        1,
        "acme",
        "app",
        head_sha="a" * 40,
    )
    provider.get_pr_info = AsyncMock(return_value=info)
    provider.get_pr_diff = AsyncMock(
        return_value="--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,2 @@\n+first = 1\n+second = 2\n"
    )
    provider.get_all_bot_threads = AsyncMock(return_value=[])
    provider.post_comment = AsyncMock()
    provider.find_bot_comment = AsyncMock(return_value=None)
    provider.update_comment = AsyncMock()
    provider.start_check_run = AsyncMock(return_value=123)
    provider.finish_check_run = AsyncMock()
    provider.post_review = AsyncMock(side_effect=post_review)
    model = MagicMock()
    model.count_tokens.return_value = 100
    model.usage = {}
    model.review = AsyncMock(
        return_value=json.dumps(
            {
                "comments": [
                    {
                        "path": "app.py",
                        "line": 1,
                        "severity": "blocker",
                        "category": "security",
                        "title": "Authorization missing",
                        "body": "Validate authorization before writing.",
                        "confidence": 1.0,
                    },
                    {
                        "path": "app.py",
                        "line": 2,
                        "severity": "warning",
                        "category": "bug",
                        "title": "Cursor is unbounded",
                        "body": "Bound the cursor to the result window.",
                        "confidence": 1.0,
                    },
                ]
            }
        )
    )
    config = MiraConfig()
    config.review.overlap.enabled = False
    for field in [
        "agentic_tools",
        "security_pass",
        "dependency_overlap",
        "secrets_scan",
        "walkthrough",
        "include_summary",
        "self_critique",
        "auto_resolve_conversations",
        "code_context",
    ]:
        setattr(config.review, field, False)
    return ReviewEngine(config, model, provider), provider, model


@pytest.mark.asyncio
async def test_posting_exception_persists_acknowledged_partial_outcome(tmp_path, monkeypatch):
    async def post(_info, result, **_kwargs):
        receipt = result.begin_delivery()
        receipt.begin(0, 1)
        receipt.acknowledge(0)
        receipt.set_comment_id(0, 101)
        receipt.reject(1)
        raise ProviderError("Synthetic posting rejection")

    engine, provider, _model = _engine(tmp_path, monkeypatch, post)

    with pytest.raises(ProviderError):
        await engine.review_pr("https://github.com/acme/app/pull/1")

    with closing(IndexStore.open("acme", "app")) as store:
        events = store.list_review_events_for_pr(1)
        assert len(events) == 1, "A delivery failure must not discard the computed review event"
        outcome = json.loads(events[0].outcome_json)
        assert (
            outcome["drafted"],
            outcome["retained"],
            outcome["delivered"],
            outcome["failed"],
        ) == (2, 2, 1, 1)
        assert outcome["status"] == "partial"
        assert outcome["delivery_error"] is True
        assert store.list_review_comments(1)[0].github_comment_id == 101
    assert provider.finish_check_run.call_args.args[2] != "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", [GitLabProvider, ForgejoProvider, BitbucketProvider])
@pytest.mark.parametrize("scenario", ["success", "partial", "failed", "ambiguous"])
async def test_http_acknowledgements_drive_other_provider_receipts(
    monkeypatch, provider_type, scenario
):
    from urllib.parse import parse_qs

    def respond(request):
        if request.method == "GET":
            return httpx.Response(
                200, json={"diff_refs": {"base_sha": "b", "head_sha": "h", "start_sha": "s"}}
            )
        if request.url.path.endswith("/reviews"):
            return httpx.Response(201 if scenario == "success" else 422, json={})
        if request.headers.get("content-type", "").startswith("application/json"):
            payload = json.loads(request.content)
            body = payload.get("body") or payload.get("content", {}).get("raw", "")
        else:
            body = parse_qs(request.content.decode()).get("body", [""])[0]
        first = "Other" not in body
        if scenario == "success" or (scenario in {"partial", "ambiguous"} and first):
            return httpx.Response(201, json={"id": 101})
        if scenario == "ambiguous":
            raise httpx.ReadTimeout("Synthetic unknown write outcome", request=request)
        return httpx.Response(403, json={"message": "Synthetic rejection"})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs),
    )
    provider = provider_type("synthetic-token")
    info = PRInfo("Test", "", "main", "change", "https://example.invalid/pull/1", 1, "acme", "app")
    result = _result()
    result.comments.append(
        ReviewComment("other.py", 2, None, Severity.WARNING, "bug", "Other", "Details", 1.0)
    )

    if scenario == "ambiguous":
        with pytest.raises((ProviderError, httpx.HTTPError)):
            await provider.post_review(info, result)
    else:
        await provider.post_review(info, result)

    assert result.delivery is not None
    expected = {
        "success": (2, 0, 0, "complete"),
        "partial": (1, 1, 0, "partial"),
        "failed": (0, 2, 0, "failed"),
        "ambiguous": (1, 0, 1, "partial"),
    }
    assert (
        result.delivery.delivered,
        result.delivery.failed,
        result.delivery.unknown,
        result.delivery.status,
    ) == expected[scenario]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["empty", "filtered", "complete", "legacy", "dry-run", "rejected", "ambiguous"]
)
async def test_review_events_distinguish_computation_and_delivery(tmp_path, monkeypatch, scenario):
    async def post(_info, result, **_kwargs):
        if scenario == "legacy":
            return [101, 102]
        receipt = result.begin_delivery()
        receipt.begin(*range(len(result.comments)))
        if scenario == "rejected":
            receipt.reject(*range(len(result.comments)))
        elif scenario == "ambiguous":
            raise ProviderError("Synthetic unknown transport outcome")
        else:
            receipt.acknowledge(*range(len(result.comments)))
        return [0] * len(result.comments)

    engine, provider, model = _engine(tmp_path, monkeypatch, post)
    if scenario == "empty":
        model.review.return_value = '{"comments":[]}'
    elif scenario == "filtered":
        data = json.loads(model.review.return_value)
        for comment in data["comments"]:
            comment["confidence"] = 0.01
        model.review.return_value = json.dumps(data)
    elif scenario == "dry-run":
        engine.dry_run = True

    if scenario == "ambiguous":
        with pytest.raises(ProviderError):
            await engine.review_pr("https://github.com/acme/app/pull/1")
    else:
        await engine.review_pr("https://github.com/acme/app/pull/1")

    with closing(IndexStore.open("acme", "app")) as store:
        row = store.list_review_events_for_pr(1)[0]
    outcome = ReviewOutcome.from_json(row.outcome_json)
    assert outcome is not None
    expected = {
        "empty": (0, 0, 0, 0, 0, "complete"),
        "filtered": (2, 0, 0, 0, 0, "complete"),
        "complete": (2, 2, 2, 0, 0, "complete"),
        "legacy": (2, 2, 0, 0, 2, "unknown"),
        "dry-run": (2, 2, 0, 0, 0, "not_attempted"),
        "rejected": (2, 2, 0, 2, 0, "failed"),
        "ambiguous": (2, 2, 0, 0, 2, "unknown"),
    }
    assert (
        outcome.drafted,
        outcome.retained,
        outcome.delivered,
        outcome.failed,
        outcome.unknown,
        outcome.status,
    ) == expected[scenario]
    assert outcome.coverage == "complete"
    assert row.comments_posted == outcome.retained
    if scenario in {"empty", "filtered", "dry-run"}:
        provider.post_review.assert_not_awaited()
    if scenario in {"legacy", "rejected", "ambiguous"}:
        assert provider.finish_check_run.call_args.args[2] != "success"


@pytest.mark.asyncio
async def test_incomplete_computation_is_not_clean_even_with_no_delivery_needed(
    tmp_path, monkeypatch
):
    engine, provider, model = _engine(tmp_path, monkeypatch, lambda *_a, **_kw: [])
    model.review.side_effect = ProviderError("Synthetic model failure")

    result = await engine.review_pr("https://github.com/acme/app/pull/1")

    assert result.outcome is not None
    assert result.outcome.coverage == "failed"
    assert result.outcome.retained == 0
    provider.post_review.assert_not_awaited()
    assert provider.finish_check_run.call_args.args[2] == "failure"


@pytest.mark.asyncio
async def test_retry_rejection_does_not_erase_an_earlier_unknown_write(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    provider = GitHubProvider("synthetic-token")
    client = MagicMock()
    pr = client.get_repo.return_value.get_pull.return_value
    pr.get_commits.return_value = [SimpleNamespace(sha="a" * 40)]
    pr.create_review.side_effect = [GithubException(503, {}, {}), GithubException(422, {}, {})]
    pr.create_review_comment.side_effect = GithubException(422, {}, {})
    monkeypatch.setattr(provider, "_make_client", lambda _: client)
    result = _result()
    info = PRInfo(
        "Test", "", "main", "change", "https://github.com/acme/app/pull/1", 1, "acme", "app"
    )

    await provider.post_review(info, result)

    assert result.delivery is not None
    assert (result.delivery.delivered, result.delivery.failed, result.delivery.unknown) == (0, 0, 1)
    assert result.delivery.status == "unknown"


@pytest.mark.parametrize(
    "raw",
    ["", "{", "[]", '{"version":1}', '{"version":true}', "[" * 10000],
    ids=["legacy", "invalid", "array", "missing-fields", "invalid-version", "too-deep"],
)
def test_unusable_historical_outcomes_do_not_invent_delivery_proof(raw):
    assert ReviewOutcome.from_json(raw) is None


@pytest.mark.asyncio
async def test_cancellation_preserves_partial_delivery_without_swallowing_cancellation(
    tmp_path, monkeypatch
):
    async def post(_info, result, **_kwargs):
        receipt = result.begin_delivery()
        receipt.begin(0, 1)
        receipt.acknowledge(0)
        raise asyncio.CancelledError()

    engine, _provider, _model = _engine(tmp_path, monkeypatch, post)
    with pytest.raises(asyncio.CancelledError):
        await engine.review_pr("https://github.com/acme/app/pull/1")

    with closing(IndexStore.open("acme", "app")) as store:
        events = store.list_review_events_for_pr(1)
        assert len(events) == 1
        outcome = ReviewOutcome.from_json(events[0].outcome_json)
        assert outcome is not None
        assert (outcome.delivered, outcome.unknown, outcome.status) == (1, 1, "partial")


@pytest.mark.asyncio
async def test_individual_acknowledgement_survives_lazy_id_lookup_failure(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    provider = GitHubProvider("synthetic-token")
    client = MagicMock()
    pr = client.get_repo.return_value.get_pull.return_value
    pr.get_commits.return_value = [SimpleNamespace(sha="a" * 40)]
    pr.create_review.side_effect = GithubException(422, {}, {})

    class UnreadableID:
        @property
        def id(self):
            raise RuntimeError("Synthetic lazy ID lookup failure")

    pr.create_review_comment.side_effect = [UnreadableID(), SimpleNamespace(id=102)]
    monkeypatch.setattr(provider, "_make_client", lambda _: client)
    result = _result()
    result.comments.append(
        ReviewComment("other.py", 2, None, Severity.WARNING, "bug", "Other", "Details", 1.0)
    )
    info = PRInfo(
        "Test", "", "main", "change", "https://github.com/acme/app/pull/1", 1, "acme", "app"
    )
    assert await provider.post_review(info, result) == [0, 102]
    assert result.delivery is not None
    assert (result.delivery.delivered, result.delivery.unknown) == (2, 0)
    assert pr.create_review_comment.call_count == 2


@pytest.mark.asyncio
async def test_cancelled_worker_pending_writes_are_not_recorded_as_definitely_failed(
    tmp_path, monkeypatch
):
    import threading

    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    native = GitHubProvider("synthetic-token")
    client = MagicMock()
    pr = client.get_repo.return_value.get_pull.return_value

    def wait_before_submission(*_args, **_kwargs):
        started.set()
        assert release.wait(5)
        finished.set()
        return [SimpleNamespace(sha="a" * 40)]

    pr.get_commits.side_effect = wait_before_submission
    pr.create_review.return_value.get_comments.return_value = []
    monkeypatch.setattr(native, "_make_client", lambda _: client)
    engine, _provider, _model = _engine(tmp_path, monkeypatch, native.post_review)
    task = asyncio.create_task(engine.review_pr("https://github.com/acme/app/pull/1"))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with closing(IndexStore.open("acme", "app")) as store:
            event = store.list_review_events_for_pr(1)[0]
        outcome = ReviewOutcome.from_json(event.outcome_json)
        assert outcome is not None
        assert (outcome.delivered, outcome.failed, outcome.unknown) == (0, 0, 2)
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
