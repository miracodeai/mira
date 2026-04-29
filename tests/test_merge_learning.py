"""Tests for merge-time learning: bot-metadata parsing, accept/reject synthesis,
LLM-powered human-review synthesis, and webhook routing of merged PRs."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from mira.analysis.feedback import synthesize_from_human_reviews, synthesize_rules
from mira.index.store import IndexStore
from mira.models import BotThreadRecord, HumanReviewComment
from mira.providers.github import parse_bot_comment_metadata


@pytest.fixture
def store(tmp_path):
    s = IndexStore(str(tmp_path / "t.db"))
    yield s
    s.close()


# ── parse_bot_comment_metadata ──


class TestParseBotCommentMetadata:
    def test_parses_category_severity_title(self):
        body = (
            "🐛 **Bug**\n"
            "🛑 Blocker — must fix before merge\n"
            "\n"
            "**Null pointer on empty input**\n"
            "\n"
            "The function will crash if `items` is empty.\n"
        )
        meta = parse_bot_comment_metadata(body)
        assert meta["category"] == "bug"
        assert meta["severity"] == "blocker"
        assert meta["title"] == "Null pointer on empty input"

    def test_warning_severity(self):
        body = "🔒 **Security issue**\n⚠️ Warning\n\n**Raw SQL query**\n\nbody"
        meta = parse_bot_comment_metadata(body)
        assert meta["category"] == "security"
        assert meta["severity"] == "warning"
        assert meta["title"] == "Raw SQL query"

    def test_missing_severity_still_extracts_category_and_title(self):
        body = "⚡ **Performance**\n\n**Slow loop**\n\nbody"
        meta = parse_bot_comment_metadata(body)
        assert meta["category"] == "performance"
        assert meta["severity"] == ""
        assert meta["title"] == "Slow loop"

    def test_malformed_body_returns_blanks(self):
        meta = parse_bot_comment_metadata("not a structured comment at all")
        assert meta["category"] == ""
        assert meta["severity"] == ""
        assert meta["title"] == ""

    def test_empty_body_returns_blanks(self):
        meta = parse_bot_comment_metadata("")
        assert meta == {"category": "", "severity": "", "title": ""}


# ── record_bulk_feedback ──


class TestBulkFeedback:
    def test_inserts_multiple_events(self, store):
        events = [
            {
                "pr_number": 1,
                "pr_url": "https://x/pr/1",
                "comment_path": "a.py",
                "comment_line": 10,
                "comment_category": "bug",
                "comment_severity": "warning",
                "comment_title": "t",
                "signal": "accepted",
                "actor": "u1",
            },
            {
                "pr_number": 1,
                "pr_url": "https://x/pr/1",
                "comment_path": "b.py",
                "comment_line": 5,
                "comment_category": "security",
                "comment_severity": "blocker",
                "comment_title": "t2",
                "signal": "accepted",
                "actor": "u1",
            },
        ]
        n = store.record_bulk_feedback(events)
        assert n == 2
        listed = store.list_feedback()
        assert len(listed) == 2
        assert {e.comment_path for e in listed} == {"a.py", "b.py"}

    def test_empty_list_returns_zero(self, store):
        assert store.record_bulk_feedback([]) == 0


# ── synthesize_rules (accept/reject logic) ──


def _fb(
    store: IndexStore,
    *,
    signal: str,
    category: str,
    path: str = "src/auth.py",
    pr: int = 1,
) -> None:
    store.record_feedback(
        pr_number=pr,
        pr_url=f"https://x/pr/{pr}",
        comment_path=path,
        comment_line=1,
        comment_category=category,
        comment_severity="",
        comment_title="",
        signal=signal,
        actor="tester",
    )


class TestSynthesizeRules:
    def test_no_events_no_rules(self, store):
        assert synthesize_rules(store) == 0

    def test_category_wide_reject_rule(self, store):
        for i in range(5):
            _fb(store, signal="rejected", category="style", pr=i)
        n = synthesize_rules(store)
        assert n >= 1
        rules = store.list_active_learned_rules()
        assert any(r.source_signal == "reject_pattern" for r in rules)

    def test_positive_rule_on_high_accept_rate(self, store):
        # 5 accepted, 0 rejected → 100% acceptance in a category
        for i in range(5):
            _fb(store, signal="accepted", category="bug", pr=i)
        n = synthesize_rules(store)
        assert n >= 1
        rules = store.list_active_learned_rules()
        assert any(r.source_signal == "accept_pattern" and r.category == "bug" for r in rules)

    def test_low_accept_rate_no_positive_rule(self, store):
        # 3 accepted + 3 rejected = 50% accept rate → no positive rule
        for i in range(3):
            _fb(store, signal="accepted", category="clarity", pr=i)
        for i in range(3, 6):
            _fb(store, signal="rejected", category="clarity", pr=i)
        synthesize_rules(store)
        rules = store.list_active_learned_rules()
        assert not any(r.source_signal == "accept_pattern" for r in rules)

    def test_reject_pattern_suppresses_accept_rule(self, store):
        # 5 rejects triggers a category-wide reject rule; accepts should be
        # suppressed to avoid mixed signals.
        for i in range(5):
            _fb(store, signal="rejected", category="style", pr=i)
        for i in range(5, 15):
            _fb(store, signal="accepted", category="style", pr=i)
        synthesize_rules(store)
        rules = store.list_active_learned_rules()
        # Only the reject_pattern rule should exist for this category.
        style_rules = [r for r in rules if r.category == "style"]
        assert all(r.source_signal == "reject_pattern" for r in style_rules)

    def test_ignores_unknown_category(self, store):
        for i in range(5):
            _fb(store, signal="rejected", category="", pr=i)
        assert synthesize_rules(store) == 0


# ── synthesize_from_human_reviews (LLM) ──


class TestSynthesizeFromHumanReviews:
    @pytest.mark.asyncio
    async def test_returns_zero_when_too_few_comments(self, store):
        llm = AsyncMock()
        _fb(store, signal="human_review", category="human_review")
        n = await synthesize_from_human_reviews(store, llm)
        assert n == 0
        llm.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_calls_llm_and_stores_rules(self, store):
        # Seed with 3 human-review events
        for i in range(3):
            store.record_feedback(
                pr_number=i,
                pr_url=f"https://x/{i}",
                comment_path=f"src/f{i}.py",
                comment_line=10,
                comment_category="human_review",
                comment_severity="",
                comment_title=f"Reviewer said please no raw sql #{i}",
                signal="human_review",
                actor="alice",
            )

        llm = AsyncMock()
        llm.complete.return_value = json.dumps(
            {
                "rules": [
                    {
                        "rule": "Flag raw SQL queries outside the data layer.",
                        "rationale": "Reviewers consistently pushed back on them.",
                        "evidence_count": 3,
                    },
                    {
                        "rule": "Prefer async/await over callback patterns.",
                        "rationale": "Team style.",
                        "evidence_count": 2,
                    },
                ]
            }
        )

        n = await synthesize_from_human_reviews(store, llm)
        assert n == 2
        llm.complete.assert_called_once()
        # Verify stored as human_pattern source
        rules = store.list_active_learned_rules()
        human_rules = [r for r in rules if r.source_signal == "human_pattern"]
        assert len(human_rules) == 2

    @pytest.mark.asyncio
    async def test_bad_json_returns_zero(self, store):
        for i in range(3):
            store.record_feedback(
                pr_number=i,
                pr_url=f"https://x/{i}",
                comment_path="a.py",
                comment_line=1,
                comment_category="human_review",
                comment_severity="",
                comment_title="b",
                signal="human_review",
                actor="u",
            )
        llm = AsyncMock()
        llm.complete.return_value = "this is not json"
        assert await synthesize_from_human_reviews(store, llm) == 0

    @pytest.mark.asyncio
    async def test_llm_failure_returns_zero(self, store):
        for i in range(3):
            store.record_feedback(
                pr_number=i,
                pr_url=f"https://x/{i}",
                comment_path="a.py",
                comment_line=1,
                comment_category="human_review",
                comment_severity="",
                comment_title="b",
                signal="human_review",
                actor="u",
            )
        llm = AsyncMock()
        llm.complete.side_effect = RuntimeError("api down")
        assert await synthesize_from_human_reviews(store, llm) == 0


# ── Webhook routing ──


def test_webhook_routes_merged_pr(monkeypatch):
    from fastapi.testclient import TestClient

    from mira.github_app import webhooks as wh

    called_with: dict = {}

    async def fake_handler(payload, app_auth, bot_name):
        called_with["payload"] = payload
        called_with["bot_name"] = bot_name

    monkeypatch.setattr(wh, "handle_pr_merged", fake_handler)

    app_auth = object()
    app = wh.create_app(app_auth, webhook_secret="secret", bot_name="mira")

    payload = {
        "action": "closed",
        "installation": {"id": 42},
        "sender": {"login": "someone"},
        "pull_request": {
            "number": 7,
            "merged": True,
            "title": "Fix it",
            "body": "",
            "base": {"ref": "main"},
            "head": {"ref": "f"},
            "labels": [],
        },
        "repository": {"owner": {"login": "acme"}, "name": "web"},
    }

    import hashlib
    import hmac

    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()

    client = TestClient(app)
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "pull_request",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "processing"}
    assert called_with["bot_name"] == "mira"


def test_webhook_ignores_closed_but_not_merged(monkeypatch):
    from fastapi.testclient import TestClient

    from mira.github_app import webhooks as wh

    called = []

    async def fake_handler(*args, **kwargs):
        called.append(True)

    monkeypatch.setattr(wh, "handle_pr_merged", fake_handler)

    app_auth = object()
    app = wh.create_app(app_auth, webhook_secret="secret", bot_name="mira")

    payload = {
        "action": "closed",
        "installation": {"id": 42},
        "sender": {"login": "someone"},
        "pull_request": {
            "number": 7,
            "merged": False,
            "title": "Abandoned",
            "body": "",
            "base": {"ref": "main"},
            "head": {"ref": "f"},
            "labels": [],
        },
        "repository": {"owner": {"login": "acme"}, "name": "web"},
    }

    import hashlib
    import hmac

    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()

    client = TestClient(app)
    resp = client.post(
        "/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "pull_request",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    assert called == []


# ── End-to-end handler test ──


class _StoreProxy:
    """Wraps an IndexStore so the handler's store.close() doesn't close the
    shared fixture store — the test still needs to read from it afterwards."""

    def __init__(self, inner: IndexStore) -> None:
        self._inner = inner

    def __getattr__(self, name):  # type: ignore[no-untyped-def]
        return getattr(self._inner, name)

    def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_handle_pr_merged_end_to_end(tmp_path, monkeypatch):
    """Exercise the whole handler: provider → parser → store → synthesis chain."""
    from mira.github_app import handlers

    store = IndexStore(str(tmp_path / "test.db"))

    # Seed a prior rejected event — the corresponding bot thread must NOT be
    # re-recorded as accepted on merge.
    store.record_feedback(
        pr_number=42,
        pr_url="https://github.com/acme/web/pull/42",
        comment_path="src/db.py",
        comment_line=20,
        comment_category="",
        comment_severity="",
        comment_title="",
        signal="rejected",
        actor="alice",
    )

    monkeypatch.setattr(handlers, "_open_store", lambda owner, repo: _StoreProxy(store))

    mock_provider = MagicMock()
    mock_provider.get_all_bot_threads = AsyncMock(
        return_value=[
            BotThreadRecord(
                thread_id="t1",
                path="src/auth.py",
                line=10,
                body=(
                    "🐛 **Bug**\n"
                    "🛑 Blocker — must fix before merge\n"
                    "\n"
                    "**Null handling missing**\n"
                    "\n"
                    "The function crashes on empty input.\n"
                ),
                is_resolved=False,
            ),
            # At the rejected location — should be skipped.
            BotThreadRecord(
                thread_id="t2",
                path="src/db.py",
                line=20,
                body="🔒 **Security issue**\n⚠️ Warning\n\n**Raw SQL**\n\nbody",
                is_resolved=True,
            ),
            BotThreadRecord(
                thread_id="t3",
                path="src/utils.py",
                line=5,
                body="⚡ **Performance**\n\n**Slow loop**\n\nbody",
                is_resolved=False,
            ),
            # Malformed body — should be skipped (no parseable category).
            BotThreadRecord(
                thread_id="t4",
                path="src/other.py",
                line=1,
                body="not a structured comment",
                is_resolved=False,
            ),
        ]
    )
    mock_provider.get_human_review_comments = AsyncMock(
        return_value=[
            HumanReviewComment(
                path="src/auth.py",
                line=10,
                body="Please avoid raw SQL here — use the query builder.",
                author="reviewer1",
            ),
            HumanReviewComment(
                path="src/api.py",
                line=5,
                body="Let's add proper error handling to this request path.",
                author="reviewer2",
            ),
            # Empty body — should be skipped.
            HumanReviewComment(path="x.py", line=1, body="   ", author="reviewer3"),
        ]
    )

    monkeypatch.setattr(handlers, "create_provider", lambda *a, **kw: mock_provider)

    app_auth = MagicMock()
    app_auth.get_installation_token = AsyncMock(return_value="test-token")

    fake_config = MagicMock()
    fake_config.llm = MagicMock()
    monkeypatch.setattr(handlers, "load_config", lambda: fake_config)

    # Bypass the dashboard DB lookup in llm_config_for.
    import mira.dashboard.models_config as mc

    monkeypatch.setattr(mc, "llm_config_for", lambda purpose, base: base)

    fake_llm = MagicMock()
    fake_llm.complete = AsyncMock(
        return_value=json.dumps(
            {
                "rules": [
                    {
                        "rule": "Avoid raw SQL queries outside the data-access layer.",
                        "rationale": "Reviewers consistently push back on them.",
                        "evidence_count": 2,
                    },
                ]
            }
        )
    )
    monkeypatch.setattr(handlers, "LLMProvider", lambda *a, **kw: fake_llm)

    payload = {
        "action": "closed",
        "installation": {"id": 123},
        "sender": {"login": "alice"},
        "pull_request": {
            "number": 42,
            "merged": True,
            "merged_by": {"login": "alice"},
            "title": "Add auth",
            "body": "",
            "base": {"ref": "main"},
            "head": {"ref": "f"},
            "labels": [],
        },
        "repository": {"owner": {"login": "acme"}, "name": "web"},
    }

    await handlers.handle_pr_merged(payload, app_auth, "mira")

    events = store.list_feedback(limit=100)
    by_signal: dict[str, list] = {"accepted": [], "human_review": [], "rejected": []}
    for e in events:
        by_signal.setdefault(e.signal, []).append(e)

    # Bot threads: t1 (accepted), t2 (skipped — prior reject), t3 (accepted),
    # t4 (skipped — no parseable metadata).
    assert len(by_signal["accepted"]) == 2
    paths = {e.comment_path for e in by_signal["accepted"]}
    assert paths == {"src/auth.py", "src/utils.py"}
    cats = {e.comment_category for e in by_signal["accepted"]}
    assert cats == {"bug", "performance"}

    # Titles parsed too.
    titles = {e.comment_title for e in by_signal["accepted"]}
    assert "Null handling missing" in titles
    assert "Slow loop" in titles

    # Two human-review events (third had empty body).
    assert len(by_signal["human_review"]) == 2
    human_paths = {e.comment_path for e in by_signal["human_review"]}
    assert human_paths == {"src/auth.py", "src/api.py"}

    # Original rejected event still present and not duplicated.
    assert len(by_signal["rejected"]) == 1

    # LLM was invoked exactly once for this merge.
    fake_llm.complete.assert_called_once()

    # One human_pattern rule stored.
    rules = store.list_active_learned_rules()
    human_rules = [r for r in rules if r.source_signal == "human_pattern"]
    assert len(human_rules) == 1
    assert "raw sql" in human_rules[0].rule_text.lower()

    # ── Dedup on retry ──
    fake_llm.complete.reset_mock()
    mock_provider.get_all_bot_threads.reset_mock()
    mock_provider.get_human_review_comments.reset_mock()

    await handlers.handle_pr_merged(payload, app_auth, "mira")

    # No new events should have been recorded.
    events_after = store.list_feedback(limit=100)
    assert len(events_after) == len(events)

    # Provider fetch methods should not have been called (early return).
    mock_provider.get_all_bot_threads.assert_not_called()
    mock_provider.get_human_review_comments.assert_not_called()
    fake_llm.complete.assert_not_called()

    store.close()
