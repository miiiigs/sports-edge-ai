from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sports_ai.agents.football import route_a_classifier
from sports_ai.storage import football_edge_desk


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _fixture(fixture_id: str = "fake:arsenal_chelsea") -> dict[str, object]:
    return {
        "fixture_id": fixture_id,
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "home_team_name": "Arsenal",
        "away_team_name": "Chelsea",
        "kickoff_utc": "2026-08-28T20:00:00+00:00",
    }


def _market_row(
    *,
    market_family: str,
    selection: str,
    probability: float,
    fair_price: float,
    line: float | None = None,
    as_of: str = "2026-08-26T10:00:00+00:00",
    pricing_model_version: str = "football_day10_market_pricing_v1",
) -> dict[str, object]:
    return {
        "fixture_id": "fake:arsenal_chelsea",
        "event_id": "fake:arsenal_chelsea",
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "season_code": "2026",
        "division_code": "E0",
        "match_date": "2026-08-28",
        "kickoff_time": "20:00",
        "kickoff_at": "2026-08-28T20:00:00+00:00",
        "as_of": as_of,
        "market_family": market_family,
        "line": line,
        "selection": selection,
        "source_scoreline_object_id": "scoreline_fake:arsenal_chelsea",
        "source_corner_object_id": None if market_family != "corner_total" else "corner_fake:arsenal_chelsea",
        "pricing_model_version": pricing_model_version,
        "effective_win_stake_probability": probability,
        "fair_decimal_price": fair_price,
        "settlement_probabilities": {
            "full_win_probability": probability,
            "half_win_probability": 0.0,
            "push_probability": 0.0,
            "half_loss_probability": 0.0,
            "full_loss_probability": 1.0 - probability,
        },
        "component_lines": [] if line is None else [line],
    }


def _ready_prediction(
    *,
    market_rows: list[dict[str, object]] | None = None,
    generated_at: str = "2026-08-26T10:05:00+00:00",
    model_as_of: str = "2026-08-26T10:00:00+00:00",
    model_snapshot_id: str = "snapshot_v1",
    prediction_version: str = "pred_version_v1",
) -> dict[str, object]:
    resolved_rows = market_rows or [
        _market_row(market_family="1x2", selection="HOME", probability=0.52, fair_price=1.92),
        _market_row(market_family="1x2", selection="DRAW", probability=0.24, fair_price=4.17),
        _market_row(market_family="1x2", selection="AWAY", probability=0.24, fair_price=4.17),
        _market_row(market_family="btts", selection="YES", probability=0.58, fair_price=1.72),
        _market_row(market_family="btts", selection="NO", probability=0.42, fair_price=2.38),
        _market_row(market_family="goals_total", selection="OVER", line=2.5, probability=0.55, fair_price=1.82),
        _market_row(market_family="goals_total", selection="UNDER", line=2.5, probability=0.45, fair_price=2.22),
        _market_row(market_family="goals_total", selection="OVER", line=3.5, probability=0.31, fair_price=3.23),
        _market_row(market_family="goals_total", selection="UNDER", line=3.5, probability=0.69, fair_price=1.45),
    ]
    return {
        "prediction_id": f"prediction_{prediction_version}",
        "fixture_id": "fake:arsenal_chelsea",
        "generated_at": generated_at,
        "model_as_of": model_as_of,
        "model_snapshot_id": model_snapshot_id,
        "prediction_version": prediction_version,
        "version_key": prediction_version,
        "prediction_status": "READY",
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "home_team_name": "Arsenal",
        "away_team_name": "Chelsea",
        "kickoff_utc": "2026-08-28T20:00:00+00:00",
        "model_versions": {
            "current_snapshot_model_version": model_snapshot_id,
        },
        "team_resolution": {
            "home": {"canonical_team_name": "Arsenal"},
            "away": {"canonical_team_name": "Chelsea"},
        },
        "market_rows": resolved_rows,
        "blockers": [],
        "warnings": [],
    }


def _blocked_prediction() -> dict[str, object]:
    return {
        "prediction_id": "prediction_blocked",
        "fixture_id": "fake:arsenal_chelsea",
        "generated_at": "2026-08-26T10:05:00+00:00",
        "model_as_of": None,
        "model_snapshot_id": None,
        "prediction_version": "blocked_v1",
        "version_key": "blocked_v1",
        "prediction_status": "BLOCKED",
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "home_team_name": "Arsenal",
        "away_team_name": "Chelsea",
        "kickoff_utc": "2026-08-28T20:00:00+00:00",
        "blockers": ["HOME_MODEL_HISTORY_MISSING"],
        "warnings": ["fixture missing sufficient home history"],
        "team_resolution": {
            "home": {"canonical_team_name": "Arsenal"},
            "away": {"canonical_team_name": "Chelsea"},
        },
    }


def _route_a_activation_artifact(
    *,
    active: bool = True,
    review_status: str | None = None,
    btts_status: str | None = None,
    ou25_status: str | None = None,
    validation_status: str | None = None,
) -> dict[str, object]:
    path = _repo_root() / "research" / "football_research" / "football_route_a_direct_classifier_2026-08-27_artifact.json"
    artifact = json.loads(path.read_text(encoding="utf-8"))
    resolved_validation_status = (
        validation_status
        if validation_status is not None
        else (
            route_a_classifier.ROUTE_A_VALIDATION_STATUS_PASSED
            if active
            else route_a_classifier.ROUTE_A_VALIDATION_STATUS_FAILED
        )
    )
    resolved_review_status = (
        review_status
        if review_status is not None
        else (
            route_a_classifier.ROUTE_A_REVIEW_STATUS_APPROVED
            if active
            else route_a_classifier.ROUTE_A_REVIEW_STATUS_PENDING
        )
    )
    resolved_btts_status = (
        btts_status
        if btts_status is not None
        else (
            route_a_classifier.ROUTE_A_MARKET_STATUS_VALIDATED
            if active
            else route_a_classifier.ROUTE_A_MARKET_STATUS_REJECTED
        )
    )
    resolved_ou25_status = (
        ou25_status
        if ou25_status is not None
        else (
            route_a_classifier.ROUTE_A_MARKET_STATUS_VALIDATED
            if active
            else route_a_classifier.ROUTE_A_MARKET_STATUS_REJECTED
        )
    )
    manifest = {
        "schema_version": route_a_classifier.ROUTE_A_MANIFEST_SCHEMA_VERSION,
        "agent_code": route_a_classifier.ROUTE_A_AGENT_ID,
        "artifact_reference": str(path),
        "artifact_sha256": "0" * 64,
        "model_bundle_version": route_a_classifier.ROUTE_A_MODEL_BUNDLE_VERSION,
        "feature_version": route_a_classifier.ROUTE_A_FEATURE_VERSION,
        "training_cutoff": artifact["training_cutoff"],
        "validation_status": resolved_validation_status,
        "market_validation_status": {
            route_a_classifier.MARKET_KEY_BTTS: resolved_btts_status,
            route_a_classifier.MARKET_KEY_OU25: resolved_ou25_status,
        },
        "review_status": resolved_review_status,
        "supported_markets": list(route_a_classifier.SUPPORTED_MARKETS),
        "created_at": artifact["generated_at"],
        "stable_model_id": artifact["stable_model_id"],
        "overall_route_a_status": (
            "ACTIVE"
            if resolved_validation_status == route_a_classifier.ROUTE_A_VALIDATION_STATUS_PASSED
            and resolved_review_status == route_a_classifier.ROUTE_A_REVIEW_STATUS_APPROVED
            and resolved_btts_status == route_a_classifier.ROUTE_A_MARKET_STATUS_VALIDATED
            and resolved_ou25_status == route_a_classifier.ROUTE_A_MARKET_STATUS_VALIDATED
            else (
                route_a_classifier.ROUTE_A_REVIEW_STATUS_PENDING
                if resolved_review_status != route_a_classifier.ROUTE_A_REVIEW_STATUS_APPROVED
                else route_a_classifier.ROUTE_A_RUNTIME_STATUS_REJECTED
            )
        ),
    }
    artifact["validation"]["validation_status"] = manifest["validation_status"]
    artifact["validation"]["review_status"] = manifest["review_status"]
    artifact["validation"]["market_validation_status"] = dict(manifest["market_validation_status"])
    artifact["markets"][route_a_classifier.MARKET_KEY_BTTS]["market_validation_status"] = manifest["market_validation_status"][route_a_classifier.MARKET_KEY_BTTS]
    artifact["markets"][route_a_classifier.MARKET_KEY_OU25]["market_validation_status"] = manifest["market_validation_status"][route_a_classifier.MARKET_KEY_OU25]
    artifact["overall_route_a_status"] = manifest["overall_route_a_status"]
    artifact["runtime_manifest"] = manifest
    return artifact


