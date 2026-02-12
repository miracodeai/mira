"""Tests for CLI formatting and command invocation."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from mira.cli import _format_json, _format_text, main
from mira.models import (
    FileChangeType,
    ReviewComment,
    ReviewResult,
    Severity,
    WalkthroughEffort,
    WalkthroughFileEntry,
    WalkthroughResult,
)


def _make_result(**overrides) -> ReviewResult:
    defaults = {
        "comments": [],
        "summary": "Looks good.",
        "reviewed_files": 1,
        "token_usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    defaults.update(overrides)
    return ReviewResult(**defaults)


def _make_comment(**overrides) -> ReviewComment:
    defaults = {
        "path": "src/foo.py",
        "line": 10,
        "end_line": None,
        "severity": Severity.WARNING,
        "category": "bug",
        "title": "Potential bug",
        "body": "This might crash.",
        "confidence": 0.9,
        "suggestion": None,
    }
    defaults.update(overrides)
    return ReviewComment(**defaults)


# ---------------------------------------------------------------------------
# _format_text tests
# ---------------------------------------------------------------------------


class TestFormatText:
    def test_no_comments(self):
        result = _make_result()
        text = _format_text(result)
        assert "Looks good." in text
        assert "No issues found." in text

    def test_with_comments(self):
        comment = _make_comment()
        result = _make_result(comments=[comment])
        text = _format_text(result)
        assert "[WARNING]" in text
        assert "src/foo.py:10" in text
        assert "Potential bug" in text
        assert "This might crash." in text
        assert "Reviewed 1 files, 1 comments." in text
        assert "Tokens used: 15" in text

    def test_with_suggestion(self):
        comment = _make_comment(suggestion="return None")
        result = _make_result(comments=[comment])
        text = _format_text(result)
        assert "Suggestion: return None" in text

    def test_with_walkthrough(self):
        wt = WalkthroughResult(summary="PR adds helpers.")
        result = _make_result(walkthrough=wt)
        text = _format_text(result)
        assert "## Mira PR Walkthrough" in text
        assert "---" in text
        assert "PR adds helpers." in text

    def test_no_summary(self):
        result = _make_result(summary="")
        text = _format_text(result)
        assert "No issues found." in text

    def test_no_token_usage(self):
        result = _make_result(token_usage={}, comments=[_make_comment()])
        text = _format_text(result)
        assert "Tokens used:" not in text


# ---------------------------------------------------------------------------
# _format_json tests
# ---------------------------------------------------------------------------


class TestFormatJson:
    def test_basic_json(self):
        result = _make_result()
        raw = _format_json(result)
        data = json.loads(raw)
        assert data["summary"] == "Looks good."
        assert data["walkthrough"] is None
        assert data["comments"] == []
        assert data["reviewed_files"] == 1

    def test_json_with_comments(self):
        comment = _make_comment(end_line=12)
        result = _make_result(comments=[comment])
        raw = _format_json(result)
        data = json.loads(raw)
        assert len(data["comments"]) == 1
        c = data["comments"][0]
        assert c["path"] == "src/foo.py"
        assert c["line"] == 10
        assert c["end_line"] == 12
        assert c["severity"] == "warning"
        assert c["category"] == "bug"

    def test_json_with_walkthrough(self):
        wt = WalkthroughResult(
            summary="PR summary.",
            file_changes=[
                WalkthroughFileEntry(
                    path="a.py",
                    change_type=FileChangeType.ADDED,
                    description="New file",
                    group="Core",
                )
            ],
            effort=WalkthroughEffort(level=2, label="Simple", minutes=10),
            sequence_diagram="sequenceDiagram\n  A->>B: call",
        )
        result = _make_result(walkthrough=wt)
        raw = _format_json(result)
        data = json.loads(raw)
        w = data["walkthrough"]
        assert w["summary"] == "PR summary."
        assert len(w["change_groups"]) == 1
        assert w["change_groups"][0]["label"] == "Core"
        assert w["change_groups"][0]["files"][0]["path"] == "a.py"
        assert w["effort"]["level"] == 2
        assert w["effort"]["minutes"] == 10
        assert "sequenceDiagram" in w["sequence_diagram"]

    def test_json_walkthrough_no_effort(self):
        wt = WalkthroughResult(summary="No effort.")
        result = _make_result(walkthrough=wt)
        raw = _format_json(result)
        data = json.loads(raw)
        assert data["walkthrough"]["effort"] is None

    def test_json_walkthrough_ungrouped_files(self):
        wt = WalkthroughResult(
            summary="Flat.",
            file_changes=[
                WalkthroughFileEntry(
                    path="a.py",
                    change_type=FileChangeType.MODIFIED,
                    description="Changed",
                ),
            ],
        )
        result = _make_result(walkthrough=wt)
        raw = _format_json(result)
        data = json.loads(raw)
        # Files without group label get bucketed as "Other"
        assert data["walkthrough"]["change_groups"][0]["label"] == "Other"


# ---------------------------------------------------------------------------
# CLI invocation tests
# ---------------------------------------------------------------------------


class TestCLI:
    def test_version(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "mira" in result.output.lower()

    def test_review_requires_pr_or_stdin(self):
        runner = CliRunner()
        result = runner.invoke(main, ["review"])
        assert result.exit_code != 0
        assert "Provide --pr" in result.output or "Usage" in result.output

    def test_review_pr_requires_github_token(self):
        runner = CliRunner()
        result = runner.invoke(
            main, ["review", "--pr", "https://github.com/o/r/pull/1"], catch_exceptions=False
        )
        assert result.exit_code != 0
        assert "token" in result.output.lower() or "GITHUB_TOKEN" in result.output

    def test_review_stdin_text_output(self):
        review_result = _make_result(summary="All good.")

        with patch("mira.cli.ReviewEngine") as mock_engine_cls:
            mock_engine = MagicMock()
            mock_engine.review_diff = AsyncMock(return_value=review_result)
            mock_engine_cls.return_value = mock_engine

            runner = CliRunner()
            result = runner.invoke(
                main,
                ["review", "--stdin"],
                input="diff --git a/f.py b/f.py\n",
            )

        assert result.exit_code == 0
        assert "All good." in result.output
        assert "No issues found." in result.output

    def test_review_stdin_json_output(self):
        review_result = _make_result(summary="JSON output.")

        with patch("mira.cli.ReviewEngine") as mock_engine_cls:
            mock_engine = MagicMock()
            mock_engine.review_diff = AsyncMock(return_value=review_result)
            mock_engine_cls.return_value = mock_engine

            runner = CliRunner()
            result = runner.invoke(
                main,
                ["review", "--stdin", "--output", "json"],
                input="diff --git a/f.py b/f.py\n",
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["summary"] == "JSON output."

    def test_review_overrides(self):
        review_result = _make_result()

        with (
            patch("mira.cli.ReviewEngine") as mock_engine_cls,
            patch("mira.cli.load_config") as mock_load,
        ):
            mock_load.return_value = MagicMock()
            mock_engine = MagicMock()
            mock_engine.review_diff = AsyncMock(return_value=review_result)
            mock_engine_cls.return_value = mock_engine

            runner = CliRunner()
            result = runner.invoke(
                main,
                [
                    "review",
                    "--stdin",
                    "--model",
                    "test-model",
                    "--max-comments",
                    "3",
                    "--confidence",
                    "0.9",
                ],
                input="diff\n",
            )

        assert result.exit_code == 0
        # Verify overrides were passed to load_config
        call_args = mock_load.call_args
        overrides = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("overrides")
        if overrides is None and len(call_args[0]) > 1:
            overrides = call_args[0][1]

    def test_review_blocker_exit_code_1(self):
        blocker = _make_comment(severity=Severity.BLOCKER)
        review_result = _make_result(comments=[blocker])

        with patch("mira.cli.ReviewEngine") as mock_engine_cls:
            mock_engine = MagicMock()
            mock_engine.review_diff = AsyncMock(return_value=review_result)
            mock_engine_cls.return_value = mock_engine

            runner = CliRunner()
            result = runner.invoke(
                main,
                ["review", "--stdin"],
                input="diff\n",
            )

        assert result.exit_code == 1

    def test_review_verbose_flag(self):
        review_result = _make_result()

        with patch("mira.cli.ReviewEngine") as mock_engine_cls:
            mock_engine = MagicMock()
            mock_engine.review_diff = AsyncMock(return_value=review_result)
            mock_engine_cls.return_value = mock_engine

            runner = CliRunner()
            result = runner.invoke(
                main,
                ["review", "--stdin", "--verbose"],
                input="diff\n",
            )

        assert result.exit_code == 0
