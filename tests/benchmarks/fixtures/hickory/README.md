# Hickory benchmark fixtures

`manifest.json` freezes 30 merged `hickoryai/hickory-ai` pull requests from
the 90-day window ending 2026-10-28. Every entry contains the repository, PR
URL, base and head SHAs, and the Greptile comment export. The ten newest PRs
are `held-out`; the preceding twenty are `development`.

Greptile inline comments are golden judge truth. GitHub thread resolution and
outdated state are retained as export metadata, but do not rewrite that truth.
Summary comments are provenance only and are not findings.

Golden text must never enter reviewer context. `run_baseline.py` constructs a
minimal runner fixture, passes only the PR URL to `ReviewEngine`, and makes
findings available only to Mira's judge phase.

Run the three-trial baseline with the box's existing Codex login and GitHub CLI
session; do not write either credential into a file:

```bash
GITHUB_TOKEN="$(gh auth token)" CODEX_HOME=/home/devagent/.codex \
  uv run python tests/benchmarks/fixtures/hickory/run_baseline.py
```

The command creates three raw run artifacts and `baseline-codex-cli.json` in
this directory. The summary records population σ for F1, precision, recall,
all per-PR latency observations, and each PR across the three runs.

## Router-acp harness

`run_router_acp.py` must run on the box that owns the relay, with
`MIRA_ROUTER_ACP_URL` (or its loopback relay entry) pointing at that box's
loopback relay. It constructs `RouterAcpProvider` directly with that URL.
Kory Source itself stores no relay URL for reviews: the review API hands
each model call to the client that submitted the job (HAI-12330), and
`create_llm("router-acp")` fails with `seat_provider_unconfigured`.

## Kory Source vs Greptile parity (HAI-11607)

`run_parity.py` compares Kory Source with Greptile on matched snapshots and
runs the registry A/B. HAI-11608 (the nightly Goose recipe) calls it, so the
subcommands and flags below are a stable interface.

### What is compared

Greptile reviews a PR at intermediate commits. Each inline comment records
the commit it saw (`original_commit_id`). In this manifest all 77
findings are on a commit other than the merged head. Of the 43 held-out
findings, 23 point at code that changed before merge. Reviewing the merged
head would hand Kory code in which those findings are already fixed. The
unit of comparison is therefore **one PR at one commit Greptile reviewed**:

- Each distinct `original_commit_id` gives one unit. Kory reviews the diff
  `base_sha...that_commit`, and the unit's golden set is the Greptile
  findings filed on that commit.
- A PR with a Greptile summary and no inline comment gives one clean unit at
  the summary's "Last reviewed commit".
- A PR with no Greptile output gives a unit that is listed as a coverage gap
  and **never scored**, so missing Greptile output never counts as a Kory win.
- Greptile posts only new comments on later rounds. Earlier findings on the
  same PR go to the judge as context. Any issue that contains one is left out
  of both reviewers' scores.

The frozen manifest yields 46 units (25 held-out, 21 development). The
reviewer sees only the fixture's PR title and the snapshot diff. It sees no
PR description and no bot threads, so the engine stays in round one.

Every reviewer read is pinned to the unit's commit (HAI-12817). The engine
reads sources and the repo tree at `pr_info.head_branch`, the live branch;
`PinnedGitHubProvider` rewrites that ref to `unit.sha`, and lists file
history from `unit.sha` instead of the default branch. Before HAI-12817 the
diff was pinned but source reads, the tree, history, title, and description
were live, so artifacts without `"harness": "hai-12817-pinned-reads"` came
from different reviewer inputs. Compare a baseline and a candidate only when
both ran on the same harness.

Since HAI-12817 the router-acp provider answers the engine's agentic loop
with JSON tool calls, so the reviewer can call `read_file`, `grep_repo`, and
`search_decisions`. The harness answers `search_decisions` with
`[registry unavailable]` in both arms: the live search has no as-of filter,
and in `registry_off` it would leak the registry. The A/B still measures the
prompt block only.

### Blind judge

For each (unit, arm, run), Kory's comments and Greptile's comments are
reduced to neutral text and pooled into one list. Neutral text drops HTML,
P0–P3 badges, "Prompt To Fix With AI" blocks, and reviewer names. The list
order is shuffled with a seed derived from `--seed`, the unit, and the run.
The judge is never told that there are two reviewers. It clusters comments
into issues and labels each issue `real`, `not_real`, or `uncertain`. It also
assigns a severity (critical 4, major 2, minor 1, nit 0) and rates the
evidence in each comment from 0 to 3. Python maps each comment back to its
reviewer and does all of the counting.