def _shadow_bundle_prediction(
    *,
    route_b_market_rows: list[dict[str, object]],
    route_a_market_rows: list[dict[str, object]] | None = None,
    route_a_status: str = "PREDICTED",
    route_a_blockers: list[str] | None = None,
    prediction_version: str = "shadow_bundle_v1",
) -> dict[str, object]:
    return {
        "prediction_id": f"prediction_{prediction_version}",
        "fixture_id": "fake:arsenal_chelsea",
        "generated_at": "2026-08-27T13:00:00+00:00",
        "model_as_of": "2026-08-27T12:59:00+00:00",
        "model_snapshot_id": "snapshot_v1",
        "prediction_version": prediction_version,
        "version_key": prediction_version,
        "prediction_status": "READY",
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "home_team_name": "Arsenal",
        "away_team_name": "Chelsea",
        "kickoff_utc": "2026-08-28T20:00:00+00:00",
        "edge_desk_shadow_bundle": {
            "agents": [
                {
                    "agent_id": football_edge_desk.ROUTE_B_COMPOSITE_AGENT_ID,
                    "agent_version_tag": football_edge_desk.FOUNDATION_VERSION,
                    "prediction_status": "PREDICTED",
                    "generated_at": "2026-08-27T13:00:00+00:00",
                    "model_snapshot_id": "snapshot_v1",
                    "source_prediction_version": prediction_version,
                    "market_rows": route_b_market_rows,
                },
                {
                    "agent_id": route_a_classifier.ROUTE_A_AGENT_ID,
                    "agent_version_tag": route_a_classifier.ROUTE_A_AGENT_VERSION_TAG,
                    "prediction_status": route_a_status,
                    "generated_at": "2026-08-27T13:00:00+00:00",
                    "model_snapshot_id": "snapshot_v1",
                    "source_prediction_version": prediction_version,
                    "model_version": route_a_classifier.ROUTE_A_AGENT_VERSION_TAG,
                    "model_as_of": "2026-08-27T12:59:00+00:00",
                    "market_rows": [] if route_a_market_rows is None else route_a_market_rows,
                    "market_contexts": [
                        {"market_family": "btts", "line": None},
                        {"market_family": "goals_total", "line": 2.5},
                    ],
                    "blockers": list(route_a_blockers or []),
                },
            ]
        },
    }


def _raw_row(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> dict[str, object]:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return dict(row)


def _seed_rows(conn: sqlite3.Connection) -> dict[str, dict[str, object]]:
    football_edge_desk.record_shadow_prediction_bundle(conn, fixture=_fixture(), prediction=_ready_prediction())
    return {
        "registry": _raw_row(
            conn,
            "SELECT * FROM edge_desk_agent_registry WHERE agent_id = ? AND version_tag = ?",
            (football_edge_desk.ROUTE_B_COMPOSITE_AGENT_ID, football_edge_desk.FOUNDATION_VERSION),
        ),
        "capability": _raw_row(
            conn,
            "SELECT * FROM edge_desk_agent_market_capabilities WHERE agent_id = ? AND market_family = ?",
            (football_edge_desk.ROUTE_B_COMPOSITE_AGENT_ID, "btts"),
        ),
        "config": _raw_row(
            conn,
            "SELECT * FROM edge_desk_consensus_configs WHERE config_id = ? AND config_scope_key = ?",
            (football_edge_desk.DEFAULT_CONSENSUS_CONFIG_ID, football_edge_desk.GLOBAL_CONFIG_SCOPE_KEY),
        ),
        "prediction": _raw_row(
            conn,
            "SELECT * FROM edge_desk_agent_predictions WHERE market_family = ? AND selection = ?",
            ("1x2", "HOME"),
        ),
        "consensus": _raw_row(
            conn,
            "SELECT * FROM edge_desk_consensus_evaluations WHERE market_family = ?",
            ("1x2",),
        ),
        "decision": _raw_row(
            conn,
            "SELECT * FROM edge_desk_decision_history WHERE market_family = ?",
            ("1x2",),
        ),
    }


def _assert_mutation_rejected_and_row_unchanged(
    conn: sqlite3.Connection,
    *,
    select_sql: str,
    select_params: tuple[object, ...],
    mutation_sql: str,
    mutation_params: tuple[object, ...],
) -> None:
    before = _raw_row(conn, select_sql, select_params)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(mutation_sql, mutation_params)
    after = _raw_row(conn, select_sql, select_params)
    assert after == before


def _replace_statement(
    table_name: str,
    row: dict[str, object],
    *,
    verb: str,
    overrides: dict[str, object],
) -> tuple[str, tuple[object, ...]]:
    payload = dict(row)
    payload.update(overrides)
    columns = list(payload.keys())
    placeholders = ", ".join("?" for _ in columns)
    sql = f"{verb} INTO {table_name} ({', '.join(columns)}) VALUES ({placeholders})"
    return sql, tuple(payload[column] for column in columns)


def _upsert_statement(
    table_name: str,
    row: dict[str, object],
    *,
    conflict_target: str,
    update_field: str,
    update_value: object,
) -> tuple[str, tuple[object, ...]]:
    payload = dict(row)
    payload[update_field] = update_value
    columns = list(payload.keys())
    placeholders = ", ".join("?" for _ in columns)
    sql = (
        f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict_target}) DO UPDATE SET {update_field} = excluded.{update_field}"
    )
    return sql, tuple(payload[column] for column in columns)


