"""Review passes that run alongside the main chunked review.

Each pass takes an `LLMProvider` and the input it needs; none touches the
ReviewEngine instance. Most route through the configured indexing model so
the heavyweight review model isn't paying for verification work.
"""

from __future__ import annotations

import json as _json
import logging

from mira.config import load_config
from mira.dashboard.models_config import llm_config_for
from mira.exceptions import ResponseParseError
from mira.llm.prompts.review import build_security_review_prompt
from mira.llm.provider import (
    SUBMIT_CRITIQUE_TOOL,
    SUBMIT_REVIEW_TOOL,
    LLMProvider,
)
from mira.llm.response_parser import (
    convert_to_review_comments,
    parse_llm_response,
)
from mira.models import KeyIssue, ReviewComment

logger = logging.getLogger(__name__)


async def agentic_review_loop(
    llm: LLMProvider,
    messages: list[dict],
    executor: object,
) -> str:
    """Run an agentic tool-use loop until the LLM submits a review.

    Hands the model `read_file` and `grep_repo` alongside the terminal
    `submit_review` tool. Caps at 6 hops to bound token spend; returns the
    JSON args of the final `submit_review` call (same shape `llm.review`
    returns), or "" if the loop exited without one — caller falls back to
    a forced single-tool call.
    """
    from mira.llm.agentic_tools import AGENTIC_TOOLS

    tools = [*AGENTIC_TOOLS, SUBMIT_REVIEW_TOOL]
    convo: list[dict] = [dict(m) for m in messages]
    if convo and convo[0].get("role") == "system":
        convo[0]["content"] = (
            convo[0]["content"] + "\n\n## Tools\n\n"
            "This repo isn't indexed, so you have two helpers for "
            "cross-file checks: `read_file(path)` and "
            "`grep_repo(pattern, path_glob?, path_only?)`. Use them when, "
            "and ONLY when, you need to verify a cross-file claim before "
            "filing a comment (e.g. *does the called function actually "
            "raise X?*, *is this symbol defined elsewhere?*). Don't browse — "
            "fetch what you specifically need. Skip the tools entirely if "
            "the diff alone is enough. Once you're ready, call "
            "`submit_review` with all your findings."
        )

    max_hops = 6
    for hop in range(max_hops):
        try:
            msg = await llm.complete_agentic(convo, tools=tools)
        except Exception as exc:
            logger.warning("Agentic hop %d failed: %s", hop + 1, exc)
            return ""

        tool_calls = msg.get("tool_calls") or []
        content = msg.get("content") or ""

        if not tool_calls:
            logger.debug(
                "Agentic loop exited at hop %d without submit_review (content=%d chars)",
                hop + 1,
                len(content),
            )
            return ""

        convo.append(
            {
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            }
        )

        for call in tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            if name == "submit_review":
                return fn.get("arguments") or ""

            raw_args = fn.get("arguments") or "{}"
            try:
                args = _json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except Exception:
                args = {}

            tool_result = await executor.execute(name, args)  # type: ignore[attr-defined]
            convo.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or "",
                    "content": tool_result,
                }
            )

    logger.debug("Agentic loop hit %d-hop cap without submit_review", max_hops)
    return ""


def _indexing_llm(fallback: LLMProvider) -> LLMProvider:
    """Build an indexing-tier provider, falling back to ``fallback`` on error."""
    try:
        return LLMProvider(llm_config_for("indexing", load_config().llm))
    except Exception:
        return fallback


async def security_review_pass(
    llm: LLMProvider,
    files: list,
    narrowed: list,
    pr_title: str = "",
    indexing_llm: LLMProvider | None = None,
) -> list[ReviewComment]:
    """Dedicated security review on the configured indexing model.

    Runs in parallel with the main review. Returns ``[]`` on any failure
    so a transient LLM/API error doesn't kill the main review.

    `narrowed` is `files` with migrations/lockfiles/specs stripped (caller
    decides what counts); falls back to `files` if `narrowed` is empty.

    `indexing_llm`, when passed, is the caller's already-built indexing-tier
    provider; otherwise one is constructed from ``load_config()``.
    """
    if not files:
        return []

    target_files = narrowed or files

    security_llm = indexing_llm or _indexing_llm(llm)

    messages = build_security_review_prompt(files=target_files, pr_title=pr_title)
    try:
        raw = await security_llm.complete_with_tools(
            messages=messages,
            tools=[SUBMIT_REVIEW_TOOL],
            temperature=0.0,
        )
    except Exception as exc:
        # Retry on the main LLM rather than drop the security pass entirely.
        if security_llm is not llm:
            logger.debug(
                "Security pass on indexing tier failed (%s); retrying on review LLM",
                exc,
            )
            try:
                raw = await llm.complete_with_tools(
                    messages=messages,
                    tools=[SUBMIT_REVIEW_TOOL],
                    temperature=0.0,
                )
            except Exception as exc2:
                logger.warning("Security review pass failed: %s", exc2)
                return []
        else:
            logger.warning("Security review pass failed: %s", exc)
            return []

    try:
        parsed = parse_llm_response(raw)
        comments = convert_to_review_comments(parsed, diff_files=files)
    except ResponseParseError as exc:
        logger.warning("Security review pass parse error: %s", exc)
        return []
    except Exception as exc:
        logger.warning("Security review pass conversion failed: %s", exc)
        return []

    for c in comments:
        if not c.category or c.category != "security":
            c.category = "security"
    if comments:
        logger.info("Security pass produced %d candidate comment(s)", len(comments))
    return comments


