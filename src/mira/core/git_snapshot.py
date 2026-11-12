"""Read the committed branch diff and its source from a local Git checkout."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from mira.exceptions import MiraError
from mira.index.conventions import extract_conventions, is_conventions_file

_MAX_SOURCE_BYTES = 1024 * 1024


class GitSnapshot:
    """A branch diff against its merge base, with source pinned to the same HEAD."""

    def __init__(self, base_ref: str) -> None:
        self._cwd = Path.cwd()
        self._head = self._git("rev-parse", "--verify", "HEAD^{commit}").decode().strip()
        base = self._git("rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}")
        self._base = self._git("merge-base", base.decode().strip(), self._head).decode().strip()

        self._blobs: dict[str, str] = {}
        self._reads: dict[str, asyncio.Task[str | None]] = {}
        for entry in self._git("ls-tree", "--full-tree", "-rlz", self._head).split(b"\0"):
            if not entry:
                continue
            metadata, path = entry.split(b"\t", 1)
            mode, _, oid, size = metadata.split()
            if mode in (b"100644", b"100755") and size.isdigit() and int(size) <= _MAX_SOURCE_BYTES:
                self._blobs[path.decode("utf-8", errors="surrogateescape")] = oid.decode("ascii")

    def _git(self, *args: str) -> bytes:
        try:
            result = subprocess.run(
                ["git", "--no-replace-objects", *args],
                cwd=self._cwd,
                env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
                capture_output=True,
                check=True,
            )
        except FileNotFoundError as exc:
            raise MiraError("Git is required for local branch review.") from exc
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.decode("utf-8", errors="replace").strip()
            raise MiraError(
                f"Could not read the local Git snapshot: {detail or 'no result'}"
            ) from exc
        return result.stdout

    def diff(self) -> str:
        """Return the branch changes since it diverged from the requested base."""
        return self._git(
            "-c",
            "core.quotePath=false",
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--no-relative",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--submodule=short",
            self._base,
            self._head,
            "--",
        ).decode("utf-8", errors="replace")

    def paths(self) -> list[str]:
        """List regular source candidates from the pinned tree."""
        return list(self._blobs)

    async def fetch(self, path: str) -> str | None:
        """Read a committed regular file by its exact tree path."""
        oid = self._blobs.get(path)
        if oid is None:
            return None
        read = self._reads.get(path)
        if read is None:
            read = self._reads[path] = asyncio.create_task(self._read_blob(path, oid))
        # A cancelled chunk should not cancel the read for other chunks.
        return await asyncio.shield(read)

    async def _read_blob(self, path: str, oid: str) -> str | None:
        try:
            content = await asyncio.to_thread(self._git, "cat-file", "blob", oid)
        except MiraError:
            self._reads.pop(path, None)
            raise
        if b"\0" in content:
            return None
        return content.decode("utf-8", errors="replace")

    async def conventions(self) -> str:
        """Extract the same repository convention documents used by hosted reviews."""
        contents = {}
        for path in self._blobs:
            if is_conventions_file(path) and (content := await self.fetch(path)):
                contents[path] = content
        return extract_conventions(contents)