def _legacy_phase1a_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE edge_desk_agent_registry (
            registry_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            version_tag TEXT NOT NULL,
            name TEXT NOT NULL,
            method_family TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            UNIQUE(agent_id, version_tag)
        );
        CREATE TABLE edge_desk_agent_market_capabilities (
            capability_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            market_family TEXT NOT NULL,
            version_tag TEXT NOT NULL,
            capability_status TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            created_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            UNIQUE(agent_id, market_family, version_tag)
        );
        CREATE TABLE edge_desk_consensus_configs (
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
        );
        CREATE TABLE edge_desk_agent_predictions (
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
        );
        CREATE TABLE edge_desk_consensus_evaluations (
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
        );
        CREATE TABLE edge_desk_decision_history (
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
        );
        """
    )


def test_edge_desk_registry_contains_only_route_b_active_agent(tmp_path):
    with football_edge_desk.connect(tmp_path / "edge_desk.sqlite") as conn:
        football_edge_desk.ensure_schema(conn)
        agents = football_edge_desk.list_active_agents(conn)

    assert [agent["agent_id"] for agent in agents] == [football_edge_desk.ROUTE_B_COMPOSITE_AGENT_ID]
    assert agents[0]["method_family"] == "GAP_TO_XG_TO_DIXON_COLES_TO_PRICING"


def test_route_a_foundation_rejected_artifact_does_not_become_active(tmp_path) -> None:
    with football_edge_desk.connect(tmp_path / "edge_desk.sqlite") as conn:
        football_edge_desk.ensure_schema(conn)
        football_edge_desk.ensure_route_a_foundation(conn, artifact=_route_a_activation_artifact(active=False))
        active_agents = football_edge_desk.list_active_agents(conn)
        route_a_capabilities = football_edge_desk.list_market_capabilities(conn, agent_id=route_a_classifier.ROUTE_A_AGENT_ID)
        route_a_configs = [
            row
            for row in conn.execute("SELECT * FROM edge_desk_consensus_configs WHERE config_id = ?", (route_a_classifier.ROUTE_A_CONSENSUS_CONFIG_ID,))
        ]

    assert [agent["agent_id"] for agent in active_agents] == [
        football_edge_desk.ROUTE_B_COMPOSITE_AGENT_ID,
    ]
    assert {(row["market_family"], row["capability_status"]) for row in route_a_capabilities} == {
        ("1x2", football_edge_desk.CAPABILITY_NOT_SUPPORTED),
        ("asian_handicap", football_edge_desk.CAPABILITY_NOT_SUPPORTED),
        ("btts", football_edge_desk.CAPABILITY_REJECTED),
        ("corner_total", football_edge_desk.CAPABILITY_NOT_SUPPORTED),
        ("goals_total", football_edge_desk.CAPABILITY_REJECTED),
    }
    assert {row["market_family"] for row in route_a_configs} == {"btts", "goals_total"}


def test_route_a_foundation_approved_artifact_activates_in_isolation(tmp_path) -> None:
    with football_edge_desk.connect(tmp_path / "edge_desk.sqlite") as conn:
        football_edge_desk.ensure_schema(conn)
        football_edge_desk.ensure_route_a_foundation(conn, artifact=_route_a_activation_artifact(active=True))
        active_agents = football_edge_desk.list_active_agents(conn)
        route_a_capabilities = football_edge_desk.list_market_capabilities(conn, agent_id=route_a_classifier.ROUTE_A_AGENT_ID)

    assert [agent["agent_id"] for agent in active_agents] == [
        football_edge_desk.ROUTE_B_COMPOSITE_AGENT_ID,
        route_a_classifier.ROUTE_A_AGENT_ID,
    ]
    assert {(row["market_family"], row["capability_status"]) for row in route_a_capabilities} == {
        ("1x2", football_edge_desk.CAPABILITY_NOT_SUPPORTED),
        ("asian_handicap", football_edge_desk.CAPABILITY_NOT_SUPPORTED),
        ("btts", football_edge_desk.CAPABILITY_SUPPORTED),
        ("corner_total", football_edge_desk.CAPABILITY_NOT_SUPPORTED),
        ("goals_total", football_edge_desk.CAPABILITY_SUPPORTED),
    }


def test_route_b_output_shadows_into_agent_prediction_rows_and_is_idempotent(tmp_path):
    db_path = tmp_path / "edge_desk.sqlite"
    fixture = _fixture()
    prediction = _ready_prediction()

    with football_edge_desk.connect(db_path) as conn:
        football_edge_desk.record_shadow_prediction_bundle(conn, fixture=fixture, prediction=prediction)
        football_edge_desk.record_shadow_prediction_bundle(conn, fixture=fixture, prediction=prediction)
        prediction_rows = football_edge_desk.fetch_agent_predictions(conn)
        consensus_rows = football_edge_desk.fetch_consensus_evaluations(conn)
        decision_rows = football_edge_desk.fetch_decision_history(conn)

    assert len(prediction_rows) == len(prediction["market_rows"])
    assert {row["market_family"] for row in prediction_rows} >= {"1x2", "btts", "goals_total"}
    assert len(
        [
            row
            for row in prediction_rows
            if row["market_family"] == "goals_total" and row["selection"] == "OVER"
        ]
    ) == 2
    assert all(row["prediction_status"] == "PREDICTED" for row in prediction_rows)
    assert all(row["status"] == "INSUFFICIENT_AGENTS" for row in consensus_rows)
    assert all("required quorum 2, got 1" in row["reason"] for row in consensus_rows)
    assert all(row["decision_status"] == "NO_BET" for row in decision_rows)
    assert all(row["reason_code"] == "CONSENSUS_INSUFFICIENT_AGENTS" for row in decision_rows)


def test_changed_model_version_and_as_of_create_new_prediction_truth(tmp_path):
    db_path = tmp_path / "edge_desk.sqlite"
    fixture = _fixture()
    base_market = _market_row(market_family="1x2", selection="HOME", probability=0.52, fair_price=1.92)
    version_changed_market = _market_row(
        market_family="1x2",
        selection="HOME",
        probability=0.52,
        fair_price=1.92,
        pricing_model_version="football_day10_market_pricing_v2",
    )
    as_of_changed_market = _market_row(
        market_family="1x2",
        selection="HOME",
        probability=0.52,
        fair_price=1.92,
        as_of="2026-08-26T11:00:00+00:00",
    )

    with football_edge_desk.connect(db_path) as conn:
        football_edge_desk.record_shadow_prediction_bundle(
            conn,
            fixture=fixture,
            prediction=_ready_prediction(market_rows=[base_market], prediction_version="base"),
        )
        football_edge_desk.record_shadow_prediction_bundle(
            conn,
            fixture=fixture,
            prediction=_ready_prediction(market_rows=[version_changed_market], prediction_version="version-change"),
        )
        football_edge_desk.record_shadow_prediction_bundle(
            conn,
            fixture=fixture,
            prediction=_ready_prediction(market_rows=[as_of_changed_market], prediction_version="asof-change"),
        )
        prediction_rows = football_edge_desk.fetch_agent_predictions(conn)

    assert len(prediction_rows) == 3
    assert {row["model_version"] for row in prediction_rows} == {
        "football_day10_market_pricing_v1",
        "football_day10_market_pricing_v2",
    }
    assert {row["as_of"] for row in prediction_rows} == {
        "2026-08-26T10:00:00+00:00",
        "2026-08-26T11:00:00+00:00",
    }


def test_blocked_predictions_are_stored_as_durable_data(tmp_path):
    db_path = tmp_path / "edge_desk.sqlite"
    with football_edge_desk.connect(db_path) as conn:
        football_edge_desk.record_shadow_prediction_bundle(conn, fixture=_fixture(), prediction=_blocked_prediction())
        prediction_rows = football_edge_desk.fetch_agent_predictions(conn)
        consensus_rows = football_edge_desk.fetch_consensus_evaluations(conn)
        decision_rows = football_edge_desk.fetch_decision_history(conn)

    assert {row["market_family"] for row in prediction_rows} == set(football_edge_desk.SUPPORTED_ROUTE_B_MARKET_FAMILIES)
    assert all(row["prediction_status"] == "BLOCKED" for row in prediction_rows)
    assert all(row["status"] == "NOT_EVALUABLE" for row in consensus_rows)
    assert all(row["reason"] == "AGENT_BLOCKED" for row in consensus_rows)
    assert all(row["decision_status"] == "NO_BET" for row in decision_rows)
    assert all(row["reason_code"] == "AGENT_BLOCKED" for row in decision_rows)


def test_immutable_prediction_consensus_and_decision_tables_reject_update_and_delete(tmp_path):
    db_path = tmp_path / "edge_desk.sqlite"
    with football_edge_desk.connect(db_path) as conn:
        rows = _seed_rows(conn)
        cases = [
            {
                "table_name": "edge_desk_agent_registry",
                "select_sql": "SELECT * FROM edge_desk_agent_registry WHERE registry_row_id = ?",
                "select_params": (rows["registry"]["registry_row_id"],),
                "update_sql": "UPDATE edge_desk_agent_registry SET name = 'Tampered Agent' WHERE registry_row_id = ?",
                "update_params": (rows["registry"]["registry_row_id"],),
                "delete_sql": "DELETE FROM edge_desk_agent_registry WHERE registry_row_id = ?",
                "delete_params": (rows["registry"]["registry_row_id"],),
                "replace_overrides": {"name": "Tampered Agent"},
                "conflict_target": "agent_id, version_tag",
                "upsert_field": "name",
                "upsert_value": "Tampered Agent",
            },
            {
                "table_name": "edge_desk_agent_market_capabilities",
                "select_sql": "SELECT * FROM edge_desk_agent_market_capabilities WHERE capability_row_id = ?",
                "select_params": (rows["capability"]["capability_row_id"],),
                "update_sql": (
                    "UPDATE edge_desk_agent_market_capabilities SET capability_status = 'INACTIVE' "
                    "WHERE capability_row_id = ?"
                ),
                "update_params": (rows["capability"]["capability_row_id"],),
                "delete_sql": "DELETE FROM edge_desk_agent_market_capabilities WHERE capability_row_id = ?",
                "delete_params": (rows["capability"]["capability_row_id"],),
                "replace_overrides": {"capability_status": football_edge_desk.CAPABILITY_INACTIVE},
                "conflict_target": "agent_id, market_family, version_tag",
                "upsert_field": "capability_status",
                "upsert_value": football_edge_desk.CAPABILITY_INACTIVE,
            },
            {
                "table_name": "edge_desk_consensus_configs",
                "select_sql": "SELECT * FROM edge_desk_consensus_configs WHERE config_row_id = ?",
                "select_params": (rows["config"]["config_row_id"],),
                "update_sql": "UPDATE edge_desk_consensus_configs SET minimum_quorum = 3 WHERE config_row_id = ?",
                "update_params": (rows["config"]["config_row_id"],),
                "delete_sql": "DELETE FROM edge_desk_consensus_configs WHERE config_row_id = ?",
                "delete_params": (rows["config"]["config_row_id"],),
                "replace_overrides": {"minimum_quorum": 3},
                "conflict_target": "config_id, config_scope_key, version_tag",
                "upsert_field": "minimum_quorum",
                "upsert_value": 3,
            },
            {
                "table_name": "edge_desk_agent_predictions",
                "select_sql": "SELECT * FROM edge_desk_agent_predictions WHERE prediction_id = ?",
                "select_params": (rows["prediction"]["prediction_id"],),
                "update_sql": "UPDATE edge_desk_agent_predictions SET selection = 'DRAW' WHERE prediction_id = ?",
                "update_params": (rows["prediction"]["prediction_id"],),
                "delete_sql": "DELETE FROM edge_desk_agent_predictions WHERE prediction_id = ?",
                "delete_params": (rows["prediction"]["prediction_id"],),
                "replace_overrides": {"probability": 0.99, "fair_price": 1.01},
                "conflict_target": "prediction_id",
                "upsert_field": "fair_price",
                "upsert_value": 1.01,
            },
            {
                "table_name": "edge_desk_consensus_evaluations",
                "select_sql": "SELECT * FROM edge_desk_consensus_evaluations WHERE consensus_id = ?",
                "select_params": (rows["consensus"]["consensus_id"],),
                "update_sql": "UPDATE edge_desk_consensus_evaluations SET status = 'PASSED' WHERE consensus_id = ?",
                "update_params": (rows["consensus"]["consensus_id"],),
                "delete_sql": "DELETE FROM edge_desk_consensus_evaluations WHERE consensus_id = ?",
                "delete_params": (rows["consensus"]["consensus_id"],),
                "replace_overrides": {"status": "PASSED"},
                "conflict_target": "consensus_id",
                "upsert_field": "status",
                "upsert_value": "PASSED",
            },
            {
                "table_name": "edge_desk_decision_history",
                "select_sql": "SELECT * FROM edge_desk_decision_history WHERE decision_event_id = ?",
                "select_params": (rows["decision"]["decision_event_id"],),
                "update_sql": (
                    "UPDATE edge_desk_decision_history SET decision_status = 'QUALIFIED' "
                    "WHERE decision_event_id = ?"
                ),
                "update_params": (rows["decision"]["decision_event_id"],),
                "delete_sql": "DELETE FROM edge_desk_decision_history WHERE decision_event_id = ?",
                "delete_params": (rows["decision"]["decision_event_id"],),
                "replace_overrides": {"decision_status": "QUALIFIED"},
                "conflict_target": "decision_event_id",
                "upsert_field": "decision_status",
                "upsert_value": "QUALIFIED",
            },
        ]

        for case in cases:
            _assert_mutation_rejected_and_row_unchanged(
                conn,
                select_sql=case["select_sql"],
                select_params=case["select_params"],
                mutation_sql=case["update_sql"],
                mutation_params=case["update_params"],
            )
            _assert_mutation_rejected_and_row_unchanged(
                conn,
                select_sql=case["select_sql"],
                select_params=case["select_params"],
                mutation_sql=case["delete_sql"],
                mutation_params=case["delete_params"],
            )
            replace_sql, replace_params = _replace_statement(
                case["table_name"],
                _raw_row(conn, case["select_sql"], case["select_params"]),
                verb="INSERT OR REPLACE",
                overrides=case["replace_overrides"],
            )
            _assert_mutation_rejected_and_row_unchanged(
                conn,
                select_sql=case["select_sql"],
                select_params=case["select_params"],
                mutation_sql=replace_sql,
                mutation_params=replace_params,
            )
            replace_into_sql, replace_into_params = _replace_statement(
                case["table_name"],
                _raw_row(conn, case["select_sql"], case["select_params"]),
                verb="REPLACE",
                overrides=case["replace_overrides"],
            )
            _assert_mutation_rejected_and_row_unchanged(
                conn,
                select_sql=case["select_sql"],
                select_params=case["select_params"],
                mutation_sql=replace_into_sql,
                mutation_params=replace_into_params,
            )
            upsert_sql, upsert_params = _upsert_statement(
                case["table_name"],
                _raw_row(conn, case["select_sql"], case["select_params"]),
                conflict_target=case["conflict_target"],
                update_field=case["upsert_field"],
                update_value=case["upsert_value"],
            )
            _assert_mutation_rejected_and_row_unchanged(
                conn,
                select_sql=case["select_sql"],
                select_params=case["select_params"],
                mutation_sql=upsert_sql,
                mutation_params=upsert_params,
            )


def test_prediction_replace_attempt_keeps_original_probability_and_fair_price(tmp_path):
    db_path = tmp_path / "edge_desk.sqlite"
    with football_edge_desk.connect(db_path) as conn:
        rows = _seed_rows(conn)
        original = _raw_row(
            conn,
            "SELECT * FROM edge_desk_agent_predictions WHERE prediction_id = ?",
            (rows["prediction"]["prediction_id"],),
        )
        replace_sql, replace_params = _replace_statement(
            "edge_desk_agent_predictions",
            original,
            verb="INSERT OR REPLACE",
            overrides={"probability": 0.99, "fair_price": 1.01},
        )
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(replace_sql, replace_params)

        unchanged = _raw_row(
            conn,
            "SELECT probability, fair_price FROM edge_desk_agent_predictions WHERE prediction_id = ?",
            (rows["prediction"]["prediction_id"],),
        )

    assert unchanged["probability"] == pytest.approx(0.52)
    assert unchanged["fair_price"] == pytest.approx(1.92)


def test_not_supported_capability_is_not_counted_as_a_positive_vote(tmp_path):
    db_path = tmp_path / "edge_desk.sqlite"
    with football_edge_desk.connect(db_path) as conn:
        football_edge_desk.ensure_schema(conn)
        with conn:
            conn.execute(
                """
                INSERT INTO edge_desk_agent_market_capabilities (
                    agent_id, agent_version_tag, market_family, version_tag, capability_status, valid_from, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    football_edge_desk.ROUTE_B_COMPOSITE_AGENT_ID,
                    football_edge_desk.FOUNDATION_VERSION,
                    "btts",
                    "route_b_btts_disabled_for_test",
                    football_edge_desk.CAPABILITY_NOT_SUPPORTED,
                    "2026-08-26T12:00:00+00:00",
                    "2026-08-26T12:00:00+00:00",
                    "{}",
                ),
            )
        football_edge_desk.record_shadow_prediction_bundle(
            conn,
            fixture=_fixture(),
            prediction=_ready_prediction(
                market_rows=[_market_row(market_family="btts", selection="YES", probability=0.58, fair_price=1.72)],
                prediction_version="btts-only",
            ),
        )
        consensus_rows = football_edge_desk.fetch_consensus_evaluations(conn)

    assert len(consensus_rows) == 1
    assert consensus_rows[0]["status"] == "NOT_EVALUABLE"
    assert consensus_rows[0]["reason"] == "AGENT_CAPABILITY_NOT_SUPPORTED"


