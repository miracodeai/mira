"""Priority scoring for files in a PR.

When a PR is too big to review in one pass, we use these scores to decide
which files to review first. Higher score = higher priority. The intent is
that even when truncated, the review covers the *risky* parts of the change
rather than alphabetical-first ones.

Scores aren't absolute — they're used only for sorting. Anything below 0
means "low priority, deprioritize"; anything above 0 means "this matters."
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from mira.models import FileChangeType, FileDiff

# Path patterns that signal sensitive code. Hits boost priority.
# Trailing `s?` lets "payments/" and "payment/" both match without false-positives
# on unrelated words like "paymentmaster".
_SENSITIVE_PATTERNS = [
    re.compile(r"(^|/)(auth|authn|authz|security|secrets?|crypto|password|tokens?|jwt|oauth)s?/"),
    re.compile(r"(^|/)(payment|billing|charge|refund|stripe|checkout|payout)s?/"),
    re.compile(r"(^|/)(admin|permission|role|rbac|acl)s?/"),
    re.compile(r"(^|/)(migration|schema)s?/"),
    re.compile(r"(^|/)\.env"),
    re.compile(r"(^|/)Dockerfile"),
    re.compile(r"(^|/)(deploy|infra|terraform)/"),
]

# Path patterns that signal lower-risk code. Hits reduce priority.
_LOW_PRIORITY_PATTERNS = [
    re.compile(r"(^|/)(tests?|spec|fixtures?|__tests__|__mocks__)/"),
    re.compile(r"(^|/)(docs?|examples?)/"),
    re.compile(r"(^|/)(\.github|\.vscode|\.idea)/"),
    re.compile(r"(^|/)(README|CHANGELOG|LICENSE|CODE_OF_CONDUCT|CONTRIBUTING|SECURITY)\."),
    re.compile(r"\.(md|txt|rst|adoc)$"),
    re.compile(r"\.(svg|png|jpg|jpeg|gif|ico|woff2?|ttf|eot)$"),
    re.compile(r"(^|/)dist/"),
    re.compile(r"(^|/)build/"),
    re.compile(r"\.generated\."),
    re.compile(r"_generated\."),
    re.compile(r"\.pb\.(go|py|js|ts)$"),  # protobuf-generated
]

# Path patterns we never want to review. Score = -infinity so they sort last,
# and the chunker can also choose to drop them outright.
_NEVER_PATTERNS = [
    re.compile(r"\.lock$|^Pipfile\.lock$|package-lock\.json$|yarn\.lock$|pnpm-lock\.yaml$"),
    re.compile(r"poetry\.lock$|go\.sum$|Gemfile\.lock$|Cargo\.lock$"),
    re.compile(r"\.min\.(js|css)$"),
    re.compile(r"\.map$"),
]


@dataclass(frozen=True)
class FilePriority:
    """Sortable priority record for a file in a PR."""

    file_path: str
    score: float
    reasons: tuple[str, ...]


def _matches_any(patterns: list[re.Pattern[str]], path: str) -> bool:
    return any(p.search(path) for p in patterns)


def score_file(f: FileDiff, learned_reject_categories: set[str] | None = None) -> FilePriority:
    """Score a single FileDiff. Higher = higher priority.

    Heuristics:
      - +5 for sensitive paths (auth, payments, etc.)
      - +1 per 50 changed lines (capped at +5)
      - +2 if the file's category was previously learned-rejected by this team
      - -5 for low-priority paths (tests, docs, generated)
      - -100 for "never review" paths (lockfiles, minified)
      - +1 base bonus for added/modified files (deletions are less interesting
        unless they touch sensitive paths)
    """
    score = 0.0
    reasons: list[str] = []

    if _matches_any(_NEVER_PATTERNS, f.path):
        return FilePriority(f.path, -100.0, ("never-review path",))

    if _matches_any(_SENSITIVE_PATTERNS, f.path):
        score += 5.0
        reasons.append("sensitive path")

    if _matches_any(_LOW_PRIORITY_PATTERNS, f.path):
        score -= 5.0
        reasons.append("low-priority path")

    change_size_bonus = min(5.0, f.total_changes / 50.0)
    score += change_size_bonus
    if f.total_changes >= 100:
        reasons.append(f"{f.total_changes} line changes")

    if f.change_type == FileChangeType.ADDED:
        score += 1.0
        reasons.append("new file")
    elif f.change_type == FileChangeType.DELETED:
        score -= 1.0  # pure deletions less likely to need review
    else:
        score += 0.5

    # Learned-rule signal — categories the team has *rejected* before.
    # Rejections mean those reviews waste reviewer time, so we deprioritize.
    if learned_reject_categories:
        # Check if the file path strongly suggests one of the rejected categories
        # (rough heuristic — matches the category name as a path component).
        for cat in learned_reject_categories:
            if cat and cat.lower() in f.path.lower():
                score -= 2.0
                reasons.append(f"category '{cat}' often rejected by team")
                break

    return FilePriority(f.path, score, tuple(reasons))


def rank_files(
    files: list[FileDiff],
    learned_reject_categories: set[str] | None = None,
) -> list[tuple[FileDiff, FilePriority]]:
    """Rank files by priority, highest first. Returns (file, priority) pairs."""
    scored = [(f, score_file(f, learned_reject_categories)) for f in files]
    scored.sort(key=lambda x: (-x[1].score, x[0].path))
    return scored
