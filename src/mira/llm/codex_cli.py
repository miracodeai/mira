"""Codex CLI-backed provider using the local Codex OAuth session.

This provider intentionally keeps Mira's review contract unchanged: callers pass
Mira's existing chat messages/tool schemas and receive the same JSON strings the
OpenAI-compatible provider would have returned. The only difference is that the
model execution happens through ``codex exec`` and ``CODEX_HOME`` instead of an
HTTP API key.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import tempfile
from pathlib import Path

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from mira.config import LLMConfig
from mira.exceptions import LLMError

logger = logging.getLogger(__name__)


class CodexCLIProvider:
    """LLM provider that shells out to OpenAI Codex CLI.

    Auth is provided by Codex itself, normally via ``$CODEX_HOME/auth.json`` from
    ``codex login``. No OpenAI API key is read or sent by this provider.
    """

    supports_json_mode: bool = True
    supports_tool_calling: bool = False

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    @property
    def usage(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
        }

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4) if text else 0

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        codex_home = self.config.codex_home or env.get("CODEX_HOME")
        if codex_home:
            env["CODEX_HOME"] = str(Path(codex_home).expanduser())
        return env

    def _command(self, output_path: str) -> list[str]:
        codex_command = self.config.codex_command or "codex"
        if any(char in codex_command for char in (" ", "\t", "\n", ";", "|", "&")):
            raise ValueError(
                f"Invalid codex_command: {codex_command!r}. "
                "Set it to a single executable path/name without arguments."
            )
        cmd = [
            codex_command,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            self.config.codex_sandbox,
            "--output-last-message",
            output_path,
            "--ephemeral",
        ]
        if self.config.model not in {"", "default", "codex-default"}:
            cmd.extend(["-m", self.config.model])
        cmd.append("-")
        return cmd

    def _cwd(self) -> str | None:
        if self.config.codex_workdir:
            return str(Path(self.config.codex_workdir).expanduser())
        return None

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type(LLMError),
        reraise=True,
    )
    async def _run_codex(self, prompt: str) -> str:
        with tempfile.TemporaryDirectory(prefix="mira-codex-") as tmpdir:
            output_path = str(Path(tmpdir) / "last-message.txt")
            cmd = self._command(output_path)
            logger.debug("Running Codex CLI provider: %s", shlex.join(cmd[:-1] + ["<stdin>"]))
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._env(),
                    cwd=self._cwd(),
                )
            except FileNotFoundError as exc:
                raise LLMError(
                    f"Codex CLI command not found: {self.config.codex_command!r}. "
                    "Install @openai/codex or set llm.codex_command."
                ) from exc

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(prompt.encode("utf-8")),
                    timeout=self.config.codex_timeout_seconds,
                )
            except TimeoutError as exc:
                proc.kill()
                await proc.wait()
                raise LLMError(
                    f"Codex CLI timed out after {self.config.codex_timeout_seconds}s"
                ) from exc
            except BaseException:
                proc.kill()
                await proc.wait()
                raise

            stdout_text = stdout.decode("utf-8", errors="replace")
            stderr_text = stderr.decode("utf-8", errors="replace")
            output_file = Path(output_path)
            last_message = output_file.read_text(encoding="utf-8") if output_file.exists() else ""

            if proc.returncode != 0:
                detail = (stderr_text or stdout_text or last_message).strip()
                raise LLMError(f"Codex CLI failed with exit {proc.returncode}: {detail[-2000:]}")

            return (last_message or stdout_text).strip()

    def _messages_prompt(self, messages: list[dict]) -> str:
        parts = [
            "You are running as Mira's model backend through Codex CLI.",
            "Follow the Mira review instructions exactly. Do not mention Codex CLI.",
            "Return only the requested final answer; no prose wrappers unless explicitly requested.",
            "",
            "## Mira messages",
        ]
        for i, message in enumerate(messages, 1):
            role = message.get("role", "user")
            content = message.get("content", "")
            parts.append(f"\n### Message {i}: {role}\n{content}")
        return "\n".join(parts)

    def _tool_prompt(self, messages: list[dict], tools: list[dict]) -> str:
        tool = tools[0].get("function", {}) if tools else {}
        tool_name = tool.get("name") or "submit_result"
        schema = tool.get("parameters") or {"type": "object"}
        return (
            self._messages_prompt(messages)
            + "\n\n## Required output\n"
            + f"Return ONLY a JSON object containing the arguments for `{tool_name}`.\n"
            + "Do not wrap the JSON in markdown fences. Do not include explanatory text.\n"
            + "The JSON object must conform to this schema:\n"
            + json.dumps(schema, indent=2, sort_keys=True)
        )

    def _extract_json_object(self, text: str) -> str:
        candidate = text.strip()
        if not candidate:
            raise LLMError("Codex CLI returned an empty response")

        if "```" in candidate:
            blocks = candidate.split("```")
            for block in blocks[1::2]:
                block = block.strip()
                if block.startswith("json"):
                    block = block[4:].strip()
                try:
                    json.loads(block)
                    return block
                except Exception:
                    continue

        try:
            json.loads(candidate)
            return candidate
        except Exception:
            pass

        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            obj = candidate[start : end + 1]
            try:
                json.loads(obj)
                return obj
            except Exception as exc:
                raise LLMError(
                    f"Codex CLI returned malformed JSON: {exc}: {candidate[:1000]}"
                ) from exc

        raise LLMError(f"Codex CLI response did not contain a JSON object: {candidate[:1000]}")

    async def complete(
        self,
        messages: list[dict[str, str]],
        json_mode: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        prompt = self._messages_prompt(messages)
        if json_mode:
            prompt += (
                "\n\n## Required output\n"
                "Return ONLY one valid JSON object. No markdown fences or explanatory text."
            )
        raw = await self._run_codex(prompt)
        self.total_prompt_tokens += self.count_tokens(prompt)
        result = self._extract_json_object(raw) if json_mode else raw
        self.total_completion_tokens += self.count_tokens(result)
        return result

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict],
        temperature: float | None = None,
    ) -> str:
        prompt = self._tool_prompt(messages, tools)
        raw = await self._run_codex(prompt)
        self.total_prompt_tokens += self.count_tokens(prompt)
        result = self._extract_json_object(raw)
        self.total_completion_tokens += self.count_tokens(result)
        return result

    async def complete_agentic(
        self,
        messages: list,
        tools: list[dict],
        temperature: float | None = None,
    ) -> dict:
        # Codex CLI is already agentic but does not expose Mira's incremental
        # OpenAI-style tool-call loop. Return content-only so Mira falls back to
        # the normal forced JSON review call, preserving the output contract.
        raw = await self.complete_with_tools(messages, tools)
        return {"content": raw, "tool_calls": []}

    async def review(self, messages: list[dict[str, str]], temperature: float | None = None) -> str:
        from mira.llm.provider import SUBMIT_REVIEW_TOOL

        return await self.complete_with_tools(
            messages, tools=[SUBMIT_REVIEW_TOOL], temperature=temperature
        )

    async def walkthrough(self, messages: list[dict[str, str]]) -> str:
        from mira.llm.provider import SUBMIT_WALKTHROUGH_TOOL

        return await self.complete_with_tools(messages, tools=[SUBMIT_WALKTHROUGH_TOOL])
