"""Main review orchestration engine."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mira.llm.base import LLMProviderProtocol

from mira.analysis.noise_filter import filter_noise
from mira.analysis.severity import classify_severity
from mira.config import MiraConfig
from mira.core.chunker import chunk_files
from mira.core.context import expand_context
from mira.core.diff_parser import parse_diff
from mira.core.file_filter import filter_files
from mira.core.priority import rank_files
from mira.exceptions import ResponseParseError
from mira.index.context import build_code_context
from mira.index.store import IndexStore
from mira.llm.prompts.review import (
    build_review_prompt,
    build_walkthrough_prompt,
)
from mira.llm.prompts.verify_fixes import build_verify_fixes_prompt, parse_verify_fixes_response
from mira.llm.response_parser import (
    convert_to_review_comments,
    convert_to_walkthrough_result,
    parse_llm_response,
    parse_walkthrough_response,
)
from mira.models import (
    WALKTHROUGH_MARKER,
    KeyIssue,
    PRInfo,
    ReviewChunk,
    ReviewComment,
    ReviewResult,
    Severity,
    ThreadDecision,
    UnresolvedThread,
    WalkthroughResult,
    build_review_stats,
)
from mira.providers.base import BaseProvider

logger = logging.getLogger(__name__)


def _clamp_confidence_to_findings(
    walkthrough: WalkthroughResult,
    comments: list[ReviewComment],
) -> None:
    """Tighten the walkthrough confidence score based on actual review findings.

    The LLM rates confidence before chunked review runs, so it hasn't yet seen
    blockers or warnings discovered later. This never *raises* the score — it
    only lowers it when the findings contradict an optimistic initial read.

    Rubric (1=major concerns, 5=safe to merge):
      - ≥1 blocker → score ≤ 2
      - ≥3 warnings (and no blocker) → score ≤ 3
    """
    cs = walkthrough.confidence_score
    if cs is None:
        return

    blockers = sum(1 for c in comments if c.severity == Severity.BLOCKER)
    warnings = sum(1 for c in comments if c.severity == Severity.WARNING)
    original = cs.score

    if blockers > 0 and cs.score > 2:
        cs.score = 2
        cs.label = "Do not merge"
        cs.reason = (
            f"Found {blockers} blocker{'s' if blockers != 1 else ''} "
            "that must be fixed before merge."
        )
    elif warnings >= 3 and cs.score > 3:
        cs.score = 3
        cs.label = "Needs review"
        cs.reason = f"Found {warnings} warnings that need attention before merge."

    if cs.score != original:
        logger.info(
            "Clamped walkthrough confidence from %d to %d (%d blocker(s), %d warning(s))",
            original,
            cs.score,
            blockers,
            warnings,
        )


_MAX_FULL_FILE_LINES = 500
_LARGE_FILE_CONTEXT_LINES = 50  # ±50 lines = 100-line window


# Path patterns where security findings are extremely unlikely. Used to
# narrow the dedicated security pass — see _security_review_pass. We keep
# this conservative: every excluded category is "shouldn't house auth /
# crypto / origin / injection logic." When uncertain, keep the file.
_SECURITY_PASS_SKIP_PATTERNS = (
    # DB migrations: schema changes, indexes — no request handling.
    "db/migrate/",
    "/migrations/",
    # Tests: assertions about behavior, not the behavior itself.
    "spec/",
    "/__tests__/",
    "/__fixtures__/",
    "/fixtures/",
    # Docs and changelogs.
    ".md",
    ".rst",
    ".txt",
    "CHANGELOG",
    "LICENSE",
    # Pure styles: extremely rare attack surface; XSS through CSS would
    # show up in the embedded JS or template, not the .scss.
    ".css",
    ".scss",
    ".less",
    ".sass",
    # Lockfiles: package manager output, not application code.
    ".lock",
    "package-lock.json",
    "yarn.lock",
    "Pipfile.lock",
    # Common build / vendor output, in case file_filter let it through.
    "/dist/",
    "/build/",
    "/vendor/",
    "/node_modules/",
)

_SECURITY_TEST_SUFFIXES = (
    "_test.go",
    "_test.py",
    "_spec.rb",
    "_spec.js",
    "_spec.ts",
    ".test.js",
    ".test.jsx",
    ".test.ts",
    ".test.tsx",
    ".spec.js",
    ".spec.jsx",
    ".spec.ts",
    ".spec.tsx",
)


def _security_relevant_files(files: list) -> list:
    """Return the subset of files plausibly containing security findings.

    The dedicated security pass runs as one LLM call across the entire
    diff. When the diff is dominated by migrations / specs / lockfiles,
    those non-code files dilute attention away from the actual vulnerable
    code. This filter trims the obvious-no-finding cases so the model can
    focus.
    """
    keep = []
    for f in files:
        path = f.path
        lower = path.lower()
        if any(p in lower for p in _SECURITY_PASS_SKIP_PATTERNS):
            continue
        if any(lower.endswith(s) for s in _SECURITY_TEST_SUFFIXES):
            continue
        keep.append(f)
    return keep


def _select_files_by_priority(
    files: list,
    max_total_size: int,
    max_per_file_size: int,
    only_paths: set[str] | None = None,
) -> tuple[list, list[tuple[str, str]]]:
    """Rank-and-select files for a single review pass.

    Returns ``(selected, skipped)``. ``selected`` is the list of FileDiff
    objects to actually review. ``skipped`` is a list of ``(path, reason)``
    pairs explaining what was dropped — surfaced to the user in the walkthrough.

    Selection rule:
      1. Drop files whose individual diff text exceeds ``max_per_file_size``
         (typically lockfiles, generated SDKs).
      2. If ``only_paths`` is set, drop everything not in it (used by the
         ``review-rest`` command to target previously-skipped files only).
      3. Rank remaining files by priority, then take from the top until the
         total size exceeds ``max_total_size``.
    """
    selected: list = []
    skipped: list[tuple[str, str]] = []

    candidates: list = []
    for f in files:
        if only_paths is not None and f.path not in only_paths:
            continue
        # Per-file size estimate: the diff text length for this file.
        file_diff_text_len = sum(len(h.content) for h in f.hunks)
        if file_diff_text_len > max_per_file_size:
            skipped.append((f.path, f"file diff too large ({file_diff_text_len} chars)"))
            continue
        candidates.append((f, file_diff_text_len))

    ranked = rank_files([f for f, _ in candidates])
    sizes = {f.path: size for f, size in candidates}

    running_size = 0
    for f, _priority in ranked:
        size = sizes.get(f.path, 0)
        if running_size + size > max_total_size:
            skipped.append((f.path, "diff size limit reached"))
            continue
        selected.append(f)
        running_size += size

    return selected, skipped


def _number_lines(content: str) -> str:
    """Add line numbers to file content for LLM context."""
    lines = content.splitlines()
    width = len(str(len(lines)))
    return "\n".join(f"{i + 1:>{width}}| {line}" for i, line in enumerate(lines))


_ORPHAN_LINE_TOLERANCE = 3


def _drop_orphan_key_issues(
    key_issues: list[KeyIssue],
    final_comments: list[ReviewComment],
) -> list[KeyIssue]:
    """Drop key_issues whose inline comment didn't survive filtering.

    The LLM emits ``key_issues`` and ``comments`` as independent arrays in
    ``submit_review``, so a key_issue can outlive the comment it points to
    after noise filtering or self-critique drops the inline. Keep only
    key_issues that point near a surviving comment so the Walkthrough's
    "Key Issues" table stays in sync with what actually got posted.

    A small ±_ORPHAN_LINE_TOLERANCE line tolerance is applied because the
    LLM sometimes picks the function/header line for a key_issue while
    filing the inline at the actual problem line a few lines below.
    """
    by_path: dict[str, list[tuple[int, int]]] = {}
    for c in final_comments:
        start = c.line - _ORPHAN_LINE_TOLERANCE
        end = (c.end_line or c.line) + _ORPHAN_LINE_TOLERANCE
        by_path.setdefault(c.path, []).append((start, end))

    def _matches(ki: KeyIssue) -> bool:
        return any(start <= ki.line <= end for start, end in by_path.get(ki.path, ()))

    return [ki for ki in key_issues if _matches(ki)]


def _short_thread_description(body: str) -> str:
    """Pull a one-line description out of a bot review-comment body for
    use as 'already addressed' context. Strips badge/category lines and
    looks for the first bold title; falls back to the first non-empty line.
    """
    body = body or ""
    for line in body.splitlines():
        s = line.strip()
        # Skip badge/category lines (e.g. "🐛 **Bug**", "⚠️ Warning")
        if not s or s.startswith(("⚠️", "🐛", "💡", "🔒", "⚡", "🛑", "🔵", "🟡", "🟠", "🔴")):
            continue
        # Pure-bold title line is the canonical short description
        if s.startswith("**") and s.endswith("**") and len(s) > 4:
            return s.strip("* ")[:160]
        return s[:160]
    return ""


def _extract_sections(
    lines: list[str],
    threads: list[UnresolvedThread],
    context_lines: int,
) -> str:
    """Extract and merge relevant sections around each thread's comment line.

    Returns a line-numbered string with merged windows joined by ``...`` separators.
    """
    total = len(lines)
    width = len(str(total))
    # Collect (start, end) ranges for each thread
    ranges: list[tuple[int, int]] = []
    for t in threads:
        start = max(0, t.line - 1 - context_lines)
        end = min(total, t.line - 1 + context_lines + 1)
        ranges.append((start, end))

    # Sort and merge overlapping ranges
    ranges.sort()
    merged: list[tuple[int, int]] = [ranges[0]]
    for start, end in ranges[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))

    # Build snippet with original line numbers
    parts: list[str] = []
    for start, end in merged:
        numbered = [f"{i + 1:>{width}}| {lines[i]}" for i in range(start, end)]
        parts.append("\n".join(numbered))
    return "\n...\n".join(parts)


class ReviewEngine:
    """Orchestrates the full PR review pipeline."""

    def __init__(
        self,
        config: MiraConfig,
        llm: LLMProviderProtocol,
        provider: BaseProvider | None = None,
        bot_name: str = "miracodeai",
        dry_run: bool = False,
        indexing_llm: LLMProviderProtocol | None = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.indexing_llm = indexing_llm or llm
        self.provider = provider
        self.bot_name = bot_name
        self.dry_run = dry_run
        # Set during _build_context.
        #
        # `_jit_needed` is True when the index store has no summaries for
        # the *changed files in this PR* — controls JIT cross-file lookup
        # and the agentic tool-use path. This fires regularly even on a
        # well-indexed repo (e.g. a PR touching only README.md, which the
        # indexer skips), so it must NOT be used to tell the user "your
        # repo isn't indexed."
        #
        # `_index_was_empty` is True only when the index store has NO data
        # at all for this repo — that's the actual "this repo hasn't been
        # indexed" signal that drives the walkthrough nudge.
        self._jit_needed = False
        self._index_was_empty = False
        # Captured during _build_context for the agentic tool-use path: a
        # source fetcher and the repo tree at PR head. Both already needed
        # for JIT, so reusing them is free. None when no provider/PR is
        # attached (CLI / dry-run).
        self._agentic_source_fetcher: object | None = None
        self._agentic_repo_tree: list[str] = []

    async def _post_placeholder_comment(self, pr_info: PRInfo) -> int | None:
        """Post an immediate 'Reviewing this PR...' comment and return its ID.

        Uses the walkthrough marker so subsequent updates can swap in the
        real walkthrough + review stats in place.
        """
        if not self.provider:
            return None
        placeholder = f"{WALKTHROUGH_MARKER}\n## Mira PR Walkthrough\n\n*🔍 Reviewing this PR…*\n"
        # Reuse an existing walkthrough comment if one exists (e.g. on
        # synchronize events where a review posted previously).
        existing_id = await self.provider.find_bot_comment(pr_info, WALKTHROUGH_MARKER)
        if existing_id is not None:
            await self.provider.update_comment(pr_info, existing_id, placeholder)
            return existing_id
        await self.provider.post_comment(pr_info, placeholder)
        # Re-fetch to get the ID of the comment we just posted. post_comment
        # doesn't return one, and this keeps the provider interface narrow.
        return await self.provider.find_bot_comment(pr_info, WALKTHROUGH_MARKER)

    async def review_pr(self, pr_url: str) -> ReviewResult:
        """Full pipeline: fetch PR -> review -> post results.

        Runs thread resolution and diff fetching in parallel to reduce latency.
        """
        import asyncio as _asyncio
        import time as _time

        _review_start = _time.monotonic()

        if not self.provider:
            raise RuntimeError("A provider is required for PR review")

        pr_info = await self.provider.get_pr_info(pr_url)
        self._pr_info = pr_info

        # ── Run thread resolution and diff fetch in parallel ──
        async def _resolve_threads() -> tuple[
            int, int, list[UnresolvedThread], list[ThreadDecision]
        ]:
            if not self.bot_name:
                return 0, 0, [], []
            try:
                return await self._resolve_verified_threads(pr_info)
            except Exception as exc:
                logger.warning("Thread resolution failed, continuing: %s", exc)
                return 0, 0, [], []

        thread_result, diff_text = await _asyncio.gather(
            _resolve_threads(),
            self.provider.get_pr_diff(pr_info),
        )

        threads_checked, llm_resolved, unresolved_threads, thread_decisions = thread_result

        # Count lines changed for metrics
        _lines_changed = sum(
            1 for line in diff_text.splitlines() if line.startswith("+") or line.startswith("-")
        )

        # ── Post placeholder comment immediately so the user sees activity
        # within a second of opening the PR. The placeholder uses the walkthrough
        # marker so find_bot_comment can locate it as a fallback. ──
        placeholder_id: int | None = None
        if not self.dry_run:
            try:
                placeholder_id = await self._post_placeholder_comment(pr_info)
            except Exception as exc:
                logger.warning("Failed to post walkthrough placeholder: %s", exc)

        async def _on_walkthrough_ready(wt: WalkthroughResult | None) -> None:
            """Update the placeholder with the walkthrough the moment the LLM
            call resolves — typically before chunked review completes."""
            if self.dry_run or wt is None or placeholder_id is None:
                return
            try:
                markdown = wt.to_markdown(
                    bot_name=self.bot_name or "miracodeai",
                    in_progress=True,
                )
                await self.provider.update_comment(pr_info, placeholder_id, markdown)
            except Exception as exc:
                logger.warning("Failed to post in-progress walkthrough: %s", exc)

        # Detect review round and surface resolved threads as context. Round
        # 1 is the first time the bot reviews; round 2+ raises the comment
        # threshold so we converge instead of dripping new findings on every
        # push. Resolved-thread paths are passed to the prompt as "already
        # addressed — don't re-flag this area".
        review_round = 1
        resolved_thread_dicts: list[dict] = []
        try:
            if self.bot_name and self.provider is not None:
                all_bot_threads = await self.provider.get_all_bot_threads(
                    pr_info,
                    self.bot_name,
                )
                if all_bot_threads:
                    review_round = 2
                resolved_thread_dicts = [
                    {
                        "path": t.path,
                        "line": t.line,
                        "description": _short_thread_description(t.body),
                    }
                    for t in all_bot_threads
                    if t.is_resolved
                ]
        except Exception as exc:
            logger.warning("Failed to compute review round: %s", exc)

        # Incremental diff for round 2+: if we have a stored last-reviewed
        # SHA, fetch only what's been pushed since then. This prevents the
        # bot from "discovering" issues in untouched files between rounds —
        # which feels to authors like the bot withheld findings on round 1.
        # Falls back silently to the full diff if anything goes wrong.
        if review_round >= 2 and pr_info.head_sha:
            try:
                from mira.dashboard.api import _app_db

                last_sha = _app_db.get_last_reviewed_sha(
                    pr_info.owner,
                    pr_info.repo,
                    pr_info.number,
                )
                if last_sha and last_sha != pr_info.head_sha:
                    incremental = await self.provider.get_compare_diff(
                        pr_info,
                        last_sha,
                        pr_info.head_sha,
                    )
                    if incremental.strip():
                        logger.info(
                            "Round %d incremental diff %s..%s on PR %s (was %d chars, now %d)",
                            review_round,
                            last_sha[:8],
                            pr_info.head_sha[:8],
                            pr_info.url,
                            len(diff_text),
                            len(incremental),
                        )
                        diff_text = incremental
                    else:
                        # Same head SHA or empty diff — nothing new to review.
                        logger.info(
                            "Round %d: no new commits since %s on PR %s, skipping review",
                            review_round,
                            last_sha[:8],
                            pr_info.url,
                        )
                        diff_text = ""
            except Exception as exc:
                logger.warning(
                    "Incremental diff fetch failed, falling back to full diff: %s",
                    exc,
                )

        # Pull the team conventions text the indexer extracted from
        # CONTRIBUTING.md / AGENTS.md / STYLE.md so the LLM knows
        # repo-specific style rules.
        team_conventions = ""
        try:
            from mira.dashboard.api import _app_db

            repo_record = _app_db.get_repo(pr_info.owner, pr_info.repo)
            if repo_record and repo_record.conventions:
                team_conventions = repo_record.conventions
        except Exception:
            pass

        result = await self._review_diff_internal(
            diff_text,
            pr_title=pr_info.title,
            pr_description=pr_info.description,
            existing_comments=unresolved_threads or None,
            on_walkthrough_ready=_on_walkthrough_ready,
            review_round=review_round,
            resolved_threads=resolved_thread_dicts or None,
            team_conventions=team_conventions,
        )

        # Final walkthrough update with real review stats. Tighten confidence
        # now that we know what the review found — the initial LLM score
        # predates the chunked review.
        #
        # Wait for the in-progress walkthrough update to finish before posting
        # the final one. If we don't, the in-progress write can land after
        # this final one (race lands reliably on PRs with zero comments where
        # chunk review is faster than a GitHub API write), leaving the
        # comment stuck on "Code review in progress…".
        notify_task = getattr(self, "_walkthrough_notify_task", None)
        if notify_task is not None:
            # Already logged inside the task; just proceed to final post.
            with contextlib.suppress(Exception):
                await notify_task

        if result.walkthrough:
            _clamp_confidence_to_findings(result.walkthrough, result.comments)
            if self.dry_run:
                logger.info("Dry run: skipping walkthrough comment posting")
            else:
                try:
                    stats = build_review_stats(result.comments)

                    # Get cross-repo blast radius
                    cross_repo_blast: list[dict] | None = None
                    try:
                        from mira.index.relationships import RelationshipStore

                        rs = RelationshipStore()
                        full_name = f"{pr_info.owner}/{pr_info.repo}"
                        edges = rs.resolve_edges()
                        dependents = [
                            {
                                "repo": e.source_repo,
                                "files": [{"kind": r.kind, "target": r.target} for r in e.refs],
                            }
                            for e in edges
                            if e.target_repo == full_name
                        ]
                        if dependents:
                            cross_repo_blast = dependents
                        rs.close()
                    except Exception:
                        pass

                    import os as _os

                    markdown = result.walkthrough.to_markdown(
                        bot_name=self.bot_name,
                        review_stats=stats,
                        existing_issues=len(unresolved_threads),
                        blast_radius=cross_repo_blast,
                        reviewed_files=result.reviewed_files,
                        total_comments=len(result.comments),
                        key_issues=result.key_issues or None,
                        skipped_paths=result.skipped_paths or None,
                        total_paths=result.total_paths or None,
                        index_was_empty=getattr(self, "_index_was_empty", False),
                        dashboard_url=_os.environ.get("MIRA_DASHBOARD_URL", ""),
                    )
                    # Prefer the known placeholder ID. Fall back to marker-based
                    # lookup if the placeholder never posted (network blip, etc.).
                    comment_id = placeholder_id
                    if comment_id is None:
                        comment_id = await self.provider.find_bot_comment(
                            pr_info, WALKTHROUGH_MARKER
                        )
                    if comment_id is not None:
                        await self.provider.update_comment(pr_info, comment_id, markdown)
                    else:
                        await self.provider.post_comment(pr_info, markdown)
                except Exception as exc:
                    logger.warning("Failed to post walkthrough comment: %s", exc)

        logger.info(
            "Thread resolution for PR %s: checked %d, resolved %d",
            pr_info.url,
            threads_checked,
            llm_resolved,
        )

        # Only post if there are comments
        if result.comments:
            if self.dry_run:
                logger.info(
                    "Dry run: would post %d comment(s) on PR %s",
                    len(result.comments),
                    pr_info.url,
                )
            else:
                await self.provider.post_review(pr_info, result, bot_name=self.bot_name)
        else:
            logger.info("No code suggestions for PR %s", pr_info.url)

        result.thread_decisions = thread_decisions

        # Record review event for metrics
        try:
            from mira.models import Severity

            store = IndexStore.open(pr_info.owner, pr_info.repo)
            blocker_count = sum(1 for c in result.comments if c.severity == Severity.BLOCKER)
            warning_count = sum(1 for c in result.comments if c.severity == Severity.WARNING)
            suggestion_count = sum(
                1 for c in result.comments if c.severity in (Severity.SUGGESTION, Severity.NITPICK)
            )
            categories = ",".join(sorted({c.category for c in result.comments if c.category}))
            duration = int((_time.monotonic() - _review_start) * 1000)
            store.record_review(
                pr_number=pr_info.number,
                pr_title=pr_info.title,
                pr_url=pr_info.url,
                comments_posted=len(result.comments),
                blockers=blocker_count,
                warnings=warning_count,
                suggestions=suggestion_count,
                files_reviewed=result.reviewed_files,
                lines_changed=_lines_changed,
                tokens_used=result.token_usage.get("total_tokens", 0),
                duration_ms=duration,
                categories=categories,
            )
            # Run lightweight feedback synthesis
            try:
                from mira.analysis.feedback import synthesize_rules

                synthesize_rules(store)
            except Exception as synth_err:
                logger.debug("Feedback synthesis failed: %s", synth_err)
            store.close()

            # Persist per-PR review progress so `@mira-bot review-rest` can
            # later target the unreviewed paths. Merges with prior progress
            # when the same PR has already been partially reviewed.
            try:
                from mira.dashboard.api import _app_db
                from mira.dashboard.db import PRReviewProgress

                prior = _app_db.get_pr_review_progress(
                    pr_info.owner,
                    pr_info.repo,
                    pr_info.number,
                )
                # Merge: union of reviewed paths + remember newly skipped paths.
                # If a path was skipped previously and reviewed now, drop it
                # from the skipped list.
                prior_reviewed = set(prior.reviewed_paths) if prior else set()
                prior_skipped = set(prior.skipped_paths) if prior else set()
                new_reviewed = prior_reviewed | set(result.reviewed_paths)
                new_skipped = (prior_skipped | set(result.skipped_paths)) - new_reviewed
                _app_db.upsert_pr_review_progress(
                    PRReviewProgress(
                        owner=pr_info.owner,
                        repo=pr_info.repo,
                        pr_number=pr_info.number,
                        total_paths=result.total_paths or list(new_reviewed | new_skipped),
                        reviewed_paths=sorted(new_reviewed),
                        skipped_paths=sorted(new_skipped),
                        chunk_index=(prior.chunk_index + 1) if prior else 1,
                    )
                )
            except Exception as progress_err:
                logger.debug("Failed to persist review progress: %s", progress_err)
        except Exception as exc:
            logger.debug("Failed to record review event: %s", exc)

        # Always anchor the SHA after a successful review — including rounds
        # that found zero comments. Otherwise round 2 has no SHA to diff
        # against and falls back to a full review, which is the bug we're
        # trying to avoid.
        if pr_info.head_sha:
            try:
                from mira.dashboard.api import _app_db

                _app_db.set_last_reviewed_sha(
                    pr_info.owner,
                    pr_info.repo,
                    pr_info.number,
                    pr_info.head_sha,
                )
            except Exception as exc:
                logger.debug("Failed to record last reviewed SHA: %s", exc)

        return result

    async def review_diff(self, diff_text: str) -> ReviewResult:
        """Review a diff from stdin — no provider needed."""
        return await self._review_diff_internal(diff_text)

    async def _review_diff_internal(
        self,
        diff_text: str,
        pr_title: str = "",
        pr_description: str = "",
        existing_comments: list[UnresolvedThread] | None = None,
        on_walkthrough_ready: Callable[[WalkthroughResult | None], Awaitable[None]] | None = None,
        review_round: int = 1,
        resolved_threads: list[dict] | None = None,
        team_conventions: str = "",
    ) -> ReviewResult:
        """Core review pipeline.

        Runs walkthrough and review in parallel where possible.

        If ``on_walkthrough_ready`` is provided, it is invoked as a fire-and-
        forget task the moment the walkthrough LLM call resolves — allowing
        callers to post the walkthrough to GitHub well before chunked review
        completes. Exceptions in the callback are logged and swallowed.
        """
        import asyncio as _asyncio

        # Parse the full diff first — we want to know about every file before
        # we decide what to skip, so the user sees the complete picture.
        patch = parse_diff(diff_text)
        if not patch.files:
            return ReviewResult(summary="No files to review.")

        # Apply user filter rules (excludes, etc.)
        filtered = filter_files(patch.files, self.config.filter)
        if not filtered:
            return ReviewResult(
                summary="All files were filtered out.",
                skipped_reason="All files matched exclusion rules",
            )

        # Priority-rank and select files until we hit the size cap. Files that
        # don't fit are listed in skipped_files so the walkthrough banner can
        # surface them and the user can invoke `@mira-bot review-rest`.
        only_paths = getattr(self, "_review_only_paths", None)
        selected, skipped = _select_files_by_priority(
            filtered,
            max_total_size=self.config.review.max_diff_size,
            max_per_file_size=self.config.review.max_file_size,
            only_paths=only_paths,
        )
        if not selected:
            return ReviewResult(
                summary="No files were selected for review.",
                skipped_reason="All files exceeded size limits or were deprioritized.",
            )

        all_paths = [f.path for f in filtered]
        selected_paths = [f.path for f in selected]
        skipped_paths_only = [p for p, _reason in skipped]

        if skipped:
            logger.info(
                "Reviewing %d of %d files (skipped %d due to size/priority caps)",
                len(selected),
                len(filtered),
                len(skipped),
            )

        # filtered is the full ranked set; selected is what we'll actually review
        filtered = selected

        # ── Run walkthrough and context building in parallel ──

        async def _generate_walkthrough() -> WalkthroughResult | None:
            if not self.config.review.walkthrough:
                return None
            try:
                wt_messages = build_walkthrough_prompt(
                    files=filtered,
                    config=self.config,
                    pr_title=pr_title,
                    pr_description=pr_description,
                )
                wt_raw = await self.llm.walkthrough(wt_messages)
                wt_parsed = parse_walkthrough_response(wt_raw)
                return convert_to_walkthrough_result(wt_parsed)
            except Exception as exc:
                logger.warning("Walkthrough generation failed, skipping: %s", exc)
                return None

        async def _build_context() -> str:
            if not self.config.review.code_context:
                return ""
            try:
                pr_info = getattr(self, "_pr_info", None)
                if pr_info is not None:
                    store = IndexStore.open(pr_info.owner, pr_info.repo)
                    source_fetcher = None
                    if self.provider and pr_info:
                        from mira.index.context import ProviderSourceFetcher

                        source_fetcher = ProviderSourceFetcher(
                            self.provider, pr_info, pr_info.head_branch
                        )
                    changed_paths = [f.path for f in filtered]
                    ctx = await build_code_context(
                        changed_paths=changed_paths,
                        store=store,
                        token_budget=self.config.review.context_token_budget,
                        source_fetcher=source_fetcher,
                    )
                    doc_context = store.get_all_review_context_text()
                    if doc_context:
                        ctx = ctx + "\n\n" + doc_context

                    # Two distinct conditions, easily confused:
                    #
                    #   `index_has_data_for_changed` — at least one of the PR's
                    #     changed files has a summary in the store. Drives JIT
                    #     fallback for cross-file context: when this is False,
                    #     parse imports + fetch HEAD content live.
                    #
                    #   `index_is_truly_empty` — the store has zero summaries
                    #     for *any* file in the repo. Drives the user-visible
                    #     "your repo isn't indexed" walkthrough nudge.
                    #
                    # The first can be False on a well-indexed repo (PR touches
                    # only README.md, which the indexer skips) — that case must
                    # not trigger the nudge, otherwise we tell users their
                    # indexed repo isn't indexed.
                    index_has_data_for_changed = bool(store.get_summaries(changed_paths))
                    self._jit_needed = not index_has_data_for_changed
                    self._index_was_empty = not bool(store.all_paths())
                    if not index_has_data_for_changed and source_fetcher is not None:
                        try:
                            from mira.index.jit_context import (
                                build_jit_cross_file_context,
                            )

                            tree_paths: set[str] | None = None
                            if hasattr(self.provider, "get_repo_tree"):
                                try:
                                    tree_paths = set(
                                        await self.provider.get_repo_tree(
                                            pr_info,
                                            pr_info.head_branch,
                                        )
                                    )
                                except Exception as exc:
                                    logger.debug(
                                        "JIT: tree fetch failed: %s",
                                        exc,
                                    )
                            # Stash for the agentic tool-use path so
                            # _review_chunk can give the LLM read_file /
                            # grep_repo without re-fetching the tree.
                            self._agentic_source_fetcher = source_fetcher
                            self._agentic_repo_tree = sorted(tree_paths) if tree_paths else []
                            jit = await build_jit_cross_file_context(
                                changed_files=filtered,
                                source_fetcher=source_fetcher,
                                repo_tree=tree_paths,
                                char_budget=(self.config.review.context_token_budget * 4),
                                enable_java_go=self.config.review.jit_java_go,
                            )
                            if jit:
                                ctx = ctx + "\n\n" + jit
                        except Exception as exc:
                            logger.debug("JIT context build failed: %s", exc)

                    # Append cross-repo impact so inline reviews know about
                    # other repositories that depend on the changed code.
                    try:
                        from mira.index.relationships import RelationshipStore

                        rs = RelationshipStore()
                        full_name = f"{pr_info.owner}/{pr_info.repo}"
                        edges = rs.resolve_edges()
                        cross_parts: list[str] = []
                        for e in edges:
                            if e.target_repo == full_name and e.refs:
                                ref_details = []
                                for r in e.refs[:5]:
                                    ref_details.append(f"`{r.file_path}` ({r.kind})")
                                cross_parts.append(
                                    f"- **{e.source_repo}** — {len(e.refs)} reference(s): "
                                    + ", ".join(ref_details)
                                )
                        if cross_parts:
                            ctx += "\n\n### Cross-Repo Impact\n"
                            ctx += "Other repositories depend on code in this repo. "
                            ctx += "Breaking changes here may affect:\n"
                            ctx += "\n".join(cross_parts)
                            ctx += "\n"
                        rs.close()
                    except Exception as exc:
                        logger.debug("Cross-repo context lookup failed: %s", exc)

                    store.close()
                    return ctx
            except Exception as exc:
                logger.warning("Code context lookup failed, continuing without: %s", exc)
            return ""

        # Fire walkthrough as its own task so the caller can post it early
        # via on_walkthrough_ready. Code context is still awaited here because
        # chunks depend on it.
        walkthrough_task = _asyncio.create_task(_generate_walkthrough())

        # The notify task is referenced on `self` so the caller (review_pr)
        # can await it before its own final update — without this, a slow
        # in-progress update can land *after* the final update on PRs with
        # zero review comments (chunk review finishes faster than the
        # callback's GitHub API write), leaving the comment stuck on
        # "Code review in progress…".
        self._walkthrough_notify_task = None
        if on_walkthrough_ready is not None:

            async def _notify_caller() -> None:
                try:
                    wt = await walkthrough_task
                    await on_walkthrough_ready(wt)
                except Exception as exc:
                    logger.warning("on_walkthrough_ready callback failed: %s", exc)

            self._walkthrough_notify_task = _asyncio.create_task(_notify_caller())

        # ── Fetch decision-archaeology history in parallel with code context.
        # The provider may not exist (CLI / dry-run); in that case we just skip.
        async def _fetch_file_history() -> dict:
            pr_info = getattr(self, "_pr_info", None)
            if pr_info is None or self.provider is None:
                return {}
            if not getattr(self.provider, "get_file_history", None):
                return {}
            try:
                paths = [f.path for f in filtered]
                history = await self.provider.get_file_history(pr_info, paths, max_per_file=5)
                return history
            except Exception as exc:
                logger.debug("File history fetch failed: %s", exc)
                return {}

        code_context_block, file_history = await _asyncio.gather(
            _build_context(),
            _fetch_file_history(),
        )

        # Expand context
        expanded = expand_context(filtered, self.config.review.context_lines)

        # Chunk
        chunks = chunk_files(
            expanded,
            max_tokens=self.config.llm.max_context_tokens,
            provider=self.llm,
        )

        # Fetch learned rules + custom rules for prompt injection
        learned_rules: list[str] = []
        custom_rules: list[dict[str, str]] = []
        try:
            pr_info = getattr(self, "_pr_info", None)
            if pr_info is not None:
                _rules_store = IndexStore.open(pr_info.owner, pr_info.repo)

                learned_rules = _rules_store.get_learned_rules_text()

                # Per-repo custom rules
                for ctx in _rules_store.list_review_context():
                    custom_rules.append({"title": ctx.title, "content": ctx.content})

                _rules_store.close()

                # Global rules
                try:
                    from mira.dashboard.db import AppDatabase

                    _app_db = AppDatabase()
                    for rule_text in _app_db.get_global_rules_text():
                        parts = rule_text.split(": ", 1)
                        title = parts[0] if len(parts) > 1 else "Global Rule"
                        content = parts[1] if len(parts) > 1 else rule_text
                        custom_rules.insert(0, {"title": title, "content": content})
                except Exception:
                    pass
        except Exception:
            pass

        # Review chunks in parallel
        valid_paths = {f.path for f in filtered}
        base_existing = list(existing_comments) if existing_comments else []
        semaphore = _asyncio.Semaphore(self.config.review.max_concurrent_chunks)

        async def _review_chunk(
            idx: int,
            chunk: ReviewChunk,
        ) -> tuple[list[ReviewComment], list[KeyIssue], str]:
            async with semaphore:
                logger.info(
                    "Reviewing chunk %d/%d (%d files)",
                    idx + 1,
                    len(chunks),
                    len(chunk.files),
                )
                try:
                    chunk_history = {
                        f.path: file_history[f.path] for f in chunk.files if f.path in file_history
                    }
                    messages = build_review_prompt(
                        files=chunk.files,
                        config=self.config,
                        pr_title=pr_title,
                        pr_description=pr_description,
                        existing_comments=base_existing or None,
                        code_context=code_context_block,
                        learned_rules=learned_rules or None,
                        custom_rules=custom_rules or None,
                        file_history=chunk_history or None,
                        review_round=review_round,
                        resolved_threads=resolved_threads,
                        team_conventions=team_conventions,
                    )
                    raw_response = ""
                    # Agentic tool-use path: only when the static context for
                    # this PR's changed files is necessarily thin (no summaries
                    # in the index for those paths). When the repo has summaries
                    # for the changed files the static block already has the
                    # full picture, so the extra tool-call hops would just slow
                    # things down. We use `_jit_needed` (per-PR signal) rather
                    # than `_index_was_empty` (whole-repo signal) so a PR that
                    # touches files the indexer skipped still gets the agentic
                    # fallback even on an indexed repo.
                    use_agentic = (
                        self.config.review.agentic_tools
                        and getattr(self, "_jit_needed", False)
                        and self._agentic_source_fetcher is not None
                    )
                    if use_agentic:
                        from mira.llm.agentic_tools import AgenticToolExecutor

                        executor = AgenticToolExecutor(
                            source_fetcher=self._agentic_source_fetcher,  # type: ignore[arg-type]
                            repo_tree=list(self._agentic_repo_tree),
                        )
                        raw_response = await self._agentic_review_loop(messages, executor)
                    if not raw_response:
                        raw_response = await self.llm.review(messages)
                    parsed = parse_llm_response(raw_response)
                    comments = convert_to_review_comments(
                        parsed,
                        valid_paths,
                        diff_files=chunk.files,
                    )
                    key_issues = [
                        KeyIssue(issue=ki.issue, path=ki.path, line=ki.line)
                        for ki in parsed.key_issues
                    ]
                    return comments, key_issues, parsed.summary or ""
                except ResponseParseError as exc:
                    logger.warning(
                        "Chunk %d/%d failed to parse, skipping: %s",
                        idx + 1,
                        len(chunks),
                        exc,
                    )
                    return [], [], ""

        # Run the main chunked review and the security pass in parallel.
        # Security findings get merged into all_comments and go through the
        # same noise filter (dedup catches any overlap with main-pass
        # findings on the same line).
        review_task = _asyncio.gather(*[_review_chunk(i, c) for i, c in enumerate(chunks)])
        security_task = _asyncio.create_task(
            self._security_review_pass(filtered, pr_title)
            if self.config.review.security_pass
            else _asyncio.sleep(0, result=[])
        )
        chunk_results, security_comments = await _asyncio.gather(review_task, security_task)

        all_comments: list[ReviewComment] = []
        all_key_issues: list[KeyIssue] = []
        summaries: list[str] = []
        for comments, key_issues, summary_text in chunk_results:
            all_comments.extend(comments)
            all_key_issues.extend(key_issues)
            if summary_text:
                summaries.append(summary_text)
        all_comments.extend(security_comments)

        # Classify severity
        all_comments = [classify_severity(c) for c in all_comments]

        # Noise filter — pass review_round so round 2+ raises the floor.
        final_comments = filter_noise(
            all_comments,
            self.config.filter,
            review_round=review_round,
        )

        # Self-critique pass — re-verify each draft comment before posting.
        # Catches confident-but-wrong claims (the LLM's own analysis errors)
        # that the noise filter can't catch because confidence scores are
        # also LLM-generated. Runs on the configured indexing model.
        if final_comments and self.config.review.self_critique:
            try:
                final_comments = await self._self_critique(final_comments)
            except Exception as exc:
                logger.warning("Self-critique pass failed, keeping original comments: %s", exc)

        all_key_issues = _drop_orphan_key_issues(all_key_issues, final_comments)

        if self.config.review.include_summary:
            original_summary = " ".join(summaries) if summaries else ""
            # Regenerate the summary from the FINAL filed outputs so the prose
            # cannot mention issues that aren't backed by an inline comment or
            # key_issue. The first-pass summary may reference findings the LLM
            # noticed but didn't file — or that got dropped by noise filter /
            # self-critique / orphan filter — and that mismatch confuses readers.
            try:
                summary = await self._regenerate_summary(
                    final_comments,
                    all_key_issues,
                    pr_title,
                    pr_description,
                    fallback=original_summary,
                )
            except Exception as exc:
                logger.warning("Summary regeneration failed, using original: %s", exc)
                summary = original_summary or "No issues found."
        else:
            summary = ""

        # Walkthrough task may have finished long ago; this just collects it.
        walkthrough = await walkthrough_task

        return ReviewResult(
            comments=final_comments,
            key_issues=all_key_issues,
            summary=summary,
            reviewed_files=len(filtered),
            token_usage=self.llm.usage,
            walkthrough=walkthrough,
            reviewed_paths=selected_paths,
            skipped_paths=skipped_paths_only,
            total_paths=all_paths,
        )

    async def _agentic_review_loop(
        self,
        messages: list[dict],
        executor: object,
    ) -> str:
        """Run an agentic tool-use loop until the LLM submits a review.

        Hands the model `read_file` and `grep_repo` alongside the terminal
        `submit_review` tool. Each hop: call the model, dispatch any
        non-terminal tool calls, append results, repeat. Caps at 6 hops so
        a confused model can't burn unbounded tokens.

        Returns the JSON args of the final `submit_review` call (same
        shape `self.llm.review` returns), or "" if the loop exited without
        one — caller falls back to a forced single-tool call.
        """
        import json as _json

        from mira.llm.agentic_tools import AGENTIC_TOOLS
        from mira.llm.provider import SUBMIT_REVIEW_TOOL

        tools = [*AGENTIC_TOOLS, SUBMIT_REVIEW_TOOL]
        # Augment the existing system message with a brief note on tool use.
        # Putting it inline keeps prompt structure intact for the rest of
        # the flow (parsing, summary regen, etc.).
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
                msg = await self.llm.complete_agentic(convo, tools=tools)
            except Exception as exc:
                logger.warning("Agentic hop %d failed: %s", hop + 1, exc)
                return ""

            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content") or ""

            if not tool_calls:
                # The model ended without calling submit_review — fall back.
                logger.debug(
                    "Agentic loop exited at hop %d without submit_review (content=%d chars)",
                    hop + 1,
                    len(content),
                )
                return ""

            # Append the assistant message verbatim so tool_call_ids resolve.
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

    async def _security_review_pass(
        self,
        files: list,
        pr_title: str = "",
    ) -> list[ReviewComment]:
        """Dedicated security review on the configured indexing model.

        Runs in parallel with the main review. The output is a list of
        ``ReviewComment`` to merge into ``all_comments`` before noise
        filtering — overlapping findings dedupe naturally.

        Routed through the indexing tier (typically a smaller, cheaper
        model) because security findings here are mostly pattern-shaped
        — XSS, injection, hard-coded secrets, missing CSRF — and don't
        need the deeper reasoning the main pass does. Defense-in-depth:
        the main pass on the review tier still catches the chained-
        inference security bugs. Users who want the heavy model on
        every pass can set ``llm.indexing_model`` to the same model as
        ``llm.review_model``.

        Returns ``[]`` and logs (debug) on any failure so a transient
        LLM/API error doesn't kill the main review.
        """
        if not files:
            return []

        # Narrow input to files where security findings are plausible.
        # On large diffs, migrations / lockfiles / fixtures / tests drown
        # out the actual vulnerable code. With them in the prompt the model
        # can miss explicit patterns (`X-Frame-Options: "ALLOWALL"`) that
        # the prompt's category list calls out by name.
        narrowed = _security_relevant_files(files)
        if not narrowed:
            # All files were filtered out — fall back to the original set
            # rather than skip the pass entirely. A PR that's purely
            # migrations / specs CAN still introduce auth/permission bugs.
            narrowed = files

        from mira.llm.prompts.review import build_security_review_prompt
        from mira.llm.provider import SUBMIT_REVIEW_TOOL

        messages = build_security_review_prompt(files=narrowed, pr_title=pr_title)
        try:
            raw = await self.indexing_llm.complete_with_tools(
                messages=messages,
                tools=[SUBMIT_REVIEW_TOOL],
                temperature=0.0,
            )
        except Exception as exc:
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

        # Force category=security so the badge renders correctly even if the
        # LLM put something else.
        for c in comments:
            if not c.category or c.category != "security":
                c.category = "security"
        if comments:
            logger.info("Security pass produced %d candidate comment(s)", len(comments))
        return comments

    async def _self_critique(self, comments: list[ReviewComment]) -> list[ReviewComment]:
        """Run a second-pass critique on each draft comment.

        The critic asks: *for each comment, can you cite specific lines that
        prove this is a real, actionable issue?* Comments without verifiable
        citations or with confidently-wrong analysis get dropped.

        Uses the configured indexing model — the critic is a verification
        step, not a generation step. Returns the kept comments in their
        original order.
        """
        import json as _json

        from mira.llm.provider import SUBMIT_CRITIQUE_TOOL

        if not comments:
            return comments

        # Build a compact draft block for the critic. We include the verbatim
        # cited code so the critic can verify the claim against ground truth.
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
            "## Draft comments\n\n" + "\n".join(draft_lines)
        )

        try:
            raw = await self.indexing_llm.complete_with_tools(
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
        # Log what got dropped so reviewers can audit calibration over time.
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

    async def _regenerate_summary(
        self,
        comments: list[ReviewComment],
        key_issues: list[KeyIssue],
        pr_title: str,
        pr_description: str,
        fallback: str,
    ) -> str:
        """Rewrite the review summary so it describes only what was actually filed.

        The first-pass summary is generated alongside the comments and can
        mention issues that never got filed (LLM put them in prose only) or
        that were later dropped by noise filter / self-critique / orphan
        filter. Regenerate from the surviving structured outputs using the
        configured indexing model so the prose stays grounded in the Key
        Issues table and inline comments that actually shipped.
        """
        if not comments and not key_issues:
            return "No issues found."

        # Compact representation of what got filed.
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

        try:
            text = await self.indexing_llm.complete(
                messages=[{"role": "user", "content": prompt}],
                json_mode=False,
                temperature=0.0,
            )
        except Exception as exc:
            logger.warning("Summary regen LLM call failed: %s", exc)
            return fallback or "No issues found."

        text = (text or "").strip()
        return text or fallback or "No issues found."

    async def _resolve_verified_threads(
        self, pr_info: PRInfo
    ) -> tuple[int, int, list[UnresolvedThread], list[ThreadDecision]]:
        """Check all unresolved bot threads and resolve those the LLM confirms as fixed.

        Returns:
            Tuple of (threads_checked, threads_resolved, remaining_unresolved, decisions).
        """
        assert self.provider is not None

        threads = await self.provider.get_unresolved_bot_threads(pr_info, self.bot_name)
        if not threads:
            logger.debug("No unresolved bot threads found for PR %s", pr_info.url)
            return 0, 0, [], []

        logger.info(
            "Found %d unresolved bot thread(s) to verify on PR %s",
            len(threads),
            pr_info.url,
        )

        # Fetch current code for each thread's file (dedupe by path)
        file_contents: dict[str, str] = {}
        for t in threads:
            if t.path not in file_contents:
                file_contents[t.path] = await self.provider.get_file_content(
                    pr_info, t.path, pr_info.head_branch
                )

        # Group threads by file and build size-aware context
        threads_by_path: dict[str, list[UnresolvedThread]] = {}
        for t in threads:
            threads_by_path.setdefault(t.path, []).append(t)

        file_groups: list[tuple[str, str, list[UnresolvedThread]]] = []
        for path, path_threads in threads_by_path.items():
            content = file_contents.get(path, "")
            lines = content.splitlines()
            if len(lines) <= _MAX_FULL_FILE_LINES:
                file_groups.append((path, _number_lines(content), path_threads))
            else:
                has_unknown_lines = any(t.line <= 0 for t in path_threads)
                if has_unknown_lines:
                    # Can't extract targeted sections without valid line numbers
                    file_groups.append((path, _number_lines(content), path_threads))
                else:
                    snippet = _extract_sections(lines, path_threads, _LARGE_FILE_CONTEXT_LINES)
                    file_groups.append((path, snippet, path_threads))

        # Single LLM call to verify which issues are fixed
        verified_ids = await self._verify_fixes(file_groups)
        verified_set = set(verified_ids)

        # Build per-thread decisions
        decisions = [
            ThreadDecision(
                thread_id=t.thread_id,
                path=t.path,
                line=t.line,
                body=t.body,
                fixed=t.thread_id in verified_set,
            )
            for t in threads
        ]

        resolved = 0
        if verified_ids:
            if self.dry_run:
                resolved = len(verified_ids)
                logger.info("Dry run: would resolve %d thread(s): %s", resolved, verified_ids)
            else:
                resolved = await self.provider.resolve_threads(pr_info, verified_ids)
                if resolved < len(verified_ids):
                    logger.error(
                        "Failed to resolve %d/%d verified-fixed thread(s) on PR %s",
                        len(verified_ids) - resolved,
                        len(verified_ids),
                        pr_info.url,
                    )

        logger.info(
            "LLM verification: checked %d thread(s), %d confirmed fixed, %d resolved",
            len(threads),
            len(verified_ids),
            resolved,
        )

        remaining = [t for t in threads if t.thread_id not in verified_set]
        return len(threads), resolved, remaining, decisions

    async def _verify_fixes(
        self, file_groups: list[tuple[str, str, list[UnresolvedThread]]]
    ) -> list[str]:
        """Ask the LLM which review issues have been fixed."""
        prompt = build_verify_fixes_prompt(file_groups)
        logger.debug("Verify-fixes prompt:\n%s", prompt[1]["content"])
        response = await self.llm.complete(prompt, json_mode=True, temperature=0.0)
        logger.debug("Verify-fixes raw response:\n%s", response)
        return parse_verify_fixes_response(response)
