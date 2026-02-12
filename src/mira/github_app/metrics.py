"""Thin PostHog wrapper for anonymous usage metrics."""

from __future__ import annotations

import hashlib
from typing import Any


class Metrics:
    """Anonymous usage metrics via PostHog.

    If api_key is None, all methods are silent no-ops and posthog is never imported.
    """

    def __init__(self, api_key: str | None = None, host: str | None = None) -> None:
        self._client: Any = None
        if api_key:
            from posthog import Posthog

            self._client = Posthog(api_key, host=host or "https://us.i.posthog.com")

    def track(
        self, event: str, installation_id: int, properties: dict[str, Any] | None = None
    ) -> None:
        """Track an anonymous event. No-op if metrics are disabled."""
        if self._client is None:
            return
        distinct_id = hashlib.sha256(str(installation_id).encode()).hexdigest()
        self._client.capture(distinct_id=distinct_id, event=event, properties=properties or {})

    def shutdown(self) -> None:
        """Flush pending events and shut down. No-op if metrics are disabled."""
        if self._client is None:
            return
        self._client.shutdown()
