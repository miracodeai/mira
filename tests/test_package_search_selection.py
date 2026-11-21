"""A bounded selector must match exhaustive grouping for each backend order."""

from __future__ import annotations

import random
from itertools import permutations

import pytest

from mira.index.manifests import _is_lockfile_path


def _row(name, *, kind="pip", path="pyproject.toml", owner="acme", repo="demo", tag=""):
    return {
        "owner": owner,
        "repo": repo,
        "kind": kind,
        "name": name,
        "version": tag,
        "file_path": path,
    }


def _oracle(rows, limit, postgres_order):
    ordered = (
        list(rows)
        if postgres_order
        else sorted(rows, key=lambda r: (r["name"].lower(), r["owner"], r["repo"]))
    )
    groups = {}
    for row in ordered:
        key = (row["owner"], row["repo"], row["kind"], row["name"].lower())
        old = groups.get(key)
        if old is None or (
            _is_lockfile_path(row["file_path"]) and not _is_lockfile_path(old["file_path"])
        ):
            groups[key] = row
    return list(groups.values())[:limit]


def _select(rows, limit, postgres_order):
    from mira.index._package_search import PackageGroups

    selector = PackageGroups(limit, postgres_order=postgres_order)
    for row in rows:
        selector.add(row)
    return selector.finish()


@pytest.mark.parametrize("postgres_order", [False, True])
@pytest.mark.parametrize("limit", [1, 2, 4])
def test_package_groups_match_exhaustive_oracle_across_arrival_orders(postgres_order, limit):
    rows = [
        _row("alpha", tag="manifest"),
        _row("ALPHA", path="uv.lock", tag="lock"),
        _row("alpha", kind="npm", tag="equal-sort-different-kind"),
        _row("bravo", path="uv.lock"),
        _row("İ", path="poetry.lock"),
        _row("i\u0307", tag="same-lowercase"),
    ]
    for arrival in permutations(rows):
        assert _select(arrival, limit, postgres_order) == _oracle(arrival, limit, postgres_order)


@pytest.mark.parametrize("postgres_order", [False, True])
def test_package_groups_keep_lowercase_identity_without_casefolding(postgres_order):
    rng = random.Random(20261029)
    names = ["ß", "ss", "Σ", "σ", "ς", "K", "K", "İ", "i\u0307", "日本"]
    for _ in range(100):
        rows = [
            _row(
                rng.choice(names),
                kind=rng.choice(["pip", "npm"]),
                path=rng.choice(["pyproject.toml", "uv.lock", "poetry.lock"]),
                owner=rng.choice(["a", "_literal", "日本"]),
                tag=str(i),
            )
            for i in range(50)
        ]
        assert _select(rows, 7, postgres_order) == _oracle(rows, 7, postgres_order)
    assert len(_select([_row("ß"), _row("ss")], 2, postgres_order)) == 2


def test_package_groups_forget_evicted_groups_without_promoting_late_reencounters():
    rows = [
        _row("z"),
        _row("y"),
        _row("a"),
        _row("z", path="uv.lock"),
        _row("b"),
        _row("y", path="uv.lock"),
        _row("a", path="poetry.lock"),
    ]
    assert _select(rows, 2, False) == _oracle(rows, 2, False)


def test_package_groups_bound_retained_state_through_repeated_evictions():
    from mira.index._package_search import PackageGroups

    selector = PackageGroups(17)
    for number in range(10000, 0, -1):
        selector.add(_row(f"{number:06}"))
        # This verifies the requested resource bound, not a particular heap order.
        assert len(selector._groups) == len(selector._heap) <= 17
    for _ in range(1000):
        selector.add(_row("000001", path="uv.lock"))
        assert len(selector._groups) == len(selector._heap) == 17
    assert [r["name"] for r in selector.finish()] == [f"{n:06}" for n in range(1, 18)]