def test_passed_consensus_is_recorded_as_pending_value_and_risk_not_qualified(tmp_path):
    db_path = tmp_path / "edge_desk.sqlite"
    fixture = _fixture()
    route_b_market = _market_row(market_family="btts", selection="YES", probability=0.58, fair_price=1.72)
    agent_two_market = _market_row(market_family="btts", selection="YES", probability=0.56, fair_price=1.79)

    with football_edge_desk.connect(db_path) as conn:
        football_edge_desk.ensure_schema(conn)
        with conn:
            conn.execute(
                """
                INSERT INTO edge_desk_agent_registry (
                    agent_id, version_tag, name, method_family, status, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "TEST_AGENT_TWO",
                    "test_agent_two_v1",
                    "Test Agent Two",
                    "TEST_METHOD",
                    football_edge_desk.ACTIVE_AGENT_STATUS,
                    "2026-08-26T11:00:00+00:00",
                    "{}",
                ),
            )
            conn.execute(
                """
                INSERT INTO edge_desk_agent_market_capabilities (
                    agent_id, agent_version_tag, market_family, version_tag, capability_status, valid_from, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "TEST_AGENT_TWO",
                    "test_agent_two_v1",
                    "btts",
                    "test_agent_two_capability_v1",
                    football_edge_desk.CAPABILITY_SUPPORTED,
                    "2026-08-26T11:00:00+00:00",
                    "2026-08-26T11:00:00+00:00",
                    "{}",
                ),
            )

        football_edge_desk.record_shadow_prediction_bundle(
            conn,
            fixture=fixture,
            prediction=_ready_prediction(market_rows=[route_b_market], prediction_version="route-b-btts"),
        )

        second_row = {
            "prediction_row_id": 999,
            "prediction_id": "edge_pred_test_agent_two_btts_yes",
            "prediction_key": "test_agent_two_btts_yes",
            "fixture_id": fixture["fixture_id"],
            "kickoff_utc": fixture["kickoff_utc"],
            "agent_id": "TEST_AGENT_TWO",
            "agent_version_tag": "test_agent_two_v1",
            "market_family": "btts",
            "line": None,
            "selection": "YES",
            "probability": float(agent_two_market["effective_win_stake_probability"]),
            "fair_price": float(agent_two_market["fair_decimal_price"]),
            "prediction_status": "PREDICTED",
            "as_of": str(agent_two_market["as_of"]),
            "generated_at": "2026-08-26T11:05:00+00:00",
            "model_version": "test_agent_two_model_v1",
            "model_snapshot_id": "test_agent_two_snapshot_v1",
            "source_prediction_version": "test_agent_two_pred_v1",
            "source_payload_hash": "test_agent_two_payload_hash",
            "block_reason": None,
            "source_references_json": "{}",
            "metadata_json": "{}",
            "payload_json": "{}",
            "created_at": "2026-08-26T11:05:00+00:00",
        }
        second_sql, second_params = _replace_statement(
            "edge_desk_agent_predictions",
            second_row,
            verb="INSERT",
            overrides={},
        )
        conn.execute(second_sql, second_params)

        grouped_rows = [
            football_edge_desk.fetch_agent_predictions(conn, fixture_id=fixture["fixture_id"])[0],
            football_edge_desk._deserialize_prediction_row(second_row.copy()),
        ]
        consensus_row = football_edge_desk._build_consensus_row(conn, grouped_rows)
        stored_consensus = football_edge_desk._insert_consensus_row(conn, consensus_row)
        decision_row = football_edge_desk._build_decision_row(grouped_rows, stored_consensus)
        stored_decision = football_edge_desk._insert_decision_row(conn, decision_row)

    assert stored_consensus["status"] == "PASSED"
    assert stored_decision["decision_status"] == "CONSENSUS_PASSED"
    assert stored_decision["reason_code"] == "VALUE_RISK_PENDING"


