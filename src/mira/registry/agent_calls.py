"""Ingest the relay's agent Linear-call report into the registry.

The dual-write exit needs three consecutive UTC days with no agent Linear
calls. The relay counts those calls and serves the report at
``GET /api/linear/agent-calls``. This module copies that report. It does
not call Linear.

Run ``python -m mira.registry.agent_calls ingest --url <relay report>``.
That is a dry run unless ``--apply`` is given. ``--apply`` writes one
snapshot row in the registry database.

An unhealthy report is stored as not meeting the exit, and the previously
stored days are left as they were. A gap the relay could not read is not
recorded as a quiet day.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

# The relay keeps this many days and rejects a larger ``days`` query.
WINDOW_DAYS = 45
# Today is still open, so the exit looks at the days before it.
EXIT_ZERO_DAYS = 3
REPORT_PATH = "/api/linear/agent-calls"
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CALLER = re.compile(r"^[\w.:-]{1,120}$")
_SNAPSHOT_ID = 1


class AgentCallReportError(ValueError):
    """The relay report is missing, malformed, or inconsistent."""


def shift_day(day: str, delta: int) -> str:
    """Move a ``YYYY-MM-DD`` date by calendar days."""
    return (date.fromisoformat(day) + timedelta(days=delta)).isoformat()


def report_url(base: str) -> str:
    """The relay report URL, always asking for the full retained window."""
    parsed = urlsplit(base.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise AgentCallReportError("report url must be an http(s) relay url")
    if parsed.path.rstrip("/") != REPORT_PATH:
        raise AgentCallReportError("report url must be the relay agent-calls route")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["days"] = str(WINDOW_DAYS)
    return urlunsplit((parsed.scheme, parsed.netloc, REPORT_PATH, urlencode(query), ""))


def _day(value: Any, field: str) -> str:
    if not isinstance(value, str) or _DAY.fullmatch(value) is None:
        raise AgentCallReportError(f"{field} must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise AgentCallReportError(f"{field} is not a calendar day") from exc
    return value


def _whole(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AgentCallReportError(f"{field} must be an integer")
    if value < 0:
        raise AgentCallReportError(f"{field} must be zero or more")
    return value


def _callers(value: Any, total: int) -> dict[str, int]:
    if not isinstance(value, dict):
        raise AgentCallReportError("byCaller must be an object")
    out: dict[str, int] = {}
    for key, count in value.items():
        if not isinstance(key, str) or _CALLER.fullmatch(key) is None:
            raise AgentCallReportError("byCaller has a bad caller name")
        out[key] = _whole(count, f"byCaller.{key}")
    if sum(out.values()) != total:
        raise AgentCallReportError("byCaller does not add up to the day's total")
    return dict(sorted(out.items()))


def consecutive_zero_days(days: list[dict], tracked_since: str | None) -> int:
    """Quiet days ending yesterday. Today is open. ``tracked_since`` is excluded.

    This is the relay's rule: walk the returned days after today, and stop
    on a non-zero total or on the day tracking began.
    """
    quiet = 0
    for row in days[1:]:
        if row["total"] != 0 or not tracked_since or row["day"] <= tracked_since:
            break
        quiet += 1
    return quiet


def parse_report(payload: Any) -> dict:
    """Check the relay's report and return a normalized copy of it."""
    if not isinstance(payload, dict):
        raise AgentCallReportError("report must be a JSON object")
    healthy = payload.get("healthy")
    if not isinstance(healthy, bool):
        raise AgentCallReportError("healthy must be a boolean")
    reason = payload.get("reason")
    if healthy and reason is not None:
        raise AgentCallReportError("a healthy report has no reason")
    if not healthy and not isinstance(reason, str):
        raise AgentCallReportError("an unhealthy report needs a reason")
    raw_days = payload.get("days")
    if not isinstance(raw_days, list) or not 1 <= len(raw_days) <= WINDOW_DAYS:
        raise AgentCallReportError(f"days must be 1 to {WINDOW_DAYS} rows")
    days: list[dict] = []
    for index, raw in enumerate(raw_days):
        if not isinstance(raw, dict):
            raise AgentCallReportError("each day must be an object")
        day = _day(raw.get("day"), "day")
        if index and day != shift_day(days[0]["day"], -index):
            raise AgentCallReportError("days must be contiguous, newest first")
        total = _whole(raw.get("total"), "total")
        days.append(
            {
                "day": day,
                "total": total,
                "byCaller": _callers(raw.get("byCaller"), total),
                "sessions": _whole(raw.get("sessions"), "sessions"),
            }
        )
    tracked = payload.get("trackedSince")
    tracked_since = None if tracked is None else _day(tracked, "trackedSince")
    streak = _whole(payload.get("consecutiveZeroDays"), "consecutiveZeroDays")
    if not healthy and streak != 0:
        raise AgentCallReportError("an unhealthy report cannot claim quiet days")
    if healthy and streak != consecutive_zero_days(days, tracked_since):
        raise AgentCallReportError("consecutiveZeroDays does not match the days")
    return {
        "healthy": healthy,
        "reason": None if healthy else reason,
        "consecutiveZeroDays": streak,
        "trackedSince": tracked_since,
        "days": days,
    }


