"""LLM judge: match Mira's filed comments against ground-truth findings.

One LLM call per PR. The judge sees the labeled findings and the filed
comments, and returns a matching via a forced tool call. TP/FP/FN are
counted in plain Python from that matching, so the judge never does
arithmetic.

The matching rule mirrors the Martian bench: a comment matches a finding
if it identifies the same underlying defect in the same file — wording
and exact line numbers don't matter, misdiagnosis does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from mira.llm.provider import LLMProvider
from mira.models import ReviewComment

SUBMIT_JUDGEMENT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_judgement",
        "description": (
            "Submit a verdict for each filed comment: which ground-truth "
            "finding it identifies (if any) and whether it restates an "
            "earlier comment."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "comment_verdicts": {
                    "type": "array",
                    "description": "One entry per filed comment, in input order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "comment_index": {"type": "integer"},
                            "addresses_finding": {
                                "type": ["string", "null"],
                                "description": (
                                    "The id of the ground-truth finding this comment "
                                    "identifies, or null if it addresses none of them."
                                ),
                            },
                            "duplicate_of_comment": {
                                "type": ["integer", "null"],
                                "description": (
                                    "Index of an earlier comment that flags the SAME "
                                    "underlying issue (e.g. an inline comment and a summary "
                                    "restating it), or null. Used to avoid counting one "
                                    "issue as multiple false positives."
                                ),
                            },
                            "reason": {
                                "type": "string",
                                "description": "One short sentence justifying the verdict.",
                            },
                        },
                        "required": ["comment_index", "addresses_finding"],
                    },
                },
            },
            "required": ["comment_verdicts"],
        },
    },
}


@dataclass
class JudgeResult:
    """Per-PR judgement: which findings were caught, which comments were noise."""

    tp: int = 0
    fp: int = 0
    fn: int = 0
    matched: dict[str, int] = field(default_factory=dict)  # finding_id -> comment index
    missed: list[str] = field(default_factory=list)  # finding_ids
    fp_indices: list[int] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)  # finding_id -> judge reason


def _findings_block(findings: list[dict]) -> str:
    lines = []
    for f in findings:
        loc = f.get("path") or "(file unknown)"
        if f.get("line_start"):
            loc += f":{f['line_start']}"
            if f.get("line_end") and f["line_end"] != f["line_start"]:
                loc += f"-{f['line_end']}"
        lines.append(
            f"[{f['id']}] ({f.get('category', '?')}/{f.get('severity', '?')}) {loc}\n"
            f"    {f['description']}"
        )
    return "\n".join(lines)


def _comments_block(comments: list[ReviewComment]) -> str:
    if not comments:
        return "(the tool filed no comments)"
    lines = []
    for i, c in enumerate(comments):
        body = (c.body or "").strip().replace("\n", " ")[:400]
        lines.append(f"[{i}] {c.path}:{c.line} — {c.title}\n    {body}")
    return "\n".join(lines)


def build_judge_prompt(findings: list[dict], comments: list[ReviewComment]) -> str:
    return (
        "You are scoring an automated code-review tool against ground-truth "
        "findings for one pull request.\n\n"
        "For each filed comment, decide which ground-truth finding it "
        "identifies, if any. Do these describe the same underlying issue? "
        "Different wording is fine if it's the same problem. Rules:\n"
        "- Match on root cause, not phrasing. A comment that flags the right "
        "code but misdiagnoses the defect addresses NO finding.\n"
        "- The file must agree when the finding specifies one; line numbers "
        "may differ slightly.\n"
        "- More than one comment may address the same finding.\n"
        "- If two comments flag the SAME issue (e.g. an inline note and a "
        "summary line restating it), mark the later one `duplicate_of_comment` "
        "the earlier — they should not count as separate false positives.\n"
        "- A comment addressing no finding and not a duplicate is a false positive.\n\n"
        "## Ground-truth findings\n\n"
        + _findings_block(findings)
        + "\n\n## Filed comments\n\n"
        + _comments_block(comments)
    )


def _cluster_roots(n: int, verdicts_by_idx: dict[int, dict]) -> dict[int, int]:
    """Union-find over duplicate_of edges. Returns comment index -> cluster root."""
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for i, v in verdicts_by_idx.items():
        dup = v.get("duplicate_of_comment")
        if isinstance(dup, int) and 0 <= dup < n and dup != i:
            union(i, dup)

    return {i: find(i) for i in range(n)}


def count_results(
    findings: list[dict],
    comments: list[ReviewComment],
    verdicts: list[dict],
) -> JudgeResult:
    """Count TP/FN/FP from per-comment verdicts.

    TP = findings addressed by >=1 comment; FN = the rest. FP = comment
    *clusters* (a comment plus everything transitively marked a duplicate of
    it) where no member addresses any finding — so one issue flagged in
    several places counts once, mirroring Martian's dedup.
    """
    result = JudgeResult()
    valid_ids = {f["id"] for f in findings}
    n = len(comments)

    by_idx: dict[int, dict] = {}
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        idx = v.get("comment_index")
        if isinstance(idx, int) and 0 <= idx < n:
            by_idx[idx] = v

    # comment index -> finding id it addresses (validated)
    addresses: dict[int, str] = {}
    for idx, v in by_idx.items():
        fid = v.get("addresses_finding")
        if isinstance(fid, str) and fid in valid_ids:
            addresses[idx] = fid

    for f in findings:
        hits = [idx for idx, fid in addresses.items() if fid == f["id"]]
        if hits:
            first = min(hits)
            result.matched[f["id"]] = first
            result.reasons[f["id"]] = str(by_idx[first].get("reason", ""))[:200]
        else:
            result.missed.append(f["id"])

    roots = _cluster_roots(n, by_idx)
    addressing_roots = {roots[idx] for idx in addresses}
    fp_indices = [i for i in range(n) if roots[i] not in addressing_roots]
    # Report one representative index per false-positive cluster.
    fp_cluster_roots = {roots[i] for i in fp_indices}

    result.tp = len(result.matched)
    result.fn = len(result.missed)
    result.fp_indices = sorted(fp_indices)
    result.fp = len(fp_cluster_roots)
    return result


async def judge_pr(
    fixture: dict,
    comments: list[ReviewComment],
    judge_llm: LLMProvider,
) -> JudgeResult:
    """Judge one PR's review output against its ground-truth findings."""
    findings = fixture["findings"]
    if not comments:
        # Nothing filed: every finding is a miss, no judge call needed.
        return count_results(findings, comments, [])

    prompt = build_judge_prompt(findings, comments)
    raw = await judge_llm.complete_with_tools(
        messages=[{"role": "user", "content": prompt}],
        tools=[SUBMIT_JUDGEMENT_TOOL],
        temperature=0.0,
    )
    data = json.loads(raw) if raw else {}
    verdicts = data.get("comment_verdicts") or []
    return count_results(findings, comments, verdicts)


def prf1(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def aggregate(results: list[tuple[dict, JudgeResult]]) -> dict:
    """Roll up per-PR judgements into overall + per-language + per-slice scores."""

    def _bucket(pairs: list[tuple[dict, JudgeResult]]) -> dict:
        tp = sum(r.tp for _, r in pairs)
        fp = sum(r.fp for _, r in pairs)
        fn = sum(r.fn for _, r in pairs)
        p, r, f1 = prf1(tp, fp, fn)
        return {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": round(p, 4),
            "recall": round(r, 4),
            "f1": round(f1 * 100, 2),
            "prs": len(pairs),
        }

    report = {"overall": _bucket(results), "by_language": {}, "by_source": {}}
    for key, attr in (("by_language", "language"), ("by_source", "source")):
        values = sorted({fx.get(attr, "?") for fx, _ in results})
        for v in values:
            report[key][v] = _bucket([(fx, r) for fx, r in results if fx.get(attr, "?") == v])
    return report
