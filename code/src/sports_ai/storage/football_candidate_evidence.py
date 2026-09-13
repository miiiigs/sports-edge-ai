"""Scientific evidence ledger for Football preliminary candidates.

This ledger is intentionally separate from the actionable paper-trade ledger.
Every fresh Day 11 preliminary candidate can be observed, audited, closed and
settled even when a downstream xG/context safeguard blocks the recommendation.
That lets us measure whether vetoes help without contaminating client ROI or
portfolio selection.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from ..locking import canonical_hash

CANDIDATE_EVIDENCE_SCHEMA_VERSION = "football_candidate_evidence_v1"
ACTIVE_STATES = {"PENDING", "OPEN", "UNSETTLED"}
FINAL_STATES = {"WON", "LOST", "PUSH", "HALF_WIN", "HALF_LOSS", "VOID"}

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS football_candidate_observations (
        observation_id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        fixture_id TEXT NOT NULL,
        prediction_id TEXT NOT NULL,
        model_snapshot_id TEXT NOT NULL,
        market TEXT NOT NULL,
        selection TEXT NOT NULL,
        line REAL,
        model_probability REAL,
        model_fair_price REAL NOT NULL,
        available_market_price REAL NOT NULL,
        edge REAL,
        minimum_acceptable_price REAL,
        decision_timestamp TEXT NOT NULL,
        kickoff TEXT NOT NULL,
        bookmaker TEXT,
        provider TEXT,
        entry_odds REAL NOT NULL,
        entry_captured_at TEXT NOT NULL,
        entry_snapshot_id INTEGER,
        closing_odds REAL,
        closing_captured_at TEXT,
        closing_snapshot_id INTEGER,
        trade_state TEXT NOT NULL DEFAULT 'PENDING',
        gross_return REAL,
        net_pnl REAL,
        settlement_payload_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS football_candidate_observation_audit (
        audit_id TEXT PRIMARY KEY,
        observation_id TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        action_eligible INTEGER NOT NULL,
        current_decision_state TEXT,
        day12_state TEXT,
        xg_status TEXT,
        context_mode TEXT,
        context_status TEXT,
        context_reason_codes_json TEXT NOT NULL,
        context_threshold_adjustment REAL,
        context_adjusted_threshold REAL,
        context_threshold_gate_passed INTEGER,
        shadow_context_status TEXT,
        shadow_context_reason_codes_json TEXT NOT NULL DEFAULT '[]',
        shadow_context_threshold_gate_passed INTEGER,
        diagnostics_json TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        FOREIGN KEY(observation_id) REFERENCES football_candidate_observations(observation_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_candidate_observations_fixture_state
    ON football_candidate_observations(fixture_id, trade_state, kickoff)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_candidate_audit_observation_time
    ON football_candidate_observation_audit(observation_id, observed_at, audit_id)
    """,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure_audit_columns(conn: sqlite3.Connection) -> None:
    existing = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(football_candidate_observation_audit)").fetchall()
    }
    columns = {
        "context_mode": "TEXT",
        "shadow_context_status": "TEXT",
        "shadow_context_reason_codes_json": "TEXT NOT NULL DEFAULT '[]'",
        "shadow_context_threshold_gate_passed": "INTEGER",
    }
    with conn:
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(
                    f"ALTER TABLE football_candidate_observation_audit ADD COLUMN {name} {definition}"
                )


def ensure_schema(conn: sqlite3.Connection) -> None:
    with conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
    _ensure_audit_columns(conn)


def observation_id_for_row(row: dict[str, Any]) -> str:
    return "obs_" + canonical_hash(
        {
            "fixture_id": str(row.get("fixture_id") or ""),
            "market": str(row.get("market_family") or row.get("market") or "").lower(),
            "selection": str(row.get("selection") or "").upper(),
            "line": None if row.get("line") is None else float(row["line"]),
        }
    )


def _shadow_context(row: dict[str, Any]) -> dict[str, Any]:
    diagnostics = dict(row.get("context_diagnostics") or {})
    nested = diagnostics.get("shadow_context")
    if isinstance(nested, dict):
        return dict(nested)
    status = row.get("context_shadow_status")
    if status is None:
        return {}
    return {
        "status": status,
        "reason_codes": list(row.get("context_shadow_reason_codes") or []),
        "threshold_adjustment": row.get("context_shadow_threshold_adjustment"),
        "adjusted_threshold": row.get("context_shadow_adjusted_threshold"),
        "threshold_gate_passed": row.get("context_shadow_threshold_gate_passed"),
        "diagnostics": dict(row.get("context_shadow_diagnostics") or {}),
    }


