"""Evaluation and assessment helpers for paper/shadow decision outputs."""

from __future__ import annotations

import math
from collections import Counter
from statistics import mean
from typing import Any

from .clv import clv_summary
from .metrics import brier, calibration_slope, log_loss

SETTLEMENT_TO_BINARY = {
    "WIN": 1,
    "LOSS": 0,
}
CANDIDATE_DECISIONS = {"BET_CANDIDATE", "PAPER_SHADOW_CANDIDATE"}
ASSESSABLE_DECISIONS = CANDIDATE_DECISIONS | {"RESEARCH_SIGNAL"}
INVALIDATION_MARKERS = ("INVALID_LATE_MARKET_SNAPSHOT", "SUPERSEDED_BY_RERUN")


def _is_invalidated_row(row: dict[str, Any]) -> bool:
    text = f"{row.get('notes') or ''} {row.get('outcome_notes') or ''}"
    return any(marker in text for marker in INVALIDATION_MARKERS)


def _candidate_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("decision") in ASSESSABLE_DECISIONS and not _is_invalidated_row(row)]


def _safe_mean(values: list[float]) -> float | None:
    return mean(values) if values else None


def _settlement_units(row: dict[str, Any]) -> float | None:
    settlement = (row.get("settlement") or "").upper()
    entry_odds = row.get("available_price")
    if not entry_odds:
        return None

    if settlement == "WIN":
        return entry_odds - 1.0
    if settlement == "LOSS":
        return -1.0
    if settlement in {"VOID", "CANCELLED", "PUSH"}:
        return 0.0
    if settlement == "HALF_WIN":
        return (entry_odds - 1.0) / 2.0
    if settlement == "HALF_LOSS":
        return -0.5
    return None


def _prediction_metric_payload(rows: list[dict[str, Any]]) -> tuple[list[float], list[int]]:
    probs: list[float] = []
    outcomes: list[int] = []
    for row in rows:
        settlement = (row.get("settlement") or "").upper()
        outcome = SETTLEMENT_TO_BINARY.get(settlement)
        prob = row.get("fair_probability")
        if outcome is None or prob is None:
            continue
        probs.append(prob)
        outcomes.append(outcome)
    return probs, outcomes


