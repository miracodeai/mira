"""Webhook handlers for indexing events (installation, push, startup)."""

from __future__ import annotations

import logging
import os
from typing import Any

from mira.config import load_config
from mira.github_app.auth import GitHubAppAuth
from mira.index.indexer import _fetch_repo_tree, _should_index, index_diff, index_repo
from mira.index.store import IndexStore
from mira.llm.provider import LLMProvider

logger = logging.getLogger(__name__)


def _get_app_db():
    """Get the app database instance. Lazy import to avoid circular deps."""
    from mira.dashboard.api import _app_db

    return _app_db


async def _count_files_for_repos(
    app_auth: GitHubAppAuth,
    installation_id: int,
    repos: list[dict],
) -> None:
    """Count indexable files per repo via GitHub API and cache to DB."""
    try:
        token = await app_auth.get_installation_token(installation_id)
    except Exception as exc:
        logger.warning(
            "Cannot count files for installation %s — token fetch failed (%s). "
            "This usually means MIRA_GITHUB_APP_ID/MIRA_GITHUB_PRIVATE_KEY don't "
            "match the installed App, or the installation has been revoked.",
            installation_id,
            exc,
        )
        return

    app_db = _get_app_db()
    for repo_info in repos:
        full_name = repo_info.get("full_name", "")
        if "/" not in full_name:
            continue
        owner, repo = full_name.split("/", 1)
        try:
            tree_paths = await _fetch_repo_tree(owner, repo, token)
            indexable = [p for p in tree_paths if _should_index(p)]
            app_db.set_repo_file_count(owner, repo, len(indexable))
            logger.info("Counted %d indexable files in %s", len(indexable), full_name)
        except Exception as exc:
            logger.warning("Failed to count files for %s: %s", full_name, exc)


async def handle_installation(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
) -> None:
    """Handle installation.created — register all repos (no indexing until user confirms)."""
    installation_id: int = payload.get("installation", {}).get("id", 0)
    repos = payload.get("repositories", [])

    if not repos:
        try:
            repos_from_api = await app_auth.list_installation_repos(installation_id)
            repos = [{"full_name": r.get("full_name", "")} for r in repos_from_api]
        except Exception as exc:
            logger.warning("Failed to list repos via API: %s", exc)

    logger.info("handle_installation: registering %d repos", len(repos))

    try:
        app_db = _get_app_db()
        for repo_info in repos:
            full_name = repo_info.get("full_name", "")
            if "/" not in full_name:
                continue
            owner, repo = full_name.split("/", 1)
            app_db.register_repo(owner, repo, installation_id)
            logger.info("Registered repo %s (pending indexing)", full_name)

        # Count files in background — no LLM, just GitHub API
        import asyncio

        asyncio.create_task(_count_files_for_repos(app_auth, installation_id, repos))

        # Notify connected clients
        from mira.dashboard.events import bus

        bus.emit(
            "install_created",
            {
                "installation_id": installation_id,
                "repos": [r.get("full_name", "") for r in repos],
            },
        )

    except Exception:
        logger.exception("Error registering repos from installation")


async def handle_installation_deleted(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
) -> None:
    """Handle installation.deleted — queue pending uninstall (keep data until user decides)."""
    installation_id: int = payload.get("installation", {}).get("id", 0)
    account = payload.get("installation", {}).get("account", {})
    owner = str(account.get("login", "unknown"))
    logger.info(
        "handle_installation_deleted: queuing uninstall for installation %d (%s)",
        installation_id,
        owner,
    )

    try:
        app_db = _get_app_db()
        app_db.add_pending_uninstall(installation_id, owner)

        from mira.dashboard.events import bus

        bus.emit("uninstall_pending", {"installation_id": installation_id, "owner": owner})
    except Exception:
        logger.exception("Error handling installation.deleted")


async def handle_repos_removed(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
) -> None:
    """Handle installation_repositories.removed — remove specific repos."""
    repos = payload.get("repositories_removed", [])
    logger.info("handle_repos_removed: removing %d repos", len(repos))

    try:
        app_db = _get_app_db()
        for repo_info in repos:
            full_name = repo_info.get("full_name", "")
            if "/" not in full_name:
                continue
            owner, repo = full_name.split("/", 1)
            app_db.delete_repo(owner, repo)
            logger.info("Removed repo %s", full_name)
    except Exception:
        logger.exception("Error handling repos_removed")


async def handle_repos_added(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
) -> None:
    """Handle installation_repositories.added — register newly added repos."""
    installation_id: int = payload.get("installation", {}).get("id", 0)
    repos = payload.get("repositories_added", [])

    logger.info("handle_repos_added: registering %d repos", len(repos))

    try:
        app_db = _get_app_db()
        for repo_info in repos:
            full_name = repo_info.get("full_name", "")
            if "/" not in full_name:
                continue
            owner, repo = full_name.split("/", 1)
            app_db.register_repo(owner, repo, installation_id)
            logger.info("Registered repo %s (pending indexing)", full_name)

        import asyncio

        asyncio.create_task(_count_files_for_repos(app_auth, installation_id, repos))

        from mira.dashboard.events import bus

        bus.emit(
            "repos_added",
            {
                "installation_id": installation_id,
                "repos": [r.get("full_name", "") for r in repos],
            },
        )
    except Exception:
        logger.exception("Error registering repos_added")


_INCREMENTAL_FILE_CAP = 50  # Above this, queue a full re-index instead


