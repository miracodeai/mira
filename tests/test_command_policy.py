"""Tests for interactive command authorization."""

from __future__ import annotations

import pytest

from mira.config import (
    CommandOverrideConfig,
    CommandsConfig,
    MiraConfig,
)
from mira.platforms.command_policy import is_command_allowed


class _Provider:
    def __init__(self, permission: str | None = "write", error: Exception | None = None):
        self.permission = permission
        self.error = error
        self.calls: list[tuple[str, str, str, int | str | None]] = []

    async def get_repo_permission(
        self,
        owner: str,
        repo: str,
        username: str,
        *,
        user_id: int | str | None = None,
    ) -> str | None:
        self.calls.append((owner, repo, username, user_id))
        if self.error:
            raise self.error
        return self.permission


def _config(**kwargs) -> MiraConfig:
    return MiraConfig(commands=CommandsConfig(**kwargs))


async def _allowed(config: MiraConfig, command: str, actor: str, provider: _Provider) -> bool:
    return await is_command_allowed(
        config,
        command,
        actor,
        provider=provider,
        owner="acme",
        repo="widgets",
    )


@pytest.mark.asyncio
async def test_writer_allowed_by_default():
    provider = _Provider("write")
    assert await _allowed(MiraConfig(), "review", "alice", provider)


@pytest.mark.asyncio
async def test_reader_denied_by_default():
    provider = _Provider("read")
    assert not await _allowed(MiraConfig(), "review", "alice", provider)


@pytest.mark.asyncio
async def test_unknown_permission_denied():
    provider = _Provider(None)
    assert not await _allowed(MiraConfig(), "review", "alice", provider)


@pytest.mark.asyncio
async def test_permission_lookup_failure_denied():
    provider = _Provider(error=RuntimeError("api down"))
    assert not await _allowed(MiraConfig(), "review", "alice", provider)


@pytest.mark.asyncio
async def test_allowed_user_skips_permission_lookup():
    provider = _Provider("read")
    config = _config(allowed_users=["alice"])
    assert await _allowed(config, "review", "alice", provider)
    assert provider.calls == []


@pytest.mark.asyncio
async def test_blocked_user_wins_over_allowlist_and_write_access():
    provider = _Provider("write")
    config = _config(allowed_users=["alice"], blocked_users=["alice"])
    assert not await _allowed(config, "review", "alice", provider)


@pytest.mark.asyncio
async def test_disabling_write_access_denies_writer():
    provider = _Provider("write")
    config = _config(allow_repo_write_access=False)
    assert not await _allowed(config, "review", "alice", provider)


@pytest.mark.asyncio
async def test_per_command_block_beats_write_access():
    provider = _Provider("write")
    config = _config(ask=CommandOverrideConfig(blocked_users=["alice"]))

    assert await _allowed(config, "review", "alice", provider)
    assert not await _allowed(config, "ask", "alice", provider)


@pytest.mark.asyncio
async def test_per_command_block_beats_allowlist():
    provider = _Provider("read")
    config = _config(
        allowed_users=["alice"],
        review=CommandOverrideConfig(blocked_users=["alice"]),
    )

    assert await _allowed(config, "ask", "alice", provider)
    assert not await _allowed(config, "review", "alice", provider)


@pytest.mark.asyncio
async def test_per_command_block_only_affects_its_command():
    provider = _Provider("write")
    config = _config(
        ask=CommandOverrideConfig(blocked_users=["alice"]),
        pause=CommandOverrideConfig(blocked_users=["bob"]),
    )

    assert not await _allowed(config, "ask", "alice", provider)
    assert not await _allowed(config, "pause", "bob", provider)
    assert await _allowed(config, "pause", "alice", provider)
    assert await _allowed(config, "ask", "bob", provider)


@pytest.mark.asyncio
async def test_global_block_cannot_be_undone_by_per_command_allowlist():
    provider = _Provider("write")
    config = _config(
        blocked_users=["alice"],
        review=CommandOverrideConfig(allowed_users=["alice"]),
    )

    assert not await _allowed(config, "review", "alice", provider)