def test_route_a_and_route_b_same_direction_pass_consensus_once_per_market(tmp_path) -> None:
    db_path = tmp_path / "edge_desk.sqlite"
    fixture = _fixture()
    prediction = _shadow_bundle_prediction(
        route_b_market_rows=[
            _market_row(market_family="btts", selection="YES", probability=0.58, fair_price=1.72),
            _market_row(market_family="btts", selection="NO", probability=0.42, fair_price=2.38),
        ],
        route_a_market_rows=[
            _market_row(
                market_family="btts",
                selection="YES",
                probability=0.56,
                fair_price=1.79,
                pricing_model_version="ROUTE_A_LOGISTIC_V1_BTTS",
            ),
            _market_row(
                market_family="btts",
                selection="NO",
                probability=0.44,
                fair_price=2.27,
                pricing_model_version="ROUTE_A_LOGISTIC_V1_BTTS",
            ),
        ],
    )

    with football_edge_desk.connect(db_path) as conn:
        football_edge_desk.ensure_schema(conn)
        football_edge_desk.ensure_route_a_foundation(conn, artifact=_route_a_activation_artifact(active=True))
        football_edge_desk.record_shadow_prediction_bundle(conn, fixture=fixture, prediction=prediction)
        football_edge_desk.record_shadow_prediction_bundle(conn, fixture=fixture, prediction=prediction)
        consensus_rows = football_edge_desk.fetch_consensus_evaluations(conn, fixture_id=fixture["fixture_id"])
        decision_rows = football_edge_desk.fetch_decision_history(conn, fixture_id=fixture["fixture_id"])

    assert len(consensus_rows) == 1
    assert consensus_rows[0]["status"] == "PASSED"
    assert consensus_rows[0]["reason"] == "CONSENSUS_PASSED"
    assert consensus_rows[0]["agreed_selection"] == "YES"
    assert consensus_rows[0]["probability_spread"] == pytest.approx(0.02)
    assert len(decision_rows) == 1
    assert decision_rows[0]["decision_status"] == "CONSENSUS_PASSED"
    assert decision_rows[0]["reason_code"] == "VALUE_RISK_PENDING"


