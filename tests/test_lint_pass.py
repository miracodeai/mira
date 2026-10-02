"""Lint pass (real ruff binary) and the untouched-tests prompt note."""

from __future__ import annotations

import shutil

import pytest

from mira.core.diff_parser import parse_diff
from mira.core.engine import _untouched_tests_note
from mira.core.lint_pass import lint_pass

DIFF = """diff --git a/app/calc.py b/app/calc.py
--- a/app/calc.py
+++ b/app/calc.py
@@ -1,2 +1,3 @@
 def total(xs):
-    return sum(xs)
+    count = len(xs)
+    return sum(xs) / cuont
"""


class _Fetcher:
    async def fetch(self, path: str) -> str:
        return "def total(xs):\n    count = len(xs)\n    return sum(xs) / cuont\n"


@pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff not installed")
async def test_lint_pass_flags_undefined_name_on_added_line():
    comments = await lint_pass(parse_diff(DIFF).files, _Fetcher())

    assert [(c.path, c.line, c.source_pass) for c in comments] == [("app/calc.py", 3, "lint")]
    assert "cuont" in comments[0].title


def test_untouched_tests_note_lists_only_unchanged_tests():
    tree = ["src/calc.py", "tests/test_calc.py", "web/api.ts", "web/api.test.ts", "web/util.ts"]

    note = _untouched_tests_note(
        ["src/calc.py", "web/api.ts", "web/util.ts"],
        changed={"src/calc.py", "web/api.ts", "web/api.test.ts", "web/util.ts"},
        tree=tree,
    )

    assert "- src/calc.py (tests: tests/test_calc.py)" in note
    assert "web/api.ts" not in note  # its test is part of the PR
    assert "web/util.ts" not in note  # no test exists
    assert _untouched_tests_note(["web/util.ts"], set(), tree) == ""
