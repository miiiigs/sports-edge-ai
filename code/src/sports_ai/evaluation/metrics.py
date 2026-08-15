"""Evaluation metrics: log loss, Brier, calibration."""

from __future__ import annotations

import math


def log_loss(probs: list[float], outcomes: list[int], eps: float = 1e-12) -> float:
    assert len(probs) == len(outcomes)
    total = 0.0
    for p, y in zip(probs, outcomes):
        p = min(1 - eps, max(eps, p))
        total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return total / len(probs)


def brier(probs: list[float], outcomes: list[int]) -> float:
    return sum((p - y) ** 2 for p, y in zip(probs, outcomes)) / len(probs)


def calibration_bins(
    probs: list[float],
    outcomes: list[int],
    n_bins: int = 10,
) -> list[dict]:
    bins: list[dict] = [
        {"lo": i / n_bins, "hi": (i + 1) / n_bins, "n": 0, "p_sum": 0.0, "y_sum": 0}
        for i in range(n_bins)
    ]
    for p, y in zip(probs, outcomes):
        idx = min(n_bins - 1, int(p * n_bins))
        bins[idx]["n"] += 1
        bins[idx]["p_sum"] += p
        bins[idx]["y_sum"] += y

    for bucket in bins:
        bucket["mean_pred"] = bucket["p_sum"] / bucket["n"] if bucket["n"] else None
        bucket["observed"] = bucket["y_sum"] / bucket["n"] if bucket["n"] else None
    return bins


def calibration_slope(probs: list[float], outcomes: list[int]) -> float:
    """OLS slope of observed vs predicted over bins."""

    pts = [
        (bucket["mean_pred"], bucket["observed"])
        for bucket in calibration_bins(probs, outcomes)
        if bucket["n"] >= 5
    ]
    if len(pts) < 3:
        return float("nan")

    xs, ys = zip(*pts)
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return float("nan")
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