def test_route_a_and_route_b_direction_disagreement_fails_consensus(tmp_path) -> None:
    db_path = tmp_path / "edge_desk.sqlite"
    fixture = _fixture()
    prediction = _shadow_bundle_prediction(
        route_b_market_rows=[
            _market_row(market_family="btts", selection="YES", probability=0.58, fair_price=1.72),
            _market_row(market_family="btts", selection="NO", probability=0.42, fair_price=2.38),
        ],
        route_a_market_rows=[
            _market_row(
                market_family="btts",
                selection="YES",
                probability=0.43,
                fair_price=2.33,
                pricing_model_version="ROUTE_A_LOGISTIC_V1_BTTS",
            ),
            _market_row(
                market_family="btts",
                selection="NO",
                probability=0.57,
                fair_price=1.75,
                pricing_model_version="ROUTE_A_LOGISTIC_V1_BTTS",
            ),
        ],
        prediction_version="shadow_bundle_direction_fail",
    )

    with football_edge_desk.connect(db_path) as conn:
        football_edge_desk.ensure_schema(conn)
        football_edge_desk.ensure_route_a_foundation(conn, artifact=_route_a_activation_artifact(active=True))
        football_edge_desk.record_shadow_prediction_bundle(conn, fixture=fixture, prediction=prediction)
        consensus_rows = football_edge_desk.fetch_consensus_evaluations(conn, fixture_id=fixture["fixture_id"])
        decision_rows = football_edge_desk.fetch_decision_history(conn, fixture_id=fixture["fixture_id"])

    assert consensus_rows[0]["status"] == "FAILED"
    assert consensus_rows[0]["reason"] == "CONSENSUS_DIRECTIONAL_DISAGREEMENT"
    assert decision_rows[0]["decision_status"] == "NO_BET"
    assert decision_rows[0]["reason_code"] == "CONSENSUS_DIRECTIONAL_DISAGREEMENT"


def test_route_a_and_route_b_spread_above_threshold_fails_consensus(tmp_path) -> None:
    db_path = tmp_path / "edge_desk.sqlite"
    fixture = _fixture()
    prediction = _shadow_bundle_prediction(
        route_b_market_rows=[
            _market_row(market_family="btts", selection="YES", probability=0.58, fair_price=1.72),
            _market_row(market_family="btts", selection="NO", probability=0.42, fair_price=2.38),
        ],
        route_a_market_rows=[
            _market_row(
                market_family="btts",
                selection="YES",
                probability=0.50,
                fair_price=2.0,
                pricing_model_version="ROUTE_A_LOGISTIC_V1_BTTS",
            ),
            _market_row(
                market_family="btts",
                selection="NO",
                probability=0.50,
                fair_price=2.0,
                pricing_model_version="ROUTE_A_LOGISTIC_V1_BTTS",
            ),
        ],
        prediction_version="shadow_bundle_spread_fail",
    )

    with football_edge_desk.connect(db_path) as conn:
        football_edge_desk.ensure_schema(conn)
        football_edge_desk.ensure_route_a_foundation(conn, artifact=_route_a_activation_artifact(active=True))
        football_edge_desk.record_shadow_prediction_bundle(conn, fixture=fixture, prediction=prediction)
        consensus_rows = football_edge_desk.fetch_consensus_evaluations(conn, fixture_id=fixture["fixture_id"])
        decision_rows = football_edge_desk.fetch_decision_history(conn, fixture_id=fixture["fixture_id"])

    assert consensus_rows[0]["status"] == "FAILED"
    assert consensus_rows[0]["reason"] == "CONSENSUS_PROBABILITY_SPREAD"
    assert consensus_rows[0]["probability_spread"] == pytest.approx(0.08)
    assert decision_rows[0]["decision_status"] == "NO_BET"
    assert decision_rows[0]["reason_code"] == "CONSENSUS_PROBABILITY_SPREAD"


@pytest.mark.parametrize(
    ("route_a_probability", "expected_status"),
    [
        (0.520001, "PASSED"),
        (0.520000, "PASSED"),
        (0.519999, "FAILED"),
    ],
)
def test_route_a_consensus_spread_boundary_is_exact(tmp_path, route_a_probability: float, expected_status: str) -> None:
    db_path = tmp_path / "edge_desk.sqlite"
    fixture = _fixture()
    prediction = _shadow_bundle_prediction(
        route_b_market_rows=[
            _market_row(market_family="btts", selection="YES", probability=0.58, fair_price=1.72),
            _market_row(market_family="btts", selection="NO", probability=0.42, fair_price=2.38),
        ],
        route_a_market_rows=[
            _market_row(
                market_family="btts",
                selection="YES",
                probability=route_a_probability,
                fair_price=1.0 / route_a_probability,
                pricing_model_version="ROUTE_A_LOGISTIC_V2_BTTS",
            ),
            _market_row(
                market_family="btts",
                selection="NO",
                probability=1.0 - route_a_probability,
                fair_price=1.0 / (1.0 - route_a_probability),
                pricing_model_version="ROUTE_A_LOGISTIC_V2_BTTS",
            ),
        ],
        prediction_version=f"boundary_{route_a_probability:.6f}",
    )

    with football_edge_desk.connect(db_path) as conn:
        football_edge_desk.ensure_schema(conn)
        football_edge_desk.ensure_route_a_foundation(conn, artifact=_route_a_activation_artifact(active=True))
        football_edge_desk.record_shadow_prediction_bundle(conn, fixture=fixture, prediction=prediction)
        consensus_rows = football_edge_desk.fetch_consensus_evaluations(conn, fixture_id=fixture["fixture_id"])

    assert len(consensus_rows) == 1
    assert consensus_rows[0]["status"] == expected_status
    assert consensus_rows[0]["probability_spread"] == pytest.approx(abs(0.58 - route_a_probability))


def test_pending_review_route_a_foundation_does_not_enable_voting(tmp_path) -> None:
    db_path = tmp_path / "edge_desk.sqlite"
    fixture = _fixture()
    prediction = _shadow_bundle_prediction(
        route_b_market_rows=[
            _market_row(market_family="btts", selection="YES", probability=0.58, fair_price=1.72),
            _market_row(market_family="btts", selection="NO", probability=0.42, fair_price=2.38),
        ],
        route_a_market_rows=None,
        route_a_status="BLOCKED",
        route_a_blockers=["MODEL_REVIEW_PENDING"],
        prediction_version="route_a_pending_review",
    )

    with football_edge_desk.connect(db_path) as conn:
        football_edge_desk.ensure_schema(conn)
        football_edge_desk.ensure_route_a_foundation(
            conn,
            artifact=_route_a_activation_artifact(
                active=False,
                review_status=route_a_classifier.ROUTE_A_REVIEW_STATUS_PENDING,
                validation_status=route_a_classifier.ROUTE_A_VALIDATION_STATUS_PASSED,
                btts_status=route_a_classifier.ROUTE_A_MARKET_STATUS_VALIDATED,
                ou25_status=route_a_classifier.ROUTE_A_MARKET_STATUS_VALIDATED,
            ),
        )
        football_edge_desk.record_shadow_prediction_bundle(conn, fixture=fixture, prediction=prediction)
        consensus_rows = football_edge_desk.fetch_consensus_evaluations(conn, fixture_id=fixture["fixture_id"])
        route_a_capabilities = football_edge_desk.list_market_capabilities(conn, agent_id=route_a_classifier.ROUTE_A_AGENT_ID)

    assert any(row["capability_status"] == football_edge_desk.CAPABILITY_PENDING_REVIEW for row in route_a_capabilities if row["market_family"] == "btts")
    assert consensus_rows[0]["status"] == "INSUFFICIENT_AGENTS"


