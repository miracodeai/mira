"""Tests for the verify-fixes prompt builder and response parser."""

from __future__ import annotations

import json

from mira.llm.prompts.verify_fixes import build_verify_fixes_prompt, parse_verify_fixes_response
from mira.models import UnresolvedThread


def _make_thread(
    thread_id: str = "T1",
    path: str = "src/app.py",
    line: int = 29,
    body: str = "Hardcoded API key.",
) -> UnresolvedThread:
    return UnresolvedThread(thread_id=thread_id, path=path, line=line, body=body)


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
        assert "no longer present" in system.lower()


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
