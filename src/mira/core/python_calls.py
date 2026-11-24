"""Verify introduced argument-binding errors for explicit Python imports."""

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
    from collections.abc import Awaitable, Callable, Iterator

    from mira.index.context import SourceFetcher

logger = logging.getLogger(__name__)
SOURCE_PASS = "python-call"
_MAX_FILES = 12
_MAX_SOURCE_CHARS = 128_000
_MAX_PAIRS = 16
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SCOPES = (
    *_FUNCTIONS,
    ast.ClassDef,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)
_ImportBinding = tuple[int, str, str | None]


@dataclass
class _Revision:
    module: ast.Module
    imports: dict[str, _ImportBinding]
    mutated_imports: set[_ImportBinding]
    functions: dict[str, ast.FunctionDef]
    scope: symtable.SymbolTable


@dataclass
class _Change:
    before: _Revision
    head: _Revision
    added: set[int]
    removed: set[int]
    covered: set[int]
    old_line: dict[int, int]


@dataclass(frozen=True)
class _Call:
    scope: str
    callee: str
    name: str
    node: ast.Call


@dataclass(frozen=True)
class _SignatureChange:
    before: Signature
    head: Signature
    anchor: int


@dataclass(frozen=True)
class CheckedCall:
    caller: str
    line: int
    callee: str
    signature_line: int
    error: str | None
    signature_anchor: bool = False

    def comment(self) -> ReviewComment | None:
        if self.error is None:
            return None
        if self.signature_anchor:
            path, line = self.callee, self.signature_line
            evidence = f"The unchanged call at `{self.caller}:{self.line}`"
            target = "this committed signature"
        else:
            path, line = self.caller, self.line
            evidence = "The committed call"
            target = f"the signature at `{self.callee}:{self.signature_line}`"
        return ReviewComment(
            path=path,
            line=line,
            end_line=None,
            severity=Severity.BLOCKER,
            category="bug",
            title="Python call no longer binds to its signature",
            body=(
                f"{evidence} cannot bind to {target}: {self.error}. "
                "Update this call or preserve a compatible signature."
            ),
            confidence=1.0,
            source_pass=SOURCE_PASS,
        )


def _walk_scope(nodes: list[ast.stmt]) -> Iterator[ast.AST]:
    pending: list[ast.AST] = list(reversed(nodes))
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, _SCOPES):
            pending.extend(reversed(list(ast.iter_child_nodes(node))))


def _revision(source: str) -> _Revision | None:
    module = ast.parse(source)
    imports: dict[str, _ImportBinding] = {}
    import_bindings: list[tuple[str, _ImportBinding]] = []
    functions: dict[str, ast.FunctionDef] = {}
    import_counts: dict[str, int] = {}
    declarations: dict[str, int] = {}
    written: set[str] = set()
    mutated: set[str] = set()
    for node in _walk_scope(module.body):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    return None
                name = alias.asname or alias.name.split(".")[0]
                import_counts[name] = import_counts.get(name, 0) + 1
                if isinstance(node, ast.Import) and (alias.asname or "." not in alias.name):
                    import_bindings.append((name, (0, alias.name, None)))
                elif isinstance(node, ast.ImportFrom) and node.module:
                    import_bindings.append((name, (node.level, node.module, alias.name)))
        elif isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
            declarations[node.name] = declarations.get(node.name, 0) + 1
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            written.add(node.id)
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            written.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            written.add(node.rest)

    # Attribute and global writes make the affected binding ambiguous.
    for node in ast.walk(module):
        if isinstance(node, ast.Global):
            written.update(node.names)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and isinstance(node.ctx, (ast.Store, ast.Del))
        ):
            mutated.add(node.value.id)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"setattr", "delattr"}
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            mutated.add(node.args[0].id)

    written.update(mutated)
    for node in module.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name
                if import_counts.get(name) != 1 or name in written or name in declarations:
                    continue
                if isinstance(node, ast.Import):
                    imports[name] = (0, alias.name, None)
                elif node.module:
                    imports[name] = (node.level, node.module, alias.name)
        elif (
            isinstance(node, ast.FunctionDef)
            and not node.decorator_list
            and node.name not in written
            and node.name not in import_counts
            and declarations[node.name] == 1
        ):
            functions[node.name] = node
    mutated_imports = {binding for alias, binding in import_bindings if alias in mutated}
    return _Revision(
        module, imports, mutated_imports, functions, symtable.symtable(source, "", "exec")
    )


