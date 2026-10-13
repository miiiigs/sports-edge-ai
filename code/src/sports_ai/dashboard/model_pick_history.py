"""Read-only MODEL PICK presentation payload (model-only, no bookmaker evidence).

This is a strict read model over ``football_model_pick_entries`` plus optional
dashboard fixture metadata. It NEVER writes, never reads bookmaker odds, and
never mixes model picks with the value-bet/paper ledger.

A MODEL PICK is a model reference line (not a bookmaker line, not a value bet).
Open picks are classified as ``UPCOMING`` (kickoff in the future) or
``RESULT_PENDING`` (kickoff passed, no verified final sporting truth) and are
never treated as losses.

Only FORWARD picks (ledger row written strictly before kickoff) feed the headline
``summary``, ``by_market`` and daily history. Retrospective backfills and rows
with unknown timing are retained and counted in ``by_issuance_timing`` for
audit, but no success rate is ever computed from them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..storage.football_model_pick_ledger import (
    ISSUANCE_TIMING_FORWARD,
    ISSUANCE_TIMING_RETROSPECTIVE,
    ISSUANCE_TIMING_UNKNOWN,
    issuance_timing,
)

POLICY_ID ="football_model_pick_balanced_line_v1"
POLICY_VERSION = "v1"

MARKET_ASIAN_HANDICAP = "asian_handicap"
MARKET_CORNER_TOTAL = "corner_total"
MARKETS = (MARKET_ASIAN_HANDICAP, MARKET_CORNER_TOTAL)

RECORD_ORIGIN_LIVE_ISSUED = "LIVE_ISSUED"
RECORD_ORIGIN_CURRENT_SYSTEM_REPLAY = "CURRENT_SYSTEM_REPLAY"

STATE_FULL_WIN = "FULL_WIN"
STATE_HALF_WIN = "HALF_WIN"
STATE_PUSH = "PUSH"
STATE_HALF_LOSS = "HALF_LOSS"
STATE_FULL_LOSS = "FULL_LOSS"
_FINAL_STATES = (STATE_FULL_WIN, STATE_HALF_WIN, STATE_PUSH, STATE_HALF_LOSS, STATE_FULL_LOSS)
_STATE_ALIASES = {"WON": STATE_FULL_WIN, "LOST": STATE_FULL_LOSS}

STATE_UPCOMING = "UPCOMING"
STATE_RESULT_PENDING = "RESULT_PENDING"

LABEL_WEIGHTED_SUCCESS = "Weighted Success"
WEIGHTED_SUCCESS_NOTE = "Full win = 1.0, half win = 0.5; pushes excluded."


def _parse(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _local_day(value: Any, tz: ZoneInfo) -> str | None:
    parsed = _parse(value)
    return None if parsed is None else parsed.astimezone(tz).date().isoformat()


def normalize_state(state: Any) -> str | None:
    if state is None:
        return None
    text = str(state)
    return _STATE_ALIASES.get(text, text)


def _weighted(fw: int, hw: int, hl: int, fl: int) -> float | None:
    decisive = fw + hw + hl + fl
    return round((fw + 0.5 * hw) / decisive, 4) if decisive else None


def _aggregate(rows: Sequence[Mapping[str, Any]], *, with_rate: bool = True) -> dict[str, Any]:
    counts = dict.fromkeys(_FINAL_STATES, 0)
    open_count = 0
    for row in rows:
        state = normalize_state(row.get("trade_state"))
        if state in counts:
            counts[state] += 1
        else:
            open_count += 1
    return {
        "issued": len(rows),
        "settled": sum(counts.values()),
        "open": open_count,
        "full_wins": counts[STATE_FULL_WIN],
        "half_wins": counts[STATE_HALF_WIN],
        "pushes": counts[STATE_PUSH],
        "half_losses": counts[STATE_HALF_LOSS],
        "full_losses": counts[STATE_FULL_LOSS],
        "weighted_success_rate": _weighted(
            counts[STATE_FULL_WIN], counts[STATE_HALF_WIN], counts[STATE_HALF_LOSS], counts[STATE_FULL_LOSS]
        )
        if with_rate
        else None,
    }


def _timing_cohorts(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        timing: _aggregate(
            [r for r in rows if issuance_timing(r) == timing], with_rate=timing == ISSUANCE_TIMING_FORWARD
        )
        for timing in (ISSUANCE_TIMING_FORWARD, ISSUANCE_TIMING_RETROSPECTIVE, ISSUANCE_TIMING_UNKNOWN)
    }


def _display_row(row: Mapping[str, Any], *, now: datetime, tz: ZoneInfo) -> dict[str, Any]:
    state = normalize_state(row.get("trade_state"))
    if state is None:
        kickoff = _parse(row.get("kickoff_utc"))
        state = STATE_UPCOMING if (kickoff and kickoff > now) else STATE_RESULT_PENDING
    return {
        "model_pick_id": row.get("model_pick_id"),
        "fixture_id": row.get("fixture_id"),
        "canonical_fixture_id": row.get("canonical_fixture_id"),
        "competition_id": row.get("competition_id"),
        "competition_name": row.get("competition_name"),
        "home_team_name": row.get("home_team_name"),
        "away_team_name": row.get("away_team_name"),
        "kickoff_utc": row.get("kickoff_utc"),
        "market": row.get("market"),
        "selection": row.get("selection"),
        "line": row.get("line"),
        "line_source": "model_reference_line",
        "model_probability": row.get("model_probability"),
        "model_fair_price": row.get("model_fair_price"),
        "result_state": state,
        "record_origin": row.get("record_origin"),
        "issuance_timing": issuance_timing(row),
        "forward_issued": issuance_timing(row) == ISSUANCE_TIMING_FORWARD,
        "prediction_generated_at": row.get("prediction_generated_at"),
    }


def _recent_days(rows: Sequence[Mapping[str, Any]], *, now: datetime, tz: ZoneInfo) -> list[dict[str, Any]]:
    today = now.astimezone(tz).date()
    by_day: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        day = _local_day(row.get("kickoff_utc"), tz)
        if day is None:
            continue
        if date.fromisoformat(day) > today:
            continue  # future excluded from historical history
        by_day.setdefault(day, []).append(row)
    days: list[dict[str, Any]] = []
    for day in sorted(by_day):
        day_rows = by_day[day]
        agg = _aggregate(day_rows)
        fixtures = {(r.get("canonical_fixture_id") or r.get("fixture_id")) for r in day_rows}
        days.append(
            {
                "date": day,
                "fixtures_represented": len(fixtures),
                "ah_issued": sum(1 for r in day_rows if r.get("market") == MARKET_ASIAN_HANDICAP),
                "corner_issued": sum(1 for r in day_rows if r.get("market") == MARKET_CORNER_TOTAL),
                "settled": agg["settled"],
                "open": agg["open"],
                "full_wins": agg["full_wins"],
                "half_wins": agg["half_wins"],
                "pushes": agg["pushes"],
                "half_losses": agg["half_losses"],
                "full_losses": agg["full_losses"],
                "weighted_success_rate": agg["weighted_success_rate"],
            }
        )
    return days


def _connect_readonly(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def build_model_pick_history_payload(
    *,
    model_pick_db_path: str | Path,
    timezone_name: str = "Asia/Manila",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read-only client payload. Degrades to ``available: False`` when absent."""
    resolved_now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(timezone_name)
    payload: dict[str, Any] = {
        "available": False,
        "generated_at": resolved_now.isoformat(timespec="seconds"),
        "policy": {"policy_id": POLICY_ID, "policy_version": POLICY_VERSION},
        "summary": _aggregate([]),
        "by_market": {m: _aggregate([]) for m in MARKETS},
        "headline_cohort": ISSUANCE_TIMING_FORWARD,
        "by_issuance_timing": _timing_cohorts([]),
        "forward_by_market": {m: _aggregate([]) for m in MARKETS},
        "excluded_from_headline": 0,
        "labels": {
            "latest_model_pick_issued_day": None,
            "latest_model_pick_scheduled_kickoff_day": None,
            "latest_settled_ah_fixture_day": None,
            "latest_settled_corners_fixture_day": None,
        },
        "weighted_success_label": LABEL_WEIGHTED_SUCCESS,
        "weighted_success_note": WEIGHTED_SUCCESS_NOTE,
        "recent_days": [],
        "recent_settled": [],
        "result_pending": [],
        "upcoming": [],
    }

    path = Path(model_pick_db_path)
    if not path.exists():
        return payload
    try:
        conn = _connect_readonly(path)
    except sqlite3.Error:
        return payload
    try:
        try:
            rows = [dict(r) for r in conn.execute("SELECT * FROM football_model_pick_entries")]
        except sqlite3.OperationalError:
            return payload
    finally:
        conn.close()

    payload["available"] = True
    forward = [r for r in rows if issuance_timing(r) == ISSUANCE_TIMING_FORWARD]
    ah = [r for r in forward if r.get("market") == MARKET_ASIAN_HANDICAP]
    corners = [r for r in forward if r.get("market") == MARKET_CORNER_TOTAL]
    payload["summary"] = _aggregate(forward)
    payload["by_market"] = {MARKET_ASIAN_HANDICAP: _aggregate(ah), MARKET_CORNER_TOTAL: _aggregate(corners)}
    payload["by_issuance_timing"] = _timing_cohorts(rows)
    payload["forward_by_market"] = payload["by_market"]
    payload["excluded_from_headline"] = len(rows) - len(forward)

    issued_days = [d for d in (_local_day(r.get("prediction_generated_at"), tz) for r in rows) if d]
    kickoff_days = [d for d in (_local_day(r.get("kickoff_utc"), tz) for r in rows) if d]
    settled_ah_days = [d for d in (_local_day(r.get("kickoff_utc"), tz) for r in ah if r.get("trade_state")) if d]
    settled_corner_days = [d for d in (_local_day(r.get("kickoff_utc"), tz) for r in corners if r.get("trade_state")) if d]
    payload["labels"] = {
        "latest_model_pick_issued_day": max(issued_days, default=None),
        "latest_model_pick_scheduled_kickoff_day": max(kickoff_days, default=None),
        "latest_settled_ah_fixture_day": max(settled_ah_days, default=None),
        "latest_settled_corners_fixture_day": max(settled_corner_days, default=None),
    }

    payload["recent_days"] = _recent_days(forward, now=resolved_now, tz=tz)
    settled = sorted(
        (r for r in rows if normalize_state(r.get("trade_state"))),
        key=lambda r: str(r.get("kickoff_utc") or ""),
        reverse=True,
    )
    payload["recent_settled"] = [_display_row(r, now=resolved_now, tz=tz) for r in settled[:50]]
    open_rows = [r for r in rows if not normalize_state(r.get("trade_state"))]
    upcoming = [r for r in open_rows if (_parse(r.get("kickoff_utc")) or resolved_now) > resolved_now]
    pending = [r for r in open_rows if (_parse(r.get("kickoff_utc")) or resolved_now) <= resolved_now]
    payload["upcoming"] = sorted(
        (_display_row(r, now=resolved_now, tz=tz) for r in upcoming), key=lambda r: str(r.get("kickoff_utc") or "")
    )
    payload["result_pending"] = sorted(
        (_display_row(r, now=resolved_now, tz=tz) for r in pending), key=lambda r: str(r.get("kickoff_utc") or "")
    )
    return payload
