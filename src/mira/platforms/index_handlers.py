"""Platform-neutral indexing handlers (incremental push indexing)."""

from __future__ import annotations

import logging

import httpx

from mira.config import load_config
from mira.index.indexer import IndexingRecoveryPending, index_diff, index_repo
from mira.index.store import IndexingSuperseded, IndexStore
from mira.llm import create_llm
from mira.platforms.fetch import EmptyRepoError, IncompleteTreeError, RepoFetcher

logger = logging.getLogger(__name__)

_INCREMENTAL_FILE_CAP = 50  # Above this, queue a full re-index instead


def _get_app_db():
    """Get the app database instance. Lazy import to avoid circular deps."""
    from mira.dashboard.api import _app_db

    return _app_db


async def run_incremental_index(
    owner: str,
    repo: str,
    fetcher: RepoFetcher,
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
    store = IndexStore.open(owner, repo, platform=platform, allow_fallback=False)

    try:
        run = store.claim_index_run()
        total_affected = len(changed_paths) + len(removed_paths)
        full_reindex = total_affected > _INCREMENTAL_FILE_CAP or run.needs_reconciliation
        ref = default_branch
        tree_paths = None
        try:
            if removed_paths:
                # Resolve the current head after claiming so a newer run can fence its writes.
                ref = await fetcher.branch_head(owner, repo, default_branch)
            if removed_paths or full_reindex:
                tree_paths = await fetcher.repo_tree(owner, repo, ref)
        except (EmptyRepoError, IncompleteTreeError, httpx.HTTPError) as exc:
            logger.warning(
                "Index tree unavailable for %s/%s: %s. Indexing changed paths and leaving recovery pending.",
                owner,
                repo,
                exc,
            )
            await index_diff(
                owner=owner,
                repo=repo,
                config=config,
                store=store,
                llm=llm,
                changed_paths=changed_paths,
                branch=ref,
                fetcher=fetcher,
                run=run,
                platform=platform,
                changes_only=True,
            )
            return

        if full_reindex:
            if run.needs_reconciliation:
                logger.info("Reconciling unfinished index for %s/%s", owner, repo)
            else:
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
                branch=ref,
                fetcher=fetcher,
                run=run,
                platform=platform,
                tree_paths=tree_paths,
            )
        else:
            if tree_paths is not None:
                tree = set(tree_paths)
                candidates = set(removed_paths)
                removed = candidates - tree
                changed_paths = sorted((set(changed_paths) | (candidates & tree)) - removed)
                removed_paths = sorted(removed)
            count = await index_diff(
                owner=owner,
                repo=repo,
                config=config,
                store=store,
                llm=llm,
                changed_paths=changed_paths,
                removed_paths=removed_paths,
                branch=ref,
                fetcher=fetcher,
                run=run,
                platform=platform,
            )

        # A push can supersede a completed manual run before that run gets
        # to publish ready. Refresh status even when the push indexes no files.
        try:
            with store.index_write(run):
                app_db.set_repo_status(
                    owner,
                    repo,
                    "ready",
                    files_indexed=len(store.all_paths()),
                    bump_last_indexed=count > 0 or run.needs_reconciliation,
                    platform=platform,
                )
        except IndexingSuperseded:
            raise
        except Exception as exc:
            logger.warning("Failed to update repo status after push: %s", exc)
        logger.info("Incremental index for %s/%s: %d files", owner, repo, count)
    except IndexingSuperseded:
        logger.info("Indexing superseded for %s/%s", owner, repo)
    except IndexingRecoveryPending as exc:
        logger.warning("Index recovery still pending for %s/%s: %s", owner, repo, exc)
    finally:
        store.close()
