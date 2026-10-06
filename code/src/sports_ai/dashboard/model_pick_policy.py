"""Model-only representative pick policy: football_model_pick_balanced_line_v1.

This selects ONE representative model reference pick per
``fixture + market family`` from the persisted Day-10 model rows, using ONLY the
model's own fair decimal prices. It is NOT bookmaker line selection and it does
NOT read odds, edges, bookmaker prices, outcomes, or settlement results.

Interpretation: "model-implied balanced reference line".

Asian Handicap
    group model rows into complete HOME/AWAY pairs by line (both sides required),
    balance_score = abs(HOME_fair - AWAY_fair), choose the line with the minimum
    balance_score; at that line choose the lower fair decimal price (model-favored
    side). Tie-break: Day-10 ASIAN_HANDICAP_LINES catalog order, then HOME.

Corners
    group model rows into complete OVER/UNDER pairs by line, balance_score =
    abs(OVER_fair - UNDER_fair), choose the minimum; choose the lower fair price.
    Tie-break: Day-10 CORNER_TOTAL_LINES catalog order, then OVER.

Why NOT max probability: maximising ``effective_win_stake_probability``
structurally prefers easier/extreme lines (e.g. AH +1.0, Corners Under 10.5) and
can inflate a raw hit rate without representing the fixture. That is rejected as
a representative policy.

This module is intentionally pure (no I/O, no DB, no network).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

POLICY_ID = "football_model_pick_balanced_line_v1"
POLICY_VERSION = "v1"

MARKET_ASIAN_HANDICAP = "asian_handicap"
MARKET_CORNER_TOTAL = "corner_total"

# Existing Day-10 line catalogues (engineering verification lines).
ASIAN_HANDICAP_LINES: tuple[float, ...] = (-1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0)
CORNER_TOTAL_LINES: tuple[float, ...] = (8.5, 9.0, 9.25, 9.5, 9.75, 10.0, 10.5)

_FORBIDDEN_INPUT_KEYS = ("odds", "edge", "bookmaker", "best_available_price", "closing", "settlement_result")


@dataclass(frozen=True)
class ModelPick:
    market: str
    line: float
    selection: str
    model_probability: float
    model_fair_price: float
    balance_score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "line": self.line,
            "selection": self.selection,
            "model_probability": self.model_probability,
            "model_fair_price": self.model_fair_price,
            "balance_score": self.balance_score,
            "policy_id": POLICY_ID,
            "policy_version": POLICY_VERSION,
        }


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _rows_by_line(market_rows: Sequence[Mapping[str, Any]], family: str, selection: str) -> dict[float, Mapping[str, Any]]:
    out: dict[float, Mapping[str, Any]] = {}
    for row in market_rows:
        if str(row.get("market_family")) != family:
            continue
        if str(row.get("selection")) != selection:
            continue
        line = _finite(row.get("line"))
        if line is None:
            continue
        out[line] = row
    return out


def _balanced_pick(
    *,
    market_rows: Sequence[Mapping[str, Any]],
    family: str,
    positive: str,
    negative: str,
    catalog: Sequence[float],
) -> ModelPick | None:
    positive_rows = _rows_by_line(market_rows, family, positive)
    negative_rows = _rows_by_line(market_rows, family, negative)
    order = {float(line): index for index, line in enumerate(catalog)}

    candidates: list[tuple[float, int, float, Mapping[str, Any], Mapping[str, Any]]] = []
    for line in catalog:
        key = float(line)
        pos = positive_rows.get(key)
        neg = negative_rows.get(key)
        if pos is None or neg is None:
            continue
        pos_fair = _finite(pos.get("fair_decimal_price"))
        neg_fair = _finite(neg.get("fair_decimal_price"))
        if pos_fair is None or neg_fair is None:
            # Infinite/undefined fair price (zero win probability) is never a
            # representative balanced line.
            continue
        candidates.append((abs(pos_fair - neg_fair), order.get(key, len(catalog)), key, pos, neg))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1]))  # balance, then catalog order
    balance, _rank, line, pos, neg = candidates[0]
    pos_fair = float(pos["fair_decimal_price"])
    neg_fair = float(neg["fair_decimal_price"])
    chosen = pos if pos_fair <= neg_fair else neg  # tie -> positive side (HOME/OVER)
    return ModelPick(
        market=family,
        line=line,
        selection=str(chosen["selection"]),
        model_probability=float(chosen["effective_win_stake_probability"]),
        model_fair_price=float(chosen["fair_decimal_price"]),
        balance_score=balance,
    )


def select_model_picks(market_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Return the balanced-line model pick for AH and corners (+ diagnostics)."""
    for row in market_rows:
        for forbidden in _FORBIDDEN_INPUT_KEYS:
            if forbidden in row:
                raise ValueError(f"model pick policy must not read {forbidden!r}")

    asian_handicap = _balanced_pick(
        market_rows=market_rows,
        family=MARKET_ASIAN_HANDICAP,
        positive="HOME",
        negative="AWAY",
        catalog=ASIAN_HANDICAP_LINES,
    )
    corner_total = _balanced_pick(
        market_rows=market_rows,
        family=MARKET_CORNER_TOTAL,
        positive="OVER",
        negative="UNDER",
        catalog=CORNER_TOTAL_LINES,
    )
    diagnostics: dict[str, Any] = {}
    if asian_handicap is None:
        diagnostics["asian_handicap"] = "NO_COMPLETE_PAIRED_LINE"
    if corner_total is None:
        diagnostics["corner_total"] = "NO_COMPLETE_PAIRED_LINE"
    return {
        "policy_id": POLICY_ID,
        "policy_version": POLICY_VERSION,
        "asian_handicap": asian_handicap.to_dict() if asian_handicap else None,
        "corner_total": corner_total.to_dict() if corner_total else None,
        "diagnostics": diagnostics,
    }
