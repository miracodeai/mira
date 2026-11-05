"""LLM provider that sends one prompt to a router-acp seat through the relay.

Public interface for later callers (indexing relink, reviews, the benchmark):

* ``RouterAcpProvider`` implements ``LLMProviderProtocol``.
* ``complete``, ``complete_with_tools``, ``review``, and ``walkthrough``
  return a string.
* ``complete_agentic`` is a JSON tool-call adapter (HAI-12817). Each hop is
  one ordinary completion: the prompt carries the tool schemas and the whole
  call/result history, and the seat answers with exactly one
  ``{"tool": name, "arguments": {...}}`` object. The provider checks the name
  and arguments against the supplied schemas and returns an OpenAI-shaped
  assistant message with one tool call. A reply it cannot use returns no tool
  calls, so the engine falls back to one forced review. The seat never runs
  the tools: Mira's own executor answers ``read_file`` and ``grep_repo``.
* ``supports_tool_calling`` (native tool calling) and ``supports_temperature``
  are false. Ensemble sampling stays off.
* A model pin is a router candidate id (``grok/grok-4.6``). ``auto``,
  ``default``, ``router-default``, and ``router-acp-default`` are unpinned.
  A pin that does not run raises ``router_acp_model_mismatch``. The provider
  never substitutes another model.
* The default transport is HTTP ``POST {router_acp_url}/api/agent/llm-complete``.
  Only a direct ``RouterAcpProvider(config)`` uses it (the benchmark harness
  and its tests). The relay owns ``router-acp`` and its credentials. This
  module does not launch a provider binary and does not read session files.
* For that HTTP path, set ``router_acp_url``, ``router_acp_token``, and
  ``router_acp_session`` in config. Environment variables
  ``MIRA_ROUTER_ACP_URL``, ``MIRA_ROUTER_ACP_TOKEN``, and
  ``MIRA_ROUTER_ACP_SESSION`` apply only when the matching config field is
  unset. ``KORY_CODE_SESSION_ID`` is not read.
* ``relay=False`` sends only ``{"body", "timeout"}`` to the injected
  transport and never reads the relay URL, token, or session. The review API
  uses it to hand each call to the requester that submitted the job
  (HAI-12330). Kory Source stores no relay for reviews.
* ``create_llm`` does not build this provider. It returns
  ``UnconfiguredSeatProvider``, which fails every completion with
  ``seat_provider_unconfigured``. Webhook reviews and indexing call
  ``refuse_unrequested_seat`` first so they fail before any GitHub write.
* ``job_llm`` is a context variable holding the running review job's
  requester-backed provider. The registry judge uses it during a job.
* ``attribution_key`` is a context variable the benchmark sets around one PR.
  ``call_log`` keeps the latest 256 logical completions (review and judge).
* ``call_observer`` is a context variable holding a callback. The review API
  sets it around one job so each job sees only its own completions, however
  many jobs share ``call_log``. A reply the provider cannot parse is reported
  as a ``bad_response`` entry after the ``ok`` transport entry.

Every logical call goes through ``hickory_common.llm_tracing.traced_llm_call``.
An unset ``LLM_OTLP_ENDPOINT``, a missing tracer, or a failed exporter raises
``router_acp_tracing_unavailable``. ``MIRA_ROUTER_ACP_ALLOW_UNTRACED=1`` is the
only untraced opt-out. Tests inject ``tracer`` and ``transport``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any, ClassVar
from urllib.parse import urlparse

import httpx

from mira.config import LLMConfig, _is_local_host
from mira.exceptions import LLMError, NonRetriableLLMError

logger = logging.getLogger(__name__)

UNPINNED_MODELS = frozenset({"", "auto", "default", "router-default", "router-acp-default"})
RETRYABLE_STATES = frozenset({"seat_exhausted", "workstation_outage"})
_CALL_LOG_LIMIT = 256
_STATE_CODE = {
    "seat_exhausted": "router_acp_seat_exhausted",
    "workstation_outage": "router_acp_workstation_outage",
    "model_mismatch": "router_acp_model_mismatch",
    "empty_response": "router_acp_empty_response",
    "timeout": "router_acp_timeout",
    "bad_response": "router_acp_bad_response",
}
_STATUS_KIND = {
    429: "seat_exhausted",
    503: "workstation_outage",
    409: "model_mismatch",
    504: "timeout",
    502: "router_failed",
}

attribution_key: ContextVar[str] = ContextVar("mira_router_acp_attribution", default="")
call_observer: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "mira_router_acp_call_observer", default=None
)
job_llm: ContextVar[Any] = ContextVar("mira_router_acp_job_llm", default=None)
_tracing_ready = False


class RouterTransportError(Exception):
    """One failed HTTP attempt. ``kind`` is a visible completion state."""

    def __init__(
        self, message: str, *, kind: str = "router_failed", body: dict | None = None
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.body = body or {}


def _import_tracing() -> tuple[Callable[..., None], Callable[..., Awaitable[Any]]]:
    from hickory_common.llm_tracing import setup_otlp_tracing, traced_llm_call

    return setup_otlp_tracing, traced_llm_call


def _note_span(requested: str, actual: str | None = None) -> None:
    try:
        from opentelemetry import trace
    except ImportError:
        return
    span = trace.get_current_span()
    try:
        span.set_attribute("gen_ai.request.model", actual or requested)
        span.set_attribute(
            "hickory.metadata",
            json.dumps({"requested_model": requested, "actual_model": actual or ""}),
        )
    except Exception:
        logger.debug("router span attribute skipped", exc_info=True)


def _untraced_allowed() -> bool:
    return os.environ.get("MIRA_ROUTER_ACP_ALLOW_UNTRACED", "").strip() == "1"


def _shared_tracer() -> Callable[..., Awaitable[Any]]:
    global _tracing_ready
    endpoint = os.environ.get("LLM_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        raise LLMError(
            "router_acp_tracing_unavailable",
            detail="LLM_OTLP_ENDPOINT is unset",
        )
    try:
        setup_otlp_tracing, traced_llm_call = _import_tracing()
    except ImportError as exc:
        raise LLMError(
            "router_acp_tracing_unavailable",
            detail="hickory_common.llm_tracing is not installed",
        ) from exc
    if not _tracing_ready:
        try:
            setup_otlp_tracing(
                endpoint=endpoint,
                service_name="kory-source",
                token=os.environ.get("CLAUDE_OTEL_SHARED_TOKEN"),
                environment=os.environ.get("ENVIRONMENT") or None,
            )
        except Exception as exc:
            raise LLMError("router_acp_tracing_unavailable", detail=str(exc)[:300]) from exc
        _tracing_ready = True
    return traced_llm_call


def _checked_relay_url(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme not in {"http", "https"} or not host:
        raise LLMError("router_acp_bad_response", detail="relay URL scheme or host is invalid")
    if parsed.scheme == "http" and not _is_local_host(host):
        raise LLMError(
            "router_acp_bad_response",
            detail=f"relay URL must be https or local http, got http://{host}",
        )
    return url.rstrip("/")


class RouterAcpProvider:
    """One Mira completion per relay request. The relay opens the seat."""

    supports_json_mode: bool = True
    supports_tool_calling: bool = False
    supports_temperature: bool = False
    supports_max_tokens: bool = False
    call_log: ClassVar[deque[dict[str, Any]]] = deque(maxlen=_CALL_LOG_LIMIT)

    def __init__(
        self,
        config: LLMConfig,
        *,
        transport: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        tracer: Callable[..., Awaitable[Any]] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        relay: bool = True,
    ) -> None:
        self.config = config
        self._relay = relay
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self._transport = transport or self._http_transport
        self._tracer = tracer
        self._sleep = sleep or asyncio.sleep

    @property
    def usage(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
        }

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4) if text else 0

    def _requested_model(self) -> str:
        model = (self.config.model or "").strip()
        if model in {"", "auto", "default"}:
            return "auto"
        return model

    def _priority(self) -> str:
        return "interactive" if self.config.router_acp_priority == "interactive" else "review"

    def _relay_url(self) -> str:
        configured = self.config.router_acp_url
        raw = configured or os.environ.get("MIRA_ROUTER_ACP_URL") or "http://127.0.0.1:8790"
        return _checked_relay_url(raw)

    def _header_value(self, configured: str | None, env_name: str) -> str:
        if configured:
            return configured
        return os.environ.get(env_name) or ""

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        token = self._header_value(self.config.router_acp_token, "MIRA_ROUTER_ACP_TOKEN")
        session = self._header_value(self.config.router_acp_session, "MIRA_ROUTER_ACP_SESSION")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if session:
            headers["X-Kory-Agent-Session"] = session
        return headers

    def _record(self, *, state: str, requested: str, actual: str, attempts: int) -> None:
        entry = {
            "state": state,
            "requested_model": requested,
            "actual_model": actual,
            "attempts": attempts,
            "priority": self._priority(),
            "attribution": attribution_key.get(),
        }
        self.call_log.append(entry)
        observer = call_observer.get()
        if observer is not None:
            observer(dict(entry))

    def _checked_json(self, raw: str) -> str:
        try:
            return self._extract_json_object(raw)
        except LLMError:
            self._record(
                state="bad_response",
                requested=self._requested_model(),
                actual="unknown",
                attempts=1,
            )
            raise

    def _fail(
        self,
        state: str,
        *,
        requested: str,
        actual: str,
        detail: str,
        attempts: int,
    ) -> NonRetriableLLMError:
        self._record(state=state, requested=requested, actual=actual, attempts=attempts)
        code = _STATE_CODE.get(state, "router_acp_failed")
        return NonRetriableLLMError(
            code,
            detail=detail[:500],
            requested=requested,
            actual=actual,
            seconds=self.config.router_acp_timeout_seconds,
        )

    async def _http_transport(self, request: dict[str, Any]) -> dict[str, Any]:
        timeout = httpx.Timeout(request["timeout"] + 30, connect=10.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    request["url"], headers=request["headers"], json=request["body"]
                )
        except httpx.TimeoutException as exc:
            raise RouterTransportError("timed out", kind="timeout") from exc
        except httpx.TransportError as exc:
            raise RouterTransportError(str(exc), kind="workstation_outage") from exc
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if response.status_code >= 400:
            state = str(
                payload.get("state") or _STATUS_KIND.get(response.status_code, "router_failed")
            )
            detail = str(payload.get("detail") or payload.get("error") or state)
            raise RouterTransportError(detail, kind=state, body=payload)
        return payload

    async def _post_once(self, prompt: str, requested: str) -> dict[str, Any]:
        body = {
            "prompt": prompt,
            "model": requested,
            "priority": self._priority(),
            "timeout_seconds": self.config.router_acp_timeout_seconds,
        }
        request: dict[str, Any] = {
            "body": body,
            "timeout": self.config.router_acp_timeout_seconds,
        }
        if self._relay:
            request["url"] = f"{self._relay_url()}/api/agent/llm-complete"
            request["headers"] = self._headers()
        return await self._transport(request)

    async def _complete_raw(self, prompt: str, requested: str) -> dict[str, Any]:
        attempts = 0
        extra = self.config.router_acp_max_retries
        while True:
            attempts += 1
            try:
                payload = await self._post_once(prompt, requested)
            except RouterTransportError as exc:
                actual = str(exc.body.get("actual_model") or "unknown")
                if exc.kind in RETRYABLE_STATES and attempts <= extra:
                    logger.info(
                        "router completion retry state=%s requested=%s attempt=%s",
                        exc.kind,
                        requested,
                        attempts,
                    )
                    await self._sleep(0.2 * attempts)
                    continue
                raise self._fail(
                    exc.kind,
                    requested=requested,
                    actual=actual,
                    detail=str(exc),
                    attempts=attempts,
                ) from exc
            actual = str(payload.get("actual_model") or "unknown")
            text = str(payload.get("text") or "")
            pinned = requested not in UNPINNED_MODELS
            if pinned and actual != requested:
                raise self._fail(
                    "model_mismatch",
                    requested=requested,
                    actual=actual,
                    detail=f"requested {requested}, actual {actual}",
                    attempts=attempts,
                )
            if not text.strip():
                raise self._fail(
                    "empty_response",
                    requested=requested,
                    actual=actual,
                    detail="empty completion",
                    attempts=attempts,
                )
            self._record(state="ok", requested=requested, actual=actual, attempts=attempts)
            logger.info(
                "router completion state=ok requested=%s actual=%s attempts=%s",
                requested,
                actual,
                attempts,
            )
            return payload

    def _resolve_tracer(self) -> Callable[..., Awaitable[Any]] | None:
        if self._tracer is not None:
            return self._tracer
        if _untraced_allowed():
            return None
        return _shared_tracer()

    async def _generate(self, prompt: str) -> tuple[str, dict[str, Any] | None]:
        requested = self._requested_model()
        tracer = self._resolve_tracer()

        async def call() -> dict[str, Any]:
            _note_span(requested)
            payload = await self._complete_raw(prompt, requested)
            actual = str(payload.get("model") or payload.get("actual_model") or "unknown")
            _note_span(requested, actual)
            return {
                "model": actual,
                "text": str(payload.get("text") or ""),
                "usage": payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
            }

        if tracer is None:
            traced = await call()
        else:
            traced = await tracer(
                call,
                name="kory_source.router_acp.complete",
                prompt=prompt,
                provider="router-acp",
                model_id=None,
                app_surface="kory-source",
                metadata={"requested_model": requested, "provider": "router-acp"},
            )
        usage = traced.get("usage") if isinstance(traced, dict) else None
        text = str(traced.get("text") or "") if isinstance(traced, dict) else str(traced)
        return text, usage if isinstance(usage, dict) else None

    def _account(self, prompt: str, result: str, usage: dict[str, Any] | None) -> None:
        prompt_tokens = usage.get("input_tokens") if usage else None
        completion_tokens = usage.get("output_tokens") if usage else None
        if isinstance(prompt_tokens, int):
            self.total_prompt_tokens += prompt_tokens
        else:
            self.total_prompt_tokens += self.count_tokens(prompt)
        if isinstance(completion_tokens, int):
            self.total_completion_tokens += completion_tokens
        else:
            self.total_completion_tokens += self.count_tokens(result)

    def _messages_prompt(self, messages: list[dict]) -> str:
        parts = [
            "You are running as Mira's model backend through a router-acp seat.",
            "Follow the Mira review instructions exactly.",
            "Return only the requested final answer.",
            "",
            "## Mira messages",
        ]
        for index, message in enumerate(messages, 1):
            role = message.get("role", "user")
            content = message.get("content", "")
            parts.append(f"\n### Message {index}: {role}\n{content}")
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
            raise LLMError("router_acp_empty_response", detail="empty completion")

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
                "router_acp_bad_response",
                detail=f"JSON {type(parsed_candidate).__name__} is not an object",
            )

        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            obj = candidate[start : end + 1]
            try:
                parsed = json.loads(obj)
            except Exception as exc:
                raise LLMError("router_acp_bad_response", detail=str(exc)[:500]) from exc
            if isinstance(parsed, dict):
                return obj
        raise LLMError("router_acp_bad_response", detail=candidate[:500])

    async def complete(
        self,
        messages: list[dict[str, str]],
        json_mode: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        prompt = self._messages_prompt(messages)
        if json_mode:
            prompt += "\n\n## Required output\nReturn ONLY one valid JSON object. No markdown fences or explanatory text."
        raw, usage = await self._generate(prompt)
        result = self._checked_json(raw) if json_mode else raw
        self._account(prompt, result, usage)
        return result

    async def complete_with_tools(
        self,
        messages: list[dict[str, str]],
        tools: list[dict],
        temperature: float | None = None,
    ) -> str:
        prompt = self._tool_prompt(messages, tools)
        raw, usage = await self._generate(prompt)
        result = self._checked_json(raw)
        self._account(prompt, result, usage)
        return result

    async def complete_agentic(
        self,
        messages: list,
        tools: list[dict],
        temperature: float | None = None,
    ) -> dict:
        """One agentic hop: ask the seat for one tool call, as JSON.

        Returns ``{"role": "assistant", "content": "", "tool_calls": [call]}``,
        or no tool calls when the reply is not one valid call. The caller's loop
        owns hop caps, tool execution, and the forced-review fallback.
        """
        specs = _tool_specs(tools)
        if not specs:
            return {"role": "assistant", "content": "", "tool_calls": []}
        prompt = self._agentic_prompt(messages, specs)
        raw, usage = await self._generate(prompt)
        self._account(prompt, raw, usage)
        try:
            name, arguments = _parse_tool_call(self._extract_json_object(raw), specs)
        except LLMError as exc:
            self._record(
                state="bad_response",
                requested=self._requested_model(),
                actual="unknown",
                attempts=1,
            )
            logger.info("router agentic reply unusable: %s", exc)
            return {"role": "assistant", "content": "", "tool_calls": []}
        call_id = f"router_call_{_prior_tool_calls(messages) + 1}"
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }

    def _agentic_prompt(self, messages: list, specs: dict[str, dict]) -> str:
        parts = [
            "You are running as Mira's model backend through a router-acp seat.",
            "Follow the Mira review instructions exactly.",
            "Mira runs the tools listed below for you and returns their results in the next request.",
            "Do not use any other tools, files, or commands of your own.",
            "",
            "## Mira messages",
        ]
        for index, message in enumerate(messages, 1):
            role = message.get("role", "user")
            content = message.get("content") or ""
            if role == "tool":
                call_id = message.get("tool_call_id") or ""
                parts.append(f"\n### Message {index}: tool result for `{call_id}`\n{content}")
                continue
            parts.append(f"\n### Message {index}: {role}\n{content}".rstrip())
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                parts.append(
                    f"Tool call `{call.get('id') or ''}`: "
                    + json.dumps({"tool": fn.get("name"), "arguments": _loads_args(fn)})
                )
        parts += ["", "## Tools"]
        for name, spec in specs.items():
            parts.append(f"\n### `{name}`\n{spec.get('description') or ''}".rstrip())
            parts.append("Arguments schema:\n" + json.dumps(spec["parameters"], sort_keys=True))
        parts += [
            "",
            "## Required output",
            "Choose exactly one tool call as your next action.",
            'Return ONLY one JSON object: {"tool": "<tool name>", "arguments": {...}}.',
            "The arguments must conform to that tool's schema.",
            "Do not wrap the JSON in markdown fences. Do not include explanatory text.",
        ]
        return "\n".join(parts)

    async def review(self, messages: list[dict[str, str]], temperature: float | None = None) -> str:
        from mira.llm.tool_schemas import SUBMIT_REVIEW_TOOL

        return await self.complete_with_tools(
            messages, tools=[SUBMIT_REVIEW_TOOL], temperature=temperature
        )

    async def walkthrough(self, messages: list[dict[str, str]]) -> str:
        from mira.llm.tool_schemas import SUBMIT_WALKTHROUGH_TOOL

        return await self.complete_with_tools(messages, tools=[SUBMIT_WALKTHROUGH_TOOL])


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def _tool_specs(tools: list[dict]) -> dict[str, dict]:
    specs: dict[str, dict] = {}
    for tool in tools or []:
        fn = tool.get("function") or {}
        name = fn.get("name")
        if name:
            specs[name] = {
                "description": fn.get("description") or "",
                "parameters": fn.get("parameters") or {"type": "object"},
            }
    return specs


def _prior_tool_calls(messages: list) -> int:
    return sum(len(m.get("tool_calls") or []) for m in messages if m.get("role") == "assistant")


def _loads_args(fn: dict) -> Any:
    raw = fn.get("arguments")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return raw
    return raw if raw is not None else {}


def _type_ok(value: Any, expected: Any) -> bool:
    names = expected if isinstance(expected, list) else [expected]
    for name in names:
        if name == "null":
            if value is None:
                return True
            continue
        kinds = _JSON_TYPES.get(name)
        if kinds is None:
            return True  # a type this adapter does not check
        if isinstance(value, bool) and name in {"integer", "number"}:
            continue
        if isinstance(value, kinds):
            return True
    return False


def _parse_tool_call(text: str, specs: dict[str, dict]) -> tuple[str, dict]:
    """``{"tool": name, "arguments": {...}}`` checked against ``specs``."""
    obj = json.loads(text)
    name = obj.get("tool")
    if not isinstance(name, str) or name not in specs:
        raise LLMError("router_acp_bad_response", detail=f"unknown tool {name!r}")
    arguments = obj.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except Exception as exc:
            raise LLMError("router_acp_bad_response", detail=f"{name}: arguments not JSON") from exc
    if not isinstance(arguments, dict):
        raise LLMError("router_acp_bad_response", detail=f"{name}: arguments not an object")
    schema = specs[name]["parameters"]
    for key in schema.get("required") or []:
        if key not in arguments:
            raise LLMError("router_acp_bad_response", detail=f"{name}: missing {key}")
    properties = schema.get("properties") or {}
    for key, value in arguments.items():
        expected = (properties.get(key) or {}).get("type")
        if expected is not None and not _type_ok(value, expected):
            raise LLMError("router_acp_bad_response", detail=f"{name}: {key} is not {expected}")
    return name, arguments


def _is_router_acp(config: LLMConfig) -> bool:
    return config.provider in {"router-acp", "router_acp"}


def refuse_unrequested_seat(config: LLMConfig, caller: str) -> None:
    """Raise before a caller with no requester would use a router-acp seat."""
    if _is_router_acp(config):
        raise LLMError(
            "seat_provider_unconfigured",
            detail=f"{caller} has no requester to run router-acp calls",
        )


class UnconfiguredSeatProvider(RouterAcpProvider):
    """What ``create_llm`` returns for router-acp: no relay, no requester."""

    def __init__(self, config: LLMConfig) -> None:
        super().__init__(config, transport=self._refuse, relay=False)

    async def _refuse(self, request: dict[str, Any]) -> dict[str, Any]:
        raise LLMError("seat_provider_unconfigured", detail="no requester for this call")

    async def _generate(self, prompt: str) -> tuple[str, dict[str, Any] | None]:
        raise LLMError("seat_provider_unconfigured", detail="no requester for this call")
