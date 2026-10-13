"""Opt-in temperature calibration for corner-total market rows.

The leakage-safe confidence replay (``experiments/football_model_pick_confidence_replay``,
docs/FOOTBALL_MODEL_ACCURACY_AUDIT.md §4) found corner probabilities overconfident
in the tails: holdout ECE 0.026. A logit temperature of T = 1.20, fitted on the
2022/23–2023/24 seasons only, gave holdout ECE 0.011 and lower log loss.

The temperature acts on the push-excluded stake share q = W / (W + L) = 1 / fair:
q' = sigmoid(logit(q) / T). Push mass and the full/half split within the win and
loss sides are preserved. OVER and UNDER on one line stay exact complements,
because logit scaling is odd-symmetric.

Off by default. T = 1.0 (unset) returns rows unchanged. Only softening
(1.0 <= T <= 2.0) is accepted; any other value fails closed to 1.0.
"""

from __future__ import annotations

import math
import os
from typing import Any, Mapping

CORNER_TEMPERATURE_ENV_VAR = "SPORTS_AI_FOOTBALL_CORNER_PROBABILITY_TEMPERATURE"
DEFAULT_CORNER_TEMPERATURE = 1.0
MAX_CORNER_TEMPERATURE = 2.0
CORNER_CALIBRATION_VERSION = "football_corner_logit_temperature_v1"


def resolve_corner_temperature(value: Any = None) -> float:
    raw = os.environ.get(CORNER_TEMPERATURE_ENV_VAR) if value is None else value
    try:
        t = float(raw) if raw not in (None, "") else DEFAULT_CORNER_TEMPERATURE
    except (TypeError, ValueError):
        return DEFAULT_CORNER_TEMPERATURE
    if not math.isfinite(t) or t < 1.0 or t > MAX_CORNER_TEMPERATURE:
        return DEFAULT_CORNER_TEMPERATURE
    return t


def _temper(q: float, temperature: float) -> float:
    q = min(max(q, 1e-12), 1.0 - 1e-12)
    return 1.0 / (1.0 + math.exp(-math.log(q / (1.0 - q)) / temperature))


def apply_corner_temperature(row: Mapping[str, Any], temperature: float) -> dict[str, Any]:
    """Return a corner-total row with tempered settlement probabilities (copy)."""
    out = dict(row)
    if temperature == 1.0 or row.get("market_family") != "corner_total":
        return out
    sp = dict(row["settlement_probabilities"])
    fw, hw = float(sp["full_win_probability"]), float(sp["half_win_probability"])
    hl, fl = float(sp["half_loss_probability"]), float(sp["full_loss_probability"])
    push = float(sp["push_probability"])
    win_mass, loss_mass = fw + hw, fl + hl
    if win_mass <= 0.0 or loss_mass <= 0.0:
        return out  # degenerate line: nothing to temper
    kw = (fw + 0.5 * hw) / win_mass
    kl = (fl + 0.5 * hl) / loss_mass
    q = (fw + 0.5 * hw) / (fw + 0.5 * hw + fl + 0.5 * hl)
    q_new = _temper(q, temperature)
    nonpush = win_mass + loss_mass
    new_win_mass = q_new * kl * nonpush / (kw * (1.0 - q_new) + q_new * kl)
    new_loss_mass = nonpush - new_win_mass
    sw, sl = new_win_mass / win_mass, new_loss_mass / loss_mass
    new = {
        "full_win_probability": fw * sw,
        "half_win_probability": hw * sw,
        "push_probability": push,
        "half_loss_probability": hl * sl,
        "full_loss_probability": fl * sl,
    }
    win_stake = new["full_win_probability"] + 0.5 * new["half_win_probability"]
    loss_stake = new["full_loss_probability"] + 0.5 * new["half_loss_probability"]
    out["settlement_probabilities"] = new
    out["effective_win_stake_probability"] = win_stake
    out["effective_loss_stake_probability"] = loss_stake
    out["fair_decimal_price"] = 1.0 + loss_stake / win_stake
    meta = dict(row.get("numerical_metadata") or {})
    meta["corner_calibration_version"] = CORNER_CALIBRATION_VERSION
    meta["corner_probability_temperature"] = temperature
    meta["corner_untempered_fair_decimal_price"] = row["fair_decimal_price"]
    out["numerical_metadata"] = meta
    return out


__all__ = [
    "CORNER_CALIBRATION_VERSION",
    "CORNER_TEMPERATURE_ENV_VAR",
    "apply_corner_temperature",
    "resolve_corner_temperature",
]
