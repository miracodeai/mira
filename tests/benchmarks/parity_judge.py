"""Blind pooled judge for the Kory Source vs Greptile comparison (HAI-11607).

The judge never learns that there are two reviewers. Both reviewers' comments
for one snapshot (plus earlier Greptile comments on the same PR, as context)
are reduced to neutral text, pooled, and shuffled by a seed stored in the
artifact. The judge clusters comments into issues, decides whether each issue
is a real defect in the shown diff, assigns severity, and rates the evidence
in each comment. Python then maps comments back to their reviewers and does
every count, so a stored judge response can be rescored without a model.

``rescore_unit`` and ``score_unit`` take only what the artifact stores
(``pool`` with owners, the raw judge JSON), which is what makes verdicts
reproducible from the artifact.
"""

from __future__ import annotations

import hashlib
import json
import random
from typing import Any

KORY = "kory"
GREPTILE = "greptile"
PRIOR = "prior"
REVIEWERS = (KORY, GREPTILE)

SEVERITY_WEIGHT = {"critical": 4, "major": 2, "minor": 1, "nit": 0}
VERDICTS = ("real", "not_real", "uncertain")
_MAX_DIFF_CHARS = 40_000

JUDGE_PROMPT_VERSION = "hai-11607-v2"

SUBMIT_ISSUES_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_issues",
        "description": "Group the review comments into issues and judge each issue.",
        "parameters": {
            "type": "object",
            "properties": {
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "comment_indices": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "description": "Every comment that describes this same root cause.",
                            },
                            "verdict": {"type": "string", "enum": list(VERDICTS)},
                            "severity": {
                                "type": "string",
                                "enum": list(SEVERITY_WEIGHT),
                                "description": "Impact if real. Use nit for style-only.",
                            },
                            "reason": {"type": "string"},
                        },
                        "required": ["comment_indices", "verdict", "severity"],
                    },
                },
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "comment_index": {"type": "integer"},
                            "quality": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 3,
                                "description": (
                                    "0 wrong or unsupported; 1 vague; 2 names the code and the "
                                    "mechanism; 3 also gives a concrete trigger or a correct fix."
                                ),
                            },
                        },
                        "required": ["comment_index", "quality"],
                    },
                },
            },
            "required": ["issues", "evidence"],
        },
    },
}


def build_pool(
    unit_id: str,
    run_label: str,
    kory: list[dict],
    greptile: list[dict],
    prior: list[dict],
    seed: int,
) -> tuple[list[dict], int]:
    """Shuffle all comments into one anonymous list. Returns (pool, shuffle_seed).

    The shuffle seed is derived from the run seed, the unit, and the run label,
    so every (unit, run) has its own order and the order can be rebuilt.
    """
    tagged = (
        [(KORY, c) for c in kory] + [(GREPTILE, c) for c in greptile] + [(PRIOR, c) for c in prior]
    )
    digest = hashlib.sha256(f"{seed}:{unit_id}:{run_label}".encode()).hexdigest()
    shuffle_seed = int(digest[:12], 16)
    random.Random(shuffle_seed).shuffle(tagged)
    pool = []
    for index, (owner, comment) in enumerate(tagged):
        pool.append(
            {
                "index": index,
                "owner": owner,
                "source_id": comment.get("id", ""),
                "path": comment.get("path", ""),
                "line": int(comment.get("line") or 0),
                "title": comment.get("title", ""),
                "body": comment.get("body", ""),
            }
        )
    return pool, shuffle_seed


def _trim_diff(diff_text: str, paths: set[str]) -> str:
    """Keep the files that comments point at, then cap the size."""
    blocks: list[str] = []
    current: list[str] = []
    keep = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current and keep:
                blocks.append("\n".join(current))
            current = [line]
            keep = any(f" b/{p}" in line or f" a/{p} " in line for p in paths) if paths else True
        else:
            current.append(line)
    if current and keep:
        blocks.append("\n".join(current))
    text = "\n".join(blocks) or diff_text
    if len(text) > _MAX_DIFF_CHARS:
        text = text[:_MAX_DIFF_CHARS] + "\n… (diff truncated)"
    return text


