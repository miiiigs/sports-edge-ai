"""Focused tests for the market-freshness paper-eligibility gate."""

from __future__ import annotations

import pytest

from sports_ai.dashboard.current_market import build_fixture_market_evaluation
from sports_ai.dashboard.day12_rolling_xg import SOURCE_IDENTIFIER, build_xg_inspection
from sports_ai.experiments.football_day11_manager_core import DECISION_NO_BET
from sports_ai.experiments.football_day12_safeguards import (
    DAY12_BLOCKED_MISSING_XG_DATA,
    XG_APPLICABLE_COMPETITIONS,
)
from sports_ai.market_data.providers import NormalizedMarketSnapshot
from sports_ai.market_data.the_odds_api import (
    FRESHNESS_STATE_FRESH,
    FRESHNESS_STATE_STALE,
    FRESHNESS_STATE_UNKNOWN,
    TheOddsApiProvider,
    classify_freshness_state,
    resolve_market_max_age_minutes,
)

FIXTURE_ID = "football-data.org:560546"
MODEL_AS_OF = "2026-05-24T23:59:59+00:00"
KICKOFF = "2026-08-23T14:00:00+00:00"
EVAL = "2026-08-23T09:00:00+00:00"

FRESH_SOURCE = "2026-08-23T08:50:00+00:00"   # 10 min before EVAL
STALE_SOURCE = "2026-08-23T07:00:00+00:00"   # 2 h before EVAL
FUTURE_SOURCE = "2026-08-23T09:10:00+00:00"  # after EVAL (future)


def _classify(source_timestamp, evaluated_at, threshold, kickoff=None):
    return classify_freshness_state(
        source_timestamp=source_timestamp,
        evaluated_at=evaluated_at,
        threshold_minutes=threshold,
        kickoff_utc=kickoff,
    )


# ---------------------------------------------------------------------------
# Freshness classification (pure function)
# ---------------------------------------------------------------------------


def test_fresh_snapshot_classified_fresh():
    assert _classify(FRESH_SOURCE, EVAL, 60) == FRESHNESS_STATE_FRESH


def test_stale_snapshot_classified_stale():
    assert _classify(STALE_SOURCE, EVAL, 60) == FRESHNESS_STATE_STALE


def test_missing_timestamp_unknown():
    assert _classify(None, EVAL, 60) == FRESHNESS_STATE_UNKNOWN


def test_malformed_timestamp_unknown():
    assert _classify("not-a-date", EVAL, 60) == FRESHNESS_STATE_UNKNOWN


def test_future_timestamp_unknown():
    assert _classify(FUTURE_SOURCE, EVAL, 60) == FRESHNESS_STATE_UNKNOWN


def test_threshold_none_unknown():
    assert _classify(FRESH_SOURCE, EVAL, None) == FRESHNESS_STATE_UNKNOWN


def test_exact_threshold_boundary_fresh():
    # age exactly 60 minutes -> FRESH (age <= threshold)
    assert _classify("2026-08-23T08:00:00+00:00", EVAL, 60) == FRESHNESS_STATE_FRESH


def test_threshold_boundary_plus_epsilon_stale():
    # age 61 minutes -> STALE
    assert _classify("2026-08-23T07:59:00+00:00", EVAL, 60) == FRESHNESS_STATE_STALE


def test_post_kickoff_source_stale():
    # source timestamp after fixture kickoff -> STALE (never a pre-match FRESH)
    assert _classify(FRESH_SOURCE, EVAL, 60, kickoff="2026-08-23T08:00:00+00:00") == FRESHNESS_STATE_STALE


def test_snapshot_ages_out_at_later_decision_time():
    # Same source is FRESH at T1 but STALE when evaluated 2h later: the gate must
    # recompute against decision time, not persist capture-time freshness.
    source = "2026-08-23T07:50:00+00:00"
    assert _classify(source, "2026-08-23T08:00:00+00:00", 60) == FRESHNESS_STATE_FRESH
    assert _classify(source, "2026-08-23T10:00:00+00:00", 60) == FRESHNESS_STATE_STALE


def test_resolve_market_max_age_minutes(monkeypatch):
    monkeypatch.delenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", raising=False)
    assert resolve_market_max_age_minutes() is None
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    assert resolve_market_max_age_minutes() == 60
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "0")
    assert resolve_market_max_age_minutes() is None
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "-5")
    assert resolve_market_max_age_minutes() is None
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "abc")
    assert resolve_market_max_age_minutes() is None