def _change(file: PatchedFile, source: str) -> _Change | None:
    """Reconstruct the old side only when the diff matches the pinned head."""
    head_lines = source.splitlines(keepends=True)
    before_lines: list[str] = []
    added: set[int] = set()
    removed: set[int] = set()
    covered: set[int] = set()
    old_line: dict[int, int] = {}
    cursor = 0

    def copy_context(end: int) -> None:
        nonlocal cursor
        for i in range(cursor, end):
            old_line[i + 1] = len(before_lines) + 1
            before_lines.append(head_lines[i])
        cursor = end

    removals: list[int] = []
    additions: list[int] = []

    def pair_replacements() -> None:
        if len(removals) == len(additions):
            old_line.update(zip(additions, removals, strict=True))
        elif removals and additions:
            # Keep the start of a call that changed from one line to several or vice versa.
            old_line[additions[0]] = removals[0]
        removals.clear()
        additions.clear()

    for hunk in file:
        start = hunk.target_start - 1 if hunk.target_length else hunk.target_start
        old_start = hunk.source_start - 1 if hunk.source_length else hunk.source_start
        if not cursor <= start <= len(head_lines):
            return None
        copy_context(start)
        if len(before_lines) != old_start:
            return None
        for line in hunk:
            if line.is_context:
                pair_replacements()
            elif line.is_removed:
                removals.append(len(before_lines) + 1)
                removed.add(len(before_lines) + 1)
            elif line.is_added:
                additions.append(cursor + 1)
                added.add(cursor + 1)
            else:
                return None
            if line.is_context or line.is_added:
                if cursor >= len(head_lines) or head_lines[cursor] != line.value:
                    return None
                covered.add(cursor + 1)
                if line.is_context:
                    old_line[cursor + 1] = len(before_lines) + 1
                cursor += 1
            if line.is_context or line.is_removed:
                before_lines.append(line.value)
        pair_replacements()
    copy_context(len(head_lines))
    before = _revision("".join(before_lines))
    head = _revision(source)
    return _Change(before, head, added, removed, covered, old_line) if before and head else None


def _scoped_functions(
    nodes: list[ast.stmt], table: symtable.SymbolTable, prefix: str = ""
) -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef, symtable.SymbolTable]]:
    scopes = {(child.get_name(), child.get_lineno()): child for child in table.get_children()}
    for node in nodes:
        if isinstance(node, (*_FUNCTIONS, ast.ClassDef)):
            scope = scopes.get((node.name, node.lineno))
            if scope is None:
                continue
            if isinstance(node, ast.ClassDef):
                yield from _scoped_functions(node.body, scope, prefix + node.name + ".")
            else:
                yield prefix + node.name, node, scope


def _module_file(
    path: str,
    binding: _ImportBinding,
    *,
    as_module: bool,
    known_paths: set[str],
) -> str | None:
    level, module, imported = binding
    package = PurePosixPath(path).parent
    if level > len(package.parts):
        return None
    root = package.parts[: len(package.parts) - level + 1] if level else ()
    module_path = str(PurePosixPath(*root, *module.split(".")))
    if as_module and imported is not None:
        # A package initializer could export an object in place of the submodule.
        if f"{module_path}/__init__.py" in known_paths or f"{module_path}.py" in known_paths:
            return None
        module_path = f"{module_path}/{imported}"
    elif not as_module and imported is None:
        return None
    if f"{module_path}/__init__.py" in known_paths:
        return None
    sibling = package / module_path
    if (
        not level
        and str(sibling) != module_path
        and (f"{sibling}.py" in known_paths or f"{sibling}/__init__.py" in known_paths)
    ):
        return None
    return f"{module_path}.py"


def _calls(revision: _Revision, path: str, known_paths: set[str]) -> dict[int, _Call]:
    calls: dict[int, _Call] = {}
    ambiguous = set()
    # An attribute write through one import alias affects the module seen by all aliases.
    mutated_files = {
        _module_file(path, binding, as_module=True, known_paths=known_paths)
        for binding in revision.mutated_imports
    }
    for scope_name, wrapper, scope in _scoped_functions(revision.module.body, revision.scope):
        for node in _walk_scope(wrapper.body):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            if isinstance(target, ast.Name):
                alias, attribute = target.id, None
            elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                alias, attribute = target.value.id, target.attr
            else:
                continue
            if alias not in revision.imports or not scope.lookup(alias).is_global():
                continue
            binding = revision.imports[alias]
            callee = _module_file(
                path, binding, as_module=attribute is not None, known_paths=known_paths
            )
            name = attribute or binding[2]
            if (
                callee is None
                or callee in mutated_files
                or name is None
                or node.lineno in ambiguous
            ):
                continue
            if node.lineno in calls:
                ambiguous.add(node.lineno)
                del calls[node.lineno]
                continue
            calls[node.lineno] = _Call(scope_name, callee, name, node)
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


