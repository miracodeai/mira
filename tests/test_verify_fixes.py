"""Tests for the verify-fixes prompt builder and response parser."""

from __future__ import annotations

import json

from mira.llm.prompts.verify_fixes import (
    _extract_issue_description,
    build_verify_fixes_prompt,
    parse_verify_fixes_response,
)
from mira.models import UnresolvedThread

# Realistic formatted body matching _format_comment_body output
_FORMATTED_BODY = (
    "\U0001f512 **Security issue**\n"
    "\u26a0\ufe0f Warning\n"
    "\n"
    "**Weak cryptographic hash function MD5 used for password hashing**\n"
    "\n"
    "MD5 is cryptographically broken and unsuitable for password hashing. "
    "It's fast to compute, making brute-force attacks feasible, and lacks salt, "
    "allowing rainbow table attacks. Use a modern password hashing algorithm "
    "like bcrypt, scrypt, or argon2 with proper salting.\n"
    "\n"
    "**Suggested fix:**\n"
    "```suggestion\n"
    "import bcrypt\n"
    "def hash_password(password):\n"
    "    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()\n"
    "```\n"
    "\n"
    "<details>\n"
    "<summary>\U0001f916 Prompt for AI Agents</summary>\n"
    "\n"
    "```text\n"
    "In src/auth.py around line 29, replace the MD5-based hash_password function "
    "with bcrypt.\n"
    "```\n"
    "\n"
    "</details>"
)


def _make_thread(
    thread_id: str = "T1",
    path: str = "src/app.py",
    line: int = 29,
    body: str = "Hardcoded API key.",
) -> UnresolvedThread:
    return UnresolvedThread(thread_id=thread_id, path=path, line=line, body=body)


class TestExtractIssueDescription:
    def test_plain_text_body_unchanged(self):
        result = _extract_issue_description("Hardcoded API key.")
        assert result == "Hardcoded API key."

    def test_strips_badges_and_suggestion_from_formatted_body(self):
        result = _extract_issue_description(_FORMATTED_BODY)
        # Should contain the title and explanation
        assert "MD5" in result
        assert "cryptographically broken" in result
        # Should NOT contain badges, suggestion code, or agent prompt
        assert "Security issue" not in result
        assert "Warning" not in result
        assert "```suggestion" not in result
        assert "bcrypt.hashpw" not in result
        assert "Prompt for AI Agents" not in result

    def test_strips_suggestion_block(self):
        body = "**Title**\n\nDescription.\n\n**Suggested fix:**\n```suggestion\ncode\n```"
        result = _extract_issue_description(body)
        assert "Description" in result
        assert "suggestion" not in result.lower()
        assert "code" not in result

    def test_strips_agent_prompt_details(self):
        body = "**Title**\n\nDescription.\n\n<details>\n<summary>Agent</summary>\nstuff\n</details>"
        result = _extract_issue_description(body)
        assert "Description" in result
        assert "Agent" not in result

    def test_truncates_long_descriptions(self):
        body = "A" * 500
        result = _extract_issue_description(body)
        assert len(result) <= 301  # 300 + ellipsis char

    def test_empty_body(self):
        result = _extract_issue_description("")
        assert result == ""


