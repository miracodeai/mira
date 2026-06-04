# Changelog

All notable changes to Mira are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.2] — 2026-06-03

### Added

- **Blast Radius toggle** — new `review.blast_radius` setting (default on) with a Review-settings switch in the dashboard. Turns the walkthrough's cross-repo "Blast Radius" section on or off; when off, the relationship-store lookup is skipped entirely.
- **Loading skeletons** on the Dashboard, Repositories, and Vulnerabilities pages, so a slow data fetch no longer looks identical to an empty result.
- **Light-mode logo** — the dashboard logo now swaps with the theme across the sidebar, login, setup, and setup modal.

### Fixed

- **Learned rules now survive self-critique** — the self-critique pass was discarding review comments that enforced a team's own learned/custom rules (e.g. "we always want tests") as style nits. The critic now sees the active rules and keeps comments that enforce them.
- **Dashboard version indicator** — the version under the sidebar logo queried `/api/version` against the dev server without credentials and never rendered; now fixed.

### Changed

- Internal code-hygiene pass: trimmed redundant comments and split the review passes out of `engine.py` into `core/passes.py` and `core/threads.py`. No behaviour change.

## [0.2.1] — 2026-06-02

### Added

- **AWS Bedrock provider** — set `llm.provider: "bedrock"` to run reviews against Claude (or other models) on Amazon Bedrock via the Converse API, instead of an OpenAI-compatible endpoint. Auth uses the standard AWS credential chain (env vars, instance profile, ECS task role, SSO), with an optional `aws_profile`. Configurable `region` and `fallback_model`. See [Choosing a model](https://docs.miracode.ai/configuration/models#aws-bedrock).

### Changed

- Dependency bumps (recharts, lucide-react, prettier, typescript-eslint, eslint-plugin-react-hooks, shadcn, docker/metadata-action) and README updates.

## [0.2.0] — 2026-05-14

### Added

- **Custom LLM endpoints** — `llm.base_url` and `llm.api_key_env` in `.mira.yaml` let you point Mira at any OpenAI-compatible chat-completions API. Out-of-the-box examples for **vLLM**, **Ollama**, **LiteLLM proxy**, **LocalAI**, **llama.cpp server**, **Together**, **Fireworks**, **Groq**, and **Cerebras**. Defaults still target OpenRouter — existing configs keep working unchanged. Set `api_key_env: ""` for local endpoints that need no auth. OpenRouter-specific ranking headers (`HTTP-Referer`, `X-Title`) are only sent when targeting OpenRouter.
- **`@miracodeai help` command** — posts an inline command list on the PR. Aliases: `?`, `commands`. New [Commands docs page](https://docs.miracode.ai/commands) documents every verb (`review`, `review-rest`, `pause`, `resume`, `help`, free-form Q&A on PRs; `reject`/`dismiss`/`resolve`/`ignore` on review threads; `ignore` in PR body).
- **Benchmark section in README** — Mira's speed/quality position on the [public Code Review Bench](https://codereview.withmartian.com/?mode=offline), with a Pareto-frontier scatter plot and per-language F1 bars. Chart generator at `scripts/render_benchmark_charts.py` (one-off `uv run --with matplotlib`; no new runtime dependency).
- **`docs.miracode.ai` badge** in the README, next to the Discord badge.

## [0.1.1] — 2026-05-11

### Added

- **Layered config: `mira serve --config /path/to/mira.yaml`** — deployment-wide YAML defaults loaded once at startup. Per-repo `.mira.yaml` deep-merges over it; admin UI overrides layer between the two. Replaces the env-var grab-bag for non-secret settings; secrets stay in env. (`MIRA_CONFIG` env var also accepted.)
- **Admin Settings → Review behaviour overrides** — DB-backed runtime overrides for `filter` and `review` knobs (confidence threshold, max comments, walkthrough, self-critique, security pass, max concurrent chunks). Editable from the dashboard with field-level validation, inline error messages, bounded inputs, and "Overrides `mira.yaml`" badges.
- **`/api/admin/settings`** GET/PUT (admin-only) and **`/api/version`** endpoints.
- **Version chip under the dashboard logo** — shows the running Mira version at a glance.
- **Auto-detected bot `@mention`** — `mira serve` reads the GitHub App's slug from `GET /app` at startup; `MIRA_BOT_NAME` is now optional and only needed for overrides or when the lookup fails.
- **LiteLLM-style Docker invocation** — `ENTRYPOINT ["mira", "serve"]` so `docker run … image --config /app/mira.yaml` passes through cleanly.
- **TLS termination examples** in the docs — Caddy, nginx + Let's Encrypt, Cloudflare Tunnel.
- **Vulnerabilities page collapses repeats by package** — multiple advisories against the same `(repo, package, version)` collapse to one row with the highest required upgrade target in a new "Upgrade to" column and an advisory-count chip. Click to expand for the individual GHSAs.
- **Changelog button next to the docs logo** — History-icon chip linking to the changelog page.

### Changed

- **`@miracodeai` is the canonical bot mention** — docs/README updated everywhere from the old `@mira-bot` placeholder.
- **Walkthrough nudge no longer fires on indexed repos** — split `_index_was_empty` (whole-repo signal) from `_jit_needed` (per-PR signal). PRs that touch only files the indexer skips (e.g. `README.md`) no longer falsely tell users "this repo isn't indexed."
- **Inline review comments stopped failing with 422** — reverted forced `side: RIGHT` / `start_side: RIGHT` on review-comment payloads; let GitHub auto-infer side from the diff.
- **Mermaid sequence diagrams render cleanly** — removed the duplicate sanitizer in `models.to_markdown` that was re-introducing the nested-quote bug `_sanitize_mermaid` had just fixed.
- **`agentic_tools._grep_repo` capped at 15 files** (was 60) to bound the per-grep network spend.
- **Postgres `set_last_reviewed_sha`** got an explicit `commit()` mirroring the SQLite branch (defense in depth — the connection is autocommit, but explicit is safer).
- **Sidebar item count + version chip** in the dashboard reads `/api/version` so admins can confirm what's deployed.
- **`.mira.yml` → `.mira.yaml`** everywhere in docs and code paths; legacy `.mira.yml` is still read for backward-compat.
- **Dashboard "Repositories" card** subtitle reads "N repository relationships" (was "N cross-repo edges") — clearer wording, same underlying count.
- **Repo detail page stat cards** — "Symbols" replaced with "Lines of code" (sums per-file `loc`); "External Refs" renamed "External references" (the metric covers npm/pip/go packages, Docker images, Terraform modules, and outbound API endpoints, not just package calls).
- **Breadcrumb owner segment** on `/repos/{owner}/{repo}` now links to `/repos?owner={owner}`; the repos page seeds its filter from that query param.

### Fixed

- Validation errors from `/api/admin/settings` now surface as humanized, field-keyed messages (`Confidence threshold must be ≤ 1.0`) instead of raw Pydantic stacks.
- Number inputs on the Settings page handle decimal entry, backspace, and arrow-key stepping correctly.
- **Setup modal stops re-appearing after "Skip for now"** — the popup trigger now also checks `index_mode !== "none"`, so an explicit skip persists across reloads instead of nagging on every refresh.
- **`_run_initial_indexing` no longer re-indexes already-ready repos** when a later install lands — filters by `status in ("pending", "indexing")` rather than blindly walking every repo with a non-`none` index mode.

## [0.1.0] — 2026-04-29

Initial public release.

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

[0.2.2]: https://github.com/miracodeai/mira/releases/tag/v0.2.2
[0.2.1]: https://github.com/miracodeai/mira/releases/tag/v0.2.1
[0.2.0]: https://github.com/miracodeai/mira/releases/tag/v0.2.0
[0.1.1]: https://github.com/miracodeai/mira/releases/tag/v0.1.1
[0.1.0]: https://github.com/miracodeai/mira/releases/tag/v0.1.0
