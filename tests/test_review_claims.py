"""Tests for durable, cross-worker PR review claims."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import MagicMock, patch

import pytest

from mira.dashboard.db import AppDatabase
from mira.platforms.handlers import _review_claim


def _db(path: str) -> AppDatabase:
    return AppDatabase(url=path, admin_password="admin")


def test_only_one_database_instance_can_claim_a_pr(tmp_path) -> None:
    db_path = str(tmp_path / "app.db")
    first = _db(db_path)
    second = _db(db_path)
    barrier = Barrier(2)

    def claim(db: AppDatabase) -> str | None:
        barrier.wait()
        return db.try_claim_review("acme", "api", 42, platform="forgejo")

    with ThreadPoolExecutor(max_workers=2) as pool:
        tokens = list(pool.map(claim, (first, second)))

    assert sum(token is not None for token in tokens) == 1


def test_expired_claim_can_be_recovered_but_old_owner_cannot_release_it(tmp_path) -> None:
    db_path = str(tmp_path / "app.db")
    first = _db(db_path)
    second = _db(db_path)

    expired_token = first.try_claim_review("acme", "api", 42, platform="forgejo", ttl_seconds=-1)
    replacement_token = second.try_claim_review("acme", "api", 42, platform="forgejo")

    assert expired_token is not None
    assert replacement_token is not None
    assert replacement_token != expired_token
    assert not first.release_review_claim("acme", "api", 42, expired_token, platform="forgejo")
    assert second.refresh_review_claim("acme", "api", 42, replacement_token, platform="forgejo")
    assert second.release_review_claim("acme", "api", 42, replacement_token, platform="forgejo")


@pytest.mark.asyncio
async def test_review_claim_releases_lease_when_review_fails() -> None:
    db = MagicMock()
    db.try_claim_review.return_value = "owned-token"
    db.release_review_claim.return_value = True
    tracker = MagicMock()

    with (
        patch("mira.dashboard.api._app_db", db),
        patch("mira.platforms.handlers.review_tracker", tracker),
        pytest.raises(RuntimeError, match="review failed"),
    ):
        async with _review_claim(
            "acme", "api", 42, "Improve API", "https://forgejo/pr/42", "forgejo"
        ) as claimed:
            assert claimed
            raise RuntimeError("review failed")

    tracker.start.assert_called_once()
    db.release_review_claim.assert_called_once_with(
        "acme", "api", 42, "owned-token", platform="forgejo"
    )


@pytest.mark.asyncio
async def test_review_claim_skips_when_another_worker_owns_pr() -> None:
    db = MagicMock()
    db.try_claim_review.return_value = None
    tracker = MagicMock()

    with (
        patch("mira.dashboard.api._app_db", db),
        patch("mira.platforms.handlers.review_tracker", tracker),
    ):
        async with _review_claim(
            "acme", "api", 42, "Improve API", "https://forgejo/pr/42", "forgejo"
        ) as claimed:
            assert not claimed

    tracker.start.assert_not_called()
    db.release_review_claim.assert_not_called()
