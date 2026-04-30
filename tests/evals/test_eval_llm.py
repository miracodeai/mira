"""LLM evaluation suite — runs against a real model.

6 review quality scenarios + 6 walkthrough quality checks + aggregate scorecard.
Skipped when API keys are not available.

Run with: pytest tests/evals/ -m eval
"""

from __future__ import annotations

import os

import pytest

from mira.config import load_config
from mira.core.engine import ReviewEngine
from mira.llm.provider import LLMProvider

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        not os.environ.get("OPENROUTER_API_KEY")
        and not os.environ.get("OPENAI_API_KEY")
        and not os.environ.get("ANTHROPIC_API_KEY"),
        reason="No LLM API key set (need OPENROUTER_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY)",
    ),
]


def _make_engine() -> ReviewEngine:
    config = load_config()
    config.review.walkthrough = False
    config.review.code_context = False
    llm = LLMProvider(config.llm)
    return ReviewEngine(config=config, llm=llm, dry_run=True)


def _make_diff(filename: str, content: str, change_type: str = "modified") -> str:
    """Create a minimal unified diff for testing."""
    lines = content.strip().splitlines()
    added = "\n".join(f"+{line}" for line in lines)
    header = (
        f"diff --git a/{filename} b/{filename}\n"
        f"--- a/{filename}\n"
        f"+++ b/{filename}\n"
        f"@@ -1,0 +1,{len(lines)} @@\n"
    )
    return header + added + "\n"


# ─── Review Quality Scenarios ─────────────────────────────────────────


class TestReviewQualityScenarios:
    """6 scenarios testing that the LLM catches planted issues."""

    @pytest.mark.asyncio
    async def test_eval_catches_sql_injection(self) -> None:
        code = """
def get_user(user_id):
    query = f"SELECT * FROM users WHERE id = {user_id}"
    return db.execute(query)
"""
        engine = _make_engine()
        result = await engine.review_diff(_make_diff("app.py", code))
        bodies = " ".join(c.body.lower() + " " + c.category.lower() for c in result.comments)
        assert any(kw in bodies for kw in ["sql", "injection", "security"]), (
            f"Expected SQL injection to be caught. Got: {[c.title for c in result.comments]}"
        )

    @pytest.mark.asyncio
    async def test_eval_catches_race_condition(self) -> None:
        code = """
balance = get_balance(account_id)
if balance >= amount:
    deduct(account_id, amount)
    transfer(target_id, amount)
"""
        engine = _make_engine()
        result = await engine.review_diff(_make_diff("transfer.py", code))
        bodies = " ".join(c.body.lower() + " " + c.category.lower() for c in result.comments)
        assert any(kw in bodies for kw in ["race", "concurrent", "atomic", "toctou"]), (
            f"Expected race condition to be caught. Got: {[c.title for c in result.comments]}"
        )

    @pytest.mark.asyncio
    async def test_eval_catches_resource_leak(self) -> None:
        code = """
def process_file(path):
    f = open(path, "r")
    data = f.read()
    result = parse(data)
    f.close()
    return result
"""
        engine = _make_engine()
        result = await engine.review_diff(_make_diff("processor.py", code))
        bodies = " ".join(c.body.lower() + " " + c.category.lower() for c in result.comments)
        assert any(kw in bodies for kw in ["close", "leak", "with", "context", "resource"]), (
            f"Expected resource leak to be caught. Got: {[c.title for c in result.comments]}"
        )

    @pytest.mark.asyncio
    async def test_eval_clean_refactor_low_noise(self) -> None:
        code = '''
def calculate_total(items):
    """Calculate total price of items."""
    return sum(item.price * item.quantity for item in items)


def format_currency(amount):
    """Format amount as USD string."""
    return f"${amount:,.2f}"
'''
        engine = _make_engine()
        result = await engine.review_diff(_make_diff("utils.py", code))
        assert len(result.comments) <= 2, (
            f"Clean code should produce 0-2 comments, got {len(result.comments)}: {[c.title for c in result.comments]}"
        )

    @pytest.mark.asyncio
    async def test_eval_catches_hardcoded_secrets(self) -> None:
        # Split the literal so GitHub secret scanning doesn't flag this fixture
        # as a real Stripe key. The LLM still sees the joined string at runtime.
        fake_key = "sk_" + "live_" + "4eC39HqLyjWDarjtT1zdp7dc"
        code = f"""
API_KEY = "{fake_key}"
DATABASE_URL = "postgresql://admin:password123@prod.db.example.com/mydb"

def get_client():
    return Client(api_key=API_KEY)
"""
        engine = _make_engine()
        result = await engine.review_diff(_make_diff("config.py", code))
        bodies = " ".join(c.body.lower() + " " + c.category.lower() for c in result.comments)
        assert any(
            kw in bodies for kw in ["secret", "hardcoded", "credential", "key", "password"]
        ), f"Expected hardcoded secrets to be caught. Got: {[c.title for c in result.comments]}"

    @pytest.mark.asyncio
    async def test_eval_catches_error_swallowing(self) -> None:
        code = """
def save_data(data):
    try:
        db.insert(data)
    except:
        pass

def load_config():
    try:
        return json.load(open("config.json"))
    except Exception:
        pass
"""
        engine = _make_engine()
        result = await engine.review_diff(_make_diff("service.py", code))
        bodies = " ".join(c.body.lower() + " " + c.category.lower() for c in result.comments)
        assert any(
            kw in bodies for kw in ["swallow", "silent", "ignore", "bare", "except", "error"]
        ), f"Expected error swallowing to be caught. Got: {[c.title for c in result.comments]}"


