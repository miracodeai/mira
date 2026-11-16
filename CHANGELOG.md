# Changelog

All notable changes to Mira are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
### Added

- **Auto-review destination-branch scoping.** `filter.allowed_base_branches` limits automatic reviews to PRs whose destination (base/target) branch matches one of the configured glob patterns — e.g. `["main", "release/*"]` — and `filter.allowed_base_branch_overrides` replaces that list per repository (an empty override disables auto-review for that repo). A per-repo Review Policy tab in the dashboard sets the same policy without a restart: inherit the deployment default, override the branch list, or disable automatic reviews for that repo. Unconfigured deployments review every branch, exactly as before; manual `@bot review` commands, PR lifecycle tracking, indexing, and merged-PR learning are never gated by the branch policy. Skips are logged at INFO (see the companion to #316).
- **Reusable dashboard read helpers.** `mira.dashboard.queries` builds the dashboard's repo list, activity feed, per-repo review list and aggregate stats from an explicit list of repositories, so another process can serve the same response models over a subset of the registry. The `/api/activity`, `/api/stats` and `/api/repos` routes now call these helpers and behave exactly as before.

### Changed

- The GitHub webhook dispatcher accepts any scheduler exposing `add_task` (the `TaskScheduler` protocol) instead of requiring FastAPI's `BackgroundTasks`, and the HMAC check is exported as `verify_signature` so other entry points can validate deliveries the same way. `_verify_signature` remains as an alias.

### Security

- **Review rules, review context and cross-repo overrides can only be changed by admins.** Any signed-in dashboard user could create, edit, disable or delete global and per-repo review rules and review-context entries, all of which go straight into the review prompt, and could confirm, deny or invent cross-repo relationships, which every blast-radius analysis reads. Those fourteen routes now require the admin role like the rest of the configuration API; reading them is unchanged, and the Rules, Relationships and repo Review Context pages hide the editing controls from non-admins. A test now sweeps every mutating dashboard route and fails if one ships without an admin check. Reported by hackchang and Serhii Shunko (ccsimplyspolit).
- **Changing or resetting a password signs that user out everywhere else.** A session established with the old password stayed valid for its full seven days after an admin reset it, so rotating a suspected-compromised account's password did not cut off whoever held its cookie. An admin reset now revokes every session of the target user, and a self-service change revokes all but the one making the change. The transparent upgrade of pre-0.7.1 password hashes on login is a storage change, not a rotation, and leaves sessions alone. Reported by auuftakt.
- **Outbound webhooks connect to the address the SSRF check approved.** The guard resolved a webhook's hostname and rejected private, loopback, link-local and cloud-metadata addresses, but the HTTP client then resolved the name again, so a DNS record flipped between the two lookups could still reach an internal endpoint. Delivery now connects to one of the vetted addresses, keeping the `Host` header and the TLS server name on the original hostname, and a retry rotates to the next vetted address instead of re-resolving. IP literals and hosts in `MIRA_WEBHOOK_ALLOWED_HOSTS` are sent as written. Reported by pcornelissen (KIberblick).
- **The public blast-radius badge no longer reveals private repository structure.** The badge SVG is served without a session so GitHub can embed it, and it labelled the most depended-on files of any indexed repo and the names of every repo depending on it, private ones included. An anonymous viewer now gets file names only for a repo the registry knows to be public, and only dependents known to be public; a private or not-yet-synced repo's badge says "Private repository", and a signed-in dashboard user sees the full badge as before. Reported by pcornelissen (KIberblick).
- A learned rule an admin rejected keeps the wording they rejected: re-synthesis from new feedback no longer rewrites its text and sample count under the same verdict. Reported by Serhii Shunko (ccsimplyspolit).

### Changed

- **A re-review's walkthrough describes the whole pull request, not just the latest push.** From the second review on, Mira reviews only the commits pushed since the last review and rewrites the walkthrough comment in place, and that comment came back describing the push alone: on a 25-file PR the second review left a summary of the 5-file fix, a fresh 5/5 and "5 files reviewed", as if 20 files had been skipped (miracodeai/mira#434). The walkthrough's summary, diagram and PR-description block are now built from the whole PR diff on every round (the walkthrough prompt carries only file metadata, so this costs little), while inline findings stay on the latest push. The confidence score is the verdict on the PR as it stands: an unresolved blocker from an earlier review holds it at 2, unresolved warnings count together with new ones, and a push with nothing new to say scores at most 4 while an earlier warning is still open. Still-open threads are listed under the key files to review, and the footer and check-run details read "25 files in PR · 5 reviewed in this push · 1 new comment · 2 unresolved threads". The unresolved-thread count now comes from the bot's own threads, so it also shows when automatic thread resolution is off. A first review is unchanged.

## [0.11.1] — 2026-11-09
### Added

- **Release smoke eval on real pull requests.** `tests/evals/test_eval_release_smoke.py` reviews five merged PRs of this repository end to end in dry-run mode (3 to 30 files, so the agentic loop, hop cap and forced submission are exercised) and fails the release gate when a review comes back with a failed chunk, a failed security pass or no walkthrough, allowing one retry per PR. The evals workflow passes `GITHUB_TOKEN` for read access and `MIRA_MODEL` from the `MIRA_EVAL_MODEL` repository variable so the gate runs on the deployed model.
- The benchmark replay script reports failed chunks, security failures and whether a walkthrough was generated in its per-PR summary.

### Changed

- **Reviews think at `medium` effort with a 16k-token output budget by default.** These are the settings the published benchmark ran with; the previous defaults (no reasoning setting, 4096 tokens) were never measured and are what broke on 0.11.0. Both apply to the review and security passes; indexing stays without thinking. Set `llm.review_reasoning_effort: "off"` to review without thinking, which is faster and cheaper, or pick a mode in the dashboard. The dashboard's models form now shows the resolved mode and stores exactly what the admin saves, so choosing "Off" there is honoured instead of falling back to the config default. An endpoint that rejects the 16k budget with a 400 is retried at 4096 and kept there for that model.

### Fixed

- **Pull requests from forks are reviewed with working tools.** The reviewer's `read_file` and `grep_repo` tools, the repository tree and the just-in-time context all fetched source from the base repository at the PR's head *branch name*. A fork's branch does not exist there, and a merged PR's branch is usually deleted, so every fetch returned 404, the model was told `[failed to read …]` and `[grep unavailable: repo tree not loaded]` on each call, and it reviewed blind; thinking models then reason at length about tools that never answer, which is where most of the slow and cut-off reviews on this repository's own fork PRs came from. Source is now fetched at the head commit, which is reachable in the base repository for any PR, open or merged. This predates 0.11.0.
- **Every "retry" hint on a failed or incomplete review names the bot.** The check-run summary, the walkthrough verdict and the failure notice said "Comment `review-rest`", but a comment only reaches Mira when it mentions the bot, so following the hint did nothing. They now read "Comment `@<bot> review-rest`" with the deployment's bot name, as the walkthrough footer and the skipped-files notice already did.
- **The self-critique no longer discards every draft when its verdicts cannot be read.** On a benchmark rerun one host returned the critic's tool arguments as a complete verdict object followed by leaked reasoning and tool-call markup; the lenient JSON loader could not parse the trailing junk, read it as "no verdicts", and dropped all four drafts of a review, three of them correct. The loader now falls back to the first complete JSON value in the text, which recovers that shape everywhere it is used, and a critic reply with no verdicts at all keeps the drafts, as a failed critic call already did, instead of silently emptying the review.
- **A reply cut off at the output budget is retried once, with a larger budget and, when thinking caused it, with thinking off.** Thinking models count their reasoning against `llm.max_tokens`, so a review-sized prompt could spend the whole budget thinking and return no tool call at all, or a `submit_review` call truncated mid-JSON that then failed to parse. The release smoke eval caught this on DeepSeek v4.1 flash on four of five real PRs at the old 4096 default, and on two of them the model still spent all of a 16,384-token budget reasoning at `medium` effort. Chat Completions calls that finish with `length` are now retried once on the review, agentic and JSON-mode paths: a long answer gets the 16k budget; a reply whose tokens were nearly all reasoning is also sent with the provider's thinking-off payload, after which the same prompts completed in seconds. A model that fails even that fails as before, with the response-shape diagnostic intact.
- **`reasoning_effort` unset now turns thinking off on OpenRouter, as the setting documents.** Mira sent nothing reasoning-shaped when the effort was unset, so a model that thinks by default (DeepSeek v4.1 flash) reasoned without bound: on two of the five smoke-eval PRs it spent even the 16,384-token retry budget thinking and still returned no tool call. The OpenRouter profile now sends `reasoning: {"enabled": false}` when no effort is configured, matching what the direct DeepSeek profile already did. A model whose reasoning is mandatory (some Claude endpoints) refuses that once with a 400 and is then sent nothing reasoning-shaped, on every request path; a model that refuses the effort field still falls back to the off payload as before. To review with thinking, set `llm.review_reasoning_effort` (the benchmark used `medium` with `llm.max_tokens: 16384`).
- **A walkthrough submission missing its summary gets two corrective retries, not one.** Probing Claude Sonnet 5 on real PRs showed the model omitting `summary` from the `submit_walkthrough` call on about one submission in six, with nothing in the assistant text to fall back on, so a review that drew two in a row posted "Walkthrough was not generated". The prompt now says the summary is mandatory and lists every field a submission must carry, the correction names what was wrong, a third attempt is made, and each invalid response is logged with a bounded excerpt so the next occurrence is diagnosable from the server log.
- **A failed review no longer reports itself as a success.** When every chunk failed, 0.11.0 still concluded the check run as "Code Review Complete" and rewrote the walkthrough verdict to 5/5 "No issues to flag", because the conclusion only considered a missing walkthrough and the verdict only counted posted comments. The check run now concludes `failure` ("Code Review Failed") when no chunk completed and `neutral` ("Code Review Incomplete") when some chunks or the security pass failed, with the counts in its details; the verdict is capped at 3/5 "Review incomplete" with a reason that says what failed and how to retry. A known blocker still wins, and a score the model already set at 2 or below is left alone.

## [0.11.0] — 2026-11-07

### Added

- **Prompt caching for Anthropic models on OpenRouter.** Review and security prompts now send their stable instructions as a separate first system message with a `cache_control` breakpoint, and the agentic tool loop adds a top-level automatic breakpoint, so repeated reviews read the shared prefix from cache instead of paying full input price. Applies to `anthropic/*` models on providers whose profile sets `"prompt_caching": "anthropic"` (OpenRouter); `llm.prompt_caching: false` turns it off. Cache read/write tokens are logged per call.
- **Bitbucket Cloud support.** A fourth platform adapter reviews pull requests posted to `/bitbucket/webhook` (HMAC-SHA256 `X-Hub-Signature`), answers `@mention` commands and threaded replies, resolves outdated bot threads, learns from merged PRs, and incrementally indexes pushes to the main branch using the diffstat API. Configure with `MIRA_BITBUCKET_TOKEN` (access token, or `email:api_token`) and `MIRA_BITBUCKET_WEBHOOK_SECRET`; `MIRA_BITBUCKET_WORKSPACE` scopes dashboard discovery and `MIRA_BITBUCKET_BOT_USER` names the bot when the token can't read `/user`. `pullrequest:updated` events only trigger a review when the source commit changed, and the pause state lives in Mira's database because Bitbucket PRs have no labels. Command authorization matches the commenter's account id (Bitbucket nicknames are not unique), so `commands.allowed_users` takes account ids on Bitbucket. Branches and tags are resolved to their commit before indexing, since `/src/{branch}/` 404s for names containing `/`.
- **Opt-in "Worth a look (not verified)" walkthrough section.** With `review.show_unverified_findings: true` (default `false`), findings the self-critique graded *plausible* but did not post are listed in a collapsed section: `path:line` and title, at most 5, never inline and never in key issues. With the section shown and no comments posted, the verdict reads 4/5 "No confirmed issues".
- **PR reviews publish a check run on the head commit**, so the PR and checks pages show a live status row: in progress while the review runs, success/failure on completion (neutral on GitHub when the walkthrough summary was not generated). GitHub uses the Checks API; Forgejo — which has no Checks API — adapts the lifecycle onto commit statuses with a single fixed context that is replaced in place per SHA (its statuses have no neutral state, so a neutral conclusion displays as success). The completed run's expandable details section shows the review stats (files reviewed, comment count with the severity breakdown) so the status row is informative without opening the walkthrough comment.

### Changed

- **The reviewer's investigation is never thrown away.** When the agentic review loop reaches its turn budget, or the model answers in prose instead of calling a tool, Mira now forces the structured `submit_review` call over the full transcript — the diff plus every file and search result the model fetched — instead of discarding that work and re-reviewing the bare diff from scratch. On the five-PR development sample this fallback fired on three of five PRs and silently cost the findings the model had just gathered evidence for, plus a redundant 75–105 second review call. The budget is `review.agentic_max_hops` (default 10, was a fixed 6).
- **A second look recovers the defects the reviewer talked itself out of.** Traces showed the model routinely establishing a concrete defect during investigation and then withholding it as "minor", "not user-facing" or "not security". After the reviewer submits, Mira now asks it once more, over the same transcript, to file the concrete mismatches it analysed but did not submit (a check on one value with a use of another, behaviour lost in a refactor, an implementer or caller behind a changed signature, a lookup that can never match), with severity expressing impact and confidence expressing certainty. The extra comments are appended and go through the usual noise filter and self-critique. One additional, prompt-cached model call per chunk. On the five-PR sample it raised pooled recall from 50% to 56–59% at a precision cost that left F1 flat and added about 25 seconds per DeepSeek review, so it ships off: enable with `review.second_look: true` when recall matters more than quiet. The main prompt's "would a senior engineer stop to mention this?" test now applies to style remarks only, and the security pass hands off plain defects it notices (`bug`, `error-handling`, `race-condition`, `resource-leak`, `performance`, `configuration`, at most three) instead of discarding them as out of scope; a vulnerability it files under `bug` by mistake is still relabelled `security`.
- **Self-critique sees the code around the finding.** The critic's evidence was the first 1,200 characters of the covering hunk, so for a new file (one hunk) a finding about line 60 was graded "plausible, code not shown" and dropped. The evidence is now a window of 20 diff lines on each side of the cited lines, removed lines included. The critic is also told to grade the code fact rather than its consequence: a method that visibly returns a fresh instance instead of the receiver is `proven`, even though the effect on callers is inferred, instead of being held back as `plausible` and dropped.
- **Adjacent duplicate comments collapse.** Two passes, or the reviewer and its own second look, often anchor one bug on neighbouring lines of the same statement with unrelated titles ("dead fallback" vs "predicate compares to itself"). Same-category comments within three lines whose title and body overlap substantially are now one finding; distinct bugs on neighbouring lines, such as a case-sensitive lookup next to a predicate with side effects, still both post.
- **`grep_repo` searches the files that matter first.** Content search used to scan the first 15 repository paths in alphabetical order, so half of all searches on large repos returned "no matches" for symbols that plainly existed. Candidates are now ranked by filename match against the pattern, directory proximity to the PR's changed files, and language, and up to 40 files are fetched concurrently. An empty result says how many files were left unscanned so the model knows to narrow with `path_glob`.
- **`read_file` takes `start_line`/`end_line`.** Large files are returned as a window that reports where it stopped and how to continue, so the model can read the specific function a grep hit pointed at instead of only the first 12 KB of the file.
- **Findings with an imprecise code quote are kept.** A comment whose `existing_code` could not be found verbatim in the diff was discarded outright, which dropped correct findings whenever the quote spanned a removed line (the model quotes code as it reads in the new file) or differed only in whitespace. Quotes are now matched against the new-file and old-file views of each hunk, whitespace-insensitively, with a per-line fallback; a quote that still cannot be located is cleared (along with any suggestion anchored on it) while the finding itself is kept for self-critique to grade.
- **Self-critique posts "plausible" warnings at confidence 0.75 and above** (was 0.8). The grade means the critic could not contradict the claim from the hunk; findings the reviewer rated above the 0.7 posting floor are worth the reader's time.
- **Review prompt asks for complete coverage.** The "aim for N or fewer comments" instruction, which taught the model to stop early on large PRs, is replaced by a no-quota rule plus a final "what did I not file?" pass. New sections walk the model through refactor regressions (behaviour present on removed lines and absent on added ones) and contract changes (every implementer and call site of a changed signature), and the language footgun lists gain the recurring shapes behind missed bugs: truthiness checks on numeric zero, receiver methods returning a fresh instance, dropped nil guards and trace IDs, sentinel arithmetic, case-sensitive identifier lookups, inverted flag conditions, and self-referential lookup predicates.
- **"Missing test coverage" is a `suggestion`, raised only when a PR touches no test file at all.** Previously a `warning` whenever the model judged coverage incomplete, which was the single most common unmatched finding.
- **Submodule pointers are not reviewed.** A `mode 160000` gitlink carries only a commit SHA; it is skipped like a lockfile instead of being sent to the model to speculate about.
- **Walkthrough verdict reflects what was posted.** When filtering and self-critique leave no comments, a walkthrough the LLM scored 3 or higher now shows 5/5 "No issues to flag" with a neutral reason, instead of a label such as "Safe with minor fixes" promising fixes the PR never shows. Scores of 2 or lower are unchanged, since their concerns are in the reason rather than in line comments.
- **GitHub App permission: `checks: read & write`** (newly required; earlier releases needed no check permission). Existing installations must add the permission to see review status updates; a missing permission is logged and the review proceeds without the status.

### Fixed

- **A walkthrough that fails to post no longer leaves the PR on "Reviewing this PR…".** When the final walkthrough comment is rejected (for example, by an encoding error), the placeholder is replaced with a note that the review finished and any findings are posted as review comments.
- **Walkthroughs on PRs with skipped files post again.** The "📋 Reviewed N of M files" heading was written as the escapes `\ud83d\udccb`, which Python keeps as two lone UTF-16 surrogates rather than one emoji. Encoding the walkthrough then failed with "surrogates not allowed", so on any PR large enough for files to be skipped the walkthrough was never posted (GitLab, Forgejo and Bitbucket send UTF-8 JSON bodies).
- **A review whose chunks failed no longer reports a clean result.** When a chunk's structured `submit_review` call fails for good, the walkthrough summary opens with a "Review incomplete: N of M chunks failed" notice (a dedicated "Review failed" notice when every chunk failed), the CLI text output says so instead of "No issues found", and the failed files are recorded so `review-rest` retries them. Security-pass failures surface as a separate security-incomplete notice; dependency-scan failures are logged and audited only.
- **Empty model replies are retried instead of being parsed as a review.** A Chat Completions or Responses reply with missing, null or whitespace-only text is now a retryable `empty_response` error that uses the existing attempt limits and fallback model; an explicit refusal or content filter stops immediately with `completion_blocked`.
- **`review.walkthrough_sequence_diagram: false` suppresses diagrams the model supplies anyway.** The setting only removed the instruction from the prompt; a diagram the model returned regardless is now dropped before the walkthrough is rendered or posted, on the first attempt and on the retry.
- **A failed OSV batch no longer clears vulnerability alerts.** A timeout or HTTP error from OSV was treated as a clean result, deleting stored alerts for every package in the batch. Packages with no result now keep their alerts, successful results from other batches still apply, and the poller warns that the scan was incomplete instead of logging a clean run.
- **Unchanged dependencies keep their vulnerability history.** Pruning compared OSV ecosystem labels such as `PyPI` against the persisted internal kinds such as `pip`, so every non-npm alert was removed and recreated on each scan, resetting `first_seen_at`.
- **Dashboard package search applies its limit to unique packages.** The cap was applied to raw manifest rows before grouping, so duplicate rows from several manifests could fill the limit and hide a matching lockfile version.
- **Org-wide SQLite readers no longer recreate deleted databases.** Package, vulnerability and learned-rule listings open repository databases read-only, so a store removed between discovery and connection is skipped instead of being recreated as an empty file.
- **Resuming a review on Forgejo keeps the PR's other labels.** The pause label was removed through the label-collection endpoint, which deletes every label on the issue, and its 204 response was then rejected as an error. The single-label endpoint is used now.
- **Self-hosted Forgejo with only `MIRA_FORGEJO_API_URL` set works for incremental reviews.** The web base URL used for compare requests is inferred from the API URL, subpaths included, instead of defaulting to Codeberg, which also sent the Forgejo token there. An explicit `MIRA_FORGEJO_BASE_URL` still wins; an API URL whose path does not end in `/api/v1` fails at startup with a `ConfigError`.
- **`review.failed` webhook payloads include the PR `number`** on GitHub and Bitbucket, matching `review.completed`, `review.high_severity` and the Settings "Send test" sample; the built-in chat message shows `#<number>` on failures too.

## [0.10.1] — 2026-10-28

### Fixed

- **Dashboard charts include the complete review history.** Time-series metrics no longer stop at the newest 500 reviews per repository, so older dates, categories, and totals remain visible.
- **Activity search finds older reviews.** Search scans persisted history before applying the 500-match limit per repository; the unfiltered recent-activity feed is unchanged.
- **SQLite vulnerability summaries report persisted alerts.** Organization-wide severity counts now include alerts from repository databases, matching the vulnerability listing.
- **Invalid walkthroughs are retried once.** Empty summaries and leaked tool-call XML are rejected; if the retry also fails, the PR placeholder reports that the walkthrough could not be generated.
- **Repository file requests preserve refs and paths.** GitHub, GitLab, and Forgejo correctly encode refs containing query delimiters or literal percent sequences, and GitHub content paths preserve filename delimiters.
- **Repository tree requests preserve branch names.** GitHub, GitLab, and Forgejo correctly encode refs containing slashes, query delimiters, or literal percent sequences so indexing reads the intended tree.
- **Repository archive requests preserve refs.** Tarball downloads on GitHub, GitLab, and Forgejo correctly encode branch and tag refs instead of treating special characters as URL paths, queries, or fragments.
- **GitLab and Forgejo archive fetches honor path filters.** Excluded files are discarded during tarball extraction instead of being decoded and retained in memory.
- **Incomplete repository trees are rejected.** Truncated GitHub trees and incomplete GitLab or Forgejo pagination no longer silently produce partial indexes.
- **PR vulnerability scans preserve package identity and comment origins.** Internal ecosystem kinds remain intact, findings use the matching manifest location, and repeated occurrences of the same package and version produce one OSV query and finding per scan.
- **LLM requests follow provider-specific reasoning and token settings.** Chat and Responses requests use the appropriate reasoning controls and output-token fields, including compatible-provider fallbacks.
- **Agentic tool turns retain reasoning context.** Chat and Responses conversations preserve reasoning data and tool-call identifiers across turns.
- **Bedrock thinking and tools use compatible request shapes.** Thinking settings, forced tool choice, and tool specifications now follow model requirements.
- **Codex CLI receives the configured reasoning effort.** Review invocations now pass the selected reasoning setting to the CLI.

## [0.10.0] — 2026-10-26

### Security

- **Breaking: comment commands require repository write access by default.** GitHub, GitLab, and Forgejo now check permissions before running `review`, `review-rest`, `help`, `pause`, `resume`, `reject`, or free-form questions. Permission lookup failures deny access. To retain access for read-only users, add them to `commands.allowed_users` or a per-command `commands.<name>.allowed_users` list; `commands.blocked_users` always wins.
- **Free-form disagreements respect the reject policy.** A reply classified as disagreement resolves the thread and records rejected feedback only when its author can run `reject`; answering the reply also requires `ask` permission.
- **Malformed outbound webhook URLs are contained.** Invalid URLs are rejected during validation and cannot escape delivery error handling. HTTP redirects and other non-success delivery statuses are reported as failures.

### Added

- **Broader source and configuration file support.** Review and indexing recognize more languages, templates, build files, and configuration formats, while documentation remains excluded from indexing.
- **Configurable indexing concurrency.** `index.llm_concurrency` controls concurrent LLM summarization batches for full and incremental indexing. The default is 8, with a supported range of 1–32, so deployments can respect provider concurrency limits.

### Fixed

- **Nested GitLab namespaces work throughout the dashboard.** Repository details, setup, rules, learnings, and vulnerability routes accept nested groups, validate repository keys, and render useful breadcrumbs.
- **GitHub Enterprise uses the configured API URL.** Review and PR-description helpers now build clients against the deployment's configured GitHub endpoint.
- **Indexing fetches files omitted from tarball caches.** Conventions and package manifests fall back to individual source fetches when the cached archive does not contain them.
- **Failed initial indexing updates repository status.** Setup failures mark affected repositories as failed instead of leaving them pending indefinitely.
- **PostgreSQL contributor quality matches SQLite.** Per-author review and feedback quality is available when using the PostgreSQL index store.
- **Walkthrough parsing accepts capitalized keys.** Capitalized file and change-group keys from model output no longer cause valid walkthrough content to be lost.
- **Self-critique handles missing verdict indexes.** Positional matching is used only for a complete list of unindexed verdicts, preventing partial results from being assigned to the wrong findings.
- **JSON output instructions survive provider routing.** Chat Completions and Responses requests keep the required JSON instruction in user input, including when a proxy moves system content into separate instructions.
- **DeepSeek respects disabled reasoning.** Requests explicitly disable DeepSeek thinking when reasoning is set to `off`.
- **Codex CLI loads review schemas correctly.** Review schema imports use the shared tool-schema module.
- **Dependency polling retains distinct npm resolutions.** Multiple installed versions of the same package are preserved during vulnerability polling.
- **Weekly review metrics use ISO week years.** Reviews around New Year are grouped into the correct ISO week.
- **React and React DOM stay aligned at 19.3.0.** Matching runtime and type packages prevent the version mismatch that could leave the dashboard blank.

### Changed

- **Clearer review diagnostics.** GitHub webhook filter decisions are logged at INFO level, and LLM failures include transport timing or response-shape details without logging reasoning text.
- **Dashboard dependencies refreshed.** UI libraries and development tools have been updated, including Vite, ESLint, TypeScript tooling, and formatting utilities.

## [0.9.1] — 2026-09-23

### Added

- **Layered security review.** A dedicated security-model tier combines a focused LLM pass, optional agentic `read_file`/`grep_repo` verification, a deterministic regex-and-entropy secrets scan, and review-time OSV checks for dependencies added or upgraded by a PR.
- **OpenAI Responses API support.** OpenAI-compatible providers can set `llm.api_style: responses` to use the `/responses` protocol instead of Chat Completions, including JSON output, tool calling, reasoning, fallback models, and usage accounting.
- **Broader indexing support.** A shared file-type registry keeps review and indexing language detection consistent and adds Lua to the indexed languages.
- **Agentic tools on indexed repositories.** The main reviewer can use `read_file` and `grep_repo` even when an index already exists, allowing it to verify callers and dispatch paths beyond pre-fetched context.

### Fixed

- **Very large GitHub PRs remain reviewable.** When GitHub rejects the complete diff with HTTP 406, Mira reconstructs a unified diff from per-file patches, including valid added, removed, and renamed-file headers.
- **Walkthrough parsing tolerates malformed model output**, including leaked tool-call XML, malformed optional fields, and incomplete JSON structures.
- **Review failures update the in-progress placeholder comment** instead of leaving a PR appearing permanently stuck.
- **Repository indexes remain platform-isolated** when the same owner/repository name exists on more than one provider.
- **Incremental indexing reports the stored repository total** rather than only the number of files processed in the latest update.
- **Large repository tarballs avoid out-of-memory extraction** by discarding non-indexable and oversized files before loading their contents.
- **Contributor backfill uses PyGithub's current rate-limit API.**
- **Responses JSON mode includes the required object-output hint**, preventing compatible endpoints from rejecting walkthrough and review requests.

## [0.8.0] — 2026-07-27

### Added

- **Opt out of per-commit reviews.** `review.review_on_synchronize` (default `true`) controls whether every push to an open PR triggers a fresh review. Turn it off and Mira only reviews when a PR is opened or reopened — later commits are ignored until someone comments `@bot review`. Useful when you batch commits locally before pushing and only want the final diff reviewed, saving tokens and cutting mid-work noise. Honoured on GitHub, GitLab, and Forgejo, and toggleable from the Settings page.
- **Duplicate-dependency warnings.** A new dependency-review pass flags when a PR adds a package that overlaps in function with one the repo already has (e.g. `react-table` is present and the PR adds `@tanstack/react-table`). It runs on the indexing tier and only when the PR touches a manifest (`package.json`, `pyproject.toml`, `go.mod`, …), so most PRs make no extra LLM call; it compares the added deps against the repo's existing package names from the index (falling back to the manifest diff on an unindexed repo), tags findings `category=dependency`, and merges them into the normal review so they go through the same noise filter and self-critique. Lockfiles are excluded (transitive churn is noise). Gated by `review.dependency_overlap` (default on), admin-toggleable in Settings.
- **Configurable LLM retry and timeout.** Four new `llm` keys — `max_retries` (3), `request_timeout` (120s), `retry_min_wait` (2s), `retry_max_wait` (30s) — read at runtime instead of being baked in at import time. Raise them for flex-tier models or proxied endpoints that stall under load. Documented in `.mira.yaml.example`.
- **Concurrent-review de-duplication.** An in-memory review tracker records which PRs are mid-review, so a burst of webhook events (a rapid push + comment, a redelivery) no longer starts overlapping reviews of the same PR — the second attempt is skipped atomically. Review status (`reviewing` / `completed` / `failed`) is wired through the GitHub, GitLab, and Forgejo dispatchers so the dashboard reflects in-flight work.

### Changed

- **LLM retries only fire on transient failures.** The retry predicate is narrowed to timeouts, network errors, and 5xx/429 responses; a 4xx client error now raises `NonRetriableLLMError` and returns immediately instead of burning the full retry budget on a request that can't succeed.

### Security

- **Webhook SSRF via DNS names is closed.** Outbound webhook URLs were only screened for raw private/loopback/metadata *IP literals* — a hostname resolving into one of those ranges slipped through. Delivery now resolves the host and rejects it if any resolved address is private, loopback, link-local, reserved, multicast, or unspecified. Named internal services no longer pass implicitly; allow specific ones with `MIRA_WEBHOOK_ALLOWED_HOSTS` (comma-separated). Unresolvable hosts are treated as unsafe. Still best-effort — a narrow DNS-rebinding window remains for an admin who also controls a fast-flipping record.
- **No default admin password.** The dashboard no longer ships an `admin` / `admin` account. With `ADMIN_PASSWORD` unset, first start generates a random password and writes it to `<MIRA_INDEX_DIR>/initial_admin_password` (mode `0600`) rather than to logs; if an admin already exists with a well-known default (`admin` / `changeme`), Mira logs a warning to change it. Password storage moves from static-salt SHA-256 to PBKDF2-HMAC-SHA256 (600k iterations, per-user salt, constant-time compare); existing hashes are upgraded on the next successful login.
- **Several dashboard endpoints were missing auth.** The GitLab/Forgejo sync and repo-register endpoints, model-settings update, uninstall keep/delete, and setup-complete all accepted unauthenticated requests; they now require an admin session.
- **Public-path auth bypass narrowed.** The auth middleware treated the entire `/api/repos/` prefix and *any* path ending in `.svg` as public (for GitHub badge embedding), exposing more than intended. It now matches only the blast-radius badge (`/api/repos/{owner}/{repo}/blast-radius.svg`) exactly.
- **Session cookie is marked `Secure` over HTTPS.** The login cookie now sets `Secure` when the request is served over HTTPS (directly or via a trusted `X-Forwarded-Proto`), so it isn't sent over plaintext.
- **SPA fallback path traversal is closed.** The catch-all static handler resolved `../` sequences before checking the file, so a crafted path could escape the UI dist directory and serve arbitrary files. Resolved paths are now confined to the dist root.

## [0.7.0] — 2026-07-24

### Added

- **Forgejo support — a third first-class git host.** Mira now auto-reviews pull requests on Forgejo (including Codeberg, the default endpoint) with the same pipeline as GitHub and GitLab: inline review comments, walkthroughs, `@mention` commands (`review`, `pause`/`resume`, inline `reject`, free-form questions), push-triggered incremental indexing, and automatic repo discovery at startup. Authentication is an access token — set `MIRA_FORGEJO_TOKEN` + `MIRA_FORGEJO_WEBHOOK_SECRET` and point a webhook at `/forgejo/webhook`; self-hosted instances set `MIRA_FORGEJO_API_URL`. GitHub and GitLab credentials are optional when Forgejo is configured — set any platform, or any combination. See the [Forgejo setup guide](https://docs.miracode.ai/forgejo).
- **Cross-PR overlap detection.** While reviewing a PR, Mira compares it against the repo's other open PRs and flags the ones stepping on it in the walkthrough — same files (**merge-conflict risk**) or same goal (**duplicate effort**). A cheap deterministic pre-filter (shared files/symbols + title similarity, backed by a per-repo fingerprint cache) runs first, so only genuinely-overlapping candidates cost a single batched LLM call; stacked branches and drafts are excluded, and any failure degrades to "no findings" without blocking the review. Tune or disable under `review.overlap` (`enabled`, `max_candidates`, `confidence_floor`, `title_similarity_threshold`).
- **PR author allow / deny lists.** `filter.allowed_authors` restricts auto-review to the named authors (empty = review all, the default) and `filter.blocked_authors` always wins over it — handy for muting bots. Matching strips a trailing `[bot]`, so `dependabot` matches `dependabot[bot]`. The dispatcher applies the lists to PR events, `@mention` commands, and push-triggered indexing; an explicit `@miracodeai review` still works on a filtered author's PR.
- **Review dashboard** — a new admin-only **Review** page focused on keeping a human in the loop on PR reviews: **stale / waiting PRs** (how long open, how long idle, who they're waiting on), a **reviewer-responsiveness leaderboard** that surfaces the bottleneck (pending review queue + median time to respond once requested), **throughput trends** (median time-to-first-review and time-to-merge, this week vs last), an **approved-&-merged** count, **rubber-stamp detection** (approvals with no substantive review — empty/"LGTM" body and no real inline comments — surfaced org-wide and per reviewer), and an **open-PR status board** (approved / changes-requested / awaiting). Review timing is captured live from webhooks (`pull_request_review`, review-requested) and seeded by a light GitHub backfill of open PRs + existing reviews on repo add / admin **Refresh** / the `mira backfill-contributors` CLI.
- **Contribution analytics (secondary)** — each contributor's detail page also shows authoring stats (commits, PRs opened/merged, lines), a GitHub-style year-long contribution heatmap, and Mira's differentiated **review-quality** signal (blockers/warnings their PRs triggered + the accept rate of Mira's feedback). Contributors are keyed provider-agnostically `(provider, login)` so non-GitHub providers can be added later.
- **Threaded PR activity timeline.** Mira now stores each review's comments and the human replies to them, and the dashboard's Activity detail renders the exchange as threaded conversations grouped under each review pass — replies nested under the exact Mira comment they answer, severity dots aligned to file lines, newest first.
- A reusable **DataTable** component (column defs, header sorting, standalone pagination, loading/empty states) plus an inline 5-bar **gauge**, both used across the review pages.

### Fixed

- **Review comments always anchor to the PR's own diff.** Two related round-2 bugs: a merge commit's incremental diff dragged in everything the base branch had merged (so Mira reviewed — and spent tokens on — code that wasn't the PR's), and findings on such mainline code produced inline comments GitHub rejects (422: line not in the PR diff), leaving orphaned 🔴 rows in the review's Key Issues table with no inline comment attached. Incremental diffs are now restricted to the PR's own files, unanchorable comments are dropped before posting, and the Key Issues table is re-synced to what actually posts.
- **Postgres writes for review comments and PR replies survive stale connections** — two write paths missed by 0.6.0's reconnecting-cursor fix now route through it instead of crashing on an idle-dropped connection.
- **Long learned-rule text truncates with an ellipsis** in the dashboard's pending-learnings card instead of overflowing the table.

## [0.6.0] — 2026-07-10

### Added

- **GitLab support — full parity with GitHub.** Mira now auto-reviews merge requests via webhooks, posts inline review comments anchored to diff positions (with a plain-note fallback when GitLab rejects a position), and answers `@mention` commands on MRs: `review`, `review-rest`, `pause`/`resume`, inline `reject`, free-form questions, and merge-time learning. Round 2+ incremental re-review, thread auto-resolution once a finding is fixed, JIT cross-file context, file-history context, codebase indexing, incremental push indexing, and vulnerability/package search all work the same as on GitHub. Authentication is a group or project access token — set `MIRA_GITLAB_TOKEN` + `MIRA_GITLAB_WEBHOOK_SECRET` and point a project webhook at `/gitlab/webhook`; self-managed instances set `MIRA_GITLAB_API_URL`. GitHub App credentials are now optional when GitLab is configured (set either, or both). See the [GitLab setup guide](https://docs.miracode.ai/gitlab).
- **Dynamic platform layer.** Adding a git host is now data, not code: a `platforms.json` registry (mirroring `providers.json` for LLMs) carries each platform's API URL, webhook route, signature scheme, and terminology, overridable at runtime via `MIRA_PLATFORMS_JSON_PATH`. The repo data model extends to `(platform, owner, repo)`, so same-named repos coexist across hosts; existing GitHub rows migrate automatically with `platform = github`.
- **Mira answers to both its handles.** On either platform, a mention of the configured `MIRA_BOT_NAME` *or* the bot's real account username (GitHub App slug / GitLab token user) triggers a command — whichever a teammate types.

### Changed

- **Review comment headers use one severity marker instead of two emojis.** The category line is now plain bold text (`**Security issue**`) above the severity badge, rather than a second leading emoji. Cleaner, still scannable, and it renders correctly on both GitHub and GitLab (GitLab needed a Markdown hard break where GitHub tolerated a bare newline).

### Fixed

- **Malformed LLM JSON is recovered instead of dropped.** Responses that leak Anthropic tool-call XML (`</parameter></invoke>`) or arrive with an unbalanced brace are now repaired before parsing, so a chunk's comments are no longer lost when a model's output strays from clean JSON. Affects both the main and security passes.
- **Stale PostgreSQL connections after idle** — `AppDatabase` and `pg_store` keep
  long-lived psycopg handles that can go dead behind poolers or `idle_session_timeout`.
  A new `mira.db.postgres` module adds `ReconnectingCursor`, which retries once on
  `OperationalError` for `execute` and `executemany` (covering dashboard API routes,
  indexing, and the vulnerability poller) without adding per-query liveness probes.
- **`/health` reflects Postgres availability** — when `DATABASE_URL` is Postgres,
  the endpoint runs a one-shot `SELECT 1` on a dedicated connection that is always
  closed afterward, and returns HTTP 503 if the database is unreachable.
- **Creating or editing a learning requires authentication** — the learned-rule
  create and update endpoints now return 401 for unauthenticated requests instead
  of proceeding with an anonymous author.
- **Dashboard setup and repo sync respect a repo's platform** — completing setup
  writes index mode and status to the correct `(platform, owner, repo)` row (GitLab
  repos could previously not be opted out of initial indexing), and the GitHub repo
  sync no longer targets GitLab rows when pruning stale repos.

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

[0.11.1]: https://github.com/miracodeai/mira/releases/tag/v0.11.1
[0.11.0]: https://github.com/miracodeai/mira/releases/tag/v0.11.0
[0.10.1]: https://github.com/miracodeai/mira/releases/tag/v0.10.1
[0.10.0]: https://github.com/miracodeai/mira/releases/tag/v0.10.0
[0.9.1]: https://github.com/miracodeai/mira/releases/tag/v0.9.1
[0.6.0]: https://github.com/miracodeai/mira/releases/tag/v0.6.0
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
