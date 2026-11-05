"""Statistics for the parity report (HAI-11607). Pure functions, no I/O."""

from __future__ import annotations

import random
import statistics
from typing import Any

from .judge import prf1

# Defaults are the proposal posted for human approval. The artifact records
# the values actually used.
DEFAULT_RULE = {
    "bootstrap_resamples": 10_000,
    "bootstrap_seed": 11607,
    "confidence": 0.95,
    # Kory is "as good" if the CI lower bound of the per-unit difference in
    # severity-weighted true defects is above -margin * Greptile's mean.
    "non_inferiority_margin": 0.2,
    # Precision may trail Greptile's by at most this much (absolute).
    "precision_margin": 0.10,
    "min_scored_units": 20,
    "min_coverage": 0.9,
    # A failed judge call leaves that unit's comments uncertain, which hides defects.
    "max_judge_error_rate": 0.05,
    "registry_sigma_multiple": 2.0,
}


def mean_sigma(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "sigma": None, "n": 0}
    return {
        "mean": round(statistics.mean(values), 4),
        "sigma": round(statistics.pstdev(values), 4),
        "n": len(values),
    }


def totals(scores: list[dict], who: str) -> dict[str, Any]:
    keys = ("tp", "fp", "uncertain", "missed", "weighted_tp", "weighted_missed", "comments")
    out: dict[str, Any] = {k: sum(int(s[who][k]) for s in scores) for k in keys}
    ev_n = sum(int(s[who]["evidence_n"]) for s in scores)
    out["evidence_mean"] = (
        round(sum(int(s[who]["evidence_sum"]) for s in scores) / ev_n, 4) if ev_n else None
    )
    out["corroborated_tp"] = sum(int(s[who]["corroborated_tp"]) for s in scores)
    p, r, f1 = prf1(out["tp"], out["fp"], out["missed"])
    out.update(precision=round(p, 4), recall=round(r, 4), defect_f1=round(f1 * 100, 2))
    out["by_severity"] = (
        {
            sev: sum(int(s[who]["by_severity"][sev]) for s in scores)
            for sev in scores[0][who]["by_severity"]
        }
        if scores
        else {}
    )
    return out


def golden_totals(scores: list[dict]) -> dict[str, Any]:
    tp = sum(s["golden"]["tp"] for s in scores)
    fp = sum(s["golden"]["fp"] for s in scores)
    fn = sum(s["golden"]["fn"] for s in scores)
    p, r, f1 = prf1(tp, fp, fn)
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(p, 4),
        "recall": round(r, 4),
        "f1": round(f1 * 100, 2),
    }


def paired_bootstrap(
    deltas_by_cluster: dict[str, list[float]], resamples: int, seed: int, confidence: float
) -> dict[str, float | None]:
    """Cluster bootstrap of the mean per-unit delta. Clusters are PRs."""
    clusters = list(deltas_by_cluster.values())
    flat = [d for c in clusters for d in c]
    if not flat:
        return {"mean": None, "lo": None, "hi": None}
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        sample = [d for _ in clusters for d in rng.choice(clusters)]
        means.append(statistics.mean(sample))
    means.sort()
    tail = (1 - confidence) / 2
    lo = means[int(tail * resamples)]
    hi = means[min(resamples - 1, int((1 - tail) * resamples))]
    return {"mean": round(statistics.mean(flat), 4), "lo": round(lo, 4), "hi": round(hi, 4)}


