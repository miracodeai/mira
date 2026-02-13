"""Tests for LLM response parsing."""

from __future__ import annotations

import json

import pytest

from mira.exceptions import ResponseParseError
from mira.llm.response_parser import (
    convert_to_review_comments,
    parse_llm_response,
)
from mira.models import FileChangeType, FileDiff, HunkInfo, Severity


class TestParseLLMResponse:
    def test_parse_valid_response(self, sample_llm_response_text: str):
        result = parse_llm_response(sample_llm_response_text)
        assert len(result.comments) == 3
        assert result.summary != ""
        assert result.metadata.reviewed_files == 2

    def test_parse_with_code_fences(self, sample_llm_response_text: str):
        wrapped = f"```json\n{sample_llm_response_text}\n```"
        result = parse_llm_response(wrapped)
        assert len(result.comments) == 3

    def test_parse_empty_comments(self):
        data = json.dumps(
            {
                "comments": [],
                "summary": "All good!",
                "metadata": {"reviewed_files": 1},
            }
        )
        result = parse_llm_response(data)
        assert len(result.comments) == 0
        assert result.summary == "All good!"

    def test_invalid_json(self):
        with pytest.raises(ResponseParseError, match="not valid JSON"):
            parse_llm_response("not json at all")

    def test_non_object_json(self):
        with pytest.raises(ResponseParseError, match="Expected JSON object"):
            parse_llm_response("[1, 2, 3]")

    def test_missing_fields_use_defaults(self):
        result = parse_llm_response("{}")
        assert result.comments == []
        assert result.summary == ""


class TestConvertToReviewComments:
    def test_converts_comments(self, sample_llm_response_text: str):
        response = parse_llm_response(sample_llm_response_text)
        comments = convert_to_review_comments(response)
        assert len(comments) == 3
        assert comments[0].severity == Severity.WARNING
        assert comments[1].severity == Severity.BLOCKER

    def test_filters_hallucinated_paths(self, sample_llm_response_text: str):
        response = parse_llm_response(sample_llm_response_text)
        comments = convert_to_review_comments(response, valid_paths={"src/main.py"})
        assert len(comments) == 0  # All comments are for src/utils.py

    def test_filters_invalid_lines(self):
        data = json.dumps(
            {
                "comments": [{"path": "a.py", "line": 0, "title": "bad", "body": "bad"}],
                "summary": "",
                "metadata": {"reviewed_files": 1},
            }
        )
        response = parse_llm_response(data)
        comments = convert_to_review_comments(response)
        assert len(comments) == 0

    def test_truncates_long_titles(self):
        data = json.dumps(
            {
                "comments": [
                    {
                        "path": "a.py",
                        "line": 1,
                        "title": "A" * 100,
                        "body": "details",
                        "severity": "warning",
                        "confidence": 0.9,
                    }
                ],
                "summary": "",
                "metadata": {"reviewed_files": 1},
            }
        )
        response = parse_llm_response(data)
        comments = convert_to_review_comments(response)
        assert len(comments[0].title) == 80


def _make_diff_files(path: str, hunk_content: str) -> list[FileDiff]:
    return [
        FileDiff(
            path=path,
            change_type=FileChangeType.MODIFIED,
            hunks=[HunkInfo(1, 5, 1, 5, hunk_content)],
            language="python",
            added_lines=1,
            deleted_lines=1,
        )
    ]


class TestExistingCodeValidation:
    def test_drops_comment_with_hallucinated_existing_code(self):
        data = json.dumps(
            {
                "comments": [
                    {
                        "path": "a.py",
                        "line": 2,
                        "title": "Problem",
                        "body": "This is wrong",
                        "severity": "warning",
                        "confidence": 0.9,
                        "existing_code": "code_not_in_diff()",
                    }
                ],
                "summary": "",
                "metadata": {"reviewed_files": 1},
            }
        )
        diff_files = _make_diff_files("a.py", "@@ -1,5 +1,5 @@\n-old\n+new_line()")
        response = parse_llm_response(data)
        comments = convert_to_review_comments(
            response,
            valid_paths={"a.py"},
            diff_files=diff_files,
        )
        assert len(comments) == 0

    def test_keeps_comment_with_valid_existing_code(self):
        data = json.dumps(
            {
                "comments": [
                    {
                        "path": "a.py",
                        "line": 2,
                        "title": "Problem",
                        "body": "This is wrong",
                        "severity": "warning",
                        "confidence": 0.9,
                        "existing_code": "new_line()",
                    }
                ],
                "summary": "",
                "metadata": {"reviewed_files": 1},
            }
        )
        diff_files = _make_diff_files("a.py", "@@ -1,5 +1,5 @@\n-old\n+new_line()")
        response = parse_llm_response(data)
        comments = convert_to_review_comments(
            response,
            valid_paths={"a.py"},
            diff_files=diff_files,
        )
        assert len(comments) == 1

    def test_keeps_comment_without_existing_code(self):
        """Comments without existing_code should pass through unchanged."""
        data = json.dumps(
            {
                "comments": [
                    {
                        "path": "a.py",
                        "line": 2,
                        "title": "Problem",
                        "body": "This is wrong",
                        "severity": "warning",
                        "confidence": 0.9,
                    }
                ],
                "summary": "",
                "metadata": {"reviewed_files": 1},
            }
        )
        diff_files = _make_diff_files("a.py", "@@ -1,5 +1,5 @@\n-old\n+new")
        response = parse_llm_response(data)
        comments = convert_to_review_comments(
            response,
            valid_paths={"a.py"},
            diff_files=diff_files,
        )
        assert len(comments) == 1


