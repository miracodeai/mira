# Changelog

All notable changes to Mira are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Mira is fully open source.** All features — including org-wide package
  search, vulnerability scanning, global rules, and learned rules — are
  available to every self-hosted user with no purchase required.
  See [`FEATURES.md`](FEATURES.md).

### Added

- **Decision archaeology** — review prompt now includes recent commit history
  for files touched by the PR, so the LLM can explain *why* code exists
  before suggesting deletion.
- **Learned rules dashboard** at `/learned-rules` — surfaces what Mira has
  synthesized from feedback signals across the org.
- **Vulnerability scanning** via OSV.dev with hourly polling and per-repo CVE
  badges.
- **Org-wide package search** at `/packages` — answer "which repos use
  lodash@4.17.20?" for incident response.
- **Manifest parsing** for `package.json`, `requirements.txt`, `pyproject.toml`,
  `go.mod`, and `Dockerfile` — extracts declared dependency versions
  deterministically (no LLM cost).
- **Streaming walkthrough comments** — placeholder posts within ~1s, narrative
  walkthrough at ~10s, final review with stats once chunk review completes.
- **Confidence clamping** — walkthrough confidence is auto-tightened by review
  findings (a blocker forces "Do not merge" regardless of LLM's initial read).
- **Merge-time learning** — when a PR merges, Mira analyzes accept/reject
  signals and human review comments; LLM synthesizes recurring reviewer
  patterns into rules that inject into future reviews.
- **Cancel indexing** button on the repo detail page.
- **Last-indexed timestamp** in the repo header.

### Fixed

- Bot self-loops where Mira's own walkthrough mentioned the bot name and
  triggered a reply.
- `sync_repos` no longer wipes the entire DB if `list_installations()` fails
  or returns empty.
- `handle_push_index` now updates `updated_at` after incremental re-indexing
  so the "Indexed X ago" timestamp tracks reality.

[Unreleased]: https://github.com/miracodeai/mira/commits/main
