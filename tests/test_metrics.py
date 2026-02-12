"""Tests for PostHog metrics wrapper."""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

from mira.github_app.metrics import Metrics


def test_noop_when_no_api_key() -> None:
    """track() and shutdown() don't raise when no API key is set."""
    m = Metrics()
    m.track("test_event", installation_id=123)
    m.shutdown()


def test_track_calls_posthog_capture() -> None:
    """track() calls capture on the PostHog client."""
    m = Metrics()
    m._client = MagicMock()

    m.track("webhook_received", installation_id=42, properties={"event_type": "pull_request"})

    m._client.capture.assert_called_once_with(
        distinct_id=hashlib.sha256(b"42").hexdigest(),
        event="webhook_received",
        properties={"event_type": "pull_request"},
    )


def test_distinct_id_is_hashed() -> None:
    """distinct_id is a SHA-256 hex digest, not the raw installation_id."""
    m = Metrics()
    m._client = MagicMock()

    m.track("test_event", installation_id=12345)

    call_kwargs = m._client.capture.call_args[1]
    expected = hashlib.sha256(b"12345").hexdigest()
    assert call_kwargs["distinct_id"] == expected
    assert call_kwargs["distinct_id"] != "12345"


def test_shutdown_calls_posthog_shutdown() -> None:
    """shutdown() flushes the PostHog client."""
    m = Metrics()
    m._client = MagicMock()

    m.shutdown()

    m._client.shutdown.assert_called_once()
