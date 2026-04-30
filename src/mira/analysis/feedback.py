"""Synthesise learned rules from accumulated feedback events."""

from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from mira.index.store import IndexStore

logger = logging.getLogger(__name__)

# Minimum reject count before we generate a rule for a (category, dir) pair.
_MIN_REJECTS_PER_DIR = int(os.environ.get("MIRA_FEEDBACK_MIN_DIR", "3"))
# Minimum reject count across all paths for a category-wide rule.
_MIN_REJECTS_CATEGORY = int(os.environ.get("MIRA_FEEDBACK_MIN_CAT", "5"))
# Minimum total events and accept rate threshold for positive rules.
_MIN_CATEGORY_EVENTS_FOR_ACCEPT = int(os.environ.get("MIRA_FEEDBACK_ACCEPT_MIN", "5"))
_ACCEPT_RATE_THRESHOLD = float(os.environ.get("MIRA_FEEDBACK_ACCEPT_RATE", "0.8"))
# Max human-review comments to include in a single LLM synthesis call.
_MAX_HUMAN_COMMENTS = int(os.environ.get("MIRA_HUMAN_SYNTH_MAX", "50"))
# Max rules the LLM is allowed to emit per synthesis run.
_MAX_LLM_RULES = int(os.environ.get("MIRA_HUMAN_SYNTH_MAX_RULES", "5"))

_TEMPLATE_DIR = Path(__file__).parent.parent / "llm" / "prompts" / "templates"


def _dir_of(path: str) -> str:
    """Extract the top-level directory from a file path, or '' for root files."""
    parts = path.split("/")
    return parts[0] if len(parts) > 1 else ""


def synthesize_rules(store: IndexStore) -> int:
    """Analyse feedback events and upsert learned rules.

    Handles both ``rejected`` signals (avoid-rules) and ``accepted`` signals
    (positive rules that reinforce well-received comment categories).

    Returns the number of rules created or updated.
    """
    events = store.list_feedback(limit=2000)
    if not events:
        return 0

    rejects_by_cat_dir: dict[tuple[str, str], int] = defaultdict(int)
    rejects_by_cat: dict[str, int] = defaultdict(int)
    accepts_by_cat: dict[str, int] = defaultdict(int)
    total_by_cat: dict[str, int] = defaultdict(int)

    for ev in events:
        cat = ev.comment_category or "unknown"
        if cat == "unknown":
            continue
        if ev.signal == "rejected":
            directory = _dir_of(ev.comment_path)
            rejects_by_cat_dir[(cat, directory)] += 1
            rejects_by_cat[cat] += 1
            total_by_cat[cat] += 1
        elif ev.signal == "accepted":
            accepts_by_cat[cat] += 1
            total_by_cat[cat] += 1

    upserted = 0

    # Category-wide reject rules (higher threshold)
    for cat, count in rejects_by_cat.items():
        if count < _MIN_REJECTS_CATEGORY:
            continue
        store.upsert_learned_rule(
            rule_text=(
                f"This team frequently rejects '{cat}' suggestions "
                f"({count} rejections). Raise the bar significantly for this "
                f"category — only flag clear, high-confidence issues."
            ),
            source_signal="reject_pattern",
            category=cat,
            path_pattern="",
            sample_count=count,
        )
        upserted += 1

    # Per-directory reject rules (lower threshold)
    for (cat, directory), count in rejects_by_cat_dir.items():
        if not directory or count < _MIN_REJECTS_PER_DIR:
            continue
        # Skip if already covered by a category-wide rule
        if rejects_by_cat.get(cat, 0) >= _MIN_REJECTS_CATEGORY:
            continue
        store.upsert_learned_rule(
            rule_text=(
                f"Avoid '{cat}' comments on files in {directory}/ "
                f"— this team has rejected {count} such suggestions."
            ),
            source_signal="reject_pattern",
            category=cat,
            path_pattern=f"{directory}/**",
            sample_count=count,
        )
        upserted += 1

    # Accepted-pattern rules (positive reinforcement)
    for cat, total in total_by_cat.items():
        if total < _MIN_CATEGORY_EVENTS_FOR_ACCEPT:
            continue
        accepts = accepts_by_cat.get(cat, 0)
        rate = accepts / total if total else 0.0
        if rate < _ACCEPT_RATE_THRESHOLD:
            continue
        # Skip if there's already a reject rule for this category — avoid mixed signals.
        if rejects_by_cat.get(cat, 0) >= _MIN_REJECTS_CATEGORY:
            continue
        store.upsert_learned_rule(
            rule_text=(
                f"This team consistently values '{cat}' feedback "
                f"({accepts}/{total} accepted, {rate:.0%}). Continue flagging "
                f"issues in this category when you spot them."
            ),
            source_signal="accept_pattern",
            category=cat,
            path_pattern="",
            sample_count=total,
        )
        upserted += 1

    return upserted


async def synthesize_from_human_reviews(store: IndexStore, llm) -> int:  # type: ignore[no-untyped-def]
    """Use an LLM to extract reviewer-style rules from human review comments.

    Collects ``human_review`` feedback events (bodies of human PR review
    comments), sends them to the LLM, and upserts the returned rules with
    ``source_signal='human_pattern'``. Intended to run after each PR merge
    against the cheap indexing model.

    Returns the number of rules upserted.
    """
    events = store.list_feedback(limit=500)
    human_events = [e for e in events if e.signal == "human_review"]
    if len(human_events) < 2:
        return 0

    # Most recent comments first; cap to the most recent N for prompt size.
    recent = human_events[:_MAX_HUMAN_COMMENTS]
    comments = [
        {
            "path": e.comment_path,
            "line": e.comment_line,
            "author": e.actor,
            "body": e.comment_title,  # stored body text lives in comment_title column
        }
        for e in recent
    ]

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("synthesize_feedback.jinja2")
    prompt = template.render(comments=comments, max_rules=_MAX_LLM_RULES)

    try:
        raw = await llm.complete(
            messages=[{"role": "user", "content": prompt}],
            json_mode=True,
            temperature=0.0,
        )
    except Exception as exc:
        logger.warning("LLM synthesis failed: %s", exc)
        return 0

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("LLM returned non-JSON response for feedback synthesis")
        return 0

    rules = data.get("rules") or []
    if not isinstance(rules, list):
        return 0

    upserted = 0
    for idx, item in enumerate(rules[:_MAX_LLM_RULES]):
        if not isinstance(item, dict):
            continue
        rule_text = str(item.get("rule") or "").strip()
        if not rule_text:
            continue
        rationale = str(item.get("rationale") or "").strip()
        evidence = int(item.get("evidence_count") or 0)
        if rationale:
            rule_text = f"{rule_text} ({rationale})"
        # Use a synthetic unique path_pattern per rule so upsert keys don't collide.
        store.upsert_learned_rule(
            rule_text=rule_text,
            source_signal="human_pattern",
            category="human_review",
            path_pattern=f"__llm_pattern_{idx}__",
            sample_count=max(evidence, len(comments)),
        )
        upserted += 1

    return upserted