def _signature_changes(changes: dict[str, _Change]) -> dict[tuple[str, str], _SignatureChange]:
    signatures = {}
    for path, change in changes.items():
        for name, head in change.head.functions.items():
            before = change.before.functions.get(name)
            if before is None:
                continue
            head_span = set(range(head.lineno, max(head.lineno + 1, head.body[0].lineno)))
            before_span = set(range(before.lineno, max(before.lineno + 1, before.body[0].lineno)))
            anchors = (head_span & change.added) or (head_span & change.covered)
            if not anchors or not (head_span & change.added or before_span & change.removed):
                continue
            old_signature, new_signature = _signature(before), _signature(head)
            if (
                old_signature is not None
                and new_signature is not None
                and old_signature != new_signature
            ):
                signatures[path, name] = _SignatureChange(
                    old_signature, new_signature, max(anchors)
                )
    return signatures


def _binding_error(signature: Signature, call: ast.Call) -> str | None:
    try:
        signature.bind(
            *[None for _ in call.args],
            **{kw.arg: None for kw in call.keywords if kw.arg is not None},
        )
    except TypeError as exc:
        return str(exc)
    return None


def _check_call(
    path: str,
    line: int,
    old: _Call,
    new: _Call,
    signatures: dict[tuple[str, str], _SignatureChange],
    *,
    unchanged: bool = False,
) -> CheckedCall | None:
    if (old.scope, old.callee, old.name) != (new.scope, new.callee, new.name):
        return None
    for call in (old.node, new.node):
        keys = [kw.arg for kw in call.keywords]
        if (
            any(isinstance(a, ast.Starred) for a in call.args)
            or None in keys
            or len(set(keys)) != len(keys)
        ):
            return None
    signature = signatures.get((new.callee, new.name))
    if signature is None or _binding_error(signature.before, old.node) is not None:
        return None
    return CheckedCall(
        path,
        line,
        new.callee,
        signature.anchor,
        _binding_error(signature.head, new.node),
        signature_anchor=unchanged,
    )


async def check_python_calls(
    diff_text: str,
    chunk_by_path: dict[str, int],
    repo_paths: set[str],
    fetcher: SourceFetcher,
    unchanged_importers: Callable[[list[str]], Awaitable[list[str]]] | None = None,
) -> list[CheckedCall]:
    """Check changed calls across chunks and optionally search proven unchanged importers."""
    patch = PatchSet(diff_text)
    changed_paths = {f.path for f in patch} | {
        f.source_file.removeprefix("a/") for f in patch if f.is_rename
    }
    known_paths = repo_paths | changed_paths
    files = [
        f
        for f in patch
        if f.path in chunk_by_path
        and f.path.endswith(".py")
        and f.is_modified_file
        and not f.is_rename
    ]
    cross_chunk = len({chunk_by_path[f.path] for f in files}) >= 2
    if not files or len(files) > _MAX_FILES or not (cross_chunk or unchanged_importers):
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
    signatures = _signature_changes(changes)
    if not signatures:
        return []
    checked = []
    if cross_chunk:
        for path, change in changes.items():
            old_calls = _calls(change.before, path, known_paths)
            for line, new in _calls(change.head, path, known_paths).items():
                old = old_calls.get(change.old_line.get(line, 0))
                if (
                    old is None
                    or new.callee not in chunk_by_path
                    or chunk_by_path[path] == chunk_by_path[new.callee]
                ):
                    continue
                span = set(range(line, (new.node.end_lineno or line) + 1))
                old_span = set(range(old.node.lineno, (old.node.end_lineno or old.node.lineno) + 1))
                if not (span & change.added or old_span & change.removed):
                    continue
                anchors = span & change.covered
                if anchors and (result := _check_call(path, min(anchors), old, new, signatures)):
                    checked.append(result)
                    if len(checked) >= _MAX_PAIRS:
                        return checked

    if unchanged_importers is not None:
        modules = sorted({path.removesuffix(".py").replace("/", ".") for path, _ in signatures})
        try:
            importers = await unchanged_importers(modules)
        except Exception as exc:
            logger.debug("Unchanged Python importer lookup unavailable: %s", exc)
            return checked
        reported: set[tuple[str, int]] = set()
        for path in importers:
            if path in changed_paths or path not in repo_paths:
                continue
            try:
                source = await fetcher.fetch(path)
                revision = (
                    _revision(source) if source and len(source) <= _MAX_SOURCE_CHARS else None
                )
            except Exception as exc:
                logger.debug("Python importer %s skipped: %s", path, exc)
                continue
            if revision is None:
                continue
            for line, call in _calls(revision, path, known_paths).items():
                result = _check_call(path, line, call, call, signatures, unchanged=True)
                if result is None:
                    continue
                anchor = result.callee, result.signature_line
                if result.error and anchor in reported:
                    continue
                if result.error:
                    reported.add(anchor)
                checked.append(result)
                if len(checked) >= _MAX_PAIRS:
                    return checked
    return checked
