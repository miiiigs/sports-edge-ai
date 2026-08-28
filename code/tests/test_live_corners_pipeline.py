"""Integration tests: live corner totals through the Day 10/11/12 paper pipeline."""

from __future__ import annotations

import pytest

from sports_ai.dashboard.current_market import (
    build_fixture_market_evaluation,
    current_snapshot_to_day11_row,
)
from sports_ai.dashboard.day12_rolling_xg import SOURCE_IDENTIFIER, build_xg_inspection
from sports_ai.experiments.football_day11_manager_core import (
    DECISION_MISSING_REQUIRED_MARKET_DATA,
    DECISION_PRELIMINARY_CANDIDATE,
    REASON_MISSING_REFERENCE_MARKET,
    canonical_market_identity,
)
from sports_ai.market_data.providers import NormalizedMarketSnapshot
from sports_ai.storage.football_paper_trading import (
    PaperTradeEntry,
    connect,
    find_existing_paper_trade_entry,
    record_paper_trade_entry,
    settlement_state_for_market,
)

FIXTURE_ID = "football-data.org:560546"
MODEL_AS_OF = "2026-05-24T23:59:59+00:00"
KICKOFF = "2026-08-23T14:00:00+00:00"
EVAL = "2026-08-23T09:00:00+00:00"
FRESH_SOURCE = "2026-08-23T08:50:00+00:00"
STALE_SOURCE = "2026-08-23T07:00:00+00:00"

FIXTURE_CONTEXT = {
    "fixture_id": FIXTURE_ID,
    "competition_id": "ENG1",
    "competition_name": "Premier League",
    "division_code": "E0",
    "season": "2026",
    "home_team_name": "Home FC",
    "away_team_name": "Away FC",
    "kickoff_utc": KICKOFF,
}


def _corner_snapshot(
    bookmaker,
    selection,
    price,
    *,
    line,
    reference,
    source_timestamp,
    freshness="FRESH",
):
    return NormalizedMarketSnapshot(
        provider="the-odds-api",
        fixture_id=FIXTURE_ID,
        provider_fixture_id="odds_event_1",
        captured_at=EVAL,
        source_timestamp=source_timestamp,
        market="corner_total",
        selection=selection,
        line=line,
        decimal_odds=price,
        bookmaker=bookmaker,
        ingestion_version="the_odds_api_current_market_provider_v1",
        provider_market_id=f"odds_event_1:{bookmaker}:alternate_totals_corners:{selection}:{line}",
        is_pre_match=True,
        home_team_name="Home FC",
        away_team_name="Away FC",
        competition_id="ENG1",
        competition_name="Premier League",
        kickoff_utc=KICKOFF,
        freshness_state=freshness,
        reference_designation="reference" if reference else "available",
        reference_rank=0 if reference else None,
    )


def _market_row(selection, win, loss, fair, *, line):
    return {
        "as_of": MODEL_AS_OF,
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "division_code": "E0",
        "effective_loss_stake_probability": loss,
        "effective_win_stake_probability": win,
        "event_id": FIXTURE_ID,
        "fair_decimal_price": fair,
        "fixture_id": FIXTURE_ID,
        "kickoff_at": KICKOFF,
        "kickoff_time": "14:00",
        "line": line,
        "market_family": "corner_total",
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
        "source_corner_object_id": f"corner_{FIXTURE_ID}",
        "source_scoreline_object_id": None,
    }


def _prediction():
    return {
        "prediction_status": "READY",
        "prediction_id": "prediction_1",
        "version_key": "prediction_1|v1",
        "fixture_id": FIXTURE_ID,
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "home_team_name": "Home FC",
        "away_team_name": "Away FC",
        "kickoff_utc": KICKOFF,
        "generated_at": EVAL,
        "model_as_of": MODEL_AS_OF,
        "model_snapshot_id": "snapshot_1",
        "expected_goals_home": 1.6,
        "expected_goals_away": 1.2,
        "expected_total_goals": 2.8,
        "market_rows": [
            _market_row("OVER", 0.56, 0.44, 1.89, line=9.5),
            _market_row("UNDER", 0.44, 0.56, 2.12, line=9.5),
            _market_row("OVER", 0.45, 0.55, 2.22, line=10.0),
            _market_row("UNDER", 0.55, 0.45, 1.82, line=10.0),
        ],
    }


