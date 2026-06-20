from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mira.config import LLMConfig
from mira.llm import create_llm
from mira.llm.codex_cli import CodexCLIProvider


class TestCodexCLIProvider:
    def test_factory_selects_codex_cli_provider(self):
        provider = create_llm(LLMConfig(provider="codex-cli", model="gpt-5-codex"))
        assert isinstance(provider, CodexCLIProvider)

    def test_command_uses_stdin_oauth_cli_not_http_api(self):
        provider = CodexCLIProvider(
            LLMConfig(
                provider="codex-cli",
                model="gpt-5-codex",
                codex_command="/usr/local/bin/codex",
                codex_sandbox="read-only",
            )
        )
        cmd = provider._command("/tmp/out.txt")
        assert cmd == [
            "/usr/local/bin/codex",
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--output-last-message",
            "/tmp/out.txt",
            "--ephemeral",
            "-m",
            "gpt-5-codex",
            "-",
        ]

    def test_command_omits_model_for_codex_default(self):
        provider = CodexCLIProvider(LLMConfig(provider="codex-cli", model="codex-default"))
        cmd = provider._command("/tmp/out.txt")
        assert "-m" not in cmd
        assert cmd[-1] == "-"

    def test_extracts_fenced_json(self):
        provider = CodexCLIProvider(LLMConfig(provider="codex-cli"))
        assert provider._extract_json_object('Here you go:\n```json\n{"comments": []}\n```') == (
            '{"comments": []}'
        )

    @pytest.mark.asyncio
    async def test_complete_json_mode_returns_json_only(self):
        provider = CodexCLIProvider(LLMConfig(provider="codex-cli"))
        provider._run_codex = AsyncMock(return_value='text before {"ok": true} text after')  # type: ignore[method-assign]

        result = await provider.complete([{"role": "user", "content": "return json"}])

        assert result == '{"ok": true}'
        provider._run_codex.assert_awaited_once()
        assert provider.total_prompt_tokens > 0
        assert provider.total_completion_tokens > 0

    @pytest.mark.asyncio
    async def test_complete_with_tools_prompts_for_tool_arguments_json(self):
        provider = CodexCLIProvider(LLMConfig(provider="codex-cli"))
        provider._run_codex = AsyncMock(return_value='{"comments": [], "summary": "ok"}')  # type: ignore[method-assign]
        tool = {
            "type": "function",
            "function": {
                "name": "submit_review",
                "parameters": {
                    "type": "object",
                    "properties": {"comments": {"type": "array"}, "summary": {"type": "string"}},
                    "required": ["comments", "summary"],
                },
            },
        }

        result = await provider.complete_with_tools(
            [{"role": "user", "content": "review this"}], tools=[tool]
        )

        await_args = provider._run_codex.await_args
        assert await_args is not None
        prompt = await_args.args[0]
        assert "Return ONLY a JSON object containing the arguments for `submit_review`" in prompt
        assert '"required": [' in prompt
        assert result == '{"comments": [], "summary": "ok"}'

    @pytest.mark.asyncio
    async def test_complete_agentic_returns_content_only_for_mira_fallback(self):
        provider = CodexCLIProvider(LLMConfig(provider="codex-cli"))
        provider._run_codex = AsyncMock(return_value='{"comments": []}')  # type: ignore[method-assign]

        msg = await provider.complete_agentic([{"role": "user", "content": "hi"}], tools=[])

        assert msg == {"content": '{"comments": []}', "tool_calls": []}