def exit_met(healthy: bool, consecutive: int) -> bool:
    """True when the exit's three quiet days are fully observed."""
    return healthy and consecutive >= EXIT_ZERO_DAYS


def fetch_report(url: str, *, client: httpx.Client | None = None) -> Any:
    """GET the report. The caller supplies a client in tests."""
    own = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        response = http.get(url)
        response.raise_for_status()
        try:
            return response.json()
        except ValueError as exc:
            raise AgentCallReportError("report was not JSON") from exc
    finally:
        if own:
            http.close()


class AgentCallIngest:
    """Copy one relay report into ``registry_agent_call_snapshot``."""

    def __init__(
        self,
        db: Any = None,
        *,
        clock: Callable[[], float] = time.time,
        fetch: Callable[..., Any] = fetch_report,
    ) -> None:
        self.db = db
        self.clock = clock
        self.fetch = fetch

    def ingest(self, url: str, *, apply: bool = False) -> dict:
        report = parse_report(self.fetch(report_url(url)))
        summary = {
            "apply": apply,
            "healthy": report["healthy"],
            "reason": report["reason"],
            "consecutiveZeroDays": report["consecutiveZeroDays"],
            "trackedSince": report["trackedSince"],
            "exitMet": exit_met(report["healthy"], report["consecutiveZeroDays"]),
            "days": len(report["days"]),
            "applied": False,
        }
        if not apply:
            return summary
        if self.db is None:
            raise AgentCallReportError("no registry to write")
        days_json = json.dumps(report["days"], separators=(",", ":"), sort_keys=True)
        # An unhealthy reading must not replace the stored days with a
        # window of apparent zeros. The first reading stores an empty window.
        if not report["healthy"]:
            days_json = "[]"
        self.db.execute(
            "INSERT INTO registry_agent_call_snapshot (id, healthy, reason, "
            "consecutive_zero_days, tracked_since, days_json, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO UPDATE SET "
            "healthy = excluded.healthy, reason = excluded.reason, "
            "consecutive_zero_days = excluded.consecutive_zero_days, "
            "tracked_since = excluded.tracked_since, "
            "days_json = CASE WHEN excluded.healthy = 1 THEN excluded.days_json "
            "ELSE registry_agent_call_snapshot.days_json END, "
            "ingested_at = excluded.ingested_at",
            (
                _SNAPSHOT_ID,
                1 if report["healthy"] else 0,
                report["reason"] or "",
                report["consecutiveZeroDays"],
                report["trackedSince"] or "",
                days_json,
                self.clock(),
            ),
        )
        summary["applied"] = True
        return summary


def read_snapshot(db: Any) -> dict | None:
    """The stored report, or None when nothing has been applied."""
    rows = db.execute(
        "SELECT healthy, reason, consecutive_zero_days, tracked_since, days_json, ingested_at "
        "FROM registry_agent_call_snapshot WHERE id = ?",
        (_SNAPSHOT_ID,),
    )
    if not rows:
        return None
    healthy, reason, streak, tracked, days_json, ingested_at = rows[0]
    return {
        "healthy": bool(healthy),
        "reason": reason or None,
        "consecutiveZeroDays": streak,
        "trackedSince": tracked or None,
        "days": json.loads(days_json),
        "ingestedAt": ingested_at,
        "exitMet": exit_met(bool(healthy), streak),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mira.registry.agent_calls")
    parser.add_argument("command", choices=("ingest",))
    parser.add_argument("--url", required=True, help="relay GET /api/linear/agent-calls URL")
    parser.add_argument("--apply", action="store_true", help="write the snapshot into the registry")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    db = None
    if args.apply:
        from mira.registry import get_registry

        registry = get_registry()
        assert registry is not None
        db = registry.db
    try:
        out = AgentCallIngest(db, fetch=fetch_report).ingest(args.url, apply=args.apply)
    except AgentCallReportError as exc:
        logger.error("%s", exc)
        return 1
    except httpx.HTTPError:
        logger.exception("agent-call ingest failed")
        return 1
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