def _audit_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_as_of": row.get("market_as_of"),
        "market_freshness": row.get("market_freshness"),
        "decision": row.get("decision"),
        "reason_codes": list(row.get("reason_codes") or []),
        "settlement_aware_edge": row.get("settlement_aware_edge"),
        "best_available_price": row.get("best_available_price"),
        "best_available_bookmaker": row.get("best_available_bookmaker"),
        "best_available_source_timestamp": row.get("best_available_source_timestamp"),
        "day12_state": row.get("day12_state"),
        "xg_status": row.get("xg_status"),
        "xg_reason_codes": list(row.get("xg_reason_codes") or []),
        "context_mode": row.get("context_mode"),
        "context_status": row.get("context_status"),
        "context_reason_codes": list(row.get("context_reason_codes") or []),
        "context_threshold_adjustment": row.get("context_threshold_adjustment"),
        "context_adjusted_threshold": row.get("context_adjusted_threshold"),
        "context_threshold_gate_passed": row.get("context_threshold_gate_passed"),
        "shadow_context": _shadow_context(row),
        "context_diagnostics": dict(row.get("context_diagnostics") or {}),
        "paper_entry_eligible": bool(row.get("paper_entry_eligible")),
        "current_decision_state": row.get("current_decision_state"),
    }


def record_candidate_observation(
    conn: sqlite3.Connection,
    *,
    prediction: dict[str, Any],
    decision_row: dict[str, Any],
    provider: str,
) -> str | None:
    """Record one fresh preliminary candidate and append a distinct gate audit."""
    if str(decision_row.get("decision") or "") != "PRELIMINARY_CANDIDATE":
        return None
    if str(decision_row.get("market_freshness") or "") != "FRESH":
        return None
    best_price = decision_row.get("best_available_price")
    if best_price is None or float(best_price) <= 1.0:
        return None

    ensure_schema(conn)
    observation_id = observation_id_for_row(decision_row)
    now = _utc_now()
    observed_at = str(
        decision_row.get("best_available_source_timestamp")
        or decision_row.get("market_as_of")
        or now
    )
    entry_snapshot_id = decision_row.get("entry_snapshot_id")
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO football_candidate_observations (
                observation_id, schema_version, fixture_id, prediction_id,
                model_snapshot_id, market, selection, line, model_probability,
                model_fair_price, available_market_price, edge,
                minimum_acceptable_price, decision_timestamp, kickoff,
                bookmaker, provider, entry_odds, entry_captured_at,
                entry_snapshot_id, trade_state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
            """,
            (
                observation_id,
                CANDIDATE_EVIDENCE_SCHEMA_VERSION,
                str(prediction["fixture_id"]),
                str(prediction["prediction_id"]),
                str(prediction["model_snapshot_id"]),
                str(decision_row["market_family"]),
                str(decision_row["selection"]),
                None if decision_row.get("line") is None else float(decision_row["line"]),
                None if decision_row.get("model_probability") is None else float(decision_row["model_probability"]),
                float(decision_row["model_fair_decimal_price"]),
                float(best_price),
                None if decision_row.get("settlement_aware_edge") is None else float(decision_row["settlement_aware_edge"]),
                None if decision_row.get("minimum_acceptable_price") is None else float(decision_row["minimum_acceptable_price"]),
                str(decision_row["market_as_of"]),
                str(prediction["kickoff_utc"]),
                None if decision_row.get("best_available_bookmaker") is None else str(decision_row["best_available_bookmaker"]),
                provider,
                float(best_price),
                observed_at,
                entry_snapshot_id,
                now,
                now,
            ),
        )

        audit_payload = _audit_payload(decision_row)
        shadow = dict(audit_payload.get("shadow_context") or {})
        audit_id = "audit_" + canonical_hash(
            {
                "observation_id": observation_id,
                "observed_at": observed_at,
                "payload": audit_payload,
            }
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO football_candidate_observation_audit (
                audit_id, observation_id, schema_version, observed_at,
                action_eligible, current_decision_state, day12_state, xg_status,
                context_mode, context_status, context_reason_codes_json,
                context_threshold_adjustment, context_adjusted_threshold,
                context_threshold_gate_passed, shadow_context_status,
                shadow_context_reason_codes_json, shadow_context_threshold_gate_passed,
                diagnostics_json, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                observation_id,
                CANDIDATE_EVIDENCE_SCHEMA_VERSION,
                observed_at,
                1 if bool(decision_row.get("paper_entry_eligible")) else 0,
                None if decision_row.get("current_decision_state") is None else str(decision_row["current_decision_state"]),
                None if decision_row.get("day12_state") is None else str(decision_row["day12_state"]),
                None if decision_row.get("xg_status") is None else str(decision_row["xg_status"]),
                None if decision_row.get("context_mode") is None else str(decision_row["context_mode"]),
                None if decision_row.get("context_status") is None else str(decision_row["context_status"]),
                json.dumps(list(decision_row.get("context_reason_codes") or []), sort_keys=True),
                None
                if decision_row.get("context_threshold_adjustment") is None
                else float(decision_row["context_threshold_adjustment"]),
                None
                if decision_row.get("context_adjusted_threshold") is None
                else float(decision_row["context_adjusted_threshold"]),
                None
                if decision_row.get("context_threshold_gate_passed") is None
                else (1 if bool(decision_row["context_threshold_gate_passed"]) else 0),
                None if shadow.get("status") is None else str(shadow["status"]),
                json.dumps(list(shadow.get("reason_codes") or []), sort_keys=True),
                None
                if shadow.get("threshold_gate_passed") is None
                else (1 if bool(shadow["threshold_gate_passed"]) else 0),
                json.dumps(dict(decision_row.get("context_diagnostics") or {}), sort_keys=True, default=str),
                json.dumps(audit_payload, sort_keys=True, default=str),
            ),
        )
    return observation_id


def fetch_observations(conn: sqlite3.Connection, *, active_only: bool = False) -> list[dict[str, Any]]:
    ensure_schema(conn)
    where = "WHERE trade_state IN ('PENDING', 'OPEN', 'UNSETTLED')" if active_only else ""
    rows = conn.execute(
        f"""
        SELECT *
        FROM football_candidate_observations
        {where}
        ORDER BY decision_timestamp, observation_id
        """
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        audit = conn.execute(
            """
            SELECT *
            FROM football_candidate_observation_audit
            WHERE observation_id = ?
            ORDER BY observed_at DESC, audit_id DESC
            LIMIT 1
            """,
            (item["observation_id"],),
        ).fetchone()
        if audit is not None:
            item["latest_audit"] = dict(audit)
            item["latest_context_reason_codes"] = json.loads(str(audit["context_reason_codes_json"]))
            item["latest_shadow_context_reason_codes"] = json.loads(
                str(audit["shadow_context_reason_codes_json"] or "[]")
            )
            item["latest_diagnostics"] = json.loads(str(audit["diagnostics_json"]))
        else:
            item["latest_audit"] = None
            item["latest_context_reason_codes"] = []
            item["latest_shadow_context_reason_codes"] = []
            item["latest_diagnostics"] = {}
        result.append(item)
    return result


def fetch_observation(
    conn: sqlite3.Connection,
    *,
    fixture_id: str,
    market: str,
    selection: str,
    line: float | None,
) -> dict[str, Any] | None:
    ensure_schema(conn)
    observation_id = "obs_" + canonical_hash(
        {
            "fixture_id": fixture_id,
            "market": market.lower(),
            "selection": selection.upper(),
            "line": None if line is None else float(line),
        }
    )
    row = conn.execute(
        "SELECT * FROM football_candidate_observations WHERE observation_id = ?",
        (observation_id,),
    ).fetchone()
    return None if row is None else dict(row)


def attach_closing_snapshot(
    conn: sqlite3.Connection,
    *,
    observation_id: str,
    closing_odds: float,
    closing_captured_at: str,
    closing_snapshot_id: int | None,
) -> None:
    ensure_schema(conn)
    with conn:
        conn.execute(
            """
            UPDATE football_candidate_observations
            SET closing_odds = ?, closing_captured_at = ?, closing_snapshot_id = ?, updated_at = ?
            WHERE observation_id = ? AND closing_odds IS NULL
            """,
            (closing_odds, closing_captured_at, closing_snapshot_id, _utc_now(), observation_id),
        )


def update_trade_state(
    conn: sqlite3.Connection,
    *,
    observation_id: str,
    trade_state: str,
    gross_return: float | None,
    net_pnl: float | None,
    settlement_payload: dict[str, Any],
) -> None:
    if trade_state not in ACTIVE_STATES | FINAL_STATES:
        raise ValueError(f"invalid candidate evidence trade state {trade_state!r}")
    ensure_schema(conn)
    with conn:
        conn.execute(
            """
            UPDATE football_candidate_observations
            SET trade_state = ?, gross_return = ?, net_pnl = ?,
                settlement_payload_json = ?, updated_at = ?
            WHERE observation_id = ?
            """,
            (
                trade_state,
                gross_return,
                net_pnl,
                json.dumps(settlement_payload, sort_keys=True, default=str),
                _utc_now(),
                observation_id,
            ),
        )


def _cohort_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    settled = [row for row in rows if str(row.get("trade_state") or "") in FINAL_STATES]
    net_units = sum(float(row.get("net_pnl") or 0.0) for row in settled)
    clv_values: list[float] = []
    for row in rows:
        try:
            entry = float(row["entry_odds"])
            closing = float(row["closing_odds"])
        except (KeyError, TypeError, ValueError):
            continue
        if entry <= 1.0 or closing <= 1.0 or row.get("closing_snapshot_id") in {None, ""}:
            continue
        clv_values.append((1.0 / closing) - (1.0 / entry))
    return {
        "observations": len(rows),
        "settled": len(settled),
        "net_units": net_units,
        "roi": None if not settled else net_units / len(settled),
        "clv_sample": len(clv_values),
        "average_clv": None if not clv_values else sum(clv_values) / len(clv_values),
        "positive_clv_rate": None
        if not clv_values
        else sum(1 for value in clv_values if value > 0.0) / len(clv_values),
    }


def evidence_cohort_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    """Compare all, actionable, actual-veto and shadow-veto candidate cohorts."""
    rows = fetch_observations(conn)
    actionable: list[dict[str, Any]] = []
    actual_veto: list[dict[str, Any]] = []
    shadow_veto: list[dict[str, Any]] = []
    for row in rows:
        audit = dict(row.get("latest_audit") or {})
        if int(audit.get("action_eligible") or 0) == 1:
            actionable.append(row)
        if (
            str(audit.get("context_status") or "") == "VETO"
            or str(audit.get("day12_state") or "")
            in {"REJECTED_BY_XG_VETO", "REJECTED_BY_MULTIPLE_VETOES"}
        ):
            actual_veto.append(row)
        shadow_status = str(audit.get("shadow_context_status") or "")
        shadow_threshold_failed = audit.get("shadow_context_threshold_gate_passed") == 0
        if shadow_status == "VETO" or (shadow_status == "FLAG" and shadow_threshold_failed):
            shadow_veto.append(row)
    return {
        "all_candidates": _cohort_metrics(rows),
        "actionable": _cohort_metrics(actionable),
        "actual_veto": _cohort_metrics(actual_veto),
        "shadow_veto": _cohort_metrics(shadow_veto),
        "clv_definition": "(1 / closing odds) - (1 / entry odds)",
    }


def evidence_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    ensure_schema(conn)
    total = int(conn.execute("SELECT COUNT(*) FROM football_candidate_observations").fetchone()[0])
    settled = int(
        conn.execute(
            "SELECT COUNT(*) FROM football_candidate_observations WHERE trade_state IN ('WON','LOST','PUSH','HALF_WIN','HALF_LOSS','VOID')"
        ).fetchone()[0]
    )
    vetoed = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT observation_id)
            FROM football_candidate_observation_audit
            WHERE context_status = 'VETO'
               OR day12_state IN ('REJECTED_BY_XG_VETO','REJECTED_BY_MULTIPLE_VETOES')
            """
        ).fetchone()[0]
    )
    shadow_vetoed = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT observation_id)
            FROM football_candidate_observation_audit
            WHERE shadow_context_status = 'VETO'
               OR (shadow_context_status = 'FLAG' AND shadow_context_threshold_gate_passed = 0)
            """
        ).fetchone()[0]
    )
    actionable = int(
        conn.execute(
            "SELECT COUNT(DISTINCT observation_id) FROM football_candidate_observation_audit WHERE action_eligible = 1"
        ).fetchone()[0]
    )
    return {
        "schema_version": CANDIDATE_EVIDENCE_SCHEMA_VERSION,
        "observations": total,
        "settled": settled,
        "vetoed_observations": vetoed,
        "shadow_vetoed_observations": shadow_vetoed,
        "ever_actionable_observations": actionable,
        "cohorts": evidence_cohort_metrics(conn),
    }
