"""Leakage-aware shadow calibration diagnostics for Football Route B.

This module is deliberately non-operational. It consumes already-normalized,
settled paper positions and evaluates a candidate Platt calibrator without
changing Route B probabilities, BET/NO BET qualification, thresholds, Portfolio
selection, provider behaviour, or settlement truth.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from typing import Any, Callable, Iterable

SHADOW_CALIBRATION_VERSION = "football_probability_calibration_shadow_v1"
MIN_TRAIN_SAMPLE = 75
MIN_EVALUATION_SAMPLE = 75
MIN_SEGMENT_SAMPLE = 30
EPSILON = 1e-6

RELIABILITY_BINS: tuple[tuple[str, float, float], ...] = (
    ("0-40%", 0.0, 0.40),
    ("40-50%", 0.40, 0.50),
    ("50-55%", 0.50, 0.55),
    ("55-60%", 0.55, 0.60),
    ("60-65%", 0.60, 0.65),
    ("65-70%", 0.65, 0.70),
    ("70-75%", 0.70, 0.75),
    ("75%+", 0.75, 1.0000001),
)

MARKET_LABELS = {
    "1x2": "1X2",
    "btts": "BTTS",
    "goals_total": "Goals",
    "corner_total": "Corners",
    "asian_handicap": "Asian Handicap",
}
MARKET_ORDER = {
    "1x2": 0,
    "btts": 1,
    "goals_total": 2,
    "corner_total": 3,
    "asian_handicap": 4,
}
EDGE_ORDER = {"<0%": 0, "0-2%": 1, "2-5%": 2, "5-10%": 3, "10%+": 4, "Unknown": 5}
ODDS_ORDER = {"<1.50": 0, "1.50-2.00": 1, "2.00-3.00": 2, "3.00+": 3, "Unknown": 4}


def _safe_probability(value: Any) -> float | None:
    try:
        probability = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(probability) or probability < 0.0 or probability > 1.0:
        return None
    return min(1.0 - EPSILON, max(EPSILON, probability))


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _sigmoid(value: float) -> float:
    clipped = max(-35.0, min(35.0, value))
    return 1.0 / (1.0 + math.exp(-clipped))


def _logit(probability: float) -> float:
    clipped = min(1.0 - EPSILON, max(EPSILON, probability))
    return math.log(clipped / (1.0 - clipped))


def _fit_platt(samples: list[dict[str, Any]]) -> dict[str, float] | None:
    """Fit a two-parameter Platt scaler with a tiny slope regularizer."""
    if len(samples) < 2 or len({int(sample["outcome"]) for sample in samples}) < 2:
        return None

    intercept = 0.0
    slope = 1.0
    l2 = 1e-4

    for _ in range(100):
        grad_intercept = 0.0
        grad_slope = l2 * slope
        h_ii = 0.0
        h_is = 0.0
        h_ss = l2

        for sample in samples:
            x = _logit(float(sample["probability"]))
            prediction = _sigmoid(intercept + slope * x)
            error = prediction - float(sample["outcome"])
            weight = max(1e-9, prediction * (1.0 - prediction))
            grad_intercept += error
            grad_slope += error * x
            h_ii += weight
            h_is += weight * x
            h_ss += weight * x * x

        determinant = (h_ii * h_ss) - (h_is * h_is)
        if abs(determinant) < 1e-12:
            return None
        step_intercept = ((h_ss * grad_intercept) - (h_is * grad_slope)) / determinant
        step_slope = ((-h_is * grad_intercept) + (h_ii * grad_slope)) / determinant
        next_intercept = intercept - step_intercept
        next_slope = slope - step_slope
        if max(abs(next_intercept - intercept), abs(next_slope - slope)) < 1e-9:
            intercept = next_intercept
            slope = next_slope
            break
        intercept = next_intercept
        slope = next_slope

    if not math.isfinite(intercept) or not math.isfinite(slope):
        return None
    return {"intercept": intercept, "slope": slope}


def apply_shadow_platt(probability: float, calibrator: dict[str, float]) -> float:
    """Apply a shadow candidate. This function is not wired into runtime decisions."""
    safe = _safe_probability(probability)
    if safe is None:
        raise ValueError("probability must be finite and within [0, 1]")
    return _sigmoid(float(calibrator["intercept"]) + float(calibrator["slope"]) * _logit(safe))


def _metric_pack(probabilities: list[float], outcomes: list[int]) -> dict[str, Any]:
    if not probabilities or len(probabilities) != len(outcomes):
        return {
            "sample": 0,
            "brier_score": None,
            "log_loss": None,
            "expected_calibration_error": None,
            "mean_probability": None,
            "actual_rate": None,
        }

    sample = len(probabilities)
    brier = sum((probability - outcome) ** 2 for probability, outcome in zip(probabilities, outcomes)) / sample
    log_loss = -sum(
        outcome * math.log(min(1.0 - EPSILON, max(EPSILON, probability)))
        + (1 - outcome) * math.log(min(1.0 - EPSILON, max(EPSILON, 1.0 - probability)))
        for probability, outcome in zip(probabilities, outcomes)
    ) / sample

    weighted_gap = 0.0
    for _label, lower, upper in RELIABILITY_BINS:
        indices = [index for index, probability in enumerate(probabilities) if lower <= probability < upper]
        if not indices:
            continue
        mean_probability = sum(probabilities[index] for index in indices) / len(indices)
        actual_rate = sum(outcomes[index] for index in indices) / len(indices)
        weighted_gap += abs(mean_probability - actual_rate) * len(indices)

    return {
        "sample": sample,
        "brier_score": brier,
        "log_loss": log_loss,
        "expected_calibration_error": weighted_gap / sample,
        "mean_probability": sum(probabilities) / sample,
        "actual_rate": sum(outcomes) / sample,
    }


def _edge_bucket(sample: dict[str, Any]) -> str:
    edge = sample.get("edge")
    if edge is None:
        return "Unknown"
    if edge < 0.0:
        return "<0%"
    if edge < 0.02:
        return "0-2%"
    if edge < 0.05:
        return "2-5%"
    if edge < 0.10:
        return "5-10%"
    return "10%+"


def _odds_bucket(sample: dict[str, Any]) -> str:
    odds = sample.get("odds")
    if odds is None:
        return "Unknown"
    if odds < 1.50:
        return "<1.50"
    if odds < 2.00:
        return "1.50-2.00"
    if odds < 3.00:
        return "2.00-3.00"
    return "3.00+"


def _samples_from_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for row in rows:
        state = str(row.get("trade_state") or "")
        if state not in {"WON", "LOST"}:
            continue
        probability = _safe_probability(row.get("model_probability"))
        if probability is None:
            continue
        odds = _safe_float(row.get("entry_odds"))
        if odds is None:
            odds = _safe_float(row.get("available_market_price"))
        edge = _safe_float(row.get("edge"))
        fixture_key = str(row.get("_canonical_fixture_id") or row.get("fixture_id") or row.get("paper_trade_id") or "")
        samples.append(
            {
                "paper_trade_id": str(row.get("paper_trade_id") or ""),
                "fixture_key": fixture_key,
                "market": str(row.get("market") or "Unknown"),
                "timestamp": str(row.get("kickoff") or row.get("decision_timestamp") or ""),
                "probability": probability,
                "outcome": 1 if state == "WON" else 0,
                "odds": odds,
                "edge": edge,
            }
        )
    samples.sort(key=lambda sample: (sample["timestamp"], sample["fixture_key"], sample["paper_trade_id"]))
    return samples


def _segment_diagnostics(
    samples: list[dict[str, Any]],
    *,
    key_fn: Callable[[dict[str, Any]], str],
    label_fn: Callable[[str], str] | None = None,
    order: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        grouped[str(key_fn(sample))].append(sample)
    keys = list(grouped)
    if order is None:
        keys.sort(key=lambda key: (-len(grouped[key]), key.lower()))
    else:
        keys.sort(key=lambda key: (order.get(key, 999), key))

    rows: list[dict[str, Any]] = []
    for key in keys:
        group = grouped[key]
        metrics = _metric_pack(
            [float(sample["probability"]) for sample in group],
            [int(sample["outcome"]) for sample in group],
        )
        rows.append(
            {
                "key": key,
                "label": label_fn(key) if label_fn else key,
                **metrics,
                "sample_state": "SUFFICIENT" if len(group) >= MIN_SEGMENT_SAMPLE else "INSUFFICIENT_SAMPLE",
                "minimum_sample": MIN_SEGMENT_SAMPLE,
                "calibrator_eligible": len(group) >= MIN_TRAIN_SAMPLE and len({int(sample["outcome"]) for sample in group}) == 2,
            }
        )
    return rows


def _fixture_groups(samples: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        grouped[str(sample["fixture_key"])].append(sample)
    groups = list(grouped.values())
    groups.sort(key=lambda group: (min(sample["timestamp"] for sample in group), str(group[0]["fixture_key"])))
    return groups


def _walk_forward_shadow(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Expanding-window evaluation, keeping each fixture wholly out of its own fit."""
    train: list[dict[str, Any]] = []
    raw_probabilities: list[float] = []
    shadow_probabilities: list[float] = []
    outcomes: list[int] = []
    evaluated_fixtures = 0

    for group in _fixture_groups(samples):
        calibrator = None
        if len(train) >= MIN_TRAIN_SAMPLE and len({int(sample["outcome"]) for sample in train}) == 2:
            calibrator = _fit_platt(train)
        if calibrator is not None:
            evaluated_fixtures += 1
            for sample in group:
                raw_probability = float(sample["probability"])
                raw_probabilities.append(raw_probability)
                shadow_probabilities.append(apply_shadow_platt(raw_probability, calibrator))
                outcomes.append(int(sample["outcome"]))
        train.extend(group)

    raw_metrics = _metric_pack(raw_probabilities, outcomes)
    shadow_metrics = _metric_pack(shadow_probabilities, outcomes)
    sample = int(raw_metrics["sample"])
    if sample < MIN_EVALUATION_SAMPLE:
        verdict = "INSUFFICIENT_WALK_FORWARD"
    else:
        improved = sum(
            1
            for key in ("brier_score", "log_loss", "expected_calibration_error")
            if shadow_metrics.get(key) is not None
            and raw_metrics.get(key) is not None
            and float(shadow_metrics[key]) < float(raw_metrics[key])
        )
        verdict = "PROMISING_SHADOW" if improved == 3 else "MIXED_SHADOW" if improved >= 1 else "NO_CLEAR_IMPROVEMENT"

    return {
        "method": "expanding-window fixture-safe Platt calibration",
        "minimum_train_sample": MIN_TRAIN_SAMPLE,
        "minimum_evaluation_sample": MIN_EVALUATION_SAMPLE,
        "evaluated_fixtures": evaluated_fixtures,
        "sample": sample,
        "raw": raw_metrics,
        "shadow": shadow_metrics,
        "delta": {
            key: None
            if raw_metrics.get(key) is None or shadow_metrics.get(key) is None
            else float(shadow_metrics[key]) - float(raw_metrics[key])
            for key in ("brier_score", "log_loss", "expected_calibration_error")
        },
        "verdict": verdict,
        "leakage_guard": "Every canonical fixture is scored before that fixture's outcomes are added to the calibration training set.",
    }


