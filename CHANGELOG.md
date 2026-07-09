# Changelog

All notable changes to Mira are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0] — 2026-07-09

### Added

- **GitLab support — full parity with GitHub.** Mira now auto-reviews merge requests via webhooks, posts inline review comments anchored to diff positions (with a plain-note fallback when GitLab rejects a position), and answers `@mention` commands on MRs: `review`, `review-rest`, `pause`/`resume`, inline `reject`, free-form questions, and merge-time learning. Round 2+ incremental re-review, thread auto-resolution once a finding is fixed, JIT cross-file context, file-history context, codebase indexing, incremental push indexing, and vulnerability/package search all work the same as on GitHub. Authentication is a group or project access token — set `MIRA_GITLAB_TOKEN` + `MIRA_GITLAB_WEBHOOK_SECRET` and point a project webhook at `/gitlab/webhook`; self-managed instances set `MIRA_GITLAB_API_URL`. GitHub App credentials are now optional when GitLab is configured (set either, or both). See the [GitLab setup guide](https://docs.miracode.ai/gitlab).
- **Dynamic platform layer.** Adding a git host is now data, not code: a `platforms.json` registry (mirroring `providers.json` for LLMs) carries each platform's API URL, webhook route, signature scheme, and terminology, overridable at runtime via `MIRA_PLATFORMS_JSON_PATH`. The repo data model extends to `(platform, owner, repo)`, so same-named repos coexist across hosts; existing GitHub rows migrate automatically with `platform = github`.
- **Mira answers to both its handles.** On either platform, a mention of the configured `MIRA_BOT_NAME` *or* the bot's real account username (GitHub App slug / GitLab token user) triggers a command — whichever a teammate types.

### Changed

- **Review comment headers use one severity marker instead of two emojis.** The category line is now plain bold text (`**Security issue**`) above the severity badge, rather than a second leading emoji. Cleaner, still scannable, and it renders correctly on both GitHub and GitLab (GitLab needed a Markdown hard break where GitHub tolerated a bare newline).

### Fixed

- **Malformed LLM JSON is recovered instead of dropped.** Responses that leak Anthropic tool-call XML (`</parameter></invoke>`) or arrive with an unbalanced brace are now repaired before parsing, so a chunk's comments are no longer lost when a model's output strays from clean JSON. Affects both the main and security passes.

## [0.5.1] — 2026-07-09

### Fixed

- **One malformed walkthrough entry no longer drops the whole walkthrough** — when the LLM omits a required `path` or `label` on a single `change_groups` entry (plausible on large diffs), that entry is now skipped with a warning instead of failing the entire response, so the walkthrough comment still posts. Closes #162.
- **The walkthrough placeholder is always finalized** — when a review produces no walkthrough (all files matched exclusion rules, empty diff, size limits, or walkthrough generation failed), the "Reviewing this PR…" placeholder comment is now updated with the reason instead of staying stuck forever. Part of #162.

## [0.5.0] — 2026-07-07

### Added

- **Learnings approval queue** — auto-synthesized learnings now land as *pending* and must be approved by an admin before they influence reviews. Approve or reject from a queue-clearing side panel or straight from the dashboard's pending-learnings widget.
- **Anyone can propose a learning** — non-admin submissions are created as pending; creators can edit or delete their own pending learnings while admins manage everything. Learnings track their author and show an avatar + username.
- **Learnings page overhaul** — dedicated add/edit page (replacing the modal), sortable and paginated table (recently-updated first), repo/status/enabled filters with search, URL-driven tabs so browser Back works, and GitHub links per repo.
- **Newest frontier models in the registry** — Claude Sonnet 5, Claude Opus 4.8, Claude Fable 5, GPT-5.2, Gemini 3.1 Pro Preview, DeepSeek V4 Flash/Pro, and MiniMax M3, with pricing and ids verified against the live OpenRouter catalog.

### Changed

- **Superseded registry entries removed** — GPT-4o and GPT-4o mini, GPT-4.1 Mini, Gemini 2.5 Flash/Pro, MiniMax M2.7, and the OpenRouter Claude Opus 4.6 id (the Bedrock profile remains). Deployments still configured with these keep working — the dashboard accepts any id the configured backend serves; they just lose curated pricing metadata.
- **Recommended defaults unchanged** — Claude Sonnet 4.6 for reviews and Claude Haiku 4.5 for indexing were benchmarked on the review-quality baseline and keep the badge until a newer model measurably beats them.

### Fixed

- **Dark mode dropdown contrast** — popovers and select menus use an elevated surface color instead of blending invisibly into the card behind them.
- **Pending learnings are private** — the learned-rule detail endpoint requires authentication and returns 403 when a non-admin reads someone else's pending/rejected learning.
- The learnings panel advances to the next pending item only after the approve/reject call resolves.
- Database inserts that fail to produce a row id now raise instead of silently returning id 0 (learned rules, feedback events).
- The learned-rules API client URL-encodes the status query parameter.

## [0.4.1] — 2026-07-06

### Added

- **Activity page** — an org-wide feed of PR reviews with a filterable table and a per-PR detail timeline. (#145, #146, #147 — already in the 0.4.1 betas.)
- **Backend-aware, searchable model pickers** — the dashboard's model dropdowns are now search bars that list the configured backend's live catalog (OpenRouter's tool-capable models, your Bedrock account's inference profiles, or a generic endpoint's `/models`), cached for an hour with the bundled registry as fallback. Any free-form model id can be typed directly, matching `mira.yaml`'s flexibility, and an **Inherit from deployment config** choice clears the dashboard override so `mira.yaml` is authoritative again. The effective model and its source (`dashboard setting` vs `mira.yaml`) are logged on every review, so an override is never silent. Closes #124.
- **Current-generation models in the registry** — GPT-5 Nano/Mini, GPT-5.1 Codex/Codex Mini, GPT-4.1 Mini, Gemini 3 Flash (Preview), and Gemini 3.1 Flash Lite, with pricing verified against the live OpenRouter catalog. Closes #125.

### Changed

- **`llm.base_url` is validated at config load** — non-http(s) schemes are rejected, and plain `http://` is allowed only for local endpoints (localhost, private IPs, dotless hostnames like docker-compose services); public hosts must use `https://`. A failed API-key lookup during a model-catalog fetch is now logged instead of silently sending an unauthenticated request.

### Fixed

- **Discord webhooks work via the Slack-compatible endpoint** — a `discord.com/api/webhooks/{id}/{token}/slack` URL is now detected as Slack format instead of falling through to generic JSON (which Discord rejects). Bare Discord URLs stay generic on purpose, so the test button surfaces the mismatch instead of silently guessing. Closes #158.
- **Dependency-bump pushes refresh the vulnerability inventory** — push-triggered incremental indexing skipped manifests/lockfiles entirely (they're excluded from code indexing), so merging a lockfile-only PR left `package_manifests` stale and the Vulnerabilities page kept flagging already-fixed advisories until a full re-index. `index_diff` now routes changed/removed manifests through the same parse-and-store pass the full indexer uses and fires an immediate OSV poll. Closes #157.
- **Global rules now reach reviews on Postgres deployments** — the review engine read global rules from a throwaway SQLite `AppDatabase()` instead of the configured `DATABASE_URL` backend, so dashboard-stored rules were silently ignored, a stray `_app.db` (with a default-password admin) was created in the index dir, and a DB connection leaked on every review. It now reuses the server's configured instance. Closes #123.

## [0.4.0] — 2026-06-14

### Added

- **Provider profiles — adding an LLM provider is now data, not code.** The OpenAI-compatible client's per-provider quirks (attribution headers, whether the model id keeps its `vendor/` prefix, reasoning-effort remapping, default key env var) moved out of hardcoded `if openrouter` branches into a declarative `providers.json`, matched to the configured `base_url`. OpenRouter's quirks are now a single profile entry rather than code branches; any endpoint with no matching profile gets the portable default (bare model name, no extra headers), so most OpenAI-compatible providers work with nothing but `base_url` + `api_key_env`. Extend or override the bundled list at runtime by pointing `MIRA_PROVIDERS_JSON_PATH` at your own file — same idiom as `MIRA_MODELS_JSON_PATH` for models. No behaviour change for existing configs.
- **`exclude_files` apply to indexing** — `filter.exclude_patterns` now governs the index as well as review, so committed vendor dirs, generated SDKs, and test data can be kept out of indexing without burning tokens on them. The same globs that exclude a file from review exclude it from indexing; the dashboard's per-repo file count reflects the exclusions too. Closes #97.
- **Indexing file-size limit** — new `index.max_file_size` (bytes, default 1 MB) skips any file above the limit before it reaches the summarizer. Lower it to keep indexing cheap on large codebases with big fixtures or generated files; `0` disables the limit. Replaces the previous hard-coded 1 MB tarball cap and now also covers the per-file fetch path. Closes #98.

### Fixed

- **Indexing no longer drops a whole batch on one malformed file** — summarization responses with unescaped backslashes (e.g. DeepSeek emitting PHP namespaces like `\App\Models` or Windows paths inside JSON strings) are repaired before parsing, so a single bad string no longer fails `json.loads` and discards every file in the batch. Parsing is also lenient about raw control characters. Closes #96.
- **Indexing no longer crashes on a null symbol field** — a model emitting an explicit `"signature": null` (or null `kind` / `description`) was inserting `NULL` into a `NOT NULL` column and aborting the file. Those fields are now coerced to their defaults, and symbols with no name are skipped. Part of #96.

## [0.3.1] — 2026-06-11

### Added

- **Review thinking mode** — an extended-reasoning budget for reviews (`off` / `low` / `medium` / `high` / `max`), so a model spends more effort before commenting. Set it in `mira.yaml` (`llm.review_reasoning_effort`) or via the Review Model section on the Settings page; it applies to reviews only and defaults to off. Works on OpenRouter (DeepSeek, Claude, and OpenAI reasoning models) and on Bedrock for Claude; on a model or endpoint that doesn't support a reasoning effort it's dropped automatically so the review still runs. (`max` is DeepSeek's top level — sent as `xhigh` on OpenRouter.)
- **Runtime-adjustable model registry** — point `MIRA_MODELS_JSON_PATH` at your own `models.json` to add custom models (a cost-effective DeepSeek/MiniMax entry, a local endpoint, …) or override bundled ones, without reinstalling. Entries overlay the bundled list by id; a missing or invalid file is ignored with a warning, and a partial entry falls back to default pricing rather than crashing. Closes #83.

### Changed

- The eval suite is now hermetic for reliable release gating — the eval engine pins its filter config so ambient dashboard/DB overrides can't change what the tests see, the planted-issue catch tests retry to absorb model variance, and the noisy comment-count metric moved to the nightly benchmark.

## [0.3.0] — 2026-06-11

### Added

- **Outbound webhooks** — POST to Slack, Microsoft Teams, or a generic JSON endpoint when a review finishes, a review fails, a high-severity finding lands, or a repo finishes indexing. Configured on the admin Settings page (dedicated list + add/edit pages). Delivery is best-effort and SSRF-guarded (private/internal addresses are refused), so a slow or misconfigured endpoint can't delay or break a review.
- **User management** — self-service password change and admin password reset (as proper pages, not modals), a sidebar user dropdown with account switching, DiceBear avatars, and last-sign-in tracking shown in the users table.
- **Per-page browser tab titles** — each dashboard page now sets its own `document.title` instead of a single static title.

### Fixed

- **Thinking-mode models no longer fail reviews** — models that reject a forced `tool_choice` (e.g. deepseek thinking mode, which returns a 400) are detected and retried with `tool_choice: "auto"`, and the model is remembered so later calls skip the doomed attempt. Fixes #82.

### Changed

- **Evals gate the release build** — the LLM eval suite now runs on a release tag and the container is only built/pushed if it passes. The noisy threshold-based scorecard moved to a separate nightly `benchmark` job (and a `benchmark` pytest marker) so it's tracked without gating releases.
- The dashboard API client was split into per-domain modules (internal refactor, no behaviour change).

## [0.2.3] — 2026-06-08

### Added

- **MiniMax M2.7 support with think-block stripping** — `<think>…</think>` reasoning blocks (as emitted by MiniMax and some other models) are stripped before JSON parsing, so models that "think out loud" work for indexing and review. New `minimax/MiniMax-M2.7` registry entry.
- **Dynamic bot @mention in the dashboard** — the UI now shows the App's real handle (auto-detected from its GitHub slug, default `@miracodeai`) instead of a hardcoded `@mira-bot`. Exposed via `/api/version`.

### Fixed

- **Blast Radius no longer leaks private repos** — a public repo's review never names a dependent repo that isn't known to be public. Repo visibility is tracked in the registry, backfilled automatically on startup/sync, and unknown visibility is treated as private (safe by default).
- **No more duplicate review comments on re-review** — findings that already have an open bot thread are skipped, so each push stops re-posting the same suggestion.
- **Indexing is resilient to bad files** — a duplicate symbol name no longer crashes the whole index (`symbols` upsert is conflict-safe on both Postgres and SQLite), and a single failed file/batch is skipped instead of aborting the repo (which also stops runaway token spend after a failure).
- **Thread-resolution failures are now logged** — the real GraphQL error surfaces instead of being silently swallowed.
- **Think-block regex** now strips the full `<think>…</think>` block (it previously matched only the opening tag).
- **Sidebar navigation active state** — the active nav item is driven off `aria-current` (single source of truth), with a cleaner fill + bold treatment and a fixed header divider.

### Changed

- Dependency bumps (vite 8, tailwindcss 4.3, react-dom, eslint 10, lucide-react, @vitejs/plugin-react, @types/node, etc.).

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

[0.5.1]: https://github.com/miracodeai/mira/releases/tag/v0.5.1
[0.5.0]: https://github.com/miracodeai/mira/releases/tag/v0.5.0
[0.4.1]: https://github.com/miracodeai/mira/releases/tag/v0.4.1
[0.4.0]: https://github.com/miracodeai/mira/releases/tag/v0.4.0
[0.3.1]: https://github.com/miracodeai/mira/releases/tag/v0.3.1
[0.3.0]: https://github.com/miracodeai/mira/releases/tag/v0.3.0
[0.2.3]: https://github.com/miracodeai/mira/releases/tag/v0.2.3
[0.2.2]: https://github.com/miracodeai/mira/releases/tag/v0.2.2
[0.2.1]: https://github.com/miracodeai/mira/releases/tag/v0.2.1
[0.2.0]: https://github.com/miracodeai/mira/releases/tag/v0.2.0
[0.1.1]: https://github.com/miracodeai/mira/releases/tag/v0.1.1
[0.1.0]: https://github.com/miracodeai/mira/releases/tag/v0.1.0