The judge's router-acp provider drops the reviewer framing that
`RouterAcpProvider` puts on every prompt ("Follow the Mira review
instructions"). The judge prompt has no such instructions. In the first live
run, Grok went looking for them and answered in prose on 30% of units.

`run` refuses an unpinned judge model. It also refuses a judge from the same
model family as a pinned reviewer, unless `--allow-same-family-judge` is
passed. An unpinned reviewer (`auto`) can still be routed to the judge's
family, so the harness checks every reviewer reply. A reply whose actual model
is in the judge's family, or is `unknown`, is never scored: it is logged as a
failed `excluded_family` seat call and re-requested, at most
`router_acp_max_retries` times per call. After the run, the report compares
the judge's actual model family with the families of every scored reviewer
seat call. Any overlap makes the parity verdict `inconclusive`.

### Scores

Each reviewer gets these scores:

- true defects (TP);
- false positives (`not_real`);
- `uncertain` issues;
- misses (real issues only the other reviewer found);
- severity-weighted TP and the TP count by severity;
- mean evidence quality on real issues.

The report also includes:

- **Golden F1:** Kory against Greptile's comments as truth, on the matched
  snapshot.
- **Disagreement:** counts for both reviewers, Kory only, and Greptile only,
  split by verdict.
- **Later-fix cross-check:** for each flagged location, whether the code at
  `line ± 2` survives unchanged into the merged PR. The check reads the file
  at both commits, so it works across rebases. Its results match GitHub's
  "outdated" thread flag for 22 of 23 outdated Greptile threads. Treat it as
  corroboration only, because it is biased toward Greptile: authors fixed
  what Greptile showed them.
- **Reverts:** merged PRs titled "Revert" that reference the PR number.
- Coverage, latency (mean, σ, median, p90), and seat usage. Seat usage covers
  logical calls, seat sessions, failures, and actual models, for both the
  review and the judge.

A crashed Kory review files nothing, so every real issue in that unit counts
as a Kory miss.

### Decision rules (defaults; proposal pending human approval)

- **Parity.** For each unit, take Kory's severity-weighted TP (the mean over
  runs) minus Greptile's. Compute a 95% cluster bootstrap CI of that
  difference, clustered by PR, with 10,000 resamples and seed 11607.
  - `better`: lower bound > 0, and Kory precision ≥ Greptile precision − 0.10.
  - `as_good`: lower bound ≥ −0.2 × Greptile's mean weighted TP per unit, with
    the same precision condition.
  - `worse`: neither condition holds.
  - `inconclusive`: fewer than 20 scored units, Kory coverage under 90%, a
    judge/reviewer family overlap, or failed judge calls on more than 5% of
    units. A failed judge call leaves that unit's comments `uncertain`.
- **Registry A/B.** Arms `registry_on` and `registry_off`. In `registry_off`,
  `custom_rules` gets no registry block. In `registry_on`, the shipped block
  is limited to objects approved before Greptile's review time, so later
  decisions cannot leak in. Keep the block **on** only if the mean of 3 Kory
  defect F1 runs exceeds `registry_off` by more than 2 × the population σ of
  `registry_off`, or if `--registry-override "<name, date: reason>"` records
  a written human override. When no unit receives a block, the arms are
  identical and the result is `off` with that reason. Pass the registry
  snapshot with `--registry-db`; its SHA-256 is recorded.

The parity verdict comes from the arm the A/B keeps.

### Commands

```bash
# Offline: units, call count, estimated seat sessions.
uv run python tests/benchmarks/fixtures/hickory/run_parity.py plan --split held-out --runs 3

# Live (needs the approved parameters; uses router-acp seats).
GITHUB_TOKEN="$(gh auth token)" MIRA_ROUTER_ACP_RELAY_ENTRY=<loopback entry> \
  uv run python tests/benchmarks/fixtures/hickory/run_parity.py run \
    --manifest tests/benchmarks/fixtures/hickory/manifest.json --split held-out \
    --runs 3 --arms registry_on,registry_off \
    --reviewer-model auto --judge-provider router-acp --judge-model <pinned candidate> \
    --seed 11607 --seat-budget <approved budget> \
    --registry-db <registry.db snapshot> \
    --out tests/benchmarks/fixtures/hickory/parity/held-out

# Offline: rebuild the report from the artifact; --check exits 1 on any drift.
uv run python tests/benchmarks/fixtures/hickory/run_parity.py rescore \
  tests/benchmarks/fixtures/hickory/parity/held-out.json --check

# Nightly input: export a merged-PR window as a manifest.
GITHUB_TOKEN="$(gh auth token)" uv run python tests/benchmarks/fixtures/hickory/run_parity.py export \
  --since 2026-10-01 --until 2026-10-28 --limit 30 --out /tmp/nightly-manifest.json
```

`run` writes `<out>.json` and `<out>.md`. The JSON artifact holds:

- the inputs: manifest hash, git SHA, rerun command, and whether calls were
  traced;
- the judge and reviewer configs, with tokens redacted;
- the seed;
- for every (unit, arm, run): the exact judge prompt, its SHA-256, the
  shuffle seed, the pool with owners, the raw and parsed judge responses, the
  neutral Kory comments, latency, and seat calls;
- for every review: the engine's audit trail (per-chunk drafts, the
  reviewer's tool calls, and each comment a filter or critique stage
  dropped), plus reviewed and skipped paths. The report summarizes them
  under "Pipeline (informational)". Scoring never reads them, and artifacts
  without them still rescore;
- the later-fix evidence and the reverts.

`rescore` uses nothing else. `run` aborts before any call if the seat-session
estimate exceeds `--seat-budget`. The budget is also a hard cap during the
run: every relay request counts as one seat session, and once the count
reaches the budget each further request fails with `seat_budget_exhausted`
and no new unit starts. A run that hits the cap is `inconclusive`.
The `plan` estimate counts one-shot reviews only. With agentic hops a review
can take up to six more seat calls per chunk, so set the budget from the
worst case, not the estimate.

Both arms replace the engine's whole registry hook with the prompt block
alone. The contradiction check (HAI-12196) does not run in either arm: it
would judge decisions approved after Greptile's review, and its seat calls
fall outside the A/B and the budget estimate.
