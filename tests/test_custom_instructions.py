"""Tests for review.custom_instructions: config merging and the Operator Instructions block."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import mira.config as mira_config
from mira.config import MAX_CUSTOM_INSTRUCTIONS_CHARS, MiraConfig, load_config, set_global_defaults
from mira.core.passes import (
    dependency_review_pass,
    generate_pr_summary,
    regenerate_summary,
    security_review_pass,
)
from mira.core.threads import verify_fixes
from mira.dashboard.api import GlobalSettingsUpdate
from mira.dashboard.db import AppDatabase
from mira.dashboard.routers.admin import set_global_settings
from mira.exceptions import ConfigError
from mira.llm.prompt_cache import split_system_prompt
from mira.llm.prompts.operator import render_operator_instructions, with_operator_instructions
from mira.llm.prompts.overlap import build_overlap_prompt
from mira.llm.prompts.review import (
    build_conversation_prompt,
    build_dependency_review_prompt,
    build_review_prompt,
    build_security_review_prompt,
    build_walkthrough_prompt,
)
from mira.llm.prompts.verify_fixes import build_verify_fixes_prompt
from mira.models import (
    FileChangeType,
    FileDiff,
    HunkInfo,
    OpenPRRef,
    PRFingerprint,
    PRInfo,
    ReviewComment,
    Severity,
    UnresolvedThread,
    WalkthroughFileEntry,
    WalkthroughResult,
)

INSTRUCTIONS = "Keep every comment concise and use a formal tone."
HEADER = "## Operator Instructions"


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    saved = mira_config._global_defaults
    mira_config._global_defaults = {}
    monkeypatch.setattr("mira.dashboard.api._app_db", None)
    monkeypatch.chdir(tmp_path)
    try:
        yield
    finally:
        mira_config._global_defaults = saved


def _config(instructions: str = "") -> MiraConfig:
    return MiraConfig.model_validate({"review": {"custom_instructions": instructions}})


def _files() -> list[FileDiff]:
    return [
        FileDiff(
            path="app.py",
            change_type=FileChangeType.MODIFIED,
            hunks=[HunkInfo(1, 2, 1, 2, "@@ -1,2 +1,2 @@\n-old\n+new")],
            language="python",
            added_lines=1,
            deleted_lines=1,
        )
    ]


def _manifest() -> list[FileDiff]:
    return [
        FileDiff(
            path="package.json",
            change_type=FileChangeType.MODIFIED,
            hunks=[HunkInfo(1, 1, 1, 2, '@@ -1,1 +1,2 @@\n+"axios": "1.0.0"')],
            language="json",
            added_lines=1,
        )
    ]


def _threads() -> list[tuple[str, str, list[UnresolvedThread]]]:
    return [("app.py", "1| new", [UnresolvedThread("T1", "app.py", 1, "**Bug**\n\nold value")])]


def _overlap_args() -> tuple:
    pr = PRInfo("t", "", "main", "f", "u", 1, "o", "r")
    current = PRFingerprint(pr_number=1, head_sha="a", title="t", body="", paths=["app.py"])
    ref = OpenPRRef(number=2, title="u", body="", head_sha="b", author="bob")
    other = PRFingerprint(pr_number=2, head_sha="b", title="u", body="", paths=["app.py"])
    return pr, current, [(ref, other, ["app.py"])]


def _system_text(messages: list[dict]) -> str:
    return "\n\n".join(m["content"] for m in messages if m["role"] == "system")


# Each builder called with and without instructions.
_BUILDERS = {
    "review": lambda ci: build_review_prompt(_files(), _config(ci), pr_title="t"),
    "security_review": lambda ci: build_security_review_prompt(
        _files(), pr_title="t", custom_instructions=ci
    ),
    "dependency_review": lambda ci: build_dependency_review_prompt(
        _manifest(), ["requests"], pr_title="t", custom_instructions=ci
    ),
    "walkthrough": lambda ci: build_walkthrough_prompt(_files(), _config(ci), pr_title="t"),
    "conversation": lambda ci: build_conversation_prompt(
        "why?", "diff", pr_title="t", custom_instructions=ci
    ),
    "overlap": lambda ci: build_overlap_prompt(*_overlap_args(), custom_instructions=ci),
    "verify_fixes": lambda ci: build_verify_fixes_prompt(_threads(), custom_instructions=ci),
}


class TestOperatorHelper:
    @pytest.mark.parametrize("value", ["", "   \n\t", None])
    def test_blank_renders_nothing(self, value):
        assert render_operator_instructions(value) == ""
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        assert with_operator_instructions(messages, value) is messages

    def test_block_wraps_stripped_text_and_fixes_format_scope(self):
        block = render_operator_instructions(f"  {INSTRUCTIONS}\n")
        assert block.startswith(HEADER)
        assert f"<operator_instructions>\n{INSTRUCTIONS}\n</operator_instructions>" in block
        assert "JSON keys" in block
        assert "code identifiers" in block

    def test_appends_to_last_system_message(self):
        messages = [
            {"role": "system", "content": "stable"},
            {"role": "system", "content": "per-request\n"},
            {"role": "user", "content": "u"},
        ]
        out = with_operator_instructions(messages, INSTRUCTIONS)
        assert out[0] == {"role": "system", "content": "stable"}
        assert out[1]["content"].startswith("per-request\n\n" + HEADER)
        assert INSTRUCTIONS in out[1]["content"]
        assert out[2] == {"role": "user", "content": "u"}
        assert messages[1]["content"] == "per-request\n"

    def test_inserts_leading_system_message_when_none(self):
        messages = [{"role": "user", "content": "u"}]
        out = with_operator_instructions(messages, INSTRUCTIONS)
        assert [m["role"] for m in out] == ["system", "user"]
        assert out[0]["content"] == render_operator_instructions(INSTRUCTIONS)
        assert messages == [{"role": "user", "content": "u"}]


class TestConfig:
    def test_default_is_empty(self):
        assert MiraConfig().review.custom_instructions == ""

    def test_strips_whitespace_and_accepts_null(self):
        assert _config(f"\n  {INSTRUCTIONS}  \n").review.custom_instructions == INSTRUCTIONS
        cfg = MiraConfig.model_validate({"review": {"custom_instructions": None}})
        assert cfg.review.custom_instructions == ""

    def test_length_cap(self):
        assert len(_config("a" * MAX_CUSTOM_INSTRUCTIONS_CHARS).review.custom_instructions) == (
            MAX_CUSTOM_INSTRUCTIONS_CHARS
        )
        with pytest.raises(ValueError):
            _config("a" * (MAX_CUSTOM_INSTRUCTIONS_CHARS + 1))

    def test_global_defaults_then_repo_yaml(self, tmp_path: Path):
        global_file = tmp_path / "mira.yaml"
        global_file.write_text(
            f"review:\n  custom_instructions: {INSTRUCTIONS}\n", encoding="utf-8"
        )
        set_global_defaults(global_file)
        assert load_config().review.custom_instructions == INSTRUCTIONS

        repo_file = tmp_path / "repo" / ".mira.yaml"
        repo_file.parent.mkdir()
        repo_file.write_text("review:\n  custom_instructions: Use English.\n", encoding="utf-8")
        assert load_config(repo_file).review.custom_instructions == "Use English."

    def test_too_long_yaml_is_config_error(self, tmp_path: Path):
        repo_file = tmp_path / ".mira.yaml"
        repo_file.write_text(
            f"review:\n  custom_instructions: {'a' * (MAX_CUSTOM_INSTRUCTIONS_CHARS + 1)}\n"
        )
        with pytest.raises(ConfigError):
            load_config(repo_file)


class TestDashboardOverride:
    @pytest.fixture
    def db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppDatabase:
        monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
        db = AppDatabase(url="", admin_password="admin")
        monkeypatch.setattr("mira.dashboard.api._app_db", db)
        return db

    @staticmethod
    def _admin() -> SimpleNamespace:
        return SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(is_admin=True)))

    def test_put_persists_and_load_config_merges(self, db: AppDatabase, tmp_path: Path):
        global_file = tmp_path / "mira.yaml"
        global_file.write_text("review:\n  custom_instructions: from global\n")
        set_global_defaults(global_file)

        body = GlobalSettingsUpdate(overrides={"review": {"custom_instructions": INSTRUCTIONS}})
        assert set_global_settings(body, self._admin()) == {"ok": True}
        assert db.get_global_review_overrides()["review"]["custom_instructions"] == INSTRUCTIONS
        assert load_config().review.custom_instructions == INSTRUCTIONS

    def test_put_rejects_too_long(self, db: AppDatabase):
        from fastapi import HTTPException

        too_long = "a" * (MAX_CUSTOM_INSTRUCTIONS_CHARS + 1)
        body = GlobalSettingsUpdate(overrides={"review": {"custom_instructions": too_long}})
        with pytest.raises(HTTPException) as exc:
            set_global_settings(body, self._admin())
        assert exc.value.status_code == 400
        assert exc.value.detail["field"] == "review.custom_instructions"
        assert db.get_global_review_overrides() == {}


class TestPromptBuilders:
    @pytest.mark.parametrize("name", sorted(_BUILDERS))
    def test_injected_when_set(self, name):
        messages = _BUILDERS[name](INSTRUCTIONS)
        system = _system_text(messages)
        assert HEADER in system
        assert INSTRUCTIONS in system
        assert all(HEADER not in m["content"] for m in messages if m["role"] != "system")

    @pytest.mark.parametrize("name", sorted(_BUILDERS))
    def test_absent_when_blank(self, name):
        baseline = _BUILDERS[name]("")
        assert all(HEADER not in m["content"] for m in baseline)
        assert _BUILDERS[name]("   ") == baseline

    @pytest.mark.parametrize("name", sorted(_BUILDERS))
    def test_only_appends_to_blank_prompt(self, name):
        baseline = _BUILDERS[name]("")
        assert _BUILDERS[name](INSTRUCTIONS) == with_operator_instructions(baseline, INSTRUCTIONS)

    @pytest.mark.parametrize("name", ["review", "security_review"])
    def test_cached_prefix_unchanged(self, name):
        baseline = _BUILDERS[name]("")
        injected = _BUILDERS[name](INSTRUCTIONS)
        assert len(baseline) == len(injected) == 3
        assert injected[0] == baseline[0]
        assert injected[1]["content"].endswith("</operator_instructions>")

    def test_split_prompt_shape_preserved(self):
        rendered = "head\n<<<MIRA_CACHE_BOUNDARY>>>\ntail"
        messages = [*split_system_prompt(rendered), {"role": "user", "content": "u"}]
        assert [m["role"] for m in with_operator_instructions(messages, INSTRUCTIONS)] == [
            "system",
            "system",
            "user",
        ]


def _capture_llm(raw: str = "") -> MagicMock:
    llm = MagicMock(spec=["complete", "complete_with_tools"])
    llm.complete = AsyncMock(return_value=raw or "summary")
    llm.complete_with_tools = AsyncMock(return_value=raw or '{"comments": []}')
    return llm


def _sent_messages(mock: AsyncMock) -> list[dict]:
    args, kwargs = mock.call_args
    return kwargs.get("messages", args[0] if args else None)


class TestPassWiring:
    @pytest.mark.parametrize("value", [INSTRUCTIONS, ""], ids=["set", "blank"])
    async def test_regenerate_summary(self, value):
        llm = _capture_llm()
        comment = ReviewComment(
            path="app.py",
            line=1,
            end_line=None,
            severity=Severity.WARNING,
            category="bug",
            title="t",
            body="b",
            confidence=0.9,
        )
        await regenerate_summary(
            llm, [comment], [], "t", "", "fb", indexing_llm=llm, custom_instructions=value
        )
        assert (HEADER in _system_text(_sent_messages(llm.complete))) is bool(value)

    @pytest.mark.parametrize("value", [INSTRUCTIONS, ""], ids=["set", "blank"])
    async def test_generate_pr_summary(self, value):
        llm = _capture_llm()
        walkthrough = WalkthroughResult(
            summary="s",
            file_changes=[
                WalkthroughFileEntry(
                    path="app.py", change_type=FileChangeType.MODIFIED, description="d"
                )
            ],
        )
        await generate_pr_summary(
            llm, walkthrough, "t", "", indexing_llm=llm, custom_instructions=value
        )
        assert (HEADER in _system_text(_sent_messages(llm.complete))) is bool(value)

    @pytest.mark.parametrize("value", [INSTRUCTIONS, ""], ids=["set", "blank"])
    async def test_security_pass(self, value):
        llm = _capture_llm()
        await security_review_pass(
            llm, _files(), [], "t", security_llm=llm, custom_instructions=value
        )
        assert (HEADER in _system_text(_sent_messages(llm.complete_with_tools))) is bool(value)

    @pytest.mark.parametrize("value", [INSTRUCTIONS, ""], ids=["set", "blank"])
    async def test_dependency_pass(self, value):
        llm = _capture_llm()
        await dependency_review_pass(
            llm, _manifest(), [], "t", indexing_llm=llm, custom_instructions=value
        )
        assert (HEADER in _system_text(_sent_messages(llm.complete_with_tools))) is bool(value)

    @pytest.mark.parametrize("value", [INSTRUCTIONS, ""], ids=["set", "blank"])
    async def test_verify_fixes(self, value):
        llm = _capture_llm('{"results": []}')
        await verify_fixes(llm, _threads(), value)
        assert (HEADER in _system_text(_sent_messages(llm.complete))) is bool(value)


class TestHandlerWiring:
    @pytest.mark.parametrize("value", [INSTRUCTIONS, ""], ids=["set", "blank"])
    async def test_conversation_reply(self, value):
        from mira.platforms.handlers import run_pr_command

        llm = _capture_llm("answer")
        provider = MagicMock()
        provider.get_repo_permission = AsyncMock(return_value="write")
        provider.get_pr_info = AsyncMock(return_value=_overlap_args()[0])
        provider.get_pr_diff = AsyncMock(return_value="diff")
        provider.post_comment = AsyncMock()
        with (
            patch("mira.platforms.handlers.load_config", return_value=_config(value)),
            patch("mira.platforms.handlers.create_llm", return_value=llm),
        ):
            await run_pr_command(provider, "o", "r", 1, "u", "why?", "alice", "mira")
        assert (HEADER in _system_text(_sent_messages(llm.complete))) is bool(value)

    @pytest.mark.parametrize("value", [INSTRUCTIONS, ""], ids=["set", "blank"])
    async def test_thread_reply(self, value):
        from mira.platforms.handlers import run_thread_reply

        llm = _capture_llm('{"intent": "question", "reply": "Answer"}')
        provider = MagicMock()
        provider.get_repo_permission = AsyncMock(return_value="write")
        provider.reply_to_review_comment = AsyncMock()
        pr_info = MagicMock(owner="o", repo="r", number=1, url="u")
        with (
            patch("mira.platforms.handlers.load_config", return_value=_config(value)),
            patch("mira.platforms.handlers.create_llm", return_value=llm),
            patch("mira.platforms.handlers.llm_config_for"),
        ):
            await run_thread_reply(provider, pr_info, "why?", 1, actor="alice")
        messages = _sent_messages(llm.complete_with_tools)
        assert (HEADER in _system_text(messages)) is bool(value)
        provider.reply_to_review_comment.assert_awaited_once_with(pr_info, 1, "Answer")
