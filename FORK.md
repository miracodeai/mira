# Kory Source fork baseline

`hickoryai/kory-source` is a deliberately narrow fork of
`miracodeai/mira`. Keep upstream behavior intact unless a change falls inside
the policy below.

## Fork-delta policy

Fork-owned product changes are limited to:

- one Hickory LLM provider under `src/mira/llm/`;
- one package named `registry`;
- dashboard pages;
- configuration;
- database migrations.

Everything else should be contributed upstream or kept out of the fork. Do
not edit upstream source merely to make a sync easier.

## Divergent files

The fork was cut at `567ed7e98b0155448e3449477a58f58b453fcc2e`
(`origin/main`). That commit is also the common ancestor with upstream, so the
fork-authored baseline at the fork point is **0 lines**.

This drift snapshot was refreshed after `git fetch upstream`:

- fork baseline: `567ed7e98b0155448e3449477a58f58b453fcc2e`;
- upstream baseline: `dddb6d333cb9f3337a2ff24549df60afecbb12d2`;
- task head measured: `6bfd34b958a3892c1b04fba90ecb2619c2f50f46`.

At the task head, the only fork-authored files are:

- `FORK.md`;
- `tests/benchmarks/fixtures/hickory/README.md`;
- `tests/benchmarks/fixtures/hickory/baseline-codex-cli.json`;
- `tests/benchmarks/fixtures/hickory/baseline-run-1.json`;
- `tests/benchmarks/fixtures/hickory/baseline-run-2.json`;
- `tests/benchmarks/fixtures/hickory/baseline-run-3.json`;
- `tests/benchmarks/fixtures/hickory/manifest.json`;
- `tests/benchmarks/fixtures/hickory/run_baseline.py`.

There are no fork-owned divergent files under `src/`, `tests/` outside that
fixture directory, or `ui/` at the baseline snapshot above. The seven fixture
files account for 8,730 insertions under the drift command's path scope;
`FORK.md` is outside that scope.

The router-acp seat provider adds these fork-owned files and edits. They are
the intentional delta after the baseline snapshot:

- `src/mira/llm/router_acp.py` (new provider; prompt in, string out);
- `src/mira/llm/__init__.py` (factory alias `router-acp` / `router_acp`);
- `src/mira/config.py` (deployment-only relay URL, token, session, timeout,
  retries, and priority);
- `src/mira/error_messages.py` (visible seat, outage, mismatch, empty,
  timeout, tracing, and bad-response codes);
- `src/mira/dashboard/model_catalog.py` and `src/mira/llm/models.json`
  (`router-default`, no provider-key fetch);
- `tests/test_router_acp_provider.py`;
- `tests/test_config.py` (a repository file cannot retarget the relay);
- `tests/benchmarks/fixtures/hickory/run_router_acp.py`.

`src/mira/core/engine.py` is unchanged. Ensemble stays off because the
provider reports `supports_temperature = false`. Tool calling stays off.

Measure source drift with:

```bash
git diff --stat upstream/main -- src/ tests/ ui/
```

At the revisions above, the command reports **39 paths, 8,858 insertions, and
2,938 deletions**. That total is the sum of the seven fork fixture files and
these 32 upstream-only paths whose newer upstream changes have not yet been
synchronized into the fork:

- `src/mira/core/engine.py`;
- `src/mira/core/passes.py`;
- `src/mira/dashboard/routers/core.py`;
- `src/mira/dashboard/routers/vulnerabilities.py`;
- `src/mira/index/pg_store.py`;
- `src/mira/index/store.py`;
- `src/mira/llm/base.py`;
- `src/mira/llm/bedrock.py`;
- `src/mira/llm/codex_cli.py`;
- `src/mira/llm/provider.py`;
- `src/mira/llm/provider_profiles.py`;
- `src/mira/llm/providers.json`;
- `src/mira/llm/response_parser.py`;
- `src/mira/llm/responses.py`;
- `src/mira/platforms/fetch.py`;
- `src/mira/security/pr_scan.py`;
- `tests/test_activity.py`;
- `tests/test_activity_search_postgres.py`;
- `tests/test_agentic_tools.py`;
- `tests/test_bedrock_provider.py`;
- `tests/test_codex_cli_provider.py`;
- `tests/test_engine.py`;
- `tests/test_index_store.py`;
- `tests/test_llm_provider.py`;
- `tests/test_org_wide_store.py`;
- `tests/test_pr_scan.py`;
- `tests/test_provider_profiles.py`;
- `tests/test_repo_fetcher.py`;
- `tests/test_responses_provider.py`;
- `tests/test_review_history_postgres.py`;
- `tests/test_vulnerability_routes.py`;
- `tests/test_walkthrough.py`.

