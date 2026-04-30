"""Main review orchestration engine."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

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
from mira.llm.prompts.review import build_review_prompt, build_walkthrough_prompt
from mira.llm.prompts.verify_fixes import build_verify_fixes_prompt, parse_verify_fixes_response
from mira.llm.provider import LLMProvider
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
        llm: LLMProvider,
        provider: BaseProvider | None = None,
        bot_name: str = "miracodeai",
        dry_run: bool = False,
    ) -> None:
        self.config = config
        self.llm = llm
        self.provider = provider
        self.bot_name = bot_name
        self.dry_run = dry_run

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

        result = await self._review_diff_internal(
            diff_text,
            pr_title=pr_info.title,
            pr_description=pr_info.description,
            existing_comments=unresolved_threads or None,
            on_walkthrough_ready=_on_walkthrough_ready,
        )

        # Final walkthrough update with real review stats. Tighten confidence
        # now that we know what the review found — the initial LLM score
        # predates the chunked review.
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
                    ctx = await build_code_context(
                        changed_paths=[f.path for f in filtered],
                        store=store,
                        token_budget=self.config.review.context_token_budget,
                        source_fetcher=source_fetcher,
                    )
                    doc_context = store.get_all_review_context_text()
                    if doc_context:
                        ctx = ctx + "\n\n" + doc_context

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

        if on_walkthrough_ready is not None:

            async def _notify_caller() -> None:
                try:
                    wt = await walkthrough_task
                    await on_walkthrough_ready(wt)
                except Exception as exc:
                    logger.warning("on_walkthrough_ready callback failed: %s", exc)

            _asyncio.create_task(_notify_caller())

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
                    )
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

        chunk_results = await _asyncio.gather(*[_review_chunk(i, c) for i, c in enumerate(chunks)])

        all_comments: list[ReviewComment] = []
        all_key_issues: list[KeyIssue] = []
        summaries: list[str] = []
        for comments, key_issues, summary_text in chunk_results:
            all_comments.extend(comments)
            all_key_issues.extend(key_issues)
            if summary_text:
                summaries.append(summary_text)

        # Classify severity
        all_comments = [classify_severity(c) for c in all_comments]

        # Noise filter
        final_comments = filter_noise(all_comments, self.config.filter)

        if self.config.review.include_summary:
            summary = " ".join(summaries) if summaries else "No issues found."
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
