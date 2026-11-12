"""Verify introduced Python call-binding errors across review chunks."""

from __future__ import annotations

import ast
import asyncio
import logging
import symtable
from dataclasses import dataclass
from inspect import Parameter, Signature
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from unidiff import PatchSet
from unidiff.patch import PatchedFile

from mira.models import ReviewComment, Severity

if TYPE_CHECKING:
    from mira.index.context import SourceFetcher

logger = logging.getLogger(__name__)
SOURCE_PASS = "python-call"
_MAX_FILES = 8
_MAX_SOURCE_CHARS = 50_000
_MAX_PAIRS = 16


@dataclass
class _Revision:
    imports: dict[str, tuple[str, str | None]]
    functions: dict[str, ast.FunctionDef]
    scopes: dict[tuple[str, int], symtable.SymbolTable]


@dataclass
class _Change:
    before: _Revision
    head: _Revision
    added: set[int]
    replaced: dict[int, int]


@dataclass(frozen=True)
class CheckedCall:
    caller: str
    line: int
    callee: str
    signature_line: int
    error: str | None

    def comment(self) -> ReviewComment | None:
        if self.error is None:
            return None
        return ReviewComment(
            path=self.caller,
            line=self.line,
            end_line=None,
            severity=Severity.BLOCKER,
            category="bug",
            title="Python call no longer binds to its signature",
            body=(
                f"The committed call cannot bind to the signature at "
                f"`{self.callee}:{self.signature_line}`: {self.error}. "
                "Update this call or preserve a compatible signature."
            ),
            confidence=1.0,
            source_pass=SOURCE_PASS,
        )


def _revision(source: str) -> _Revision | None:
    module = ast.parse(source)
    imports: dict[str, tuple[str, str | None]] = {}
    functions: dict[str, ast.FunctionDef] = {}
    entries: list[tuple[str, tuple[str, str | None]]]
    for statement in module.body:
        if isinstance(statement, ast.Import):
            if any("." in alias.name and not alias.asname for alias in statement.names):
                return None
            entries = [
                (alias.asname or alias.name, (alias.name, None)) for alias in statement.names
            ]
        elif isinstance(statement, ast.ImportFrom) and statement.module and statement.level <= 1:
            if any(alias.name == "*" for alias in statement.names):
                return None
            module_name = "." * statement.level + statement.module
            entries = [
                (alias.asname or alias.name, (module_name, alias.name)) for alias in statement.names
            ]
        elif isinstance(statement, ast.FunctionDef) and not statement.decorator_list:
            if statement.name in imports or statement.name in functions:
                return None
            functions[statement.name] = statement
            continue
        elif (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        else:
            return None
        for name, target in entries:
            if name in imports or name in functions:
                return None
            imports[name] = target
    # Mutating globals or module attributes invalidates this simple import model.
    if any(
        isinstance(node, (ast.Global, ast.Nonlocal))
        or (isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load))
        for node in ast.walk(module)
    ):
        return None
    scopes = {
        (scope.get_name(), scope.get_lineno()): scope
        for scope in symtable.symtable(source, "", "exec").get_children()
    }
    return _Revision(imports, functions, scopes)


def _change(file: PatchedFile, source: str) -> _Change | None:
    """Reconstruct the diff preimage only when its head lines match the pinned source."""
    head_lines = source.splitlines(keepends=True)
    before_lines: list[str] = []
    added: set[int] = set()
    replaced: dict[int, int] = {}
    cursor = 0
    for hunk in file:
        start = hunk.target_start - 1 if hunk.target_length else hunk.target_start
        old_start = hunk.source_start - 1 if hunk.source_length else hunk.source_start
        if not cursor <= start <= len(head_lines):
            return None
        before_lines.extend(head_lines[cursor:start])
        if len(before_lines) != old_start:
            return None
        cursor = start
        removals: list[int] = []
        additions: list[int] = []
        for line in hunk:
            if line.is_context:
                # Pair only one-for-one replacements. Insertions and rewritten
                # blocks cannot establish which earlier call this one replaces.
                if len(removals) == len(additions) == 1:
                    replaced[additions[0]] = removals[0]
                removals.clear()
                additions.clear()
            elif line.is_removed:
                removals.append(len(before_lines) + 1)
            elif line.is_added:
                additions.append(cursor + 1)
                added.add(cursor + 1)
            else:
                return None
            if line.is_context or line.is_added:
                if cursor >= len(head_lines) or head_lines[cursor] != line.value:
                    return None
                cursor += 1
            if line.is_context or line.is_removed:
                before_lines.append(line.value)
        if len(removals) == len(additions) == 1:
            replaced[additions[0]] = removals[0]
    before_lines.extend(head_lines[cursor:])
    before = _revision("".join(before_lines))
    head = _revision(source)
    return _Change(before, head, added, replaced) if before and head else None


