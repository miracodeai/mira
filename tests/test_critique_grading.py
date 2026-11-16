"""Evidence-graded self-critique: keep rule, hunk evidence, verdict parsing."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from mira.core.passes import _critique_keep, _hunk_evidence, self_critique
from mira.models import FileChangeType, FileDiff, HunkInfo, ReviewComment, Severity


def _comment(
    severity: Severity = Severity.WARNING,
    confidence: float = 0.9,
    line: int = 10,
    title: str = "t",
) -> ReviewComment:
    return ReviewComment(
        path="app.py",
        line=line,
        end_line=None,
        severity=severity,
        category="bug",
        title=title,
        body="body",
        confidence=confidence,
    )


class TestKeepRule:
    def test_proven_always_kept(self):
        c = _comment(severity=Severity.NITPICK, confidence=0.1)
        assert _critique_keep({"evidence": "proven"}, c)

    def test_unsupported_always_dropped(self):
        c = _comment(severity=Severity.BLOCKER, confidence=1.0)
        assert not _critique_keep({"evidence": "unsupported"}, c)

    def test_plausible_kept_only_when_severe_and_confident(self):
        assert _critique_keep({"evidence": "plausible"}, _comment(Severity.WARNING, confidence=0.8))
        assert _critique_keep({"evidence": "plausible"}, _comment(Severity.BLOCKER, 0.95))
        # 0.75 sits above the 0.7 posting floor: a warning the reviewer rated
        # there and the critic could not contradict is posted.
        assert _critique_keep({"evidence": "plausible"}, _comment(Severity.WARNING, 0.75))
        assert not _critique_keep({"evidence": "plausible"}, _comment(Severity.WARNING, 0.7))
        assert not _critique_keep({"evidence": "plausible"}, _comment(Severity.SUGGESTION, 0.95))

    def test_legacy_keep_boolean_honored(self):
        c = _comment()
        assert _critique_keep({"keep": True}, c)
        assert not _critique_keep({"keep": False}, c)
        assert not _critique_keep({}, c)


class TestHunkEvidence:
    def _file(self) -> FileDiff:
        return FileDiff(
            path="app.py",
            change_type=FileChangeType.MODIFIED,
            hunks=[
                HunkInfo(1, 5, 1, 5, "hunk-one content"),
                HunkInfo(20, 10, 8, 10, "hunk-two content"),
            ],
        )

    def test_picks_covering_hunk(self):
        c = _comment(line=10)
        assert _hunk_evidence(c, [self._file()]) == "hunk-two content"

    def test_no_match_returns_empty(self):
        c = _comment(line=100)
        assert _hunk_evidence(c, [self._file()]) == ""
        assert _hunk_evidence(c, None) == ""
        assert _hunk_evidence(c, []) == ""

    def test_long_hunks_truncated(self):
        from mira.core.passes import _MAX_HUNK_EVIDENCE_CHARS

        f = FileDiff(
            path="app.py",
            change_type=FileChangeType.MODIFIED,
            hunks=[HunkInfo(1, 5, 1, 50, "x" * 5000)],
        )
        out = _hunk_evidence(_comment(line=10), [f])
        assert len(out) <= _MAX_HUNK_EVIDENCE_CHARS + 1

    def test_window_is_centred_on_the_cited_line_not_the_hunk_head(self):
        """A new file is one hunk; the critic must see the cited region, not
        the first screenful, or it grades a real finding as 'code not shown'."""
        body = "\n".join(f"+line {i}" for i in range(1, 121))
        f = FileDiff(
            path="app.py",
            change_type=FileChangeType.ADDED,
            hunks=[HunkInfo(0, 0, 1, 120, "@@ -0,0 +1,120 @@\n" + body)],
        )
        out = _hunk_evidence(_comment(line=80), [f])
        assert "+line 80" in out
        assert "+line 60" in out and "+line 100" in out
        assert "+line 1\n" not in out and "+line 120" not in out
        assert "earlier line(s)" in out and "later line(s)" in out

    def test_removed_lines_stay_inside_the_window(self):
        content = "@@ -1,4 +1,4 @@\n a\n-old guard\n+new code\n b"
        f = FileDiff(
            path="app.py",
            change_type=FileChangeType.MODIFIED,
            hunks=[HunkInfo(1, 4, 1, 4, content)],
        )
        out = _hunk_evidence(_comment(line=2), [f])
        assert "-old guard" in out and "+new code" in out


class TestEvidenceGradedCritique:
    @pytest.mark.asyncio
    async def test_verdicts_without_index_follow_input_order(self, monkeypatch):
        comments = [_comment(title="real bug"), _comment(title="bogus", severity=Severity.BLOCKER)]
        response = json.dumps({"verdicts": [{"evidence": "proven"}, {"evidence": "unsupported"}]})
        from mira.llm import provider as provider_mod

        async def fake_complete_with_tools(self, messages, tools, temperature=None):
            return response

        monkeypatch.setattr(
            provider_mod.LLMProvider, "complete_with_tools", fake_complete_with_tools
        )

        kept = await self_critique(AsyncMock(), comments)

        assert [c.title for c in kept] == ["real bug"]

    @pytest.mark.asyncio
    async def test_verdicts_without_index_ignored_when_count_differs(self, monkeypatch):
        comments = [_comment(title="a"), _comment(title="b"), _comment(title="c")]
        response = json.dumps({"verdicts": [{"evidence": "proven"}, {"evidence": "proven"}]})
        from mira.llm import provider as provider_mod

        async def fake_complete_with_tools(self, messages, tools, temperature=None):
            return response

        monkeypatch.setattr(
            provider_mod.LLMProvider, "complete_with_tools", fake_complete_with_tools
        )

        assert await self_critique(AsyncMock(), comments) == []

    @pytest.mark.asyncio
    async def test_mixed_indexed_verdicts_skip_positional_fallback(self, monkeypatch):
        comments = [_comment(title="a"), _comment(title="b")]
        verdicts = [{"index": 1, "evidence": "proven"}, {"evidence": "proven"}]
        response = json.dumps({"verdicts": verdicts})
        from mira.llm import provider as provider_mod

        async def fake_complete_with_tools(self, messages, tools, temperature=None):
            return response

        monkeypatch.setattr(
            provider_mod.LLMProvider, "complete_with_tools", fake_complete_with_tools
        )

        assert [c.title for c in await self_critique(AsyncMock(), comments)] == ["b"]

    @pytest.mark.asyncio
    async def test_grades_applied_through_llm_flow(self, monkeypatch):
        comments = [
            _comment(title="proven bug"),
            _comment(title="plausible low-sev", severity=Severity.SUGGESTION),
            _comment(title="unsupported claim", severity=Severity.BLOCKER),
        ]
        response = json.dumps(
            {
                "verdicts": [
                    {"index": 0, "evidence": "proven", "reason": "shown in diff"},
                    {"index": 1, "evidence": "plausible", "reason": "depends on caller"},
                    {"index": 2, "evidence": "unsupported", "reason": "code contradicts"},
                ]
            }
        )
        from mira.llm import provider as provider_mod

        captured = {}

        async def fake_complete_with_tools(self, messages, tools, temperature=None):
            captured["prompt"] = messages[0]["content"]
            return response

        monkeypatch.setattr(
            provider_mod.LLMProvider, "complete_with_tools", fake_complete_with_tools
        )

        diff_files = [
            FileDiff(
                path="app.py",
                change_type=FileChangeType.MODIFIED,
                hunks=[HunkInfo(1, 5, 1, 50, "+    def should_block?(email)")],
            )
        ]
        kept = await self_critique(AsyncMock(), comments, diff_files=diff_files)

        assert [c.title for c in kept] == ["proven bug"]
        # The critic saw the actual hunk, not just the citation.
        assert "should_block?" in captured["prompt"]

    @pytest.mark.asyncio
    async def test_audit_records_dropped_with_evidence_and_reason(self, monkeypatch):
        comments = [
            _comment(title="kept bug"),
            _comment(title="dropped claim", severity=Severity.BLOCKER),
        ]
        response = json.dumps(
            {
                "verdicts": [
                    {"index": 0, "evidence": "proven", "reason": "shown"},
                    {"index": 1, "evidence": "unsupported", "reason": "code contradicts"},
                ]
            }
        )
        from mira.llm import provider as provider_mod

        async def fake(self, messages, tools, temperature=None):
            return response

        monkeypatch.setattr(provider_mod.LLMProvider, "complete_with_tools", fake)

        audit: list[dict] = []
        await self_critique(AsyncMock(), comments, audit=audit)

        assert len(audit) == 1
        entry = audit[0]
        assert entry["stage"] == "self_critique"
        assert entry["title"] == "dropped claim"
        assert entry["reason"] == "unsupported: code contradicts"

    @pytest.mark.asyncio
    async def test_held_back_collects_plausible_drops_only(self, monkeypatch):
        comments = [
            _comment(title="proven bug"),
            _comment(title="maybe", severity=Severity.SUGGESTION, confidence=0.9),
            _comment(title="wrong", severity=Severity.BLOCKER),
        ]
        response = json.dumps(
            {
                "verdicts": [
                    {"index": 0, "evidence": "proven", "reason": "shown"},
                    {"index": 1, "evidence": "plausible", "reason": "depends on callers"},
                    {"index": 2, "evidence": "unsupported", "reason": "code contradicts"},
                ]
            }
        )
        from mira.llm import provider as provider_mod

        async def fake(self, messages, tools, temperature=None):
            return response

        monkeypatch.setattr(provider_mod.LLMProvider, "complete_with_tools", fake)

        held_back: list[ReviewComment] = []
        kept = await self_critique(AsyncMock(), comments, held_back=held_back)

        assert [c.title for c in kept] == ["proven bug"]
        # Structured objects, not audit strings; unsupported drops excluded.
        assert held_back == [comments[1]]

    @pytest.mark.asyncio
    async def test_audit_records_missing_verdict(self, monkeypatch):
        # The critic graded one draft and skipped the other: the skipped one is
        # dropped and audited. (A reply with no verdicts at all is a failed
        # critic and keeps every draft — see test_critic_unreadable_verdicts.)
        comments = [_comment(title="no verdict for me"), _comment(title="graded")]
        from mira.llm import provider as provider_mod

        async def fake(self, messages, tools, temperature=None):
            return json.dumps({"verdicts": [{"index": 1, "evidence": "proven"}]})

        monkeypatch.setattr(provider_mod.LLMProvider, "complete_with_tools", fake)

        audit: list[dict] = []
        kept = await self_critique(AsyncMock(), comments, audit=audit)

        assert [c.title for c in kept] == ["graded"]
        assert audit[0]["reason"].startswith("no-verdict:")


class TestEvidenceWindowEdges:
    def test_range_spanning_two_hunks_keeps_both(self):
        f = FileDiff(
            path="app.py",
            change_type=FileChangeType.MODIFIED,
            hunks=[
                HunkInfo(1, 3, 1, 3, "@@ -1,3 +1,3 @@\n+one\n+two\n+three"),
                HunkInfo(20, 3, 20, 3, "@@ -20,3 +20,3 @@\n+twenty\n+twenty-one\n+twenty-two"),
            ],
        )
        c = _comment(line=2)
        c.end_line = 21
        out = _hunk_evidence(c, [f])
        assert "+two" in out and "+twenty-one" in out

    def test_no_newline_marker_does_not_shift_numbering(self):
        body = "\n".join(f"+line {i}" for i in range(1, 60))
        content = "@@ -0,0 +1,59 @@\n" + body + "\n\\ No newline at end of file"
        f = FileDiff(
            path="app.py", change_type=FileChangeType.ADDED, hunks=[HunkInfo(0, 0, 1, 59, content)]
        )
        out = _hunk_evidence(_comment(line=59), [f])
        assert "+line 59" in out
