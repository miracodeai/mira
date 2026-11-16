"""Authorization for interactive @-mention commands.

Auto-review is governed by ``filter.allowed_authors`` / ``blocked_authors``.
This module gates the commands that spend API credits or mutate a PR: a user
must either be explicitly allowlisted or have write access to the repo.
"""

from __future__ import annotations

import logging

from mira.config import CommandsConfig, MiraConfig
from mira.providers.base import BaseProvider

logger = logging.getLogger(__name__)

COMMANDS = frozenset({"review", "review_rest", "help", "pause", "resume", "reject", "ask"})


def _normalized(value: str) -> str:
    return value.strip().lower()


def _matches(login: str, names: list[str]) -> bool:
    """Case-insensitive match, also accepting the un-suffixed bot login."""
    if not login:
        return False
    normalized_names = {_normalized(name) for name in names}
    variants = {_normalized(login), _normalized(login.removesuffix("[bot]"))}
    return any(variant in normalized_names for variant in variants)


def _allowed_users(commands: CommandsConfig, command: str) -> list[str]:
    override = getattr(commands, command, None)
    if override is None or override.allowed_users is None:
        return commands.allowed_users
    return override.allowed_users


def _blocked_users(commands: CommandsConfig, command: str) -> list[str]:
    override = getattr(commands, command, None)
    if override is None or override.blocked_users is None:
        return []
    return override.blocked_users


async def is_command_allowed(
    config: MiraConfig,
    command: str,
    actor: str,
    *,
    provider: BaseProvider,
    owner: str,
    repo: str,
    user_id: int | str | None = None,
) -> bool:
    """Whether ``actor`` may run ``command`` on ``owner/repo``.

    On platforms whose usernames are not unique (``provider.unique_usernames``
    is False, e.g. Bitbucket nicknames), ``allowed_users`` grants match
    ``user_id`` only, so another account with the same name cannot claim
    them. ``blocked_users`` matches the name or the id, so blocking still
    errs on the side of denial.
    """
    if command not in COMMANDS:
        logger.warning("Unknown Mira command %r; denying", command)
        return False
    if not actor:
        return False

    commands = config.commands
    unique_names = getattr(provider, "unique_usernames", True)
    user_key = str(user_id) if user_id not in (None, "") else ""
    # The identifiers an allowlist entry may match for this actor.
    grant_keys = [actor, user_key] if unique_names else [user_key]

    def blocked(names: list[str]) -> bool:
        return _matches(actor, names) or _matches(user_key, names)

    def granted(names: list[str]) -> bool:
        return any(_matches(key, names) for key in grant_keys)

    if blocked(commands.blocked_users):
        logger.info(
            "Command %s denied for @%s on %s/%s — user is in commands.blocked_users",
            command,
            actor,
            owner,
            repo,
        )
        return False

    if blocked(_blocked_users(commands, command)):
        logger.info(
            "Command %s denied for @%s on %s/%s — user is in commands.%s.blocked_users",
            command,
            actor,
            owner,
            repo,
            command,
        )
        return False

    if granted(_allowed_users(commands, command)):
        return True

    if not commands.allow_repo_write_access:
        logger.info(
            "Command %s denied for @%s on %s/%s — not in the allowlist and "
            "repo write access is disabled",
            command,
            actor,
            owner,
            repo,
        )
        return False

    try:
        permission = await provider.get_repo_permission(owner, repo, actor, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 - authorization must fail closed
        logger.warning(
            "Command %s denied for @%s on %s/%s — permission lookup failed: %s",
            command,
            actor,
            owner,
            repo,
            exc,
        )
        return False

    if permission == "write":
        return True

    logger.info(
        "Command %s denied for @%s on %s/%s — repo permission is %r",
        command,
        actor,
        owner,
        repo,
        permission,
    )
    return False