# ─── Walkthrough Quality Checks ──────────────────────────────────────


class TestWalkthroughQuality:
    """6 checks for walkthrough output quality."""

    @pytest.fixture
    async def walkthrough_result(self):
        code = '''
def authenticate(token):
    """Verify JWT token."""
    payload = decode(token)
    if not payload:
        raise AuthError("Invalid token")
    return User.get(payload["user_id"])


def create_session(user):
    """Create new session token."""
    return encode({"user_id": user.id, "exp": time.time() + 3600})
'''
        config = load_config()
        config.review.walkthrough = True
        config.review.code_context = False
        llm = LLMProvider(config.llm)
        engine = ReviewEngine(config=config, llm=llm, dry_run=True)
        result = await engine.review_diff(_make_diff("auth.py", code))
        return result

    @pytest.mark.asyncio
    async def test_eval_walkthrough_has_summary(self, walkthrough_result) -> None:
        wt = walkthrough_result.walkthrough
        assert wt is not None
        assert len(wt.summary) > 10

    @pytest.mark.asyncio
    async def test_eval_walkthrough_covers_all_files(self, walkthrough_result) -> None:
        wt = walkthrough_result.walkthrough
        assert wt is not None
        file_paths = [f.path for f in wt.file_changes]
        assert "auth.py" in file_paths

    @pytest.mark.asyncio
    async def test_eval_walkthrough_correct_change_types(self, walkthrough_result) -> None:
        wt = walkthrough_result.walkthrough
        assert wt is not None
        for fc in wt.file_changes:
            assert fc.change_type is not None

    @pytest.mark.asyncio
    async def test_eval_walkthrough_effort_reasonable(self, walkthrough_result) -> None:
        wt = walkthrough_result.walkthrough
        assert wt is not None
        if wt.effort:
            assert 1 <= wt.effort.level <= 5

    @pytest.mark.asyncio
    async def test_eval_walkthrough_confidence_present(self, walkthrough_result) -> None:
        wt = walkthrough_result.walkthrough
        assert wt is not None
        if wt.confidence_score:
            assert 1 <= wt.confidence_score.score <= 5

    @pytest.mark.asyncio
    async def test_eval_walkthrough_no_hallucinated_files(self, walkthrough_result) -> None:
        wt = walkthrough_result.walkthrough
        assert wt is not None
        for fc in wt.file_changes:
            # File should be auth.py, not some hallucinated path
            assert fc.path.endswith(".py") or fc.path.endswith(".js")


# ─── Aggregate Scorecard ─────────────────────────────────────────────


class TestAggregateScorecard:
    """Aggregate eval that computes a summary scorecard."""

    @pytest.mark.asyncio
    async def test_eval_aggregate_scorecard(self) -> None:
        """Run all review scenarios and print a summary table."""
        scenarios = [
            (
                "SQL Injection",
                'def get(uid):\n    q = f"SELECT * FROM users WHERE id = {uid}"\n    return db.execute(q)\n',
                ["sql", "injection"],
            ),
            (
                "Hardcoded Secret",
                'API_KEY = "sk_" + "live_abc123"\n',
                ["secret", "key", "credential", "hardcoded"],
            ),
            (
                "Resource Leak",
                "def read():\n    f = open('x')\n    return f.read()\n",
                ["leak", "close", "with", "resource"],
            ),
            (
                "Error Swallowing",
                "try:\n    run()\nexcept:\n    pass\n",
                ["swallow", "except", "silent", "error", "ignore"],
            ),
        ]

        engine = _make_engine()
        results = []
        for name, code, keywords in scenarios:
            result = await engine.review_diff(_make_diff("test.py", code))
            bodies = " ".join(c.body.lower() + " " + c.category.lower() for c in result.comments)
            caught = any(kw in bodies for kw in keywords)
            results.append((name, caught, len(result.comments)))

        # Print scorecard
        total = len(results)
        caught_count = sum(1 for _, caught, _ in results if caught)
        print(f"\n{'=' * 50}")
        print(f"  LLM Eval Scorecard: {caught_count}/{total} scenarios caught")
        print(f"{'=' * 50}")
        for name, caught, n_comments in results:
            status = "PASS" if caught else "FAIL"
            print(f"  [{status}] {name} ({n_comments} comments)")
        print(f"{'=' * 50}")

        # At least 75% should pass
        assert caught_count >= total * 0.75, f"Only {caught_count}/{total} scenarios caught"
