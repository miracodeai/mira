"""Abstract provider interface for code hosting platforms."""

from __future__ import annotations

import abc

from mira.models import PRInfo, ReviewResult, UnresolvedThread


class BaseProvider(abc.ABC):
    """Abstract base class for code hosting providers."""

    @abc.abstractmethod
    def __init__(self, token: str) -> None:
        """Initialize the provider with an authentication token.

        Subclasses must implement this to configure their API client
        using the provided token.
        """

    @abc.abstractmethod
    async def get_pr_info(self, pr_url: str) -> PRInfo:
        """Fetch metadata about a pull request."""

    @abc.abstractmethod
    async def get_pr_diff(self, pr_info: PRInfo) -> str:
        """Fetch the raw diff for a pull request."""

    @abc.abstractmethod
    async def post_review(
        self,
        pr_info: PRInfo,
        result: ReviewResult,
    ) -> None:
        """Post review comments to a pull request."""

    @abc.abstractmethod
    async def post_comment(self, pr_info: PRInfo, body: str) -> None:
        """Post a top-level comment on a pull request."""

    @abc.abstractmethod
    async def find_bot_comment(self, pr_info: PRInfo, marker: str) -> int | None:
        """Find an existing comment containing the marker. Returns comment ID or None."""

    @abc.abstractmethod
    async def update_comment(self, pr_info: PRInfo, comment_id: int, body: str) -> None:
        """Edit an existing comment by its ID."""

    @abc.abstractmethod
    async def resolve_outdated_review_threads(self, pr_info: PRInfo) -> int:
        """Resolve all unresolved review threads authored by this bot. Returns count resolved."""

    async def get_unresolved_bot_threads(
        self, pr_info: PRInfo, bot_login: str | None = None
    ) -> list[UnresolvedThread]:
        """Fetch all unresolved review threads authored by the bot."""
        return []

    async def resolve_threads(self, pr_info: PRInfo, thread_ids: list[str]) -> int:
        """Resolve review threads by ID. Returns count of successfully resolved."""
        return 0

    async def get_file_content(self, pr_info: PRInfo, path: str, ref: str) -> str:
        """Fetch file content at a specific ref."""
        return ""
