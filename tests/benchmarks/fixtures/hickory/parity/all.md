# Kory Source vs Greptile parity report (HAI-11607)

**Verdict: worse** (arm `registry_off`)

- Units scored: 46 of 46; Greptile missing: 0 (never counted as a win).
- Δ severity-weighted true defects per unit (Kory − Greptile): mean -1.0652, 95% CI [-1.561, -0.5766]; non-inferiority margin −0.3014.
- Judge: grok@59aa6fc18f3f4b929dd9d59a6f3bfc11/grok-4.7, unknown (families ['xai']; reviewer families ['anthropic']).

## Inputs

- Manifest: `tests/benchmarks/fixtures/hickory/manifest.json` (sha256 `556bcf9b541ddc3b44acda8a4d5e9cdfcff28dd54dc2b0ecf7d84e73571db118`), split `all`: 30 PRs, 46 units, 77 golden findings.
- Arms ['registry_on', 'registry_off'] × 3 runs: 276 reviews, 276 judge calls. Estimated seat sessions: 1242 (3.50 seat calls per review from router-acp-held-out.json, plus 1 judge call).
- Git: `d1306b192812562f2899c4da2ca307efd0592ff9`. Shuffle seed: 11607.
- Command: `GITHUB_TOKEN="$(gh auth token)" MIRA_ROUTER_ACP_RELAY_ENTRY=<loopback entry> uv run python tests/benchmarks/fixtures/hickory/run_parity.py run --manifest tests/benchmarks/fixtures/hickory/manifest.json --split all --runs 3 --arms registry_on,registry_off --reviewer-model claude@068f70d4/opus --judge-provider router-acp --judge-model grok@59aa6fc18f3f4b929dd9d59a6f3bfc11/grok-4.7 --seed 11607 --seat-budget 1500 --out /opt/dev/artifacts/HAI-11607/rerun-2026-11-04/all`

## Attribution

- Reviewer: actual models {'claude@068f70d4/opus': 1089, 'unknown': 1}; families ['anthropic'].
- Judge: requested `grok@59aa6fc18f3f4b929dd9d59a6f3bfc11/grok-4.7`; actual models {'grok@59aa6fc18f3f4b929dd9d59a6f3bfc11/grok-4.7': 246, 'unknown': 1}; families ['xai'].
- Reviewer/judge family overlap: none. Judge is blind: pooled, anonymized, shuffled per (unit, arm, run) from seed.
- `unknown` is an unusable reply (no model reported); it is never scored.

## Uncertainty

| arm | verdict | Δ weighted TP/unit mean | 95% CI | non-inferiority margin | Greptile weighted TP/unit | precision ok | judge error rate |
|---|---|---|---|---|---|---|---|
| registry_on | worse | -1.0 | [-1.539, -0.4516] | −0.3188 | 1.5942 | True | 0.0 |
| registry_off | worse | -1.0652 | [-1.561, -0.5766] | −0.3014 | 1.5072 | True | 0.0072 |

Cluster bootstrap by PR, 10000 resamples, seed 11607, confidence 0.95; margin 0.2 × Greptile's mean; precision margin 0.1; at least 20 units, 0.9 coverage, judge errors ≤ 0.05.

## Per arm

| arm | Kory defect F1 | Greptile defect F1 | golden F1 | golden recall | Kory P | Greptile P | Kory evidence | coverage | latency s (mean ± σ / median / p90) | verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| registry_on | 39.4833 ± 4.6139 | 84.92 ± 2.1157 | 10.2267 ± 1.084 | 0.0606 | 0.9722 | 0.8551 | 3.0 | 0.9928 | 92.0435 ± 49.6432 / 75.48 / 161.05 | worse |
| registry_off | 34.5 ± 6.1615 | 88.55 ± 1.2067 | 11.7933 ± 0.8087 | 0.0692 | 0.9643 | 0.8819 | 2.9643 | 1.0 | 92.4092 ± 48.1633 / 79.38 / 159.66 | worse |

## Registry A/B

- on: 39.4833 ± 4.6139 (runs [33.33, 40.68, 44.44])
- off: 34.5 ± 6.1615 (runs [36.73, 40.68, 26.09])
- rule: keep on iff mean(on) - mean(off) > 2 * pstdev(off); blocks seen: 0
- decision: **off** (no unit received a registry block, so the arms are identical)

## Disagreement and cross-check

- both_not_real: 0
- both_real: 13
- both_uncertain: 3
- greptile_only_not_real: 15
- greptile_only_real: 99
- greptile_only_uncertain: 101
- kory_only_not_real: 1
- kory_only_real: 14
- kory_only_uncertain: 9
- not_real_fixed_later: 9
- not_real_total: 16
- real_fixed_later: 91
- real_total: 126
- uncertain_fixed_later: 49
- uncertain_total: 113

## Seat usage

- review: 1090 calls, 1089 seat sessions, models {'claude@068f70d4/opus': 1089, 'unknown': 1}
- judge: 247 calls, 246 seat sessions, models {'grok@59aa6fc18f3f4b929dd9d59a6f3bfc11/grok-4.7': 246, 'unknown': 1}
- metered: 1335 of 1500 seat sessions, 0 refused at the cap

`fixed_later` is biased toward Greptile (authors fixed what Greptile showed them) and can include base-branch merges into the PR's own files.
