"""The review prompt block and the ``search_decisions`` agentic tool.

Only approved objects reach a review: the block and the tool both filter on
``status = 'approved'``. Proposals stay out of review context entirely.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

BLOCK_TITLE = "Kory Source registry (approved decisions and rules)"
_MAX_ENTRIES = 25
_MAX_TOOL_HITS = 10

SEARCH_DECISIONS_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search_decisions",
        "description": (
            "Search the Kory Source registry for approved decisions, rules, constraints, "
            "and requirements. Filter by concept, file path, or symbol in this repository, "
            "or by words in the statement. If a finding rests on a result, cite it as "
            "`(registry: reg_…)`."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": ["string", "null"], "description": "Words in the statement."},
                "concept": {"type": ["string", "null"], "description": "Concept name."},
                "path": {"type": ["string", "null"], "description": "Repo-relative file path."},
                "symbol": {
                    "type": ["string", "null"],
                    "description": "Qualified symbol name, e.g. `AuthService.login`.",
                },
                "kind": {
                    "type": ["string", "null"],
                    "description": "decision, rule, constraint, or requirement.",
                },
            },
        },
    },
}


def _line(obj: dict) -> str:
    where = ", ".join(
        f"`{a['path']}`" + (f" `{a['qualified_name']}`" if a["qualified_name"] else "")
        for a in obj.get("anchors") or []
    )
    text = obj["summary"] or obj["body"]
    head = f"- (registry: {obj['id']}) [{obj['kind']}, confidence {obj['confidence']:.2f}]"
    out = f"{head} **{obj['title']}**"
    if text and text != obj["title"]:
        out += f" — {text.strip()[:400]}"
    if where:
        out += f" (on {where})"
    elif obj["scope"] == "org":
        out += " (org-wide)"
    return out


def render_block(objects: Iterable[dict]) -> dict[str, str] | None:
    approved = [o for o in objects if o["status"] == "approved"][:_MAX_ENTRIES]
    if not approved:
        return None
    lines = [
        "Approved by a person, ranked by confidence. Flag code that violates one. "
        "A finding that rests on an entry must quote its id as `(registry: reg_…)`.",
        "",
        *(_line(o) for o in approved),
    ]
    return {"title": BLOCK_TITLE, "content": "\n".join(lines)}


def registry_rule(pr_info: Any, paths: Iterable[str]) -> dict[str, str] | None:
    """The ``custom_rules`` entry for one review, or None if there is nothing approved."""
    from mira.registry import get_registry

    registry = get_registry(create=False)
    if registry is None:
        return None
    repo = f"{pr_info.owner}/{pr_info.repo}"
    return render_block(registry.context_for_paths(repo, list(paths), include_proposals=False))


def search_approved(registry: Any, repo: str, args: dict) -> str:
    kind = args.get("kind") or None
    if kind not in (None, "decision", "rule", "constraint", "requirement"):
        return f"[error: unknown kind `{kind}`]"
    hits = registry.search(
        str(args.get("query") or ""),
        kind=kind,
        concept=args.get("concept") or None,
        repo=repo,
        path=args.get("path") or None,
        symbol=args.get("symbol") or None,
        statuses=("approved",),
        limit=_MAX_TOOL_HITS,
    )
    hits = [h for h in hits if h["kind"] in ("decision", "rule", "constraint", "requirement")]
    if not hits:
        return "[no approved registry entries match]"
    for h in hits:
        h["anchors"] = registry.anchors(h["id"])
    return "\n".join(_line(h) for h in hits)


class RegistryToolExecutor:
    """Wraps an ``AgenticToolExecutor`` and answers ``search_decisions`` itself."""

    def __init__(self, inner: Any, repo: str) -> None:
        self._inner = inner
        self._repo = repo

    async def execute(self, name: str, args: dict) -> str:
        if name != SEARCH_DECISIONS_TOOL["function"]["name"]:
            return await self._inner.execute(name, args)
        self._inner.call_log.append({"tool": name, "arg": (args or {}).get("query") or ""})
        from mira.registry import get_registry

        registry = get_registry(create=False)
        if registry is None:
            return "[registry unavailable]"
        try:
            return search_approved(registry, self._repo, args or {})
        except Exception as exc:
            return f"[error executing `{name}`: {exc}]"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def with_registry_tool(executor: Any, pr_info: Any) -> Any:
    if pr_info is None:
        return executor
    return RegistryToolExecutor(executor, f"{pr_info.owner}/{pr_info.repo}")


async def apply_registry(
    engine: Any,
    pr_info: Any,
    files: list[Any],
    custom_rules: list[dict[str, str]],
    diff_text: str,
    pr_title: str,
    pr_body: str,
) -> None:
    """Engine hook: registry prompt block plus contradiction findings. No writes."""
    from mira.registry.review_gate import attach

    await attach(engine, pr_info, files, custom_rules, diff_text, pr_title, pr_body)


async def finish_registry(engine: Any, comments: list[Any]) -> list[Any]:
    """Engine hook after critique: blockers first, advisories posted. No registry writes."""
    from mira.registry.review_gate import finish

    return await finish(engine, comments)