def _valid_xg_rows(prediction):
    rows = {
        (FIXTURE_ID, "HOME"): {
            "fixture_id": FIXTURE_ID,
            "team_role": "HOME",
            "recent_xg_for_per_match": float(prediction["expected_goals_home"]),
            "matches_in_window": 5,
            "as_of": "2026-05-24T20:00:00+00:00",
            "window_start_at": "2026-04-01T20:00:00+00:00",
            "window_end_at": "2026-05-24T20:00:00+00:00",
            "source": SOURCE_IDENTIFIER,
            "competition_id": "ENG1",
        },
        (FIXTURE_ID, "AWAY"): {
            "fixture_id": FIXTURE_ID,
            "team_role": "AWAY",
            "recent_xg_for_per_match": float(prediction["expected_goals_away"]),
            "matches_in_window": 5,
            "as_of": "2026-05-24T20:00:00+00:00",
            "window_start_at": "2026-04-01T20:00:00+00:00",
            "window_end_at": "2026-05-24T20:00:00+00:00",
            "source": SOURCE_IDENTIFIER,
            "competition_id": "ENG1",
        },
    }
    return rows, build_xg_inspection(rows)


def _evaluate(monkeypatch, *, snapshots, prediction, current_xg_connected=False):
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    kwargs = {}
    if current_xg_connected:
        rows, inspection = _valid_xg_rows(prediction)
        kwargs["xg_profile_provider"] = lambda: (rows, inspection)
    return build_fixture_market_evaluation(
        fixture_context=FIXTURE_CONTEXT,
        prediction=prediction,
        snapshots=snapshots,
        current_xg_connected=current_xg_connected,
        market_freshness_evaluated_at=EVAL,
        **kwargs,
    )


def _corner_row(evaluation, *, selection, line):
    for row in evaluation.get("decision_rows") or []:
        if row["market_family"] == "corner_total" and row["selection"] == selection and row["line"] == line:
            return row
    return None


def test_current_corner_snapshot_canonical_identity_matches_day10_row():
    snapshot = _corner_snapshot(
        "pinnacle",
        "OVER",
        1.93,
        line=9.5,
        reference=True,
        source_timestamp=FRESH_SOURCE,
    )
    row = current_snapshot_to_day11_row(snapshot, fixture_context=FIXTURE_CONTEXT)
    assert row["market_family"] == "corner_total"
    assert row["normalized_line"] == 9.5
    assert row["normalized_selection"] == "OVER"
    assert row["canonical_market_identity"] == canonical_market_identity(
        fixture_id=FIXTURE_ID,
        market_family="corner_total",
        normalized_line=9.5,
        normalized_selection="OVER",
    )


def test_corner_missing_reference_market_fails_closed(monkeypatch):
    prediction = _prediction()
    snapshots = [
        _corner_snapshot("bet365", "OVER", 2.22, line=9.5, reference=False, source_timestamp=FRESH_SOURCE),
        _corner_snapshot("bet365", "UNDER", 1.70, line=9.5, reference=False, source_timestamp=FRESH_SOURCE),
    ]
    evaluation = _evaluate(monkeypatch, snapshots=snapshots, prediction=prediction)

    over = _corner_row(evaluation, selection="OVER", line=9.5)
    under = _corner_row(evaluation, selection="UNDER", line=9.5)
    assert over is not None
    assert under is not None
    assert over["decision"] == DECISION_MISSING_REQUIRED_MARKET_DATA
    assert under["decision"] == DECISION_MISSING_REQUIRED_MARKET_DATA
    assert over["reason_codes"] == [REASON_MISSING_REFERENCE_MARKET]
    assert under["reason_codes"] == [REASON_MISSING_REFERENCE_MARKET]
    assert over["paper_entry_eligible"] is False
    assert under["paper_entry_eligible"] is False


def test_no_nearest_corner_line_matching_and_best_offer_stays_separate(monkeypatch):
    prediction = _prediction()
    snapshots = [
        _corner_snapshot("pinnacle", "OVER", 1.95, line=9.5, reference=True, source_timestamp=FRESH_SOURCE),
        _corner_snapshot("pinnacle", "UNDER", 1.91, line=9.5, reference=True, source_timestamp=FRESH_SOURCE),
        _corner_snapshot("bet365", "OVER", 2.22, line=9.5, reference=False, source_timestamp=FRESH_SOURCE),
        _corner_snapshot("bet365", "UNDER", 1.70, line=9.5, reference=False, source_timestamp=FRESH_SOURCE),
        _corner_snapshot("pinnacle", "OVER", 2.20, line=10.5, reference=True, source_timestamp=FRESH_SOURCE),
        _corner_snapshot("pinnacle", "UNDER", 1.72, line=10.5, reference=True, source_timestamp=FRESH_SOURCE),
        _corner_snapshot("bet365", "OVER", 2.35, line=10.5, reference=False, source_timestamp=FRESH_SOURCE),
        _corner_snapshot("bet365", "UNDER", 1.65, line=10.5, reference=False, source_timestamp=FRESH_SOURCE),
    ]
    evaluation = _evaluate(
        monkeypatch,
        snapshots=snapshots,
        prediction=prediction,
        current_xg_connected=True,
    )

    lines = sorted({row["line"] for row in evaluation["decision_rows"]})
    assert lines == [9.5]

    over = _corner_row(evaluation, selection="OVER", line=9.5)
    assert over is not None
    assert over["decision"] == DECISION_PRELIMINARY_CANDIDATE
    assert over["best_available_bookmaker"] == "bet365"
    assert over["best_available_price"] == pytest.approx(2.22)
    assert over["best_available_provider_market_id"] == (
        "odds_event_1:bet365:alternate_totals_corners:OVER:9.5"
    )
    assert over["paper_entry_eligible"] is True


