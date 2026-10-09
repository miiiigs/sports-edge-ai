"""Read-only evidence-integrity diagnostics for frozen Value candidates.

A valid two-sided football market must satisfy an implied-probability sum
(overround) of at least 1.0 for its paired selections at the SAME line
(e.g. AH HOME/AWAY, Corners OVER/UNDER). A sum below 1.0 is a guaranteed-profit
arbitrage and is impossible for a real bookmaker, so it proves the two quotes are
NOT genuine opposite sides at the same physical line.

This module performs no writes and no provider calls.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

INTEGRITY_VALID = "VALID"
INTEGRITY_BOOK_PAIRING_INCONSISTENT = "BOOK_PAIRING_INCONSISTENT"
INTEGRITY_REFERENCE_INCONSISTENT = "REFERENCE_INCONSISTENT"
INTEGRITY_MISSING_PAIR = "MISSING_PAIR"

# A real two-outcome book overround is normally ~1.02-1.15. Allow a modest band;
# anything below 1.0 is mathematically impossible (arbitrage).
PAIR_IMPLIED_MIN = 1.0
PAIR_IMPLIED_MAX = 1.30

_AH_SELECTIONS = ("HOME", "AWAY")
_CORNER_SELECTIONS = ("OVER", "UNDER")


def expected_selections_for_market(market: str) -> tuple[str, str]:
    return _CORNER_SELECTIONS if str(market) == "corner_total" else _AH_SELECTIONS


def implied_probability_sum(odds: Sequence[float]) -> float:
    return sum(1.0 / float(o) for o in odds)


def classify_pair_integrity(odds: Sequence[float]) -> str:
    if any(o is None or float(o) <= 1.0 for o in odds):
        return INTEGRITY_MISSING_PAIR
    total = implied_probability_sum(odds)
    if total < PAIR_IMPLIED_MIN or total > PAIR_IMPLIED_MAX:
        return INTEGRITY_BOOK_PAIRING_INCONSISTENT
    return INTEGRITY_VALID


def _bookmaker_key(value: Any) -> str:
    return "".join(ch for ch in str(value or "").strip().lower() if ch.isalnum())


def pair_odds_at_line(
    rows: Sequence[Mapping[str, Any]],
    *,
    bookmaker: str,
    market: str,
    line: float,
    selections: Sequence[str],
) -> dict[str, float]:
    """Latest odds per expected selection for one bookmaker at the exact line."""
    wanted = {str(s) for s in selections}
    out: dict[str, float] = {}
    latest_captured: dict[str, str] = {}
    for row in rows:
        if _bookmaker_key(row.get("bookmaker")) != _bookmaker_key(bookmaker):
            continue
        if str(row.get("market") or "") != str(market):
            continue
        if row.get("line") is None or abs(float(row["line"]) - float(line)) > 1e-12:
            continue
        selection = str(row.get("selection") or "")
        if selection not in wanted:
            continue
        captured = str(row.get("captured_at") or "")
        if selection in out and captured < latest_captured.get(selection, ""):
            continue
        out[selection] = float(row["odds"])
        latest_captured[selection] = captured
    return out


def audit_book_pair(
    rows: Sequence[Mapping[str, Any]],
    *,
    bookmaker: str,
    market: str,
    line: float,
) -> dict[str, Any]:
    selections = expected_selections_for_market(market)
    odds = pair_odds_at_line(rows, bookmaker=bookmaker, market=market, line=line, selections=selections)
    ordered = [odds.get(selection) for selection in selections]
    state = classify_pair_integrity([o for o in ordered if o is not None]) if all(
        o is not None for o in ordered
    ) else INTEGRITY_MISSING_PAIR
    total = None if any(o is None for o in ordered) else round(implied_probability_sum(ordered), 4)
    return {
        "bookmaker": str(bookmaker),
        "market": str(market),
        "line": float(line),
        "selections": dict(zip(selections, ordered)),
        "implied_probability_sum": total,
        "state": state,
    }


def audit_candidate_integrity(
    rows: Sequence[Mapping[str, Any]],
    *,
    market: str,
    line: float,
    best_bookmaker: str,
    reference_bookmaker: str,
) -> dict[str, Any]:
    book = audit_book_pair(rows, bookmaker=best_bookmaker, market=market, line=line)
    reference = audit_book_pair(rows, bookmaker=reference_bookmaker, market=market, line=line)
    if book["state"] != INTEGRITY_VALID:
        overall = INTEGRITY_BOOK_PAIRING_INCONSISTENT
    elif reference["state"] != INTEGRITY_VALID:
        overall = INTEGRITY_REFERENCE_INCONSISTENT
    else:
        overall = INTEGRITY_VALID
    return {"book_pair": book, "reference_pair": reference, "state": overall}
