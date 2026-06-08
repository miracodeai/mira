"""LLM eval: does Mira recognise a fix and mark the thread for resolution?

`verify_fixes` is the judgment behind auto-resolving review threads — given a
previously-flagged issue and the file's *current* code, it returns the threads
it considers fixed. These evals check that judgment against a real model:

  - a genuinely-fixed issue is marked fixed (→ thread gets resolved),
  - a still-present issue is NOT marked fixed (→ thread stays open),
  - when some are fixed and some aren't, it picks out only the fixed ones.

Probabilistic: each assertion retries a few times and accepts the first clean
result, since hosted models aren't fully deterministic at temperature 0.

Run with: pytest tests/evals/test_eval_thread_resolution.py -m eval
"""

from __future__ import annotations

import os

import pytest

from mira.config import load_config
from mira.core.threads import _number_lines, verify_fixes
from mira.dashboard.models_config import llm_config_for
from mira.llm.provider import LLMProvider
from mira.models import UnresolvedThread

pytestmark = [
    pytest.mark.eval,
    pytest.mark.skipif(
        not os.environ.get("OPENROUTER_API_KEY")
        and not os.environ.get("OPENAI_API_KEY")
        and not os.environ.get("ANTHROPIC_API_KEY"),
        reason="No LLM API key set (need OPENROUTER_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY)",
    ),
]

_RETRIES = 3


def _llm() -> LLMProvider:
    return LLMProvider(llm_config_for("review", load_config().llm))


async def _verify(files: list[tuple[str, str, list[UnresolvedThread]]]) -> list[str]:
    groups = [(path, _number_lines(content), threads) for path, content, threads in files]
    return await verify_fixes(_llm(), groups)


# ── fixtures: a flagged issue + the fixed and still-broken versions ──

_MD5_THREAD = UnresolvedThread(
    thread_id="T-md5",
    path="auth.py",
    line=3,
    body="MD5 is insecure for password hashing. Use bcrypt/argon2 with a salt.",
)
_MD5_FIXED = (
    "import bcrypt\n\ndef hash_pw(p):\n    return bcrypt.hashpw(p.encode(), bcrypt.gensalt())\n"
)
_MD5_BROKEN = "import hashlib\n\ndef hash_pw(p):\n    return hashlib.md5(p.encode()).hexdigest()\n"

_NULL_THREAD = UnresolvedThread(
    thread_id="T-null",
    path="user.py",
    line=2,
    body="`user` can be None here; accessing `.email` will raise AttributeError.",
)
_NULL_FIXED = (
    "def email_of(user):\n    if user is None:\n        return None\n    return user.email\n"
)
_NULL_BROKEN = "def email_of(user):\n    return user.email\n"


async def _verify_passes(files, predicate) -> bool:
    for _ in range(_RETRIES):
        if predicate(await _verify(files)):
            return True
    return False


class TestRecognisesFix:
    @pytest.mark.asyncio
    async def test_security_fix_is_marked_resolved(self):
        ok = await _verify_passes(
            [("auth.py", _MD5_FIXED, [_MD5_THREAD])],
            lambda fixed: "T-md5" in fixed,
        )
        assert ok, "fixed MD5→bcrypt should be recognised as resolved"

    @pytest.mark.asyncio
    async def test_bug_fix_is_marked_resolved(self):
        ok = await _verify_passes(
            [("user.py", _NULL_FIXED, [_NULL_THREAD])],
            lambda fixed: "T-null" in fixed,
        )
        assert ok, "added None-guard should be recognised as resolved"


class TestKeepsUnfixedOpen:
    @pytest.mark.asyncio
    async def test_unfixed_security_issue_stays_open(self):
        # Still using MD5 — must NOT be marked fixed across all retries.
        for _ in range(_RETRIES):
            assert "T-md5" not in await _verify([("auth.py", _MD5_BROKEN, [_MD5_THREAD])])

    @pytest.mark.asyncio
    async def test_unfixed_bug_stays_open(self):
        for _ in range(_RETRIES):
            assert "T-null" not in await _verify([("user.py", _NULL_BROKEN, [_NULL_THREAD])])


class TestDistinguishesMixed:
    @pytest.mark.asyncio
    async def test_resolves_only_the_fixed_thread(self):
        # MD5 fixed, null-deref still present → resolve T-md5 only.
        ok = await _verify_passes(
            [
                ("auth.py", _MD5_FIXED, [_MD5_THREAD]),
                ("user.py", _NULL_BROKEN, [_NULL_THREAD]),
            ],
            lambda fixed: "T-md5" in fixed and "T-null" not in fixed,
        )
        assert ok, "should resolve the fixed thread and leave the unfixed one open"