The upstream-only portion is 32 paths, 128 insertions, and 2,938 deletions
when comparing `upstream/main` to the fork baseline. These are absent upstream
commits, not fork-authored edits. After an upstream-sync PR lands, any
remaining output is the intentional fork delta and must comply with the policy
above.

## Review-without-post API (HAI-12286)

`POST /api/reviews` runs the normal engine against one exact PR snapshot and
returns findings without writing to GitHub. The contract is in
`docs/review-api.md`. These files are the intentional delta:

- `src/mira/review_api/__init__.py`, `settings.py`, `tokens.py`,
  `snapshot.py`, `store.py`, `runner.py`, `executor.py`, `service.py`
  (new package: token digests, SHA-pinned GitHub reads with a provider that
  refuses every write, the `review_api_jobs` table, the bounded executor, and
  the `custom_rules_providers` hook for HAI-11605);
- `src/mira/dashboard/routers/reviews.py` (new dashboard routes and the job
  report page) and `src/mira/dashboard/api.py` (one import line);
- `src/mira/dashboard/auth.py` (a bearer token reaches `/api/reviews` only;
  HTTPS unless loopback);
- `src/mira/platforms/server.py` (lifespan starts and stops the review
  service);
- `src/mira/llm/router_acp.py` (`call_observer` context variable for per-job
  model attribution; unparseable replies are recorded as `bad_response`);
- `src/mira/core/engine.py` (see below);
- `docs/review-api.md`;
- `tests/test_review_api.py` and `tests/test_api_routes.py` (route
  snapshot).

`src/mira/core/engine.py` gains `ReviewEngine.review_snapshot` and an
`extra_custom_rules` argument on `_review_diff_internal`. `review_pr` cannot
be reused: even with `dry_run` it records review history, PR progress, and
the last-reviewed SHA, and it reads the live PR diff. `review_snapshot`
calls the same `_review_diff_internal` pipeline with a caller-supplied diff
and nothing else. No upstream behaviour changes; offer it upstream.

The job table is created idempotently at startup on the application
database, the same way `AppDatabase` creates its own tables. It adds one
table and one index and alters nothing. If HAI-11605 lands schema first,
rebase onto it before merge and re-run the fresh and upgrade database tests.

The executor is off by default (`MIRA_REVIEW_API_ENABLED`). On Cloud Run it
needs CPU between requests, which the `kory_source` module provides whenever
`min_instance_count >= 1`: prod already sets 1; dev sets 0 and must be raised
before the executor is enabled there.

## Requester-run review seats (HAI-12330)

Review-API model calls run on the submitting client's router-acp seats, not
on a relay Kory Source dials. Each attempt is a `review_api_work` row that
the client reads from `pending_work` and answers through
`POST /api/reviews/{id}/work/{work_id}`. The worker polls the row every
500 ms, so any instance sharing the database can take the answer. Contract:
`docs/review-api.md`. Files changed:

- `src/mira/review_api/store.py` (the `review_api_work` table, created
  idempotently; conditional answer, timeout, and expiry; `finish` and
  `interrupt_expired` expire pending work);
- `src/mira/review_api/runner.py` (`requester_transport`; providers built
  with `relay=False`; `job_llm` set around `review_snapshot`);
- `src/mira/dashboard/routers/reviews.py` (`pending_work` on the job view
  and the work route) and `src/mira/dashboard/auth.py` (bearer regex admits
  `/work/{work_id}`);
- `src/mira/llm/router_acp.py` (`relay=False`, `job_llm`,
  `UnconfiguredSeatProvider`, `refuse_unrequested_seat`; the HTTP transport
  stays for the benchmark harness), `src/mira/llm/__init__.py` (factory
  returns `UnconfiguredSeatProvider`), `src/mira/error_messages.py`
  (`seat_provider_unconfigured`);
- `src/mira/registry/conflicts.py` (`requester_judge` reads `job_llm`; the
  process-wide `Registry.judge` is never swapped);
- `src/mira/platforms/handlers.py` and `src/mira/platforms/index_handlers.py`
  (fail closed with `seat_provider_unconfigured` before any GitHub write or
  index call when `llm.provider` is router-acp);
- `docs/review-api.md`, `tests/benchmarks/fixtures/hickory/README.md`, and
  the `run_router_acp.py` docstring;
- `tests/test_review_api.py`, `tests/test_api_routes.py`,
  `tests/test_router_acp_provider.py`.

