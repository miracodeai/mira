"""Tests for cross-chunk Python call verification."""

from __future__ import annotations

from difflib import unified_diff
from unittest.mock import AsyncMock, MagicMock

import pytest

from mira.config import FilterConfig, MiraConfig
from mira.core.engine import ReviewEngine
from mira.core.noise_filter import filter_noise
from mira.core.python_calls import check_python_calls
from mira.models import PRInfo


def _diff(path: str, before: str, head: str) -> str:
    return "".join(
        unified_diff(before.splitlines(True), head.splitlines(True), f"a/{path}", f"b/{path}")
    )


def _caller(call: str, imports: str = "from service import greet", arg: str = "") -> str:
    return f"{imports}\n\ndef run({arg}):\n    return {call}\n"


_OLD = "def greet(name):\n    return name\n"
_HEAD = "def greet(name, prefix):\n    return name\n"
_CHUNKS = {"caller.py": 0, "service.py": 1}


@pytest.mark.parametrize(
    ("imports", "callee"),
    [
        ("from service import greet", "greet"),
        ("from service import greet as hello", "hello"),
        ("import service as api", "api.greet"),
    ],
)
async def test_checks_newly_incompatible_and_compatible_replacements(imports, callee):
    before = _caller(f"{callee}('Ada')", imports)
    for call, expected in (
        (f"{callee}('Grace')", "missing a required argument: 'prefix'"),
        (f"{callee}('Grace', 'Hi ')", None),
    ):
        head = _caller(call, imports)
        sources = {"caller.py": head, "service.py": _HEAD}
        diff = _diff("caller.py", before, head) + _diff("service.py", _OLD, _HEAD)
        fetcher = AsyncMock()
        fetcher.fetch.side_effect = sources.get

        (checked,) = await check_python_calls(diff, _CHUNKS, set(sources), fetcher)

        assert (checked.caller, checked.line, checked.callee, checked.signature_line) == (
            "caller.py",
            4,
            "service.py",
            1,
        )
        assert checked.error == expected
        assert bool(checked.comment()) == bool(expected)


@pytest.mark.parametrize(
    ("before", "head", "old_service", "head_source", "extra_path"),
    [
        (_caller("greet('Ada')"), _caller("greet(*('Grace',))"), _OLD, _HEAD, None),
        (
            _caller("greet('Ada')", arg="greet"),
            _caller("greet('Grace')", arg="greet"),
            _OLD,
            _HEAD,
            None,
        ),
        (_caller("greet('Ada', 'Hi ')"), _caller("greet('Grace')"), _OLD, _HEAD, None),
        (_caller("greet('Ada')"), _caller("greet('Grace')"), _OLD, _HEAD, "service/__init__.py"),
        (
            _caller("greet('Ada')"),
            _caller("greet('Grace')"),
            _OLD,
            "def greet(name, prefix=None):\n    return name\n",
            None,
        ),
    ],
    ids=["starred", "shadowed", "already-broken", "ambiguous-module", "stale-diff"],
)
async def test_skips_unproven_pairs(before, head, old_service, head_source, extra_path):
    sources = {"caller.py": head, "service.py": head_source}
    diff = _diff("caller.py", before, head) + _diff("service.py", old_service, _HEAD)
    fetcher = AsyncMock()
    fetcher.fetch.side_effect = sources.get
    tree = set(sources)
    if extra_path:
        tree.add(extra_path)

    assert await check_python_calls(diff, _CHUNKS, tree, fetcher) == []


async def test_multiple_calls_remain_distinct_and_same_chunk_is_skipped():
    before = _caller("greet('Ada')") + "\ndef second():\n    return greet('Bob')\n"
    head = _caller("greet('Grace')") + "\ndef second():\n    return greet('Alice')\n"
    sources = {"caller.py": head, "service.py": _HEAD}
    diff = _diff("caller.py", before, head) + _diff("service.py", _OLD, _HEAD)
    fetcher = AsyncMock()
    fetcher.fetch.side_effect = sources.get
    assert (
        await check_python_calls(diff, {"caller.py": 0, "service.py": 0}, set(sources), fetcher)
        == []
    )
    fetcher.fetch.assert_not_awaited()

    checked = await check_python_calls(diff, _CHUNKS, set(sources), fetcher)
    comments = [comment for call in checked if (comment := call.comment())]
    assert [c.line for c in filter_noise(comments + comments[:1], FilterConfig())] == [4, 7]


