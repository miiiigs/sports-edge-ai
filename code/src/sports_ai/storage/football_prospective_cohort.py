"""Prospective focused-cohort evidence and CLV for Football AH + Corners.

Historical/all-time reconciled paper performance is not, by itself, evidence
that newly published AH + Corners picks will keep performing. This module adds
an explicit, persisted, reproducible cohort boundary so the system can answer
two distinct questions:

1. What has Football done historically?  (all-time reconciled performance)
2. What have NEW AH/Corners positions done since the focus became effective?
   (prospective focused cohort)

CLV convention (preserved, not redefined)
-----------------------------------------
This repository's established CLV is the margin-adjusted decimal-odds CLV::

    clv = entry_odds / closing_odds / (1 + closing_margin) - 1

* ``entry_odds`` and ``closing_odds`` are decimal odds for the SAME market,
  selection and normalized line on the SAME canonical fixture.
* ``closing_margin`` is the overround of the persisted closing book.
* **Positive CLV** means the entry price beat the closing market (the entry odds
  exceed what the closing book implies after removing vig).
* **Negative CLV** means it did not.

CLV is price-quality evidence only. It is never derived from realized win/loss.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .football_paper_trading import margin_adjusted_clv, paper_trading_performance

FOCUS_COHORT_ID = "football_ah_corners_focus_v1"
FOCUS_MARKETS = ("asian_handicap", "corner_total")

# The AH + Corners focus landed on main 2026-09-27. Any focused position decided
# at or after this timestamp is prospective; everything earlier is historical.
DEFAULT_EFFECTIVE_FROM = "2026-09-27T00:00:00+00:00"

MIN_CLV_SAMPLE = 20

_FINAL_SETTLEMENT_STATES = {"WON", "LOST", "PUSH", "HALF_WIN", "HALF_LOSS", "VOID"}
_ACTIVE_SETTLEMENT_STATES = {"PENDING", "OPEN", "UNSETTLED"}
_STRIKE_RATE_STATES = {"WON", "LOST", "HALF_WIN", "HALF_LOSS"}

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS prospective_focus_cohort (
        cohort_id TEXT PRIMARY KEY,
        effective_from TEXT NOT NULL,
        focus_markets TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
)


@dataclass(frozen=True)
class ProspectiveCohort:
    cohort_id: str
    effective_from: str
    focus_markets: tuple[str, ...]


def default_cohort() -> ProspectiveCohort:
    return ProspectiveCohort(
        cohort_id=FOCUS_COHORT_ID,
        effective_from=DEFAULT_EFFECTIVE_FROM,
        focus_markets=FOCUS_MARKETS,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)


def _ensure_schema_if_requested(conn: sqlite3.Connection, *, initialize_schema: bool) -> None:
    if initialize_schema:
        ensure_schema(conn)


def record_cohort(
    conn: sqlite3.Connection,
    cohort: ProspectiveCohort,
    *,
    initialize_schema: bool = True,
) -> None:
    """Persist the cohort definition idempotently.

    Re-recording the SAME ``cohort_id`` with the SAME definition is a no-op.
    Re-recording with a DIFFERENT effective boundary is an integrity error so the
    prospective boundary can never be silently moved (preserves reproducibility
    and auditability after restart).
    """
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    existing = conn.execute(
        "SELECT effective_from, focus_markets FROM prospective_focus_cohort WHERE cohort_id = ?",
        (cohort.cohort_id,),
    ).fetchone()
    markets = ",".join(cohort.focus_markets)
    if existing is not None:
        if str(existing["effective_from"]) != cohort.effective_from or str(existing["focus_markets"]) != markets:
            raise sqlite3.IntegrityError(
                "PROSPECTIVE_COHORT_CONFLICT: "
                f"{cohort.cohort_id} already exists with effective_from="
                f"{existing['effective_from']} and cannot be redefined"
            )
        return
    conn.execute(
        """
        INSERT INTO prospective_focus_cohort (cohort_id, effective_from, focus_markets, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (cohort.cohort_id, cohort.effective_from, markets, _utc_now()),
    )


def fetch_cohort(conn: sqlite3.Connection, *, initialize_schema: bool = True) -> ProspectiveCohort | None:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    row = conn.execute(
        "SELECT cohort_id, effective_from, focus_markets FROM prospective_focus_cohort ORDER BY created_at LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return ProspectiveCohort(
        cohort_id=str(row["cohort_id"]),
        effective_from=str(row["effective_from"]),
        focus_markets=tuple(market for market in str(row["focus_markets"]).split(",") if market),
    )


def resolve_cohort(conn: sqlite3.Connection, *, initialize_schema: bool = True) -> ProspectiveCohort:
    """Fetch the persisted cohort, recording the default on first use."""
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    cohort = fetch_cohort(conn, initialize_schema=False)
    if cohort is None:
        cohort = default_cohort()
        record_cohort(conn, cohort, initialize_schema=False)
    return cohort