def test_stale_corner_candidate_not_paper_eligible(monkeypatch):
    prediction = _prediction()
    snapshots = [
        _corner_snapshot("pinnacle", "OVER", 1.95, line=9.5, reference=True, source_timestamp=STALE_SOURCE),
        _corner_snapshot("pinnacle", "UNDER", 1.91, line=9.5, reference=True, source_timestamp=STALE_SOURCE),
        _corner_snapshot("bet365", "OVER", 2.22, line=9.5, reference=False, source_timestamp=STALE_SOURCE),
        _corner_snapshot("bet365", "UNDER", 1.70, line=9.5, reference=False, source_timestamp=STALE_SOURCE),
    ]
    evaluation = _evaluate(
        monkeypatch,
        snapshots=snapshots,
        prediction=prediction,
        current_xg_connected=True,
    )

    over = _corner_row(evaluation, selection="OVER", line=9.5)
    assert over is not None
    assert over["decision"] == DECISION_PRELIMINARY_CANDIDATE
    assert over["market_freshness"] == "STALE"
    assert over["freshness_gate_passed"] is False
    assert over["paper_entry_eligible"] is False


def test_corner_total_settlement_supports_exact_lines_and_missing_results_gap():
    assert settlement_state_for_market(
        market="corner_total",
        selection="OVER",
        line=9.5,
        actual_home_goals=0,
        actual_away_goals=0,
        actual_total_corners=10,
    ) == "WON"
    assert settlement_state_for_market(
        market="corner_total",
        selection="UNDER",
        line=10.0,
        actual_home_goals=0,
        actual_away_goals=0,
        actual_total_corners=10,
    ) == "PUSH"
    with pytest.raises(ValueError):
        settlement_state_for_market(
            market="corner_total",
            selection="OVER",
            line=9.5,
            actual_home_goals=0,
            actual_away_goals=0,
            actual_total_corners=None,
        )


def test_corner_ledger_stores_exact_line_and_deduplicates(tmp_path):
    conn = connect(tmp_path / "paper.db")
    common = dict(
        prediction_id="pred_1",
        fixture_id=FIXTURE_ID,
        model_snapshot_id="snap_1",
        model_version="football_day10_market_pricing_v1",
        market="corner_total",
        model_fair_price=1.89,
        available_market_price=2.22,
        decision="PAPER_CANDIDATE",
        decision_timestamp=EVAL,
        kickoff=KICKOFF,
        safeguard_state="PASSED",
        entry_eligibility="PASSED",
        model_probability=0.56,
        edge=0.2432,
    )
    over_95 = PaperTradeEntry(**common, selection="OVER", line=9.5)
    over_105 = PaperTradeEntry(**common, selection="OVER", line=10.5)
    assert over_95.paper_trade_id != over_105.paper_trade_id

    record_paper_trade_entry(conn, over_95)
    record_paper_trade_entry(conn, over_105)

    found_95 = find_existing_paper_trade_entry(
        conn, prediction_id="pred_1", fixture_id=FIXTURE_ID, market="corner_total", selection="OVER", line=9.5
    )
    found_105 = find_existing_paper_trade_entry(
        conn, prediction_id="pred_1", fixture_id=FIXTURE_ID, market="corner_total", selection="OVER", line=10.5
    )
    assert found_95["paper_trade_id"] == over_95.paper_trade_id
    assert found_105["paper_trade_id"] == over_105.paper_trade_id
    assert found_95["line"] == 9.5
    assert found_105["line"] == 10.5

    again = find_existing_paper_trade_entry(
        conn, prediction_id="pred_1", fixture_id=FIXTURE_ID, market="corner_total", selection="OVER", line=9.5
    )
    assert again["paper_trade_id"] == over_95.paper_trade_id
