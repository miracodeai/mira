"""The relay agent-call report copies into the registry without calling Linear."""

from __future__ import annotations

import json

import httpx
import pytest

from mira.registry.agent_calls import (
    EXIT_ZERO_DAYS,
    AgentCallIngest,
    AgentCallReportError,
    consecutive_zero_days,
    fetch_report,
    main,
    parse_report,
    read_snapshot,
    report_url,
    shift_day,
)
from mira.registry.db import RegistryDB

TODAY = "2026-10-30"


def window(
    totals: list[int], tracked_since: str, *, healthy: bool = True, reason: str | None = None
) -> dict:
    days = []
    for index, total in enumerate(totals):
        days.append(
            {
                "day": shift_day(TODAY, -index),
                "total": total,
                "byCaller": {} if total == 0 else {"cli.issue": total},
                "sessions": 0 if total == 0 else 1,
                "topSessions": {"sess-1": total} if total else {},
            }
        )
    streak = consecutive_zero_days(
        [{"day": row["day"], "total": row["total"]} for row in days],
        tracked_since,
    )
    return {
        "days": days,
        "consecutiveZeroDays": 0 if not healthy else streak,
        "trackedSince": tracked_since,
        "healthy": healthy,
        "reason": None if healthy else reason,
    }


def quiet_report() -> dict:
    # Today is open and non-zero. The three days before it are quiet.
    return window([2, 0, 0, 0, 4], "2026-10-01")


def test_report_url_asks_for_the_full_window_and_refuses_other_paths():
    url = report_url("https://relay.example/api/linear/agent-calls?days=1&x=1")
    assert url == "https://relay.example/api/linear/agent-calls?days=45&x=1"
    with pytest.raises(AgentCallReportError):
        report_url("https://api.linear.app/graphql")
    with pytest.raises(AgentCallReportError):
        report_url("file:///tmp/report.json")


def test_a_healthy_quiet_report_meets_the_exit():
    report = parse_report(quiet_report())
    assert report["consecutiveZeroDays"] == EXIT_ZERO_DAYS
    assert report["days"][0]["byCaller"] == {"cli.issue": 2}
    assert "topSessions" not in report["days"][0]


def test_the_day_tracking_began_does_not_count():
    report = parse_report(window([0, 0, 0, 0], "2026-10-29"))
    assert report["consecutiveZeroDays"] == 0


def test_a_call_stops_the_quiet_run():
    report = parse_report(window([0, 0, 1, 0], "2026-10-01"))
    assert report["consecutiveZeroDays"] == 1


def test_an_unhealthy_report_cannot_claim_quiet_days():
    payload = window([0, 0, 0, 0], "2026-10-01", healthy=False, reason="kept aside")
    payload["consecutiveZeroDays"] = 3
    with pytest.raises(AgentCallReportError, match="quiet days"):
        parse_report(payload)


def test_a_streak_that_disagrees_with_the_days_is_refused():
    payload = quiet_report()
    payload["consecutiveZeroDays"] = 9
    with pytest.raises(AgentCallReportError, match="does not match"):
        parse_report(payload)


def test_caller_totals_must_add_up_and_bools_are_not_integers():
    payload = quiet_report()
    payload["days"][0]["byCaller"] = {"cli.issue": 1}
    with pytest.raises(AgentCallReportError, match="add up"):
        parse_report(payload)
    payload = quiet_report()
    payload["days"][0]["total"] = True
    with pytest.raises(AgentCallReportError, match="integer"):
        parse_report(payload)


def test_dry_run_writes_nothing(tmp_path):
    db = RegistryDB(path=str(tmp_path / "registry.db"))
    summary = AgentCallIngest(db, fetch=lambda url: quiet_report()).ingest(
        "http://relay.example/api/linear/agent-calls",
        apply=False,
    )
    assert summary["exitMet"] is True and summary["applied"] is False
    assert read_snapshot(db) is None
    db.close()


def test_apply_stores_the_window_and_a_later_unhealthy_report_keeps_it(tmp_path):
    db = RegistryDB(path=str(tmp_path / "registry.db"))
    clock = {"now": 100.0}
    reports = {
        "ok": quiet_report(),
        "bad": window([0, 0, 0, 0], "2026-10-01", healthy=False, reason="write failed"),
    }
    holder = {"which": "ok"}
    ingest = AgentCallIngest(
        db,
        clock=lambda: clock["now"],
        fetch=lambda url: reports[holder["which"]],
    )
    url = "http://relay.example/api/linear/agent-calls"
    first = ingest.ingest(url, apply=True)
    assert first["applied"] is True and first["exitMet"] is True
    stored = read_snapshot(db)
    assert stored is not None
    assert stored["exitMet"] is True
    assert [row["total"] for row in stored["days"]] == [2, 0, 0, 0, 4]
    assert stored["ingestedAt"] == 100.0

    holder["which"] = "bad"
    clock["now"] = 200.0
    second = ingest.ingest(url, apply=True)
    assert second["exitMet"] is False and second["healthy"] is False
    kept = read_snapshot(db)
    assert kept is not None
    assert kept["healthy"] is False
    assert kept["reason"] == "write failed"
    assert kept["consecutiveZeroDays"] == 0
    assert [row["total"] for row in kept["days"]] == [2, 0, 0, 0, 4]
    assert kept["ingestedAt"] == 200.0
    db.close()


def test_fetch_reads_json_and_rejects_an_error_status():
    def ok(request: httpx.Request) -> httpx.Response:
        assert request.url.params["days"] == "45"
        return httpx.Response(200, json=quiet_report())

    with httpx.Client(transport=httpx.MockTransport(ok)) as client:
        report = parse_report(
            fetch_report(report_url("http://relay.example/api/linear/agent-calls"), client=client)
        )
    assert report["consecutiveZeroDays"] == EXIT_ZERO_DAYS

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    with (
        httpx.Client(transport=httpx.MockTransport(down)) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        fetch_report(report_url("http://relay.example/api/linear/agent-calls"), client=client)


def test_main_dry_run_prints_the_summary_and_a_bad_report_fails(monkeypatch, capsys):
    monkeypatch.setattr(
        "mira.registry.agent_calls.fetch_report",
        lambda url, client=None: quiet_report(),
    )
    assert main(["ingest", "--url", "http://relay.example/api/linear/agent-calls"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["exitMet"] is True and summary["applied"] is False

    monkeypatch.setattr(
        "mira.registry.agent_calls.fetch_report",
        lambda url, client=None: {"healthy": True},
    )
    assert main(["ingest", "--url", "http://relay.example/api/linear/agent-calls"]) == 1
