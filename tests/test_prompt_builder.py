"""Tests for prompt building."""

from __future__ import annotations

from mira.config import MiraConfig
from mira.llm.prompts.review import (
    build_dependency_review_prompt,
    build_review_prompt,
)
from mira.models import FileChangeType, FileDiff, HunkInfo


class TestBuildReviewPrompt:
    def test_returns_two_messages(self):
        files = [
            FileDiff(
                path="test.py",
                change_type=FileChangeType.MODIFIED,
                hunks=[HunkInfo(1, 5, 1, 5, "@@ -1,5 +1,5 @@\n-old\n+new")],
                language="python",
                added_lines=1,
                deleted_lines=1,
            )
        ]
        config = MiraConfig()
        messages = build_review_prompt(files, config)
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    def test_system_message_contains_instructions(self):
        files = [
            FileDiff(
                path="test.py",
                change_type=FileChangeType.MODIFIED,
                hunks=[HunkInfo(1, 5, 1, 5, "content")],
                language="python",
                added_lines=1,
                deleted_lines=1,
            )
        ]
        config = MiraConfig()
        messages = build_review_prompt(files, config)
        system = messages[0]["content"]
        assert "Mira" in system
        assert "submit_review" in system
        assert "blocker" in system

    def test_includes_file_paths(self):
        files = [
            FileDiff(
                path="src/app.py",
                change_type=FileChangeType.MODIFIED,
                hunks=[HunkInfo(1, 5, 1, 5, "content")],
                added_lines=1,
                deleted_lines=0,
            ),
            FileDiff(
                path="src/utils.py",
                change_type=FileChangeType.ADDED,
                hunks=[HunkInfo(1, 5, 1, 5, "content")],
                added_lines=5,
                deleted_lines=0,
            ),
        ]
        config = MiraConfig()
        messages = build_review_prompt(files, config)
        system = messages[0]["content"]
        assert "src/app.py" in system
        assert "src/utils.py" in system

    def test_includes_pr_info(self):
        files = [
            FileDiff(
                path="test.py",
                change_type=FileChangeType.MODIFIED,
                hunks=[HunkInfo(1, 5, 1, 5, "content")],
                added_lines=1,
                deleted_lines=0,
            )
        ]
        config = MiraConfig()
        messages = build_review_prompt(
            files,
            config,
            pr_title="Add feature X",
            pr_description="This PR adds feature X",
        )
        system = messages[0]["content"]
        assert "Add feature X" in system
        assert "This PR adds feature X" in system

    def test_user_message_contains_diffs(self):
        files = [
            FileDiff(
                path="test.py",
                change_type=FileChangeType.MODIFIED,
                hunks=[HunkInfo(1, 5, 1, 5, "@@ content here @@")],
                language="python",
                added_lines=1,
                deleted_lines=0,
            )
        ]
        config = MiraConfig()
        messages = build_review_prompt(files, config)
        user = messages[1]["content"]
        assert "test.py" in user
        assert "content here" in user

    def test_focus_only_on_problems_default(self):
        files = [
            FileDiff(
                path="test.py",
                change_type=FileChangeType.MODIFIED,
                hunks=[HunkInfo(1, 5, 1, 5, "content")],
                language="python",
                added_lines=1,
                deleted_lines=0,
            )
        ]
        config = MiraConfig()  # default: focus_only_on_problems=False
        messages = build_review_prompt(files, config)
        system = messages[0]["content"]
        assert "You may suggest improvements" in system
        assert "Only comment on critical problems" not in system

    def test_focus_on_shows_problems_only(self):
        files = [
            FileDiff(
                path="test.py",
                change_type=FileChangeType.MODIFIED,
                hunks=[HunkInfo(1, 5, 1, 5, "content")],
                language="python",
                added_lines=1,
                deleted_lines=0,
            )
        ]
        config = MiraConfig(review={"focus_only_on_problems": True})
        messages = build_review_prompt(files, config)
        system = messages[0]["content"]
        assert "Only comment on critical problems" in system
        assert "You may suggest improvements" not in system

    def test_scope_boundary_instructions(self):
        files = [
            FileDiff(
                path="test.py",
                change_type=FileChangeType.MODIFIED,
                hunks=[HunkInfo(1, 5, 1, 5, "content")],
                language="python",
                added_lines=1,
                deleted_lines=0,
            )
        ]
        config = MiraConfig()
        messages = build_review_prompt(files, config)
        system = messages[0]["content"]
        assert "not the entire codebase" in system
        assert "scope boundary" in system

    def test_existing_code_in_schema(self):
        files = [
            FileDiff(
                path="test.py",
                change_type=FileChangeType.MODIFIED,
                hunks=[HunkInfo(1, 5, 1, 5, "content")],
                language="python",
                added_lines=1,
                deleted_lines=0,
            )
        ]
        config = MiraConfig()
        messages = build_review_prompt(files, config)
        system = messages[0]["content"]
        assert "existing_code" in system


class TestBuildDependencyReviewPrompt:
    def _manifest(self):
        return [
            FileDiff(
                path="package.json",
                change_type=FileChangeType.MODIFIED,
                hunks=[
                    HunkInfo(
                        1,
                        6,
                        1,
                        7,
                        '@@ -1,6 +1,7 @@\n {\n+    "@tanstack/react-table": "^8.10.0",\n }',
                    )
                ],
                added_lines=1,
                deleted_lines=0,
            )
        ]

    def test_returns_two_messages_with_paths(self):
        messages = build_dependency_review_prompt(
            self._manifest(), existing_packages=["react-table"]
        )
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert "package.json" in messages[0]["content"]
        assert "submit_review" in messages[0]["content"]

    def test_injects_existing_package_names(self):
        messages = build_dependency_review_prompt(
            self._manifest(), existing_packages=["react-table", "lodash"]
        )
        system = messages[0]["content"]
        assert "react-table" in system
        assert "lodash" in system
        # The pass must tag its findings so they're recognised downstream.
        assert "dependency" in system

    def test_handles_no_existing_packages(self):
        """Unindexed repo (empty list) must still render without error."""
        messages = build_dependency_review_prompt(self._manifest(), existing_packages=[])
        system = messages[0]["content"]
        assert "isn't indexed" in system
