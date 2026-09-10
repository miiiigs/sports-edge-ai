"""Integration tests: live Asian Handicap through the Day 10/11/12 paper pipeline."""

from __future__ import annotations

import pytest

from sports_ai.dashboard.current_market import (
    build_fixture_market_evaluation,
    current_snapshot_to_day11_row,
)
from sports_ai.dashboard.day12_rolling_xg import SOURCE_IDENTIFIER, build_xg_inspection
from sports_ai.experiments.football_day11_manager_core import (
    DECISION_MISSING_REQUIRED_MARKET_DATA,
    DECISION_NO_BET,
    DECISION_PRELIMINARY_CANDIDATE,
    REASON_INCOMPLETE_REFERENCE_MARKET,
    build_reference_market_booksum,
    canonical_market_identity,
    collapse_latest_bookmaker_quotes,
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


def _ah_snapshot(
    bookmaker,
    selection,
    price,
    *,
    line,
    reference,
    source_timestamp,
    captured_at=EVAL,
    freshness="FRESH",
):
    return NormalizedMarketSnapshot(
        provider="the-odds-api",
        fixture_id=FIXTURE_ID,
        provider_fixture_id="odds_event_1",
        captured_at=captured_at,
        source_timestamp=source_timestamp,
        market="asian_handicap",
        selection=selection,
        line=line,
        decimal_odds=price,
        bookmaker=bookmaker,
        ingestion_version="the_odds_api_current_market_provider_v1",
        provider_market_id=f"odds_event_1:{bookmaker}:spreads:{selection}:{line}",
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


def _snapshots(*, source_timestamp):
    return [
        _ah_snapshot("pinnacle", "HOME", 1.95, line=-0.5, reference=True, source_timestamp=source_timestamp),
        _ah_snapshot("pinnacle", "AWAY", 1.91, line=-0.5, reference=True, source_timestamp=source_timestamp),
        _ah_snapshot("bet365", "HOME", 2.18, line=-0.5, reference=False, source_timestamp=source_timestamp),
        _ah_snapshot("bet365", "AWAY", 1.80, line=-0.5, reference=False, source_timestamp=source_timestamp),
        _ah_snapshot("pinnacle", "HOME", 2.25, line=-0.75, reference=True, source_timestamp=source_timestamp),
        _ah_snapshot("pinnacle", "AWAY", 1.72, line=-0.75, reference=True, source_timestamp=source_timestamp),
    ]


def _market_row(
    selection,
    win,
    loss,
    fair,
    *,
    line,
    competition_id="ENG1",
    competition_name="Premier League",
):
    return {
        "as_of": MODEL_AS_OF,
        "competition_id": competition_id,
        "competition_name": competition_name,
        "division_code": "E0",
        "effective_loss_stake_probability": loss,
        "effective_win_stake_probability": win,
        "event_id": FIXTURE_ID,
        "fair_decimal_price": fair,
        "fixture_id": FIXTURE_ID,
        "kickoff_at": KICKOFF,
        "kickoff_time": "14:00",
        "line": line,
        "market_family": "asian_handicap",
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
        "source_scoreline_object_id": f"scoreline_{FIXTURE_ID}",
    }


def _prediction(*, current_xg=False):
    rows = [
        _market_row("HOME", 0.55, 0.45, 1.95, line=-0.5),
        _market_row("AWAY", 0.45, 0.55, 2.20, line=-0.5),
    ]
    if current_xg:
        rows.extend(
            [
                _market_row("HOME", 0.37, 0.63, 2.80, line=-0.75),
                _market_row("AWAY", 0.63, 0.37, 1.60, line=-0.75),
            ]
        )
    return {
        "prediction_status": "READY",
        "fixture_id": FIXTURE_ID,
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "home_team_name": "Home FC",
        "away_team_name": "Away FC",
        "kickoff_utc": KICKOFF,
        "model_as_of": MODEL_AS_OF,
        "model_snapshot_id": "snapshot_1",
        "expected_goals_home": 1.6,
        "expected_goals_away": 1.2,
        "expected_total_goals": 2.8,
        "market_rows": rows,
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


def _evaluate(monkeypatch, *, source_timestamp, snapshots=None, prediction=None, current_xg_connected=False):
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    prediction = _prediction() if prediction is None else prediction
    kwargs = {}
    if current_xg_connected:
        rows, inspection = _valid_xg_rows(prediction)
        kwargs["xg_profile_provider"] = lambda: (rows, inspection)
    return build_fixture_market_evaluation(
        fixture_context=FIXTURE_CONTEXT,
        prediction=prediction,
        snapshots=_snapshots(source_timestamp=source_timestamp) if snapshots is None else snapshots,
        current_xg_connected=current_xg_connected,
        market_freshness_evaluated_at=EVAL,
        **kwargs,
    )


def _candidate(evaluation, *, selection, line):
    for row in evaluation.get("decision_rows") or []:
        if row["market_family"] == "asian_handicap" and row["selection"] == selection and row["line"] == line:
            return row
    return None


def _line_minus_one_prediction():
    prediction = _prediction()
    prediction["market_rows"].extend(
        [
            _market_row("HOME", 0.34, 0.66, 2.94, line=-1.0),
            _market_row("AWAY", 0.66, 0.34, 1.52, line=-1.0),
        ]
    )
    return prediction


def _line_minus_one_reference_rows():
    return [
        _ah_snapshot("pinnacle", "HOME", 1.38, line=-1.0, reference=True, source_timestamp=FRESH_SOURCE),
        _ah_snapshot("pinnacle", "AWAY", 3.18, line=-1.0, reference=True, source_timestamp=FRESH_SOURCE),
    ]


def test_provider_ah_canonical_identity_matches_day10_row():
    snapshot = _ah_snapshot(
        "pinnacle",
        "HOME",
        1.95,
        line=-0.5,
        reference=True,
        source_timestamp=FRESH_SOURCE,
    )
    row = current_snapshot_to_day11_row(snapshot, fixture_context=FIXTURE_CONTEXT)
    assert row["market_family"] == "asian_handicap"
    assert row["normalized_line"] == -0.5
    assert row["normalized_selection"] == "HOME"
    assert row["canonical_market_identity"] == canonical_market_identity(
        fixture_id=FIXTURE_ID,
        market_family="asian_handicap",
        normalized_line=-0.5,
        normalized_selection="HOME",
    )


def test_day11_devigs_home_away_reference_pair_at_exact_home_handicap_line(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    rows = [current_snapshot_to_day11_row(s, fixture_context=FIXTURE_CONTEXT) for s in _snapshots(source_timestamp=FRESH_SOURCE)]
    pinnacle = [r for r in rows if r["bookmaker"] == "pinnacle" and r["normalized_line"] == -0.5]
    reference = build_reference_market_booksum(pinnacle, final_method="shin")
    assert reference["market_family"] == "asian_handicap"
    assert reference["normalized_line"] == -0.5
    assert abs(reference["booksum"] - 1.0) < 0.06
    assert set(reference["devigged_probabilities_by_selection"]) == {"HOME", "AWAY"}


def test_incomplete_reference_pair_fails_closed(monkeypatch):
    prediction = _prediction()
    snapshots = [
        _ah_snapshot("pinnacle", "HOME", 1.95, line=-0.5, reference=True, source_timestamp=FRESH_SOURCE),
        _ah_snapshot("bet365", "HOME", 2.18, line=-0.5, reference=False, source_timestamp=FRESH_SOURCE),
        _ah_snapshot("bet365", "AWAY", 1.80, line=-0.5, reference=False, source_timestamp=FRESH_SOURCE),
    ]
    evaluation = _evaluate(monkeypatch, source_timestamp=FRESH_SOURCE, prediction=prediction, snapshots=snapshots)
    home = _candidate(evaluation, selection="HOME", line=-0.5)
    away = _candidate(evaluation, selection="AWAY", line=-0.5)
    assert home is not None
    assert away is not None
    assert home["decision"] == DECISION_MISSING_REQUIRED_MARKET_DATA
    assert away["decision"] == DECISION_MISSING_REQUIRED_MARKET_DATA
    assert home["reason_codes"] == [REASON_INCOMPLETE_REFERENCE_MARKET]
    assert away["reason_codes"] == [REASON_INCOMPLETE_REFERENCE_MARKET]
    assert home["paper_entry_eligible"] is False
    assert away["paper_entry_eligible"] is False


def test_day11_evaluates_exact_ah_line_and_keeps_reference_separate_from_entry(monkeypatch):
    rows = [current_snapshot_to_day11_row(s, fixture_context=FIXTURE_CONTEXT) for s in _snapshots(source_timestamp=FRESH_SOURCE)]
    reference = build_reference_market_booksum(
        [row for row in rows if row["bookmaker"] == "pinnacle" and row["normalized_line"] == -0.5],
        final_method="shin",
    )
    evaluation = _evaluate(monkeypatch, source_timestamp=FRESH_SOURCE, current_xg_connected=True)
    home = _candidate(evaluation, selection="HOME", line=-0.5)
    away = _candidate(evaluation, selection="AWAY", line=-0.5)
    assert home is not None
    assert away is not None
    assert home["decision"] == DECISION_PRELIMINARY_CANDIDATE
    assert away["decision"] == DECISION_NO_BET
    assert home["best_available_bookmaker"] == "bet365"
    assert home["best_available_price"] == pytest.approx(2.18)
    assert home["best_available_provider_market_id"] == "odds_event_1:bet365:spreads:HOME:-0.5"
    assert home["paper_entry_eligible"] is True
    assert reference["reference_bookmaker"] == "pinnacle"
    assert reference["raw_implied_probabilities"]["HOME"] == pytest.approx(1 / 1.95)
    assert reference["raw_implied_probabilities"]["AWAY"] == pytest.approx(1 / 1.91)


def test_current_ah_uses_latest_same_bookmaker_quote_when_price_decreases(monkeypatch):
    snapshots = [
        *_line_minus_one_reference_rows(),
        _ah_snapshot(
            "Betano",
            "AWAY",
            3.25,
            line=-1.0,
            reference=False,
            source_timestamp="2026-08-23T08:40:00+00:00",
            captured_at="2026-08-23T08:45:00+00:00",
        ),
        _ah_snapshot(
            "Betano",
            "AWAY",
            3.15,
            line=-1.0,
            reference=False,
            source_timestamp="2026-08-23T08:50:00+00:00",
            captured_at="2026-08-23T08:55:00+00:00",
        ),
    ]

    evaluation = _evaluate(monkeypatch, source_timestamp=FRESH_SOURCE, prediction=_line_minus_one_prediction(), snapshots=snapshots)
    away = _candidate(evaluation, selection="AWAY", line=-1.0)

    assert away is not None
    assert away["best_available_bookmaker"] == "Betano"
    assert away["best_available_price"] == pytest.approx(3.15)
    assert away["best_available_source_timestamp"] == "2026-08-23T08:50:00+00:00"


def test_current_ah_uses_latest_same_bookmaker_quote_when_price_increases(monkeypatch):
    snapshots = [
        *_line_minus_one_reference_rows(),
        _ah_snapshot("Betano", "AWAY", 3.15, line=-1.0, reference=False, source_timestamp="2026-08-23T08:40:00+00:00"),
        _ah_snapshot("Betano", "AWAY", 3.25, line=-1.0, reference=False, source_timestamp="2026-08-23T08:50:00+00:00"),
    ]

    evaluation = _evaluate(monkeypatch, source_timestamp=FRESH_SOURCE, prediction=_line_minus_one_prediction(), snapshots=snapshots)
    away = _candidate(evaluation, selection="AWAY", line=-1.0)

    assert away is not None
    assert away["best_available_bookmaker"] == "Betano"
    assert away["best_available_price"] == pytest.approx(3.25)


def test_current_ah_chooses_best_price_after_bookmaker_quote_collapse(monkeypatch):
    snapshots = [
        *_line_minus_one_reference_rows(),
        _ah_snapshot("Betano", "AWAY", 3.25, line=-1.0, reference=False, source_timestamp="2026-08-23T08:40:00+00:00"),
        _ah_snapshot("Betano", "AWAY", 3.15, line=-1.0, reference=False, source_timestamp="2026-08-23T08:50:00+00:00"),
        _ah_snapshot("Bookmaker B", "AWAY", 3.20, line=-1.0, reference=False, source_timestamp="2026-08-23T08:45:00+00:00"),
    ]

    evaluation = _evaluate(monkeypatch, source_timestamp=FRESH_SOURCE, prediction=_line_minus_one_prediction(), snapshots=snapshots)
    away = _candidate(evaluation, selection="AWAY", line=-1.0)

    assert away is not None
    assert away["best_available_bookmaker"] == "Bookmaker B"
    assert away["best_available_price"] == pytest.approx(3.20)


def test_current_ah_old_high_quote_cannot_win_inside_fresh_window(monkeypatch):
    snapshots = [
        *_line_minus_one_reference_rows(),
        _ah_snapshot("Betano", "AWAY", 4.00, line=-1.0, reference=False, source_timestamp="2026-08-23T08:40:00+00:00"),
        _ah_snapshot("Betano", "AWAY", 3.15, line=-1.0, reference=False, source_timestamp="2026-08-23T08:50:00+00:00"),
    ]

    evaluation = _evaluate(monkeypatch, source_timestamp=FRESH_SOURCE, prediction=_line_minus_one_prediction(), snapshots=snapshots)
    away = _candidate(evaluation, selection="AWAY", line=-1.0)

    assert away is not None
    assert away["best_available_price"] == pytest.approx(3.15)


def test_reference_bookmaker_uses_latest_superseding_quotes_for_devig():
    rows = [
        current_snapshot_to_day11_row(
            _ah_snapshot("pinnacle", "HOME", 1.50, line=-1.0, reference=True, source_timestamp="2026-08-23T08:40:00+00:00"),
            fixture_context=FIXTURE_CONTEXT,
        ),
        current_snapshot_to_day11_row(
            _ah_snapshot("pinnacle", "AWAY", 2.80, line=-1.0, reference=True, source_timestamp="2026-08-23T08:40:00+00:00"),
            fixture_context=FIXTURE_CONTEXT,
        ),
        current_snapshot_to_day11_row(
            _ah_snapshot("pinnacle", "HOME", 1.38, line=-1.0, reference=True, source_timestamp="2026-08-23T08:50:00+00:00"),
            fixture_context=FIXTURE_CONTEXT,
        ),
        current_snapshot_to_day11_row(
            _ah_snapshot("pinnacle", "AWAY", 3.18, line=-1.0, reference=True, source_timestamp="2026-08-23T08:50:00+00:00"),
            fixture_context=FIXTURE_CONTEXT,
        ),
    ]

    reference = build_reference_market_booksum(collapse_latest_bookmaker_quotes(rows), final_method="shin")

    assert reference["reference_bookmaker"] == "pinnacle"
    assert reference["market_as_of"] == "2026-08-23T08:50:00+00:00"
    assert reference["raw_implied_probabilities"]["HOME"] == pytest.approx(1 / 1.38)
    assert reference["raw_implied_probabilities"]["AWAY"] == pytest.approx(1 / 3.18)


def test_selection_time_truth_remains_separate_from_current_ah_quote(monkeypatch):
    historical_selection = {
        "bookmaker": "Betano",
        "selection_time_odds": 3.25,
        "edge_at_selection": 1.30,
    }
    snapshots = [
        *_line_minus_one_reference_rows(),
        _ah_snapshot("Betano", "AWAY", 3.15, line=-1.0, reference=False, source_timestamp=FRESH_SOURCE),
    ]

    evaluation = _evaluate(monkeypatch, source_timestamp=FRESH_SOURCE, prediction=_line_minus_one_prediction(), snapshots=snapshots)
    away = _candidate(evaluation, selection="AWAY", line=-1.0)

    assert away is not None
    assert historical_selection == {
        "bookmaker": "Betano",
        "selection_time_odds": 3.25,
        "edge_at_selection": 1.30,
    }
    assert away["best_available_price"] == pytest.approx(3.15)
    assert away["settlement_aware_edge"] != pytest.approx(historical_selection["edge_at_selection"])


def test_no_cross_match_between_different_ah_lines(monkeypatch):
    prediction = _prediction()
    prediction["market_rows"].extend(
        [
            _market_row("HOME", 0.34, 0.66, 2.94, line=-1.0),
            _market_row("AWAY", 0.66, 0.34, 1.52, line=-1.0),
        ]
    )
    evaluation = _evaluate(monkeypatch, source_timestamp=FRESH_SOURCE, prediction=prediction, current_xg_connected=True)
    lines = sorted({row["line"] for row in evaluation["decision_rows"]})
    assert lines == [-0.5]
    assert _candidate(evaluation, selection="HOME", line=-1.0) is None
    assert _candidate(evaluation, selection="AWAY", line=-1.0) is None


def test_stale_ah_market_cannot_qualify(monkeypatch):
    evaluation = _evaluate(monkeypatch, source_timestamp=STALE_SOURCE)
    home = _candidate(evaluation, selection="HOME", line=-0.5)
    assert home is not None
    assert home["decision"] == DECISION_PRELIMINARY_CANDIDATE
    assert home["market_freshness"] == "STALE"
    assert home["freshness_gate_passed"] is False
    assert home["paper_entry_eligible"] is False


def test_ledger_stores_exact_ah_line_and_deduplicates(tmp_path):
    conn = connect(tmp_path / "paper.db")
    common = dict(
        prediction_id="pred_ah",
        fixture_id=FIXTURE_ID,
        model_snapshot_id="snap_ah",
        model_version="football_day10_market_pricing_v1",
        market="asian_handicap",
        model_fair_price=1.95,
        available_market_price=2.18,
        decision="PAPER_CANDIDATE",
        decision_timestamp=EVAL,
        kickoff=KICKOFF,
        safeguard_state="PASSED",
        entry_eligibility="PASSED",
        model_probability=0.55,
        edge=0.149,
    )
    home_minus_half = PaperTradeEntry(**common, selection="HOME", line=-0.5)
    home_minus_three_quarter = PaperTradeEntry(**common, selection="HOME", line=-0.75)
    record_paper_trade_entry(conn, home_minus_half)
    record_paper_trade_entry(conn, home_minus_three_quarter)
    found_half = find_existing_paper_trade_entry(
        conn, prediction_id="pred_ah", fixture_id=FIXTURE_ID, market="asian_handicap", selection="HOME", line=-0.5
    )
    found_three_quarter = find_existing_paper_trade_entry(
        conn, prediction_id="pred_ah", fixture_id=FIXTURE_ID, market="asian_handicap", selection="HOME", line=-0.75
    )
    assert found_half["paper_trade_id"] == home_minus_half.paper_trade_id
    assert found_three_quarter["paper_trade_id"] == home_minus_three_quarter.paper_trade_id
    assert found_half["line"] == -0.5
    assert found_three_quarter["line"] == -0.75


@pytest.mark.parametrize(
    ("selection", "line", "home_goals", "away_goals", "expected_state"),
    [
        ("HOME", -0.5, 1, 0, "WON"),
        ("HOME", 0.5, 0, 0, "WON"),
        ("HOME", -1.0, 1, 0, "PUSH"),
        ("AWAY", -1.0, 0, 0, "WON"),
        ("HOME", -0.25, 1, 1, "HALF_LOSS"),
        ("HOME", 0.25, 1, 1, "HALF_WIN"),
        ("AWAY", 0.75, 0, 1, "HALF_WIN"),
    ],
)
def test_ah_settlement_preserves_full_push_and_half_outcomes(selection, line, home_goals, away_goals, expected_state):
    assert (
        settlement_state_for_market(
            market="asian_handicap",
            selection=selection,
            line=line,
            actual_home_goals=home_goals,
            actual_away_goals=away_goals,
        )
        == expected_state
    )
