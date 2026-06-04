"""Main review orchestration engine."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mira.llm.base import LLMProviderProtocol

from mira.analysis.severity import classify_severity
from mira.config import MiraConfig
from mira.core.chunker import chunk_files
from mira.core.context import expand_context
from mira.core.diff_parser import parse_diff
from mira.core.file_filter import filter_files
from mira.core.noise_filter import filter_noise
from mira.core.passes import (
    agentic_review_loop,
    regenerate_summary,
    security_review_pass,
    self_critique,
)
from mira.core.priority import rank_files
from mira.core.threads import resolve_verified_threads, short_thread_description
from mira.exceptions import ResponseParseError
from mira.index.context import build_code_context
from mira.index.store import IndexStore
from mira.llm.prompts.review import (
    build_review_prompt,
    build_walkthrough_prompt,
)
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


# Paths excluded from the dedicated security pass — see core/passes.py.
# Keep conservative: anything that might house auth/crypto/origin/injection logic stays in.
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
        # `_jit_needed` (per-PR: index has no summaries for *this PR's* files)
        # is not the same as `_index_was_empty` (whole-repo: no data at all).
        # Only the latter drives the user-visible "your repo isn't indexed" nudge.
        self._jit_needed = False
        self._index_was_empty = False
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
        existing_id = await self.provider.find_bot_comment(pr_info, WALKTHROUGH_MARKER)
        if existing_id is not None:
            await self.provider.update_comment(pr_info, existing_id, placeholder)
            return existing_id
        await self.provider.post_comment(pr_info, placeholder)
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

        async def _resolve_threads() -> tuple[
            int, int, list[UnresolvedThread], list[ThreadDecision]
        ]:
            if not self.bot_name:
                return 0, 0, [], []
            try:
                assert self.provider is not None
                return await resolve_verified_threads(
                    self.provider, self.llm, pr_info, self.bot_name, self.dry_run
                )
            except Exception as exc:
                logger.warning("Thread resolution failed, continuing: %s", exc)
                return 0, 0, [], []

        thread_result, diff_text = await _asyncio.gather(
            _resolve_threads(),
            self.provider.get_pr_diff(pr_info),
        )

        threads_checked, llm_resolved, unresolved_threads, thread_decisions = thread_result

        _lines_changed = sum(
            1 for line in diff_text.splitlines() if line.startswith("+") or line.startswith("-")
        )

        placeholder_id: int | None = None
        if not self.dry_run:
            try:
                placeholder_id = await self._post_placeholder_comment(pr_info)
            except Exception as exc:
                logger.warning("Failed to post walkthrough placeholder: %s", exc)

        async def _on_walkthrough_ready(wt: WalkthroughResult | None) -> None:
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

        # Round 2+ raises the comment threshold so we converge instead of
        # dripping new findings on every push.
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
                        "description": short_thread_description(t.body),
                    }
                    for t in all_bot_threads
                    if t.is_resolved
                ]
        except Exception as exc:
            logger.warning("Failed to compute review round: %s", exc)

        # Round 2+ uses incremental diff to avoid re-flagging untouched files.
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

        # The final walkthrough must land after the in-progress one, or it gets
        # overwritten on fast PRs and stays stuck on "in progress".
        notify_task = getattr(self, "_walkthrough_notify_task", None)
        if notify_task is not None:
            with contextlib.suppress(Exception):
                await notify_task

        if result.walkthrough:
            _clamp_confidence_to_findings(result.walkthrough, result.comments)
            if self.dry_run:
                logger.info("Dry run: skipping walkthrough comment posting")
            else:
                try:
                    stats = build_review_stats(result.comments)

                    cross_repo_blast: list[dict] | None = None
                    if self.config.review.blast_radius:
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
            try:
                from mira.analysis.feedback import synthesize_rules

                synthesize_rules(store)
            except Exception as synth_err:
                logger.debug("Feedback synthesis failed: %s", synth_err)
            store.close()

            # Merge with any prior progress for this PR so @mira-bot review-rest
            # can target only the still-unreviewed paths.
            try:
                from mira.dashboard.api import _app_db
                from mira.dashboard.db import PRReviewProgress

                prior = _app_db.get_pr_review_progress(
                    pr_info.owner,
                    pr_info.repo,
                    pr_info.number,
                )
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

        # Anchor the SHA even on zero-finding rounds; without it round 2 has
        # nothing to compare against and falls back to a full review.
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

        # Parse the full diff (not just the priority-selected subset) so the
        # walkthrough can surface skipped files to the user.
        patch = parse_diff(diff_text)
        if not patch.files:
            return ReviewResult(summary="No files to review.")

        filtered = filter_files(patch.files, self.config.filter)
        if not filtered:
            return ReviewResult(
                summary="All files were filtered out.",
                skipped_reason="All files matched exclusion rules",
            )

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

        filtered = selected

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

                    # `_jit_needed` and `_index_was_empty` aren't the same signal —
                    # see the field comments in __init__ before changing this.
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

        # Fire walkthrough early so review_pr can post it before chunk review finishes.
        walkthrough_task = _asyncio.create_task(_generate_walkthrough())

        # `_walkthrough_notify_task` exposed on self so review_pr can await it
        # before its own final write — otherwise the in-progress update can land
        # after the final one on fast PRs (see review_pr).
        self._walkthrough_notify_task = None
        if on_walkthrough_ready is not None:

            async def _notify_caller() -> None:
                try:
                    wt = await walkthrough_task
                    await on_walkthrough_ready(wt)
                except Exception as exc:
                    logger.warning("on_walkthrough_ready callback failed: %s", exc)

            self._walkthrough_notify_task = _asyncio.create_task(_notify_caller())

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

        expanded = expand_context(filtered, self.config.review.context_lines)

        chunks = chunk_files(
            expanded,
            max_tokens=self.config.llm.max_context_tokens,
            provider=self.llm,
        )

        learned_rules: list[str] = []
        custom_rules: list[dict[str, str]] = []
        try:
            pr_info = getattr(self, "_pr_info", None)
            if pr_info is not None:
                _rules_store = IndexStore.open(pr_info.owner, pr_info.repo)

                learned_rules = _rules_store.get_learned_rules_text()

                for ctx in _rules_store.list_review_context():
                    custom_rules.append({"title": ctx.title, "content": ctx.content})

                _rules_store.close()

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
                        raw_response = await agentic_review_loop(self.llm, messages, executor)
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

        review_task = _asyncio.gather(*[_review_chunk(i, c) for i, c in enumerate(chunks)])
        security_task = _asyncio.create_task(
            security_review_pass(
                self.llm,
                filtered,
                _security_relevant_files(filtered),
                pr_title,
                indexing_llm=self.indexing_llm,
            )
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

        all_comments = [classify_severity(c) for c in all_comments]

        final_comments = filter_noise(
            all_comments,
            self.config.filter,
            review_round=review_round,
        )

        # Self-critique catches confident-but-wrong claims that the noise
        # filter can't, since confidence scores are LLM-generated too. Pass
        # the team's documented preferences so the critic doesn't strip
        # findings that enforce them as "style nits".
        if final_comments and self.config.review.self_critique:
            try:
                final_comments = await self_critique(
                    self.llm,
                    final_comments,
                    learned_rules=learned_rules or None,
                    custom_rules=custom_rules or None,
                    indexing_llm=self.indexing_llm,
                )
            except Exception as exc:
                logger.warning("Self-critique pass failed, keeping original comments: %s", exc)

        all_key_issues = _drop_orphan_key_issues(all_key_issues, final_comments)

        if self.config.review.include_summary:
            original_summary = " ".join(summaries) if summaries else ""
            # Regenerate from the FINAL filed outputs so summary prose can't
            # claim issues that were dropped by the filter/critique passes.
            try:
                summary = await regenerate_summary(
                    self.llm,
                    final_comments,
                    all_key_issues,
                    pr_title,
                    pr_description,
                    fallback=original_summary,
                    indexing_llm=self.indexing_llm,
                )
            except Exception as exc:
                logger.warning("Summary regeneration failed, using original: %s", exc)
                summary = original_summary or "No issues found."
        else:
            summary = ""

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
