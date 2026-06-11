"""Outbound webhook notifications (Slack / Microsoft Teams / generic JSON).

Mira can POST to user-configured endpoints when interesting things happen —
a PR review finishes, a review errors out, a repo finishes indexing. Webhooks
are configured globally on the admin Settings page and stored as a JSON list in
the dashboard ``settings`` table (key ``"webhooks"``).

Delivery is *always* best-effort and fully guarded: a misconfigured or slow
endpoint must never break or delay a review. Every public entry point swallows
its own exceptions and logs instead of propagating.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# ── Event types ──────────────────────────────────────────────────────────────

REVIEW_COMPLETED = "review.completed"
REVIEW_FAILED = "review.failed"
REVIEW_HIGH_SEVERITY = "review.high_severity"
INDEXING_COMPLETED = "indexing.completed"

# Surfaced to the Settings UI so it can render an event picker per webhook.
AVAILABLE_EVENTS: list[dict[str, str]] = [
    {
        "value": REVIEW_COMPLETED,
        "label": "Review completed",
        "description": "After Mira finishes reviewing a pull request.",
    },
    {
        "value": REVIEW_HIGH_SEVERITY,
        "label": "High-severity findings",
        "description": "When a review surfaces a blocker or warning.",
    },
    {
        "value": REVIEW_FAILED,
        "label": "Review failed",
        "description": "When a review errors out instead of completing.",
    },
    {
        "value": INDEXING_COMPLETED,
        "label": "Indexing completed",
        "description": "When a repository finishes indexing.",
    },
]
_EVENT_VALUES = {e["value"] for e in AVAILABLE_EVENTS}

# Network budget per delivery. Short on purpose — a webhook is fire-and-forget.
_TIMEOUT_SECONDS = 5.0


class WebhookConfig(BaseModel):
    """A single configured outbound webhook."""

    id: str = ""
    name: str = ""
    url: str
    events: list[str] = Field(default_factory=lambda: [REVIEW_COMPLETED])
    enabled: bool = True

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v

    @field_validator("events")
    @classmethod
    def _validate_events(cls, v: list[str]) -> list[str]:
        bad = [e for e in v if e not in _EVENT_VALUES]
        if bad:
            raise ValueError(f"Unknown event(s): {bad}. Allowed: {sorted(_EVENT_VALUES)}")
        return v


def detect_format(url: str) -> str:
    """Classify a webhook URL as ``"slack"``, ``"teams"`` or ``"generic"``."""
    host = (urlparse(url).hostname or "").lower()
    if host == "hooks.slack.com":
        return "slack"
    # Classic Teams connector (*.webhook.office.com) and the newer Teams
    # Workflows endpoints (Power Automate / Logic Apps, *.logic.azure.com).
    # Match on a leading dot so a look-alike like "evilwebhook.office.com"
    # can't pass via subdomain confusion.
    if (
        host == "webhook.office.com"
        or host.endswith(".webhook.office.com")
        or host.endswith(".logic.azure.com")
    ):
        return "teams"
    return "generic"


def mask_url(url: str) -> str:
    """``https://hooks.slack.com/services/T0/B0/XXXX`` → ``https://hooks.slack.com/…XXXX``."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    scheme = parsed.scheme or "https"
    tail = url[-4:] if len(url) >= 4 else url
    return f"{scheme}://{host}/…{tail}"


# ── Payload rendering ────────────────────────────────────────────────────────


def _message(event: str, data: dict[str, Any]) -> tuple[str, str, str]:
    """Return ``(title, markdown_body, theme_color)`` for an event."""
    repo = data.get("repo", "")
    pr_url = data.get("pr_url", "")
    number = data.get("number")
    pr_ref = f"{repo} #{number}" if number is not None else repo

    if event == REVIEW_COMPLETED:
        comments = data.get("comments", 0)
        issues = data.get("key_issues", 0)
        title = f"✅ Mira reviewed {pr_ref}"
        body = f"*<{pr_url}|{pr_ref}>* — {comments} comment(s), {issues} key issue(s)"
        return title, body, "2EB67D"

    if event == REVIEW_HIGH_SEVERITY:
        issues = data.get("key_issues", 0)
        sev = data.get("severities") or {}
        breakdown = ", ".join(f"{n} {name}" for name, n in sev.items()) or f"{issues} issue(s)"
        title = f"🛑 Mira found high-severity issues in {pr_ref}"
        body = f"*<{pr_url}|{pr_ref}>* — {breakdown}"
        return title, body, "E01E5A"

    if event == REVIEW_FAILED:
        err = data.get("error", "unknown error")
        title = f"❌ Mira review failed for {pr_ref}"
        body = f"*<{pr_url}|{pr_ref}>* — review failed: {err}"
        return title, body, "E01E5A"

    if event == INDEXING_COMPLETED:
        files = data.get("files_indexed", 0)
        title = f"📚 Mira finished indexing {repo}"
        body = f"*{repo}* is indexed — {files} file(s) ready for cross-repo context."
        return title, body, "2EB67D"

    # Fallback for unknown events — still deliver something useful.
    return f"Mira: {event}", f"Event `{event}` for {repo or 'Mira'}.", "5B6770"


