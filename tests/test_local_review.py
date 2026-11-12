"""Exercise local review with real Git history and a scripted model."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from mira.cli import main
from mira.core.git_snapshot import GitSnapshot
from mira.exceptions import MiraError


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-c",
            "user.name=Mira Tests",
            "-c",
            "user.email=mira@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=repo,
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _git(tmp_path, "init", "--initial-branch=main", "--template=")
    src = tmp_path / "src"
    src.mkdir()
    (src / "service.py").write_text("def greet(name):\n    return name.upper()\n")
    (src / "caller.py").write_text(
        "from .service import greet\n\ndef welcome():\n    return greet('Ada')\n"
    )
    (tmp_path / "AGENTS.md").write_text("Preserve compatibility with existing callers.\n")
    (tmp_path / "link.py").symlink_to("src/caller.py")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "Add service and caller")
    _git(tmp_path, "checkout", "-b", "change")
    (src / "service.py").write_text("def greet(name, greeting):\n    return greeting + name\n")
    _git(tmp_path, "commit", "-am", "Require a greeting")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _tool_call(name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": f"call_{name}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def test_local_review_reads_unchanged_caller(checkout: Path, monkeypatch: pytest.MonkeyPatch):
    assert _git(checkout, "diff", "--name-only", "main...HEAD") == "src/service.py"
    (checkout / "src/caller.py").write_text("UNSTAGED_CALLER\n")
    (checkout / "AGENTS.md").write_text("DIRTY_RULE\n")
    _git(checkout, "add", "AGENTS.md")
    (checkout / "untracked.py").write_text("UNTRACKED_SOURCE\n")
    (checkout / ".mira.yaml").write_text(
        "review:\n"
        "  self_critique: false\n"
        "  security_pass: false\n"
        "  second_look: false\n"
        "  include_summary: false\n"
    )
    monkeypatch.chdir(checkout / "src")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("MIRA_GIT_TOKEN", raising=False)
    dashboard = MagicMock()
    monkeypatch.setitem(sys.modules, "mira.dashboard.api", dashboard)

    llm = MagicMock()
    llm.count_tokens.return_value = 100
    llm.usage = {}

    async def complete_agentic(messages, tools):
        if not any(msg["role"] == "tool" for msg in messages):
            prompt = "\n".join(msg.get("content", "") for msg in messages)
            assert "def greet(name, greeting)" in prompt
            assert "Preserve compatibility with existing callers." in prompt
            assert "DIRTY_RULE" not in prompt
            return _tool_call("read_file", {"path": "src/caller.py"})
        tool_result = next(msg["content"] for msg in messages if msg["role"] == "tool")
        assert "return greet('Ada')" in tool_result
        assert "UNSTAGED_CALLER" not in tool_result
        return _tool_call(
            "submit_review",
            {
                "comments": [
                    {
                        "path": "src/service.py",
                        "line": 1,
                        "severity": "blocker",
                        "category": "bug",
                        "title": "Existing caller omits the new argument",
                        "body": "src/caller.py calls greet('Ada') and now raises TypeError.",
                        "confidence": 0.99,
                    }
                ]
            },
        )

    llm.complete_agentic = AsyncMock(side_effect=complete_agentic)
    monkeypatch.setattr("mira.cli.create_llm", lambda _: llm)

    result = CliRunner().invoke(
        main, ["review", "--base", "main", "--no-walkthrough", "--output", "json"]
    )

    assert result.exit_code == 1, result.output
    assert llm.complete_agentic.await_count == 2
    assert dashboard.mock_calls == []
    findings = json.loads(result.output)["comments"]
    assert [(finding["path"], finding["line"]) for finding in findings] == [("src/service.py", 1)]
    assert "src/caller.py" in findings[0]["body"]


def test_branch_diff_uses_merge_base(checkout: Path):
    _git(checkout, "checkout", "main")
    (checkout / "base_only.py").write_text("BASE_ONLY = True\n")
    _git(checkout, "add", "base_only.py")
    _git(checkout, "commit", "-m", "Advance the base branch")
    _git(checkout, "checkout", "change")

    diff = GitSnapshot("main").diff()

    assert "a/src/service.py" in diff
    assert "base_only.py" not in diff


async def test_snapshot_reads_only_regular_files_from_captured_head(checkout: Path):
    snapshot = GitSnapshot("main")
    (checkout / "src/caller.py").write_text("NEW_HEAD\n")
    _git(checkout, "commit", "-am", "Update the caller")
    (checkout / "untracked.py").write_text("UNTRACKED_SOURCE\n")

    assert "greet('Ada')" in (await snapshot.fetch("src/caller.py") or "")
    assert "NEW_HEAD" not in snapshot.diff()
    for path in ("link.py", "untracked.py", "../outside.py", str(checkout / "src/caller.py")):
        assert await snapshot.fetch(path) is None


@pytest.mark.parametrize("content", [b"source\n", b"binary\0data"])
async def test_snapshot_reuses_blob_reads(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, content: bytes
):
    (checkout / "sample.py").write_bytes(content)
    _git(checkout, "add", "sample.py")
    _git(checkout, "commit", "-m", "Add sample file")
    snapshot = GitSnapshot("main")
    git = MagicMock(wraps=snapshot._git)
    monkeypatch.setattr(snapshot, "_git", git)

    expected = None if b"\0" in content else content.decode()
    assert await asyncio.gather(snapshot.fetch("sample.py"), snapshot.fetch("sample.py")) == [
        expected,
        expected,
    ]
    assert await snapshot.fetch("sample.py") == expected
    git.assert_called_once()


async def test_snapshot_retries_failed_blob_read(checkout: Path, monkeypatch: pytest.MonkeyPatch):
    snapshot = GitSnapshot("main")
    git = MagicMock(side_effect=[MiraError("Git read failed"), b"source\n"])
    monkeypatch.setattr(snapshot, "_git", git)

    with pytest.raises(MiraError, match="Git read failed"):
        await snapshot.fetch("src/caller.py")
    assert await snapshot.fetch("src/caller.py") == "source\n"
    assert git.call_count == 2


@pytest.mark.parametrize("other_mode", [["--stdin"], ["--pr", "https://example.invalid/pull/1"]])
def test_local_review_rejects_other_input_modes(other_mode: list[str]):
    result = CliRunner().invoke(main, ["review", "--base", "main", *other_mode])

    assert result.exit_code == 2
    assert "--base cannot be combined with --pr or --stdin" in result.output


def test_unknown_base_is_reported_before_model_setup(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
):
    create_llm = MagicMock()
    monkeypatch.setattr("mira.cli.create_llm", create_llm)

    result = CliRunner().invoke(main, ["review", "--base", "unknown"])

    assert result.exit_code == 1
    assert "Could not read the local Git snapshot" in result.output
    create_llm.assert_not_called()
