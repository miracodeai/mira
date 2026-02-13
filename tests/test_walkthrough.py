"""Tests for walkthrough prompt builder, response parser, and markdown rendering."""

from __future__ import annotations

import json

import pytest

from mira.config import MiraConfig
from mira.llm.prompts.review import build_walkthrough_prompt
from mira.llm.response_parser import (
    convert_to_walkthrough_result,
    parse_walkthrough_response,
)
from mira.models import (
    FileChangeType,
    FileDiff,
    HunkInfo,
    WalkthroughEffort,
    WalkthroughFileEntry,
    WalkthroughResult,
)


class TestBuildWalkthroughPrompt:
    def _make_files(self) -> list[FileDiff]:
        return [
            FileDiff(
                path="src/utils.py",
                change_type=FileChangeType.ADDED,
                hunks=[
                    HunkInfo(
                        source_start=0,
                        source_length=0,
                        target_start=1,
                        target_length=5,
                        content="@@ -0,0 +1,5 @@\n+import os\n+def run(): pass",
                    )
                ],
                language="python",
                added_lines=5,
                deleted_lines=0,
            ),
            FileDiff(
                path="src/main.py",
                change_type=FileChangeType.MODIFIED,
                hunks=[
                    HunkInfo(
                        source_start=10,
                        source_length=3,
                        target_start=10,
                        target_length=5,
                        content=(
                            "@@ -10,3 +10,5 @@ class App:\n"
                            "     def start(self):\n+        debug=False"
                        ),
                    )
                ],
                language="python",
                added_lines=2,
                deleted_lines=0,
            ),
        ]

    def test_returns_two_messages(self):
        messages = build_walkthrough_prompt(
            files=self._make_files(),
            config=MiraConfig(),
        )
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    def test_system_prompt_contains_file_metadata(self):
        messages = build_walkthrough_prompt(
            files=self._make_files(),
            config=MiraConfig(),
        )
        system = messages[0]["content"]
        assert "src/utils.py" in system
        assert "src/main.py" in system
        assert "added" in system
        assert "modified" in system

    def test_includes_pr_title(self):
        messages = build_walkthrough_prompt(
            files=self._make_files(),
            config=MiraConfig(),
            pr_title="Add utilities",
            pr_description="Some new helpers",
        )
        system = messages[0]["content"]
        assert "Add utilities" in system
        assert "Some new helpers" in system

    def test_sequence_diagram_flag(self):
        config = MiraConfig()
        config.review.walkthrough_sequence_diagram = True
        messages = build_walkthrough_prompt(
            files=self._make_files(),
            config=config,
        )
        system = messages[0]["content"]
        assert "sequence diagram" in system.lower() or "sequence_diagram" in system
        # Prompt must instruct the LLM to use actual code components, not generic actors
        assert "code-level component interactions" in system
        # Verify the prohibition is explicit — template renders bold markdown
        assert "**Do NOT**" in system
        assert "**Do NOT** use abstract actors" in system
        assert "Developer" in system
        assert "null" in system  # instruction to omit when no interactions

    def test_hunk_headers_extracted(self):
        messages = build_walkthrough_prompt(
            files=self._make_files(),
            config=MiraConfig(),
        )
        system = messages[0]["content"]
        assert "@@ -0,0 +1,5 @@" in system


class TestParseWalkthroughResponse:
    def test_basic_parse(self, sample_walkthrough_response_text: str):
        result = parse_walkthrough_response(sample_walkthrough_response_text)
        assert result.summary != ""
        assert len(result.change_groups) == 2
        assert result.change_groups[0].label == "Core"
        assert result.change_groups[0].files[0].path == "src/utils.py"
        assert result.change_groups[0].files[0].change_type == "added"

    def test_with_code_fences(self):
        raw = '```json\n{"summary": "test", "change_groups": []}\n```'
        result = parse_walkthrough_response(raw)
        assert result.summary == "test"

    def test_invalid_json_raises(self):
        from mira.exceptions import ResponseParseError

        with pytest.raises(ResponseParseError, match="not valid JSON"):
            parse_walkthrough_response("NOT JSON {{{")

    def test_non_object_raises(self):
        from mira.exceptions import ResponseParseError

        with pytest.raises(ResponseParseError, match="Expected JSON object"):
            parse_walkthrough_response("[1, 2, 3]")

    def test_with_sequence_diagram(self):
        raw = json.dumps(
            {
                "summary": "Changes",
                "change_groups": [],
                "sequence_diagram": "sequenceDiagram\n    A->>B: call",
            }
        )
        result = parse_walkthrough_response(raw)
        assert result.sequence_diagram is not None
        assert "sequenceDiagram" in result.sequence_diagram

    def test_with_effort(self):
        raw = json.dumps(
            {
                "summary": "Changes",
                "change_groups": [],
                "effort": {"level": 3, "label": "Moderate", "minutes": 20},
            }
        )
        result = parse_walkthrough_response(raw)
        assert result.effort is not None
        assert result.effort.level == 3
        assert result.effort.label == "Moderate"
        assert result.effort.minutes == 20

    def test_without_effort(self):
        raw = json.dumps({"summary": "Changes", "change_groups": []})
        result = parse_walkthrough_response(raw)
        assert result.effort is None