def render(event: str, data: dict[str, Any], fmt: str) -> dict[str, Any]:
    """Build the HTTP JSON body for ``event`` in the destination ``fmt``."""
    title, body, color = _message(event, data)

    if fmt == "slack":
        # `text` is the notification fallback; blocks render richly in-channel.
        return {
            "text": title,
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n{body}"}}
            ],
        }

    if fmt == "teams":
        # MessageCard renders on both classic connectors and Workflows.
        return {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "summary": title,
            "themeColor": color,
            "title": title,
            "text": body,
        }

    # generic: a stable, self-describing JSON envelope.
    return {"event": event, "timestamp": time.time(), "data": data}


# ── Delivery ─────────────────────────────────────────────────────────────────


def is_safe_webhook_url(url: str) -> bool:
    """Best-effort SSRF guard run before delivery.

    Rejects ``localhost`` and any private / loopback / link-local / reserved IP
    *literal* so an admin can't point a webhook at internal services or a cloud
    metadata endpoint (e.g. ``169.254.169.254``). Hostnames are intentionally
    not resolved — named internal services (e.g. a docker-compose ``mattermost``
    host) stay usable on self-hosted deployments; this only blocks the obvious
    raw-IP probe vectors.
    """
    host = (urlparse(url).hostname or "").lower()
    if not host or host == "localhost":
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # not an IP literal — a hostname; allow without resolving
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def deliver_one(
    webhook: dict[str, Any], event: str, data: dict[str, Any]
) -> tuple[bool, str]:
    """POST a single event to a single webhook.

    Returns ``(ok, detail)`` where ``detail`` is a short human string (HTTP
    status or error) suitable for surfacing in the Settings "Send test" UI.
    Never raises.
    """
    url = webhook.get("url", "")
    if not url:
        return False, "missing url"
    if not is_safe_webhook_url(url):
        return False, "URL points to a private or internal address"
    fmt = detect_format(url)
    payload = render(event, data, fmt)

    last_detail = ""
    # One retry: transient network blips and 5xx are worth a second attempt;
    # 4xx is a real misconfiguration, so we stop immediately.
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code < 400:
                return True, f"HTTP {resp.status_code}"
            last_detail = f"HTTP {resp.status_code}"
            if resp.status_code < 500:
                return False, last_detail  # client error — don't retry
        except httpx.HTTPError as exc:
            last_detail = f"{type(exc).__name__}: {exc}"
        if attempt == 1:
            logger.debug("Webhook %s attempt 1 failed (%s); retrying", url, last_detail)
    return False, last_detail or "delivery failed"


async def dispatch_event(event: str, data: dict[str, Any]) -> None:
    """Deliver ``event`` to every enabled webhook subscribed to it.

    Guarded end-to-end: a DB hiccup or a dead endpoint logs a warning and is
    swallowed, so this is safe to ``await`` directly from a review path.
    """
    try:
        # Lazy import: the notifier runs in contexts (CLI/tests) with no DB
        # attached — mirror config.load_config's defensive lookup.
        from mira.dashboard.api import _app_db

        if _app_db is None:
            return
        webhooks = _app_db.get_webhooks()
    except Exception as exc:  # noqa: BLE001
        logger.debug("dispatch_event: no webhooks available (%s)", exc)
        return

    targets = [w for w in webhooks if w.get("enabled", True) and event in (w.get("events") or [])]
    for webhook in targets:
        ok, detail = await deliver_one(webhook, event, data)
        if ok:
            logger.info("Webhook '%s' delivered %s (%s)", webhook.get("name", ""), event, detail)
        else:
            logger.warning(
                "Webhook '%s' failed to deliver %s: %s", webhook.get("name", ""), event, detail
            )


def sample_data(event: str) -> dict[str, Any]:
    """A representative payload used by the Settings 'Send test' button."""
    if event == INDEXING_COMPLETED:
        return {"repo": "octocat/hello-world", "files_indexed": 128}
    if event == REVIEW_FAILED:
        return {
            "repo": "octocat/hello-world",
            "pr_url": "https://github.com/octocat/hello-world/pull/42",
            "number": 42,
            "error": "example error (test delivery)",
        }
    return {
        "repo": "octocat/hello-world",
        "pr_url": "https://github.com/octocat/hello-world/pull/42",
        "number": 42,
        "title": "Add widget",
        "comments": 3,
        "key_issues": 1,
        "severities": {"blocker": 1, "warning": 2},
    }