async def handle_push_index(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
) -> None:
    """Handle push to default branch — incremental index of changed files.

    If the push touches more than _INCREMENTAL_FILE_CAP files (e.g. a large
    rebase), queues a full re-index instead of inline incremental updates.
    """
    installation_id: int = payload.get("installation", {}).get("id", 0)

    try:
        token = await app_auth.get_installation_token(installation_id)

        owner = payload["repository"]["owner"]["login"]
        repo_name = payload["repository"]["name"]
        default_branch = payload.get("repository", {}).get("default_branch", "main")

        # Check repo status — only re-index repos that are already indexed
        app_db = _get_app_db()
        repo_record = app_db.get_repo(owner, repo_name)
        if not repo_record or repo_record.status not in ("ready", "indexing"):
            logger.debug(
                "Push to %s/%s skipped — repo status is %s",
                owner,
                repo_name,
                repo_record.status if repo_record else "not registered",
            )
            return

        # Extract changed and removed paths from commits
        changed_paths: set[str] = set()
        removed_paths: set[str] = set()
        for commit in payload.get("commits", []):
            changed_paths.update(commit.get("added", []))
            changed_paths.update(commit.get("modified", []))
            removed_paths.update(commit.get("removed", []))

        # Files that were removed shouldn't be re-indexed
        changed_paths -= removed_paths

        if not changed_paths and not removed_paths:
            logger.debug("Push to %s/%s had no file changes", owner, repo_name)
            return

        config = load_config()

        # Use the configured indexing model (not the default review model)
        from mira.dashboard.models_config import llm_config_for

        llm = LLMProvider(llm_config_for("indexing", config.llm))
        store = IndexStore.open(owner, repo_name)

        # If too many files changed (large rebase/squash), do a full re-index
        total_affected = len(changed_paths) + len(removed_paths)
        if total_affected > _INCREMENTAL_FILE_CAP:
            logger.info(
                "Push to %s/%s touched %d files (cap=%d), running full re-index",
                owner,
                repo_name,
                total_affected,
                _INCREMENTAL_FILE_CAP,
            )
            count = await index_repo(
                owner=owner,
                repo=repo_name,
                token=token,
                config=config,
                store=store,
                llm=llm,
                branch=default_branch,
            )
        else:
            count = await index_diff(
                owner=owner,
                repo=repo_name,
                token=token,
                config=config,
                store=store,
                llm=llm,
                changed_paths=list(changed_paths),
                removed_paths=list(removed_paths),
                branch=default_branch,
            )

        store.close()

        # Bump the repo's updated_at + files_indexed so the dashboard's
        # "last indexed" timestamp reflects the push. Skip if nothing changed.
        if count > 0:
            try:
                app_db.set_repo_status(owner, repo_name, "ready", files_indexed=count)
            except Exception as exc:
                logger.warning("Failed to update repo status after push: %s", exc)

        logger.info("Incremental index for %s/%s: %d files", owner, repo_name, count)

    except Exception:
        logger.exception("Error handling push indexing")


def _index_is_populated(owner: str, repo: str) -> bool:
    """Check whether a repo has a non-empty index."""
    index_dir = os.environ.get("MIRA_INDEX_DIR", "/data/indexes")
    db_path = os.path.join(index_dir, owner, f"{repo}.db")
    if not os.path.isfile(db_path):
        return False
    # DB file exists but might be empty (created then interrupted)
    try:
        store = IndexStore(db_path)
        has_files = len(store.all_paths()) > 0
        store.close()
        return has_files
    except Exception:
        return False


def _reconcile_repo_statuses() -> None:
    """Heal any 'indexing' rows left over from a crashed/restarted indexing job.

    If the actual IndexStore has files for a repo whose row says 'indexing',
    promote the row to 'ready' with the real file count. If the store is
    empty, demote to 'pending' so the user can retry from the dashboard.
    """
    app_db = _get_app_db()
    for r in app_db.list_repos():
        if r.status != "indexing":
            continue
        try:
            store = IndexStore.open(r.owner, r.repo)
            count = len(store.all_paths())
            store.close()
        except Exception:
            count = 0
        if count > 0:
            app_db.set_repo_status(r.owner, r.repo, "ready", files_indexed=count)
            logger.info("Reconciled %s/%s: indexing → ready (%d files)", r.owner, r.repo, count)
        else:
            app_db.set_repo_status(r.owner, r.repo, "pending")
            logger.info("Reconciled %s/%s: indexing → pending (no files)", r.owner, r.repo)


async def backfill_missing_indexes(
    app_auth: GitHubAppAuth,
) -> None:
    """Register all repos from GitHub App installations.

    Called at server startup. Only registers repos — does not start indexing.
    Indexing is user-initiated via the setup page.
    """
    try:
        # Reconcile any stale 'indexing' rows left over from a previous run
        # that crashed or was restarted mid-flight.
        _reconcile_repo_statuses()

        installations = await app_auth.list_installations()
        logger.info("Startup: found %d installation(s)", len(installations))

        app_db = _get_app_db()
        registered = 0

        for inst in installations:
            raw_id = inst.get("id", 0)
            installation_id = int(raw_id) if isinstance(raw_id, (int, str)) else 0
            if not installation_id:
                continue

            try:
                repos = await app_auth.list_installation_repos(installation_id)
            except Exception as exc:
                logger.warning("Failed to list repos for installation %d: %s", installation_id, exc)
                continue

            for repo_info in repos:
                full_name = str(repo_info.get("full_name", ""))
                if "/" not in full_name:
                    continue
                owner, repo = full_name.split("/", 1)
                app_db.register_repo(owner, repo, installation_id)
                registered += 1

            # Count files in background
            import asyncio

            asyncio.create_task(_count_files_for_repos(app_auth, installation_id, repos))

        logger.info("Startup: registered %d repo(s)", registered)
    except Exception:
        logger.exception("Error during startup repo registration")
