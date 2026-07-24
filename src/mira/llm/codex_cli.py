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
import signal
import tempfile
from contextlib import suppress
from pathlib import Path

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from mira.config import LLMConfig
from mira.exceptions import LLMError

logger = logging.getLogger(__name__)

_SAFE_ENV_KEYS = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
    }
)


class CodexCLIProvider:
    """LLM provider that shells out to OpenAI Codex CLI.

    Auth is provided by Codex itself, normally via ``$CODEX_HOME/auth.json`` from
    ``codex login``. No OpenAI API key is read or sent by this provider.
    """

    supports_json_mode: bool = True
    supports_tool_calling: bool = False
    supports_temperature: bool = False
    supports_max_tokens: bool = False

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

    def _env(self, runtime_home: str, runtime_codex_home: str) -> dict[str, str]:
        """Build a minimal child environment without Mira service credentials."""
        env = {key: value for key, value in os.environ.items() if key in _SAFE_ENV_KEYS}
        env["HOME"] = runtime_home
        env["CODEX_HOME"] = runtime_codex_home
        return env

    def _prepare_codex_home(self, invocation_root: str) -> str:
        """Create writable ephemeral Codex state containing only OAuth auth."""
        destination = Path(invocation_root) / "codex-home"
        destination.mkdir(parents=True, mode=0o700)
        source_home = self.config.codex_home or os.environ.get("CODEX_HOME")
        if source_home:
            source_auth = Path(source_home).expanduser() / "auth.json"
            if not source_auth.is_file():
                raise LLMError(f"Codex auth file not found: {source_auth}")
            destination_auth = destination / "auth.json"
            destination_auth.write_bytes(source_auth.read_bytes())
            destination_auth.chmod(0o600)
        return str(destination)

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
            "--ignore-user-config",
            "--ignore-rules",
            "-c",
            'shell_environment_policy.inherit="none"',
        ]
        if self.config.model not in {"", "default", "codex-default"}:
            cmd.extend(["-m", self.config.model])
        cmd.append("-")
        return cmd

    async def _terminate_process_tree(self, proc: asyncio.subprocess.Process) -> None:
        """Terminate Codex and model-spawned descendants before returning."""
        if os.name == "posix":
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                if proc.returncode is None:
                    with suppress(ProcessLookupError):
                        proc.kill()
        elif proc.returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
        if proc.returncode is None:
            await proc.wait()

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type(LLMError),
        reraise=True,
    )
    async def _run_codex(self, prompt: str) -> str:
        with tempfile.TemporaryDirectory(prefix="mira-codex-") as tmpdir:
            output_path = str(Path(tmpdir) / "last-message.txt")
            runtime_home = str(Path(tmpdir) / "runtime")
            Path(runtime_home).mkdir(mode=0o700)
            runtime_codex_home = self._prepare_codex_home(tmpdir)
            cmd = self._command(output_path)
            logger.debug("Running Codex CLI provider: %s", shlex.join(cmd[:-1] + ["<stdin>"]))
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._env(runtime_home, runtime_codex_home),
                    cwd=runtime_home,
                    start_new_session=os.name == "posix",
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
                await self._terminate_process_tree(proc)
                raise LLMError(
                    f"Codex CLI timed out after {self.config.codex_timeout_seconds}s"
                ) from exc
            except BaseException:
                await self._terminate_process_tree(proc)
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

        def is_object(value: str) -> bool:
            try:
                return isinstance(json.loads(value), dict)
            except Exception:
                return False

        if "```" in candidate:
            blocks = candidate.split("```")
            for block in blocks[1::2]:
                block = block.strip()
                if block.startswith("json"):
                    block = block[4:].strip()
                if is_object(block):
                    return block

        try:
            parsed_candidate = json.loads(candidate)
        except Exception:
            parsed_candidate = None
        else:
            if isinstance(parsed_candidate, dict):
                return candidate
            raise LLMError(
                f"Codex CLI response must be a JSON object, got {type(parsed_candidate).__name__}"
            )

        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            obj = candidate[start : end + 1]
            try:
                parsed = json.loads(obj)
            except Exception as exc:
                raise LLMError(
                    f"Codex CLI returned malformed JSON: {exc}: {candidate[:1000]}"
                ) from exc
            if isinstance(parsed, dict):
                return obj

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
        # Codex CLI does not expose Mira's incremental OpenAI-style tool calls.
        # Defer immediately so the caller performs exactly one forced review.
        return {"content": "", "tool_calls": []}

    async def review(self, messages: list[dict[str, str]], temperature: float | None = None) -> str:
        from mira.llm.provider import SUBMIT_REVIEW_TOOL

        return await self.complete_with_tools(
            messages, tools=[SUBMIT_REVIEW_TOOL], temperature=temperature
        )

    async def walkthrough(self, messages: list[dict[str, str]]) -> str:
        from mira.llm.provider import SUBMIT_WALKTHROUGH_TOOL

        return await self.complete_with_tools(messages, tools=[SUBMIT_WALKTHROUGH_TOOL])
