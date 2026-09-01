"""Tests for cross-PR overlap detection and the fingerprint cache."""

from __future__ import annotations

import pytest

from mira.config import MiraConfig
from mira.core.overlap import (
    _is_stacked,
    _parse_overlap_response,
    _prefilter,
    detect_overlaps,
)
from mira.index.store import IndexStore
from mira.models import OpenPRRef, PRFingerprint, PRInfo


def _fp(number, *, title="", body="", paths=None, symbols=None, head_sha="sha"):
    return PRFingerprint(
        pr_number=number,
        head_sha=head_sha,
        title=title,
        body=body,
        paths=paths or [],
        symbols=symbols or [],
    )


# ── Fingerprint cache round-trip ──────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    s = IndexStore(str(tmp_path / "test.db"))
    yield s
    s.close()


def test_fingerprint_upsert_and_list(store):
    fp = _fp(7, title="Add caching", paths=["a.py", "b.py"], symbols=["foo", "bar"])
    store.upsert_pr_fingerprint(fp)

    rows = store.list_pr_fingerprints()
    assert len(rows) == 1
    got = rows[0]
    assert got.pr_number == 7
    assert got.title == "Add caching"
    assert got.paths == ["a.py", "b.py"]
    assert got.symbols == ["foo", "bar"]


def test_fingerprint_upsert_replaces_on_same_pr(store):
    store.upsert_pr_fingerprint(_fp(7, head_sha="old", paths=["a.py"]))
    store.upsert_pr_fingerprint(_fp(7, head_sha="new", paths=["a.py", "c.py"]))

    rows = store.list_pr_fingerprints()
    assert len(rows) == 1
    assert rows[0].head_sha == "new"
    assert rows[0].paths == ["a.py", "c.py"]


def test_fingerprint_upsert_prunes_stale_rows(store):
    import time

    now = time.time()
    old = _fp(1)
    old.updated_at = now - IndexStore._FINGERPRINT_TTL - 3600
    fresh = _fp(2)
    fresh.updated_at = now - 60
    store.upsert_pr_fingerprint(old)
    store.upsert_pr_fingerprint(fresh)
    store.upsert_pr_fingerprint(_fp(3))

    numbers = {fp.pr_number for fp in store.list_pr_fingerprints()}
    assert numbers == {2, 3}


# ── Pre-filter lanes ──────────────────────────────────────────────────────


def test_prefilter_full_file_overlap():
    current = _fp(1, paths=["x.py", "y.py"])
    cand = _fp(2, paths=["y.py", "z.py"])
    keep, shared = _prefilter(current, cand, title_threshold=0.4)
    assert keep is True
    assert shared == ["y.py"]


def test_prefilter_no_overlap():
    current = _fp(1, title="Fix login bug", paths=["auth.py"])
    cand = _fp(2, title="Update README typos", paths=["README.md"])
    keep, shared = _prefilter(current, cand, title_threshold=0.4)
    assert keep is False
    assert shared == []


def test_prefilter_symbol_overlap_without_shared_files():
    current = _fp(1, paths=["a.py"], symbols=["process_order"])
    cand = _fp(2, paths=["b.py"], symbols=["process_order"])
    keep, shared = _prefilter(current, cand, title_threshold=0.4)
    assert keep is True
    # No shared files — the symbol lane is what kept it.
    assert shared == []


def test_prefilter_text_similarity_lane():
    # Different files, but near-identical titles → duplicate-effort candidate.
    current = _fp(1, title="add rate limiting to auth middleware", paths=["a.py"])
    cand = _fp(2, title="add rate limiting to auth middleware again", paths=["b.py"])
    keep, shared = _prefilter(current, cand, title_threshold=0.4)
    assert keep is True
    assert shared == []


def test_prefilter_dissimilar_titles_dropped():
    current = _fp(1, title="add rate limiting to auth middleware", paths=["a.py"])
    cand = _fp(2, title="bump dependency versions", paths=["b.py"])
    keep, _ = _prefilter(current, cand, title_threshold=0.4)
    assert keep is False


# ── Stacked-PR suppression ────────────────────────────────────────────────


def _pr_info(**kw):
    defaults = {
        "title": "t",
        "description": "d",
        "base_branch": "main",
        "head_branch": "feature/x",
        "url": "https://github.com/o/r/pull/1",
        "number": 1,
        "owner": "o",
        "repo": "r",
        "head_sha": "h",
    }
    defaults.update(kw)
    return PRInfo(**defaults)


def _ref(number, **kw):
    defaults = {
        "title": "t",
        "body": "",
        "head_sha": "s",
        "author": "alice",
        "draft": False,
        "base_ref": "main",
        "head_ref": "feature/y",
        "url": f"https://github.com/o/r/pull/{number}",
    }
    defaults.update(kw)
    return OpenPRRef(number=number, **defaults)


def test_stacked_candidate_built_on_this_pr():
    pr = _pr_info(head_branch="feature/base")
    # Candidate branches off this PR's head → stacked.
    ref = _ref(2, base_ref="feature/base")
    assert _is_stacked(pr, ref) is True


def test_stacked_this_pr_built_on_candidate():
    pr = _pr_info(base_branch="feature/parent")
    ref = _ref(2, head_ref="feature/parent")
    assert _is_stacked(pr, ref) is True


