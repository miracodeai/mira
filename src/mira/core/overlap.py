"""Cross-PR overlap detection — flag other open PRs stepping on this one.

While reviewing a PR, Mira compares it against the repo's other open PRs and
surfaces the ones that either touch the same code (merge-conflict risk) or
pursue the same goal (duplicate effort). The pipeline is:

    list open PRs → build/lookup fingerprints → cheap pre-filter → LLM judgment

The deterministic pre-filter (file/symbol intersection + title similarity) runs
first so only genuinely-overlapping candidates reach the single batched LLM
call. Everything here is best-effort: any failure returns no findings rather
than blocking the review.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from mira.config import MiraConfig
from mira.core.noise_filter import _jaccard_similarity
from mira.llm.base import LLMProviderProtocol
from mira.llm.prompts.overlap import build_overlap_prompt
from mira.llm.utils import strip_code_fences, strip_think_blocks
from mira.models import OpenPRRef, OverlapFinding, PRFingerprint, PRInfo

logger = logging.getLogger(__name__)

# Severity ordering for sorting findings (higher = surfaced first).
_KIND_RANK = {"both": 3, "merge_conflict": 2, "duplicate_effort": 1, "none": 0}
_VALID_KINDS = {"merge_conflict", "duplicate_effort", "both", "none"}


def _prefilter(
    current: PRFingerprint,
    candidate: PRFingerprint,
    *,
    title_threshold: float,
) -> tuple[bool, list[str]]:
    """Decide cheaply whether ``candidate`` is worth an LLM judgment.

    Returns ``(keep, shared_files)``. A candidate is kept when it shares files
    or symbols with the current PR (merge-conflict lane), or when its title is
    similar enough to suggest the same goal (duplicate-effort lane). Returning
    the shared files here avoids recomputing them downstream.
    """
    shared_files = sorted(set(current.paths) & set(candidate.paths))
    if shared_files:
        return True, shared_files
    if set(current.symbols) & set(candidate.symbols):
        return True, shared_files
    if _jaccard_similarity(current.title, candidate.title) >= title_threshold:
        return True, shared_files
    return False, shared_files


def _is_stacked(pr_info: PRInfo, ref: OpenPRRef) -> bool:
    """True when the candidate is part of the same branch stack as the PR.

    Stacked PRs (one branch built on top of another) share files by design;
    flagging them as conflicts is noise. Suppress when the candidate's base is
    this PR's head, or its head is this PR's base.
    """
    if ref.head_ref and ref.head_ref == pr_info.base_branch:
        return True
    return bool(ref.base_ref and ref.base_ref == pr_info.head_branch)


def _parse_overlap_response(raw: str) -> dict[int, tuple[str, str, float]]:
    """Parse the LLM verdict JSON into ``pr_number → (kind, reason, confidence)``.

    Tolerant by design: malformed or partial output yields whatever entries
    could be read, never an exception.
    """
    cleaned = strip_code_fences(strip_think_blocks(raw))
    try:
        data = json.loads(cleaned, strict=False)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Overlap verdict was not valid JSON; dropping")
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[int, tuple[str, str, float]] = {}
    for entry in data.get("overlaps", []) or []:
        if not isinstance(entry, dict):
            continue
        try:
            number = int(entry["pr_number"])
        except (KeyError, TypeError, ValueError):
            continue
        kind = str(entry.get("kind", "none")).strip().lower()
        if kind not in _VALID_KINDS:
            kind = "none"
        reason = str(entry.get("reason", "")).strip()
        try:
            confidence = float(entry.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        out[number] = (kind, reason, max(0.0, min(1.0, confidence)))
    return out


async def detect_overlaps(
    *,
    provider: object,
    llm: LLMProviderProtocol,
    config: MiraConfig,
    pr_info: PRInfo,
    current: PRFingerprint,
    cached: dict[int, PRFingerprint],
    candidates: list[OpenPRRef],
    save_fp: Callable[[PRFingerprint], None] | None = None,
) -> list[OverlapFinding]:
    """Return confirmed overlaps between ``pr_info`` and other open PRs.

    ``cached`` maps PR number → fingerprint for PRs Mira has already reviewed.
    ``candidates`` are the open PRs to consider (already excluding the current
    PR, drafts, and bots). Candidates without a fresh cached fingerprint have
    their changed files fetched on demand via ``provider.get_pr_files`` and
    persisted through ``save_fp`` so the next review skips the fetch.
    """
    overlap_cfg = config.review.overlap

    # Stage 1 — resolve each candidate's fingerprint and run the cheap filter.
    survivors: list[tuple[OpenPRRef, PRFingerprint, list[str]]] = []
    for ref in candidates:
        if _is_stacked(pr_info, ref):
            continue
        fp = cached.get(ref.number)
        if fp is None or fp.head_sha != ref.head_sha:
            # Unseen or stale — fetch just the filenames (cheap) to compare.
            paths: list[str] = []
            if hasattr(provider, "get_pr_files"):
                try:
                    paths = await provider.get_pr_files(  # type: ignore[attr-defined]
                        pr_info.owner, pr_info.repo, ref.number
                    )
                except Exception as exc:  # noqa: BLE001 — never block the review
                    logger.debug("get_pr_files failed for #%s: %s", ref.number, exc)
                    continue
            fp = PRFingerprint(
                pr_number=ref.number,
                head_sha=ref.head_sha,
                title=ref.title,
                body=ref.body,
                paths=paths,
                symbols=[],
            )
            if save_fp is not None:
                try:
                    save_fp(fp)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Fingerprint save failed for #%s: %s", ref.number, exc)
        keep, shared = _prefilter(
            current, fp, title_threshold=overlap_cfg.title_similarity_threshold
        )
        if keep:
            survivors.append((ref, fp, shared))

    if not survivors:
        return []

    logger.info(
        "Overlap: %d candidate PR(s) survived pre-filter for PR %s",
        len(survivors),
        pr_info.url,
    )

    # Stage 2 — one batched LLM judgment over the shortlist.
    messages = build_overlap_prompt(pr_info, current, survivors)
    try:
        raw = await llm.complete(messages, json_mode=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Overlap LLM judgment failed, skipping: %s", exc)
        return []

    verdicts = _parse_overlap_response(raw)

    findings: list[OverlapFinding] = []
    for ref, _fp, shared in survivors:
        verdict = verdicts.get(ref.number)
        if verdict is None:
            continue
        kind, reason, confidence = verdict
        if kind == "none" or confidence < overlap_cfg.confidence_floor:
            continue
        findings.append(
            OverlapFinding(
                pr_number=ref.number,
                url=ref.url,
                title=ref.title,
                kind=kind,
                reason=reason,
                confidence=confidence,
                shared_files=shared,
            )
        )

    findings.sort(key=lambda f: (_KIND_RANK.get(f.kind, 0), f.confidence), reverse=True)
    return findings