def _calls(revision: _Revision) -> dict[int, tuple[str, str, str, ast.Call]]:
    calls = {}
    for wrapper in revision.functions.values():
        body = wrapper.body
        if ast.get_docstring(wrapper) is not None:
            body = body[1:]
        if not body or not isinstance(body[0], (ast.Return, ast.Expr)):
            continue
        call = body[0].value
        if not isinstance(call, ast.Call) or call.lineno != call.end_lineno:
            continue
        if len(body) > 1 and body[1].lineno == call.lineno:
            continue
        target = call.func
        if isinstance(target, ast.Name):
            name, attribute = target.id, None
        elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            name, attribute = target.value.id, target.attr
        else:
            continue
        scope = revision.scopes[wrapper.name, wrapper.lineno]
        if name not in revision.imports or not scope.lookup(name).is_global():
            continue
        module, imported = revision.imports[name]
        if (attribute is None) == (imported is None):
            continue
        function = attribute or imported
        if function is not None:
            calls[call.lineno] = wrapper.name, module, function, call
    return calls


def _signature(function: ast.FunctionDef) -> Signature | None:
    args = function.args
    if args.vararg or args.kwarg:
        return None
    positional = [*args.posonlyargs, *args.args]
    required = len(positional) - len(args.defaults)
    params = [
        Parameter(
            arg.arg,
            Parameter.POSITIONAL_ONLY
            if i < len(args.posonlyargs)
            else Parameter.POSITIONAL_OR_KEYWORD,
            default=Parameter.empty if i < required else None,
        )
        for i, arg in enumerate(positional)
    ]
    params.extend(
        Parameter(
            arg.arg,
            Parameter.KEYWORD_ONLY,
            default=Parameter.empty if default is None else None,
        )
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
    )
    return Signature(params)


def _literal_call(call: ast.Call) -> bool:
    return (
        all(isinstance(arg, ast.Constant) for arg in call.args)
        and all(kw.arg is not None and isinstance(kw.value, ast.Constant) for kw in call.keywords)
        and len({kw.arg for kw in call.keywords}) == len(call.keywords)
    )


def _binding_error(signature: Signature, call: ast.Call) -> str | None:
    try:
        signature.bind(
            *[None for _ in call.args],
            **{kw.arg: None for kw in call.keywords if kw.arg is not None},
        )
    except TypeError as exc:
        return str(exc)
    return None


async def check_python_calls(
    diff_text: str,
    chunk_by_path: dict[str, int],
    repo_paths: set[str],
    fetcher: SourceFetcher,
) -> list[CheckedCall]:
    """Check changed, explicit imports between different review chunks."""
    patch = PatchSet(diff_text)
    known_paths = (
        repo_paths
        | {f.path for f in patch}
        | {f.source_file.removeprefix("a/") for f in patch if f.is_rename}
    )
    files = [
        f
        for f in patch
        if f.path in chunk_by_path
        and f.path.endswith(".py")
        and f.is_modified_file
        and not f.is_rename
    ]
    if not 2 <= len(files) <= _MAX_FILES or len({chunk_by_path[f.path] for f in files}) < 2:
        return []

    async def load(file: PatchedFile) -> _Change | None:
        try:
            source = await fetcher.fetch(file.path)
            if source and len(source) <= _MAX_SOURCE_CHARS:
                return _change(file, source)
        except Exception as exc:
            logger.debug("Python call verification skipped %s: %s", file.path, exc)
        return None

    changes = {
        file.path: change
        for file, change in zip(files, await asyncio.gather(*(load(f) for f in files)), strict=True)
        if change is not None
    }
    checked = []
    for path, change in changes.items():
        old_calls = _calls(change.before)
        for line, (wrapper, module, name, call) in _calls(change.head).items():
            old = old_calls.get(change.replaced.get(line, 0))
            if old is None or old[:3] != (wrapper, module, name):
                continue
            package = PurePosixPath(path).parent
            if module.startswith("."):
                if str(package) == "." or f"{package}/__init__.py" not in repo_paths:
                    continue
                module_path = str(package / module[1:].replace(".", "/"))
            else:
                module_path = module.replace(".", "/")
                sibling = package / module_path
                if str(sibling) != module_path and (
                    f"{sibling}.py" in known_paths or f"{sibling}/__init__.py" in known_paths
                ):
                    continue
            callee = f"{module_path}.py"
            if (
                callee not in changes
                or callee not in repo_paths
                or f"{module_path}/__init__.py" in known_paths
                or chunk_by_path[path] == chunk_by_path[callee]
                or not (_literal_call(old[3]) and _literal_call(call))
            ):
                continue
            target = changes[callee]
            before = target.before.functions.get(name)
            head = target.head.functions.get(name)
            if before is None or head is None or head.lineno not in target.added:
                continue
            before_signature, signature = _signature(before), _signature(head)
            if (
                before_signature is None
                or signature is None
                or before_signature == signature
                or _binding_error(before_signature, old[3]) is not None
            ):
                continue
            checked.append(
                CheckedCall(path, line, callee, head.lineno, _binding_error(signature, call))
            )
            if len(checked) == _MAX_PAIRS:
                return checked
    return checked
