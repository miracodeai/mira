"""Tests for response parsing with MiniMax think block outputs."""

from __future__ import annotations

from mira.llm.response_parser import (
    parse_llm_response,
    parse_walkthrough_response,
)


class TestParseWithThinkBlocks:
    """parse_llm_response and parse_walkthrough_response must handle
    <think>… think blocks emitted by models like MiniMax-2.7."""

    def test_llm_response_with_think_block_wrapping_json(self):
        """Real MiniMax output: think block around the JSON block."""
        raw = (
            "<think> Let me analyze the security implications of this code change. "
            "The subprocess.run call with shell=True is dangerous. "
            "I should flag this as a blocker issue."
            '```json\n{"comments":[{"path":"src/utils.py","line":9,"severity":"blocker","category":"security",'
            '"title":"Shell injection","body":"Using shell=True is vulnerable","confidence":0.95,'
            '"existing_code":"subprocess.run(cmd, shell=True)"}],"summary":"Security issue found",'
            '"metadata":{"reviewed_files":1,"skipped_reason":null}}\n```'
        )
        result = parse_llm_response(raw)
        assert len(result.comments) == 1
        assert result.comments[0].severity == "blocker"
        assert result.summary == "Security issue found"

    def test_llm_response_with_think_no_fences_no_truncation(self):
        """Think block without fences — preamble has no backticks but also no newlines
        before the JSON, so no truncation happens. JSON starts immediately after text."""
        raw = '<think> Summarizing the changes.{"comments":[],"summary":"ok","metadata":{"reviewed_files":1}}'
        result = parse_llm_response(raw)
        assert result.comments == []

    def test_walkthrough_response_with_think_block(self):
        """parse_walkthrough_response must also strip think blocks."""
        raw = (
            "<think> Let me summarize the changes in this PR. "
            "There are two main file groups to highlight."
            '```json\n{"summary":"PR adds utility module","change_groups":[{"label":"Core",'
            '"files":[{"path":"src/utils.py","change_type":"added",'
            '"description":"New utility functions"}]}]}\n```'
        )
        result = parse_walkthrough_response(raw)
        assert result.summary == "PR adds utility module"
        assert len(result.change_groups) == 1

    def test_walkthrough_response_no_fences_no_truncation(self):
        """Think block without fences and no newlines in preamble — JSON starts after text."""
        raw = '<think> Summarizing.{"summary":"All good","change_groups":[]}'
        result = parse_walkthrough_response(raw)
        assert result.summary == "All good"


class TestParserRegressionExisting:
    """Existing models (non-thinking) must not be affected by think-block stripping."""

    def test_parse_without_think_block_is_unchanged(self):
        """Bare JSON without think blocks parses identically to before."""
        data = '{"comments":[{"path":"a.py","line":1,"severity":"warning","category":"clarity","title":"T","body":"D","confidence":0.9,"existing_code":"x"}],"summary":"ok","metadata":{"reviewed_files":1}}'
        result = parse_llm_response(data)
        assert len(result.comments) == 1

    def test_parse_with_code_fences_still_works(self):
        """Code fence stripping still works when no think block is present."""
        raw = '```json\n{"comments":[],"summary":"ok","metadata":{"reviewed_files":1}}\n```'
        result = parse_llm_response(raw)
        assert result.summary == "ok"

    def test_walkthrough_code_fences_still_works(self):
        raw = '```json\n{"summary":"ok","change_groups":[]}\n```'
        result = parse_walkthrough_response(raw)
        assert result.summary == "ok"
