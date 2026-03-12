"""Webhook handlers for indexing events (installation, push)."""

from __future__ import annotations

import logging
import time
from typing import Any

from mira.config import load_config
from mira.github_app.auth import GitHubAppAuth
from mira.github_app.metrics import Metrics
from mira.index.indexer import index_diff, index_repo
from mira.index.store import IndexStore
from mira.llm.provider import LLMProvider

logger = logging.getLogger(__name__)


async def handle_installation(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
    metrics: Metrics | None = None,
) -> None:
    """Handle installation.created — full index all accessible repos."""
    installation_id: int = payload.get("installation", {}).get("id", 0)
    repos = payload.get("repositories", [])
    start = time.monotonic()

    try:
        token = await app_auth.get_installation_token(installation_id)
        config = load_config()
        llm = LLMProvider(config.llm)

        for repo_info in repos:
            full_name = repo_info.get("full_name", "")
            if "/" not in full_name:
                continue
            owner, repo = full_name.split("/", 1)

            try:
                store = IndexStore.open(owner, repo)
                count = await index_repo(
                    owner=owner,
                    repo=repo,
                    token=token,
                    config=config,
                    store=store,
                    llm=llm,
                    full=True,
                )
                store.close()
                logger.info("Full index complete for %s: %d files", full_name, count)
            except Exception as exc:
                logger.warning("Failed to index %s: %s", full_name, exc)

        if metrics:
            metrics.track(
                "index_installation_completed",
                installation_id=installation_id,
                properties={
                    "duration_s": round(time.monotonic() - start, 2),
                    "repos_count": len(repos),
                },
            )
    except Exception:
        logger.exception("Error handling installation indexing")


async def handle_repos_added(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
    metrics: Metrics | None = None,
) -> None:
    """Handle installation_repositories.added — full index newly added repos."""
    installation_id: int = payload.get("installation", {}).get("id", 0)
    repos = payload.get("repositories_added", [])
    start = time.monotonic()

    try:
        token = await app_auth.get_installation_token(installation_id)
        config = load_config()
        llm = LLMProvider(config.llm)

        for repo_info in repos:
            full_name = repo_info.get("full_name", "")
            if "/" not in full_name:
                continue
            owner, repo = full_name.split("/", 1)

            try:
                store = IndexStore.open(owner, repo)
                count = await index_repo(
                    owner=owner,
                    repo=repo,
                    token=token,
                    config=config,
                    store=store,
                    llm=llm,
                    full=True,
                )
                store.close()
                logger.info("Full index complete for %s: %d files", full_name, count)
            except Exception as exc:
                logger.warning("Failed to index %s: %s", full_name, exc)

        if metrics:
            metrics.track(
                "index_repos_added_completed",
                installation_id=installation_id,
                properties={
                    "duration_s": round(time.monotonic() - start, 2),
                    "repos_count": len(repos),
                },
            )
    except Exception:
        logger.exception("Error handling repos_added indexing")


async def handle_push_index(
    payload: dict[str, Any],
    app_auth: GitHubAppAuth,
    bot_name: str,
    metrics: Metrics | None = None,
) -> None:
    """Handle push to default branch — incremental index of changed files."""
    installation_id: int = payload.get("installation", {}).get("id", 0)
    start = time.monotonic()

    try:
        token = await app_auth.get_installation_token(installation_id)

        owner = payload["repository"]["owner"]["login"]
        repo_name = payload["repository"]["name"]
        default_branch = payload.get("repository", {}).get("default_branch", "main")

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
        llm = LLMProvider(config.llm)
        store = IndexStore.open(owner, repo_name)

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

        logger.info("Incremental index for %s/%s: %d files", owner, repo_name, count)

        if metrics:
            metrics.track(
                "index_push_completed",
                installation_id=installation_id,
                properties={
                    "duration_s": round(time.monotonic() - start, 2),
                    "files_changed": len(changed_paths),
                    "files_removed": len(removed_paths),
                    "files_indexed": count,
                },
            )
    except Exception:
        logger.exception("Error handling push indexing")
