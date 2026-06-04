"""Methods shared verbatim between SQLite and Postgres index stores.

Both `IndexStore` and `PgIndexStore` mix this in. The methods here only call
primitives (`get_summary`, `_load_*`, etc.) that each backend implements;
they don't touch SQL themselves, so they live in one place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mira.index.store import (
        DirectorySummary,
        ExternalRef,
        FileSummary,
    )


class _StoreSharedMixin:
    def get_summaries(self, paths: list[str]) -> dict[str, FileSummary]:
        result: dict[str, FileSummary] = {}
        for path in paths:
            s = self.get_summary(path)  # type: ignore[attr-defined]
            if s is not None:
                result[path] = s
        return result

    def get_directory_summaries(self, paths: list[str]) -> dict[str, DirectorySummary]:
        result: dict[str, DirectorySummary] = {}
        for path in paths:
            ds = self.get_directory_summary(path)  # type: ignore[attr-defined]
            if ds is not None:
                result[path] = ds
        return result

    def upsert_batch(self, summaries: list[FileSummary]) -> None:
        for s in summaries:
            self.upsert_summary(s)  # type: ignore[attr-defined]

    def get_external_refs_for_paths(self, paths: list[str]) -> list[ExternalRef]:
        result: list[ExternalRef] = []
        for path in paths:
            result.extend(self._load_external_refs(path))  # type: ignore[attr-defined]
        return result

    def get_all_review_context_text(self) -> str:
        entries = self.list_review_context()  # type: ignore[attr-defined]
        if not entries:
            return ""
        parts = ["## Repository Documentation Context\n"]
        for entry in entries:
            parts.append(f"### {entry.title}\n{entry.content}\n")
        return "\n".join(parts)

    def get_learned_rules_text(self) -> list[str]:
        rules = self.list_active_learned_rules()  # type: ignore[attr-defined]
        return [r.rule_text for r in rules[:10]]
