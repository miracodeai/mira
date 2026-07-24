"""Provider-profile registry — per-provider quirks for the OpenAI-compatible client.

Backed by ``providers.json``. A profile captures what Mira used to special-case
for OpenRouter (attribution headers, model-prefix policy, reasoning remapping)
as plain data, so adding a provider is a one-line registry entry rather than a
code branch. Profiles are matched to a request by ``base_url``; an endpoint with
no matching profile gets ``DEFAULT_PROFILE`` (portable OpenAI-compatible shape).

Operators can extend or override the bundled list at runtime by pointing
``MIRA_PROVIDERS_JSON_PATH`` at their own ``providers.json`` — same idiom as
``MIRA_MODELS_JSON_PATH`` for the model registry.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_BUNDLED_PATH = Path(__file__).parent / "providers.json"
_OVERRIDE_ENV = "MIRA_PROVIDERS_JSON_PATH"

# The portable fallback for any endpoint without a profile: bare model name,
# no attribution headers, no reasoning remap.
DEFAULT_PROFILE: dict = {
    "name": "",
    "model_prefix": "strip",
    "extra_headers": {},
    "reasoning_effort_map": {},
    "api_key_env": None,
}


def _read(path: Path) -> dict[str, dict]:
    """Parse a providers.json file, dropping the leading ``_*`` doc keys."""
    raw = json.loads(path.read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


@lru_cache(maxsize=1)
def _load() -> dict[str, dict]:
    """Load the registry once per process, overlaying any runtime override."""
    profiles = _read(_BUNDLED_PATH)
    override = os.environ.get(_OVERRIDE_ENV)
    if override:
        path = Path(override)
        try:
            profiles = {**profiles, **_read(path)}
            logger.info("Loaded provider overrides from %s (%s)", _OVERRIDE_ENV, path)
        except FileNotFoundError:
            logger.warning("%s=%s not found; using bundled providers only", _OVERRIDE_ENV, path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Failed to read %s=%s (%s); using bundled providers only",
                _OVERRIDE_ENV,
                path,
                exc,
            )
    return profiles


def all_profiles() -> dict[str, dict]:
    """Return the full registry as ``{name: profile}``."""
    return _load()


def get(name: str) -> dict | None:
    """Return the profile named ``name``, or None."""
    profile = _load().get(name)
    return {**profile, "name": name} if profile else None


def _norm(url: str) -> str:
    return url.rstrip("/")


def resolve(base_url: str) -> dict:
    """Return the profile whose ``base_url`` matches, or ``DEFAULT_PROFILE``.

    Matched profiles are merged onto the default so callers can read every
    field (``model_prefix``, ``extra_headers``, …) without per-key guards.
    """
    target = _norm(base_url)
    for name, profile in _load().items():
        if _norm(profile.get("base_url", "")) == target:
            return {**DEFAULT_PROFILE, **profile, "name": name}
    return dict(DEFAULT_PROFILE)
