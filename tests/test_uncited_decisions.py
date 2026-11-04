"""Advisory findings for decisions without a registry ID (HAI-12775).

The detector is deterministic and calls no model. Linked tickets are read from
the registry's Linear mirror only. Nothing here writes a registry object.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from mira.config import MiraConfig
from mira.core.engine import ReviewEngine
from mira.models import PRInfo, Severity
from mira.registry import set_registry
from mira.registry.db import RegistryDB
from mira.registry.linear import issue_item
from mira.registry.model import Actor
from mira.registry.service import Registry
from mira.registry.uncited import (
    COMMENT_MARKER,
    MAX_FINDINGS,
    SOURCE_PASS,
    TITLE,
    advisory_findings,
    find_uncited,
    linked_tickets,
)

HUMAN = Actor("human", "zane")
CITED = "reg_0123456789ab"
OTHER = "reg_abcdef012345"

# A PR body the way agents write them: summary prose, a decisions section,
# a test plan, and a pasted example that must not count.
PR_BODY = f"""## Summary
Adds token caching to the gateway. Refs HAI-901.

We decided to cache tokens for ten minutes. We chose httpx for the client ({CITED}).

## Decisions I made
- Retry upstream calls three times before failing
- Keep the queue in SQLite ([{OTHER}](https://kory.example/registry/{OTHER}))
- Store refresh keys in the vault, see reg_... once recorded

## Test plan
- Ran pytest and ruff

> We decided to drop the cache entirely. (quoted review feedback)

```
# example template
We decided to <describe the decision>.
```
"""


def quotes(text: str) -> list[str]:
    return [d.quote for d in find_uncited(text)]


# ── detector ────────────────────────────────────────────────────────────


def test_pr_body_finds_every_uncited_decision_and_only_those():
    found = find_uncited(PR_BODY)
    assert [d.quote for d in found] == [
        "We decided to cache tokens for ten minutes.",
        "Retry upstream calls three times before failing",
        "Store refresh keys in the vault, see reg_... once recorded",
    ]
    # Lines are 1-based in the source text.
    lines = PR_BODY.split("\n")
    for d in found:
        assert d.quote.split(",")[0].rstrip(".") in lines[d.line - 1]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("We decided to shard by tenant.", ["We decided to shard by tenant."]),
        ("We've opted for a single writer.", ["We've opted for a single writer."]),
        ("I chose JSON lines for the log.", ["I chose JSON lines for the log."]),
        ("The decision was to keep v1 alive.", ["The decision was to keep v1 alive."]),
        ("Decision: tokens live ten minutes", ["Decision: tokens live ten minutes"]),
        ("**Decision:** tokens live ten minutes", ["Decision: tokens live ten minutes"]),
        ("We decided not to cache errors.", ["We decided not to cache errors."]),
        ("Decisions I made: one worker per repo", ["one worker per repo"]),
        ("**Decisions I made:** one worker per repo", ["one worker per repo"]),
        ("### Decisions I made\n1. One worker\n2. Two queues", ["One worker", "Two queues"]),
        (
            "Decisions I made:\n- Retry three times\n  and back off\n",
            ["Retry three times and back off"],
        ),
        (
            "Decisions I made:\nA single paragraph\nwrapped over lines.",
            ["A single paragraph wrapped over lines."],
        ),
    ],
)
def test_positive_forms(text, expected):
    assert quotes(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        f"We decided to shard by tenant ({CITED}).",
        f"We decided to shard by tenant. Recorded as {CITED}.",
        f"Decisions I made:\n- Shard by tenant ([{CITED}](https://x/{CITED}))",
        f"Decisions I made:\n- Shard by tenant\n  `{CITED}`",
        "## Decisions I made\nNone",
        "## Decisions I made\n- None.\n- N/A",
        "Decisions I made: none",
        "Decisions I made: no new decisions",
        "## Decisions I made\n\n## Test plan\n- ran tests",
        "Should we decided to shard? We have not decided.",
        "Decisions I made:\n- Should this be async?\n  Not sure yet",
        "If we decided to shard, reads would fan out.",
        "Decisions I made:\n- If needed, shard later\n- Maybe add a cache",
        "We haven't decided on retries. I never chose a format.",
        "I don't think we decided anything here.",
        "> We decided to drop the cache.",
        "```\nWe decided to drop the cache.\n```",
        "~~~\nDecisions I made:\n- drop the cache\n~~~",
        "Use the phrase `we decided` in the template.",
        'The template says "we decided to X" here.',
        "<!-- Decisions I made: list them here -->",
        "Decisions I made:\n- Added tests for the parser\n- Ran ruff and mypy",
        "Added a retry loop and tests. Fixed a typo.",
        "Decision: none",
        # Explicit no-decision statements (the false-enforcement guard).
        "## Decisions I made\nNo new product behavior is ratified here.",
        "## Decisions I made\n- No product behavior changed",
        "## Decisions I made\n- No product behavior changed.\n- No decisions were needed",
        "Decisions I made: None — no product behavior changed.",
        "## Decisions I made\nThis PR ratifies no new product behavior.",
        "Decisions I made:\n- Nothing user-facing is affected",
    ],
)
def test_negative_controls(text):
    assert quotes(text) == []


def test_a_no_decision_line_does_not_hide_a_real_one():
    text = "## Decisions I made\n- No product behavior changed\n- No retries on 4xx"
    assert quotes(text) == ["No retries on 4xx"]


def test_one_citation_does_not_cover_another_decision():
    text = f"We chose A ({CITED}). We decided B. Recorded as {OTHER}."
    # B is cited by the following sentence; A by its own.
    assert quotes(text) == []
    text = f"We chose A. We decided B ({CITED})."
    assert quotes(text) == ["We chose A."]
    text = f"Decisions I made:\n- A ({CITED})\n- B"
    assert quotes(text) == ["B"]


def test_placeholder_and_short_ids_are_not_citations():
    assert quotes("We decided to cache (reg_...).") == ["We decided to cache (reg_...)."]
    assert quotes("We decided to cache (reg_1).") == ["We decided to cache (reg_1)."]
    assert quotes("We decided to cache (xreg_0123456789ab).") != []


def test_duplicates_collapse_within_a_source():
    text = (
        "We decided to cache.\n\nWe decided to cache.\n\nDecisions I made:\n- We decided to cache."
    )
    assert quotes(text) == ["We decided to cache."]


def test_a_section_ends_at_the_next_label_after_a_blank_line():
    text = "Decisions I made:\n- Retry three times\n\nTesting notes go here.\n"
    assert quotes(text) == ["Retry three times"]


def test_ticket_keys_are_bounded_and_ordered():
    assert linked_tickets("HAI-2: fix", "Refs HAI-1 and HAI-2, OPS-3") == [
        "HAI-2",
        "HAI-1",
        "OPS-3",
    ]
    many = " ".join(f"HAI-{i}" for i in range(20))
    assert len(linked_tickets("", many)) == 5


# ── sources ─────────────────────────────────────────────────────────────


def _pr(body: str = PR_BODY, title: str = "HAI-900: token caching") -> PRInfo:
    return PRInfo(
        title=title,
        description=body,
        base_branch="main",
        head_branch="feat/cache",
        url="https://github.com/hickoryai/widget/pull/7",
        number=7,
        owner="hickoryai",
        repo="widget",
    )


def _issue(key: str, description: str, *, trashed: bool = False) -> dict:
    return issue_item(
        {
            "id": f"lin-{key}",
            "identifier": key,
            "title": f"Ticket {key}",
            "description": description,
            "url": f"https://linear.app/hickory/issue/{key}",
            "updatedAt": "2026-11-01T00:00:00.000Z",
            "state": {"type": "started"},
            "trashed": trashed,
        }
    )


@pytest.fixture
def reg(tmp_path):
    registry = Registry(RegistryDB(path=str(tmp_path / "registry.db")))
    set_registry(registry)
    yield registry
    set_registry(None)


def test_findings_contract_for_pr_body_without_a_registry():
    set_registry(None)
    findings = advisory_findings(_pr(), registry=None)
    assert len(findings) == 3
    for f in findings:
        assert f.severity == Severity.SUGGESTION
        assert (f.category, f.source_pass, f.title) == ("registry", SOURCE_PASS, TITLE)
        assert (f.path, f.line, f.end_line, f.suggestion) == ("", 1, None, None)
        assert f.body.startswith("The pull request body, line ")
        assert "does not block the merge" in f.body
    assert '"We decided to cache tokens for ten minutes."' in findings[0].body


def test_linked_linear_mirror_is_read_and_cited_lines_skipped(reg: Registry):
    reg.import_items(
        "linear",
        [
            _issue("HAI-900", f"Decisions I made:\n- Ten minute TTL\n- One cache ({CITED})"),
            _issue("HAI-901", "We decided to rotate keys weekly."),
            _issue("HAI-999", "We decided something unrelated."),
        ],
        HUMAN,
    )
    before = len(reg._select("1=1"))
    findings = advisory_findings(_pr())
    assert len(reg._select("1=1")) == before
    linear = [f for f in findings if "Linear" in f.body]
    assert [f.body.split("\n")[0] for f in linear] == [
        "The Linear HAI-900 description (https://linear.app/hickory/issue/HAI-900), line 2, "
        "states a decision without a registry ID:",
        "The Linear HAI-901 description (https://linear.app/hickory/issue/HAI-901), line 1, "
        "states a decision without a registry ID:",
    ]
    assert '"Ten minute TTL"' in linear[0].body
    assert "unrelated" not in "".join(f.body for f in findings)


def test_missing_retired_and_foreign_mirrors_yield_nothing(reg: Registry):
    reg.import_items("linear", [_issue("HAI-900", "We decided X.", trashed=True)], HUMAN)
    reg.import_items(
        "jira",
        [{**_issue("HAI-901", "We decided Y."), "source_id": "jira-1"}],
        HUMAN,
    )
    findings = advisory_findings(_pr(body="Refs HAI-901 and HAI-902."))
    assert findings == []


def test_registry_outage_keeps_the_pr_body_findings():
    class Broken:
        def work_by_ticket(self, ticket):  # type: ignore[no-untyped-def]
            raise RuntimeError("database is locked")

    findings = advisory_findings(_pr(), registry=Broken())
    assert len(findings) == 3


def test_findings_are_capped():
    body = "\n\n".join(f"We decided rule {i}." for i in range(MAX_FINDINGS + 5))
    assert len(advisory_findings(_pr(body=body), registry=None)) == MAX_FINDINGS


# ── GitHub-posting reviews ──────────────────────────────────────────────

DIFF = """diff --git a/app/handler.py b/app/handler.py
index 1111111..2222222 100644
--- a/app/handler.py
+++ b/app/handler.py
@@ -1,2 +1,3 @@
 def handle(x):
-    return x
+    y = x + 1
+    return y
"""


class PostingProvider:
    """Records every write the engine's registry hook makes to the PR."""

    def __init__(self, existing: int | None = None, fail: bool = False) -> None:
        self.existing = existing
        self.fail = fail
        self.posted: list[str] = []
        self.updated: list[tuple[int, str]] = []
        self.markers: list[str] = []

    async def find_bot_comment(self, pr_info, marker):  # type: ignore[no-untyped-def]
        self.markers.append(marker)
        if self.fail:
            raise RuntimeError("GitHub is down")
        return self.existing

    async def post_comment(self, pr_info, body):  # type: ignore[no-untyped-def]
        self.posted.append(body)

    async def update_comment(self, pr_info, comment_id, body):  # type: ignore[no-untyped-def]
        self.updated.append((comment_id, body))


def _engine(provider: object | None, *, dry_run: bool = False) -> ReviewEngine:
    config = MiraConfig()
    for flag in (
        "walkthrough",
        "self_critique",
        "security_pass",
        "osv_scan",
        "secrets_scan",
        "agentic_tools",
        "code_context",
        "dependency_overlap",
        "include_summary",
        "blast_radius",
    ):
        setattr(config.review, flag, False)
    llm = MagicMock()
    llm.review = AsyncMock(return_value='{"comments":[],"key_issues":[],"summary":""}')
    llm.count_tokens = MagicMock(return_value=10)
    llm.usage = {}
    return ReviewEngine(config=config, llm=llm, provider=provider, dry_run=dry_run)  # type: ignore[arg-type]


async def _review(engine: ReviewEngine, body: str):  # type: ignore[no-untyped-def]
    pr = _pr(body=body)
    engine._pr_info = pr
    return await engine._review_diff_internal(DIFF, pr_title=pr.title, pr_description=body)


async def test_posting_review_puts_advisories_in_one_general_comment(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    set_registry(None)
    provider = PostingProvider()
    body = PR_BODY + "\nDecisions I made: ping @octocat about the TTL\n"
    result = await _review(_engine(provider), body)
    # Nothing with an empty path reaches the inline review.
    assert all(c.path for c in result.comments)
    assert all(c.source_pass != SOURCE_PASS for c in result.comments)
    [comment] = provider.posted
    assert provider.updated == []
    assert comment.startswith(COMMENT_MARKER)
    assert TITLE in comment and "does not block the merge" in comment
    assert (
        '- The pull request body, line 4: "We decided to cache tokens for ten minutes."' in comment
    )
    assert '"Retry upstream calls three times before failing"' in comment
    assert "Keep the queue in SQLite" not in comment
    assert "@octocat" not in comment and "@\u200boctocat" in comment


async def test_re_review_edits_the_comment_and_clears_it_when_cited(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    set_registry(None)
    provider = PostingProvider(existing=55)
    await _review(_engine(provider), "We decided to cache.")
    assert provider.posted == []
    assert provider.updated[0][0] == 55 and '"We decided to cache."' in provider.updated[0][1]
    await _review(_engine(provider), f"We decided to cache ({CITED}).")
    assert provider.posted == []
    assert "now cites a registry ID" in provider.updated[1][1]


async def test_nothing_is_posted_without_decisions_dry_run_or_a_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    set_registry(None)
    quiet = PostingProvider()
    await _review(_engine(quiet), "## Decisions I made\nNo new product behavior is ratified here.")
    assert (quiet.posted, quiet.updated) == ([], [])
    dry = PostingProvider()
    await _review(_engine(dry, dry_run=True), "We decided to cache.")
    assert (dry.posted, dry.updated, dry.markers) == ([], [], [])
    result = await _review(_engine(None), "We decided to cache.")
    assert all(c.source_pass != SOURCE_PASS for c in result.comments)


async def test_comment_failure_does_not_fail_the_review(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    set_registry(None)
    provider = PostingProvider(fail=True)
    result = await _review(_engine(provider), "We decided to cache.")
    assert provider.markers == [COMMENT_MARKER]
    assert result.reviewed_files == 1


LOCK_ONLY = "diff --git a/poetry.lock b/poetry.lock\n--- a/poetry.lock\n+++ b/poetry.lock\n@@ -1,1 +1,2 @@\n line\n+new\n"


def _pull_provider(body: str, diff: str, existing: int | None) -> AsyncMock:
    provider = AsyncMock()
    provider.get_pr_info.return_value = _pr(body=body)
    provider.get_pr_diff.return_value = diff
    provider.get_unresolved_bot_threads = AsyncMock(return_value=[])

    async def find(pr_info, marker):  # type: ignore[no-untyped-def]
        return existing if marker == COMMENT_MARKER else None

    provider.find_bot_comment = AsyncMock(side_effect=find)
    return provider


def _advisory_writes(provider: AsyncMock) -> tuple[list[str], list[tuple[int, str]]]:
    posted = [
        c.args[1] for c in provider.post_comment.call_args_list if COMMENT_MARKER in c.args[1]
    ]
    updated = [
        (c.args[1], c.args[2])
        for c in provider.update_comment.call_args_list
        if COMMENT_MARKER in c.args[2]
    ]
    return posted, updated


@pytest.mark.parametrize("diff", ["", LOCK_ONLY], ids=["empty-diff", "all-files-filtered"])
async def test_webhook_review_with_no_reviewable_file_posts_the_advisory(
    diff, tmp_path, monkeypatch
):
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    set_registry(None)
    provider = _pull_provider("We decided to cache tokens for ten minutes.", diff, None)
    engine = _engine(provider)
    result = await engine.review_pr("https://github.com/hickoryai/widget/pull/7")
    assert result.reviewed_files == 0
    posted, updated = _advisory_writes(provider)
    assert updated == []
    [comment] = posted
    assert comment.startswith(COMMENT_MARKER)
    assert '"We decided to cache tokens for ten minutes."' in comment
    provider.post_review.assert_not_called()


async def test_webhook_review_with_no_reviewable_file_updates_the_advisory(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    set_registry(None)
    provider = _pull_provider("Decisions I made:\n- One worker per repo", LOCK_ONLY, 91)
    await _engine(provider).review_pr("https://github.com/hickoryai/widget/pull/7")
    posted, updated = _advisory_writes(provider)
    assert posted == []
    [(comment_id, body)] = updated
    assert comment_id == 91 and '"One worker per repo"' in body


async def test_reviewable_pr_posts_the_advisory_once(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    set_registry(None)
    provider = _pull_provider("We decided to cache.", DIFF, None)
    result = await _engine(provider).review_pr("https://github.com/hickoryai/widget/pull/7")
    assert result.reviewed_files == 1
    posted, updated = _advisory_writes(provider)
    assert len(posted) == 1 and updated == []
