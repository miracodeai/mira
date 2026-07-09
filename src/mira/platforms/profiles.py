"""Platform-profile registry — per-platform wire data for git hosts.

Backed by ``profiles.json``. A profile captures what Mira used to special-case
for GitHub (API urls, webhook signature scheme, event header, terminology) as
plain data, so adding a platform is a registry entry plus a provider class
rather than scattered ``if platform == "github"`` branches. Resolved by name.

Operators can extend or override the bundled list at runtime by pointing
``MIRA_PLATFORMS_JSON_PATH`` at their own ``profiles.json`` — same idiom as
``MIRA_PROVIDERS_JSON_PATH`` / ``MIRA_MODELS_JSON_PATH``.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_BUNDLED_PATH = Path(__file__).parent / "profiles.json"
_OVERRIDE_ENV = "MIRA_PLATFORMS_JSON_PATH"

# Fields every resolved profile carries, so callers never guard on missing keys.
DEFAULT_PROFILE: dict = {
    "name": "",
    "api_url": "",
    "graphql_url": None,
    "webhook_route": "",
    "auth_model": "token",
    "signature": {},
    "event_header": "",
    "terminology": {"pr_short": "PR"},
    "supports": {},
}


def _read(path: Path) -> dict[str, dict]:
    """Parse a profiles.json file, dropping the leading ``_*`` doc keys."""
    raw = json.loads(path.read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


@lru_cache(maxsize=1)
def _load() -> dict[str, dict]:
    """Load the registry once per process, overlaying any runtime override.

    GitHub's api/graphql urls are seeded from MIRA_GITHUB_API_URL /
    MIRA_GITHUB_GRAPHQL_URL and GitLab's from MIRA_GITLAB_API_URL, so existing
    GitHub Enterprise and self-managed GitLab deployments work without editing
    the JSON.
    """
    profiles = _read(_BUNDLED_PATH)
    override = os.environ.get(_OVERRIDE_ENV)
    if override:
        path = Path(override)
        try:
            profiles = {**profiles, **_read(path)}
            logger.info("Loaded platform overrides from %s (%s)", _OVERRIDE_ENV, path)
        except FileNotFoundError:
            logger.warning("%s=%s not found; using bundled platforms only", _OVERRIDE_ENV, path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Failed to read %s=%s (%s); using bundled platforms only",
                _OVERRIDE_ENV,
                path,
                exc,
            )

    gh = profiles.get("github")
    if gh:
        api = os.environ.get("MIRA_GITHUB_API_URL")
        if api:
            gh["api_url"] = api.rstrip("/")
            gh["graphql_url"] = os.environ.get(
                "MIRA_GITHUB_GRAPHQL_URL", f"{gh['api_url']}/graphql"
            )
    gl = profiles.get("gitlab")
    if gl:
        api = os.environ.get("MIRA_GITLAB_API_URL")
        if api:
            gl["api_url"] = api.rstrip("/")
    return profiles


def all_profiles() -> dict[str, dict]:
    """Return the full registry as ``{name: profile}``."""
    return _load()


def get(name: str) -> dict | None:
    """Return the profile named ``name`` (with ``name`` injected), or None."""
    profile = _load().get(name)
    return {**DEFAULT_PROFILE, **profile, "name": name} if profile else None


def resolve(name: str) -> dict:
    """Return the named profile merged onto the default.

    Unknown names get the bare default with ``name`` set — callers should pass a
    registered platform, but this never KeyErrors.
    """
    return get(name) or {**DEFAULT_PROFILE, "name": name}
