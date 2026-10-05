"""Immutable Football MODEL PICK ledger (model-only, no bookmaker dependency).

This ledger is COMPLETELY SEPARATE from the value-bet/paper ledger:

* model picks are derived from persisted model-only Day-10 rows via the
  versioned ``football_model_pick_balanced_line_v1`` policy;
* no bookmaker price, edge, stake, units, ROI, or CLV is ever stored here;
* ``record_origin`` distinguishes ``LIVE_ISSUED`` from ``CURRENT_SYSTEM_REPLAY``
  and the two are never silently interconverted.

Settlement uses only the actual sporting result (score for AH, total corners for
corners) via the existing Football settlement semantics. No price is required.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..locking import canonical_hash

PROVIDER_AGNOSTIC_POLICY_LINE_CATALOG = "football_model_pick_balanced_line_v1"

RECORD_ORIGIN_LIVE_ISSUED = "LIVE_ISSUED"
RECORD_ORIGIN_CURRENT_SYSTEM_REPLAY = "CURRENT_SYSTEM_REPLAY"
RECORD_ORIGINS = frozenset({RECORD_ORIGIN_LIVE_ISSUED, RECORD_ORIGIN_CURRENT_SYSTEM_REPLAY})

# Settlement states (existing engine returns WON/LOST; normalised to FULL_*).
STATE_FULL_WIN = "FULL_WIN"
STATE_HALF_WIN = "HALF_WIN"
STATE_PUSH = "PUSH"
STATE_HALF_LOSS = "HALF_LOSS"
STATE_FULL_LOSS = "FULL_LOSS"
_FINAL_STATES = (STATE_FULL_WIN, STATE_HALF_WIN, STATE_PUSH, STATE_HALF_LOSS, STATE_FULL_LOSS)
_STATE_ALIASES = {"WON": STATE_FULL_WIN, "LOST": STATE_FULL_LOSS}

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS football_model_pick_entries (
        model_pick_id TEXT PRIMARY KEY,
        policy_id TEXT NOT NULL,
        policy_version TEXT NOT NULL,
        fixture_id TEXT NOT NULL,
        canonical_fixture_id TEXT,
        competition_id TEXT,
        competition_name TEXT,
        kickoff_utc TEXT,
        market TEXT NOT NULL,
        line REAL,
        selection TEXT NOT NULL,
        model_probability REAL,
        model_fair_price REAL,
        prediction_id TEXT,
        model_snapshot_id TEXT,
        model_version TEXT,
        prediction_generated_at TEXT,
        model_as_of TEXT,
        record_origin TEXT NOT NULL,
        source_prediction_version_key TEXT,
        trade_state TEXT,
        settlement_payload_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(policy_id, fixture_id, market)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_model_pick_fixture ON football_model_pick_entries(fixture_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_model_pick_kickoff ON football_model_pick_entries(kickoff_utc)
    """,
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


def model_pick_id(*, policy_id: str, fixture_id: str, market: str) -> str:
    return "model_pick_" + canonical_hash({"policy_id": policy_id, "fixture_id": fixture_id, "market": market})


def normalize_settlement_state(state: str | None) -> str | None:
    if state is None:
        return None
    return _STATE_ALIASES.get(str(state), str(state))