def cohort_decision_timestamp(row: dict[str, Any]) -> str:
    """The timestamp that places a position in the prospective or historical set."""
    return str(row.get("decision_timestamp") or row.get("created_at") or "")


def _parse_ts(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _ts_ge(left: str, right: str) -> bool:
    left_dt = _parse_ts(left)
    right_dt = _parse_ts(right)
    if left_dt is not None and right_dt is not None:
        return left_dt >= right_dt
    return left >= right


def _ts_lt(left: str, right: str) -> bool:
    left_dt = _parse_ts(left)
    right_dt = _parse_ts(right)
    if left_dt is not None and right_dt is not None:
        return left_dt < right_dt
    return left < right


def _ts_gt(left: str, right: str) -> bool:
    left_dt = _parse_ts(left)
    right_dt = _parse_ts(right)
    if left_dt is not None and right_dt is not None:
        return left_dt > right_dt
    return left > right


def is_prospective_focus_row(row: dict[str, Any], cohort: ProspectiveCohort) -> bool:
    """True when a paper row belongs to the prospective focused cohort.

    A row is prospective when its market is one of the focused markets AND its
    decision timestamp is at/after the cohort effective-from boundary.
    """
    market = str(row.get("market") or "").lower()
    if market not in cohort.focus_markets:
        return False
    timestamp = cohort_decision_timestamp(row)
    if not timestamp:
        return False
    return _ts_ge(timestamp, cohort.effective_from)


# ----------------------------------------------------------------- CLV helpers
def _normalize_line(line: Any) -> float | None:
    if line is None:
        return None
    try:
        normalized = float(line)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(normalized):
        return None
    return 0.0 if normalized == 0.0 else normalized


def closing_quote_matches_position(
    *,
    quote_market: str,
    quote_selection: str,
    quote_line: Any,
    position_market: str,
    position_selection: str,
    position_line: Any,
) -> bool:
    """Strict same-market / same-selection / same-normalized-line identity.

    Asian Handicap ``Home -0.25`` is NOT interchangeable with ``Home -0.5``.
    Corners ``Over 9.5`` is NOT interchangeable with ``Over 10.5``. A closing
    quote must match the exact normalized handicap/total line and selection.
    """
    if str(quote_market or "").lower() != str(position_market or "").lower():
        return False
    if str(quote_selection or "").upper() != str(position_selection or "").upper():
        return False
    return _normalize_line(quote_line) == _normalize_line(position_line)


def closing_line_value(*, entry_odds: float, closing_odds: float, closing_margin: float) -> float:
    """Margin-adjusted decimal-odds CLV (this repo's established definition).

    ``entry_odds / closing_odds / (1 + closing_margin) - 1``.

    Positive means the entry beat the closing market; negative means it did not.
    """
    return margin_adjusted_clv(
        entry_odds=float(entry_odds),
        closing_odds=float(closing_odds),
        closing_margin=float(closing_margin),
    )


def resolve_closing_quote(
    snapshots: list[dict[str, Any]],
    *,
    fixture_id: str,
    market: str,
    selection: str,
    line: float | None,
    entry_captured_at: str,
    kickoff_utc: str | None,
) -> dict[str, Any] | None:
    """Select the genuine closing quote for a position, or None.

    A quote qualifies only when it:
      * belongs to the SAME fixture, market, selection and normalized line
        (no unrelated-line substitution);
      * was captured at/after entry time (no lookahead);
      * was captured before kickoff (closing is a pre-match concept).

    The latest qualifying quote wins. Returns None when no genuine closing quote
    exists, so CLV fails closed instead of fabricating a price.
    """
    candidates: list[dict[str, Any]] = []
    for snapshot in snapshots:
        if str(snapshot.get("fixture_id") or "") != str(fixture_id or ""):
            continue
        if not closing_quote_matches_position(
            quote_market=str(snapshot.get("market") or ""),
            quote_selection=str(snapshot.get("selection") or ""),
            quote_line=snapshot.get("line"),
            position_market=market,
            position_selection=selection,
            position_line=line,
        ):
            continue
        captured_at = str(snapshot.get("captured_at") or "")
        if not captured_at:
            continue
        if entry_captured_at and _ts_lt(captured_at, entry_captured_at):
            continue
        if kickoff_utc and _ts_gt(captured_at, kickoff_utc):
            continue
        candidates.append(snapshot)
    if not candidates:
        return None
    return max(candidates, key=lambda snapshot: str(snapshot.get("captured_at") or ""))


# ------------------------------------------------------- cohort aggregation
def _stake_units_for_row(row: dict[str, Any]) -> float:
    payload = row.get("latest_update_payload") or {}
    stake = payload.get("stake")
    try:
        return 1.0 if stake is None else float(stake)
    except (TypeError, ValueError):
        return 1.0


def _strike_rate(rows: list[dict[str, Any]]) -> float | None:
    decision_rows = [row for row in rows if str(row.get("trade_state") or "") in _STRIKE_RATE_STATES]
    if not decision_rows:
        return None
    weighted_wins = sum(
        1.0 if row.get("trade_state") == "WON" else 0.5 if row.get("trade_state") == "HALF_WIN" else 0.0
        for row in decision_rows
    )
    return weighted_wins / len(decision_rows)


def clv_summary_for_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Margin-adjusted CLV summary using only genuinely matched closing quotes.

    Uses the persisted ``clv_pct`` field, which ``attach_closing_snapshot`` only
    populates for a strictly-matched, complete closing book. Rows with no closing
    evidence contribute zero to the sample and are reported via ``coverage``.
    """
    values: list[float] = []
    by_market_values: dict[str, list[float]] = {}
    for row in rows:
        raw = row.get("clv_pct")
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        values.append(value)
        by_market_values.setdefault(str(row.get("market") or "Unknown"), []).append(value)

    settled_count = sum(1 for row in rows if str(row.get("trade_state") or "") in _FINAL_SETTLEMENT_STATES)
    by_market = [
        {
            "market": market,
            "sample": len(market_values),
            "average_clv": sum(market_values) / len(market_values),
            "positive_rate": sum(1 for value in market_values if value > 0.0) / len(market_values),
        }
        for market, market_values in sorted(by_market_values.items())
    ]
    return {
        "sample": len(values),
        "coverage": None if settled_count == 0 else len(values) / settled_count,
        "average_clv": None if not values else sum(values) / len(values),
        "positive_rate": None if not values else sum(1 for value in values if value > 0.0) / len(values),
        "status": "SUFFICIENT" if len(values) >= MIN_CLV_SAMPLE else "INSUFFICIENT_DATA",
        "minimum_sample": MIN_CLV_SAMPLE,
        "by_market": by_market,
        "method_note": (
            "CLV uses the Football build-spec definition: "
            "entry_odds / closing_odds / (1 + closing_margin) - 1. "
            "Only strictly-matched, complete closing books are counted; "
            "missing closing evidence fails closed."
        ),
    }


def _market_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    settled = [row for row in rows if str(row.get("trade_state") or "") in _FINAL_SETTLEMENT_STATES]
    open_rows = [row for row in rows if str(row.get("trade_state") or "") in _ACTIVE_SETTLEMENT_STATES]
    net_units = sum(float(row.get("net_pnl") or 0.0) for row in settled)
    stake = sum(_stake_units_for_row(row) for row in settled)
    clv = clv_summary_for_rows(rows)
    return {
        "positions": len(rows),
        "settled_positions": len(settled),
        "pending_positions": len(open_rows),
        "wins": sum(1 for row in settled if row.get("trade_state") == "WON"),
        "losses": sum(1 for row in settled if row.get("trade_state") == "LOST"),
        "pushes": sum(1 for row in settled if row.get("trade_state") == "PUSH"),
        "half_wins": sum(1 for row in settled if row.get("trade_state") == "HALF_WIN"),
        "half_losses": sum(1 for row in settled if row.get("trade_state") == "HALF_LOSS"),
        "voids": sum(1 for row in settled if row.get("trade_state") == "VOID"),
        "net_units": net_units,
        "roi": None if stake <= 0 else net_units / stake,
        "strike_rate": _strike_rate(rows),
        "clv_sample": clv["sample"],
        "clv_coverage": clv["coverage"],
        "average_clv": clv["average_clv"],
    }


def prospective_focus_performance(
    rows: list[dict[str, Any]],
    cohort: ProspectiveCohort,
) -> dict[str, Any]:
    """Split ``rows`` into historical (all-time) and prospective focused cohorts.

    ``rows`` must already be the reconciled (duplicate-excluded) paper rows so
    provider namespace differences cannot double-count prospective performance.
    """
    prospective = [row for row in rows if is_prospective_focus_row(row, cohort)]
    by_market: dict[str, list[dict[str, Any]]] = {}
    for row in prospective:
        by_market.setdefault(str(row.get("market") or ""), []).append(row)

    if not prospective:
        sample_status = "NO_PROSPECTIVE_POSITIONS_YET"
    elif sum(1 for row in prospective if str(row.get("trade_state") or "") in _FINAL_SETTLEMENT_STATES) < 5:
        sample_status = "INSUFFICIENT_SAMPLE"
    else:
        sample_status = "TRACKING"

    return {
        "cohort": {
            "cohort_id": cohort.cohort_id,
            "effective_from": cohort.effective_from,
            "focus_markets": list(cohort.focus_markets),
        },
        "historical": paper_trading_performance(rows),
        "prospective": _market_summary(prospective),
        "by_market": {
            market: _market_summary(market_rows)
            for market, market_rows in sorted(by_market.items())
        },
        "clv": clv_summary_for_rows(prospective),
        "sample_status": sample_status,
    }
