"""Immutable Football Edge Desk shadow ledger storage."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..locking import canonical_hash

ROUTE_B_COMPOSITE_AGENT_ID = "ROUTE_B_COMPOSITE"
ACTIVE_AGENT_STATUS = "ACTIVE"
CAPABILITY_SUPPORTED = "SUPPORTED"
CAPABILITY_NOT_SUPPORTED = "NOT_SUPPORTED"
CAPABILITY_INACTIVE = "INACTIVE"
PREDICTION_STATUSES = {"PREDICTED", "ABSTAINED", "BLOCKED", "NOT_APPLICABLE"}
CONSENSUS_STATUSES = {"PASSED", "FAILED", "INSUFFICIENT_AGENTS", "NOT_EVALUABLE"}
DECISION_STATUSES = {"QUALIFIED", "NO_BET"}
DECISION_REASON_CODES = {
    "CONSENSUS_INSUFFICIENT_AGENTS",
    "CONSENSUS_DIRECTIONAL_DISAGREEMENT",
    "CONSENSUS_PROBABILITY_SPREAD",
    "AGENT_BLOCKED",
    "AGENT_ABSTAINED",
    "VALUE_FAILED",
    "RISK_FAILED",
    "MARKET_STALE",
    "OTHER",
}
SUPPORTED_ROUTE_B_MARKET_FAMILIES = (
    "1x2",
    "asian_handicap",
    "btts",
    "corner_total",
    "goals_total",
)
FOUNDATION_VERSION = "football_edge_desk_foundation_v1"
FOUNDATION_CREATED_AT = "2026-08-26T00:00:00+00:00"
DEFAULT_CONSENSUS_CONFIG_ID = "football_edge_desk_shadow_consensus"
DEFAULT_CONSENSUS_VERSION = "football_edge_desk_shadow_consensus_v1"
DEFAULT_CONSENSUS_VALID_FROM = "2026-08-26T00:00:00+00:00"

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS edge_desk_agent_registry (
        registry_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        version_tag TEXT NOT NULL,
        name TEXT NOT NULL,
        method_family TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        UNIQUE(agent_id, version_tag)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS edge_desk_agent_market_capabilities (
        capability_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL,
        market_family TEXT NOT NULL,
        version_tag TEXT NOT NULL,
        capability_status TEXT NOT NULL,
        valid_from TEXT NOT NULL,
        created_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        UNIQUE(agent_id, market_family, version_tag)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS edge_desk_consensus_configs (
        config_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        config_id TEXT NOT NULL,
        market_family TEXT,
        version_tag TEXT NOT NULL,
        required_agents_json TEXT NOT NULL,
        minimum_quorum INTEGER NOT NULL,
        same_direction_required INTEGER NOT NULL,
        max_probability_spread REAL,
        valid_from TEXT NOT NULL,
        created_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        UNIQUE(config_id, market_family, version_tag)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS edge_desk_agent_predictions (
        prediction_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        prediction_id TEXT NOT NULL UNIQUE,
        prediction_key TEXT NOT NULL UNIQUE,
        fixture_id TEXT NOT NULL,
        kickoff_utc TEXT,
        agent_id TEXT NOT NULL,
        market_family TEXT NOT NULL,
        line REAL,
        selection TEXT,
        probability REAL,
        fair_price REAL,
        prediction_status TEXT NOT NULL,
        as_of TEXT,
        generated_at TEXT NOT NULL,
        model_version TEXT,
        model_snapshot_id TEXT,
        source_prediction_version TEXT,
        source_payload_hash TEXT NOT NULL,
        block_reason TEXT,
        source_references_json TEXT NOT NULL,
        metadata_json TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS edge_desk_consensus_evaluations (
        consensus_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        consensus_id TEXT NOT NULL UNIQUE,
        consensus_key TEXT NOT NULL UNIQUE,
        fixture_id TEXT NOT NULL,
        kickoff_utc TEXT,
        market_family TEXT NOT NULL,
        line REAL,
        config_id TEXT NOT NULL,
        config_version_tag TEXT NOT NULL,
        participating_agents_json TEXT NOT NULL,
        prediction_ids_json TEXT NOT NULL,
        agreed_selection TEXT,
        consensus_probability REAL,
        probability_spread REAL,
        status TEXT NOT NULL,
        reason TEXT NOT NULL,
        evaluated_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS edge_desk_decision_history (
        decision_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_event_id TEXT NOT NULL UNIQUE,
        decision_key TEXT NOT NULL UNIQUE,
        fixture_id TEXT NOT NULL,
        kickoff_utc TEXT,
        market_family TEXT NOT NULL,
        line REAL,
        selection TEXT,
        consensus_id TEXT,
        agent_prediction_ids_json TEXT NOT NULL,
        decision_status TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        reason_detail TEXT,
        decided_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
)

IMMUTABLE_TABLES = (
    "edge_desk_agent_registry",
    "edge_desk_agent_market_capabilities",
    "edge_desk_consensus_configs",
    "edge_desk_agent_predictions",
    "edge_desk_consensus_evaluations",
    "edge_desk_decision_history",
)


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    with conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
        for table_name in IMMUTABLE_TABLES:
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table_name}_prevent_update
                BEFORE UPDATE ON {table_name}
                BEGIN
                    SELECT RAISE(ABORT, '{table_name} is immutable');
                END
                """
            )
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table_name}_prevent_delete
                BEFORE DELETE ON {table_name}
                BEGIN
                    SELECT RAISE(ABORT, '{table_name} is immutable');
                END
                """
            )
    seed_foundation(conn)


def seed_foundation(conn: sqlite3.Connection) -> None:
    ensure_schema_only(conn)
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO edge_desk_agent_registry (
                agent_id, version_tag, name, method_family, status, created_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ROUTE_B_COMPOSITE_AGENT_ID,
                FOUNDATION_VERSION,
                "Route B Composite",
                "GAP_TO_XG_TO_DIXON_COLES_TO_PRICING",
                ACTIVE_AGENT_STATUS,
                FOUNDATION_CREATED_AT,
                _json_dumps(
                    {
                        "shadow_mode": True,
                        "notes": "Current implemented Football predictive agent routed through the immutable Edge Desk ledger.",
                    }
                ),
            ),
        )
        for market_family in SUPPORTED_ROUTE_B_MARKET_FAMILIES:
            conn.execute(
                """
                INSERT OR IGNORE INTO edge_desk_agent_market_capabilities (
                    agent_id, market_family, version_tag, capability_status, valid_from, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ROUTE_B_COMPOSITE_AGENT_ID,
                    market_family,
                    FOUNDATION_VERSION,
                    CAPABILITY_SUPPORTED,
                    FOUNDATION_CREATED_AT,
                    FOUNDATION_CREATED_AT,
                    _json_dumps({"shadow_mode": True}),
                ),
            )
        conn.execute(
            """
            INSERT OR IGNORE INTO edge_desk_consensus_configs (
                config_id, market_family, version_tag, required_agents_json, minimum_quorum,
                same_direction_required, max_probability_spread, valid_from, created_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                DEFAULT_CONSENSUS_CONFIG_ID,
                None,
                DEFAULT_CONSENSUS_VERSION,
                _json_dumps([]),
                2,
                1,
                0.10,
                DEFAULT_CONSENSUS_VALID_FROM,
                FOUNDATION_CREATED_AT,
                _json_dumps(
                    {
                        "shadow_mode": True,
                        "phase": "1A",
                        "notes": "Minimum quorum stays above the currently implemented Route B-only state so shadow consensus cannot falsely pass.",
                    }
                ),
            ),
        )


def ensure_schema_only(conn: sqlite3.Connection) -> None:
    with conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
        for table_name in IMMUTABLE_TABLES:
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table_name}_prevent_update
                BEFORE UPDATE ON {table_name}
                BEGIN
                    SELECT RAISE(ABORT, '{table_name} is immutable');
                END
                """
            )
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table_name}_prevent_delete
                BEFORE DELETE ON {table_name}
                BEGIN
                    SELECT RAISE(ABORT, '{table_name} is immutable');
                END
                """
            )


def list_active_agents(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT agent_id, version_tag, name, method_family, status, created_at, metadata_json
        FROM edge_desk_agent_registry
        WHERE status = ?
        ORDER BY created_at, registry_row_id
        """,
        (ACTIVE_AGENT_STATUS,),
    ).fetchall()
    result = []
    for row in rows:
        payload = dict(row)
        payload["metadata"] = json.loads(str(payload.pop("metadata_json")))
        result.append(payload)
    return result


