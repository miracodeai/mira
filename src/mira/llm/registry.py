"""Supported-model registry — single source of truth for capabilities and pricing.

Backed by ``models.json``. Adding a new model is a one-line registry entry
plus a release note; no other code needs to change. To deny a model entirely,
remove its entry — the dashboard validation and dropdown derive from this file.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_REGISTRY_PATH = Path(__file__).parent / "models.json"


@lru_cache(maxsize=1)
def _load() -> dict[str, dict]:
    """Load the registry once per process, dropping the leading ``_*`` docs."""
    raw = json.loads(_REGISTRY_PATH.read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


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

    Falls back to Sonnet pricing for unknown models so cost estimates aren't
    silently zero.
    """
    info = get(model_id)
    if info is None:
        return (3.00, 15.00)
    return (float(info["input_cost_per_1m"]), float(info["output_cost_per_1m"]))
