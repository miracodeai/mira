"""Shared data models for Mira."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

WALKTHROUGH_MARKER = "<!-- mira-walkthrough -->"
PR_SUMMARY_START = "<!-- mira-pr-summary-start -->"
PR_SUMMARY_END = "<!-- mira-pr-summary-end -->"


class FileChangeType(enum.Enum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


class Severity(enum.IntEnum):
    """Review comment severity, ordered from most to least severe."""

    BLOCKER = 4
    WARNING = 3
    SUGGESTION = 2
    NITPICK = 1

    @classmethod
    def from_str(cls, value: str) -> Severity:
        mapping = {
            "blocker": cls.BLOCKER,
            "critical": cls.BLOCKER,
            "error": cls.BLOCKER,
            "warning": cls.WARNING,
            "warn": cls.WARNING,
            "suggestion": cls.SUGGESTION,
            "suggest": cls.SUGGESTION,
            "nitpick": cls.NITPICK,
            "nit": cls.NITPICK,
            "style": cls.NITPICK,
        }
        normalized = value.strip().lower()
        if normalized in mapping:
            return mapping[normalized]
        return cls.SUGGESTION

    @property
    def emoji(self) -> str:
        return {
            Severity.BLOCKER: "\U0001f6d1",  # stop sign
            Severity.WARNING: "\u26a0\ufe0f",  # warning
            Severity.SUGGESTION: "\U0001f4a1",  # light bulb
            Severity.NITPICK: "\U0001f4ac",  # speech bubble
        }[self]


@dataclass
class HunkInfo:
    """A single diff hunk within a file."""

    source_start: int
    source_length: int
    target_start: int
    target_length: int
    content: str


@dataclass
class FileDiff:
    """Parsed diff for a single file."""

    path: str
    change_type: FileChangeType
    hunks: list[HunkInfo] = field(default_factory=list)
    language: str = ""
    old_path: str | None = None
    is_binary: bool = False
    added_lines: int = 0
    deleted_lines: int = 0

    @property
    def total_changes(self) -> int:
        return self.added_lines + self.deleted_lines


@dataclass
class PatchSet:
    """A collection of file diffs representing a PR's changes."""

    files: list[FileDiff] = field(default_factory=list)

    @property
    def total_files(self) -> int:
        return len(self.files)

    @property
    def total_additions(self) -> int:
        return sum(f.added_lines for f in self.files)

    @property
    def total_deletions(self) -> int:
        return sum(f.deleted_lines for f in self.files)


def build_review_stats(comments: list[ReviewComment]) -> dict[Severity, int]:
    """Count review comments grouped by severity.

    Returns a mapping of severity → count, only including severities with > 0 comments.
    """
    counts: dict[Severity, int] = {}
    for c in comments:
        counts[c.severity] = counts.get(c.severity, 0) + 1
    return counts


@dataclass
class KeyIssue:
    """A critical issue highlighted for human reviewers."""

    issue: str
    path: str
    line: int


@dataclass
class ReviewComment:
    """A single review comment to post."""

    path: str
    line: int
    end_line: int | None
    severity: Severity
    category: str
    title: str
    body: str
    confidence: float
    suggestion: str | None = None
    agent_prompt: str | None = None
    # Verbatim diff snippet used by self-critique; stripped before posting.
    existing_code: str = ""
    # Which pipeline pass produced this ("main", "security", or "osv") — lets eval
    # artifacts attribute FP share per pass. Not posted anywhere.
    source_pass: str = "main"


def _format_stats_breakdown(stats: dict[Severity, int]) -> str:
    """Format severity counts as a parenthetical breakdown, e.g. ' (1 blocker, 2 warnings)'."""
    labels = {
        Severity.BLOCKER: "blocker",
        Severity.WARNING: "warning",
        Severity.SUGGESTION: "suggestion",
        Severity.NITPICK: "nitpick",
    }
    items: list[str] = []
    for sev in (Severity.BLOCKER, Severity.WARNING, Severity.SUGGESTION, Severity.NITPICK):
        count = stats.get(sev, 0)
        if count:
            name = labels[sev]
            items.append(f"{sev.emoji} {count} {name}{'s' if count != 1 else ''}")
    return f" ({', '.join(items)})" if items else ""


