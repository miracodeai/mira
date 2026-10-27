"""Tests for the Bitbucket webhook route: signature check and event dispatch."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from mira.config import FilterConfig, MiraConfig, ReviewConfig
from mira.platforms.bitbucket import state
from mira.platforms.bitbucket.auth import BitbucketTokenAuth
from mira.platforms.bitbucket.webhook import verify_bitbucket_signature
from mira.platforms.server import create_app

SECRET = "bb-secret"
BOT = "mira-bot"
BOT_USER = {"nickname": "mira-bot", "account_id": "557058:bot", "uuid": "{bot}"}
WEBHOOK = "mira.platforms.bitbucket.webhook"


@pytest.fixture
def bitbucket_auth():  # noqa: ANN201
    auth = BitbucketTokenAuth("tok")
    auth.get_bot_identity = AsyncMock(return_value="mira-bot")  # type: ignore[method-assign]
    auth.get_bot_ids = AsyncMock(  # type: ignore[method-assign]
        return_value={"mira-bot", "557058:bot", "{bot}"}
    )
    return auth


@pytest.fixture
def app(bitbucket_auth):  # noqa: ANN001, ANN201
    return create_app(
        app_auth=None,
        webhook_secret=None,
        bot_name=BOT,
        bitbucket_auth=bitbucket_auth,
        bitbucket_webhook_secret=SECRET,
    )


@pytest.fixture
async def client(app) -> AsyncClient:  # noqa: ANN001
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def pr_state():  # noqa: ANN201
    """In-memory stand-in for the app-db settings table."""
    store: dict[str, str] = {}

    class _DB:
        def get_setting(self, key):  # noqa: ANN001, ANN202
            return store.get(key)

        def set_setting(self, key, value):  # noqa: ANN001, ANN202
            store[key] = value

    with patch.object(state, "_db", lambda: _DB()):
        yield store


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _actor(nickname: str) -> dict:
    return {"nickname": nickname, "account_id": f"557058:{nickname}"}


def _pr_payload(actor: str, head: str = "aaa111", pr_state: str = "OPEN") -> dict:
    return {
        "actor": _actor(actor),
        "pullrequest": {
            "id": 7,
            "title": "T",
            "state": pr_state,
            "author": _actor(actor),
            "source": {"branch": {"name": "feature"}, "commit": {"hash": head}},
            "links": {"html": {"href": "https://bitbucket.org/acme/widgets/pull-requests/7"}},
        },
        "repository": {"full_name": "acme/widgets", "is_private": True},
    }


def _comment_payload(raw: str, actor: dict) -> dict:
    payload = _pr_payload("alice")
    payload["actor"] = actor
    payload["comment"] = {"id": 50, "content": {"raw": raw}, "user": actor}
    return payload


async def _post(client: AsyncClient, event: str, payload: dict, *, signature: str | None = None):
    body = json.dumps(payload).encode()
    return await client.post(
        "/bitbucket/webhook",
        content=body,
        headers={"X-Event-Key": event, "X-Hub-Signature": signature or _sign(body)},
    )


class TestSignature:
    def test_valid(self):
        assert verify_bitbucket_signature(_sign(b"{}"), b"{}", SECRET)

    def test_wrong_secret(self):
        assert not verify_bitbucket_signature(_sign(b"{}"), b"{}", "other")

    def test_empty_secret_never_verifies(self):
        sig = "sha256=" + hmac.new(b"", b"{}", hashlib.sha256).hexdigest()
        assert not verify_bitbucket_signature(sig, b"{}", "")

    @pytest.mark.asyncio
    async def test_route_rejects_bad_signature(self, client):
        resp = await _post(
            client, "pullrequest:created", _pr_payload("alice"), signature="sha256=x"
        )
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_pr_created_is_reviewed(client):
    with patch(f"{WEBHOOK}.handle_bitbucket_pr", new=AsyncMock()) as h:
        resp = await _post(client, "pullrequest:created", _pr_payload("alice"))
    assert resp.json()["status"] == "processing"
    h.assert_awaited_once()


@pytest.mark.asyncio
async def test_updated_without_new_commit_is_ignored(client):
    with patch(f"{WEBHOOK}.handle_bitbucket_pr", new=AsyncMock()) as h:
        await _post(client, "pullrequest:created", _pr_payload("alice", head="aaa111"))
        # A title/description edit fires `updated` with the same source commit.
        resp = await _post(client, "pullrequest:updated", _pr_payload("alice", head="aaa111"))
        assert resp.json()["status"] == "ignored"
        resp = await _post(client, "pullrequest:updated", _pr_payload("alice", head="bbb222"))
        assert resp.json()["status"] == "processing"
    assert h.await_count == 2


@pytest.mark.asyncio
async def test_updated_respects_review_on_synchronize(client):
    cfg = MiraConfig(review=ReviewConfig(review_on_synchronize=False))
    with (
        patch(f"{WEBHOOK}.handle_bitbucket_pr", new=AsyncMock()) as h,
        patch(f"{WEBHOOK}.load_config", return_value=cfg),
    ):
        resp = await _post(client, "pullrequest:updated", _pr_payload("alice", head="ccc333"))
    assert resp.json()["status"] == "ignored"
    h.assert_not_called()


@pytest.mark.asyncio
async def test_declined_pr_is_ignored(client):
    with patch(f"{WEBHOOK}.handle_bitbucket_pr", new=AsyncMock()) as h:
        resp = await _post(client, "pullrequest:updated", _pr_payload("alice", pr_state="DECLINED"))
    assert resp.json()["status"] == "ignored"
    h.assert_not_called()


@pytest.mark.asyncio
async def test_blocked_author_filtered(client):
    cfg = MiraConfig(filter=FilterConfig(blocked_authors=["renovate"]))
    with (
        patch(f"{WEBHOOK}.handle_bitbucket_pr", new=AsyncMock()) as h,
        patch(f"{WEBHOOK}.load_config", return_value=cfg),
    ):
        resp = await _post(client, "pullrequest:created", _pr_payload("renovate"))
    assert resp.json()["status"] == "ignored"
    h.assert_not_called()


@pytest.mark.asyncio
async def test_merge_triggers_learning(client):
    with patch(f"{WEBHOOK}.handle_bitbucket_merge", new=AsyncMock()) as h:
        resp = await _post(client, "pullrequest:fulfilled", _pr_payload("alice", pr_state="MERGED"))
    assert resp.json()["status"] == "processing"
    h.assert_awaited_once()


class TestComments:
    @pytest.mark.asyncio
    async def test_handle_mention(self, client):
        with patch(f"{WEBHOOK}.handle_bitbucket_comment", new=AsyncMock()) as h:
            resp = await _post(
                client,
                "pullrequest:comment_created",
                _comment_payload("@mira-bot review", _actor("alice")),
            )
        assert resp.json()["status"] == "processing"
        h.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_account_id_mention(self, client):
        """Bitbucket's editor stores autocompleted mentions as @{account_id}."""
        with patch(f"{WEBHOOK}.handle_bitbucket_comment", new=AsyncMock()) as h:
            resp = await _post(
                client,
                "pullrequest:comment_created",
                _comment_payload("@{557058:bot} explain this", _actor("alice")),
            )
        assert resp.json()["status"] == "processing"
        h.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_mention_ignored(self, client):
        with patch(f"{WEBHOOK}.handle_bitbucket_comment", new=AsyncMock()) as h:
            resp = await _post(
                client,
                "pullrequest:comment_created",
                _comment_payload("looks good", _actor("alice")),
            )
        assert resp.json()["status"] == "ignored"
        h.assert_not_called()

    @pytest.mark.asyncio
    async def test_bot_own_comment_ignored(self, client):
        """The bot's comments mention @mira-bot in help text; never loop on them."""
        with patch(f"{WEBHOOK}.handle_bitbucket_comment", new=AsyncMock()) as h:
            resp = await _post(
                client,
                "pullrequest:comment_created",
                _comment_payload("Reply `@mira-bot reject` to dismiss", BOT_USER),
            )
        assert resp.json()["status"] == "ignored"
        h.assert_not_called()