def test_fake_active_json_cannot_register_route_a_foundation(tmp_path) -> None:
    with football_edge_desk.connect(tmp_path / "edge_desk.sqlite") as conn:
        football_edge_desk.ensure_schema(conn)
        football_edge_desk.ensure_route_a_foundation(
            conn,
            artifact={"overall_route_a_status": "ACTIVE", "validation": {"overall_route_a_status": "ACTIVE"}},
        )
        active_agents = football_edge_desk.list_active_agents(conn)
        route_a_capabilities = football_edge_desk.list_market_capabilities(conn, agent_id=route_a_classifier.ROUTE_A_AGENT_ID)

    assert [agent["agent_id"] for agent in active_agents] == [football_edge_desk.ROUTE_B_COMPOSITE_AGENT_ID]
    assert route_a_capabilities == []


def test_route_a_blocked_row_is_persisted_without_false_consensus_pass(tmp_path) -> None:
    db_path = tmp_path / "edge_desk.sqlite"
    fixture = _fixture()
    prediction = _shadow_bundle_prediction(
        route_b_market_rows=[
            _market_row(market_family="btts", selection="YES", probability=0.58, fair_price=1.72),
            _market_row(market_family="btts", selection="NO", probability=0.42, fair_price=2.38),
        ],
        route_a_market_rows=None,
        route_a_status="BLOCKED",
        route_a_blockers=["MODEL_ARTIFACT_MISSING"],
        prediction_version="shadow_bundle_route_a_blocked",
    )

    with football_edge_desk.connect(db_path) as conn:
        football_edge_desk.ensure_schema(conn)
        football_edge_desk.ensure_route_a_foundation(conn, artifact=_route_a_activation_artifact(active=True))
        football_edge_desk.record_shadow_prediction_bundle(conn, fixture=fixture, prediction=prediction)
        consensus_rows = football_edge_desk.fetch_consensus_evaluations(conn, fixture_id=fixture["fixture_id"])
        decision_rows = football_edge_desk.fetch_decision_history(conn, fixture_id=fixture["fixture_id"])
        prediction_rows = football_edge_desk.fetch_agent_predictions(conn, fixture_id=fixture["fixture_id"])

    assert any(row["agent_id"] == route_a_classifier.ROUTE_A_AGENT_ID and row["prediction_status"] == "BLOCKED" for row in prediction_rows)
    assert consensus_rows[0]["status"] == "NOT_EVALUABLE"
    assert consensus_rows[0]["reason"] == "AGENT_BLOCKED"
    assert decision_rows[0]["decision_status"] == "NO_BET"
    assert decision_rows[0]["reason_code"] == "AGENT_BLOCKED"


def test_one_agent_markets_remain_insufficient_after_route_a_activation(tmp_path) -> None:
    db_path = tmp_path / "edge_desk.sqlite"
    fixture = _fixture()

    with football_edge_desk.connect(db_path) as conn:
        football_edge_desk.ensure_schema(conn)
        football_edge_desk.ensure_route_a_foundation(conn, artifact=_route_a_activation_artifact(active=True))
        football_edge_desk.record_shadow_prediction_bundle(conn, fixture=fixture, prediction=_ready_prediction())
        consensus_rows = football_edge_desk.fetch_consensus_evaluations(conn, fixture_id=fixture["fixture_id"])

    one_x_two = [row for row in consensus_rows if row["market_family"] == "1x2"]
    goals_total_35 = [row for row in consensus_rows if row["market_family"] == "goals_total" and row["line"] == 3.5]

    assert one_x_two and all(row["status"] == "INSUFFICIENT_AGENTS" for row in one_x_two)
    assert goals_total_35 and all(row["status"] == "INSUFFICIENT_AGENTS" for row in goals_total_35)


