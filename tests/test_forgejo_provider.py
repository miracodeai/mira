"""Tests for the Forgejo provider API (permissions and check runs).

Forgejo has no Checks API; check runs are adapted onto commit statuses.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

from mira.exceptions import ConfigError, ProviderError
from mira.models import PRInfo
from mira.platforms import profiles
from mira.providers.forgejo import ForgejoProvider


class _FakeResp:
    def __init__(self, status=200, json_data=None):
        self.status_code = status
        self._json = json_data
        self.text = ""

    def json(self):
        return self._json


class _FakeClient:
    def __init__(self, handler):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, method, url, **kw):
        return self._handler(method, url, **kw)


def _patch(handler):
    return patch("mira.providers.forgejo.httpx.AsyncClient", lambda *a, **k: _FakeClient(handler))


@pytest.mark.asyncio
@pytest.mark.parametrize("subpath", ["", "/forgejo"])
async def test_compare_diff_sends_token_to_configured_instance(monkeypatch, subpath):
    monkeypatch.setenv("MIRA_FORGEJO_API_URL", f"https://git.acme.test{subpath}/api/v1/")
    monkeypatch.delenv("MIRA_FORGEJO_BASE_URL", raising=False)
    monkeypatch.delenv("MIRA_PLATFORMS_JSON_PATH", raising=False)
    profiles._load.cache_clear()
    requests = []

    def handler(method, url, **kw):
        requests.append((method, url, kw["headers"]))
        response = _FakeResp()
        response.text = "diff --git a/file b/file"
        return response

    pr = PRInfo(
        title="Example",
        description="",
        base_branch="main",
        head_branch="feature",
        url=f"https://git.acme.test{subpath}/alice/private-repo/pulls/1",
        number=1,
        owner="alice",
        repo="private-repo",
        platform="forgejo",
    )
    try:
        with _patch(handler):
            diff = await ForgejoProvider("dummy-token").get_compare_diff(pr, "base", "head")
        assert diff == "diff --git a/file b/file"
        assert requests == [
            (
                "GET",
                f"https://git.acme.test{subpath}/alice/private-repo/compare/base...head.diff",
                {"Authorization": "token dummy-token", "Accept": "text/plain"},
            )
        ]
    finally:
        profiles._load.cache_clear()


class TestGetRepoPermission:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raw", "normalized"),
        [
            ("admin", "write"),
            ("owner", "write"),
            ("write", "write"),
            ("read", "read"),
            ("none", "none"),
        ],
    )
    async def test_normalizes_permission_levels(self, raw, normalized):
        def handler(method, url, **kw):
            assert "/collaborators/alice/permission" in url
            return _FakeResp(json_data={"permission": raw})

        with _patch(handler):
            permission = await ForgejoProvider("tok").get_repo_permission(
                "acme", "widgets", "alice"
            )
        assert permission == normalized

    @pytest.mark.asyncio
    async def test_missing_collaborator_is_none(self):
        def handler(method, url, **kw):
            return _FakeResp(status=404, json_data={})

        with _patch(handler):
            permission = await ForgejoProvider("tok").get_repo_permission(
                "acme", "widgets", "alice"
            )
        assert permission == "none"

    @pytest.mark.asyncio
    async def test_unknown_permission_is_none(self):
        def handler(method, url, **kw):
            return _FakeResp(json_data={"permission": "mystery"})

        with _patch(handler):
            permission = await ForgejoProvider("tok").get_repo_permission(
                "acme", "widgets", "alice"
            )
        assert permission is None

    @pytest.mark.asyncio
    async def test_forbidden_lookup_explains_admin_requirement(self):
        def handler(method, url, **kw):
            return _FakeResp(status=403)

        with _patch(handler), pytest.raises(ProviderError, match="repository admin/owner"):
            await ForgejoProvider("tok").get_repo_permission("acme", "widgets", "alice")


class TestCheckRuns:
    """Forgejo has no Checks API: the provider adapts the check lifecycle onto
    commit statuses (same context per SHA → the status is replaced in place)."""

    def _pr_info(self, head_sha: str = "deadbeef") -> PRInfo:
        return PRInfo(
            title="t",
            description="d",
            base_branch="main",
            head_branch="h",
            url="https://codeberg.example.org/o/r/pulls/1",
            number=1,
            owner="o",
            repo="r",
            head_sha=head_sha,
        )

    @pytest.mark.asyncio
    async def test_start_posts_pending_status(self):
        provider = ForgejoProvider("tok")
        pr_info = self._pr_info()
        captured = {}

        def handler(method, url, **kw):
            captured["method"] = method
            captured["url"] = url
            captured["json"] = kw.get("json")
            return _FakeResp(status=201, json_data={})

        with _patch(handler):
            run_id = await provider.start_check_run(
                pr_info,
                title="Code Review In Progress",
                summary="Mira is analyzing your code changes…",
                details_url=pr_info.url,
            )
        assert run_id == 0  # posted sentinel
        assert captured["method"] == "POST"
        assert captured["url"].endswith("/repos/o/r/statuses/deadbeef")
        assert captured["json"] == {
            "state": "pending",
            "context": "Mira",
            "description": "Mira is analyzing your code changes…",
            "target_url": "https://codeberg.example.org/o/r/pulls/1",
        }

    @pytest.mark.asyncio
    async def test_finish_posts_terminal_status(self):
        provider = ForgejoProvider("tok")
        pr_info = self._pr_info()
        captured = {}

        def handler(method, url, **kw):
            captured["url"] = url
            captured["json"] = kw.get("json")
            return _FakeResp(status=201, json_data={})

        with _patch(handler):
            await provider.finish_check_run(
                pr_info,
                0,
                "success",
                title="Code Review Complete",
                summary="Review finished successfully.",
            )
        assert captured["url"].endswith("/repos/o/r/statuses/deadbeef")
        assert captured["json"]["state"] == "success"
        assert captured["json"]["context"] == "Mira"

    @pytest.mark.asyncio
    async def test_finish_failure_and_neutral_states(self):
        provider = ForgejoProvider("tok")
        pr_info = self._pr_info()
        seen: list[dict] = []

        def handler(method, url, **kw):
            seen.append(kw.get("json") or {})
            return _FakeResp(status=201, json_data={})

        with _patch(handler):
            await provider.finish_check_run(pr_info, 0, "failure", summary="It failed")
            await provider.finish_check_run(pr_info, 0, "neutral", summary="Warnings")
        # Forgejo statuses have no neutral state: neutral renders as success.
        assert [j["state"] for j in seen] == ["failure", "success"]
        assert all(j["context"] == "Mira" for j in seen)
        assert seen[0]["description"] == "It failed"

    @pytest.mark.asyncio
    async def test_finish_without_run_id_does_nothing(self):
        provider = ForgejoProvider("tok")
        called = []

        def handler(method, url, **kw):
            called.append(url)
            return _FakeResp(status=201, json_data={})

        with _patch(handler):
            await provider.finish_check_run(self._pr_info(), None, "success")
        assert called == []

    @pytest.mark.asyncio
    async def test_forbidden_start_raises_provider_error(self):
        provider = ForgejoProvider("tok")

        def handler(method, url, **kw):
            return _FakeResp(status=403)

        with _patch(handler), pytest.raises(ProviderError, match="403"):
            await provider.start_check_run(self._pr_info())

    @pytest.mark.asyncio
    async def test_no_head_sha_performs_no_request(self):
        provider = ForgejoProvider("tok")
        called = []

        def handler(method, url, **kw):
            called.append(url)
            return _FakeResp(status=201, json_data={})

        with _patch(handler):
            assert await provider.start_check_run(self._pr_info(head_sha="")) is None
            assert called == []


class TestForgejoURLs:
    @pytest.fixture(autouse=True)
    def isolate_profiles(self, monkeypatch):
        monkeypatch.delenv("MIRA_FORGEJO_API_URL", raising=False)
        monkeypatch.delenv("MIRA_FORGEJO_BASE_URL", raising=False)
        monkeypatch.delenv("MIRA_PLATFORMS_JSON_PATH", raising=False)
        profiles._load.cache_clear()
        yield
        profiles._load.cache_clear()

    def test_default_web_base(self):
        assert ForgejoProvider("dummy-token")._base == "https://codeberg.org"

    @pytest.mark.parametrize(
        ("api_url", "base_url"),
        [
            ("https://git.acme.test/api/v1", "https://git.acme.test"),
            ("https://git.acme.test/api/v1/", "https://git.acme.test"),
            ("https://git.acme.test/forgejo/api/v1/", "https://git.acme.test/forgejo"),
            ("http://localhost:3000/git/api/v1", "http://localhost:3000/git"),
        ],
    )
    def test_infers_web_base(self, monkeypatch, api_url, base_url):
        monkeypatch.setenv("MIRA_FORGEJO_API_URL", api_url)
        assert ForgejoProvider("dummy-token")._base == base_url

    @pytest.mark.parametrize(
        "api_url",
        [
            "https://git.acme.test/custom-api",
            "https://git.acme.test/api/v10",
            "https://[invalid/api/v1",
        ],
    )
    def test_cannot_infer_raises_config_error(self, monkeypatch, api_url):
        monkeypatch.setenv("MIRA_FORGEJO_API_URL", api_url)
        with pytest.raises(ConfigError, match="Cannot infer MIRA_FORGEJO_BASE_URL"):
            ForgejoProvider("dummy-token")

    @pytest.mark.parametrize(
        "api_url", ["https://git.acme.test/api/v1", "https://api.acme.test/custom"]
    )
    def test_explicit_base_takes_precedence(self, monkeypatch, api_url):
        monkeypatch.setenv("MIRA_FORGEJO_API_URL", api_url)
        monkeypatch.setenv("MIRA_FORGEJO_BASE_URL", "https://web.acme.test/forgejo/")
        assert ForgejoProvider("dummy-token")._base == "https://web.acme.test/forgejo"

    @pytest.mark.parametrize("explicit_base", [None, "https://web.acme.test/forgejo"])
    def test_profile_override(self, monkeypatch, tmp_path, explicit_base):
        profile = {"api_url": "https://git.acme.test/forgejo/api/v1"}
        if explicit_base:
            profile["base_url"] = explicit_base
        custom = tmp_path / "platforms.json"
        custom.write_text(json.dumps({"forgejo": profile}))
        monkeypatch.setenv("MIRA_PLATFORMS_JSON_PATH", str(custom))
        assert ForgejoProvider("dummy-token")._base == (
            explicit_base or "https://git.acme.test/forgejo"
        )


class TestRemoveLabel:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [200, 204])
    @pytest.mark.parametrize(
        ("label", "encoded"),
        [
            ("mira-paused", b"mira-paused"),
            ("needs triage", b"needs%20triage"),
            ("team/security", b"team%2Fsecurity"),
            ("label#tag?x=1&more", b"label%23tag%3Fx%3D1%26more"),
            ("literal%2Ftag", b"literal%252Ftag"),
            ("修正", b"%E4%BF%AE%E6%AD%A3"),
        ],
    )
    async def test_removes_only_requested_label(self, monkeypatch, status, label, encoded):
        from urllib.parse import unquote

        labels = {label, "bug", "release-blocker"}
        requests = []
        collection = b"/api/v1/repos/example/app/issues/7/labels"

        def serve(request):
            requests.append(request)
            raw_path = request.url.raw_path.split(b"?", 1)[0]
            if raw_path == collection:
                labels.clear()  # Forgejo's collection DELETE clears every label.
            else:
                assert raw_path.startswith(collection + b"/")
                labels.discard(unquote(raw_path[len(collection) + 1 :].decode()))
            return httpx.Response(status, json=[] if status == 200 else None)

        client_type = httpx.AsyncClient
        monkeypatch.setattr(
            "mira.providers.forgejo.httpx.AsyncClient",
            lambda **kwargs: client_type(transport=httpx.MockTransport(serve), **kwargs),
        )
        provider = ForgejoProvider("synthetic")
        provider._api = "https://forge.example.invalid/api/v1"
        info = PRInfo(
            title="Synthetic",
            description="",
            base_branch="main",
            head_branch="topic",
            url="https://forge.example.invalid/example/app/pulls/7",
            number=7,
            owner="example",
            repo="app",
            platform="forgejo",
        )

        await provider.remove_label(info, label)

        assert labels == {"bug", "release-blocker"}
        assert len(requests) == 1
        assert requests[0].method == "DELETE"
        assert requests[0].url.raw_path == collection + b"/" + encoded
        assert requests[0].url.fragment == ""
        assert requests[0].url.params.multi_items() == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [403, 404, 422, 500])
    async def test_remove_label_errors_do_not_fall_back_to_collection(self, monkeypatch, status):
        requests = []

        def serve(request):
            requests.append(request)
            return httpx.Response(status)

        client_type = httpx.AsyncClient
        monkeypatch.setattr(
            "mira.providers.forgejo.httpx.AsyncClient",
            lambda **kwargs: client_type(transport=httpx.MockTransport(serve), **kwargs),
        )
        provider = ForgejoProvider("synthetic")
        provider._api = "https://forge.example.invalid/api/v1"
        info = PRInfo(
            title="Synthetic",
            description="",
            base_branch="main",
            head_branch="topic",
            url="https://forge.example.invalid/example/app/pulls/7",
            number=7,
            owner="example",
            repo="app",
            platform="forgejo",
        )

        with pytest.raises(ProviderError) as error:
            await provider.remove_label(info, "mira-paused")

        assert error.value.status_code == status
        assert len(requests) == 1
        assert requests[0].url.raw_path == b"/api/v1/repos/example/app/issues/7/labels/mira-paused"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("label", ["", ".", ".."])
    async def test_remove_label_rejects_normalizing_identifiers_without_http(
        self, monkeypatch, label
    ):
        requests = []

        def serve(request):
            requests.append(str(request.url))
            return httpx.Response(204)

        client_type = httpx.AsyncClient
        monkeypatch.setattr(
            "mira.providers.forgejo.httpx.AsyncClient",
            lambda **kwargs: client_type(transport=httpx.MockTransport(serve), **kwargs),
        )
        provider = ForgejoProvider("synthetic")
        provider._api = "https://forge.example.invalid/api/v1"
        info = PRInfo(
            title="Synthetic",
            description="",
            base_branch="main",
            head_branch="topic",
            url="https://forge.example.invalid/example/app/pulls/7",
            number=7,
            owner="example",
            repo="app",
            platform="forgejo",
        )
        error = None
        try:
            await provider.remove_label(info, label)
        except ProviderError as exc:
            error = exc

        assert requests == []
        assert isinstance(error, ProviderError)
