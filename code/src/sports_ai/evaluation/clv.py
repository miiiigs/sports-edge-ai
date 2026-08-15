"""Closing Line Value tracked independently from wins/losses."""

from __future__ import annotations

import math
from statistics import mean, stdev


def clv_record(
    entry_odds: float,
    closing_odds: float,
    entry_prob_devig: float | None = None,
    closing_prob_devig: float | None = None,
) -> dict:
    rec = {
        "entry_odds": entry_odds,
        "closing_odds": closing_odds,
        "clv_pct": entry_odds / closing_odds - 1.0,
    }
    if entry_prob_devig is not None and closing_prob_devig is not None:
        rec["clv_prob_pts"] = closing_prob_devig - entry_prob_devig
    return rec


def clv_summary(records: list[dict]) -> dict:
    """Aggregate plus one-sample t-test against 0."""

    xs = [record.get("clv_prob_pts", record["clv_pct"]) for record in records]
    n = len(xs)
    if n < 2:
        return {"n": n, "mean": xs[0] if xs else None, "t_stat": None}

    m, s = mean(xs), stdev(xs)
    t = m / (s / math.sqrt(n)) if s > 0 else float("inf")
    return {
        "n": n,
        "mean": m,
        "stdev": s,
        "t_stat": t,
        "positive_share": sum(x > 0 for x in xs) / n,
        "gate_300_positive": n >= 300 and t > 2.0,
    }