async def test_explicit_sibling_import():
    caller, callee = "app/caller.py", "app/service.py"
    before = _caller("greet('Ada')", "from .service import greet")
    head = _caller("greet('Grace')", "from .service import greet")
    diff = _diff(caller, before, head) + _diff(callee, _OLD, _HEAD)
    fetcher = AsyncMock()
    fetcher.fetch.side_effect = {caller: head, callee: _HEAD}.get
    (checked,) = await check_python_calls(
        diff, {caller: 0, callee: 1}, {caller, callee, "app/__init__.py"}, fetcher
    )
    assert checked.callee == callee
    assert checked.comment() is not None


@pytest.mark.parametrize(
    ("has_initializer", "collapsed"),
    [(False, False), (False, True), (True, False)],
    ids=["namespace", "collapsed", "possible-reexport"],
)
async def test_multiline_submodule_call_with_keyword_values(has_initializer, collapsed):
    caller, callee = "pkg/caller.py", "pkg/service.py"
    before = (
        "from pkg import service\n\n"
        "class Caller:\n"
        "    def run(self, value):\n"
        "        result = service.greet(\n"
        "            value,\n"
        "        )\n"
        "        return result\n"
    )
    if collapsed:
        head = before.replace(
            "service.greet(\n            value,\n        )",
            "service.greet(value, prefix=self.prefix)",
        )
    else:
        head = before.replace("            value,\n", "            value, prefix=self.prefix,\n")
    before_service = "def greet(\n    name,\n    *,\n    prefix=None,\n):\n    return name\n"
    head_service = before_service.replace("prefix=None", "prefix")
    diff = _diff(caller, before, head) + _diff(callee, before_service, head_service)
    fetcher = AsyncMock()
    sources = {caller: head, callee: head_service}
    fetcher.fetch.side_effect = sources.get
    tree = set(sources)
    if has_initializer:
        tree.add("pkg/__init__.py")

    checked = await check_python_calls(diff, {caller: 0, callee: 1}, tree, fetcher)
    if has_initializer:
        assert checked == []
    else:
        (call,) = checked
        assert (call.caller, call.line, call.callee, call.signature_line) == (caller, 5, callee, 4)
        assert call.comment() is None


async def test_hosted_review_reads_python_at_head_sha(tmp_path, monkeypatch):
    monkeypatch.setenv("MIRA_INDEX_DIR", str(tmp_path))
    head = _caller("greet('Grace')")
    sources = {"caller.py": head, "service.py": _HEAD}
    diff = _diff("caller.py", _caller("greet('Ada')"), head) + _diff("service.py", _OLD, _HEAD)
    pr = PRInfo(
        "test",
        "",
        "main",
        "branch",
        "https://example.invalid/pr/1",
        1,
        "org",
        "repo",
        head_sha="a" * 40,
    )
    provider = MagicMock()
    provider.get_pr_info = AsyncMock(return_value=pr)
    provider.get_pr_diff = AsyncMock(return_value=diff)
    provider.get_repo_tree = AsyncMock(return_value=list(sources))
    provider.get_file_content = AsyncMock(side_effect=lambda _pr, path, _ref: sources[path])
    provider.get_file_history = AsyncMock(return_value={})
    model = MagicMock()
    model.review = AsyncMock(return_value='{"comments":[]}')
    model.count_tokens.return_value = 400
    model.usage = {}
    config = MiraConfig()
    config.llm.max_context_tokens = 2500
    config.review.python_call_verification = True
    config.review.agentic_tools = False
    config.review.security_pass = False
    config.review.walkthrough = False
    config.review.include_summary = False

    engine = ReviewEngine(config, model, provider=provider, dry_run=True, bot_name="")
    result = await engine.review_pr(pr.url)

    (finding,) = result.comments
    assert finding.source_pass == "python-call"
    assert model.review.await_count == 2
    provider.get_repo_tree.assert_awaited_once_with(pr, pr.head_sha)
    assert provider.get_file_content.await_count >= 2
    assert all(c.args[2] == pr.head_sha for c in provider.get_file_content.await_args_list)
