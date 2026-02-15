"""Tests for the verify-fixes prompt builder and response parser."""

from __future__ import annotations

import json

from mira.llm.prompts.verify_fixes import build_verify_fixes_prompt, parse_verify_fixes_response
from mira.models import OutdatedThread


def _make_thread(
    thread_id: str = "T1",
    path: str = "src/app.py",
    line: int = 29,
    body: str = "Hardcoded API key.",
) -> OutdatedThread:
    return OutdatedThread(thread_id=thread_id, path=path, line=line, body=body)


class TestBuildVerifyFixesPrompt:
    def test_single_thread(self):
        thread = _make_thread()
        snippet = "api_key = os.environ.get('API_KEY')"
        messages = build_verify_fixes_prompt([(thread, snippet)])

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert "verifying" in messages[0]["content"].lower()
        assert messages[1]["role"] == "user"
        assert "T1" in messages[1]["content"]
        assert "src/app.py" in messages[1]["content"]
        assert "Hardcoded API key." in messages[1]["content"]
        assert snippet in messages[1]["content"]

    def test_multiple_threads(self):
        threads = [
            (_make_thread("T1", body="Issue one"), "code1"),
            (_make_thread("T2", body="Issue two"), "code2"),
            (_make_thread("T3", body="Issue three"), "code3"),
        ]
        messages = build_verify_fixes_prompt(threads)

        content = messages[1]["content"]
        assert "Issue 1" in content
        assert "Issue 2" in content
        assert "Issue 3" in content
        assert "T1" in content
        assert "T2" in content
        assert "T3" in content

    def test_system_prompt_requests_json(self):
        messages = build_verify_fixes_prompt([(_make_thread(), "code")])
        system = messages[0]["content"]
        assert "JSON" in system
        assert '"fixed"' in system


class TestParseVerifyFixesResponse:
    def test_valid_response_all_fixed(self):
        raw = json.dumps({"results": [
            {"id": "T1", "fixed": True},
            {"id": "T2", "fixed": True},
        ]})
        assert parse_verify_fixes_response(raw) == ["T1", "T2"]

    def test_valid_response_mixed(self):
        raw = json.dumps({"results": [
            {"id": "T1", "fixed": True},
            {"id": "T2", "fixed": False},
            {"id": "T3", "fixed": True},
        ]})
        assert parse_verify_fixes_response(raw) == ["T1", "T3"]

    def test_valid_response_none_fixed(self):
        raw = json.dumps({"results": [
            {"id": "T1", "fixed": False},
        ]})
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