@dataclass
class WalkthroughConfidenceScore:
    """Confidence score for merge readiness."""

    score: int
    label: str
    reason: str


@dataclass
class WalkthroughEffort:
    """Review effort estimate for a PR."""

    level: int
    label: str
    minutes: int


@dataclass
class WalkthroughFileEntry:
    """A single file entry in the walkthrough summary."""

    path: str
    change_type: FileChangeType
    description: str
    group: str = ""


@dataclass
class ChunkFailure:
    """A review chunk that produced no usable structured review."""

    chunk_index: int
    paths: list[str] = field(default_factory=list)
    reason: str = ""
    hops: int | None = None
    excerpt: str = ""


@dataclass
class WalkthroughResult:
    """Result of the PR walkthrough generation."""

    summary: str = ""
    file_changes: list[WalkthroughFileEntry] = field(default_factory=list)
    effort: WalkthroughEffort | None = None
    confidence_score: WalkthroughConfidenceScore | None = None
    sequence_diagram: str | None = None

    def to_markdown(
        self,
        bot_name: str = "miracodeai",
        review_stats: dict[Severity, int] | None = None,
        existing_issues: int = 0,
        blast_radius: list[dict] | None = None,
        reviewed_files: int = 0,
        total_comments: int = 0,
        key_issues: list[KeyIssue] | None = None,
        in_progress: bool = False,
        skipped_paths: list[str] | None = None,
        total_paths: list[str] | None = None,
        index_was_empty: bool = False,
        dashboard_url: str = "",
        overlaps: list[OverlapFinding] | None = None,
        failure_notice: str | None = None,
        unverified_findings: list[ReviewComment] | None = None,
        failed_paths: list[str] | None = None,
        failed_chunks: list[ChunkFailure] | None = None,
        total_chunks: int = 0,
        security_failures: list[ChunkFailure] | None = None,
    ) -> str:
        """Render as a markdown PR comment.

        ``unverified_findings`` are draft findings the self-critique graded
        "plausible" but did not post; they are listed as not-verified hints.
        """
        parts = [WALKTHROUGH_MARKER, "## Mira PR Walkthrough", ""]
        parts.append(self.summary)

        failure_paths: list[str] = []
        for path in failed_paths or []:
            if path and path not in failure_paths:
                failure_paths.append(path)
        for failure in failed_chunks or []:
            for path in failure.paths:
                if path and path not in failure_paths:
                    failure_paths.append(path)

        if (failure_paths or failed_chunks) and not in_progress and not failure_notice:
            failed_count = len(failed_chunks) if failed_chunks else 1
            total_count = total_chunks or failed_count
            shown_paths = min(8, len(failure_paths))
            path_text = ", ".join(f"`{p}`" for p in failure_paths[:shown_paths])
            if len(failure_paths) > shown_paths:
                path_text += f" and {len(failure_paths) - shown_paths} more"
            path_text = path_text or "the affected files"
            parts.append("")
            parts.append(
                f"> ⚠️ **Review incomplete: {failed_count} of {total_count} "
                f"chunk{'s' if total_count != 1 else ''} failed.** No findings were "
                f"returned for {path_text}. Comment `@{bot_name} review-rest` to retry."
            )

        if security_failures and not in_progress and not failure_notice:
            security_paths: list[str] = []
            for failure in security_failures:
                for path in failure.paths:
                    if path and path not in security_paths:
                        security_paths.append(path)
            shown_security = min(6, len(security_paths))
            path_text = ", ".join(f"`{p}`" for p in security_paths[:shown_security])
            if len(security_paths) > shown_security:
                path_text += f" and {len(security_paths) - shown_security} more"
            suffix = f" for {path_text}" if path_text else ""
            parts.append("")
            parts.append(
                "> ⚠️ **Security review incomplete:** the security pass failed"
                f"{suffix}. Security findings may be missing from this review."
            )

        if self.sequence_diagram:
            # Hardening at render time — the single choke point every
            # comment path passes through. A diagram that is outside the
            # supported Mermaid subset is dropped rather than posted:
            # GitHub renders a broken ```mermaid fence as an ugly
            # "Unable to render rich display" error.
            from mira.llm.mermaid import harden_mermaid

            diagram = harden_mermaid(self.sequence_diagram)
            if diagram:
                parts.append("")
                parts.append("```mermaid")
                parts.append(diagram)
                parts.append("```")

        if self.confidence_score:
            cs = self.confidence_score
            score = cs.score
            filled = "\u25c9" * score  # ◉
            empty = "\u25cb" * (5 - score)  # ○
            label = cs.label if cs.label else ""
            parts.append("")
            parts.append(
                f"<details>\n"
                f"<summary><b>Confidence: {score}/5</b> &nbsp; {filled}{empty} &nbsp; {label}</summary>\n"
            )
            if cs.reason:
                parts.append(f"- {cs.reason}")
            if key_issues:
                parts.append("")
                parts.append("**Key files to review:**")
                for ki in key_issues:
                    parts.append(f"- `{ki.path}:{ki.line}` — {ki.issue}")
            parts.append("")
            parts.append("</details>")

        if unverified_findings and not in_progress:
            shown_findings = unverified_findings[:5]
            parts.append("")
            parts.append(
                "<details>\n"
                "<summary><b>Worth a look (not verified)</b> \u2014 "
                f"{len(unverified_findings)}</summary>\n"
            )
            parts.append(
                "_Possible issues Mira's self-check could not confirm from the diff. "
                "Not posted as comments; check them yourself._"
            )
            parts.append("")
            for f in shown_findings:
                parts.append(f"- `{f.path}:{f.line}` \u2014 {f.title}")
            if len(unverified_findings) > len(shown_findings):
                parts.append(f"- _\u2026and {len(unverified_findings) - len(shown_findings)} more_")
            parts.append("")
            parts.append("</details>")

        if overlaps:
            _kind_label = {
                "merge_conflict": "merge-conflict risk",
                "duplicate_effort": "duplicate effort",
                "both": "duplicate effort + merge-conflict risk",
            }
            parts.append("")
            parts.append(
                "> **⚠️ Potential overlap with other open PRs** — these may be stepping on this one:"
            )
            parts.append(">")
            for ov in overlaps:
                label = _kind_label.get(ov.kind, ov.kind)
                link = f"[#{ov.pr_number}]({ov.url})" if ov.url else f"#{ov.pr_number}"
                line = f"> - {link} ({label}) — {ov.reason}"
                if ov.shared_files:
                    shown = ", ".join(f"`{p}`" for p in ov.shared_files[:3])
                    if len(ov.shared_files) > 3:
                        shown += f" +{len(ov.shared_files) - 3} more"
                    line += f" Shared: {shown}"
                parts.append(line)
            parts.append("")

        if blast_radius:
            parts.append("")
            total_refs = sum(len(e.get("files", [])) for e in blast_radius)
            repo_count = len(blast_radius)
            header = f"{'repository' if repo_count == 1 else 'repositories'}"

            parts.append(
                f"> **Blast Radius** \u2014 {repo_count} dependent {header}, {total_refs} total references"
            )
            parts.append(">")
            for entry in blast_radius:
                repo = entry.get("repo", "")
                files = entry.get("files", [])
                parts.append(
                    f"> `{repo}` \u2014 {len(files)} reference{'s' if len(files) != 1 else ''}"
                )
            parts.append("")

        if in_progress:
            parts.append("")
            parts.append("*\u23f3 Code review in progress\u2026*")
        else:
            stats_parts: list[str] = []
            if reviewed_files:
                stats_parts.append(
                    f"{reviewed_files} file{'s' if reviewed_files != 1 else ''} reviewed"
                )
            if total_comments:
                comment_detail = _format_stats_breakdown(review_stats) if review_stats else ""
                stats_parts.append(
                    f"{total_comments} comment{'s' if total_comments != 1 else ''}{comment_detail}"
                )
            if existing_issues:
                stats_parts.append(
                    f"{existing_issues} unresolved thread{'s' if existing_issues != 1 else ''}"
                )
            if stats_parts:
                separator = " \u00b7 "
                parts.append("")
                parts.append(f"*{separator.join(stats_parts)}*")

        if skipped_paths and not in_progress:
            total = len(total_paths) if total_paths else (reviewed_files + len(skipped_paths))
            shown = min(8, len(skipped_paths))
            parts.append("")
            parts.append("---")
            parts.append("")
            parts.append(f"### \ud83d\udccb Reviewed {reviewed_files} of {total} files")
            parts.append("")
            parts.append(
                "This PR is large enough that some files were skipped to keep the "
                "review focused on the highest-priority changes. To review the rest, "
                f"comment `@{bot_name} review-rest` on this PR."
            )
            parts.append("")
            parts.append("**Skipped:**")
            for p in skipped_paths[:shown]:
                parts.append(f"- `{p}`")
            if len(skipped_paths) > shown:
                parts.append(f"- _\u2026and {len(skipped_paths) - shown} more_")

        if index_was_empty and not in_progress:
            parts.append("")
            parts.append("---")
            parts.append("")
            link = f"[Mira dashboard]({dashboard_url})" if dashboard_url else "the Mira dashboard"
            parts.append(
                f"> 💡 **This review will be more accurate after indexing.** "
                f"This repo hasn't been indexed yet, so the review is based on "
                f"the diff plus on-demand file lookups. Visit {link} to index "
                f"this repo — Mira will then know about callers, dependents, "
                f"and cross-repo impact."
            )

        if failure_notice:
            parts.append("")
            parts.append("---")
            parts.append("")
            parts.append(
                "<details>\n<summary><b>❌ Review failed</b> — click for details</summary>\n"
            )
            parts.append("")
            parts.append(failure_notice)
            parts.append("")
            parts.append("</details>")

        parts.append("")
        parts.append("---")
        parts.append(
            f"> Comment `@{bot_name} help` to get the list of available commands and usage tips."
        )

        return "\n".join(parts)


