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
    sequence_diagram: str | None = None

    def to_markdown(
        self,
        bot_name: str = "miracodeai",
        review_stats: dict[Severity, int] | None = None,
    ) -> str:
        """Render as a markdown PR comment."""
        parts = [WALKTHROUGH_MARKER, "## Mira PR Walkthrough", ""]
        parts.append(self.summary)

        if self.effort:
            parts.append("")
            e = self.effort
            parts.append(
                f"**Estimated effort:** {e.level} ({e.label}) \u00b7 \u23f1\ufe0f ~{e.minutes} min"
            )

        if self.file_changes:
            parts.append("")
            parts.append("### Changes")

            # Check if any files have group labels
            has_groups = any(fc.group for fc in self.file_changes)

            def _file_row(fc: WalkthroughFileEntry) -> str:
                change = fc.change_type.value.capitalize()
                return f"| `{fc.path}` | {change} | {fc.description} |"

            if has_groups:
                # Collect groups preserving order of first appearance
                groups: dict[str, list[WalkthroughFileEntry]] = {}
                for fc in self.file_changes:
                    label = fc.group or "Other"
                    groups.setdefault(label, []).append(fc)
                for label, entries in groups.items():
                    parts.append("")
                    parts.append(f"**{label}**")
                    parts.append("")
                    parts.append("| File | Change | Description |")
                    parts.append("|------|--------|-------------|")
                    for fc in entries:
                        parts.append(_file_row(fc))
            else:
                parts.append("")
                parts.append("| File | Change | Description |")
                parts.append("|------|--------|-------------|")
                for fc in self.file_changes:
                    parts.append(_file_row(fc))

        if review_stats:
            total = sum(review_stats.values())
            parts.append("")
            parts.append("### Review Status")
            parts.append("")
            parts.append(f"Found **{total}** issue{'s' if total != 1 else ''}:")
            parts.append("")
            parts.append("| Severity | Count |")
            parts.append("|----------|-------|")
            for sev in sorted(review_stats, reverse=True):
                parts.append(f"| {sev.emoji} {sev.name.capitalize()} | {review_stats[sev]} |")

        if self.sequence_diagram:
            parts.append("")
            parts.append("### Sequence Diagram")
            parts.append("")
            parts.append("```mermaid")
            parts.append(self.sequence_diagram)
            parts.append("```")

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
    summary: str = ""
    reviewed_files: int = 0
    skipped_reason: str | None = None
    token_usage: dict[str, int] = field(default_factory=dict)
    walkthrough: WalkthroughResult | None = None
    thread_decisions: list[ThreadDecision] = field(default_factory=list)


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
class ReviewChunk:
    """A chunk of files that fits within a single LLM context window."""

    files: list[FileDiff] = field(default_factory=list)
    token_estimate: int = 0
