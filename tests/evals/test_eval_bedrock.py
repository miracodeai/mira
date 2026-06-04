"""Bedrock integration eval — runs against real AWS Bedrock.

Requires AWS credentials with bedrock:InvokeModel permission.
Skipped when credentials are not available.

Run with: pytest tests/evals/test_eval_bedrock.py -m eval -v
"""

from __future__ import annotations

import json
import os

import pytest

from mira.config import LLMConfig
from mira.core.engine import ReviewEngine
from mira.llm import create_llm

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        not os.environ.get("AWS_ACCESS_KEY_ID")
        and not os.environ.get("AWS_PROFILE")
        and not os.environ.get("AWS_ROLE_ARN"),
        reason="No AWS credentials available (need AWS_ACCESS_KEY_ID, AWS_PROFILE, or AWS_ROLE_ARN)",
    ),
]

_BEDROCK_MODEL = os.environ.get("MIRA_BEDROCK_MODEL", "us.anthropic.claude-haiku-4-5-v1:0")
_BEDROCK_REGION = os.environ.get("AWS_REGION", "us-east-1")


def _make_provider():
    config = LLMConfig(
        provider="bedrock",
        model=_BEDROCK_MODEL,
        region=_BEDROCK_REGION,
        aws_profile=os.environ.get("AWS_PROFILE"),
    )
    return create_llm(config)


def _make_diff(filename: str, content: str) -> str:
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


class TestBedrockReviewIntegration:
    """End-to-end: send a diff through the review engine with Bedrock."""

    @pytest.mark.asyncio
    async def test_reviews_sql_injection(self) -> None:
        """Bedrock should catch an obvious SQL injection."""
        from mira.config import load_config

        code = """\
def get_user(user_id):
    query = f"SELECT * FROM users WHERE id = {user_id}"
    return db.execute(query)
"""
        diff = _make_diff("app/db.py", code)

        config = load_config()
        config.review.walkthrough = False
        config.review.self_critique = False
        config.review.security_pass = False

        llm = _make_provider()
        engine = ReviewEngine(config=config, llm=llm, dry_run=True)
        result = await engine.review_diff(diff)

        # Should produce at least one comment mentioning SQL or injection
        assert result.comments, "Expected at least one review comment"
        all_text = " ".join(c.title + " " + c.body for c in result.comments).lower()
        assert "sql" in all_text or "injection" in all_text

    @pytest.mark.asyncio
    async def test_basic_completion(self) -> None:
        """Verify basic Bedrock completion works."""
        provider = _make_provider()
        result = await provider.complete(
            [
                {"role": "system", "content": "Return valid JSON only."},
                {"role": "user", "content": 'Return {"status": "ok"}'},
            ],
            json_mode=True,
        )

        parsed = json.loads(result)
        assert "status" in parsed

    @pytest.mark.asyncio
    async def test_tool_calling(self) -> None:
        """Verify Bedrock tool calling works with a simple tool."""
        provider = _make_provider()
        result = await provider.complete_with_tools(
            [{"role": "user", "content": "Say hello in a greeting tool call"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "greet",
                        "description": "Generate a greeting",
                        "parameters": {
                            "type": "object",
                            "properties": {"message": {"type": "string"}},
                            "required": ["message"],
                        },
                    },
                }
            ],
        )

        parsed = json.loads(result)
        assert "message" in parsed
