"""Per-PR state Bitbucket Cloud can't hold for us.

Bitbucket pull requests have no labels, so the ``mira-paused`` label other
platforms put on the PR is kept in the app database instead. The same record
remembers the last source commit seen per PR: Bitbucket's
``pullrequest:updated`` fires for title/description edits as well as new
commits, and comparing the head hash is how we tell them apart.

Stored as one JSON settings row per PR, keyed ``bitbucket:pr:<owner>/<repo>#<n>``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _db() -> Any:
    from mira.dashboard.api import _app_db

    return _app_db


def _key(owner: str, repo: str, number: int) -> str:
    return f"bitbucket:pr:{owner}/{repo}#{number}"


def _load(owner: str, repo: str, number: int) -> dict[str, Any]:
    try:
        raw = _db().get_setting(_key(owner, repo, number))
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 - state is best-effort
        logger.debug("Failed to load Bitbucket PR state: %s", exc)
        return {}


def _save(owner: str, repo: str, number: int, data: dict[str, Any]) -> None:
    _db().set_setting(_key(owner, repo, number), json.dumps(data, sort_keys=True))


def get_labels(owner: str, repo: str, number: int) -> list[str]:
    labels = _load(owner, repo, number).get("labels") or []
    return [str(lbl) for lbl in labels]


def add_label(owner: str, repo: str, number: int, label: str) -> None:
    data = _load(owner, repo, number)
    labels = set(data.get("labels") or [])
    labels.add(label)
    data["labels"] = sorted(labels)
    _save(owner, repo, number, data)


def remove_label(owner: str, repo: str, number: int, label: str) -> None:
    data = _load(owner, repo, number)
    labels = set(data.get("labels") or [])
    labels.discard(label)
    data["labels"] = sorted(labels)
    _save(owner, repo, number, data)


def swap_head(owner: str, repo: str, number: int, head: str) -> str:
    """Record ``head`` as the PR's latest source commit; return the previous one."""
    data = _load(owner, repo, number)
    previous = str(data.get("head") or "")
    if head and head != previous:
        data["head"] = head
        try:
            _save(owner, repo, number, data)
        except Exception as exc:  # noqa: BLE001 - state is best-effort
            logger.debug("Failed to save Bitbucket PR head: %s", exc)
    return previous
