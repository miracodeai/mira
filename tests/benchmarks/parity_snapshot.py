"""Matched snapshots for the Kory Source vs Greptile comparison (HAI-11607).

Greptile reviews a pull request at intermediate commits. Its inline comment
carries ``original_commit_id``, the commit it actually saw. Reviewing the
merged head instead would show Kory code in which many Greptile findings are
already fixed. So the unit of comparison is one PR at one commit Greptile
reviewed:

* one unit per distinct ``original_commit_id`` among the inline comments;
* a PR with a Greptile summary and no inline comments gives one clean unit at
  the summary's "Last reviewed commit";
* a PR with no Greptile output at all gives a unit with
  ``greptile_reviewed = False``. It is reported as a coverage gap and never
  scored, so missing Greptile output can never count as a Kory win.

Greptile posts only new comments on later rounds. Findings from earlier units
of the same PR travel with a unit as ``prior``. The judge sees them, and
clusters that contain one are left out of both reviewers' scores.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any

from mira.models import PRInfo
from mira.providers.github import GitHubProvider

_SHA_IN_SUMMARY = re.compile(r"Last reviewed commit:.*?/commit/([0-9a-f]{40})", re.S)
_DETAILS = re.compile(r"<details>.*?</details>", re.S | re.I)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_TAG = re.compile(r"<[^>]+>")
_BADGE_WORD = re.compile(r"^\s*(P[0-3]|blocker|warning|nit(pick)?|suggestion)\s*[:\-–—]?\s*", re.I)
_IDENTITY = re.compile(r"\b(greptile|mira|miracodeai|kory(\s+source)?|coderabbit)\b", re.I)
_MAX_BODY = 700


def neutral_text(text: str) -> str:
    """Strip reviewer fingerprints: HTML, badges, fix-prompt blocks, names."""
    text = _DETAILS.sub(" ", text or "")
    text = _HTML_COMMENT.sub(" ", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("```suggestion", "```")
    text = _IDENTITY.sub("[reviewer]", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text


def split_title(text: str) -> tuple[str, str]:
    """Greptile writes ``**Title**`` then the body. Return (title, body)."""
    match = re.match(r"\s*\*\*(.+?)\*\*\s*(.*)", text, re.S)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    first, _, rest = text.strip().partition("\n")
    return first.strip(), rest.strip()


def neutral_comment(path: str, line: int | None, title: str, body: str) -> dict[str, Any]:
    title = _BADGE_WORD.sub("", neutral_text(title)).strip()
    body = neutral_text(body)
    if len(body) > _MAX_BODY:
        body = body[:_MAX_BODY].rstrip() + " …"
    return {"path": path or "", "line": int(line or 0), "title": title[:200], "body": body}


@dataclass(frozen=True)
class Unit:
    unit_id: str
    repo: str
    pr_url: str
    pr_number: int
    pr_title: str
    split: str
    merged_at: str
    base_sha: str
    sha: str
    final_head_sha: str
    greptile_reviewed: bool
    greptile_at: str = ""
    golden: tuple[dict, ...] = field(default_factory=tuple)
    prior: tuple[dict, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "repo": self.repo,
            "pr_url": self.pr_url,
            "pr_number": self.pr_number,
            "split": self.split,
            "base_sha": self.base_sha,
            "sha": self.sha,
            "final_head_sha": self.final_head_sha,
            "greptile_reviewed": self.greptile_reviewed,
            "greptile_at": self.greptile_at,
            "golden_ids": [g["id"] for g in self.golden],
            "prior_ids": [g["id"] for g in self.prior],
        }


def _golden(finding: dict, comment: dict) -> dict:
    title, body = split_title(neutral_text(finding.get("description") or comment.get("body", "")))
    text = neutral_comment(
        finding.get("path") or comment.get("path", ""),
        finding.get("line_start") or comment.get("original_line"),
        title,
        body,
    )
    return {
        "id": finding["id"],
        **text,
        "line_end": finding.get("line_end") or comment.get("original_line"),
        "greptile_severity": finding.get("severity", ""),
        "thread_resolved": bool(comment.get("thread_resolved")),
        "thread_outdated": bool(comment.get("thread_outdated")),
        "created_at": comment.get("created_at", ""),
        "url": comment.get("url", ""),
    }


def units_for_fixture(fixture: dict) -> list[Unit]:
    inline = [c for c in fixture.get("greptile_comments", []) if c.get("kind") == "inline"]
    summaries = [c for c in fixture.get("greptile_comments", []) if c.get("kind") == "summary"]
    findings = fixture.get("findings", [])
    if len(inline) != len(findings):
        raise ValueError(
            f"{fixture['pr_url']}: {len(inline)} inline comments, {len(findings)} findings"
        )
    common = {
        "repo": fixture["repo"],
        "pr_url": fixture["pr_url"],
        "pr_number": int(fixture["pr_number"]),
        "pr_title": fixture.get("pr_title", ""),
        "split": fixture.get("split", ""),
        "merged_at": fixture.get("merged_at", ""),
        "base_sha": fixture["base_sha"],
        "final_head_sha": fixture["head_sha"],
    }

    groups: dict[str, list[dict]] = {}
    for comment, finding in zip(inline, findings, strict=True):
        sha = comment.get("original_commit_id") or comment.get("commit_id") or fixture["head_sha"]
        groups.setdefault(sha, []).append(_golden(finding, comment))

    if not groups:
        last = None
        for summary in summaries:
            match = _SHA_IN_SUMMARY.search(summary.get("body", ""))
            if match:
                last = match.group(1)
        sha = last or fixture["head_sha"]
        return [
            Unit(
                unit_id=f"{common['pr_number']}@{sha[:12]}",
                sha=sha,
                greptile_reviewed=bool(summaries),
                greptile_at=summaries[0].get("created_at", "") if summaries else "",
                **common,
            )
        ]

    ordered = sorted(groups.items(), key=lambda item: min(g["created_at"] for g in item[1]))
    units: list[Unit] = []
    prior: list[dict] = []
    for sha, golden in ordered:
        units.append(
            Unit(
                unit_id=f"{common['pr_number']}@{sha[:12]}",
                sha=sha,
                greptile_reviewed=True,
                greptile_at=min(g["created_at"] for g in golden),
                golden=tuple(golden),
                prior=tuple(prior),
                **common,
            )
        )
        prior = prior + golden
    return units


def load_units(fixtures: list[dict], split: str = "all") -> list[Unit]:
    out: list[Unit] = []
    for fixture in fixtures:
        if split != "all" and fixture.get("split") != split:
            continue
        out.extend(units_for_fixture(fixture))
    return out


_GREPTILE_LINE = re.compile(r"greptile", re.I)


class PinnedGitHubProvider(GitHubProvider):
    """A read-only GitHub provider pinned to one unit's snapshot.

    The diff is ``base...sha`` (GitHub three-dot compare, the same merge-base
    diff a PR shows). No bot threads are visible, so the engine stays in round
    one, and description lines that mention Greptile are dropped so later
    "addressed review" notes cannot leak findings.
    """

    def __init__(self, token: str, unit: Unit) -> None:
        super().__init__(token=token)
        self.unit = unit

    async def get_pr_info(self, pr_url: str) -> PRInfo:
        info = await super().get_pr_info(pr_url)
        info.head_sha = self.unit.sha
        info.description = "\n".join(
            line
            for line in (info.description or "").splitlines()
            if not _GREPTILE_LINE.search(line)
        )
        return info

    async def get_pr_diff(self, pr_info: PRInfo) -> str:
        return await self.get_compare_diff(pr_info, self.unit.base_sha, self.unit.sha)

    async def get_all_bot_threads(self, pr_info: PRInfo, bot_login: str | None = None) -> list:
        return []

    async def get_unresolved_bot_threads(
        self, pr_info: PRInfo, bot_login: str | None = None
    ) -> list:
        return []

    async def get_pr_description(self, pr_info: PRInfo) -> str:
        return pr_info.description


# ---------------------------------------------------------------- later evidence

_WINDOW = 2


def point_key(path: str, line: int) -> str:
    return f"{path}:{int(line or 0)}"


def window_survives(before: str, after: str | None, line: int) -> bool:
    """Whether the code around ``line`` in ``before`` still appears in ``after``.

    Compares the stripped lines ``line ± 2`` as a contiguous run, so line
    shifts, rebases, and base merges elsewhere in the file do not count as a
    change. ``line == 0`` asks whether the file is unchanged.
    """
    if after is None:
        return False
    if not line:
        return before == after
    old = [x.strip() for x in before.splitlines()]
    lo, hi = max(0, line - 1 - _WINDOW), min(len(old), line + _WINDOW)
    window = old[lo:hi]
    if not any(window):
        return True
    new = [x.strip() for x in after.splitlines()]
    size = len(window)
    return any(new[i : i + size] == window for i in range(len(new) - size + 1))


async def later_evidence(
    provider: GitHubProvider, unit: Unit, points: set[tuple[str, int]]
) -> dict[str, Any]:
    """For each flagged (path, line): was that code changed before the PR merged?

    Reads the file at the snapshot and at the PR's final head. Works across
    rebases and force-pushes, where commit compares do not.
    """
    owner, repo = unit.repo.split("/", 1)
    info = PRInfo(
        title=unit.pr_title,
        description="",
        base_branch="",
        head_branch="",
        url=unit.pr_url,
        number=unit.pr_number,
        owner=owner,
        repo=repo,
    )
    changed: dict[str, bool] = {}
    errors: list[str] = []
    if unit.sha == unit.final_head_sha:
        return {"changed_before_merge": {point_key(p, n): False for p, n in points}, "error": ""}
    for path in sorted({p for p, _ in points if p}):
        try:
            before = await provider.get_file_content(info, path, unit.sha)
        except Exception as exc:  # the comment points at a file the snapshot lacks
            errors.append(f"{path}@snapshot: {str(exc)[:120]}")
            continue
        try:
            after: str | None = await provider.get_file_content(info, path, unit.final_head_sha)
        except Exception:
            after = None  # deleted before merge
        for p, n in points:
            if p == path:
                changed[point_key(p, n)] = not window_survives(before, after, n)
    return {"changed_before_merge": changed, "error": "; ".join(errors)[:600]}


async def find_reverts(token: str, repo: str, pr_number: int, merged_at: str) -> list[str]:
    """Merged PRs in ``repo`` that revert ``pr_number`` (GitHub search, title ``Revert``)."""
    import httpx

    query = f'repo:{repo} is:pr is:merged "Revert" in:title "#{pr_number}" in:body'
    if merged_at:
        query += f" merged:>={merged_at[:10]}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            "https://api.github.com/search/issues",
            params={"q": query, "per_page": 20},
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
    return [item["html_url"] for item in items if item.get("number") != pr_number]