def test_provider_freshness_delegates_to_shared_classifier(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    provider = TheOddsApiProvider()
    assert provider._freshness_state(
        source_timestamp=FRESH_SOURCE, captured_at=EVAL, kickoff_utc=None
    ) == FRESHNESS_STATE_FRESH


# ---------------------------------------------------------------------------
# Paper eligibility gate
# ---------------------------------------------------------------------------


def _snapshot(fixture_id, bookmaker, selection, price, reference, pid, source_timestamp):
    return NormalizedMarketSnapshot(
        provider="the-odds-api",
        fixture_id=fixture_id,
        provider_fixture_id="odds_event_1",
        captured_at=EVAL,
        source_timestamp=source_timestamp,
        market="1x2",
        selection=selection,
        line=None,
        decimal_odds=price,
        bookmaker=bookmaker,
        ingestion_version="the_odds_api_current_market_provider_v1",
        provider_market_id=pid,
        is_pre_match=True,
        home_team_name="Home FC",
        away_team_name="Away FC",
        competition_id="ENG1",
        competition_name="Premier League",
        kickoff_utc=KICKOFF,
        freshness_state="FRESH",  # diagnostic only; the gate recomputes from source_timestamp
        reference_designation="reference" if reference else "available",
        reference_rank=0 if reference else None,
    )


def _snapshots(fixture_id, target_freshness):
    fresh = FRESH_SOURCE
    target = {"FRESH": FRESH_SOURCE, "STALE": STALE_SOURCE, "UNKNOWN": None, "FUTURE": FUTURE_SOURCE}[target_freshness]
    return [
        _snapshot(fixture_id, "pinnacle", "HOME", 2.30, True, "pin-home", fresh),
        _snapshot(fixture_id, "pinnacle", "DRAW", 3.40, True, "pin-draw", fresh),
        _snapshot(fixture_id, "pinnacle", "AWAY", 3.20, True, "pin-away", fresh),
        _snapshot(fixture_id, "bet365", "HOME", 3.20, False, "b365-home", fresh),
        _snapshot(fixture_id, "bet365", "DRAW", 3.70, False, "b365-draw", fresh),
        _snapshot(fixture_id, "bet365", "AWAY", 3.60, False, "b365-away", fresh),
        _snapshot(fixture_id, "betmax", "HOME", 3.40, False, "max-home", target),
        _snapshot(fixture_id, "betmax", "DRAW", 3.90, False, "max-draw", target),
        _snapshot(fixture_id, "betmax", "AWAY", 3.80, False, "max-away", target),
    ]


def _snapshots_no_bet(fixture_id):
    fresh = FRESH_SOURCE
    return [
        _snapshot(fixture_id, "pinnacle", "HOME", 2.30, True, "pin-home", fresh),
        _snapshot(fixture_id, "pinnacle", "DRAW", 3.40, True, "pin-draw", fresh),
        _snapshot(fixture_id, "pinnacle", "AWAY", 3.20, True, "pin-away", fresh),
        _snapshot(fixture_id, "bet365", "HOME", 2.10, False, "b365-home", fresh),
        _snapshot(fixture_id, "bet365", "DRAW", 3.00, False, "b365-draw", fresh),
        _snapshot(fixture_id, "bet365", "AWAY", 2.80, False, "b365-away", fresh),
    ]


def _market_row(fixture_id, selection, win, loss, fair, competition_id="ENG1", competition_name="Premier League"):
    return {
        "as_of": MODEL_AS_OF,
        "competition_id": competition_id,
        "competition_name": competition_name,
        "division_code": "E0",
        "effective_loss_stake_probability": loss,
        "effective_win_stake_probability": win,
        "event_id": fixture_id,
        "fair_decimal_price": fair,
        "fixture_id": fixture_id,
        "kickoff_at": KICKOFF,
        "kickoff_time": "14:00",
        "line": None,
        "market_family": "1x2",
        "match_date": "2026-08-23",
        "model_versions": {"conversion_model_version": "football_day8_goal_conversion_v1"},
        "output_schema_version": "football_day10_market_price_record_v1",
        "pricing_model_version": "football_day10_market_pricing_v1",
        "season_code": "2026",
        "selection": selection,
        "settlement_engine_version": "football_day10_settlement_engine_v1",
        "settlement_pricing_policy_id": "zero_ev_with_partial_and_push_settlement_v1",
        "settlement_probabilities": {
            "full_win_probability": win,
            "full_loss_probability": loss,
            "push_probability": 0.0,
            "half_win_probability": 0.0,
            "half_loss_probability": 0.0,
        },
        "source_corner_object_id": None,
        "source_scoreline_object_id": f"scoreline_{fixture_id}",
    }


def _prediction(fixture_id, competition_id="ENG1", competition_name="Premier League"):
    return {
        "prediction_status": "READY",
        "fixture_id": fixture_id,
        "competition_id": competition_id,
        "competition_name": competition_name,
        "home_team_name": "Home FC",
        "away_team_name": "Away FC",
        "kickoff_utc": KICKOFF,
        "model_as_of": MODEL_AS_OF,
        "model_snapshot_id": "snapshot_1",
        "expected_goals_home": 1.6,
        "expected_goals_away": 1.2,
        "expected_total_goals": 2.8,
        "market_rows": [
            _market_row(fixture_id, "HOME", 0.35, 0.65, 2.60, competition_id, competition_name),
            _market_row(fixture_id, "DRAW", 0.30, 0.70, 3.40, competition_id, competition_name),
            _market_row(fixture_id, "AWAY", 0.35, 0.65, 2.90, competition_id, competition_name),
        ],
    }


def _fixture_context(fixture_id, competition_id="ENG1", competition_name="Premier League"):
    return {
        "fixture_id": fixture_id,
        "competition_id": competition_id,
        "competition_name": competition_name,
        "division_code": "E0",
        "season": "2026",
        "home_team_name": "Home FC",
        "away_team_name": "Away FC",
        "kickoff_utc": KICKOFF,
    }


def _rolling_row(fixture_id, role, recent):
    return {
        "fixture_id": fixture_id,
        "team_role": role,
        "recent_xg_for_per_match": recent,
        "matches_in_window": 5,
        "as_of": "2026-05-24T20:00:00+00:00",
        "window_start_at": "2026-04-01T20:00:00+00:00",
        "window_end_at": "2026-05-24T20:00:00+00:00",
        "source": SOURCE_IDENTIFIER,
        "competition_id": "ENG1",
    }


def _valid_xg_rows(fixture_id, prediction):
    exp_home = float(prediction["expected_goals_home"])
    exp_away = float(prediction["expected_goals_away"])
    rows = {
        (fixture_id, "HOME"): _rolling_row(fixture_id, "HOME", exp_home),
        (fixture_id, "AWAY"): _rolling_row(fixture_id, "AWAY", exp_away),
    }
    return rows, build_xg_inspection(rows)


def _home_candidate(evaluation):
    for row in evaluation.get("decision_rows") or []:
        if row["selection"] == "HOME":
            return row
    return None


def _evaluate(monkeypatch, *, freshness, competition_id="ENG3", competition_name="League One", current_xg_connected=False, xg=None):
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    kwargs = {}
    if current_xg_connected and xg is not None:
        kwargs["xg_profile_provider"] = lambda: xg
    return build_fixture_market_evaluation(
        fixture_context=_fixture_context(FIXTURE_ID, competition_id=competition_id, competition_name=competition_name),
        prediction=_prediction(FIXTURE_ID, competition_id=competition_id, competition_name=competition_name),
        snapshots=_snapshots(FIXTURE_ID, freshness),
        current_xg_connected=current_xg_connected,
        market_freshness_evaluated_at=EVAL,
        **kwargs,
    )


def test_candidate_fresh_can_continue(monkeypatch):
    evaluation = _evaluate(monkeypatch, freshness="FRESH")
    row = _home_candidate(evaluation)
    assert row is not None and row["decision"] == "PRELIMINARY_CANDIDATE"
    assert row["market_freshness"] == "FRESH"
    assert row["freshness_gate_passed"] is True
    assert row["paper_entry_eligible"] is True


def test_candidate_stale_not_paper_eligible(monkeypatch):
    evaluation = _evaluate(monkeypatch, freshness="STALE")
    row = _home_candidate(evaluation)
    assert row is not None and row["decision"] == "PRELIMINARY_CANDIDATE"
    assert row["market_freshness"] == "STALE"
    assert row["freshness_gate_passed"] is False
    assert row["paper_entry_eligible"] is False


def test_candidate_unknown_not_paper_eligible(monkeypatch):
    evaluation = _evaluate(monkeypatch, freshness="UNKNOWN")
    row = _home_candidate(evaluation)
    assert row is not None and row["decision"] == "PRELIMINARY_CANDIDATE"
    assert row["market_freshness"] == "UNKNOWN"
    assert row["freshness_gate_passed"] is False
    assert row["paper_entry_eligible"] is False


def test_candidate_future_source_not_paper_eligible(monkeypatch):
    evaluation = _evaluate(monkeypatch, freshness="FUTURE")
    row = _home_candidate(evaluation)
    assert row is not None and row["decision"] == "PRELIMINARY_CANDIDATE"
    assert row["market_freshness"] == "UNKNOWN"
    assert row["paper_entry_eligible"] is False


def test_persisted_fresh_label_does_not_override_stale_source(monkeypatch):
    # Even though the snapshot carries freshness_state="FRESH" (as if persisted),
    # the gate recomputes from source_timestamp and must still block a stale source.
    evaluation = _evaluate(monkeypatch, freshness="STALE")
    row = _home_candidate(evaluation)
    assert row is not None
    assert row["market_freshness"] == "STALE"
    assert row["paper_entry_eligible"] is False


def test_xg_pass_fresh_can_permit_paper_entry(monkeypatch):
    rows, inspection = _valid_xg_rows(FIXTURE_ID, _prediction(FIXTURE_ID))
    evaluation = _evaluate(monkeypatch, freshness="FRESH", competition_id="ENG1", competition_name="Premier League", current_xg_connected=True, xg=(rows, inspection))
    row = _home_candidate(evaluation)
    assert row is not None and row["decision"] == "PRELIMINARY_CANDIDATE"
    assert row["day12_state"] == "PASSED"
    assert row["paper_entry_eligible"] is True


def test_xg_pass_stale_still_blocks_paper_entry(monkeypatch):
    rows, inspection = _valid_xg_rows(FIXTURE_ID, _prediction(FIXTURE_ID))
    evaluation = _evaluate(monkeypatch, freshness="STALE", competition_id="ENG1", competition_name="Premier League", current_xg_connected=True, xg=(rows, inspection))
    row = _home_candidate(evaluation)
    assert row is not None and row["decision"] == "PRELIMINARY_CANDIDATE"
    assert row["day12_state"] == "PASSED"  # Day 12 evaluated; freshness still blocks eligibility
    assert row["paper_entry_eligible"] is False


def test_xg_pass_unknown_still_blocks_paper_entry(monkeypatch):
    rows, inspection = _valid_xg_rows(FIXTURE_ID, _prediction(FIXTURE_ID))
    evaluation = _evaluate(monkeypatch, freshness="UNKNOWN", competition_id="ENG1", competition_name="Premier League", current_xg_connected=True, xg=(rows, inspection))
    row = _home_candidate(evaluation)
    assert row is not None and row["decision"] == "PRELIMINARY_CANDIDATE"
    assert row["day12_state"] == "PASSED"
    assert row["paper_entry_eligible"] is False


def test_freshness_block_preserves_day11_decision(monkeypatch):
    evaluation = _evaluate(monkeypatch, freshness="STALE")
    row = _home_candidate(evaluation)
    assert row is not None
    assert row["decision"] == "PRELIMINARY_CANDIDATE"
    assert row["paper_entry_eligible"] is False


def test_no_bet_remains_no_bet(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    evaluation = build_fixture_market_evaluation(
        fixture_context=_fixture_context(FIXTURE_ID, competition_id="ENG3", competition_name="League One"),
        prediction=_prediction(FIXTURE_ID, competition_id="ENG3", competition_name="League One"),
        snapshots=_snapshots_no_bet(FIXTURE_ID),
        current_xg_connected=False,
        market_freshness_evaluated_at=EVAL,
    )
    row = _home_candidate(evaluation)
    assert row is not None
    assert row["decision"] == DECISION_NO_BET
    assert row["paper_entry_eligible"] is False


def test_disabled_xg_behavior_unchanged(monkeypatch):
    evaluation = _evaluate(monkeypatch, freshness="FRESH", competition_id="ENG1", competition_name="Premier League")
    row = _home_candidate(evaluation)
    assert "ENG1" in XG_APPLICABLE_COMPETITIONS
    assert row is not None and row["decision"] == "PRELIMINARY_CANDIDATE"
    assert row["day12_state"] == DAY12_BLOCKED_MISSING_XG_DATA
    assert row["paper_entry_eligible"] is False


def test_stale_and_unknown_ledger_entries_impossible(monkeypatch):
    for freshness in ("STALE", "UNKNOWN", "FUTURE"):
        rows, inspection = _valid_xg_rows(FIXTURE_ID, _prediction(FIXTURE_ID))
        evaluation = _evaluate(monkeypatch, freshness=freshness, competition_id="ENG1", competition_name="Premier League", current_xg_connected=True, xg=(rows, inspection))
        assert evaluation["counts"]["passed_safeguards"] == 0
        assert all(not row["paper_entry_eligible"] for row in evaluation["decision_rows"])


def test_fresh_market_and_valid_xg_allow_paper_entry(monkeypatch):
    # Integration-style: candidate + FRESH market + valid Day 12 rolling xG -> eligible.
    rows, inspection = _valid_xg_rows(FIXTURE_ID, _prediction(FIXTURE_ID))
    evaluation = _evaluate(monkeypatch, freshness="FRESH", competition_id="ENG1", competition_name="Premier League", current_xg_connected=True, xg=(rows, inspection))
    assert evaluation["counts"]["passed_safeguards"] >= 1
    assert any(row["paper_entry_eligible"] for row in evaluation["decision_rows"])
