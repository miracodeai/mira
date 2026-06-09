"""Prompt builder for cross-PR overlap detection."""

from __future__ import annotations

from mira.models import OpenPRRef, PRFingerprint, PRInfo

# Keep candidate bodies from blowing up the prompt — the title plus the lede of
# the description carries the intent; the full body rarely adds signal.
_MAX_BODY_CHARS = 600


def _truncate(text: str, limit: int = _MAX_BODY_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def build_overlap_prompt(
    pr_info: PRInfo,
    current: PRFingerprint,
    candidates: list[tuple[OpenPRRef, PRFingerprint, list[str]]],
) -> list[dict[str, str]]:
    """Build the messages asking the LLM to judge cross-PR overlap.

    ``candidates`` is the pre-filtered shortlist: each entry is
    ``(ref, fingerprint, shared_files)``. The model classifies each candidate
    relative to the PR under review and returns a JSON verdict per candidate.
    """
    system = (
        "You are a code-review assistant judging whether two open pull requests "
        "are stepping on each other. For each candidate PR, decide its "
        "relationship to the PR under review and classify it as one of:\n"
        '- "merge_conflict": they edit the same code, so merging both will '
        "collide or one will silently clobber the other.\n"
        '- "duplicate_effort": they pursue the same goal or implement the same '
        "feature/fix, even if via different files — redundant work.\n"
        '- "both": both of the above.\n'
        '- "none": no meaningful overlap; the shared files are incidental '
        "(e.g. both bump the same lockfile or touch an unrelated shared index).\n\n"
        'Be conservative: prefer "none" unless the overlap is real and worth a '
        "reviewer's attention. Reason from the titles, descriptions, and shared "
        "files — do not invent details you cannot see.\n\n"
        "Respond with ONLY a JSON object of this exact shape:\n"
        '{"overlaps": [{"pr_number": <int>, "kind": "merge_conflict|duplicate_effort|both|none", '
        '"reason": "<one concise sentence>", "confidence": <0.0-1.0>}]}\n'
        "Include one entry for every candidate PR."
    )

    lines: list[str] = []
    lines.append(f"## PR under review — #{pr_info.number}: {current.title}")
    if current.body:
        lines.append(_truncate(current.body))
    lines.append("")
    lines.append("Files changed:")
    for p in current.paths[:50]:
        lines.append(f"- {p}")
    lines.append("")
    lines.append("## Candidate open PRs")
    for ref, fp, shared in candidates:
        lines.append("")
        lines.append(f"### PR #{ref.number}: {fp.title}")
        if fp.body:
            lines.append(_truncate(fp.body))
        if shared:
            shared_str = ", ".join(shared[:20])
            lines.append(f"Files shared with the PR under review: {shared_str}")
        else:
            lines.append("Files shared with the PR under review: none")
            if fp.paths:
                lines.append("Its changed files: " + ", ".join(fp.paths[:20]))

    user = "\n".join(lines)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
