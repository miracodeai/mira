"""A critic whose verdicts cannot be read must not empty the review.

On a benchmark rerun one host returned the critic's tool arguments as a
complete verdict object followed by leaked reasoning and tool-call markup:
``{"verdicts": [...]}</think>  <｜DSML｜ calls> <｜DSML｜ invoke name="submit_crit…``.
The lenient loader could not parse the trailing junk, the code read that as
"no verdicts", and every draft was dropped as ``no-verdict``, three of them
correct findings.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from mira.core.passes import self_critique
from mira.llm.response_parser import loads_lenient
from mira.models import ReviewComment, Severity

_LEAK = '</think>  <｜DSML｜ calls> <｜DSML｜ invoke name="submit_critique"> <｜DSML｜ parameter name="verdicts" string="false">'


def _comment(title: str, severity: Severity = Severity.WARNING) -> ReviewComment:
    return ReviewComment(
        path="src/app.py",
        line=10,
        end_line=None,
        severity=severity,
        category="bug",
        title=title,
        body="body",
        confidence=0.9,
    )


class TestLoadsLenient:
    def test_takes_the_first_complete_value_when_junk_follows(self):
        payload = {"verdicts": [{"index": 0, "evidence": "proven", "reason": "shown"}]}
        assert loads_lenient(json.dumps(payload) + _LEAK) == payload

    def test_junk_before_the_value_is_skipped(self):
        assert loads_lenient("Here you go:\n" + json.dumps({"ok": True}) + "\nthanks") == {
            "ok": True
        }

    def test_plain_json_still_parses(self):
        assert loads_lenient('{"a": [1, 2]}') == {"a": [1, 2]}

    def test_hopeless_text_is_none(self):
        assert loads_lenient("no json here { still not") is None


class TestCriticWithUnreadableVerdicts:
    def _script(self, monkeypatch, response: str) -> None:
        from mira.llm import provider as provider_mod

        async def fake_complete_with_tools(
            self, messages, tools, temperature=None, force_tool=None
        ):
            return response

        monkeypatch.setattr(
            provider_mod.LLMProvider, "complete_with_tools", fake_complete_with_tools
        )

    @pytest.mark.asyncio
    async def test_verdicts_followed_by_leaked_markup_are_honoured(self, monkeypatch):
        comments = [_comment("real bug"), _comment("bogus")]
        verdicts = [
            {"index": 0, "evidence": "proven", "reason": "shown"},
            {"index": 1, "evidence": "unsupported", "reason": "not in diff"},
        ]
        self._script(monkeypatch, json.dumps({"verdicts": verdicts}) + _LEAK)

        kept = await self_critique(AsyncMock(), comments)

        assert [c.title for c in kept] == ["real bug"]

    @pytest.mark.asyncio
    async def test_no_verdicts_at_all_keeps_every_draft(self, monkeypatch):
        comments = [_comment("a"), _comment("b"), _comment("c")]
        self._script(monkeypatch, json.dumps({"verdicts": []}))

        kept = await self_critique(AsyncMock(), comments)

        assert [c.title for c in kept] == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_unparseable_reply_keeps_every_draft(self, monkeypatch):
        comments = [_comment("a"), _comment("b")]
        self._script(monkeypatch, "<｜DSML｜ calls> nothing useful here")

        kept = await self_critique(AsyncMock(), comments)

        assert [c.title for c in kept] == ["a", "b"]
