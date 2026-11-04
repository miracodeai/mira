"""Review-time contradiction check.

A PR can state a rule that narrows, widens, or reverses an approved decision
without anyone calling ``put_decision``. The check reuses the registry judge
and returns review comments only. It does not write a registry object: a
reviewer-generated proposal would land in the approval queue as agent-authored
churn.
"""

from __future__ import annotations

import logging
from typing import Any

from mira.models import ReviewComment, Severity

logger = logging.getLogger(__name__)


def _hunks(diff_text: str) -> dict[str, dict]:
    from mira.core.diff_parser import parse_diff

    try:
        patch = parse_diff(diff_text)
    except Exception as exc:
        logger.debug("Registry review skipped an unreadable diff: %s", exc)
        return {}
    hunks: dict[str, dict] = {}
    for patched in patch.files:
        text = "\n".join(hunk.content for hunk in patched.hunks)
        line = 1
        for hunk in patched.hunks:
            if hunk.target_start:
                line = hunk.target_start
                break
        hunks[patched.path] = {"text": text, "line": max(1, line)}
    return hunks


def finding_comment(hit: dict) -> ReviewComment:
    """A blocker that quotes the approved id, the PR clause, and both clauses.

    When a proposal already records the same conflict, the comment names that
    id too. Otherwise the PR clause stands in for a second registry id.
    """
    lines = [
        f"This pull request {hit['relation']} an approved decision.",
        f'Approved ({hit["approved_id"]}): "{hit["existing_clause"]}"',
        f'This PR: "{hit["new_clause"]}"',
    ]
    proposal_id = hit.get("proposal_id") or ""
    if proposal_id:
        lines.append(f"Proposal ({proposal_id}) is still unresolved.")
    lines.append(
        "A person has to keep the approved decision, supersede it, or defer. "
        "Merging does not resolve this."
    )
    return ReviewComment(
        path=hit.get("path") or "",
        line=int(hit.get("line") or 1),
        end_line=None,
        severity=Severity.BLOCKER,
        category="registry",
        title="Contradicts an approved decision",
        body="\n".join(lines),
        confidence=0.99,
        source_pass="registry",
    )


async def attach(
    engine: Any,
    pr_info: Any,
    files: list[Any],
    custom_rules: list[dict[str, str]],
    diff_text: str,
    pr_title: str,
    pr_body: str,
) -> None:
    """Put the approved-registry block first and stash contradiction findings.

    Findings are attached after critique by the engine, so the noise filter
    and the critic cannot drop the blocker. Failures here skip the block and
    leave the review itself running.
    """
    engine._registry_findings = []
    try:
        from mira.registry.prompt import registry_rule

        block = registry_rule(pr_info, [f.path for f in files])
        if block:
            custom_rules.insert(0, block)
    except Exception as exc:
        logger.debug("Registry prompt block skipped: %s", exc)
    try:
        from mira.registry import get_registry

        registry = get_registry(create=False)
        if registry is None:
            return
        hits = await registry.review_findings(
            repo=f"{pr_info.owner}/{pr_info.repo}",
            hunks=_hunks(diff_text),
            pr_title=pr_title or getattr(pr_info, "title", "") or "",
            pr_body=pr_body if pr_body is not None else getattr(pr_info, "description", "") or "",
            branch=getattr(pr_info, "head_branch", "") or "",
            pr_number=int(getattr(pr_info, "number", 0) or 0),
        )
        engine._registry_findings = [finding_comment(hit) for hit in hits]
    except Exception as exc:
        logger.debug("Registry contradiction check skipped: %s", exc)


async def finish(engine: Any, comments: list[ReviewComment]) -> list[ReviewComment]:
    """After critique: put contradiction blockers first and post advisories.

    A review that posts to GitHub gets the uncited-decision advisories as one
    general PR comment (an inline comment needs a diff line). A dry run, such
    as the review API, posts nothing; its runner returns them as findings.
    """
    blockers = engine.__dict__.pop("_registry_findings", [])
    pr_info = getattr(engine, "_pr_info", None)
    provider = getattr(engine, "provider", None)
    if pr_info is not None and provider is not None and not getattr(engine, "dry_run", True):
        from mira.registry.uncited import post_general_comment, uncited_decisions

        await post_general_comment(provider, pr_info, uncited_decisions(pr_info))
    return [*blockers, *comments]
