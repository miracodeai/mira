"""Auto-review destination-branch policy, shared across platforms.

Mirrors ``author_is_filtered``: a dispatcher-level gate that decides whether
an incoming PR/MR event should schedule an automatic review. Author filters
answer "who"; these answer "where to" — a deployment that only wants reviews
landing on integration branches (``dev*``, ``staging``) can skip everything
else without asking contributors to remember a manual command.
"""

from __future__ import annotations

import fnmatch
import json
import logging

logger = logging.getLogger(__name__)

# JSON blob shapes stored in repos.base_branch_policy.
MODE_INHERIT = "inherit"
MODE_OVERRIDE = "override"
MODE_DISABLE = "disable"


def _policy_from_json(raw: str | None) -> dict | None:
    """Parse the per-repo policy column; ``None`` when unset or unreadable.

    An unreadable column (bad JSON, wrong shape) logs and resolves to
    "unset": the dispatcher then applies the deployment default rather than
    silently disabling or enabling reviews based on corrupt data.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Unparseable base_branch_policy column; using deployment default")
        return None
    if not isinstance(data, dict) or data.get("mode") not in (
        MODE_INHERIT,
        MODE_OVERRIDE,
        MODE_DISABLE,
    ):
        logger.warning("Malformed base_branch_policy column; using deployment default")
        return None
    return data


def validate_patterns(patterns: object) -> list[str]:
    """Reject branch-pattern mistakes rather than silently allowing everything.

    A typo like ``base_branches: dev*`` (string, not list) or an empty
    pattern would otherwise narrow the glob set to nothing and quietly skip
    every review — or, worse, ``[""]`` matching nothing while looking
    configured.
    """
    if not isinstance(patterns, list) or any(
        not isinstance(p, str) or not p.strip() for p in patterns
    ):
        raise ValueError("Branch patterns must be a list of non-empty strings")
    return patterns


def _configured_patterns(config) -> tuple[list[str], dict[str, list[str]]]:
    """(default patterns, per-repo overrides) from ``filter`` config."""
    cfg = config.filter
    return cfg.allowed_base_branches, getattr(cfg, "allowed_base_branch_overrides", {}) or {}


def resolve_patterns(
    repo_full: str,
    *,
    default_patterns: list[str],
    overrides: dict[str, list[str]],
    repo_policy: dict | None,
) -> list[str] | None:
    """The glob list that applies to this repo, or ``None`` = no restriction.

    Precedence: per-repo dashboard policy ("disable" / "override") →
    config override for ``owner/repo`` → deployment default. Repo keys are
    matched case-insensitively (GitHub is); branch patterns are not.
    """
    if repo_policy:
        mode = repo_policy.get("mode")
        if mode == MODE_DISABLE:
            return []
        if mode == MODE_OVERRIDE:
            return validate_patterns(repo_policy.get("branches"))
    folded = {str(k).casefold(): v for k, v in overrides.items()}
    override = folded.get(repo_full.casefold())
    if override is not None:
        return validate_patterns(override)
    return default_patterns or None


def base_branch_is_filtered(
    repo_full: str,
    base_branch: str,
    config,
    repo_policy_raw: str | None = None,
) -> bool:
    """True when an automatic review must be skipped for this destination branch.

    Follows the author-filter contract: unconfigured means review everything
    (the pre-feature behavior); an explicitly configured list is a whitelist.
    An event with no resolvable base branch is skipped when a policy is
    configured — a missing ref must not bypass the gate — but reviewed when
    no policy exists (nothing to enforce).
    """
    default_patterns, overrides = _configured_patterns(config)
    repo_policy = _policy_from_json(repo_policy_raw)
    patterns = resolve_patterns(
        repo_full,
        default_patterns=default_patterns,
        overrides=overrides,
        repo_policy=repo_policy,
    )
    if patterns is None:
        return False
    if not base_branch:
        return True
    return not any(fnmatch.fnmatchcase(base_branch, p) for p in patterns)


def _repo_policy_json(owner: str, repo: str, platform: str) -> str | None:
    """The per-repo policy column, or ``None`` when no DB is attached.

    Fail-open with a warning: an unreachable app DB already breaks the
    review path downstream (``run_pr_review`` reads repo state from it), so
    refusing the event here would not protect anything — it would only turn
    a DB outage into silent review suppression. The deployment-wide config
    patterns still apply regardless.
    """
    try:
        from mira.dashboard.api import _app_db

        if _app_db is None:
            return None
        record = _app_db.get_repo(owner, repo, platform=platform)
        return record.base_branch_policy if record else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Base-branch policy lookup failed for %s/%s: %s", owner, repo, exc)
        return None


def base_branch_is_filtered_for_repo(
    owner: str,
    repo: str,
    base_branch: str,
    config,
    platform: str = "github",
) -> bool:
    """Dispatcher-facing gate: config + per-repo dashboard policy."""
    return base_branch_is_filtered(
        f"{owner}/{repo}",
        base_branch,
        config,
        _repo_policy_json(owner, repo, platform),
    )