class TestConvertToWalkthroughResult:
    def test_basic_conversion(self, sample_walkthrough_response_text: str):
        parsed = parse_walkthrough_response(sample_walkthrough_response_text)
        result = convert_to_walkthrough_result(parsed)
        assert isinstance(result, WalkthroughResult)
        assert result.summary != ""
        assert len(result.file_changes) == 2
        assert result.file_changes[0].change_type == FileChangeType.ADDED
        assert result.file_changes[0].group == "Core"
        assert result.file_changes[1].change_type == FileChangeType.MODIFIED
        assert result.file_changes[1].group == "App Shell"

    def test_unknown_change_type_defaults_to_modified(self):
        from mira.llm.response_parser import (
            LLMWalkthroughChangeGroup,
            LLMWalkthroughFileChange,
            LLMWalkthroughResponse,
        )

        response = LLMWalkthroughResponse(
            summary="test",
            change_groups=[
                LLMWalkthroughChangeGroup(
                    label="Misc",
                    files=[
                        LLMWalkthroughFileChange(
                            path="foo.py", change_type="unknown_type", description="desc"
                        )
                    ],
                )
            ],
        )
        result = convert_to_walkthrough_result(response)
        assert result.file_changes[0].change_type == FileChangeType.MODIFIED
        assert result.file_changes[0].group == "Misc"

    def test_effort_conversion(self):
        raw = json.dumps(
            {
                "summary": "test",
                "change_groups": [],
                "effort": {"level": 2, "label": "Simple", "minutes": 10},
            }
        )
        parsed = parse_walkthrough_response(raw)
        result = convert_to_walkthrough_result(parsed)
        assert result.effort is not None
        assert result.effort.level == 2
        assert result.effort.label == "Simple"
        assert result.effort.minutes == 10

    def test_no_effort_conversion(self):
        raw = json.dumps({"summary": "test", "change_groups": []})
        parsed = parse_walkthrough_response(raw)
        result = convert_to_walkthrough_result(parsed)
        assert result.effort is None


class TestWalkthroughToMarkdown:
    def test_grouped_markdown(self):
        result = WalkthroughResult(
            summary="Added new features.",
            file_changes=[
                WalkthroughFileEntry(
                    path="src/utils.py",
                    change_type=FileChangeType.ADDED,
                    description="New utils",
                    group="Core",
                ),
                WalkthroughFileEntry(
                    path="tests/test_utils.py",
                    change_type=FileChangeType.ADDED,
                    description="Tests for utils",
                    group="Tests",
                ),
            ],
        )
        md = result.to_markdown()
        assert "## Mira PR Walkthrough" in md
        assert "Added new features." in md
        assert "**Core**" in md
        assert "| `src/utils.py` | Added | New utils |" in md
        assert "**Tests**" in md
        assert "| `tests/test_utils.py` | Added | Tests for utils |" in md
        lines = md.split("\n")
        separator_idx = lines.index("---")
        footer_text = "\n".join(lines[separator_idx:])
        assert "@miracodeai help" in footer_text

    def test_flat_fallback_when_no_groups(self):
        result = WalkthroughResult(
            summary="Simple change.",
            file_changes=[
                WalkthroughFileEntry(
                    path="src/utils.py",
                    change_type=FileChangeType.ADDED,
                    description="New utils",
                ),
            ],
        )
        md = result.to_markdown()
        assert "## Mira PR Walkthrough" in md
        assert "| `src/utils.py` | Added | New utils |" in md
        # No group headers in flat mode
        assert "**Core**" not in md
        assert "**Other**" not in md

    def test_with_sequence_diagram(self):
        result = WalkthroughResult(
            summary="Changes.",
            sequence_diagram="sequenceDiagram\n    A->>B: call",
        )
        md = result.to_markdown()
        assert "### Sequence Diagram" in md
        assert "```mermaid" in md
        assert "sequenceDiagram" in md

    def test_no_files_no_table(self):
        result = WalkthroughResult(summary="Empty.")
        md = result.to_markdown()
        assert "### Changes" not in md
        assert "| File |" not in md

    def test_no_diagram_no_section(self):
        result = WalkthroughResult(summary="No diagram.")
        md = result.to_markdown()
        assert "### Sequence Diagram" not in md
        assert "```mermaid" not in md

    def test_with_effort(self):
        result = WalkthroughResult(
            summary="Changes.",
            effort=WalkthroughEffort(level=3, label="Moderate", minutes=20),
        )
        md = result.to_markdown()
        assert "**Estimated effort:**" in md
        assert "3 (Moderate)" in md
        assert "~20 min" in md

    def test_no_effort_no_section(self):
        result = WalkthroughResult(summary="No effort.")
        md = result.to_markdown()
        assert "**Estimated effort:**" not in md

    def test_help_footer(self):
        result = WalkthroughResult(summary="Footer test.")
        md = result.to_markdown()
        lines = md.split("\n")
        separator_idx = lines.index("---")
        footer_text = "\n".join(lines[separator_idx:])
        assert "`@miracodeai help`" in footer_text
        assert "available commands and usage tips" in footer_text

    def test_help_footer_custom_bot_name(self):
        result = WalkthroughResult(summary="Footer test.")
        md = result.to_markdown(bot_name="mybot")
        assert "`@mybot help`" in md
        assert "@miracodeai" not in md
