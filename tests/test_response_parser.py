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

    def test_recovers_leaked_tool_call_xml(self):
        # Some models (Haiku via OpenRouter) leak Anthropic tool-call XML
        # delimiters into the JSON args, breaking a strict parse. The repair
        # should cut the junk and still recover the findings.
        raw = (
            '{"comments": [{"path": "p.py", "line": 5, "severity": "blocker", '
            '"category": "security", "title": "SQL injection", "body": "x", '
            '"confidence": 1.0}], "metadata": {"reviewed_files": 1}\n'
            '</parameter>\n</invoke>": ""}'
        )
        result = parse_llm_response(raw)
        assert len(result.comments) == 1
        assert result.comments[0].title == "SQL injection"

    def test_recovers_truncated_missing_brace(self):
        raw = '{"comments": [{"path": "a.py", "line": 1, "title": "bug", "body": "y"}'
        result = parse_llm_response(raw)
        assert len(result.comments) == 1

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

    def test_parses_double_encoded_comments_array(self):
        """Haiku occasionally returns ``comments`` as a stringified JSON
        array — sometimes pretty-printed with raw newlines that strict
        JSON rejects. The parser must recover both the outer and inner."""
        inner = (
            "[\n  {\n"
            '    "path": "a.py",\n'
            '    "line": 10,\n'
            '    "body": "raw newlines inside the string",\n'
            '    "existing_code": "x"\n'
            "  }\n]"
        )
        # Outer JSON has the inner as a quoted string with raw newlines —
        # invalid in strict mode, valid with strict=False.
        raw = '{"comments": "' + inner.replace('"', '\\"') + '", "summary": ""}'
        result = parse_llm_response(raw)
        assert len(result.comments) == 1
        assert result.comments[0].body == "raw newlines inside the string"


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
        """An empty/missing citation is permitted — only present-but-wrong
        citations are dropped as hallucinations."""
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


class TestSanitizeMermaid:
    """The walkthrough's sequence_diagram is post-processed to repair
    nested-quote labels — `engine["core/"engine.py""]` would otherwise
    break Mermaid's parser on GitHub. See PR#41 for the failure mode."""

    def _sanitize(self, diagram: str) -> str:
        from mira.llm.response_parser import _sanitize_mermaid

        return _sanitize_mermaid(diagram)

    def test_repairs_nested_quotes_single_node(self):
        diagram = 'graph LR\n    engine["core/"engine.py""]\n'
        out = self._sanitize(diagram)
        assert '"core/engine.py"' in out
        assert 'core/"engine.py"' not in out

    def test_repairs_nested_quotes_multiple_nodes(self):
        diagram = 'graph LR\n    a["x/"a.py""]\n    b["y/"b.py""]\n    a --> b\n'
        out = self._sanitize(diagram)
        assert '["x/a.py"]' in out
        assert '["y/b.py"]' in out

    def test_passthrough_well_formed(self):
        diagram = 'graph LR\n    engine["core/engine.py"]\n    engine --> jit'
        assert self._sanitize(diagram) == diagram

    def test_passthrough_unquoted_labels(self):
        diagram = "graph LR\n    a --> b\n    b --> c"
        assert self._sanitize(diagram) == diagram

    def test_handles_empty_or_none(self):
        from mira.llm.response_parser import _sanitize_mermaid

        assert _sanitize_mermaid("") == ""

    def test_repairs_label_with_extra_garbage(self):
        """Real failure mode from PR#41 — a path like `prompts/"review.py" + footguns.py"`
        gets cleaned to `prompts/review.py + footguns.py`."""
        diagram = 'graph LR\n    prompts["llm/prompts/"review.py" + "footguns.py""]\n'
        out = self._sanitize(diagram)
        # All inner quotes stripped, single pair around the cleaned label.
        assert '["llm/prompts/review.py + footguns.py"]' in out
