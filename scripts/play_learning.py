"""Interactive play harness for Mira's learning loop.

Run a learning scenario end-to-end with real LLM calls — no GitHub required.
Each scenario:

  1. Seeds a fake repo's IndexStore with synthetic feedback events
     (human review comments + accept/reject signals).
  2. Calls the real synthesis pipeline (`synthesize_rules` for accept/reject
     ratios; `synthesize_from_human_reviews` for LLM pattern extraction).
  3. Reviews a target diff with the engine and prints what changed.

You can edit the SCENARIOS dict below to add cases, or pass --scenario-file
pointing at a YAML file with the same shape.

Usage:
    OPENROUTER_API_KEY=... uv run python scripts/play_learning.py
    OPENROUTER_API_KEY=... uv run python scripts/play_learning.py --scenario null-checks
    OPENROUTER_API_KEY=... uv run python scripts/play_learning.py --scenario-file mine.yaml

Requires one of: OPENROUTER_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mira.analysis.feedback import synthesize_from_human_reviews, synthesize_rules  # noqa: E402
from mira.config import load_config  # noqa: E402
from mira.core.engine import ReviewEngine  # noqa: E402
from mira.index.store import IndexStore  # noqa: E402
from mira.llm.provider import LLMProvider  # noqa: E402
from mira.models import PRInfo  # noqa: E402


# ── Scenarios ──────────────────────────────────────────────────────


@dataclass
class Scenario:
    name: str
    description: str
    # Human PR review comments — each becomes a "human_review" feedback event.
    human_comments: list[str]
    # Bot comments and how they were resolved (accepted / rejected).
    bot_history: list[dict]  # {category, severity, title, signal}
    # The diff to review after synthesis runs.
    target_diff: str
    # Free-text hint about what should emerge in the learned rules.
    expected_signal: str


SCENARIOS: dict[str, Scenario] = {
    "test-coverage": Scenario(
        name="test-coverage",
        description="Team consistently asks 'where are the tests?' on PRs.",
        human_comments=[
            "Where are the tests for this? We should cover the happy path at minimum.",
            "Missing test for the empty-input edge case.",
            "Please add a test for when the API returns 500 — this code path looks untested.",
            "I don't see a test for the new branch you added in get_user.",
            "We need a regression test here so the bug doesn't come back.",
            "Tests please — we agreed last sprint that all new public functions get unit tests.",
            "Edge case missing: what happens when the list is empty?",
            "No tests for the error path. Add one.",
        ],
        bot_history=[
            {"category": "style", "severity": "suggestion", "title": "Use double quotes", "signal": "rejected"},
            {"category": "style", "severity": "suggestion", "title": "Trailing whitespace", "signal": "rejected"},
        ],
        target_diff=(
            "diff --git a/src/payments.py b/src/payments.py\n"
            "--- a/src/payments.py\n"
            "+++ b/src/payments.py\n"
            "@@ -1,3 +1,12 @@\n"
            " def charge_card(user_id, amount):\n"
            "     # existing logic\n"
            "     return process(user_id, amount)\n"
            "+\n"
            "+def refund_card(user_id, charge_id, amount):\n"
            "+    # New refund flow — does not currently handle partial refunds.\n"
            "+    if amount <= 0:\n"
            "+        raise ValueError('amount must be positive')\n"
            "+    charge = lookup_charge(charge_id)\n"
            "+    if charge.user_id != user_id:\n"
            "+        raise PermissionError('mismatched user')\n"
            "+    return process_refund(charge_id, amount)\n"
        ),
        expected_signal="missing tests / coverage",
    ),
    "null-checks": Scenario(
        name="null-checks",
        description="Team rejects null-check suggestions as noise.",
        human_comments=[
            "These null checks are noise — Python raises AttributeError clearly enough.",
            "We don't add `if x is None` defensively, just let it fail loudly.",
            "Stop suggesting null checks; we trust internal callers.",
            "Same as before — we don't validate non-public function arguments.",
        ],
        bot_history=[
            {"category": "defensive", "severity": "warning", "title": "Add null check for user", "signal": "rejected"},
            {"category": "defensive", "severity": "warning", "title": "Validate input is not None", "signal": "rejected"},
            {"category": "defensive", "severity": "warning", "title": "Guard against missing key", "signal": "rejected"},
            {"category": "defensive", "severity": "warning", "title": "Add null check for response", "signal": "rejected"},
            {"category": "defensive", "severity": "warning", "title": "Check for None before access", "signal": "rejected"},
            {"category": "bug", "severity": "blocker", "title": "SQL injection in get_user", "signal": "accepted"},
            {"category": "bug", "severity": "blocker", "title": "Unbounded loop in poll_jobs", "signal": "accepted"},
        ],
        target_diff=(
            "diff --git a/src/api.py b/src/api.py\n"
            "--- a/src/api.py\n"
            "+++ b/src/api.py\n"
            "@@ -1,2 +1,6 @@\n"
            " def get_user_email(user):\n"
            "     return user.email\n"
            "+\n"
            "+def format_address(address):\n"
            "+    # called from internal handlers only — caller guarantees non-None\n"
            "+    return f'{address.street}, {address.city}'\n"
        ),
        expected_signal="don't add null checks / defensive checks",
    ),
}


# ── Runner ─────────────────────────────────────────────────────────


def _load_scenario_file(path: str) -> Scenario:
    import yaml

    data = yaml.safe_load(Path(path).read_text())
    return Scenario(
        name=data["name"],
        description=data["description"],
        human_comments=data["human_comments"],
        bot_history=data["bot_history"],
        target_diff=data["target_diff"],
        expected_signal=data["expected_signal"],
    )


def _seed_feedback(store: IndexStore, scenario: Scenario, *, fake_pr: int = 999) -> None:
    """Write all scenario feedback events into the store."""
    for i, body in enumerate(scenario.human_comments):
        store.record_feedback(
            pr_number=fake_pr - 100 - i,  # vary PR number to look realistic
            pr_url=f"https://github.com/play/repo/pull/{fake_pr - 100 - i}",
            comment_path="src/example.py",
            comment_line=10 + i,
            comment_category="",
            comment_severity="",
            comment_title=body,  # body lives in comment_title (matches handler convention)
            signal="human_review",
            actor="senior-eng",
        )
    for i, ev in enumerate(scenario.bot_history):
        store.record_feedback(
            pr_number=fake_pr - 200 - i,
            pr_url=f"https://github.com/play/repo/pull/{fake_pr - 200 - i}",
            comment_path="src/example.py",
            comment_line=20 + i,
            comment_category=ev["category"],
            comment_severity=ev["severity"],
            comment_title=ev["title"],
            signal=ev["signal"],
            actor="miracodeai[bot]",
        )


def _print_header(text: str) -> None:
    print(f"\n{'─' * 70}\n  {text}\n{'─' * 70}")


async def run(scenario: Scenario, *, owner: str = "play", repo: str = "demo") -> int:
    if not any(
        os.environ.get(k)
        for k in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
    ):
        print("ERROR: set OPENROUTER_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY.")
        return 2

    tmp = tempfile.mkdtemp(prefix="mira-play-")
    os.environ["MIRA_INDEX_DIR"] = tmp
    os.environ.pop("DATABASE_URL", None)  # force SQLite

    print(f"Scenario: {scenario.name}")
    print(f"  {scenario.description}")
    print(f"  Tmp index: {tmp}")

    # 1. Seed feedback
    store = IndexStore.open(owner, repo)
    _seed_feedback(store, scenario)
    seeded = len(scenario.human_comments) + len(scenario.bot_history)
    print(f"  Seeded {seeded} feedback events ({len(scenario.human_comments)} human, "
          f"{len(scenario.bot_history)} bot accept/reject)")

    # 2. Synthesise — accept/reject ratios first
    _print_header("Step 1 — synthesize_rules (accept/reject)")
    n_pattern = synthesize_rules(store)
    print(f"Pattern rules upserted: {n_pattern}")

    # 3. Synthesise — LLM extracts patterns from human comments
    _print_header("Step 2 — synthesize_from_human_reviews (LLM)")
    config = load_config()
    config.review.walkthrough = False
    config.review.code_context = False
    llm = LLMProvider(config.llm)
    n_llm = await synthesize_from_human_reviews(store, llm)
    print(f"LLM rules upserted: {n_llm}")

    # 4. Inspect rules
    _print_header("Step 3 — learned rules now in store")
    rules = store.list_active_learned_rules()
    if not rules:
        print("(none)")
    for r in rules:
        print(f"  • [{r.source_signal}/{r.category}] {r.rule_text}")
    print(f"\nExpected signal in rules: {scenario.expected_signal!r}")

    # 5. Review the target diff and confirm rules made it into the prompt
    _print_header("Step 4 — review target diff")
    engine = ReviewEngine(config=config, llm=llm, dry_run=True)
    engine._pr_info = PRInfo(  # type: ignore[attr-defined]
        title="Add refund flow",
        description="Adds a new `refund_card` function.",
        base_branch="main",
        head_branch="feature/refund",
        url=f"https://github.com/{owner}/{repo}/pull/1",
        number=1,
        owner=owner,
        repo=repo,
    )
    result = await engine.review_diff(scenario.target_diff)

    print(f"Comments produced: {len(result.comments)}")
    for c in result.comments:
        print(f"  • [{c.severity.name}/{c.category}] {c.title}")
        if c.body:
            print(f"      {c.body.splitlines()[0][:100]}")

    # 6. Verify rules were available — quick text search to confirm injection
    rule_texts = [r.rule_text for r in rules]
    if rule_texts:
        print("\nRules surfaced for this review:")
        for t in rule_texts[:10]:
            print(f"  → {t}")

    store.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", default="test-coverage", choices=sorted(SCENARIOS.keys()),
                        help="Built-in scenario to run.")
    parser.add_argument("--scenario-file", help="Path to a custom YAML scenario.")
    args = parser.parse_args()

    scenario = _load_scenario_file(args.scenario_file) if args.scenario_file else SCENARIOS[args.scenario]
    rc = asyncio.run(run(scenario))
    sys.exit(rc)


if __name__ == "__main__":
    main()
