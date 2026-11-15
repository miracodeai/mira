"""Prove npm dependency origins against the fetched JSON and its complete diff."""

from __future__ import annotations

import json
import re
from bisect import bisect_right
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mira.index.manifests import ParsedPackage, _npm_lock_package

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mira.models import FileDiff

_SPACE = re.compile(r"[ \t\r\n]*")
_MAX_CHARS = 4_000_000
_MAX_FIELDS = 100_000
_MAX_DEPTH = 64


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


@dataclass(frozen=True)
class _Field:
    key_start: int
    value_start: int


class _Document:
    def __init__(self, content: str) -> None:
        if len(content) > _MAX_CHARS:
            raise ValueError("Manifest exceeds origin-check limit")
        self.content = content
        self.position = 0
        self.fields: dict[tuple[str, ...], _Field] = {}
        self.decoder = json.JSONDecoder(
            object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
        self.data = self._value((), 0)
        self._space()
        if self.position != len(content) or not isinstance(self.data, dict):
            raise ValueError("Expected one JSON object")
        self.newlines = [i for i, char in enumerate(content) if char == "\n"]

    def _space(self) -> None:
        match = _SPACE.match(self.content, self.position)
        assert match is not None
        self.position = match.end()

    def _value(self, path: tuple[str, ...], depth: int) -> Any:
        if depth > _MAX_DEPTH:
            raise ValueError("Manifest nesting exceeds origin-check limit")
        self._space()
        if self.content[self.position : self.position + 1] != "{":
            value, self.position = self.decoder.raw_decode(self.content, self.position)
            return value
        self.position += 1
        self._space()
        result: dict[str, Any] = {}
        if self.content[self.position : self.position + 1] == "}":
            self.position += 1
            return result
        while True:
            self._space()
            key_start = self.position
            key, self.position = self.decoder.raw_decode(self.content, self.position)
            if not isinstance(key, str) or key in result:
                raise ValueError("Expected an unambiguous JSON property")
            self._space()
            if self.content[self.position : self.position + 1] != ":":
                raise ValueError("Expected JSON property separator")
            self.position += 1
            self._space()
            value_start = self.position
            result[key] = self._value((*path, key), depth + 1)
            self.fields[(*path, key)] = _Field(key_start, value_start)
            if len(self.fields) > _MAX_FIELDS:
                raise ValueError("Manifest has too many properties")
            self._space()
            delimiter = self.content[self.position : self.position + 1]
            self.position += 1
            if delimiter == "}":
                return result
            if delimiter != ",":
                raise ValueError("Expected JSON object separator")

    def line(self, position: int) -> int:
        return bisect_right(self.newlines, position) + 1


def _lines(content: str) -> list[str]:
    lines = content.split("\n")
    return lines[:-1] if lines[-1] == "" else lines


def _before_content(diff: FileDiff, content: str) -> tuple[str, set[int]]:
    head = _lines(content)
    before: list[str] = []
    added: set[int] = set()
    cursor = 0
    for hunk in diff.hunks:
        start = hunk.target_start - bool(hunk.target_length)
        if start < cursor or start > len(head):
            raise ValueError("Overlapping or out-of-range diff")
        before.extend(head[cursor:start])
        if len(before) != hunk.source_start - bool(hunk.source_length):
            raise ValueError("Inconsistent source position")
        cursor = start
        source_count = target_count = 0
        body = _lines(hunk.content)
        if not body or not body[0].startswith("@@ "):
            raise ValueError("Missing hunk header")
        for raw in body[1:]:
            if raw == "\\ No newline at end of file":
                continue
            kind, text = raw[:1], raw[1:]
            if kind not in (" ", "+", "-"):
                raise ValueError("Invalid diff body")
            if kind in (" ", "+"):
                if cursor >= len(head) or head[cursor] != text:
                    raise ValueError("Diff does not match fetched source")
                cursor += 1
                target_count += 1
                if kind == "+":
                    added.add(cursor)
            if kind in (" ", "-"):
                before.append(text)
                source_count += 1
        if (source_count, target_count) != (hunk.source_length, hunk.target_length):
            raise ValueError("Incomplete diff hunk")
    before.extend(head[cursor:])
    return "\n".join(before), added


def _packages(
    data: dict[str, Any], path: str
) -> Iterator[tuple[ParsedPackage, tuple[str, ...], tuple[str, ...]]]:
    record: tuple[str, ...]
    if path.rsplit("/", 1)[-1] == "package.json":
        for section in (
            "dependencies",
            "devDependencies",
            "peerDependencies",
            "optionalDependencies",
        ):
            block = data.get(section)
            if not isinstance(block, dict):
                continue
            for name, version in block.items():
                if not isinstance(version, str):
                    continue
                pkg = _npm_lock_package(
                    name,
                    {"version": version.strip(), "dev": section == "devDependencies"},
                    path,
                )
                if pkg is not None:
                    record = (section, name)
                    yield pkg, record, record
        return
    packages = data.get("packages")
    if not isinstance(packages, dict):
        dependencies = data.get("dependencies")
        if not isinstance(dependencies, dict):
            return
        pending: list[tuple[Iterator[tuple[str, Any]], tuple[str, ...]]] = [
            (iter(dependencies.items()), ("dependencies",))
        ]
        while pending:
            entry = next(pending[-1][0], None)
            if entry is None:
                pending.pop()
                continue
            name, info = entry
            if not isinstance(info, dict):
                continue
            record = (*pending[-1][1], name)
            pkg = _npm_lock_package(name, info, path, legacy=True)
            if pkg is not None:
                yield pkg, record, (*record, "version")
            children = info.get("dependencies")
            if isinstance(children, dict):
                pending.append((iter(children.items()), (*record, "dependencies")))
        return
    for location, info in packages.items():
        if not isinstance(info, dict):
            continue
        _, separator, name = f"/{location}".rpartition("/node_modules/")
        if not separator:
            continue
        pkg = _npm_lock_package(name, info, path)
        if pkg is not None:
            record = ("packages", location)
            yield pkg, record, (*record, "version")


def added_npm_packages(diff: FileDiff, content: str) -> list[tuple[ParsedPackage, int, str]]:
    """Return only concrete npm tuples newly introduced at a verified added line."""
    try:
        head = _Document(content)
        before_text, added = _before_content(diff, content)
        before = _Document(before_text).data if before_text.strip() else {}
        existing = {(pkg.name, pkg.version) for pkg, _, _ in _packages(before, diff.path)}
        lines = _lines(content)
        origins = []
        for pkg, record, version in _packages(head.data, diff.path):
            # Query eligibility cannot establish that an old tuple did not exist.
            if not pkg.scan_eligible or (pkg.name, pkg.version) in existing:
                continue
            for position in (head.fields[record].key_start, head.fields[version].value_start):
                line = head.line(position)
                if line in added:
                    origins.append((pkg, line, lines[line - 1]))
                    break
        return origins
    except (ValueError, KeyError, RecursionError):
        return []
