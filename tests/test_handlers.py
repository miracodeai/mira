"""Tests for webhook event handlers."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.github_app.handlers import handle_comment, handle_pull_request
from mira.models import PRInfo, ReviewResult


def _make_pr_payload() -> dict[str, Any]:
    return {
        "installation": {"id": 1},
        "action": "opened",
        "pull_request": {"number": 42},
        "repository": {
            "owner": {"login": "testowner"},
            "name": "testrepo",
        },
    }


def _make_comment_payload(body: str) -> dict[str, Any]:
    return {
        "installation": {"id": 1},
        "action": "created",
        "comment": {"body": body, "user": {"login": "alice"}},
        "issue": {
            "number": 7,
            "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/7"},
        },
        "repository": {
            "owner": {"login": "testowner"},
            "name": "testrepo",
        },
    }


@pytest.fixture
def mock_app_auth() -> AsyncMock:
    auth = AsyncMock()
    auth.get_installation_token = AsyncMock(return_value="ghs_test_token")
    return auth


@pytest.fixture
def mock_pr_info() -> PRInfo:
    return PRInfo(
        title="Test PR",
        description="A test PR",
        base_branch="main",
        head_branch="feature",
        url="https://github.com/testowner/testrepo/pull/42",
        number=42,
        owner="testowner",
        repo="testrepo",
    )


@patch("mira.github_app.handlers.ReviewEngine")
@patch("mira.github_app.handlers.GitHubProvider")
@patch("mira.github_app.handlers.LLMProvider")
@patch("mira.github_app.handlers.load_config")
async def test_handle_pr_event(
    mock_config: MagicMock,
    mock_llm_cls: MagicMock,
    mock_provider_cls: MagicMock,
    mock_engine_cls: MagicMock,
    mock_app_auth: AsyncMock,
) -> None:
    """PR event creates engine and calls review_pr."""
    mock_config.return_value = MagicMock()
    mock_engine = AsyncMock()
    mock_engine.review_pr = AsyncMock(return_value=ReviewResult(summary="ok"))
    mock_engine_cls.return_value = mock_engine

    await handle_pull_request(_make_pr_payload(), mock_app_auth, "mira-bot")

    mock_app_auth.get_installation_token.assert_awaited_once_with(1)
    mock_provider_cls.assert_called_once_with("ghs_test_token")
    mock_engine.review_pr.assert_awaited_once_with("https://github.com/testowner/testrepo/pull/42")


@patch("mira.github_app.handlers.ReviewEngine")
@patch("mira.github_app.handlers.GitHubProvider")
@patch("mira.github_app.handlers.LLMProvider")
@patch("mira.github_app.handlers.load_config")
async def test_handle_comment_review_keyword(
    mock_config: MagicMock,
    mock_llm_cls: MagicMock,
    mock_provider_cls: MagicMock,
    mock_engine_cls: MagicMock,
    mock_app_auth: AsyncMock,
) -> None:
    """'review' keyword triggers full review_pr."""
    mock_config.return_value = MagicMock()
    mock_engine = AsyncMock()
    mock_engine.review_pr = AsyncMock(return_value=ReviewResult(summary="ok"))
    mock_engine_cls.return_value = mock_engine

    payload = _make_comment_payload("@mira-bot review")
    await handle_comment(payload, mock_app_auth, "mira-bot")

    mock_engine.review_pr.assert_awaited_once()


@patch("mira.github_app.handlers.GitHubProvider")
@patch("mira.github_app.handlers.LLMProvider")
@patch("mira.github_app.handlers.load_config")
async def test_handle_comment_question(
    mock_config: MagicMock,
    mock_llm_cls: MagicMock,
    mock_provider_cls: MagicMock,
    mock_app_auth: AsyncMock,
    mock_pr_info: PRInfo,
) -> None:
    """A question triggers conversational reply via llm.complete(json_mode=False)."""
    mock_config.return_value = MagicMock()
    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value="Because of the nested loop.")
    mock_llm_cls.return_value = mock_llm

    mock_provider = AsyncMock()
    mock_provider.get_pr_info = AsyncMock(return_value=mock_pr_info)
    mock_provider.get_pr_diff = AsyncMock(return_value="diff content")
    mock_provider.post_comment = AsyncMock()
    mock_provider_cls.return_value = mock_provider

    payload = _make_comment_payload("@mira-bot why is this slow?")
    await handle_comment(payload, mock_app_auth, "mira-bot")

    mock_llm.complete.assert_awaited_once()
    _, kwargs = mock_llm.complete.call_args
    assert kwargs.get("json_mode") is False

    mock_provider.post_comment.assert_awaited_once()


@patch("mira.github_app.handlers.GitHubProvider")
@patch("mira.github_app.handlers.LLMProvider")
@patch("mira.github_app.handlers.load_config")
async def test_handle_comment_formats_reply_with_attribution(
    mock_config: MagicMock,
    mock_llm_cls: MagicMock,
    mock_provider_cls: MagicMock,
    mock_app_auth: AsyncMock,
    mock_pr_info: PRInfo,
) -> None:
    """Reply includes '> @user asked:' attribution prefix."""
    mock_config.return_value = MagicMock()
    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value="It's an O(n^2) loop.")
    mock_llm_cls.return_value = mock_llm

    mock_provider = AsyncMock()
    mock_provider.get_pr_info = AsyncMock(return_value=mock_pr_info)
    mock_provider.get_pr_diff = AsyncMock(return_value="diff")
    mock_provider.post_comment = AsyncMock()
    mock_provider_cls.return_value = mock_provider

    payload = _make_comment_payload("@mira-bot why is this slow?")
    await handle_comment(payload, mock_app_auth, "mira-bot")

    posted_body = mock_provider.post_comment.call_args[0][1]
    assert posted_body.startswith("> @alice asked: why is this slow?")
    assert "O(n^2)" in posted_body


async def test_handler_exception_logged_not_raised(
    mock_app_auth: AsyncMock, caplog: pytest.LogCaptureFixture
) -> None:
    """Exceptions in handlers are logged, not propagated."""
    mock_app_auth.get_installation_token = AsyncMock(side_effect=RuntimeError("boom"))

    with caplog.at_level(logging.ERROR):
        # Should not raise
        await handle_pull_request(_make_pr_payload(), mock_app_auth, "mira-bot")

    assert "boom" in caplog.text
