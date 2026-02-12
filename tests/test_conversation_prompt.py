"""Tests for the conversation prompt builder."""

from __future__ import annotations

from mira.llm.prompts.conversation import build_conversation_prompt


def test_returns_two_messages() -> None:
    """Should return system + user messages."""
    messages = build_conversation_prompt(
        question="Why is this slow?",
        diff_text="+ some code",
    )
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"


def test_includes_diff_and_question() -> None:
    """User message should contain both the diff and the question."""
    messages = build_conversation_prompt(
        question="Is this thread-safe?",
        diff_text="+threading.Lock()",
    )
    user_msg = messages[1]["content"]
    assert "+threading.Lock()" in user_msg
    assert "Is this thread-safe?" in user_msg


def test_includes_pr_metadata() -> None:
    """System message should include PR title and description when provided."""
    messages = build_conversation_prompt(
        question="Explain this",
        diff_text="diff",
        pr_title="Add caching layer",
        pr_description="Implements Redis-backed cache",
    )
    system_msg = messages[0]["content"]
    assert "Add caching layer" in system_msg
    assert "Redis-backed cache" in system_msg


def test_works_without_metadata() -> None:
    """Should work fine without PR title/description."""
    messages = build_conversation_prompt(
        question="What does this do?",
        diff_text="+ return 42",
    )
    assert len(messages) == 2
    # System message should not contain title/description sections
    system_msg = messages[0]["content"]
    assert "**Title**" not in system_msg