`src/mira/core/engine.py` and `src/mira/platforms/server.py` are unchanged.
Webhook reviews, push indexing, PR commands, and the CLI review command have
no requester yet; they fail with `seat_provider_unconfigured` until a
follow-up gives them one.

## Release workflow (HAI-12329)

`.github/workflows/release.yml` gates the image on an inline `tests` job
that runs plain `uv run pytest` on Python 3.12 (installing the `dev`,
`serve`, and `bedrock` extras, as CI does). Pyproject `addopts`
deselects the `eval` and `benchmark` markers, so the gate needs no provider
API key, no `production` environment, and no inherited secrets. A
`vX.Y.Z` tag pushes `ghcr.io/hickoryai/kory-source:X.Y.Z` and `:X.Y`.

`evals.yml` and `release-beta.yml` are unchanged: beta tags (`v*-beta*`)
still call `evals.yml` and still need `OPENROUTER_API_KEY` in the
`production` environment. Run `evals.yml` by hand to check model quality
before a stable tag.

## Local Python boot (SQLite)

Docker is not required. From the repository root:

```bash
uv sync --extra serve
npm --prefix ui/mira ci
npm --prefix ui/mira run build
unset DATABASE_URL
export MIRA_INDEX_DIR=/tmp/kory-source-mira-indexes
uv run uvicorn 'mira.platforms.server:create_app' --factory --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765/`. With `DATABASE_URL` unset, Mira uses SQLite.
The first boot creates a local admin password file inside `MIRA_INDEX_DIR`;
keep that generated credential out of Git.

The baseline smoke check used `GET http://127.0.0.1:8765/health` and received
`{"status":"ok"}`, then fetched the dashboard HTML from `/`.

## Monthly upstream sync

1. Fetch both remotes and start a sync branch from the current fork default:

   ```bash
   git fetch --prune origin
   git fetch --prune upstream
   git switch main
   git pull --ff-only origin main
   git switch -c upstream-sync/$(date +%Y-%m)
   git merge --no-edit upstream/main
   ```

2. Resolve conflicts without broadening the fork-delta policy. Re-run the
   drift command and review every remaining fork-owned source line.
3. Rebuild and verify:

   ```bash
   uv sync --all-extras
   npm --prefix ui/mira ci
   npm --prefix ui/mira run build
   uv run pytest
   git diff --stat upstream/main -- src/ tests/ ui/
   ```

4. Push the sync branch and open a draft PR into `main`. Record the upstream
   SHA, test results, drift output, and any intentional divergent files in the
   PR body. Never sync by force-pushing `main`.
5. After review and merge, update the fixture/baseline only when the monthly
   benchmark policy calls for it; do not expose golden comments to reviewer
   prompts or configuration.

## Symbol anchors and the indexer

The fork keeps Mira's index and adds rename-safe anchors on top of it. These
files are the intentional delta. Extractor changes are general (qualified
Python methods, TypeScript methods, interfaces, type aliases, enums, and
`export default function`) and should be offered upstream.

- `src/mira/index/fingerprint.py`
- `src/mira/index/anchor_model.py`
- `src/mira/index/anchor_rows.py`
- `src/mira/index/anchors.py`
- `src/mira/index/symbols.py`
- `src/mira/index/local.py`
- `src/mira/index/extract.py` (qualified names and TypeScript declarations)
- `src/mira/index/store.py` and `src/mira/index/pg_store.py` (fingerprint columns, `symbol_anchors`, `index_runs`)
- `src/mira/index/indexer.py` (resolve anchors at the end of every index run)
- `src/mira/index/relationships.py` (`github_repo_mentions`)
- `src/mira/platforms/fetch.py` (`head_commit`)
- `src/mira/cli.py` (`mira index-repos`)
- `src/mira/dashboard/api.py` and `src/mira/dashboard/routers/anchors.py`
- `ui/mira/src/pages/anchors.tsx` and the anchors API client
- `tests/test_symbol_anchors.py`

`mira index-repos` is the Cloud Run job command. It reads `DATABASE_URL`.
The database login is IAM: the Cloud SQL Auth Proxy runs with
`--auto-iam-authn`, and the URL has no password. The service account is
`kory-source-{env}`. A workstation uses `kory-source-indexer-{env}` and
passes `owner/repo=/checkout` so `git diff -M` can see renames. The workstation indexes tracked files only, so caches and virtualenvs on disk stay out of the graph. The image
does not contain git, so the Cloud Run job resolves anchors by path and by
fingerprint only.

The relink step is a `RelinkProposer`. The default records a pending
proposal from body similarity and does not call a model. A later provider
can implement the same interface. Pending and dangling rows stay in
`symbol_anchors` until a person confirms or retires them.