def record_model_pick(
    conn: sqlite3.Connection,
    pick: Mapping[str, Any],
    *,
    now: str | None = None,
    initialize_schema: bool = True,
) -> str:
    """Idempotently record a model pick (INSERT OR IGNORE on the unique key)."""
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    origin = str(pick["record_origin"])
    if origin not in RECORD_ORIGINS:
        raise ValueError(f"invalid record_origin {origin!r}")
    timestamp = now or _utc_now()
    pick_id = model_pick_id(policy_id=str(pick["policy_id"]), fixture_id=str(pick["fixture_id"]), market=str(pick["market"]))
    conn.execute(
        """
        INSERT OR IGNORE INTO football_model_pick_entries (
            model_pick_id, policy_id, policy_version, fixture_id, canonical_fixture_id,
            competition_id, competition_name, kickoff_utc, market, line, selection,
            model_probability, model_fair_price, prediction_id, model_snapshot_id,
            model_version, prediction_generated_at, model_as_of, record_origin,
            source_prediction_version_key, trade_state, settlement_payload_json,
            created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            pick_id,
            pick["policy_id"],
            pick["policy_version"],
            pick["fixture_id"],
            pick.get("canonical_fixture_id"),
            pick.get("competition_id"),
            pick.get("competition_name"),
            pick.get("kickoff_utc"),
            pick["market"],
            pick.get("line"),
            pick["selection"],
            pick.get("model_probability"),
            pick.get("model_fair_price"),
            pick.get("prediction_id"),
            pick.get("model_snapshot_id"),
            pick.get("model_version"),
            pick.get("prediction_generated_at"),
            pick.get("model_as_of"),
            origin,
            pick.get("source_prediction_version_key"),
            pick.get("trade_state"),
            None if pick.get("settlement_payload") is None else json.dumps(pick["settlement_payload"], sort_keys=True),
            timestamp,
            timestamp,
        ),
    )
    return pick_id


def update_settlement(
    conn: sqlite3.Connection,
    *,
    model_pick_id_value: str,
    trade_state: str,
    settlement_payload: Mapping[str, Any] | None = None,
    now: str | None = None,
    initialize_schema: bool = True,
) -> None:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    conn.execute(
        """
        UPDATE football_model_pick_entries
        SET trade_state = ?, settlement_payload_json = ?, updated_at = ?
        WHERE model_pick_id = ?
        """,
        (
            trade_state,
            None if settlement_payload is None else json.dumps(dict(settlement_payload), sort_keys=True),
            now or _utc_now(),
            model_pick_id_value,
        ),
    )


def fetch_model_pick_rows(
    conn: sqlite3.Connection,
    *,
    policy_id: str | None = None,
    initialize_schema: bool = True,
) -> list[dict[str, Any]]:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    if policy_id:
        rows = conn.execute(
            "SELECT * FROM football_model_pick_entries WHERE policy_id = ? ORDER BY kickoff_utc, market, selection",
            (policy_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM football_model_pick_entries ORDER BY kickoff_utc, market, selection"
        ).fetchall()
    return [dict(row) for row in rows]


def settle_rows(
    conn: sqlite3.Connection,
    results: Mapping[str, Mapping[str, Any]],
    *,
    now: str | None = None,
    initialize_schema: bool = True,
) -> int:
    """Settle every open model pick that has a usable sporting result.

    ``results`` maps fixture_id -> {"home_goals", "away_goals", "total_corners"}
    (total_corners may be absent for corners). Returns the number settled.
    """
    from .football_paper_trading import settlement_state_for_market

    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    settled = 0
    for row in fetch_model_pick_rows(conn, initialize_schema=False):
        if row.get("trade_state"):
            continue
        result = results.get(str(row["fixture_id"]))
        if not result:
            continue
        market = str(row["market"])
        if market == "asian_handicap":
            home = result.get("home_goals")
            away = result.get("away_goals")
            if home is None or away is None:
                continue
            state = settlement_state_for_market(
                market=market, selection=str(row["selection"]), line=row.get("line"),
                actual_home_goals=home, actual_away_goals=away,
            )
        else:
            total = result.get("total_corners")
            if total is None:
                continue
            state = settlement_state_for_market(
                market=market, selection=str(row["selection"]), line=row.get("line"),
                actual_home_goals=0, actual_away_goals=0, actual_total_corners=total,
            )
        normalized = normalize_settlement_state(state)
        update_settlement(
            conn,
            model_pick_id_value=str(row["model_pick_id"]),
            trade_state=str(normalized),
            settlement_payload={"raw_state": state, "result": dict(result)},
            now=now,
            initialize_schema=False,
        )
        settled += 1
    return settled


def model_pick_performance(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Independent model-pick metrics. NO ROI/units/CLV."""

    def _agg(subset: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        counts = {state: 0 for state in _FINAL_STATES}
        open_count = 0
        issued = 0
        for row in subset:
            issued += 1
            state = normalize_settlement_state(row.get("trade_state"))
            if state in counts:
                counts[state] += 1
            else:
                open_count += 1
        settled = sum(counts.values())
        decisive = counts[STATE_FULL_WIN] + counts[STATE_HALF_WIN] + counts[STATE_HALF_LOSS] + counts[STATE_FULL_LOSS]
        weighted = (
            (counts[STATE_FULL_WIN] + 0.5 * counts[STATE_HALF_WIN]) / decisive if decisive else None
        )
        return {
            "issued": issued,
            "settled": settled,
            "open": open_count,
            "full_wins": counts[STATE_FULL_WIN],
            "half_wins": counts[STATE_HALF_WIN],
            "pushes": counts[STATE_PUSH],
            "half_losses": counts[STATE_HALF_LOSS],
            "full_losses": counts[STATE_FULL_LOSS],
            "weighted_success_rate": weighted,
        }

    materialized = list(rows)
    return {
        "policy_id": PROVIDER_AGNOSTIC_POLICY_LINE_CATALOG,
        "overall": _agg(materialized),
        "asian_handicap": _agg([r for r in materialized if str(r.get("market")) == "asian_handicap"]),
        "corner_total": _agg([r for r in materialized if str(r.get("market")) == "corner_total"]),
        "by_origin": {
            origin: _agg([r for r in materialized if str(r.get("record_origin")) == origin])
            for origin in sorted(RECORD_ORIGINS)
        },
    }