class TestBuildVerifyFixesPrompt:
    def test_single_file_single_thread(self):
        thread = _make_thread()
        file_content = "api_key = os.environ.get('API_KEY')"
        messages = build_verify_fixes_prompt([("src/app.py", file_content, [thread])])

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert "verifying" in messages[0]["content"].lower()
        user = messages[1]["content"]
        assert "File: src/app.py" in user
        assert "T1" in user
        assert "Hardcoded API key." in user
        assert file_content in user

    def test_single_file_multiple_threads(self):
        t1 = _make_thread("T1", body="Issue one")
        t2 = _make_thread("T2", body="Issue two", line=45)
        file_content = "some code\n" * 20
        messages = build_verify_fixes_prompt([("src/app.py", file_content, [t1, t2])])

        user = messages[1]["content"]
        # Both threads listed under the same file
        assert user.count("File: src/app.py") == 1
        assert "T1" in user
        assert "T2" in user
        assert "Issue one" in user
        assert "Issue two" in user

    def test_multiple_files(self):
        t1 = _make_thread("T1", path="src/a.py", body="Issue A")
        t2 = _make_thread("T2", path="src/b.py", body="Issue B")
        messages = build_verify_fixes_prompt(
            [
                ("src/a.py", "code_a", [t1]),
                ("src/b.py", "code_b", [t2]),
            ]
        )

        user = messages[1]["content"]
        assert "File: src/a.py" in user
        assert "File: src/b.py" in user
        assert "code_a" in user
        assert "code_b" in user

    def test_system_prompt_requests_json(self):
        messages = build_verify_fixes_prompt([("src/app.py", "code", [_make_thread()])])
        system = messages[0]["content"]
        assert "JSON" in system
        assert '"fixed"' in system

    def test_system_prompt_not_overly_conservative(self):
        messages = build_verify_fixes_prompt([("src/app.py", "code", [_make_thread()])])
        system = messages[0]["content"]
        assert "if you are unsure" not in system.lower()
        assert "mark as fixed" in system.lower()

    def test_formatted_body_cleaned_in_prompt(self):
        """Formatted review comment body is cleaned before inclusion in prompt."""
        thread = _make_thread(body=_FORMATTED_BODY)
        messages = build_verify_fixes_prompt([("src/app.py", "code", [thread])])
        user = messages[1]["content"]
        # Core issue description should be present
        assert "MD5" in user
        # Suggestion code and agent prompt should NOT leak into the prompt
        assert "bcrypt.hashpw" not in user
        assert "Prompt for AI Agents" not in user


class TestParseVerifyFixesResponse:
    def test_valid_response_all_fixed(self):
        raw = json.dumps(
            {
                "results": [
                    {"id": "T1", "fixed": True},
                    {"id": "T2", "fixed": True},
                ]
            }
        )
        assert parse_verify_fixes_response(raw) == ["T1", "T2"]

    def test_valid_response_mixed(self):
        raw = json.dumps(
            {
                "results": [
                    {"id": "T1", "fixed": True},
                    {"id": "T2", "fixed": False},
                    {"id": "T3", "fixed": True},
                ]
            }
        )
        assert parse_verify_fixes_response(raw) == ["T1", "T3"]

    def test_valid_response_none_fixed(self):
        raw = json.dumps(
            {
                "results": [
                    {"id": "T1", "fixed": False},
                ]
            }
        )
        assert parse_verify_fixes_response(raw) == []

    def test_empty_results(self):
        raw = json.dumps({"results": []})
        assert parse_verify_fixes_response(raw) == []

    def test_invalid_json(self):
        assert parse_verify_fixes_response("NOT JSON {{{") == []

    def test_missing_results_key(self):
        raw = json.dumps({"something": "else"})
        assert parse_verify_fixes_response(raw) == []

    def test_results_not_a_list(self):
        raw = json.dumps({"results": "oops"})
        assert parse_verify_fixes_response(raw) == []

    def test_entry_missing_id(self):
        raw = json.dumps({"results": [{"fixed": True}]})
        assert parse_verify_fixes_response(raw) == []

    def test_entry_missing_fixed(self):
        raw = json.dumps({"results": [{"id": "T1"}]})
        assert parse_verify_fixes_response(raw) == []

    def test_entry_not_dict(self):
        raw = json.dumps({"results": ["T1", "T2"]})
        assert parse_verify_fixes_response(raw) == []

    def test_none_input(self):
        assert parse_verify_fixes_response(None) == []

    def test_json_block_after_analysis_with_other_code_blocks(self):
        """LLM returns markdown analysis with ```python blocks before the ```json result."""
        raw = (
            "I'll analyze each issue.\n\n"
            "## Issue 1\n"
            "Looking at line 171:\n"
            "```python\n"
            'f"**Estimated effort:** {e.level}"\n'
            "```\n"
            "The emoji is still present.\n\n"
            "## Issue 2\n"
            "Looking at line 506:\n"
            "```python\n"
            'emoji, label = _CATEGORY_DISPLAY.get(comment.category, ("pin", "Note"))\n'
            "```\n"
            "No duplication.\n\n"
            "```json\n"
            '{"results": [{"id": "T1", "fixed": true}, {"id": "T2", "fixed": false}]}\n'
            "```\n"
        )
        assert parse_verify_fixes_response(raw) == ["T1"]

    def test_json_block_after_plain_analysis(self):
        """LLM returns plain text analysis followed by a ```json result."""
        raw = (
            "All issues have been addressed in the current code.\n\n"
            "```json\n"
            '{"results": [{"id": "T1", "fixed": true}]}\n'
            "```\n"
        )
        assert parse_verify_fixes_response(raw) == ["T1"]
