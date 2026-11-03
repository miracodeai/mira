"""MCP server over streamable HTTP at ``/mcp``.

Stateless JSON-RPC: each POST carries a bearer registry JWT and gets a JSON
response (the spec allows ``application/json`` instead of an SSE stream). No
server-initiated messages, so GET returns 405.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from mira.registry.auth import AuthError, actor_from_header
from mira.registry.model import Actor, RegistryError

logger = logging.getLogger(__name__)

router = APIRouter()

SERVER_NAME = "kory-source"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

_STR = {"type": "string"}
_STRS = {"type": "array", "items": {"type": "string"}}
_ANCHOR: dict[str, Any] = {
    "type": "object",
    "properties": {
        "repo": {"type": "string", "description": "owner/name"},
        "path": _STR,
        "qualified_name": _STR,
        "symbol_kind": _STR,
        "anchor_id": {"type": "string", "description": "Symbol anchor id from the index."},
    },
}


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": required},
    }


TOOLS = [
    _tool(
        "put_decision",
        "Record a proposed decision, rule, constraint, requirement, or concept. Needs the "
        "session and branch so PR events can realize it. Confidence, status, decided_by, "
        "pr, and merge_commit are derived; values you send for them are ignored. Returns "
        "any conflict with an approved object, with the quoted clause on each side.",
        {
            "kind": {
                "type": "string",
                "enum": ["decision", "rule", "constraint", "requirement", "concept"],
            },
            "title": _STR,
            "summary": _STR,
            "body": {"type": "string", "description": "The full statement."},
            "scope": {"type": "string", "enum": ["repo", "org"]},
            "repo": {"type": "string", "description": "owner/name of the branch's repository."},
            "session": _STR,
            "branch": _STR,
            "ticket": _STR,
            "rationale": _STR,
            "alternatives": _STRS,
            "basis": {"type": "string", "enum": ["statement", "repository_analysis"]},
            "concepts": _STRS,
            "anchors": {"type": "array", "items": _ANCHOR},
        },
        ["title", "session", "branch", "repo"],
    ),
    _tool(
        "search_decisions",
        "Search by concept, file path, or symbol (and optionally words). Ranked by "
        "confidence. Proposals are included and flagged unless status is set.",
        {
            "query": _STR,
            "concept": _STR,
            "repo": _STR,
            "path": _STR,
            "symbol": _STR,
            "kind": _STR,
            "status": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer"},
        },
        [],
    ),
    _tool(
        "supersede_decision",
        "Mark new_id as replacing old_id. A proposal is replaced at once. An approved "
        "object is replaced when new_id is approved.",
        {"new_id": _STR, "old_id": _STR},
        ["new_id", "old_id"],
    ),
    _tool(
        "get_decision_chain",
        "The supersession chain through an object, oldest first, retired ones included.",
        {"id": _STR},
        ["id"],
    ),
    _tool(
        "link_anchor",
        "Tie an object to a symbol (repo, path, qualified_name) or an index anchor_id.",
        {"id": _STR, **_ANCHOR["properties"]},
        ["id"],
    ),
    _tool(
        "get_context",
        "Call before the first source edit. With work_id, or ticket (a Linear key such as "
        "HAI-123): the plan, then requirements, decisions, children, dependencies, "
        "evidence, and the ticket's comments. With repo plus paths, symbols, or concepts: "
        "claims in play with approval status, realization, and confidence, proposals "
        "flagged.",
        {
            "work_id": _STR,
            "ticket": _STR,
            "repo": _STR,
            "paths": _STRS,
            "symbols": _STRS,
            "concepts": _STRS,
        },
        [],
    ),
    _tool(
        "resolve_conflict",
        "A person answers a conflict on one proposal. keep rejects the new object "
        "(reason conflicts_with_approved). supersede approves it and supersedes the "
        "approved one, with decided_by set to the person. defer leaves the new object "
        "proposed. The answer binds to that proposal's session only. Refused for agent "
        "tokens and for the author.",
        {
            "id": _STR,
            "choice": {"type": "string", "enum": ["keep", "supersede", "defer"]},
        },
        ["id", "choice"],
    ),
    _tool(
        "session_conflicts",
        "Does this session hold a conflict that still blocks push? True until this "
        "session's proposal is answered keep or supersede. Defer lets the session "
        "continue editing, but push and gh pr create stay blocked. A second session "
        "does not inherit the answer. Call before git push or gh pr create.",
        {"session": _STR},
        ["session"],
    ),
    _tool(
        "conflict_prompt_counts",
        "How many conflict-judge prompts have run, per session, and the keep / supersede / "
        "defer split of answers recorded so far.",
        {},
        [],
    ),
    _tool(
        "put_work",
        "Create a work object (goal, scope, plan, owner, delivery_state), or update one by id.",
        {
            "id": _STR,
            "title": _STR,
            "summary": _STR,
            "goal": _STR,
            "work_scope": _STR,
            "plan": _STR,
            "owner": _STR,
            "delivery_state": {
                "type": "string",
                "enum": ["planned", "active", "shipped", "canceled", "archived"],
            },
            "scope": {"type": "string", "enum": ["repo", "org"]},
            "repo": _STR,
            "ticket": _STR,
        },
        [],
    ),
    _tool(
        "link_work",
        "Link work: addresses (requirement), records (decision), parent_of (child work, one "
        "parent), blocks (dependency, no cycles), or relates.",
        {
            "work_id": _STR,
            "target_id": _STR,
            "type": {
                "type": "string",
                "enum": ["addresses", "records", "parent_of", "blocks", "relates"],
            },
        },
        ["work_id", "target_id", "type"],
    ),
    _tool(
        "put_evidence",
        "Record verification evidence for requirements at a tested revision. A merged PR "
        "is not evidence.",
        {
            "requirement_ids": _STRS,
            "tested_revision": _STR,
            "method": _STR,
            "result": {"type": "string", "enum": ["pass", "fail", "inconclusive"]},
            "artifact_url": _STR,
            "summary": _STR,
            "repo": _STR,
            "session": _STR,
        },
        ["requirement_ids", "tested_revision", "method", "result"],
    ),
]


async def call_tool(name: str, args: dict, actor: Actor) -> Any:
    from mira.registry import get_registry

    reg = get_registry()
    assert reg is not None
    if name == "put_decision":
        return await reg.put_object(args, actor)
    if name == "search_decisions":
        return reg.search(
            str(args.get("query") or ""),
            kind=args.get("kind") or None,
            concept=args.get("concept") or None,
            repo=args.get("repo") or None,
            path=args.get("path") or None,
            symbol=args.get("symbol") or None,
            statuses=args.get("status") or None,
            limit=int(args.get("limit") or 20),
        )
    if name == "supersede_decision":
        return reg.supersede(args["new_id"], args["old_id"], actor)
    if name == "get_decision_chain":
        return reg.chain(args["id"])
    if name == "link_anchor":
        anchor = {k: v for k, v in args.items() if k != "id"}
        return reg.link_anchor(args["id"], anchor, actor)
    if name == "get_context":
        if args.get("work_id"):
            return reg.work_context(args["work_id"])
        if args.get("ticket"):
            return reg.work_context(reg.work_by_ticket(str(args["ticket"]))["id"])
        return reg.context_for_paths(
            str(args.get("repo") or ""),
            args.get("paths") or [],
            symbols=args.get("symbols") or [],
            concepts=args.get("concepts") or [],
        )
    if name == "resolve_conflict":
        return reg.resolve_conflict(args["id"], str(args.get("choice") or ""), actor)
    if name == "session_conflicts":
        return reg.session_conflicts(str(args.get("session") or ""))
    if name == "conflict_prompt_counts":
        return reg.conflict_prompt_counts()
    if name == "put_work":
        if args.get("id"):
            return reg.update_work(args["id"], args, actor)
        return await reg.put_work(args, actor)
    if name == "link_work":
        return reg.link_work(args["work_id"], args["target_id"], str(args["type"]), actor)
    if name == "put_evidence":
        return reg.put_evidence(args, actor)
    raise RegistryError(f"unknown tool {name}")


def _result(msg_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


async def handle_message(msg: Any, actor: Actor) -> dict | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
    method, msg_id = msg["method"], msg.get("id")
    if "id" not in msg:
        return None  # notification
    params = msg.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return _result(
            msg_id,
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": "1"},
                "instructions": "Call get_context before the first source edit. Record "
                "decisions with put_decision before announcing them.",
            },
        )
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        name = str(params.get("name") or "")
        if name not in {t["name"] for t in TOOLS}:
            return _error(msg_id, -32602, f"unknown tool {name}")
        try:
            value = await call_tool(name, params.get("arguments") or {}, actor)
        except (RegistryError, KeyError) as exc:
            text = str(exc) if isinstance(exc, RegistryError) else f"missing argument {exc}"
            return _result(msg_id, {"content": [{"type": "text", "text": text}], "isError": True})
        payload = value if isinstance(value, dict) else {"items": value}
        return _result(
            msg_id,
            {
                "content": [{"type": "text", "text": json.dumps(payload, default=str)}],
                "structuredContent": payload,
                "isError": False,
            },
        )
    return _error(msg_id, -32601, f"method not found: {method}")


@router.post("/mcp")
async def mcp_post(request: Request) -> Response:
    try:
        actor = actor_from_header(request.headers.get("authorization", ""))
    except AuthError as exc:
        return JSONResponse(
            {"error": str(exc)},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="kory-source"'},
        )
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(_error(None, -32700, "parse error"), status_code=400)
    if isinstance(body, list):
        replies = [r for r in [await handle_message(m, actor) for m in body] if r is not None]
        return JSONResponse(replies) if replies else Response(status_code=202)
    reply = await handle_message(body, actor)
    return JSONResponse(reply) if reply is not None else Response(status_code=202)


@router.get("/mcp")
@router.delete("/mcp")
async def mcp_other() -> Response:
    return Response(status_code=405, headers={"Allow": "POST"})