@pytest.mark.asyncio
async def test_push_dispatched(client):
    payload = {
        "actor": _actor("alice"),
        "repository": {"full_name": "acme/widgets"},
        "push": {"changes": []},
    }
    with patch(f"{WEBHOOK}.handle_bitbucket_push", new=AsyncMock()) as h:
        resp = await _post(client, "repo:push", payload)
    assert resp.json()["status"] == "processing"
    h.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_event_ignored(client):
    resp = await _post(client, "repo:fork", {"actor": _actor("alice")})
    assert resp.json()["status"] == "ignored"


@pytest.mark.asyncio
async def test_route_absent_without_credentials():
    app = create_app(app_auth=None, webhook_secret=None, bot_name=BOT)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/bitbucket/webhook", content=b"{}")
    assert resp.status_code in (404, 405)


@pytest.mark.asyncio
async def test_push_handler_indexes_changed_paths(pr_state):
    """repo:push carries no file lists; changed paths come from the diffstat API."""
    from mira.platforms.bitbucket.webhook import handle_bitbucket_push

    payload = {
        "repository": {"full_name": "acme/widgets", "mainbranch": {"name": "develop"}},
        "push": {
            "changes": [
                {
                    "new": {"type": "branch", "name": "develop", "target": {"hash": "new1"}},
                    "old": {"type": "branch", "name": "develop", "target": {"hash": "old1"}},
                },
                {
                    "new": {"type": "branch", "name": "feature", "target": {"hash": "x"}},
                    "old": {"type": "branch", "name": "feature", "target": {"hash": "y"}},
                },
            ]
        },
    }
    record = type("R", (), {"status": "ready"})()
    db = type("DB", (), {"get_repo": lambda self, *a, **k: record})()
    auth = BitbucketTokenAuth("tok")
    with (
        patch("mira.platforms.index_handlers._get_app_db", return_value=db),
        patch(
            f"{WEBHOOK}._changed_paths",
            new=AsyncMock(return_value=({"a.py", "moved.py"}, {"old.py"})),
        ) as diffstat,
        patch("mira.platforms.index_handlers.run_incremental_index", new=AsyncMock()) as index,
    ):
        await handle_bitbucket_push(payload, auth, BOT)
    diffstat.assert_awaited_once_with("tok", "acme", "widgets", "new1", "old1")
    args = index.await_args.args
    assert sorted(args[3]) == ["a.py", "moved.py"]
    assert args[4] == ["old.py"]
    assert args[5] == "develop"