class TestNoOpSuggestionCheck:
    def test_clears_noop_suggestion(self):
        data = json.dumps(
            {
                "comments": [
                    {
                        "path": "a.py",
                        "line": 2,
                        "title": "Problem",
                        "body": "Explaining the issue",
                        "severity": "warning",
                        "confidence": 0.9,
                        "existing_code": "x = 1",
                        "suggestion": "x = 1",
                    }
                ],
                "summary": "",
                "metadata": {"reviewed_files": 1},
            }
        )
        diff_files = _make_diff_files("a.py", "@@ -1,5 +1,5 @@\n x = 1\n+y = 2")
        response = parse_llm_response(data)
        comments = convert_to_review_comments(
            response,
            valid_paths={"a.py"},
            diff_files=diff_files,
        )
        assert len(comments) == 1
        assert comments[0].suggestion is None

    def test_keeps_real_suggestion(self):
        data = json.dumps(
            {
                "comments": [
                    {
                        "path": "a.py",
                        "line": 2,
                        "title": "Problem",
                        "body": "Use a better name",
                        "severity": "warning",
                        "confidence": 0.9,
                        "existing_code": "x = 1",
                        "suggestion": "count = 1",
                    }
                ],
                "summary": "",
                "metadata": {"reviewed_files": 1},
            }
        )
        diff_files = _make_diff_files("a.py", "@@ -1,5 +1,5 @@\n x = 1\n+y = 2")
        response = parse_llm_response(data)
        comments = convert_to_review_comments(
            response,
            valid_paths={"a.py"},
            diff_files=diff_files,
        )
        assert len(comments) == 1
        assert comments[0].suggestion == "count = 1"


class TestAgentPrompt:
    def test_agent_prompt_parsed(self):
        data = json.dumps(
            {
                "comments": [
                    {
                        "path": "a.py",
                        "line": 2,
                        "title": "Problem",
                        "body": "Explaining the issue",
                        "severity": "warning",
                        "confidence": 0.9,
                        "agent_prompt": "In a.py at line 2, replace the call to foo() with bar().",
                    }
                ],
                "summary": "",
                "metadata": {"reviewed_files": 1},
            }
        )
        response = parse_llm_response(data)
        comments = convert_to_review_comments(response, valid_paths={"a.py"})
        assert len(comments) == 1
        expected = "In a.py at line 2, replace the call to foo() with bar()."
        assert comments[0].agent_prompt == expected

    def test_agent_prompt_missing_defaults_to_none(self):
        data = json.dumps(
            {
                "comments": [
                    {
                        "path": "a.py",
                        "line": 2,
                        "title": "Problem",
                        "body": "Explaining the issue",
                        "severity": "warning",
                        "confidence": 0.9,
                    }
                ],
                "summary": "",
                "metadata": {"reviewed_files": 1},
            }
        )
        response = parse_llm_response(data)
        comments = convert_to_review_comments(response, valid_paths={"a.py"})
        assert len(comments) == 1
        assert comments[0].agent_prompt is None


class TestSuggestionWithoutBody:
    def test_skips_suggestion_without_body(self):
        data = json.dumps(
            {
                "comments": [
                    {
                        "path": "a.py",
                        "line": 2,
                        "title": "Problem",
                        "body": "",
                        "severity": "warning",
                        "confidence": 0.9,
                        "suggestion": "fixed()",
                    }
                ],
                "summary": "",
                "metadata": {"reviewed_files": 1},
            }
        )
        response = parse_llm_response(data)
        comments = convert_to_review_comments(response, valid_paths={"a.py"})
        assert len(comments) == 0

    def test_keeps_suggestion_with_body(self):
        data = json.dumps(
            {
                "comments": [
                    {
                        "path": "a.py",
                        "line": 2,
                        "title": "Problem",
                        "body": "Here is the explanation",
                        "severity": "warning",
                        "confidence": 0.9,
                        "suggestion": "fixed()",
                    }
                ],
                "summary": "",
                "metadata": {"reviewed_files": 1},
            }
        )
        response = parse_llm_response(data)
        comments = convert_to_review_comments(response, valid_paths={"a.py"})
        assert len(comments) == 1