## Work and decision registry (HAI-11605)

The registry lives in `src/mira/registry/`. It is the system of record for
work, decisions, rules, constraints, requirements, concepts, and
verification evidence. Mira's own rules code is unchanged: `rules.py`,
`analysis/feedback.py`, the learnings UI, `index/store.py`,
`platforms/github/webhook.py`, `core/passes.py`, and `llm/prompts/review.py`
have no edits.

Upstream files touched, and why:

- `src/mira/core/engine.py`: puts the approved-registry block at
  `custom_rules[0]`, wraps the main review's agentic executor so it can
  answer `search_decisions`, and adds `search_decisions` to
  `AGENTIC_TOOLS`. HAI-12196 calls `apply_registry` from that same block.
  The call inserts the block and stashes contradiction findings, which the
  engine prepends after critique so the noise filter cannot drop the
  blocker. The security pass keeps a plain executor (an upstream test
  asserts its type). If that pass calls the tool, it gets an
  `unknown tool` reply. HAI-12775 turns the post-critique line into
  `finish_registry`, which prepends the blockers and posts the
  uncited-decision comment; it replaces a fork line. A PR with no
  reviewable file returns before that hook, and its description can still
  state a decision, so `review_pr` adds 2 lines that call `finish_registry`
  when `reviewed_files` is 0. The fork-delta cap is `engine.py` < 48 against
  `567ed7e`; the measured total is 47.
- `src/mira/platforms/server.py` (10 lines): `install_registry(app)`
  (mounts `/api/registry` and `/mcp`), `observe_github` on every GitHub
  webhook as a background task, and the hourly registry job in the
  lifespan.
- `ui/mira/src/App.tsx` (the `/registry` route) and
  `ui/mira/src/components/dashboard/layout.tsx` (nav item and breadcrumb
  label).

These files are new and fork-owned:

- `src/mira/registry/`: `db.py` (one org-wide database, `DATABASE_URL`
  or `$MIRA_INDEX_DIR/registry.db`), `model.py` (vocabulary and
  confidence), `service.py` (lifecycle, realization, conflicts, work,
  evidence, import), `conflicts.py` (seat judge), `learned.py` (read-only
  learned-rule mirror), `prompt.py`, `review_gate.py` (review-time
  contradiction findings; they are comments only and do not write a
  registry object), `uncited.py` (HAI-12775: advisory findings for
  decisions in the PR body or a mirrored Linear ticket that cite no registry
  ID; the review API runner appends them as findings, and a review that
  posts to GitHub gets them as one general PR comment, edited in place on
  re-review, because an inline comment needs a diff line),
  `realization.py`, `auth.py` (registry JWT), `mcp.py`, and `api.py`.
- `ui/mira/src/pages/registry.tsx` and `ui/mira/src/lib/api/registry.ts`.
- `docs/registry-conflict-gate.md` (the client contract the hickory-ai
  push hook calls).
- `tests/test_decision_registry.py`, `tests/test_contradiction_gate.py`, and
  `tests/test_uncited_decisions.py`.
  The registry test asserts the `engine.py` (<48) and `server.py` (<25)
  caps against the fork base. The engine cap moved from 40 to 47 when
  HAI-12286 added `review_snapshot` (28 lines) beside the registry block.
  HAI-12196 fits the contradiction check into that block; the measured
  total is 45, so the cap is the next integer, 46. HAI-12775 adds 2 lines
  for PRs with no reviewable file (measured 47), and the cap moves by
  exactly those 2, to 48.

MCP is streamable HTTP at `POST /mcp` and returns JSON responses with no
SSE. Bearer JWT claims are `kind` (human|agent), `sub`, `model` (required
for agents), and `exp`. HS256 verifies against `REGISTRY_JWT_SECRET`, and
RS256 against `REGISTRY_JWT_PUBLIC_KEY`. A dashboard username and a human
JWT `sub` must use the same identity, because `resolve_conflict` refuses
the proposal's author.

The HAI-11606 import seam is `Registry.import_items()`, exposed to admins
at `POST /api/registry/import`. `/api/*` requires a dashboard session, so
an out-of-process sync job calls the Python seam or uses an admin session.

The same exit needs three consecutive UTC days with no agent Linear calls.
`python -m mira.registry.agent_calls ingest --url <relay /api/linear/agent-calls>`
copies that relay report into `registry_agent_call_snapshot`. It does not
call Linear. The command is a dry run unless `--apply` is given. An
unhealthy ledger is stored as not meeting the exit, and it does not replace
the days already stored.

### Linear import (HAI-11606)

Fork-owned, with no upstream drift:

