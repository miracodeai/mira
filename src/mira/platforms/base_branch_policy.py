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

from mira.config import MiraConfig

logger = logging.getLogger(__name__)

# JSON blob shapes stored in repos.base_branch_policy.
MODE_INHERIT = "inherit"
MODE_OVERRIDE = "override"
MODE_DISABLE = "disable"


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
    return [p.strip() for p in patterns]


def parse_repo_policy(raw: str | None) -> dict | None:
    """Parse and fully validate the stored per-repo policy; ``None`` when unusable.

    One parser shared by the dispatcher and the dashboard, so a stored blob
    is judged identically everywhere. Anything unreadable or malformed —
    bad JSON, an unknown mode, an override with missing/blank patterns —
    logs and resolves to ``None``: the dispatcher then applies the
    deployment default rather than crashing on corrupt data.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("policy must be a JSON object")
        mode = data.get("mode")
        if mode not in (MODE_INHERIT, MODE_OVERRIDE, MODE_DISABLE):
            raise ValueError(f"unknown mode {mode!r}")
        if mode == MODE_OVERRIDE:
            data["branches"] = validate_patterns(data.get("branches"))
        elif data.get("branches"):
            # Inherit/disable ignore branches entirely; drop stale values
            # so the stored blob stays canonical.
            data["branches"] = []
        return data
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "Malformed base_branch_policy column %r (%s); using deployment default", raw, exc
        )
        return None


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
            # parse_repo_policy already validated the branches.
            return repo_policy["branches"]
    folded = {str(k).casefold(): v for k, v in overrides.items()}
    override = folded.get(repo_full.casefold())
    if override is not None:
        return override
    return default_patterns or None


def base_branch_is_filtered(
    repo_full: str,
    base_branch: str,
    config: MiraConfig,
    repo_policy_raw: str | None = None,
) -> bool:
    """True when an automatic review must be skipped for this destination branch.

    Follows the author-filter contract: unconfigured means review everything
    (the pre-feature behavior); an explicitly configured list is a whitelist.
    An event with no resolvable base branch is skipped when a policy is
    configured — a missing ref must not bypass the gate — but reviewed when
    no policy exists (nothing to enforce).
    """
    cfg = config.filter
    patterns = resolve_patterns(
        repo_full,
        default_patterns=cfg.allowed_base_branches,
        overrides=cfg.allowed_base_branch_overrides,
        repo_policy=parse_repo_policy(repo_policy_raw),
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
    config: MiraConfig,
    platform: str = "github",
) -> bool:
    """Dispatcher-facing gate: config + per-repo dashboard policy."""
    return base_branch_is_filtered(
        f"{owner}/{repo}",
        base_branch,
        config,
        _repo_policy_json(owner, repo, platform),
    )
