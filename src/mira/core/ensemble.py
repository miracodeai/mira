"""Multi-run ensemble: review the same chunk N times, keep consensus findings.

Variance-driven false positives flicker across runs while real findings
recur, so a majority vote across independent samples filters both noise
and plausibility-bias FPs. Costs N× review-tier tokens; runs fire in
parallel so wall clock stays roughly flat.
"""

from __future__ import annotations

import copy

from mira.core.noise_filter import _is_duplicate
from mira.models import ReviewComment


def merge_ensemble_runs(
    runs: list[list[ReviewComment]],
    min_votes: int | None = None,
) -> list[ReviewComment]:
    """Cluster comments across runs and keep findings seen in >= min_votes runs.

    ``min_votes`` defaults to a strict majority of the runs. The kept
    representative is the cluster's highest-confidence member, with its
    confidence replaced by the cluster mean — recurrence strength doubles
    as a calibration signal.
    """
    if not runs:
        return []
    if len(runs) == 1:
        return list(runs[0])
    if min_votes is None:
        min_votes = len(runs) // 2 + 1

    clusters: list[list[tuple[int, ReviewComment]]] = []
    for run_idx, run in enumerate(runs):
        for comment in run:
            for cluster in clusters:
                if any(_is_duplicate(comment, other) for _, other in cluster):
                    cluster.append((run_idx, comment))
                    break
            else:
                clusters.append([(run_idx, comment)])

    merged: list[ReviewComment] = []
    for cluster in clusters:
        votes = len({run_idx for run_idx, _ in cluster})
        if votes < min_votes:
            continue
        rep = copy.copy(max(cluster, key=lambda pair: pair[1].confidence)[1])
        rep.confidence = round(sum(c.confidence for _, c in cluster) / len(cluster), 3)
        merged.append(rep)
    return merged
