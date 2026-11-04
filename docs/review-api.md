# Review API (review without posting)

The review API runs Kory Source's normal review engine against one exact PR
snapshot and returns the findings to the caller. It never writes to GitHub:
no review, comment, label, description edit, or thread resolution. It also
leaves webhook review history, PR progress, and the last-reviewed SHA alone.

Callers:

- the goose CLI (HAI-11602), run by an engineer on a laptop or a dev box;
- the Kory Code relay (HAI-11603).

Both clients live in their own repositories. This service only provides the
API.

Kory Source runs no model inference for these reviews. Each model call the
review needs is handed back to the client that submitted the job as a
pending work item. The client runs the prompt on its own router-acp seats
and posts the completion back (see [Requester work](#requester-work)). A
client that never answers gets `model_timeout`, never findings.

## Flow

1. `POST /api/reviews` with the repository, PR number, and the head SHA the
   caller expects.
2. The service checks the token and the repository allowlist before it reads
   anything from GitHub. Then it reads the PR. If the PR head is not the
   requested SHA, it returns `409 head_sha_mismatch` and starts nothing.
3. The job is stored and the call returns `202` with the job ID and URLs.
4. A worker claims the job and freezes the snapshot: PR metadata, the base
   SHA, and the `base...head` diff, all read by SHA. It checks the head
   again first; if the PR moved, the job fails with `head_sha_mismatch`
   before any model call. Pushes after capture do not change the review.
5. The engine reviews the frozen diff. Every file read, tree listing, and
   history lookup is pinned to the frozen head SHA. Each model call appears
   in the job's `pending_work`; the caller runs it and posts the answer to
   `POST /api/reviews/{id}/work/{work_id}`.
6. The result is written to the database, then the state becomes
   `succeeded`.
7. The caller polls `GET /api/reviews/{id}`, answering `pending_work` as it
   appears, until the state is terminal.

## Authentication

Send `Authorization: Bearer <token>`. Tokens are only accepted over HTTPS,
or over plain HTTP to a loopback host (`localhost`, `127.0.0.1`, `::1`).
Behind a TLS-terminating proxy such as Cloud Run, `X-Forwarded-Proto: https`
counts as HTTPS.

A review token reaches `/api/reviews`, `/api/reviews/{id}`,
`/api/reviews/{id}/report`, and `/api/reviews/{id}/work/{work_id}` only. It is
not a dashboard login and never grants admin access; using one anywhere else
returns `401`. Logged-in dashboard users can read any job and open its report
with their session cookie. They cannot submit or answer work, because both
must come from the token that owns the job.

### Token configuration (server)

The deployment stores SHA-256 digests only. Configure them in
`MIRA_REVIEW_API_TOKENS` (JSON) or in a file named by
`MIRA_REVIEW_API_TOKENS_FILE`:

```json
[
  {
    "principal": "zane-laptop",
    "kind": "engineer",
    "sha256": "<hex SHA-256 of the raw token>",
    "repos": ["hickoryai/kory-source"]
  },
  {
    "principal": "kory-code-relay",
    "kind": "relay",
    "sha256": "<hex SHA-256 of the raw token>",
    "repos": ["hickoryai/*"]
  }
]
```

- `kind` is `engineer` or `relay`. Give the relay and each engineer separate
  tokens.
- `repos` lists `owner/repo` names, or `owner/*` for every repository of one
  owner. Matching ignores case.
- A principal sees only its own jobs. Another principal's job returns `404`.
- Entries with a missing field, an unknown kind, a malformed digest, or a
  duplicate principal are skipped and logged.

### Issuing a token

There is no issuance UI or API. The platform administrator the coordinator
designates:

1. generates a random secret, for example
   `python3 -c 'import secrets; print("kst_" + secrets.token_urlsafe(32))'`;
2. stores `sha256(secret)` in the deployment's token config;
3. delivers the raw secret privately to its owner, who keeps it in approved
   secret storage.

To revoke a token, delete its entry. To rotate one, add the new entry, move
the client over, then delete the old entry.

### Client contract (goose CLI)

The goose CLI reads:

- `KORY_SOURCE_URL`, the service base URL (for example
  `https://kory-source.example.com`);
- `KORY_SOURCE_REVIEW_TOKEN`, the raw bearer token.

A laptop shell and a dev-box shell each load these from approved secret
storage on their own. Do not assume one forwards them to the other. Prefer a
separate token per environment. The client implementation belongs to
HAI-11602.

## `POST /api/reviews`

The body must be exactly these three fields. Extra fields and wrong types are
rejected.

```json
{
  "repo": "hickoryai/kory-source",
  "pr_number": 42,
  "head_sha": "0123456789abcdef0123456789abcdef01234567"
}
```

- `repo`: `owner/repo`.
- `pr_number`: a JSON integer greater than zero. Strings and booleans are
  rejected.
- `head_sha`: the full 40-character lowercase hex SHA.

`202 Accepted` returns the job view (below) in state `queued`. Submitting the
same principal, repository, PR, and SHA while that job is still queued or
running returns the existing job with `"deduplicated": true`.

Errors use one shape:

```json
{"error": {"code": "head_sha_mismatch", "message": "...", "current_head_sha": "..."}}
```

| Status | Code | Meaning |
| --- | --- | --- |
| 401 | (plain `error` string) | No token, an unknown token, or a token used outside `/api/reviews`. |
| 403 | (plain `error` string) | Token sent over plain HTTP to a non-loopback host. |
| 403 | `token_required` | A dashboard session tried to submit. |
| 403 | `repo_not_allowed` | The token's allowlist does not include the repository. No GitHub read happened. |
| 422 | `invalid_request` | The body is not exactly `{repo, pr_number, head_sha}`. `fields` lists the problems. |
| 503 | `review_execution_unavailable` | The executor is off on this deployment. No GitHub read happened. |
| 404 | `repo_not_installed` | The GitHub App is not installed on the repository. |
| 404 | `pr_not_found` | The PR does not exist. |
| 409 | `pr_not_open` | The PR is closed or merged. |
| 409 | `head_sha_mismatch` | The PR head is not `head_sha`. `current_head_sha` gives the real head. |
| 429 | `review_queue_full` | Too many jobs are queued. Retry later. |
| 502 | `github_unavailable` | GitHub could not be reached. |

## `GET /api/reviews/{id}`

```json
{
  "id": "rev_5f0c...",
  "state": "succeeded",
  "stage": "done",
  "repo": "hickoryai/kory-source",
  "pr_number": 42,
  "requested_head_sha": "0123...",
  "reviewed_head_sha": "0123...",
  "base_sha": "89ab...",
  "merge_base_sha": "4567...",
  "principal": "zane-laptop",
  "created_at": 1793300000.1,
  "started_at": 1793300001.2,
  "updated_at": 1793300090.3,
  "finished_at": 1793300090.3,
  "deadline_at": 1793303601.2,
  "findings": [
    {
      "id": "f1",
      "path": "src/app.py",
      "line": 12,
      "end_line": null,
      "severity": "blocker",
      "category": "bug",
      "title": "Division by zero",
      "body": "...",
      "confidence": 0.95,
      "suggestion": null,
      "source_pass": "main",
      "registry_ids": []
    }
  ],
  "key_issues": [{"issue": "...", "path": "src/app.py", "line": 12}],
  "summary": "...",
  "walkthrough": "...",
  "coverage": {
    "changed_files": 3,
    "reviewed_files": 1,
    "reviewed_paths": ["src/app.py"],
    "skipped": [
      {"path": "assets/logo.png", "reason": "patch_unavailable"},
      {"path": "dist/app.min.js", "reason": "excluded_by_filter"}
    ],
    "note": ""
  },
  "models": {"requested": ["auto"], "actual": ["grok/grok-4.6"]},
  "model_calls": [
    {"state": "ok", "requested_model": "auto", "actual_model": "grok/grok-4.6",
     "attempts": 1, "priority": "review", "attribution": ""}
  ],
  "error": null,
  "pending_work": [],
  "status_url": "https://.../api/reviews/rev_5f0c...",
  "report_url": "https://.../api/reviews/rev_5f0c.../report"
}
```

- `findings` is `null` until the job succeeds, and stays `null` if it fails.
  `[]` means the review succeeded and found nothing.
- `registry_ids` is always `[]` for now. Validated registry citations arrive
  with HAI-11605.
- A finding with an empty `path` is about the PR as a whole, not a line of
  the diff. Its `line` is `1` and means nothing. See
  [Uncited decisions](#uncited-decisions).
- `coverage.skipped[].reason` is one of `patch_unavailable` (GitHub returned
  no patch: binary or too large), `excluded_by_filter` (matched the review
  file filter), or `size_or_priority_cap` (cut by the diff size budget).
- `pending_work` lists model calls waiting for the submitting client. It is
  always an array, oldest first, and empty when nothing is waiting, on
  submit, and once the job is terminal. See [Requester work](#requester-work).
- `models.requested` and `models.actual` are the distinct router model IDs
  this job asked for and got. `model_calls` lists each logical completion
  with its outcome and attempt count. They cover this job only, even when
  other jobs run at the same time.

### Uncited decisions

The PR body, and the description of each Linear ticket the PR title or body
names, can state a decision with no registry ID next to it. The job adds one
advisory finding per such decision (HAI-12775):

```json
{
  "id": "f2",
  "path": "",
  "line": 1,
  "end_line": null,
  "severity": "suggestion",
  "category": "registry",
  "title": "Decision lacks a registry citation",
  "body": "The pull request body, line 4, states a decision without a registry ID:\n\"Divide before returning\"\n...",
  "confidence": 0.7,
  "suggestion": null,
  "source_pass": "registry_uncited_decision",
  "registry_ids": []
}
```

- Identify these by `source_pass == "registry_uncited_decision"`. Do not
  match on `title`, or on an empty `registry_ids`.
- The first body line names the source and its 1-based line: `The pull
  request body, line N` or `The Linear HAI-123 description (<url>), line N`.
  The second line quotes the decision.
- A decision is an explicit commitment ("we decided", "we chose", "we
  opted", "the decision was to", a line starting `Decision:`) or an item
  under a "Decisions I made" heading or label. Questions, hypotheticals,
  negations, empty or `None` sections, explicit no-decision statements ("No
  new product behavior is ratified here", "No product behavior changed"),
  test and lint notes, fenced code,
  blockquotes, inline code, quoted text, and HTML comments do not count.
- A citation is `reg_` plus 12 hex digits in the same item or sentence (or
  the sentence right after it), Markdown links included. `reg_...` is not a
  citation. The check is syntax only: it does not look up the id.
- Linked tickets are read from the registry's Linear mirror only, at most
  five per job. A ticket with no mirror, a retired (deleted) mirror, or an
  unreadable registry yields no finding. Mirrors are as fresh as the last
  Linear import, so a ticket edited since then is checked as it was.
- At most ten of these findings per job. They are advisory: they never fail
  the job, are not model calls, and write nothing to the registry. They are
  added even when no file was reviewable. Contradiction blockers
  (`source_pass == "registry"`) are unchanged.
- A review that posts to GitHub (the webhook path) cannot post an inline
  comment with no diff line. It gets the same decisions as one general PR
  comment with a hidden marker, edited in place on the next review. That
  includes PRs with no reviewable file.

### States and stages

| State | Terminal | Meaning |
| --- | --- | --- |
| `queued` | no | Stored, waiting for a worker. |
| `running` | no | A worker holds the job. `stage` is `capturing`, `reviewing`, or `persisting`. |
| `succeeded` | yes | Result stored. `findings` is a list. |
| `failed` | yes | `error.code` says why. `findings` is `null`. |
| `interrupted` | yes | The worker stopped before finishing. The job is never re-run, because its model calls may already have been paid for. Submit again. |

Failure codes (`error.code`):

| Code | Meaning |
| --- | --- |
| `head_sha_mismatch` | The PR moved between submission and capture. Nothing was reviewed. |
| `pr_not_found`, `repo_not_installed`, `github_unavailable` | GitHub lookup failed during capture. |
| `diff_unavailable` | GitHub could not compare the base and head commits. |
| `diff_incomplete` | GitHub returned fewer files than the PR changes (over the 300-file compare limit). The service will not report partial coverage as a full review. |
| `seat_exhausted` | The client posted `seat_exhausted` on every attempt (first try plus `llm.router_acp_max_retries`). Retry later. |
| `workstation_outage` | The client posted `workstation_outage` on every attempt. Retry after it recovers. |
| `model_timeout` | A work item got no answer within its `timeout_seconds`, or the client posted `timeout`. |
| `model_mismatch` | A pinned model was not the model that ran: the client posted `model_mismatch`, or its `actual_model` differs from the pin. |
| `model_call_failed` | Any other failed or unparseable model reply, including empty text. |
| `seat_provider_unconfigured` | The deployment is not configured for router-acp. There is no fallback. |
| `deadline_exceeded` | The job ran past its `deadline_at`. Pending work is expired. |
| `write_blocked` | The engine tried to write to GitHub and was refused. This is a bug; report it. |
| `review_failed` | The engine failed for another reason, such as an unparseable diff. |
| `internal_error` | The worker crashed. |
| `executor_interrupted` | The worker's lease lapsed (crash or restart). |
| `executor_shutdown` | The server stopped while the job ran. |

A failed model call fails the whole job, even in a pass the webhook review
would skip on error (walkthrough, security pass, summary). A partial review
is never reported as a clean one.

### Polling

Poll `status_url` every 1–2 seconds while the job is `running`, so work items
are picked up quickly, and every 5–10 seconds while it is `queued`. Answer
each new `pending_work` item as it appears. A review usually takes one to
several minutes. Stop after the job's `deadline_at`.

## Requester work

Kory Source does not call a relay for API reviews. It does not call
`POST /api/agent/llm-complete` and does not read `MIRA_ROUTER_ACP_URL`,
`MIRA_ROUTER_ACP_TOKEN`, or `MIRA_ROUTER_ACP_SESSION`, even when they are set.
Each attempt of each logical model call is one work item:

```json
{"work_id": "wrk_<32 hex>", "prompt": "<full prompt, untruncated>", "model": "auto", "timeout_seconds": 900}
```

- `model` is the requested router model: `auto`, or a pin such as
  `grok/grok-4.6`. `auto`, `default`, `router-default`, and
  `router-acp-default` are unpinned.
- `timeout_seconds` is `llm.router_acp_timeout_seconds` (default 900),
  counted from when the item was created.
- Run the prompt with `priority: review`. Priority is not a field on the item.
- Several items may be pending at once (walkthrough, review chunks, security
  pass, registry judge). The server does not cap them; bound your own seat
  queue.
- Anyone who can read the job can read the prompts. Only the submitting
  token can answer them. The HTML report does not show prompts.

### `POST /api/reviews/{id}/work/{work_id}`

Send exactly one of these bodies. Extra fields are rejected.

```json
{"text": "<the completion, non-empty>", "actual_model": "<router model that ran, max 200 chars>"}
```

```json
{"error": "seat_exhausted", "actual_model": "<optional, default unknown>"}
```

`error` is one of `seat_exhausted`, `workstation_outage`, `model_mismatch`,
or `timeout`.

`200` returns `{"work_id": "wrk_...", "state": "answered"}` or
`{"work_id": "wrk_...", "state": "failed", "error": "seat_exhausted"}`. This
does not finish the job; keep polling.

| Status | Code | When |
| --- | --- | --- |
| 401 | (plain `error` string) | No token or an unknown token. |
| 403 | (plain `error` string) | Token sent over plain HTTP to a non-loopback host. |
| 403 | `token_required` | A dashboard session tried to answer. |
| 404 | `review_not_found` | No such job, or another token's job. The item is untouched. |
| 404 | `work_not_found` | This job has no such work item. |
| 409 | `work_not_pending` | The item was already answered, failed, timed out, or expired, or the job is no longer running. |
| 422 | `invalid_request` | `text` and `error` together, blank `text`, missing `actual_model` with `text`, an unknown `error`, or extra fields. The item stays pending. |

What the server does with an answer:

- `text`: returned to the engine. If the item's `model` is pinned and
  `actual_model` differs, the call fails `model_mismatch`. The server never
  substitutes a model.
- `seat_exhausted` or `workstation_outage`: counts as one attempt. While
  `llm.router_acp_max_retries` allows, a new item with a new `work_id`
  appears after a short backoff. When attempts run out the job fails with
  that code.
- `timeout` or `model_mismatch`: the call fails at once. `timeout` becomes
  job error `model_timeout`.

### Two clocks

- Each work item expires at its creation time plus `timeout_seconds`. With
  no answer by then the job fails `model_timeout`. A later post gets `409
  work_not_pending`.
- The job expires at `deadline_at`. The job then fails `deadline_exceeded`
  and every pending item is expired.

Either way `findings` is `null`, never `[]`, and `model_calls` lists the
failed call. One failed call fails the job, including passes the webhook
review would tolerate.

## `GET /api/reviews/{id}/report`

A read-only HTML page for one job: state, SHAs, models, findings, coverage.
Open it with the same bearer token, or in a browser with a dashboard login.

## Models

Every model role (review, indexing, security) uses the router-acp seat
provider, run by the requester through [Requester work](#requester-work).
The job fails with `seat_provider_unconfigured` if `llm.provider` is not
`router-acp`; there is no cloud-key or local fallback. Ensemble sampling,
tool calling, and cross-PR overlap detection are off for API reviews. Seat
retries follow `llm.router_acp_max_retries`. Registry contradiction checks
during a job are work items on the same job.

GitHub webhook reviews, push indexing, and PR commands have no requester.
When `llm.provider` is `router-acp` they fail with
`seat_provider_unconfigured` before any GitHub write.

## Execution

The executor runs inside the web server process and is **off by default**.
Until it is on, `POST /api/reviews` returns `503
review_execution_unavailable`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MIRA_REVIEW_API_ENABLED` | unset | `1` starts the executor. |
| `MIRA_REVIEW_API_MAX_RUNNING` | `2` | Jobs running at once, across all instances sharing the database. |
| `MIRA_REVIEW_API_MAX_QUEUED` | `20` | Queued jobs before `429 review_queue_full`. |
| `MIRA_REVIEW_API_DEADLINE_SECONDS` | `3600` | Per-job deadline (minimum 60). |
| `MIRA_REVIEW_API_LEASE_SECONDS` | `120` | Lease length; the worker renews it every third of that. |
| `MIRA_REVIEW_API_POLL_SECONDS` | `2` | How often a worker looks for queued jobs. |
| `MIRA_REVIEW_API_PUBLIC_URL` | request host | Base URL for `status_url` and `report_url`. |
| `MIRA_REVIEW_API_TOKENS` / `MIRA_REVIEW_API_TOKENS_FILE` | unset | Token digests (above). |

A job is claimed with one conditional update that also enforces the running
cap. A worker renews its lease while the job runs. Running jobs whose lease
lapsed are marked `interrupted` at startup and on every poll. On shutdown a
worker cancels its jobs and marks them `interrupted`.

Jobs live in the `review_api_jobs` table and work items in the
`review_api_work` table of the application database (SQLite or PostgreSQL).
Both are created at startup if missing; no existing table changes. A waiting
call re-reads its work row every 500 ms, so an answer posted to any instance
that shares the database is picked up.

### Cloud Run

Jobs run after the `202` response returns, so the instance needs CPU between
requests and must not scale to zero mid-review.

- The `kory_source` Terraform module sets `cpu_idle = (min_instance_count == 0)`.
  CPU stays allocated whenever at least one instance is kept warm.
- **prod** sets `min_instance_count = 1`, so it already meets this. Set
  `MIRA_REVIEW_API_ENABLED=1` there when ready.
- **dev** sets `min_instance_count = 0`. Raise it to at least `1` before
  enabling the executor in dev.

Set the token config and `MIRA_REVIEW_API_PUBLIC_URL` from the service's
secret and environment configuration.

## Registry rules hook

`mira.review_api.runner.custom_rules_providers` is a list of async callables.
Each takes the frozen snapshot and returns `{"title", "content"}` rules. The
rules are added to the custom rules the engine already loads for the
repository. HAI-11605 registers the registry's provider here.

## Example

```bash
AUTH="Authorization: Bearer $KORY_SOURCE_REVIEW_TOKEN"

# 1. Submit.
curl -sS -X POST "$KORY_SOURCE_URL/api/reviews" -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d "{\"repo\":\"hickoryai/kory-source\",\"pr_number\":42,\"head_sha\":\"$(git rev-parse HEAD)\"}"
# 202 {"id": "rev_5f0c...", "state": "queued", "pending_work": [], ...}

# 2. Poll until work appears.
curl -sS "$KORY_SOURCE_URL/api/reviews/rev_5f0c..." -H "$AUTH"
# {"state": "running", "stage": "reviewing",
#  "pending_work": [{"work_id": "wrk_9a1e...", "prompt": "You are running as Mira's ...",
#                    "model": "auto", "timeout_seconds": 900}], ...}

# 3. Run the prompt on your own seat (priority review), then post the result.
curl -sS -X POST "$KORY_SOURCE_URL/api/reviews/rev_5f0c.../work/wrk_9a1e..." -H "$AUTH" \
  -H "Content-Type: application/json" \
  -d '{"text": "{\"summary\": \"...\", \"change_groups\": []}", "actual_model": "grok/grok-4.6"}'
# 200 {"work_id": "wrk_9a1e...", "state": "answered"}

# 4. Repeat 2 and 3 until the state is terminal.
curl -sS "$KORY_SOURCE_URL/api/reviews/rev_5f0c..." -H "$AUTH"
# {"state": "succeeded", "pending_work": [],
#  "findings": [{"id": "f1", "severity": "blocker", "title": "Division by zero", ...}],
#  "models": {"requested": ["auto"], "actual": ["grok/grok-4.6"]},
#  "report_url": "https://.../api/reviews/rev_5f0c.../report", ...}
```
