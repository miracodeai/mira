"""Model registry — labels, capabilities, and pricing for known models.

Backed by ``models.json``. Adding a new model is a one-line registry entry
plus a release note; no other code needs to change. Entries supply the
dashboard's labels, per-purpose lists, Recommended badges, and cost-estimate
pricing, and serve as the dropdown fallback when the backend's live catalog
can't be fetched (see ``dashboard/model_catalog.py``). The registry does not
restrict selection — the dashboard accepts free-form ids, like ``mira.yaml``.

Operators can extend or override the bundled list at runtime by pointing
``MIRA_MODELS_JSON_PATH`` at their own ``models.json`` (e.g. a volume mount):
its entries are overlaid onto the bundled ones by model id, so you can add a
custom model (``deepseek/...``, a local endpoint, …) or tweak an existing one
without forking the package. A missing/invalid override file is ignored with a
warning.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_BUNDLED_PATH = Path(__file__).parent / "models.json"
_OVERRIDE_ENV = "MIRA_MODELS_JSON_PATH"


def _read(path: Path) -> dict[str, dict]:
    """Parse a models.json file, dropping the leading ``_*`` doc keys."""
    raw = json.loads(path.read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


@lru_cache(maxsize=1)
def _load() -> dict[str, dict]:
    """Load the registry once per process.

    Starts from the bundled ``models.json``, then overlays the file at
    ``MIRA_MODELS_JSON_PATH`` (if set) by model id — user entries add new models
    or override bundled ones. The env var is read on first use (after process
    startup), so a container's environment is in effect.
    """
    models = _read(_BUNDLED_PATH)
    override = os.environ.get(_OVERRIDE_ENV)
    if override:
        path = Path(override)
        try:
            models = {**models, **_read(path)}
            logger.info("Loaded model overrides from %s (%s)", _OVERRIDE_ENV, path)
        except FileNotFoundError:
            logger.warning("%s=%s not found; using bundled models only", _OVERRIDE_ENV, path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "Failed to read %s=%s (%s); using bundled models only",
                _OVERRIDE_ENV,
                path,
                exc,
            )
    return models


def all_models() -> dict[str, dict]:
    """Return the full registry as ``{model_id: info}``."""
    return _load()


def get(model_id: str) -> dict | None:
    """Return registry entry for ``model_id``, or None if unsupported."""
    return _load().get(model_id)


def is_supported(model_id: str, purpose: str | None = None) -> bool:
    """``True`` iff ``model_id`` is in the registry. If ``purpose`` is given,
    additionally require that the model is allowed for that purpose
    (``"indexing"`` or ``"review"``)."""
    info = get(model_id)
    if info is None:
        return False
    if purpose is None:
        return True
    return purpose in (info.get("purposes") or [])


def models_for_purpose(purpose: str) -> list[dict]:
    """Return all models allowed for ``purpose``, formatted for the
    dashboard dropdown: ``[{value, label, recommended}]``."""
    out: list[dict] = []
    for model_id, info in _load().items():
        if purpose not in (info.get("purposes") or []):
            continue
        out.append(
            {
                "value": model_id,
                "label": info.get("label", model_id),
                "recommended": purpose in (info.get("recommended_for") or []),
            }
        )
    # Recommended first, then alphabetical.
    out.sort(key=lambda m: (not m["recommended"], m["label"].lower()))
    return out


def max_output_tokens(model_id: str, default: int = 4096) -> int:
    """Return ``max_output_tokens`` for the model, or ``default`` if unknown."""
    info = get(model_id)
    if info is None:
        return default
    return int(info.get("max_output_tokens", default))


def pricing(model_id: str) -> tuple[float, float]:
    """Return ``(input_cost_per_1m, output_cost_per_1m)`` USD for the model.

    Falls back to Sonnet pricing for unknown models — and for any cost field a
    (possibly user-supplied) entry omits — so cost estimates aren't silently
    zero and a partial custom entry can't crash registry load.
    """
    info = get(model_id) or {}
    return (
        float(info.get("input_cost_per_1m", 3.00)),
        float(info.get("output_cost_per_1m", 15.00)),
    )
