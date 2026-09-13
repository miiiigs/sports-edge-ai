from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sports_ai.dashboard.production_decision_policy import (
    current_decision_state,
    safeguard_label,
    safeguard_reason,
)
from sports_ai.dashboard.production_research import (
    _upsert_event_bundle,
    active_season_code,
    latest_complete_match_date,
)
from sports_ai.storage import football_candidate_evidence, football_research_store


def test_active_season_code_rolls_in_july():
    assert active_season_code(date(2026, 6, 30)) == "2526"
    assert active_season_code(date(2026, 7, 1)) == "2627"
    assert active_season_code(date(2026, 9, 13)) == "2627"


def test_model_state_refresh_upserts_sports_truth_without_market_rows(tmp_path: Path):
    db_path = tmp_path / "research.sqlite"
    with football_research_store.connect(db_path) as conn:
        football_research_store.ensure_schema(conn)
        sports_row = SimpleNamespace(
            event_id="evt_football_20260912_home_away",
            competition_id="ENG1",
            competition_name="Premier League",
            season_code="2627",
            division_code="E0",
            match_date="2026-09-12",
            kickoff_time="15:00",
            home_team="Home",
            away_team="Away",
            home_goals=2,
            away_goals=1,
            result_code="H",
            half_time_home_goals=1,
            half_time_away_goals=0,
            half_time_result_code="H",
            referee="Ref",
            home_shots=12,
            away_shots=8,
            home_shots_on_target=5,
            away_shots_on_target=3,
            home_fouls=10,
            away_fouls=11,
            home_corners=6,
            away_corners=4,
            home_yellow_cards=1,
            away_yellow_cards=2,
            home_red_cards=0,
            away_red_cards=0,
        )
        bundle = SimpleNamespace(sports_rows=(sports_row,), market_rows=(object(),))
        _upsert_event_bundle(conn, bundle)
        sports_row.home_goals = 3
        _upsert_event_bundle(conn, bundle)
        event = conn.execute(
            "SELECT home_goals, away_goals FROM football_events WHERE event_id = ?",
            (sports_row.event_id,),
        ).fetchone()
        market_count = conn.execute("SELECT COUNT(*) FROM football_market_quotes").fetchone()[0]

    assert event["home_goals"] == 3
    assert event["away_goals"] == 1
    assert market_count == 0
    assert latest_complete_match_date(db_path) == "2026-09-12"


def _candidate_row(*, eligible: bool, context_status: str, context_reason: str) -> dict:
    return {
        "fixture_id": "fixture-1",
        "market_family": "btts",
        "selection": "YES",
        "line": None,
        "model_probability": 0.58,
        "model_fair_decimal_price": 1.72,
        "best_available_price": 1.95,
        "best_available_bookmaker": "book",
        "settlement_aware_edge": 0.131,
        "minimum_acceptable_price": 1.82,
        "market_as_of": "2026-09-13T08:30:00+00:00",
        "best_available_source_timestamp": "2026-09-13T08:30:00+00:00",
        "decision": "PRELIMINARY_CANDIDATE",
        "reason_codes": ["PASS_EDGE_THRESHOLD", "PASS_MINIMUM_PRICE"],
        "market_freshness": "FRESH",
        "day12_state": "PASSED",
        "xg_status": "XG_PASS",
        "xg_reason_codes": [],
        "context_status": context_status,
        "context_reason_codes": [context_reason] if context_reason else [],
        "context_threshold_adjustment": 0.0,
        "context_adjusted_threshold": None,
        "context_threshold_gate_passed": None,
        "context_diagnostics": {"source": "test"},
        "paper_entry_eligible": eligible,
        "current_decision_state": "BET" if eligible else "NO_BET_CONTEXT_VETO",
    }


def test_candidate_evidence_is_idempotent_and_keeps_veto_audit_outside_action_ledger(tmp_path: Path):
    db_path = tmp_path / "paper.sqlite"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    prediction = {
        "fixture_id": "fixture-1",
        "prediction_id": "prediction-1",
        "model_snapshot_id": "snapshot-1",
        "kickoff_utc": "2026-09-13T10:00:00+00:00",
    }
    first = _candidate_row(eligible=True, context_status="PASS", context_reason="")
    second = _candidate_row(
        eligible=False,
        context_status="VETO",
        context_reason="TWO_OR_MORE_EXPECTED_STARTERS_MISSING",
    )
    second["best_available_source_timestamp"] = "2026-09-13T08:45:00+00:00"

    first_id = football_candidate_evidence.record_candidate_observation(
        conn, prediction=prediction, decision_row=first, provider="test-provider"
    )
    second_id = football_candidate_evidence.record_candidate_observation(
        conn, prediction=prediction, decision_row=second, provider="test-provider"
    )
    observations = football_candidate_evidence.fetch_observations(conn)
    audit_count = conn.execute(
        "SELECT COUNT(*) FROM football_candidate_observation_audit WHERE observation_id = ?",
        (first_id,),
    ).fetchone()[0]
    summary = football_candidate_evidence.evidence_summary(conn)
    conn.close()

    assert first_id == second_id
    assert len(observations) == 1
    assert observations[0]["entry_odds"] == 1.95
    assert observations[0]["latest_audit"]["context_status"] == "VETO"
    assert audit_count == 2
    assert summary["observations"] == 1
    assert summary["vetoed_observations"] == 1
    assert summary["ever_actionable_observations"] == 1


def test_context_veto_takes_precedence_over_day12_pass_in_presentation():
    row = _candidate_row(
        eligible=False,
        context_status="VETO",
        context_reason="KEY_ATTACKER_RULED_OUT_WITHIN_24H",
    )

    assert current_decision_state(row) == "NO_BET_CONTEXT_VETO"
    assert safeguard_label(row) == "FAIL"
    assert "Key Attacker" in safeguard_reason(row)


def test_context_missing_evidence_is_blocked_not_pass():
    row = _candidate_row(
        eligible=False,
        context_status="BLOCKED",
        context_reason="REQUIRED_EVIDENCE_MISSING",
    )

    assert current_decision_state(row) == "BLOCKED_CONTEXT_EVIDENCE"
    assert safeguard_label(row) == "BLOCKED"