def list_market_capabilities(
    conn: sqlite3.Connection,
    *,
    agent_id: str | None = None,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    sql = """
        SELECT agent_id, market_family, version_tag, capability_status, valid_from, created_at, metadata_json
        FROM edge_desk_agent_market_capabilities
    """
    params: list[Any] = []
    if agent_id is not None:
        sql += " WHERE agent_id = ?"
        params.append(agent_id)
    sql += " ORDER BY created_at, capability_row_id"
    rows = conn.execute(sql, params).fetchall()
    result = []
    for row in rows:
        payload = dict(row)
        payload["metadata"] = json.loads(str(payload.pop("metadata_json")))
        result.append(payload)
    return result


def fetch_agent_predictions(
    conn: sqlite3.Connection,
    *,
    fixture_id: str | None = None,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    sql = """
        SELECT *
        FROM edge_desk_agent_predictions
    """
    params: list[Any] = []
    if fixture_id is not None:
        sql += " WHERE fixture_id = ?"
        params.append(fixture_id)
    sql += " ORDER BY generated_at, prediction_row_id"
    return [_deserialize_prediction_row(dict(row)) for row in conn.execute(sql, params).fetchall()]


def fetch_consensus_evaluations(
    conn: sqlite3.Connection,
    *,
    fixture_id: str | None = None,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    sql = """
        SELECT *
        FROM edge_desk_consensus_evaluations
    """
    params: list[Any] = []
    if fixture_id is not None:
        sql += " WHERE fixture_id = ?"
        params.append(fixture_id)
    sql += " ORDER BY evaluated_at, consensus_row_id"
    return [_deserialize_consensus_row(dict(row)) for row in conn.execute(sql, params).fetchall()]


def fetch_decision_history(
    conn: sqlite3.Connection,
    *,
    fixture_id: str | None = None,
) -> list[dict[str, Any]]:
    ensure_schema(conn)
    sql = """
        SELECT *
        FROM edge_desk_decision_history
    """
    params: list[Any] = []
    if fixture_id is not None:
        sql += " WHERE fixture_id = ?"
        params.append(fixture_id)
    sql += " ORDER BY decided_at, decision_row_id"
    return [_deserialize_decision_row(dict(row)) for row in conn.execute(sql, params).fetchall()]


def record_shadow_prediction_bundle(
    conn: sqlite3.Connection,
    *,
    fixture: dict[str, Any],
    prediction: dict[str, Any],
) -> dict[str, Any]:
    ensure_schema(conn)
    prediction_rows = _build_agent_prediction_rows(fixture=fixture, prediction=prediction)
    stored_predictions = [_insert_prediction_row(conn, row) for row in prediction_rows]
    consensus_rows = []
    decision_rows = []
    for grouped_rows in _group_predictions_by_market(stored_predictions):
        consensus_row = _build_consensus_row(conn, grouped_rows)
        stored_consensus = _insert_consensus_row(conn, consensus_row)
        consensus_rows.append(stored_consensus)
        decision_row = _build_decision_row(grouped_rows, stored_consensus)
        decision_rows.append(_insert_decision_row(conn, decision_row))
    return {
        "prediction_rows": stored_predictions,
        "consensus_rows": consensus_rows,
        "decision_rows": decision_rows,
    }


def _build_agent_prediction_rows(
    *,
    fixture: dict[str, Any],
    prediction: dict[str, Any],
) -> list[dict[str, Any]]:
    generated_at = str(prediction.get("generated_at") or fixture.get("kickoff_utc") or FOUNDATION_CREATED_AT)
    kickoff_utc = None if fixture.get("kickoff_utc") is None else str(fixture["kickoff_utc"])
    source_prediction_version = str(prediction.get("version_key") or prediction.get("prediction_id") or "")
    model_snapshot_id = None if prediction.get("model_snapshot_id") is None else str(prediction["model_snapshot_id"])
    statuses = {
        "READY": "PREDICTED",
        "BLOCKED": "BLOCKED",
        "FAILED": "BLOCKED",
    }
    prediction_status = statuses.get(str(prediction.get("prediction_status")), "NOT_APPLICABLE")
    model_versions = dict(prediction.get("model_versions") or {})

    if prediction_status == "PREDICTED":
        rows: list[dict[str, Any]] = []
        for market_row in list(prediction.get("market_rows") or []):
            source_payload_hash = canonical_hash(market_row)
            model_version = (
                str(market_row.get("pricing_model_version"))
                if market_row.get("pricing_model_version") is not None
                else str(model_versions.get("current_snapshot_model_version") or prediction.get("prediction_version") or "")
            )
            payload = {
                "fixture_id": str(fixture["fixture_id"]),
                "kickoff_utc": kickoff_utc,
                "agent_id": ROUTE_B_COMPOSITE_AGENT_ID,
                "market_family": str(market_row["market_family"]),
                "line": None if market_row.get("line") is None else float(market_row["line"]),
                "selection": str(market_row["selection"]),
                "probability": float(market_row["effective_win_stake_probability"]),
                "fair_price": float(market_row["fair_decimal_price"]),
                "prediction_status": "PREDICTED",
                "as_of": str(market_row.get("as_of") or prediction.get("model_as_of") or ""),
                "generated_at": generated_at,
                "model_version": model_version,
                "model_snapshot_id": model_snapshot_id,
                "source_prediction_version": source_prediction_version,
                "source_payload_hash": source_payload_hash,
                "block_reason": None,
                "source_references": {
                    "source_scoreline_object_id": market_row.get("source_scoreline_object_id"),
                    "source_corner_object_id": market_row.get("source_corner_object_id"),
                },
                "metadata": {
                    "competition_id": prediction.get("competition_id"),
                    "competition_name": prediction.get("competition_name"),
                    "team_resolution": prediction.get("team_resolution"),
                    "market_settlement_probabilities": market_row.get("settlement_probabilities"),
                    "component_lines": market_row.get("component_lines"),
                },
                "payload": market_row,
            }
            payload["prediction_key"] = _prediction_key(payload)
            payload["prediction_id"] = f"edge_pred_{payload['prediction_key']}"
            rows.append(payload)
        return rows

    supported_families = [row["market_family"] for row in _latest_capability_rows_for_agent(ROUTE_B_COMPOSITE_AGENT_ID).values()]
    blocked_rows = []
    blocker_text = ",".join(str(item) for item in list(prediction.get("blockers") or [])) or None
    for market_family in supported_families:
        source_payload = {
            "fixture_id": fixture["fixture_id"],
            "market_family": market_family,
            "prediction_status": prediction_status,
            "blockers": list(prediction.get("blockers") or []),
            "warnings": list(prediction.get("warnings") or []),
            "prediction_version": source_prediction_version,
        }
        payload = {
            "fixture_id": str(fixture["fixture_id"]),
            "kickoff_utc": kickoff_utc,
            "agent_id": ROUTE_B_COMPOSITE_AGENT_ID,
            "market_family": market_family,
            "line": None,
            "selection": None,
            "probability": None,
            "fair_price": None,
            "prediction_status": prediction_status,
            "as_of": str(prediction.get("model_as_of") or ""),
            "generated_at": generated_at,
            "model_version": str(model_versions.get("current_snapshot_model_version") or prediction.get("prediction_version") or ""),
            "model_snapshot_id": model_snapshot_id,
            "source_prediction_version": source_prediction_version,
            "source_payload_hash": canonical_hash(source_payload),
            "block_reason": blocker_text,
            "source_references": {},
            "metadata": {
                "competition_id": prediction.get("competition_id"),
                "competition_name": prediction.get("competition_name"),
                "team_resolution": prediction.get("team_resolution"),
                "model_history": prediction.get("model_history"),
                "warnings": prediction.get("warnings"),
            },
            "payload": source_payload,
        }
        payload["prediction_key"] = _prediction_key(payload)
        payload["prediction_id"] = f"edge_pred_{payload['prediction_key']}"
        blocked_rows.append(payload)
    return blocked_rows


def _prediction_key(payload: dict[str, Any]) -> str:
    return canonical_hash(
        {
            "fixture_id": payload["fixture_id"],
            "agent_id": payload["agent_id"],
            "market_family": payload["market_family"],
            "line": payload["line"],
            "selection": payload["selection"],
            "prediction_status": payload["prediction_status"],
            "as_of": payload["as_of"],
            "model_version": payload["model_version"],
            "source_payload_hash": payload["source_payload_hash"],
        }
    )


def _group_predictions_by_market(prediction_rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    grouped: dict[tuple[str, float | None, str | None], list[dict[str, Any]]] = {}
    for row in prediction_rows:
        key = (
            str(row["market_family"]),
            None if row.get("line") is None else float(row["line"]),
            None if row.get("selection") is None else str(row["selection"]),
        )
        grouped.setdefault(key, []).append(row)
    return [grouped[key] for key in sorted(grouped)]


def _build_consensus_row(conn: sqlite3.Connection, grouped_rows: list[dict[str, Any]]) -> dict[str, Any]:
    exemplar = grouped_rows[0]
    config = _active_consensus_config(conn, market_family=str(exemplar["market_family"]))
    prediction_ids = [str(row["prediction_id"]) for row in grouped_rows]
    participating_agents = sorted({str(row["agent_id"]) for row in grouped_rows})
    supported_agents = {
        agent_id
        for agent_id in participating_agents
        if _latest_capability_status(conn, agent_id=agent_id, market_family=str(exemplar["market_family"])) == CAPABILITY_SUPPORTED
    }
    supported_predictions = [
        row
        for row in grouped_rows
        if row["agent_id"] in supported_agents and row["prediction_status"] == "PREDICTED"
    ]
    if grouped_rows and not supported_agents:
        status = "NOT_EVALUABLE"
        reason = "AGENT_CAPABILITY_NOT_SUPPORTED"
        selection = None
        consensus_probability = None
        probability_spread = None
    elif not supported_predictions:
        statuses = {str(row["prediction_status"]) for row in grouped_rows}
        if "BLOCKED" in statuses:
            reason = "AGENT_BLOCKED"
        elif "ABSTAINED" in statuses:
            reason = "AGENT_ABSTAINED"
        else:
            reason = "NO_SUPPORTED_AGENT_PREDICTIONS"
        status = "NOT_EVALUABLE"
        selection = None
        consensus_probability = None
        probability_spread = None
    elif len(supported_predictions) < int(config["minimum_quorum"]):
        status = "INSUFFICIENT_AGENTS"
        reason = (
            f"CONSENSUS_INSUFFICIENT_AGENTS: required quorum {int(config['minimum_quorum'])}, "
            f"got {len(supported_predictions)}"
        )
        selection = None
        consensus_probability = None
        probability_spread = None
    else:
        selections = {str(row["selection"]) for row in supported_predictions}
        probabilities = [float(row["probability"]) for row in supported_predictions if row.get("probability") is not None]
        probability_spread = (max(probabilities) - min(probabilities)) if probabilities else None
        if int(config["same_direction_required"]) and len(selections) > 1:
            status = "FAILED"
            reason = "CONSENSUS_DIRECTIONAL_DISAGREEMENT"
            selection = None
            consensus_probability = None
        elif (
            config.get("max_probability_spread") is not None
            and probability_spread is not None
            and probability_spread > float(config["max_probability_spread"])
        ):
            status = "FAILED"
            reason = "CONSENSUS_PROBABILITY_SPREAD"
            selection = str(supported_predictions[0]["selection"])
            consensus_probability = sum(probabilities) / len(probabilities)
        else:
            status = "PASSED"
            reason = "CONSENSUS_PASSED"
            selection = str(supported_predictions[0]["selection"])
            consensus_probability = sum(probabilities) / len(probabilities)
    consensus_payload = {
        "fixture_id": str(exemplar["fixture_id"]),
        "kickoff_utc": exemplar.get("kickoff_utc"),
        "market_family": str(exemplar["market_family"]),
        "line": exemplar.get("line"),
        "config_id": str(config["config_id"]),
        "config_version_tag": str(config["version_tag"]),
        "participating_agents": participating_agents,
        "prediction_ids": prediction_ids,
        "agreed_selection": selection,
        "consensus_probability": consensus_probability,
        "probability_spread": probability_spread,
        "status": status,
        "reason": reason,
        "evaluated_at": str(exemplar["generated_at"]),
    }
    consensus_payload["consensus_key"] = canonical_hash(
        {
            "fixture_id": consensus_payload["fixture_id"],
            "market_family": consensus_payload["market_family"],
            "line": consensus_payload["line"],
            "config_version_tag": consensus_payload["config_version_tag"],
            "prediction_ids": consensus_payload["prediction_ids"],
            "status": consensus_payload["status"],
            "reason": consensus_payload["reason"],
        }
    )
    consensus_payload["consensus_id"] = f"edge_consensus_{consensus_payload['consensus_key']}"
    return consensus_payload


def _build_decision_row(grouped_rows: list[dict[str, Any]], consensus_row: dict[str, Any]) -> dict[str, Any]:
    context_selection = None
    selections = {row.get("selection") for row in grouped_rows}
    if len(selections) == 1:
        context_selection = next(iter(selections))
    if consensus_row["status"] == "PASSED":
        decision_status = "QUALIFIED"
        reason_code = "OTHER"
    elif consensus_row["status"] == "INSUFFICIENT_AGENTS":
        decision_status = "NO_BET"
        reason_code = "CONSENSUS_INSUFFICIENT_AGENTS"
    elif consensus_row["reason"] == "CONSENSUS_DIRECTIONAL_DISAGREEMENT":
        decision_status = "NO_BET"
        reason_code = "CONSENSUS_DIRECTIONAL_DISAGREEMENT"
    elif consensus_row["reason"] == "CONSENSUS_PROBABILITY_SPREAD":
        decision_status = "NO_BET"
        reason_code = "CONSENSUS_PROBABILITY_SPREAD"
    elif consensus_row["reason"] == "AGENT_BLOCKED":
        decision_status = "NO_BET"
        reason_code = "AGENT_BLOCKED"
    elif consensus_row["reason"] == "AGENT_ABSTAINED":
        decision_status = "NO_BET"
        reason_code = "AGENT_ABSTAINED"
    else:
        decision_status = "NO_BET"
        reason_code = "OTHER"
    decision_payload = {
        "fixture_id": str(consensus_row["fixture_id"]),
        "kickoff_utc": consensus_row.get("kickoff_utc"),
        "market_family": str(consensus_row["market_family"]),
        "line": consensus_row.get("line"),
        "selection": consensus_row.get("agreed_selection") or context_selection,
        "consensus_id": str(consensus_row["consensus_id"]),
        "agent_prediction_ids": [str(row["prediction_id"]) for row in grouped_rows],
        "decision_status": decision_status,
        "reason_code": reason_code,
        "reason_detail": str(consensus_row["reason"]),
        "decided_at": str(consensus_row["evaluated_at"]),
    }
    decision_payload["decision_key"] = canonical_hash(
        {
            "consensus_id": decision_payload["consensus_id"],
            "decision_status": decision_payload["decision_status"],
            "reason_code": decision_payload["reason_code"],
        }
    )
    decision_payload["decision_event_id"] = f"edge_decision_{decision_payload['decision_key']}"
    return decision_payload


def _insert_prediction_row(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    if payload["prediction_status"] not in PREDICTION_STATUSES:
        raise ValueError(f"invalid prediction status {payload['prediction_status']!r}")
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO edge_desk_agent_predictions (
                prediction_id, prediction_key, fixture_id, kickoff_utc, agent_id, market_family, line, selection,
                probability, fair_price, prediction_status, as_of, generated_at, model_version, model_snapshot_id,
                source_prediction_version, source_payload_hash, block_reason, source_references_json, metadata_json,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["prediction_id"],
                payload["prediction_key"],
                payload["fixture_id"],
                payload.get("kickoff_utc"),
                payload["agent_id"],
                payload["market_family"],
                payload.get("line"),
                payload.get("selection"),
                payload.get("probability"),
                payload.get("fair_price"),
                payload["prediction_status"],
                payload.get("as_of"),
                payload["generated_at"],
                payload.get("model_version"),
                payload.get("model_snapshot_id"),
                payload.get("source_prediction_version"),
                payload["source_payload_hash"],
                payload.get("block_reason"),
                _json_dumps(payload.get("source_references") or {}),
                _json_dumps(payload.get("metadata") or {}),
                _json_dumps(payload.get("payload") or {}),
                payload["generated_at"],
            ),
        )
    row = conn.execute(
        "SELECT * FROM edge_desk_agent_predictions WHERE prediction_key = ?",
        (payload["prediction_key"],),
    ).fetchone()
    if row is None:
        raise RuntimeError("failed to persist edge desk agent prediction")
    return _deserialize_prediction_row(dict(row))


def _insert_consensus_row(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    if payload["status"] not in CONSENSUS_STATUSES:
        raise ValueError(f"invalid consensus status {payload['status']!r}")
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO edge_desk_consensus_evaluations (
                consensus_id, consensus_key, fixture_id, kickoff_utc, market_family, line, config_id,
                config_version_tag, participating_agents_json, prediction_ids_json, agreed_selection,
                consensus_probability, probability_spread, status, reason, evaluated_at, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["consensus_id"],
                payload["consensus_key"],
                payload["fixture_id"],
                payload.get("kickoff_utc"),
                payload["market_family"],
                payload.get("line"),
                payload["config_id"],
                payload["config_version_tag"],
                _json_dumps(payload["participating_agents"]),
                _json_dumps(payload["prediction_ids"]),
                payload.get("agreed_selection"),
                payload.get("consensus_probability"),
                payload.get("probability_spread"),
                payload["status"],
                payload["reason"],
                payload["evaluated_at"],
                _json_dumps(payload),
                payload["evaluated_at"],
            ),
        )
    row = conn.execute(
        "SELECT * FROM edge_desk_consensus_evaluations WHERE consensus_key = ?",
        (payload["consensus_key"],),
    ).fetchone()
    if row is None:
        raise RuntimeError("failed to persist edge desk consensus evaluation")
    return _deserialize_consensus_row(dict(row))


def _insert_decision_row(conn: sqlite3.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    if payload["decision_status"] not in DECISION_STATUSES:
        raise ValueError(f"invalid decision status {payload['decision_status']!r}")
    if payload["reason_code"] not in DECISION_REASON_CODES:
        raise ValueError(f"invalid decision reason {payload['reason_code']!r}")
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO edge_desk_decision_history (
                decision_event_id, decision_key, fixture_id, kickoff_utc, market_family, line, selection,
                consensus_id, agent_prediction_ids_json, decision_status, reason_code, reason_detail,
                decided_at, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["decision_event_id"],
                payload["decision_key"],
                payload["fixture_id"],
                payload.get("kickoff_utc"),
                payload["market_family"],
                payload.get("line"),
                payload.get("selection"),
                payload.get("consensus_id"),
                _json_dumps(payload["agent_prediction_ids"]),
                payload["decision_status"],
                payload["reason_code"],
                payload.get("reason_detail"),
                payload["decided_at"],
                _json_dumps(payload),
                payload["decided_at"],
            ),
        )
    row = conn.execute(
        "SELECT * FROM edge_desk_decision_history WHERE decision_key = ?",
        (payload["decision_key"],),
    ).fetchone()
    if row is None:
        raise RuntimeError("failed to persist edge desk decision history")
    return _deserialize_decision_row(dict(row))


def _active_consensus_config(conn: sqlite3.Connection, *, market_family: str) -> dict[str, Any]:
    ensure_schema(conn)
    row = conn.execute(
        """
        SELECT *
        FROM edge_desk_consensus_configs
        WHERE config_id = ?
          AND (market_family = ? OR market_family IS NULL)
        ORDER BY CASE WHEN market_family = ? THEN 0 ELSE 1 END, valid_from DESC, config_row_id DESC
        LIMIT 1
        """,
        (DEFAULT_CONSENSUS_CONFIG_ID, market_family, market_family),
    ).fetchone()
    if row is None:
        raise RuntimeError("edge desk consensus config missing")
    payload = dict(row)
    payload["required_agents"] = json.loads(str(payload.pop("required_agents_json")))
    payload["metadata"] = json.loads(str(payload.pop("metadata_json")))
    return payload


def _latest_capability_rows_for_agent(agent_id: str) -> dict[str, dict[str, Any]]:
    return {
        market_family: {
            "agent_id": agent_id,
            "market_family": market_family,
            "capability_status": CAPABILITY_SUPPORTED,
        }
        for market_family in SUPPORTED_ROUTE_B_MARKET_FAMILIES
    }


def _latest_capability_status(conn: sqlite3.Connection, *, agent_id: str, market_family: str) -> str:
    row = conn.execute(
        """
        SELECT capability_status
        FROM edge_desk_agent_market_capabilities
        WHERE agent_id = ?
          AND market_family = ?
        ORDER BY valid_from DESC, capability_row_id DESC
        LIMIT 1
        """,
        (agent_id, market_family),
    ).fetchone()
    return CAPABILITY_INACTIVE if row is None else str(row["capability_status"])


def _deserialize_prediction_row(row: dict[str, Any]) -> dict[str, Any]:
    row["source_references"] = json.loads(str(row.pop("source_references_json")))
    row["metadata"] = json.loads(str(row.pop("metadata_json")))
    row["payload"] = json.loads(str(row.pop("payload_json")))
    return row


def _deserialize_consensus_row(row: dict[str, Any]) -> dict[str, Any]:
    row["participating_agents"] = json.loads(str(row.pop("participating_agents_json")))
    row["prediction_ids"] = json.loads(str(row.pop("prediction_ids_json")))
    row["payload"] = json.loads(str(row.pop("payload_json")))
    return row


def _deserialize_decision_row(row: dict[str, Any]) -> dict[str, Any]:
    row["agent_prediction_ids"] = json.loads(str(row.pop("agent_prediction_ids_json")))
    row["payload"] = json.loads(str(row.pop("payload_json")))
    return row
