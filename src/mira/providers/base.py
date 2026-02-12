"""Abstract provider interface for code hosting platforms."""

from __future__ import annotations

import abc

from mira.models import PRInfo, ReviewResult


class BaseProvider(abc.ABC):
    """Abstract base class for code hosting providers."""

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
