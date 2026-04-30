"""Shared data models for Mira."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

WALKTHROUGH_MARKER = "<!-- mira-walkthrough -->"


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
    ) -> str:
        """Render as a markdown PR comment."""
        parts = [WALKTHROUGH_MARKER, "## Mira PR Walkthrough", ""]
        parts.append(self.summary)

        if self.sequence_diagram:
            diagram = self.sequence_diagram.strip()
            # Only render if it looks like valid Mermaid
            if diagram and any(
                diagram.startswith(k) for k in ("graph ", "flowchart ", "sequenceDiagram")
            ):
                # Sanitise: wrap node names with special chars in quotes
                import re

                lines = []
                for line in diagram.splitlines():
                    # Fix bare node names with dots/slashes — wrap in quotes
                    line = re.sub(
                        r"(\w+\.[\w./\-]+)",
                        lambda m: f'"{m.group(0)}"' if '"' not in m.group(0) else m.group(0),
                        line,
                    )
                    lines.append(line)
                diagram = "\n".join(lines)
                parts.append("")
                parts.append("```mermaid")
                parts.append(diagram)
                parts.append("```")

        # --- Confidence score (collapsible) ---
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

        # --- Blast radius ---
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

        # --- Stats footer ---
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

        # --- Skipped-files banner (large-PR partial review) ---
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
    reviewed_files: int = 0
    skipped_reason: str | None = None
    token_usage: dict[str, int] = field(default_factory=dict)
    walkthrough: WalkthroughResult | None = None
    thread_decisions: list[ThreadDecision] = field(default_factory=list)
    # Paths selected for this review pass + paths intentionally skipped due to
    # size or priority caps. Surfaced in the walkthrough banner so users can
    # invoke `@mira-bot review-rest` to review the remainder.
    reviewed_paths: list[str] = field(default_factory=list)
    skipped_paths: list[str] = field(default_factory=list)
    total_paths: list[str] = field(default_factory=list)


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
