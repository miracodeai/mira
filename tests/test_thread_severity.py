"""Reading severity and title back out of a posted bot comment."""

from __future__ import annotations

import pytest

from mira.core.threads import thread_severity, thread_title
from mira.models import ReviewComment, Severity
from mira.providers.formatting import format_comment_body


def _comment(severity: Severity) -> ReviewComment:
    return ReviewComment(
        path="x.py",
        line=1,
        end_line=None,
        severity=severity,
        category="other",
        title="Token logged in plain text",
        body="The token is written to the log.",
        confidence=0.9,
    )


@pytest.mark.parametrize("severity", list(Severity))
def test_round_trips_every_severity(severity: Severity):
    body = format_comment_body(_comment(severity), bot_name="mira")
    assert thread_severity(body) == severity
    assert thread_title(body) == "Token logged in plain text"


def test_unbadged_body_has_no_severity():
    assert thread_severity("just a note") is None
    assert thread_severity("") is None


def test_title_falls_back_to_first_line():
    assert thread_title("just a note") == "just a note"
    assert thread_title("") == ""