def parity_verdict(
    per_unit: list[dict],
    kory_totals: dict,
    greptile_totals: dict,
    coverage: float,
    judge_family_overlap: bool,
    rule: dict = DEFAULT_RULE,
    judge_error_rate: float = 0.0,
    budget_exhausted: bool = False,
) -> dict[str, Any]:
    """``per_unit`` rows: pr_url, kory_weighted_tp (mean over runs), greptile_weighted_tp."""
    by_pr: dict[str, list[float]] = {}
    for row in per_unit:
        by_pr.setdefault(row["pr_url"], []).append(
            float(row["kory_weighted_tp"]) - float(row["greptile_weighted_tp"])
        )
    ci = paired_bootstrap(
        by_pr, rule["bootstrap_resamples"], rule["bootstrap_seed"], rule["confidence"]
    )
    greptile_mean = (
        statistics.mean(float(r["greptile_weighted_tp"]) for r in per_unit) if per_unit else 0.0
    )
    margin = rule["non_inferiority_margin"] * greptile_mean
    precision_ok = (
        kory_totals["precision"] >= greptile_totals["precision"] - rule["precision_margin"]
    )
    reasons = []
    if len(per_unit) < rule["min_scored_units"]:
        reasons.append(f"{len(per_unit)} scored units < {rule['min_scored_units']}")
    if coverage < rule["min_coverage"]:
        reasons.append(f"Kory coverage {coverage:.0%} < {rule['min_coverage']:.0%}")
    if judge_family_overlap:
        reasons.append("judge shares a model family with a reviewer call")
    if judge_error_rate > rule.get("max_judge_error_rate", 1.0):
        reasons.append(
            f"judge failed on {judge_error_rate:.0%} of units > {rule['max_judge_error_rate']:.0%}"
        )
    if budget_exhausted:
        reasons.append("the seat budget ran out before every review and judge call finished")
    if reasons or ci["lo"] is None:
        verdict = "inconclusive"
    elif ci["lo"] > 0 and precision_ok:
        verdict = "better"
    elif ci["lo"] >= -margin and precision_ok:
        verdict = "as_good"
    else:
        verdict = "worse"
    return {
        "verdict": verdict,
        "delta_weighted_tp_per_unit": ci,
        "non_inferiority_margin_abs": round(margin, 4),
        "greptile_weighted_tp_per_unit": round(greptile_mean, 4),
        "precision_ok": precision_ok,
        "judge_error_rate": round(judge_error_rate, 4),
        "inconclusive_reasons": reasons,
        "rule": rule,
    }


def registry_ab(
    on_runs: list[float],
    off_runs: list[float],
    *,
    blocks_seen: int,
    override: str = "",
    sigma_multiple: float = 2.0,
) -> dict[str, Any]:
    """Keep the registry block only if mean(on) - mean(off) > k·σ(off), or a written override."""
    on, off = mean_sigma(on_runs), mean_sigma(off_runs)
    pooled = mean_sigma(on_runs + off_runs)["sigma"] if on_runs and off_runs else None
    if on["mean"] is None or off["mean"] is None:
        decision, why = "off", "an arm has no runs"
    elif blocks_seen == 0:
        decision, why = "off", "no unit received a registry block, so the arms are identical"
    else:
        delta = on["mean"] - off["mean"]
        threshold = sigma_multiple * (off["sigma"] or 0.0)
        if delta > threshold:
            decision, why = "on", f"Δ {delta:.2f} > {sigma_multiple:g}σ = {threshold:.2f}"
        else:
            decision, why = "off", f"Δ {delta:.2f} ≤ {sigma_multiple:g}σ = {threshold:.2f}"
    if override.strip():
        decision, why = "on", f"human override: {override.strip()}"
    return {
        "metric": "Kory defect F1 (pooled blind judge), percent",
        "on": on,
        "off": off,
        "on_runs": on_runs,
        "off_runs": off_runs,
        "sigma_rule": f"keep on iff mean(on) - mean(off) > {sigma_multiple:g} * pstdev(off)",
        "pooled_sigma": pooled,
        "blocks_seen": blocks_seen,
        "override": override.strip(),
        "decision": decision,
        "why": why,
    }


def model_family(model: str) -> str:
    m = (model or "").lower()
    if any(k in m for k in ("claude", "anthropic", "opus", "sonnet", "haiku", "fable")):
        return "anthropic"
    if any(k in m for k in ("gpt", "codex", "openai", "/o3", "/o4")) or m.startswith(("o3", "o4")):
        return "openai"
    if "grok" in m or "xai" in m:
        return "xai"
    if "gemini" in m or "google" in m:
        return "google"
    if "llama" in m or "meta" in m:
        return "meta"
    return "unknown"