@pytest.mark.asyncio
async def test_per_command_allowlist_replaces_global_for_that_command():
    provider = _Provider("read")
    config = _config(
        allowed_users=["alice"],
        allow_repo_write_access=False,
        review=CommandOverrideConfig(allowed_users=["bob"]),
    )
    assert not await _allowed(config, "review", "alice", provider)
    assert await _allowed(config, "ask", "alice", provider)
    assert await _allowed(config, "review", "bob", provider)
    assert not await _allowed(config, "ask", "bob", provider)


@pytest.mark.asyncio
async def test_write_access_applies_across_all_commands():
    provider = _Provider("write")
    config = _config(review=CommandOverrideConfig(allowed_users=["bob"]))

    assert await _allowed(config, "review", "alice", provider)
    assert await _allowed(config, "ask", "alice", provider)


@pytest.mark.asyncio
async def test_per_command_override_does_not_re_enable_global_write_gate():
    provider = _Provider("write")
    config = _config(
        allow_repo_write_access=False,
        review=CommandOverrideConfig(allowed_users=["bob"]),
    )

    assert not await _allowed(config, "review", "alice", provider)
    assert await _allowed(config, "review", "bob", provider)
    assert not await _allowed(config, "ask", "bob", provider)


@pytest.mark.asyncio
async def test_allowlist_matching_is_case_insensitive_and_strips_bot_suffix():
    provider = _Provider("read")
    config = _config(allowed_users=["Dependabot"])
    assert await _allowed(config, "review", "dependabot[bot]", provider)


@pytest.mark.asyncio
async def test_unknown_command_denied():
    provider = _Provider("write")
    assert not await _allowed(MiraConfig(), "delete-everything", "alice", provider)


class _NamesNotUniqueProvider(_Provider):
    """Like Bitbucket: nicknames can be shared, so only the account id is trusted."""

    unique_usernames = False


async def _allowed_by_id(
    config: MiraConfig, actor: str, user_id: str, provider: _Provider, command: str = "review"
) -> bool:
    return await is_command_allowed(
        config,
        command,
        actor,
        provider=provider,
        owner="acme",
        repo="widgets",
        user_id=user_id,
    )


@pytest.mark.asyncio
async def test_non_unique_names_allowlisted_name_is_not_a_grant():
    """Another account sharing an allowlisted nickname must not inherit the grant."""
    provider = _NamesNotUniqueProvider("read")
    config = _config(allowed_users=["alice"])
    assert not await _allowed_by_id(config, "alice", "557058:mallory", provider)
    assert provider.calls == [("acme", "widgets", "alice", "557058:mallory")]


@pytest.mark.asyncio
async def test_non_unique_names_allowlist_matches_account_id():
    provider = _NamesNotUniqueProvider("read")
    config = _config(allowed_users=["557058:alice"])
    assert await _allowed_by_id(config, "alice", "557058:alice", provider)
    assert provider.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["alice", "557058:alice"])
async def test_non_unique_names_block_matches_name_or_account_id(entry):
    provider = _NamesNotUniqueProvider("write")
    config = _config(blocked_users=[entry])
    assert not await _allowed_by_id(config, "alice", "557058:alice", provider)


@pytest.mark.asyncio
async def test_non_unique_names_per_command_allowlist_matches_account_id_only():
    provider = _NamesNotUniqueProvider("read")
    config = _config(review=CommandOverrideConfig(allowed_users=["alice", "557058:bob"]))
    assert not await _allowed_by_id(config, "alice", "557058:mallory", provider)
    assert await _allowed_by_id(config, "bob", "557058:bob", provider)


@pytest.mark.asyncio
async def test_permission_lookup_receives_account_id():
    provider = _NamesNotUniqueProvider("write")
    assert await _allowed_by_id(MiraConfig(), "alice", "557058:alice", provider)
    assert provider.calls == [("acme", "widgets", "alice", "557058:alice")]
