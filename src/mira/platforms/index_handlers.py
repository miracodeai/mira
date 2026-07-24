"""Platform-neutral indexing handlers (incremental push indexing)."""

from __future__ import annotations

import logging
from typing import Any

from mira.config import load_config
from mira.index.indexer import index_diff, index_repo
from mira.index.store import IndexStore
from mira.llm import create_llm

logger = logging.getLogger(__name__)

_INCREMENTAL_FILE_CAP = 50  # Above this, queue a full re-index instead


def _get_app_db():
    """Get the app database instance. Lazy import to avoid circular deps."""
    from mira.dashboard.api import _app_db

    return _app_db


async def run_incremental_index(
    owner: str,
    repo: str,
    fetcher: Any,
    changed_paths: list[str],
    removed_paths: list[str],
    default_branch: str,
    platform: str = "github",
) -> None:
    """Platform-neutral push-indexing core, shared by GitHub and GitLab.

    Re-indexes changed files (or a full re-index past the file cap) using the
    given ``RepoFetcher`` and bumps the repo's last-indexed timestamp.
    """
    app_db = _get_app_db()
    config = load_config()
    from mira.dashboard.models_config import llm_config_for

    llm = create_llm(llm_config_for("indexing", config.llm))
    store = IndexStore.open(owner, repo, platform=platform)

    total_affected = len(changed_paths) + len(removed_paths)
    if total_affected > _INCREMENTAL_FILE_CAP:
        logger.info(
            "Push to %s/%s touched %d files (cap=%d), running full re-index",
            owner,
            repo,
            total_affected,
            _INCREMENTAL_FILE_CAP,
        )
        count = await index_repo(
            owner=owner,
            repo=repo,
            config=config,
            store=store,
            llm=llm,
            branch=default_branch,
            fetcher=fetcher,
        )
    else:
        count = await index_diff(
            owner=owner,
            repo=repo,
            config=config,
            store=store,
            llm=llm,
            changed_paths=changed_paths,
            removed_paths=removed_paths,
            branch=default_branch,
            fetcher=fetcher,
        )

    store.close()

    if count > 0:
        try:
            app_db.set_repo_status(
                owner,
                repo,
                "ready",
                files_indexed=count,
                bump_last_indexed=True,
                platform=platform,
            )
        except Exception as exc:
            logger.warning("Failed to update repo status after push: %s", exc)

    logger.info("Incremental index for %s/%s: %d files", owner, repo, count)