def test_not_stacked_independent_branches():
    pr = _pr_info(base_branch="main", head_branch="feature/x")
    ref = _ref(2, base_ref="main", head_ref="feature/y")
    assert _is_stacked(pr, ref) is False


# ── Verdict parsing ───────────────────────────────────────────────────────


def test_parse_overlap_response_valid():
    raw = (
        '{"overlaps": [{"pr_number": 5, "kind": "duplicate_effort", '
        '"reason": "same feature", "confidence": 0.8}]}'
    )
    out = _parse_overlap_response(raw)
    assert out == {5: ("duplicate_effort", "same feature", 0.8)}


def test_parse_overlap_response_fenced_and_unknown_kind():
    raw = '```json\n{"overlaps": [{"pr_number": 5, "kind": "weird", "confidence": 2}]}\n```'
    out = _parse_overlap_response(raw)
    # Unknown kind coerced to none, confidence clamped to 1.0.
    assert out[5][0] == "none"
    assert out[5][2] == 1.0


def test_parse_overlap_response_garbage():
    assert _parse_overlap_response("not json at all") == {}


# ── End-to-end detect_overlaps with a fake provider/LLM ───────────────────


class _FakeProvider:
    def __init__(self, files_by_pr):
        self._files = files_by_pr

    async def list_open_prs(self, owner, repo, limit=20):  # pragma: no cover - unused here
        return []

    async def get_pr_files(self, owner, repo, number, limit=300):
        return self._files.get(number, [])


class _FakeLLM:
    def __init__(self, response):
        self._response = response
        self.supports_json_mode = True

    async def complete(self, messages, json_mode=True, **kw):
        return self._response


@pytest.mark.asyncio
async def test_detect_overlaps_confirms_file_conflict():
    cfg = MiraConfig()
    pr = _pr_info(number=1)
    current = _fp(1, title="Refactor auth", paths=["auth.py"])
    candidates = [_ref(2, head_sha="s2")]
    provider = _FakeProvider({2: ["auth.py", "other.py"]})
    llm = _FakeLLM(
        '{"overlaps": [{"pr_number": 2, "kind": "merge_conflict", '
        '"reason": "both edit auth.py", "confidence": 0.9}]}'
    )

    findings = await detect_overlaps(
        provider=provider,
        llm=llm,
        config=cfg,
        pr_info=pr,
        current=current,
        cached={},
        candidates=candidates,
    )
    assert len(findings) == 1
    assert findings[0].pr_number == 2
    assert findings[0].kind == "merge_conflict"
    assert findings[0].shared_files == ["auth.py"]


@pytest.mark.asyncio
async def test_detect_overlaps_persists_fetched_fingerprints():
    cfg = MiraConfig()
    pr = _pr_info(number=1)
    current = _fp(1, title="Refactor auth", paths=["auth.py"])
    # PR 2 has a fresh cached fingerprint, PR 3 is unseen → fetched + saved.
    candidates = [_ref(2, head_sha="s2"), _ref(3, head_sha="s3")]
    cached = {2: _fp(2, paths=["auth.py"], head_sha="s2")}
    provider = _FakeProvider({3: ["auth.py"]})
    llm = _FakeLLM('{"overlaps": []}')
    saved = []

    await detect_overlaps(
        provider=provider,
        llm=llm,
        config=cfg,
        pr_info=pr,
        current=current,
        cached=cached,
        candidates=candidates,
        save_fp=saved.append,
    )
    assert [fp.pr_number for fp in saved] == [3]
    assert saved[0].paths == ["auth.py"]
    assert saved[0].head_sha == "s3"


@pytest.mark.asyncio
async def test_detect_overlaps_drops_low_confidence_and_none():
    cfg = MiraConfig()
    pr = _pr_info(number=1)
    current = _fp(1, title="Refactor auth", paths=["auth.py"])
    candidates = [_ref(2, head_sha="s2"), _ref(3, head_sha="s3")]
    provider = _FakeProvider({2: ["auth.py"], 3: ["auth.py"]})
    llm = _FakeLLM(
        '{"overlaps": ['
        '{"pr_number": 2, "kind": "none", "reason": "incidental", "confidence": 0.9},'
        '{"pr_number": 3, "kind": "merge_conflict", "reason": "x", "confidence": 0.2}'
        "]}"
    )
    findings = await detect_overlaps(
        provider=provider,
        llm=llm,
        config=cfg,
        pr_info=pr,
        current=current,
        cached={},
        candidates=candidates,
    )
    assert findings == []


@pytest.mark.asyncio
async def test_detect_overlaps_no_survivors_skips_llm():
    cfg = MiraConfig()
    pr = _pr_info(number=1)
    current = _fp(1, title="Fix login", paths=["auth.py"])
    candidates = [_ref(2, head_sha="s2", title="Update docs")]
    provider = _FakeProvider({2: ["README.md"]})

    class _ExplodingLLM:
        supports_json_mode = True

        async def complete(self, *a, **k):
            raise AssertionError("LLM should not be called when no candidate survives")

    findings = await detect_overlaps(
        provider=provider,
        llm=_ExplodingLLM(),
        config=cfg,
        pr_info=pr,
        current=current,
        cached={},
        candidates=candidates,
    )
    assert findings == []