def summarize_outputs(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize current outputs into funnel, CLV, outcome, and model metrics."""

    valid_rows = [row for row in rows if not _is_invalidated_row(row)]
    candidates = _candidate_rows(rows)
    settled_candidates = [
        row
        for row in candidates
        if row.get("result_status") in {"SETTLED", "VOID", "CANCELLED"}
    ]
    clv_records = [
        {"clv_pct": row["clv"]}
        for row in candidates
        if row.get("clv") is not None
    ]

    probs, outcomes = _prediction_metric_payload(settled_candidates)
    unit_returns = [value for value in (_settlement_units(row) for row in settled_candidates) if value is not None]

    by_decision = Counter(row.get("decision", "UNKNOWN") for row in valid_rows)
    by_result_status = Counter(row.get("result_status", "UNKNOWN") for row in valid_rows)
    historical_close_only_count = sum(
        "historical_close_only=true" in ((row.get("notes") or "") + " " + (row.get("outcome_notes") or ""))
        for row in rows
    )
    invalidated_rows = sum(_is_invalidated_row(row) for row in rows)

    return {
        "total_rows": len(rows),
        "historical_close_only_rows": historical_close_only_count,
        "audit": {
            "invalidated_rows": invalidated_rows,
        },
        "funnel": {
            "PASS": by_decision.get("PASS", 0),
            "WATCH": by_decision.get("WATCH", 0),
            "BET_CANDIDATE": by_decision.get("BET_CANDIDATE", 0),
            "PAPER_SHADOW_CANDIDATE": by_decision.get("PAPER_SHADOW_CANDIDATE", 0),
            "RESEARCH_SIGNAL": by_decision.get("RESEARCH_SIGNAL", 0),
        },
        "result_status": dict(by_result_status),
        "quality": {
            "avg_edge": _safe_mean(
                [row["edge"] for row in candidates if row.get("edge") is not None]
            ),
            "avg_data_quality": _safe_mean(
                [row["data_quality"] for row in candidates if row.get("data_quality") is not None]
            ),
            "avg_uncertainty": _safe_mean(
                [row["uncertainty"] for row in candidates if row.get("uncertainty") is not None]
            ),
        },
        "clv": clv_summary(clv_records) if clv_records else {"n": 0, "mean": None, "t_stat": None},
        "returns": {
            "n": len(unit_returns),
            "total_units": sum(unit_returns) if unit_returns else 0.0,
            "roi_per_bet": (sum(unit_returns) / len(unit_returns)) if unit_returns else None,
            "win_rate": (
                sum(row.get("settlement") == "WIN" for row in settled_candidates) / len(settled_candidates)
                if settled_candidates
                else None
            ),
        },
        "prediction_metrics": {
            "n": len(probs),
            "log_loss": log_loss(probs, outcomes) if probs else None,
            "brier": brier(probs, outcomes) if probs else None,
            "calibration_slope": calibration_slope(probs, outcomes) if len(probs) >= 10 else None,
        },
    }


def explain_assessment(summary: dict[str, Any]) -> list[str]:
    """Turn metrics into a plain-language assessment."""

    lines: list[str] = []
    funnel = summary["funnel"]
    clv = summary["clv"]
    returns = summary["returns"]
    prediction = summary["prediction_metrics"]
    quality = summary["quality"]
    live_candidates = funnel["BET_CANDIDATE"] + funnel["PAPER_SHADOW_CANDIDATE"]
    invalidated_rows = summary.get("audit", {}).get("invalidated_rows", 0)

    lines.append(
        "Funnel: "
        f"{live_candidates} live candidates, {funnel['RESEARCH_SIGNAL']} research signals, "
        f"{funnel['WATCH']} watches, {funnel['PASS']} passes."
    )
    if invalidated_rows:
        lines.append(
            f"Audit: {invalidated_rows} row(s) were invalidated by review and are excluded from candidate counts and performance."
        )
    if funnel["RESEARCH_SIGNAL"] > 0:
        lines.append(
            "Classification: historical close-only rows are tracked as research signals, "
            "separate from live paper/shadow candidates."
        )

    if clv["n"] == 0:
        if summary.get("historical_close_only_rows", 0) > 0:
            lines.append(
                "CLV: unavailable for the historical close-only rows in this lane, "
                "so judge these on outcome and calibration until entry-time prices exist."
            )
        else:
            lines.append("CLV: no closing-price observations yet, so the edge is still unproven.")
    elif clv["n"] < 20:
        lines.append(
            f"CLV: {clv['n']} graded prices is too early for confidence, "
            f"but the average CLV is {clv['mean']:+.2%}."
        )
    elif clv["mean"] is not None and clv["mean"] > 0:
        lines.append(
            f"CLV: encouraging so far with average CLV {clv['mean']:+.2%} "
            f"and positive-share {clv.get('positive_share', 0):.1%}."
        )
    else:
        lines.append(
            f"CLV: weak or negative so far with average CLV {clv['mean']:+.2%}; "
            "this lane likely needs a tweak or a stop decision."
        )

    if prediction["n"] == 0:
        lines.append("Prediction quality: no settled binary outcomes yet for log-loss or calibration checks.")
    else:
        lines.append(
            "Prediction quality: "
            f"log loss {prediction['log_loss']:.4f}, "
            f"Brier {prediction['brier']:.4f}."
        )
        slope = prediction.get("calibration_slope")
        if slope is None or (isinstance(slope, float) and math.isnan(slope)):
            lines.append("Calibration: not enough settled volume yet for a trustworthy slope.")
        elif 0.9 <= slope <= 1.1:
            lines.append(f"Calibration: slope {slope:.2f}, which is close to well-calibrated.")
        elif slope < 0.9:
            lines.append(
                f"Calibration: slope {slope:.2f}, which suggests the model is too aggressive "
                "and should be shrunk or filtered."
            )
        else:
            lines.append(
                f"Calibration: slope {slope:.2f}, which suggests the model is too conservative "
                "and may be understating strong edges."
            )

    if returns["n"] == 0:
        lines.append("Returns: no settled candidate bets yet, so ROI is still secondary and unavailable.")
    else:
        lines.append(
            f"Returns: {returns['total_units']:+.2f} units total, "
            f"{returns['roi_per_bet']:+.3f} units per settled bet, "
            f"win rate {returns['win_rate']:.1%}."
        )

    avg_edge = quality.get("avg_edge")
    avg_dq = quality.get("avg_data_quality")
    avg_unc = quality.get("avg_uncertainty")
    lines.append(
        "Candidate quality: "
        f"avg edge {avg_edge:+.2%}" if avg_edge is not None else "Candidate quality: avg edge unavailable"
    )
    if avg_dq is not None:
        lines[-1] += f", avg data quality {avg_dq:.1f}"
    if avg_unc is not None:
        lines[-1] += f", avg uncertainty {avg_unc:.2f}"
    lines[-1] += "."

    if clv["n"] >= 20 and clv["mean"] is not None and clv["mean"] <= 0:
        lines.append("Action: tighten the lane or reject it; the close is not confirming the idea.")
    elif clv["n"] >= 20 and clv["mean"] is not None and clv["mean"] > 0:
        lines.append("Action: keep the lane running, then compare tweaks against this baseline version.")
    else:
        lines.append("Action: keep logging; the main goal right now is to gather enough graded observations.")

    return lines
