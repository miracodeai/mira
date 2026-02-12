"""Token-aware chunking for large PRs."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from mira.models import FileDiff, ReviewChunk

if TYPE_CHECKING:
    from mira.llm.provider import LLMProvider


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token."""
    return len(text) // 4


def _file_token_estimate(file_diff: FileDiff) -> int:
    """Estimate tokens for a file diff."""
    total = len(file_diff.path) // 4 + 20  # path + overhead
    for hunk in file_diff.hunks:
        total += _estimate_tokens(hunk.content)
    return total


def chunk_files(
    files: list[FileDiff],
    max_tokens: int,
    provider: LLMProvider | None = None,
) -> list[ReviewChunk]:
    """Split files into chunks that fit within token limits.

    Uses greedy first-fit-decreasing: sort files by estimated token count
    (largest first), then fit each into the first chunk with room.
    Single oversized files get their own chunk with trailing hunks dropped.
    """
    if not files:
        return []

    # Reserve tokens for system prompt and response
    prompt_overhead = 2000
    available = max_tokens - prompt_overhead

    # Compute estimates
    estimates: list[tuple[FileDiff, int]] = []
    for f in files:
        if provider:
            est = provider.count_tokens("\n".join(h.content for h in f.hunks))
        else:
            est = _file_token_estimate(f)
        estimates.append((f, est))

    # Sort largest first
    estimates.sort(key=lambda x: x[1], reverse=True)

    chunks: list[ReviewChunk] = []

    for file_diff, est in estimates:
        # Handle oversized files
        if est > available:
            truncated = _truncate_file(file_diff, available)
            chunks.append(
                ReviewChunk(
                    files=[truncated],
                    token_estimate=available,
                )
            )
            continue

        # Find first chunk with room
        placed = False
        for chunk in chunks:
            if chunk.token_estimate + est <= available:
                chunk.files.append(file_diff)
                chunk.token_estimate += est
                placed = True
                break

        if not placed:
            chunks.append(
                ReviewChunk(
                    files=[file_diff],
                    token_estimate=est,
                )
            )

    return chunks


def _truncate_file(file_diff: FileDiff, max_tokens: int) -> FileDiff:
    """Truncate a file by dropping trailing hunks to fit within token limit."""
    kept_hunks = []
    tokens_used = 20  # overhead

    for hunk in file_diff.hunks:
        hunk_tokens = _estimate_tokens(hunk.content)
        if tokens_used + hunk_tokens > max_tokens:
            break
        kept_hunks.append(hunk)
        tokens_used += hunk_tokens

    # Always keep at least the first hunk
    if not kept_hunks and file_diff.hunks:
        kept_hunks = [file_diff.hunks[0]]

    return replace(file_diff, hunks=kept_hunks)