- **`src/mira/registry/linear.py`** is the read-only Linear client, the
  inventory, and the resumable importer.
  - The client uses its own key, `LINEAR_SYNC_API_KEY`, so it is never counted
    as an agent Linear call. It refuses GraphQL mutations.
  - Commands: `python -m mira.registry.linear inventory` and
    `python -m mira.registry.linear import [--apply]`. Both cover every team in
    the workspace and every state, archived and trashed included. `--team KEY`
    (repeatable) narrows them to the named teams.
  - The inventory also counts the workspace's teams and its documents, and
    lists the documents linked to issues. Documents are not imported yet.
- **Each Linear issue becomes a `work` object** with source id = issue UUID
  and `ticket` = the identifier.
  - Delivery state follows Linear's state type (`duplicate` maps to
    `canceled`), or `archived`.
  - A trashed issue becomes `retired` with reason `source_deleted`, and
    untrashing restores it.
- **Comments go in `registry_comments`.** `get_context` returns them and also
  accepts `ticket`.
- **Walk cursors are saved in `registry_sync_state`.**
- **Linear is authoritative.** `update_work` refuses to change `title`,
  `owner`, or `delivery_state` on a Linear-mirrored work object. `link_work`
  refuses `parent_of`, `blocks`, and `relates` between two Linear tickets.
  Registry-only fields and links are still allowed: `plan`, `goal`,
  `records`, and `addresses`.
- **Seam fixes:**
  - An import item with links is written as `pending_links` and only marked
    `synced` after its links commit, so a crash between the two passes is
    retried rather than skipped.
  - Each item is one transaction (`RegistryDB.transaction()`).
  - Imported links are owned per item (`origin = import:{system}#{source_id}`),
    so a reversed parent link is replaced when the child is re-parented.
  - A concurrent insert of the same source row retries the item against the
    winner's row instead of failing the batch.
- **Pending links resolve on the next issue walk.** Linear does not resend an
  issue when its parent or blocker arrives, so each issue walk re-reads the
  issues still `pending_links` (`id: {in: [...]}`, same team filter). This
  also covers a crash between pages.
- **`--team` walks filter on team.** Comments are filtered by `issue.team`, and
  the client-side team check is kept as a backstop. Without `--team` no team
  filter is sent. A cursor is restarted only when
  Linear rejects it on the first page of a resumed walk; retries running out
  keep the saved cursor.
- **Query shapes are checked against Linear's schema.**
  `tests/fixtures/linear_schema.graphql.gz` is Linear's published SDL (from
  `github.com/linear/linear`, `packages/sdk/src/schema.graphql` at `3ce13b6`).
  The tests validate every query and every filter the importer sends against
  it. Refresh the fixture when Linear's schema changes.
- **CI runs the Postgres registry tests** against a `postgres:16` service
  (`REGISTRY_TEST_POSTGRES_URL`).

Drift, measured with `git diff --stat upstream/main -- src tests ui` at
upstream `e01f1e062c49536c7e12b6b7fa383e2dd28da14b`: **100 paths, 17,840
insertions, 5,856 deletions**. The total includes upstream commits that
have not been synced. Compared with this branch's base `b238ff1`, the
registry adds 18 paths and 4,107 insertions, all listed above.

## Greptile parity harness (HAI-11607)

These files are fork-owned and new. No upstream file is edited.
`tests/benchmarks/runner.py`, `judge.py`, and `scripts/run_benchmark.py`
are unchanged. The harness reuses `judge.prf1` and builds its own matched-
snapshot runner and blind pooled judge beside them.

- `tests/benchmarks/parity_snapshot.py`: units at the commits Greptile
  reviewed, neutral comment text, `PinnedGitHubProvider`, and the
  later-fix and revert evidence.
- `tests/benchmarks/parity_judge.py`: the blind pooled judge and pure
  scoring.
- `tests/benchmarks/parity_stats.py`: the parity verdict (cluster
  bootstrap), the registry A/B 2σ rule, and model families.
- `tests/benchmarks/parity_runner.py`: arms × runs × units, the registry
  arm switch (it swaps `mira.registry.prompt.registry_rule`), the artifact,
  `summarize`, and the Markdown report.
- `tests/benchmarks/parity_export.py`: exports a merged-PR window to the
  manifest shape, for the nightly run.
- `tests/benchmarks/fixtures/hickory/run_parity.py`: the CLI (`plan`,
  `run`, `rescore`, `export`). HAI-11608 depends on this interface.
- `tests/benchmarks/test_parity_unit.py`.

The design, the rules, and the commands are in
`tests/benchmarks/fixtures/hickory/README.md`.