class TestCommentHandler:
    @staticmethod
    def _provider():  # noqa: ANN205
        from mira.models import BotThreadRecord, PRInfo

        provider = AsyncMock()
        provider.get_pr_info.return_value = PRInfo(
            title="T",
            description="",
            base_branch="main",
            head_branch="feature",
            url="https://bitbucket.org/acme/widgets/pull-requests/7",
            number=7,
            owner="acme",
            repo="widgets",
            platform="bitbucket",
        )
        provider.get_thread_id_for_comment.return_value = "40"
        provider.get_discussion_root_body.return_value = "original finding"
        provider.get_all_bot_threads.return_value = [
            BotThreadRecord(thread_id="40", path="a.py", line=12, body="x", is_resolved=False)
        ]
        return provider

    @pytest.mark.asyncio
    async def test_pause_command(self, bitbucket_auth):
        from mira.platforms.bitbucket.webhook import handle_bitbucket_comment

        provider = self._provider()
        with (
            patch(f"{WEBHOOK}.create_provider", return_value=provider),
            patch(f"{WEBHOOK}.is_command_allowed", new=AsyncMock(return_value=True)),
        ):
            await handle_bitbucket_comment(
                _comment_payload("@{557058:bot} pause", _actor("alice")), bitbucket_auth, BOT
            )
        provider.add_label.assert_awaited_once()
        assert provider.add_label.await_args.args[1] == "mira-paused"

    @pytest.mark.asyncio
    async def test_reply_in_bot_thread_routes_to_thread_reply(self, bitbucket_auth):
        from mira.platforms.bitbucket.webhook import handle_bitbucket_comment

        provider = self._provider()
        payload = _comment_payload("@mira-bot why is this a problem?", _actor("alice"))
        payload["comment"]["parent"] = {"id": 40}
        with (
            patch(f"{WEBHOOK}.create_provider", return_value=provider),
            patch("mira.platforms.handlers.run_thread_reply", new=AsyncMock()) as reply,
            patch("mira.platforms.handlers.run_pr_command", new=AsyncMock()) as command,
        ):
            await handle_bitbucket_comment(payload, bitbucket_auth, BOT)
        command.assert_not_called()
        kw = reply.await_args.kwargs
        assert reply.await_args.args[2] == "why is this a problem?"
        assert kw["thread_id"] == "40"
        assert (kw["comment_path"], kw["comment_line"]) == ("a.py", 12)
        assert kw["original_suggestion"] == "original finding"
        assert kw["platform"] == "bitbucket"