def test_direct_sql_constraints_reject_invalid_domain_values_and_orphans(tmp_path):
    db_path = tmp_path / "edge_desk.sqlite"
    with football_edge_desk.connect(db_path) as conn:
        rows = _seed_rows(conn)
        prediction = _raw_row(
            conn,
            "SELECT * FROM edge_desk_agent_predictions WHERE prediction_id = ?",
            (rows["prediction"]["prediction_id"],),
        )
        consensus = _raw_row(
            conn,
            "SELECT * FROM edge_desk_consensus_evaluations WHERE consensus_id = ?",
            (rows["consensus"]["consensus_id"],),
        )
        decision = _raw_row(
            conn,
            "SELECT * FROM edge_desk_decision_history WHERE decision_event_id = ?",
            (rows["decision"]["decision_event_id"],),
        )
        capability = _raw_row(
            conn,
            "SELECT * FROM edge_desk_agent_market_capabilities WHERE capability_row_id = ?",
            (rows["capability"]["capability_row_id"],),
        )

        invalid_prediction_cases = [
            {"prediction_row_id": 101, "prediction_id": "pred_prob_high", "prediction_key": "pred_prob_high", "probability": 1.5},
            {"prediction_row_id": 102, "prediction_id": "pred_prob_low", "prediction_key": "pred_prob_low", "probability": -0.01},
            {"prediction_row_id": 103, "prediction_id": "pred_negative_price", "prediction_key": "pred_negative_price", "fair_price": -1.0},
            {
                "prediction_row_id": 104,
                "prediction_id": "pred_bad_status",
                "prediction_key": "pred_bad_status",
                "prediction_status": "WRONG",
            },
            {
                "prediction_row_id": 105,
                "prediction_id": "pred_orphan_agent",
                "prediction_key": "pred_orphan_agent",
                "agent_version_tag": "missing_agent_v1",
            },
        ]
        for overrides in invalid_prediction_cases:
            sql, params = _replace_statement(
                "edge_desk_agent_predictions",
                prediction,
                verb="INSERT",
                overrides=overrides,
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql, params)

        invalid_capability_sql, invalid_capability_params = _replace_statement(
            "edge_desk_agent_market_capabilities",
            capability,
            verb="INSERT",
            overrides={
                "capability_row_id": 999,
                "version_tag": "orphan_capability_v1",
                "agent_version_tag": "missing_agent_v1",
                "capability_status": "SUPPORTED",
            },
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(invalid_capability_sql, invalid_capability_params)

        invalid_consensus_cases = [
            {
                "consensus_row_id": 201,
                "consensus_id": "consensus_bad_status",
                "consensus_key": "consensus_bad_status",
                "status": "BROKEN",
            },
            {
                "consensus_row_id": 202,
                "consensus_id": "consensus_orphan_config",
                "consensus_key": "consensus_orphan_config",
                "config_scope_key": "missing_scope",
            },
        ]
        for overrides in invalid_consensus_cases:
            sql, params = _replace_statement(
                "edge_desk_consensus_evaluations",
                consensus,
                verb="INSERT",
                overrides=overrides,
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql, params)

        invalid_decision_sql, invalid_decision_params = _replace_statement(
            "edge_desk_decision_history",
            decision,
            verb="INSERT",
            overrides={
                "decision_row_id": 301,
                "decision_event_id": "decision_bad_status",
                "decision_key": "decision_bad_status",
                "decision_status": "NOT_REAL",
            },
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(invalid_decision_sql, invalid_decision_params)

        invalid_reason_sql, invalid_reason_params = _replace_statement(
            "edge_desk_decision_history",
            decision,
            verb="INSERT",
            overrides={
                "decision_row_id": 302,
                "decision_event_id": "decision_bad_reason",
                "decision_key": "decision_bad_reason",
                "reason_code": "NOT_REAL",
            },
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(invalid_reason_sql, invalid_reason_params)


def test_phase1a_schema_db_is_upgraded_in_place_without_losing_rows(tmp_path):
    db_path = tmp_path / "edge_desk_phase1a.sqlite"
    with football_edge_desk.connect(db_path) as conn:
        _legacy_phase1a_schema(conn)
        fixture = _fixture()
        market_row = _market_row(market_family="1x2", selection="HOME", probability=0.52, fair_price=1.92)
        with conn:
            conn.execute(
                """
                INSERT INTO edge_desk_agent_registry (
                    registry_row_id, agent_id, version_tag, name, method_family, status, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    football_edge_desk.ROUTE_B_COMPOSITE_AGENT_ID,
                    football_edge_desk.FOUNDATION_VERSION,
                    "Route B Composite",
                    "GAP_TO_XG_TO_DIXON_COLES_TO_PRICING",
                    football_edge_desk.ACTIVE_AGENT_STATUS,
                    football_edge_desk.FOUNDATION_CREATED_AT,
                    "{}",
                ),
            )
            conn.execute(
                """
                INSERT INTO edge_desk_agent_market_capabilities (
                    capability_row_id, agent_id, market_family, version_tag, capability_status, valid_from, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    football_edge_desk.ROUTE_B_COMPOSITE_AGENT_ID,
                    "1x2",
                    football_edge_desk.FOUNDATION_VERSION,
                    football_edge_desk.CAPABILITY_SUPPORTED,
                    football_edge_desk.FOUNDATION_CREATED_AT,
                    football_edge_desk.FOUNDATION_CREATED_AT,
                    "{}",
                ),
            )
            conn.execute(
                """
                INSERT INTO edge_desk_consensus_configs (
                    config_row_id, config_id, market_family, version_tag, required_agents_json, minimum_quorum,
                    same_direction_required, max_probability_spread, valid_from, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    football_edge_desk.DEFAULT_CONSENSUS_CONFIG_ID,
                    None,
                    football_edge_desk.DEFAULT_CONSENSUS_VERSION,
                    "[]",
                    2,
                    1,
                    0.10,
                    football_edge_desk.DEFAULT_CONSENSUS_VALID_FROM,
                    football_edge_desk.FOUNDATION_CREATED_AT,
                    "{}",
                ),
            )
            conn.execute(
                """
                INSERT INTO edge_desk_agent_predictions (
                    prediction_row_id, prediction_id, prediction_key, fixture_id, kickoff_utc, agent_id, market_family, line,
                    selection, probability, fair_price, prediction_status, as_of, generated_at, model_version,
                    model_snapshot_id, source_prediction_version, source_payload_hash, block_reason, source_references_json,
                    metadata_json, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    "legacy_prediction",
                    "legacy_prediction",
                    fixture["fixture_id"],
                    fixture["kickoff_utc"],
                    football_edge_desk.ROUTE_B_COMPOSITE_AGENT_ID,
                    "1x2",
                    None,
                    "HOME",
                    0.52,
                    1.92,
                    "PREDICTED",
                    "2026-08-26T10:00:00+00:00",
                    "2026-08-26T10:05:00+00:00",
                    "legacy_model_v1",
                    "legacy_snapshot_v1",
                    "legacy_pred_v1",
                    "legacy_payload_hash",
                    None,
                    "{}",
                    "{}",
                    "{}",
                    "2026-08-26T10:05:00+00:00",
                ),
            )
            conn.execute(
                """
                INSERT INTO edge_desk_consensus_evaluations (
                    consensus_row_id, consensus_id, consensus_key, fixture_id, kickoff_utc, market_family, line, config_id,
                    config_version_tag, participating_agents_json, prediction_ids_json, agreed_selection,
                    consensus_probability, probability_spread, status, reason, evaluated_at, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    "legacy_consensus",
                    "legacy_consensus",
                    fixture["fixture_id"],
                    fixture["kickoff_utc"],
                    "1x2",
                    None,
                    football_edge_desk.DEFAULT_CONSENSUS_CONFIG_ID,
                    football_edge_desk.DEFAULT_CONSENSUS_VERSION,
                    '["ROUTE_B_COMPOSITE"]',
                    '["legacy_prediction"]',
                    None,
                    None,
                    None,
                    "INSUFFICIENT_AGENTS",
                    "CONSENSUS_INSUFFICIENT_AGENTS: required quorum 2, got 1",
                    "2026-08-26T10:05:00+00:00",
                    "{}",
                    "2026-08-26T10:05:00+00:00",
                ),
            )
            conn.execute(
                """
                INSERT INTO edge_desk_decision_history (
                    decision_row_id, decision_event_id, decision_key, fixture_id, kickoff_utc, market_family, line,
                    selection, consensus_id, agent_prediction_ids_json, decision_status, reason_code, reason_detail,
                    decided_at, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    "legacy_decision",
                    "legacy_decision",
                    fixture["fixture_id"],
                    fixture["kickoff_utc"],
                    "1x2",
                    None,
                    "HOME",
                    "legacy_consensus",
                    '["legacy_prediction"]',
                    "NO_BET",
                    "CONSENSUS_INSUFFICIENT_AGENTS",
                    "CONSENSUS_INSUFFICIENT_AGENTS: required quorum 2, got 1",
                    "2026-08-26T10:05:00+00:00",
                    "{}",
                    "2026-08-26T10:05:00+00:00",
                ),
            )

        football_edge_desk.ensure_schema(conn)

        prediction_columns = {row["name"] for row in conn.execute("PRAGMA table_info(edge_desk_agent_predictions)")}
        capability_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(edge_desk_agent_market_capabilities)")
        }
        config_columns = {row["name"] for row in conn.execute("PRAGMA table_info(edge_desk_consensus_configs)")}
        consensus_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(edge_desk_consensus_evaluations)")
        }
        upgraded_prediction = _raw_row(conn, "SELECT * FROM edge_desk_agent_predictions WHERE prediction_id = ?", ("legacy_prediction",))
        upgraded_config = _raw_row(conn, "SELECT * FROM edge_desk_consensus_configs WHERE config_id = ?", (football_edge_desk.DEFAULT_CONSENSUS_CONFIG_ID,))

    assert "agent_version_tag" in prediction_columns
    assert "agent_version_tag" in capability_columns
    assert "config_scope_key" in config_columns
    assert "config_scope_key" in consensus_columns
    assert upgraded_prediction["agent_version_tag"] == football_edge_desk.FOUNDATION_VERSION
    assert upgraded_config["config_scope_key"] == football_edge_desk.GLOBAL_CONFIG_SCOPE_KEY