def build_prompt(pr_title: str, diff_text: str, pool: list[dict]) -> str:
    paths = {c["path"] for c in pool if c["path"]}
    lines = []
    for c in pool:
        loc = f"{c['path']}:{c['line']}" if c["line"] else c["path"] or "(no file)"
        lines.append(f"[{c['index']}] {loc} — {c['title']}\n    {c['body'].replace(chr(10), ' ')}")
    comments = "\n".join(lines) if lines else "(no comments)"
    return (
        "You are auditing code-review comments on one pull request snapshot. The comments "
        "come from several sources in random order. Judge each on the code shown, not on "
        "who might have written it.\n\n"
        "1. Group comments into issues. Comments belong together only if they describe the "
        "same root cause in the same place. Every comment index appears in exactly one issue.\n"
        "2. For each issue decide `real` (a defect a careful maintainer would fix: wrong "
        "behavior, security hole, data loss, crash, broken contract, missing required "
        "handling), `not_real` (incorrect, already handled, speculative, or pure style), or "
        "`uncertain` (the diff alone cannot settle it).\n"
        "3. Give the severity it would have if real: critical, major, minor, or nit.\n"
        "4. Rate the evidence in every comment from 0 to 3.\n\n"
        f"## Pull request\n\n{pr_title}\n\n"
        f"## Diff at this snapshot\n\n```diff\n{_trim_diff(diff_text, paths)}\n```\n\n"
        f"## Comments\n\n{comments}\n"
    )


def normalize_response(raw: dict, n: int) -> dict:
    """Make the judge output total: each index in exactly one issue, each rated."""
    seen: set[int] = set()
    issues: list[dict] = []
    for item in raw.get("issues") or []:
        if not isinstance(item, dict):
            continue
        members = []
        for idx in item.get("comment_indices") or []:
            if isinstance(idx, int) and 0 <= idx < n and idx not in seen:
                members.append(idx)
                seen.add(idx)
        if not members:
            continue
        verdict = item.get("verdict") if item.get("verdict") in VERDICTS else "uncertain"
        severity = item.get("severity") if item.get("severity") in SEVERITY_WEIGHT else "minor"
        issues.append(
            {
                "members": sorted(members),
                "verdict": verdict,
                "severity": severity,
                "reason": str(item.get("reason", ""))[:300],
                "judged": True,
            }
        )
    for idx in range(n):
        if idx not in seen:
            issues.append(
                {
                    "members": [idx],
                    "verdict": "uncertain",
                    "severity": "minor",
                    "reason": "judge omitted this comment",
                    "judged": False,
                }
            )
    quality: dict[int, int] = {}
    for item in raw.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        idx, q = item.get("comment_index"), item.get("quality")
        if isinstance(idx, int) and 0 <= idx < n and isinstance(q, int) and idx not in quality:
            quality[idx] = max(0, min(3, q))
    return {"issues": issues, "quality": quality}


def _reviewer_scores(
    issues: list[dict], pool: list[dict], quality: dict[int, int], who: str
) -> dict:
    scored = [i for i in issues if not i["prior"]]
    flagged = [i for i in scored if who in i["owners"]]
    real = [i for i in scored if i["verdict"] == "real"]
    tp = [i for i in flagged if i["verdict"] == "real"]
    own_real_q = [
        quality[idx]
        for i in tp
        for idx in i["members"]
        if pool[idx]["owner"] == who and idx in quality
    ]
    return {
        "comments": sum(1 for c in pool if c["owner"] == who),
        "issues_flagged": len(flagged),
        "tp": len(tp),
        "fp": sum(1 for i in flagged if i["verdict"] == "not_real"),
        "uncertain": sum(1 for i in flagged if i["verdict"] == "uncertain"),
        "missed": len(real) - len(tp),
        "weighted_tp": sum(SEVERITY_WEIGHT[i["severity"]] for i in tp),
        "weighted_missed": sum(SEVERITY_WEIGHT[i["severity"]] for i in real if i not in tp),
        "by_severity": {sev: sum(1 for i in tp if i["severity"] == sev) for sev in SEVERITY_WEIGHT},
        "evidence_sum": sum(own_real_q),
        "evidence_n": len(own_real_q),
        "corroborated_tp": sum(1 for i in tp if i["fixed_later"]),
    }