async def self_critique(
    llm: LLMProvider,
    comments: list[ReviewComment],
    learned_rules: list[str] | None = None,
    custom_rules: list[dict[str, str]] | None = None,
    indexing_llm: LLMProvider | None = None,
) -> list[ReviewComment]:
    """Re-verify each draft comment: keep only ones whose cited code proves the issue.

    Team-documented preferences (learned + custom rules) are surfaced to the
    critic so it doesn't drop comments that align with them as "style nits".

    `indexing_llm`, when passed, is the caller's already-built indexing-tier
    provider; otherwise one is constructed from ``load_config()``.
    """
    if not comments:
        return comments

    draft_lines = []
    for i, c in enumerate(comments):
        cited = (c.existing_code or "").strip()
        if len(cited) > 400:
            cited = cited[:400] + "…"
        draft_lines.append(
            f"[{i}] {c.path}:{c.line} — {c.severity.name} / {c.category}\n"
            f"    Title: {c.title}\n"
            f"    Body:  {(c.body or '').strip()[:500]}\n"
            f"    Cites: {cited or '(no code citation)'}\n"
        )

    rules_block = ""
    rule_texts: list[str] = list(learned_rules or [])
    for r in custom_rules or []:
        title = (r.get("title") or "").strip()
        content = (r.get("content") or "").strip()
        rule_texts.append(f"{title}: {content}" if title else content)
    if rule_texts:
        rules_block = (
            "## Team preferences (do NOT drop comments that enforce these)\n\n"
            + "\n".join(f"- {t}" for t in rule_texts)
            + "\n\n"
        )

    critic_prompt = (
        "You are reviewing draft PR comments produced by another reviewer. "
        "Your job is to filter out confidently-wrong analyses, speculation, "
        "and 'while I'm here' nitpicks. Keep only comments where the cited "
        "code clearly proves the issue and the fix is actionable.\n\n"
        "Be especially skeptical of:\n"
        "- Claims about Python/JS semantics that don't match the actual "
        "  language behaviour (e.g. 'decorator only registers last route' "
        "  is wrong; stacked decorators register both)\n"
        "- Race-condition or timing arguments that depend on lines not in "
        "  the citation\n"
        "- 'May not be valid' / 'could potentially' hedges without proof\n"
        "- Style preferences masquerading as warnings\n\n"
        "If the cited code clearly proves the issue, KEEP it. If you're "
        "unsure or it looks speculative, DROP it.\n\n"
        + rules_block
        + "## Draft comments\n\n"
        + "\n".join(draft_lines)
    )

    critic_llm = indexing_llm or _indexing_llm(llm)

    try:
        raw = await critic_llm.complete_with_tools(
            messages=[{"role": "user", "content": critic_prompt}],
            tools=[SUBMIT_CRITIQUE_TOOL],
            temperature=0.0,
        )
        data = _json.loads(raw) if raw else {}
    except Exception as exc:
        logger.warning("Self-critique LLM call failed: %s. Keeping all drafts.", exc)
        return comments

    verdicts = data.get("verdicts") or []
    if not isinstance(verdicts, list):
        return comments

    keep_indices = {int(v["index"]) for v in verdicts if v.get("keep") is True}
    for v in verdicts:
        if v.get("keep") is False and 0 <= int(v.get("index", -1)) < len(comments):
            idx = int(v["index"])
            logger.info(
                "Self-critique dropped [%d] %s:%d — %s",
                idx,
                comments[idx].path,
                comments[idx].line,
                str(v.get("reason", "no reason"))[:120],
            )

    return [c for i, c in enumerate(comments) if i in keep_indices]


async def regenerate_summary(
    llm: LLMProvider,
    comments: list[ReviewComment],
    key_issues: list[KeyIssue],
    pr_title: str,
    pr_description: str,
    fallback: str,
    indexing_llm: LLMProvider | None = None,
) -> str:
    """Rewrite the review summary from the final filed outputs.

    The first-pass summary can mention issues that never got filed (LLM put
    them in prose only) or that were dropped by noise filter / self-critique
    / orphan filter. Regenerate from the surviving structured outputs so
    the prose stays grounded in what actually shipped.

    `indexing_llm`, when passed, is the caller's already-built indexing-tier
    provider; otherwise one is constructed from ``load_config()``.
    """
    if not comments and not key_issues:
        return "No issues found."

    filed_lines = []
    for c in comments:
        filed_lines.append(f"- {c.path}:{c.line} [{c.severity.name} / {c.category}] {c.title}")
    for ki in key_issues:
        filed_lines.append(f"- KEY: {ki.path}:{ki.line} — {ki.issue[:200]}")

    title_line = f"PR title: {pr_title}\n" if pr_title else ""
    desc_line = f"PR description: {pr_description[:400]}\n" if pr_description else ""
    prompt = (
        "Write a 2-3 sentence summary of a PR review. The summary will "
        "appear at the top of the review on GitHub. It must describe "
        "ONLY the issues listed below — do NOT invent, speculate, or "
        "mention concerns that aren't in this list. If the list is "
        "empty, say the PR looks clean. Use plain prose, no markdown "
        "headers or bullets. Reference file paths inline where it "
        "helps.\n\n"
        f"{title_line}{desc_line}\n"
        "## Filed issues\n\n"
        + "\n".join(filed_lines)
        + "\n\nReturn just the summary text — no preamble, no quotes."
    )

    summary_llm = indexing_llm or _indexing_llm(llm)

    try:
        text = await summary_llm.complete(
            messages=[{"role": "user", "content": prompt}],
            json_mode=False,
            temperature=0.0,
        )
    except Exception as exc:
        logger.warning("Summary regen LLM call failed: %s", exc)
        return fallback or "No issues found."

    text = (text or "").strip()
    return text or fallback or "No issues found."
