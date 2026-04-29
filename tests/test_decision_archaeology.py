"""Tests for decision archaeology — file history fed into the review prompt."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mira.config import MiraConfig
from mira.llm.prompts.review import build_review_prompt
from mira.models import FileChangeType, FileDiff, FileHistoryEntry
from mira.providers.github import GitHubProvider


def _file(path: str) -> FileDiff:
    return FileDiff(path=path, change_type=FileChangeType.MODIFIED)


# ── Provider: get_file_history ──


class TestGetFileHistory:
    @pytest.mark.asyncio
    async def test_parses_recent_commits(self):
        """Mock the GitHub commits API and verify each commit becomes a FileHistoryEntry."""
        from mira.models import PRInfo

        pr_info = PRInfo(
            title="t",
            description="d",
            base_branch="main",
            head_branch="f",
            url="https://github.com/o/r/pull/1",
            number=1,
            owner="o",
            repo="r",
        )

        commits_payload = [
            {
                "sha": "abc123def456",
                "commit": {
                    "message": "Fix race in auth flow\n\nLong description...",
                    "author": {"name": "Alice", "date": "2024-12-01T10:00:00Z"},
                },
            },
            {
                "sha": "def456abc",
                "commit": {
                    "message": "Initial auth module",
                    "author": {"name": "Bob", "date": "2024-11-01T10:00:00Z"},
                },
            },
        ]

        provider = GitHubProvider(token="fake")

        with patch("mira.providers.github.httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = instance
            response = AsyncMock()
            response.status_code = 200
            response.json = lambda: commits_payload
            instance.get.return_value = response

            history = await provider.get_file_history(
                pr_info,
                ["src/auth.py"],
                max_per_file=5,
            )

        assert "src/auth.py" in history
        entries = history["src/auth.py"]
        assert len(entries) == 2
        assert entries[0].sha == "abc123de"  # truncated to 8 chars
        assert entries[0].author == "Alice"
        # Long commit message body trimmed at first paragraph break
        assert entries[0].message == "Fix race in auth flow"
        assert entries[1].author == "Bob"

    @pytest.mark.asyncio
    async def test_empty_paths_returns_empty(self):
        from mira.models import PRInfo

        provider = GitHubProvider(token="fake")
        pr_info = PRInfo(
            title="",
            description="",
            base_branch="",
            head_branch="",
            url="",
            number=1,
            owner="o",
            repo="r",
        )
        result = await provider.get_file_history(pr_info, [], max_per_file=5)
        assert result == {}

    @pytest.mark.asyncio
    async def test_handles_404_per_file(self):
        """If one file returns 404, the others still work; 404 entries are dropped."""
        from mira.models import PRInfo

        pr_info = PRInfo(
            title="",
            description="",
            base_branch="",
            head_branch="",
            url="",
            number=1,
            owner="o",
            repo="r",
        )

        def _make_response(path: str):
            r = AsyncMock()
            if "missing" in path:
                r.status_code = 404
                r.json = lambda: {"message": "Not Found"}
            else:
                r.status_code = 200
                r.json = lambda: [
                    {
                        "sha": "abc",
                        "commit": {
                            "message": "Add foo",
                            "author": {"name": "Alice", "date": "2024-12-01"},
                        },
                    }
                ]
            return r

        provider = GitHubProvider(token="fake")
        with patch("mira.providers.github.httpx.AsyncClient") as mock_client_cls:
            instance = AsyncMock()
            mock_client_cls.return_value.__aenter__.return_value = instance

            async def _get(url, headers=None, params=None):
                return _make_response(params.get("path", ""))

            instance.get.side_effect = _get

            result = await provider.get_file_history(
                pr_info,
                ["src/ok.py", "src/missing.py"],
                max_per_file=5,
            )

        assert "src/ok.py" in result
        assert "src/missing.py" not in result  # empty list filtered out


# ── Prompt rendering ──


class TestPromptRendersFileHistory:
    def test_history_section_renders_when_provided(self):
        config = MiraConfig()
        files = [_file("src/auth.py")]
        history = {
            "src/auth.py": [
                FileHistoryEntry(
                    sha="abc123de",
                    message="Fix race condition",
                    author="Alice",
                    date="2024-12-01",
                ),
            ],
        }

        messages = build_review_prompt(
            files=files,
            config=config,
            file_history=history,
        )
        system = messages[0]["content"]

        assert "File History" in system
        assert "Why Does This Code Exist?" in system
        assert "abc123de" in system
        assert "Fix race condition" in system
        assert "Alice" in system

    def test_section_omitted_when_no_history(self):
        config = MiraConfig()
        files = [_file("src/auth.py")]
        messages = build_review_prompt(files=files, config=config)
        assert "File History" not in messages[0]["content"]

    def test_empty_dict_treated_as_no_history(self):
        config = MiraConfig()
        files = [_file("src/auth.py")]
        messages = build_review_prompt(files=files, config=config, file_history={})
        assert "File History" not in messages[0]["content"]

    def test_multiple_files_rendered_separately(self):
        config = MiraConfig()
        files = [
            _file("a.py"),
            _file("b.py"),
        ]
        history = {
            "a.py": [FileHistoryEntry(sha="a1", message="msg-A", author="X", date="d")],
            "b.py": [FileHistoryEntry(sha="b1", message="msg-B", author="Y", date="d")],
        }
        messages = build_review_prompt(files=files, config=config, file_history=history)
        system = messages[0]["content"]
        assert "msg-A" in system
        assert "msg-B" in system
        assert "`a.py`" in system
        assert "`b.py`" in system


# ── Engine integration (provider missing → graceful skip) ──


@pytest.mark.asyncio
async def test_engine_skips_history_when_no_provider(sample_diff_text):
    """Running engine.review_diff (no provider) shouldn't crash on history fetch."""
    from mira.core.engine import ReviewEngine
    from mira.llm.provider import LLMProvider

    llm = MagicMock(spec=LLMProvider)
    llm.walkthrough = AsyncMock(return_value='{"summary": "x"}')
    llm.review = AsyncMock(return_value='{"comments": [], "summary": "ok", "metadata": {}}')
    llm.complete = AsyncMock(return_value="{}")
    llm.count_tokens = MagicMock(return_value=10)
    llm.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    engine = ReviewEngine(config=MiraConfig(), llm=llm)
    result = await engine.review_diff(sample_diff_text)
    # No exception; history was skipped because no provider.
    assert result.reviewed_files >= 0
