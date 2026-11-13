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
from mira.config import MiraConfig
from mira.core.engine import ReviewEngine
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


@pytest.mark.parametrize(
    ("call", "enabled", "checked", "verified"),
    [
        ("greet('Grace')", True, 1, 1),
        ("greet('Grace', 'Hi ')", True, 1, 0),
        ("greet(*('Grace',))", True, 0, 0),
        ("greet('Grace')", False, 0, 0),
    ],
    ids=["broken", "compatible", "unsupported", "disabled"],
)
async def test_local_cross_chunk_call_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, call, enabled, checked, verified
):
    _git(tmp_path, "init", "--initial-branch=main", "--template=")
    service = tmp_path / "service.py"
    caller = tmp_path / "caller.py"
    service.write_text("def greet(name):\n    return name\n")
    caller.write_text("from service import greet\n\ndef run():\n    return greet('Ada')\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "Add caller")
    _git(tmp_path, "switch", "-c", "change")
    service.write_text("def greet(name, prefix):\n    return name\n")
    caller.write_text(f"from service import greet\n\ndef run():\n    return {call}\n")
    _git(tmp_path, "commit", "-am", "Change signature and caller")
    monkeypatch.chdir(tmp_path)
    snapshot = GitSnapshot("main")
    # Working-tree edits must not change the verification result.
    service.write_text("def greet(name, prefix=None):\n    return name\n")
    config = MiraConfig()
    config.llm.max_context_tokens = 2500
    config.review.python_call_verification = enabled
    config.review.agentic_tools = False
    config.review.walkthrough = False
    config.review.security_pass = False
    config.review.include_summary = False
    model = MagicMock()
    model.count_tokens.return_value = 400
    model.usage = {}

    async def review(messages):
        prompt = "\n".join(m["content"] for m in messages)
        assert ("Independently checked Python call bindings" in prompt) == bool(checked)
        if "### `caller.py`" in prompt:
            return json.dumps(
                {
                    "comments": [
                        {
                            "path": "caller.py",
                            "line": 4,
                            "severity": "warning",
                            "category": "bug",
                            "title": "Ordinary review finding",
                            "body": "Keep this ordinary finding.",
                            "confidence": 0.95,
                        }
                    ]
                }
            )
        return '{"comments":[]}'

    model.review = AsyncMock(side_effect=review)
    critique = AsyncMock(side_effect=lambda _llm, comments, **kwargs: comments)
    monkeypatch.setattr("mira.core.engine.self_critique", critique)
    result = await ReviewEngine(config, model).review_diff(snapshot.diff(), local_snapshot=snapshot)

    assert model.review.await_count == 2
    assert result.failed_paths == []
    assert [c.title for c in critique.await_args.args[1]] == ["Ordinary review finding"]
    assert [c.title for c in result.comments if c.source_pass == "main"] == [
        "Ordinary review finding"
    ]
    findings = [c for c in result.comments if c.source_pass == "python-call"]
    assert len(findings) == verified
    if findings:
        assert (findings[0].path, findings[0].line) == ("caller.py", 4)
        assert "service.py:1" in findings[0].body


async def test_snapshot_reads_only_regular_files_from_captured_head(checkout: Path):
    snapshot = GitSnapshot("main")
    (checkout / "src/caller.py").write_text("NEW_HEAD\n")
    _git(checkout, "commit", "-am", "Update the caller")
    (checkout / "untracked.py").write_text("UNTRACKED_SOURCE\n")

    assert "greet('Ada')" in (await snapshot.fetch("src/caller.py") or "")
    assert "NEW_HEAD" not in snapshot.diff()
    for path in ("link.py", "untracked.py", "../outside.py", str(checkout / "src/caller.py")):
        assert await snapshot.fetch(path) is None


async def test_local_call_verification_finds_unchanged_importer(tmp_path, monkeypatch):
    _git(tmp_path, "init", "--initial-branch=main", "--template=")
    service = tmp_path / "service.py"
    caller = tmp_path / "caller.py"
    service.write_text("def greet(\n    name,\n    prefix,\n):\n    return name\n")
    source = (
        "from service import greet\n\n"
        "class Caller:\n"
        "    def run(self, name):\n"
        "        result = greet(\n"
        "            name, self.prefix,\n"
        "        )\n"
        "        return result\n"
    )
    caller.write_text(source)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "Add caller")
    _git(tmp_path, "switch", "-c", "change")
    service.write_text("def greet(\n    name,\n):\n    return name\n")
    _git(tmp_path, "commit", "-am", "Remove the prefix parameter")
    monkeypatch.chdir(tmp_path)
    snapshot = GitSnapshot("main")
    caller.write_text(source.replace("name, self.prefix,", "name,"))
    assert await snapshot.unchanged_importers(["service"]) == ["caller.py"]
    assert await snapshot.unchanged_importers(["not_a_module"]) == []
    assert _git(tmp_path, "diff", "--name-only", "main...HEAD") == "service.py"

    config = MiraConfig()
    config.review.python_call_verification = True
    config.review.agentic_tools = config.review.security_pass = False
    config.review.walkthrough = config.review.include_summary = False
    model = MagicMock()
    model.count_tokens.return_value = 400
    model.review = AsyncMock(return_value='{"comments":[]}')
    model.usage = {}
    result = await ReviewEngine(config, model).review_diff(snapshot.diff(), local_snapshot=snapshot)

    (finding,) = result.comments
    assert (finding.path, finding.line) == ("service.py", 3)
    assert "caller.py:5" in finding.body
    assert "too many positional arguments" in finding.body
    assert result.total_chunks == model.review.await_count == 1


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
