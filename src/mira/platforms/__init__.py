"""Code-hosting platform layer: profiles, auth, and repo fetching.

Mira talks to multiple git platforms (GitHub, GitLab) through three seams:
- ``profiles`` — declarative per-platform wire data (api urls, webhook scheme).
- ``auth`` — the ``PlatformAuth`` protocol and its implementations.
- ``fetch`` — the ``RepoFetcher`` protocol used by the indexer.

The PR-review path itself goes through ``mira.providers`` (the ``BaseProvider``
registry); this package holds the platform plumbing around it.
"""