@dataclass
class ThreadDecision:
    """Per-thread resolution decision from dry-run."""

    thread_id: str
    path: str
    line: int
    body: str
    fixed: bool


@dataclass
class ReviewResult:
    """The complete result of a review."""

    comments: list[ReviewComment] = field(default_factory=list)
    key_issues: list[KeyIssue] = field(default_factory=list)
    summary: str = ""
    pr_summary_block: str = ""
    reviewed_files: int = 0
    skipped_reason: str | None = None
    token_usage: dict[str, int] = field(default_factory=dict)
    walkthrough: WalkthroughResult | None = None
    thread_decisions: list[ThreadDecision] = field(default_factory=list)
    # Surfaced in the walkthrough banner so @miracodeai review-rest can target the rest.
    reviewed_paths: list[str] = field(default_factory=list)
    skipped_paths: list[str] = field(default_factory=list)
    total_paths: list[str] = field(default_factory=list)
    # Chunks that produced no usable structured review. Kept separate from
    # size-skipped paths so the walkthrough can say "incomplete" rather than
    # "large PR"; both are retryable through review-rest.
    failed_chunks: list[ChunkFailure] = field(default_factory=list)
    failed_paths: list[str] = field(default_factory=list)
    total_chunks: int = 0
    security_failures: list[ChunkFailure] = field(default_factory=list)
    dependency_failures: list[ChunkFailure] = field(default_factory=list)
    # Diagnostic trail: per-chunk draft counts and every comment dropped by a
    # filter/critique stage, so a benchmark run can show whether a missed
    # finding was never drafted or drafted-then-dropped. Not posted anywhere.
    audit: list[dict] = field(default_factory=list)
    # Draft findings self-critique graded "plausible" but did not post. Shown
    # in the walkthrough only with review.show_unverified_findings.
    unverified_findings: list[ReviewComment] = field(default_factory=list)


