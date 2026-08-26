from __future__ import annotations

import sqlite3

import pytest

from sports_ai.storage import football_edge_desk


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


def test_edge_desk_registry_contains_only_route_b_active_agent(tmp_path):
    with football_edge_desk.connect(tmp_path / "edge_desk.sqlite") as conn:
        football_edge_desk.ensure_schema(conn)
        agents = football_edge_desk.list_active_agents(conn)

    assert [agent["agent_id"] for agent in agents] == [football_edge_desk.ROUTE_B_COMPOSITE_AGENT_ID]
    assert agents[0]["method_family"] == "GAP_TO_XG_TO_DIXON_COLES_TO_PRICING"


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
        football_edge_desk.record_shadow_prediction_bundle(conn, fixture=_fixture(), prediction=_ready_prediction())
        prediction_row = football_edge_desk.fetch_agent_predictions(conn)[0]
        consensus_row = football_edge_desk.fetch_consensus_evaluations(conn)[0]
        decision_row = football_edge_desk.fetch_decision_history(conn)[0]

        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "UPDATE edge_desk_agent_predictions SET selection = 'DRAW' WHERE prediction_id = ?",
                (prediction_row["prediction_id"],),
            )
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "DELETE FROM edge_desk_agent_predictions WHERE prediction_id = ?",
                (prediction_row["prediction_id"],),
            )
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "UPDATE edge_desk_consensus_evaluations SET status = 'PASSED' WHERE consensus_id = ?",
                (consensus_row["consensus_id"],),
            )
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "DELETE FROM edge_desk_consensus_evaluations WHERE consensus_id = ?",
                (consensus_row["consensus_id"],),
            )
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "UPDATE edge_desk_decision_history SET decision_status = 'QUALIFIED' WHERE decision_event_id = ?",
                (decision_row["decision_event_id"],),
            )
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(
                "DELETE FROM edge_desk_decision_history WHERE decision_event_id = ?",
                (decision_row["decision_event_id"],),
            )


def test_not_supported_capability_is_not_counted_as_a_positive_vote(tmp_path):
    db_path = tmp_path / "edge_desk.sqlite"
    with football_edge_desk.connect(db_path) as conn:
        football_edge_desk.ensure_schema(conn)
        with conn:
            conn.execute(
                """
                INSERT INTO edge_desk_agent_market_capabilities (
                    agent_id, market_family, version_tag, capability_status, valid_from, created_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    football_edge_desk.ROUTE_B_COMPOSITE_AGENT_ID,
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