def _candidate_id(samples: list[dict[str, Any]], calibrator: dict[str, float]) -> str:
    payload = {
        "version": SHADOW_CALIBRATION_VERSION,
        "rows": [
            [sample["paper_trade_id"], round(float(sample["probability"]), 10), int(sample["outcome"])]
            for sample in samples
        ],
        "intercept": round(float(calibrator["intercept"]), 12),
        "slope": round(float(calibrator["slope"]), 12),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"shadow_platt_{digest[:16]}"


def build_shadow_calibration_payload(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    samples = _samples_from_rows(rows)
    raw_metrics = _metric_pack(
        [float(sample["probability"]) for sample in samples],
        [int(sample["outcome"]) for sample in samples],
    )
    walk_forward = _walk_forward_shadow(samples)

    full_calibrator = _fit_platt(samples) if len(samples) >= MIN_TRAIN_SAMPLE else None
    candidate: dict[str, Any] | None = None
    if full_calibrator is not None:
        mapped_points = []
        for raw_probability in (0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90):
            mapped_points.append(
                {
                    "raw_probability": raw_probability,
                    "shadow_probability": apply_shadow_platt(raw_probability, full_calibrator),
                }
            )
        candidate = {
            "candidate_id": _candidate_id(samples, full_calibrator),
            "version": SHADOW_CALIBRATION_VERSION,
            "method": "Platt scaling on logit(model_probability)",
            "training_sample": len(samples),
            "training_cutoff": max((sample["timestamp"] for sample in samples), default=None),
            "intercept": float(full_calibrator["intercept"]),
            "slope": float(full_calibrator["slope"]),
            "mapping": mapped_points,
            "state": "SHADOW_ONLY",
            "runtime_applied": False,
            "effective_from": None,
        }

    by_market = _segment_diagnostics(
        samples,
        key_fn=lambda sample: str(sample.get("market") or "Unknown"),
        label_fn=lambda key: MARKET_LABELS.get(key, key.replace("_", " ").title()),
        order=MARKET_ORDER,
    )
    by_edge = _segment_diagnostics(samples, key_fn=_edge_bucket, order=EDGE_ORDER)
    by_odds = _segment_diagnostics(samples, key_fn=_odds_bucket, order=ODDS_ORDER)

    return {
        "available": bool(samples),
        "version": SHADOW_CALIBRATION_VERSION,
        "state": "SHADOW_ONLY",
        "runtime_applied": False,
        "affects_bet_decisions": False,
        "affects_thresholds": False,
        "sample": len(samples),
        "raw": raw_metrics,
        "walk_forward": walk_forward,
        "candidate": candidate,
        "by_market": by_market,
        "by_edge": by_edge,
        "by_odds": by_odds,
        "promotion": {
            "eligible": False,
            "state": "NOT_ELIGIBLE_FOR_RUNTIME",
            "reason": "This build is diagnostic only. A future promotion requires explicit review plus additional prospective out-of-sample shadow evidence.",
        },
        "methodology": (
            "Only normalized WON/LOST paper positions are used. Pushes, half-results, voids and open positions are excluded. "
            "The walk-forward comparison is fixture-safe and the candidate is not connected to Route B runtime probabilities."
        ),
    }