@dataclass
class PRInfo:
    """Metadata about a pull request."""

    title: str
    description: str
    base_branch: str
    head_branch: str
    url: str
    number: int
    owner: str
    repo: str
    # Round 2+ reviews diff against last_reviewed_sha → head_sha; empty falls back to full diff.
    head_sha: str = ""
    # Hosting platform ("github" / "gitlab") — scopes per-PR review progress.
    platform: str = "github"
    # Platform login of the PR author; used to attribute review-quality stats
    # and surfaced (with avatar) in the activity dashboard.
    author: str = ""
    author_avatar_url: str = ""


@dataclass
class OpenPRRef:
    """Lightweight handle on another open PR in the same repo.

    Built from the GitHub "list pull requests" API — just enough to decide
    whether the PR is worth comparing against the one under review (without
    fetching its full diff up front).
    """

    number: int
    title: str
    body: str
    head_sha: str
    author: str
    draft: bool = False
    base_ref: str = ""
    head_ref: str = ""
    url: str = ""


@dataclass
class PRFingerprint:
    """A compact signature of a PR's changes, cached per repo.

    Populated for a PR the moment Mira reviews it (the diff is already in
    hand), so a later review of a *different* PR can compare against it with a
    cheap DB read instead of re-fetching this PR's files from GitHub.
    """

    pr_number: int
    head_sha: str
    title: str
    body: str
    paths: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    updated_at: float = 0.0


