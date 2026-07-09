"""Bot mention matching — respond to either the configured name or real identity."""

from __future__ import annotations

from mira.platforms.mentions import (
    command_after_mention,
    has_mention,
    mention_names,
    strip_mentions,
)


def test_mention_names_dedupes():
    assert mention_names("mira", None) == ["mira"]
    assert mention_names("mira", "mira") == ["mira"]
    assert mention_names("mira", "project_1_bot_x") == ["mira", "project_1_bot_x"]


def test_has_mention_matches_either():
    names = mention_names("mira", "project_1_bot_x")
    assert has_mention("hey @mira review", names)
    assert has_mention("hey @project_1_bot_x review", names)  # autocompleted real user
    assert not has_mention("no mention here", names)
    assert has_mention("@MIRA help", names)  # case-insensitive


def test_strip_mentions_removes_all_forms():
    names = mention_names("mira", "project_1_bot_x")
    assert strip_mentions("@mira review this", names) == "review this"
    assert strip_mentions("@project_1_bot_x review this", names) == "review this"


def test_command_after_mention():
    names = mention_names("mira", "project_1_bot_x")
    assert command_after_mention("@mira review", names) == "review"
    assert command_after_mention("@project_1_bot_x Reject", names) == "reject"
    assert command_after_mention("@mira", names) == ""  # no command word
    assert command_after_mention("nothing", names) == ""