def score_unit(pool: list[dict], judge_raw: dict, evidence: dict | None = None) -> dict:
    """Score one judged snapshot. Pure: the same inputs give the same scores."""
    from .parity_snapshot import point_key

    norm = normalize_response(judge_raw or {}, len(pool))
    changed = (evidence or {}).get("changed_before_merge") or {}
    issues = []
    for item in norm["issues"]:
        owners = sorted({pool[i]["owner"] for i in item["members"]})
        issues.append(
            {
                **item,
                "owners": owners,
                "prior": PRIOR in owners,
                "fixed_later": any(
                    changed.get(point_key(pool[i]["path"], pool[i]["line"]), False)
                    for i in item["members"]
                ),
            }
        )
    scored = [i for i in issues if not i["prior"]]
    kory = _reviewer_scores(issues, pool, norm["quality"], KORY)
    greptile = _reviewer_scores(issues, pool, norm["quality"], GREPTILE)

    # Greptile as golden truth (the Mira-style F1, on a matched snapshot).
    g_issues = [i for i in scored if GREPTILE in i["owners"]]
    k_issues = [i for i in scored if KORY in i["owners"]]
    golden = {
        "tp": sum(1 for i in g_issues if KORY in i["owners"]),
        "fn": sum(1 for i in g_issues if KORY not in i["owners"]),
        "fp": sum(1 for i in k_issues if GREPTILE not in i["owners"]),
    }

    agreement = {
        f"{who}_{verdict}": sum(
            1 for i in scored if i["owners"] == sorted(owners) and i["verdict"] == verdict
        )
        for who, owners in (
            ("both", REVIEWERS),
            ("kory_only", (KORY,)),
            ("greptile_only", (GREPTILE,)),
        )
        for verdict in VERDICTS
    }
    cross = {
        f"{verdict}_fixed_later": sum(
            1 for i in scored if i["verdict"] == verdict and i["fixed_later"]
        )
        for verdict in VERDICTS
    } | {
        f"{verdict}_total": sum(1 for i in scored if i["verdict"] == verdict)
        for verdict in VERDICTS
    }
    return {
        "issues": issues,
        "kory": kory,
        "greptile": greptile,
        "golden": golden,
        "agreement": agreement,
        "cross_check": cross,
        "unjudged_comments": sum(len(i["members"]) for i in issues if not i["judged"]),
    }


async def judge_unit(
    judge_llm: Any,
    *,
    unit_id: str,
    run_label: str,
    pr_title: str,
    diff_text: str,
    kory: list[dict],
    greptile: list[dict],
    prior: list[dict],
    seed: int,
) -> dict:
    """One judge call. Returns the record the artifact stores."""
    pool, shuffle_seed = build_pool(unit_id, run_label, kory, greptile, prior, seed)
    prompt = build_prompt(pr_title, diff_text, pool)
    raw_text = ""
    error = ""
    if pool:
        try:
            raw_text = await judge_llm.complete_with_tools(
                messages=[{"role": "user", "content": prompt}],
                tools=[SUBMIT_ISSUES_TOOL],
                temperature=0.0,
            )
        except Exception as exc:
            error = str(exc)[:500]
    try:
        raw = json.loads(raw_text) if raw_text else {}
    except json.JSONDecodeError as exc:
        raw, error = {}, error or f"judge returned invalid JSON: {exc}"
    return {
        "prompt_version": JUDGE_PROMPT_VERSION,
        "shuffle_seed": shuffle_seed,
        "prompt": prompt,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "pool": pool,
        "raw_response": raw_text,
        "parsed": raw,
        "error": error,
    }


def rescore_unit(record: dict) -> dict:
    """Rebuild a unit's scores from its stored judge record and evidence."""
    return score_unit(record["judge"]["pool"], record["judge"]["parsed"], record.get("evidence"))