@dataclass
class OverlapFinding:
    """A confirmed overlap between the PR under review and another open PR."""

    pr_number: int
    url: str
    title: str
    # 'merge_conflict' (touch the same code) | 'duplicate_effort' (same goal)
    # | 'both'.
    kind: str
    reason: str
    confidence: float
    shared_files: list[str] = field(default_factory=list)


@dataclass
class UnresolvedThread:
    """An unresolved review thread authored by the bot."""

    thread_id: str
    path: str
    line: int
    body: str
    is_outdated: bool = False


@dataclass
class BotThreadRecord:
    """A review thread authored by the bot, resolved or not."""

    thread_id: str
    path: str
    line: int
    body: str
    is_resolved: bool
    is_outdated: bool = False


@dataclass
class HumanReviewComment:
    """A review comment on a PR authored by a human (not the bot)."""

    path: str
    line: int
    body: str
    author: str


@dataclass
class FileHistoryEntry:
    """A commit that previously touched a file. Used by decision archaeology
    to give the review LLM context on why code exists before suggesting it
    be changed or removed."""

    sha: str
    message: str
    author: str
    date: str  # ISO-8601 timestamp from the GitHub API


@dataclass
class ReviewChunk:
    """A chunk of files that fits within a single LLM context window."""

    files: list[FileDiff] = field(default_factory=list)
    token_estimate: int = 0


@dataclass
class FeedbackEvent:
    """A recorded feedback signal on a review comment."""

    id: int
    pr_number: int
    pr_url: str
    comment_path: str
    comment_line: int
    comment_category: str
    comment_severity: str
    comment_title: str
    signal: str  # 'rejected' | 'accepted'
    actor: str
    created_at: float = 0.0


@dataclass
class LearnedRule:
    """A rule synthesised from accumulated feedback patterns."""

    id: int
    rule_text: str
    source_signal: str  # 'reject_pattern' | 'accept_pattern'
    category: str
    path_pattern: str  # e.g. 'tests/**' or '' for all
    sample_count: int
    active: bool = True
    created_at: float = 0.0
    updated_at: float = 0.0
