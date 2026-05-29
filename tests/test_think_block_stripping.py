"""Tests for think block stripping ( MiniMax etc.)."""

from __future__ import annotations

import pytest

from mira.llm.prompts.verify_fixes import parse_verify_fixes_response
from mira.llm.utils import strip_code_fences, strip_think_blocks


class TestStripThinkBlocks:
    def test_think_block_with_closing_tag(self):
        """Full <think>… block with both tags — stripped to bare JSON."""
        raw = "<think> Thinking process here.  ```json\n{\"summary\": \"ok\"}\n```"
        result = strip_think_blocks(raw)
        assert "<think>" not in result
        assert "<think>" not in result
        assert '{"summary": "ok"}' in result

    def test_think_block_at_start_no_json(self):
        """<think> at start with no JSON in remaining content — think tag stripped, rest preserved."""
        raw = "<think> thinking\n\n</think> real content"
        result = strip_think_blocks(raw)
        assert "<think>" not in result
        # No JSON found — remaining text is preserved (not empty)
        assert "real content" in result

    def test_think_block_in_middle(self):
        """<think> in middle with newlines — stripped to expose JSON."""
        raw = "prefix <think> thinking\n\n{\"key\":\"value\"}"
        result = strip_think_blocks(raw)
        assert "<think>" not in result
        assert "key" in result

    def test_think_block_with_multiline_content(self):
        """Multi-line think block content followed by bare JSON — stripped."""
        raw = "<think> line1\nline2\nline3\n\n{\"summary\":\"ok\"}"
        result = strip_think_blocks(raw)
        assert "<think>" not in result
        assert "summary" in result

    def test_think_block_with_code_fences_extracts_json(self):
        """Think blocks with backticks — the JSON inside the fence is extracted and re-serialized."""
        raw = "<think> analyzing...\n```json\n{\"summary\":\"ok\"}\n```"
        result = strip_think_blocks(raw)
        assert "<think>" not in result
        assert "```json" not in result
        assert '"summary"' in result and "ok" in result

    def test_no_think_block_passthrough(self):
        """Plain JSON without any think markers — passed through unchanged."""
        raw = '{"summary": "all good", "comments": []}'
        result = strip_think_blocks(raw)
        assert result == '{"summary": "all good", "comments": []}'

    def test_none_input(self):
        """None input returns empty string."""
        result = strip_think_blocks(None)
        assert result == ""

    def test_only_whitespace_after_stripping(self):
        """Only think block with no JSON content — think tag stripped, rest preserved."""
        raw = "<think> just thinking</think> useful output"
        result = strip_think_blocks(raw)
        assert "<think>" not in result
        assert "useful output" in result


class TestVerifyFixesWithThinkBlocks:
    """parse_verify_fixes_response must strip think blocks before JSON parsing."""

    def test_verify_fixes_with_think_block_and_fences(self):
        """MiniMax-style output: think block wrapping a JSON code fence."""
        raw = (
            "<think> Verifying which issues are fixed.\n"
            '```json\n{"results": [{"id": "abc", "fixed": true}]}\n```'
        )
        ids = parse_verify_fixes_response(raw)
        assert ids == ["abc"]

    def test_verify_fixes_with_think_block_bare_json(self):
        """Think block without fences followed by bare JSON."""
        raw = '<think> Summarizing fixes.\n{"results": [{"id": "xyz", "fixed": true}]}'
        ids = parse_verify_fixes_response(raw)
        assert ids == ["xyz"]

    def test_verify_fixes_without_think_block(self):
        """Bare JSON without think blocks — unchanged behavior."""
        raw = '{"results": [{"id": "abc", "fixed": true}]}'
        ids = parse_verify_fixes_response(raw)
        assert ids == ["abc"]

    def test_verify_fixes_empty_when_non_json(self):
        """Non-JSON think-only output returns empty list."""
        raw = "<think> just thinking about things"
        ids = parse_verify_fixes_response(raw)
        assert ids == []


class TestStripChaining:
    """Verify the correct order: strip_think_blocks first, then strip_code_fences."""

    def test_order_matters_think_then_fences(self):
        """Chaining both strip functions in the right order works."""
        raw = '<think> thinking\n```json\n{"key":"val"}\n```'
        # Correct order: think blocks stripped first, then fences
        result = strip_think_blocks(strip_code_fences(raw))
        assert "<think>" not in result
        assert "```json" not in result
        # json.dumps re-serializes with spacing
        assert '"key"' in result and '"val"' in result

    def test_fences_then_think_still_works(self):
        """Order matters less when no fences inside think block."""
        raw = '<think> thinking\n\n{"key":"val"}'
        # Both orders work for bare JSON after think
        result1 = strip_think_blocks(strip_code_fences(raw))
        result2 = strip_code_fences(strip_think_blocks(raw))
        # json.dumps re-serializes with spacing
        assert '"key"' in result1 and '"val"' in result1
        assert result1 == result2