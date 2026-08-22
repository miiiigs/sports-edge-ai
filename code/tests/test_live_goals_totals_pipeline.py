"""Integration tests: live goals totals through the Day 10/11/12 paper pipeline.

Proves that provider ``totals`` snapshots normalize to ``goals_total`` and join
to Day 10 fair-price rows at exact lines, that Day 11 de-vigs the OVER/UNDER
pair, that freshness + Day 12 gate paper eligibility, and that the ledger and
settlement store exact lines without duplication or settlement drift.
"""

from __future__ import annotations

import httpx
import pytest

from sports_ai.dashboard.current_market import (
    build_fixture_market_evaluation,
    current_snapshot_to_day11_row,
)
from sports_ai.dashboard.day12_rolling_xg import SOURCE_IDENTIFIER, build_xg_inspection
from sports_ai.experiments.football_day11_manager_core import (
    DECISION_NO_BET,
    DECISION_PRELIMINARY_CANDIDATE,
    build_reference_market_booksum,
    canonical_market_identity,
)
from sports_ai.market_data.providers import NormalizedMarketSnapshot
from sports_ai.market_data.the_odds_api import TheOddsApiProvider
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


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _goals_snapshot(
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
        market="goals_total",
        selection=selection,
        line=line,
        decimal_odds=price,
        bookmaker=bookmaker,
        ingestion_version="the_odds_api_current_market_provider_v1",
        provider_market_id=f"odds_event_1:{bookmaker}:totals:{selection}:{line}",
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
        _goals_snapshot("pinnacle", "OVER", 1.95, line=2.5, reference=True, source_timestamp=source_timestamp),
        _goals_snapshot("pinnacle", "UNDER", 1.91, line=2.5, reference=True, source_timestamp=source_timestamp),
        _goals_snapshot("bet365", "OVER", 2.10, line=2.5, reference=False, source_timestamp=source_timestamp),
        _goals_snapshot("bet365", "UNDER", 1.80, line=2.5, reference=False, source_timestamp=source_timestamp),
        _goals_snapshot("pinnacle", "OVER", 1.80, line=3.5, reference=True, source_timestamp=source_timestamp),
        _goals_snapshot("pinnacle", "UNDER", 2.05, line=3.5, reference=True, source_timestamp=source_timestamp),
    ]


def _market_row(selection, win, loss, fair, *, competition_id="ENG1", competition_name="Premier League", line=2.5):
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
        "market_family": "goals_total",
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


def _prediction(*, competition_id="ENG1", competition_name="Premier League"):
    return {
        "prediction_status": "READY",
        "fixture_id": FIXTURE_ID,
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
            _market_row("OVER", 0.55, 0.45, 1.98, competition_id=competition_id, competition_name=competition_name),
            _market_row("UNDER", 0.45, 0.55, 2.05, competition_id=competition_id, competition_name=competition_name),
        ],
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


def _evaluate(monkeypatch, *, source_timestamp, competition_id="ENG1", competition_name="Premier League", current_xg_connected=False, xg=None):
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    kwargs = {}
    if current_xg_connected and xg is not None:
        kwargs["xg_profile_provider"] = lambda: xg
    return build_fixture_market_evaluation(
        fixture_context=dict(FIXTURE_CONTEXT, competition_id=competition_id, competition_name=competition_name),
        prediction=_prediction(competition_id=competition_id, competition_name=competition_name),
        snapshots=_snapshots(source_timestamp=source_timestamp),
        current_xg_connected=current_xg_connected,
        market_freshness_evaluated_at=EVAL,
        **kwargs,
    )


def _over_candidate(evaluation):
    for row in evaluation.get("decision_rows") or []:
        if row["market_family"] == "goals_total" and row["selection"] == "OVER" and row["line"] == 2.5:
            return row
    return None


# ---------------------------------------------------------------------------
# Provider totals -> Day 10 exact-line identity
# ---------------------------------------------------------------------------


def test_provider_totals_canonical_identity_matches_day10_row(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    event = {
        "id": "odds_event_1",
        "sport_key": "soccer_epl",
        "sport_title": "EPL",
        "commence_time": KICKOFF,
        "home_team": "Home FC",
        "away_team": "Away FC",
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": FRESH_SOURCE,
                "markets": [
                    {
                        "key": "totals",
                        "last_update": FRESH_SOURCE,
                        "outcomes": [
                            {"name": "Over", "price": 1.95, "point": 2.5},
                            {"name": "Under", "price": 1.91, "point": 2.5},
                        ],
                    },
                ],
            }
        ],
    }
    headers = {"x-requests-remaining": "495", "x-requests-used": "5", "x-requests-last": "1"}
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=[event], headers=headers, request=request)
    )
    provider = TheOddsApiProvider(client=httpx.Client(transport=transport))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)

    over = [s for s in snapshots if s.market == "goals_total" and s.selection == "OVER"]
    assert len(over) == 1
    assert over[0].line == 2.5

    row = current_snapshot_to_day11_row(over[0], fixture_context=FIXTURE_CONTEXT)
    assert row["market_family"] == "goals_total"
    assert row["normalized_line"] == 2.5
    assert row["normalized_selection"] == "OVER"
    assert row["canonical_market_identity"] == canonical_market_identity(
        fixture_id=FIXTURE_ID,
        market_family="goals_total",
        normalized_line=2.5,
        normalized_selection="OVER",
    )


# ---------------------------------------------------------------------------
# Day 11 de-vig and exact-line intersection
# ---------------------------------------------------------------------------


def test_day11_devigs_goals_over_under_pair(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    rows = [current_snapshot_to_day11_row(s, fixture_context=FIXTURE_CONTEXT) for s in _snapshots(source_timestamp=FRESH_SOURCE)]
    pinnacle_25 = [r for r in rows if r["bookmaker"] == "pinnacle" and r["normalized_line"] == 2.5]
    reference = build_reference_market_booksum(pinnacle_25, final_method="shin")
    assert reference["market_family"] == "goals_total"
    assert reference["normalized_line"] == 2.5
    assert abs(reference["booksum"] - 1.0) < 0.05
    assert set(reference["devigged_probabilities_by_selection"]) == {"OVER", "UNDER"}


def test_day11_evaluates_over_and_under_at_exact_line(monkeypatch):
    evaluation = _evaluate(monkeypatch, source_timestamp=FRESH_SOURCE)
    rows_by_selection = {
        row["selection"]: row for row in evaluation["decision_rows"] if row["line"] == 2.5
    }
    assert rows_by_selection["OVER"]["decision"] == DECISION_PRELIMINARY_CANDIDATE
    assert rows_by_selection["UNDER"]["decision"] == DECISION_NO_BET
    assert rows_by_selection["OVER"]["best_available_bookmaker"] == "bet365"
    assert rows_by_selection["OVER"]["best_available_price"] == pytest.approx(2.10)


def test_no_nearest_line_matching(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    prediction = _prediction()
    prediction["market_rows"] = prediction["market_rows"] + [
        _market_row("OVER", 0.40, 0.60, 2.10, line=3.0),
        _market_row("UNDER", 0.60, 0.40, 1.85, line=3.0),
    ]
    evaluation = build_fixture_market_evaluation(
        fixture_context=FIXTURE_CONTEXT,
        prediction=prediction,
        snapshots=_snapshots(source_timestamp=FRESH_SOURCE),
        current_xg_connected=False,
        market_freshness_evaluated_at=EVAL,
    )
    # Day 10 has 3.0; the provider only offers 2.5 and 3.5. No interpolation:
    # the 3.0 model rows must not be compared against the 3.5 provider line.
    lines = sorted({row["line"] for row in evaluation["decision_rows"]})
    assert lines == [2.5]


# ---------------------------------------------------------------------------
# Freshness / Day 12 eligibility
# ---------------------------------------------------------------------------


def test_fresh_goals_candidate_day12_pass_eligible(monkeypatch):
    rows, inspection = _valid_xg_rows(FIXTURE_ID, _prediction())
    evaluation = _evaluate(
        monkeypatch,
        source_timestamp=FRESH_SOURCE,
        competition_id="ENG1",
        current_xg_connected=True,
        xg=(rows, inspection),
    )
    over = _over_candidate(evaluation)
    assert over is not None
    assert over["decision"] == DECISION_PRELIMINARY_CANDIDATE
    assert over["market_freshness"] == "FRESH"
    assert over["freshness_gate_passed"] is True
    assert over["day12_state"] == "PASSED"
    assert over["paper_entry_eligible"] is True


def test_stale_goals_candidate_not_paper_eligible(monkeypatch):
    evaluation = _evaluate(monkeypatch, source_timestamp=STALE_SOURCE)
    over = _over_candidate(evaluation)
    assert over is not None
    assert over["decision"] == DECISION_PRELIMINARY_CANDIDATE
    assert over["market_freshness"] == "STALE"
    assert over["freshness_gate_passed"] is False
    assert over["paper_entry_eligible"] is False


def test_fresh_goals_candidate_blocked_when_day12_missing(monkeypatch):
    # ENG1 is xG-applicable but xG is not connected: Day 12 blocks, even fresh.
    evaluation = _evaluate(monkeypatch, source_timestamp=FRESH_SOURCE, competition_id="ENG1")
    over = _over_candidate(evaluation)
    assert over is not None
    assert over["decision"] == DECISION_PRELIMINARY_CANDIDATE
    assert over["market_freshness"] == "FRESH"
    assert over["paper_entry_eligible"] is False


# ---------------------------------------------------------------------------
# Ledger + settlement
# ---------------------------------------------------------------------------


def test_goals_total_settlement_win_loss_push():
    assert settlement_state_for_market(
        market="goals_total", selection="OVER", line=2.5, actual_home_goals=2, actual_away_goals=1
    ) == "WON"
    assert settlement_state_for_market(
        market="goals_total", selection="UNDER", line=2.5, actual_home_goals=2, actual_away_goals=1
    ) == "LOST"
    assert settlement_state_for_market(
        market="goals_total", selection="OVER", line=3.0, actual_home_goals=2, actual_away_goals=1
    ) == "PUSH"
    assert settlement_state_for_market(
        market="goals_total", selection="UNDER", line=3.0, actual_home_goals=2, actual_away_goals=1
    ) == "PUSH"


def test_goals_total_settlement_rejects_missing_line():
    with pytest.raises(ValueError):
        settlement_state_for_market(
            market="goals_total", selection="OVER", line=None, actual_home_goals=2, actual_away_goals=1
        )


def test_ledger_stores_exact_line_and_deduplicates(tmp_path):
    conn = connect(tmp_path / "paper.db")
    common = dict(
        prediction_id="pred_1",
        fixture_id=FIXTURE_ID,
        model_snapshot_id="snap_1",
        model_version="football_day10_market_pricing_v1",
        market="goals_total",
        model_fair_price=1.98,
        available_market_price=2.10,
        decision="PAPER_CANDIDATE",
        decision_timestamp=EVAL,
        kickoff=KICKOFF,
        safeguard_state="PASSED",
        entry_eligibility="PASSED",
        model_probability=0.55,
        edge=0.155,
    )
    over_25 = PaperTradeEntry(**common, selection="OVER", line=2.5)
    over_35 = PaperTradeEntry(**common, selection="OVER", line=3.5)
    assert over_25.paper_trade_id != over_35.paper_trade_id

    record_paper_trade_entry(conn, over_25)
    record_paper_trade_entry(conn, over_35)

    found_25 = find_existing_paper_trade_entry(
        conn, prediction_id="pred_1", fixture_id=FIXTURE_ID, market="goals_total", selection="OVER", line=2.5
    )
    found_35 = find_existing_paper_trade_entry(
        conn, prediction_id="pred_1", fixture_id=FIXTURE_ID, market="goals_total", selection="OVER", line=3.5
    )
    assert found_25["paper_trade_id"] == over_25.paper_trade_id
    assert found_35["paper_trade_id"] == over_35.paper_trade_id
    assert found_25["line"] == 2.5
    assert found_35["line"] == 3.5

    # A later checkpoint of the same logical position resolves to the same row.
    again = find_existing_paper_trade_entry(
        conn, prediction_id="pred_1", fixture_id=FIXTURE_ID, market="goals_total", selection="OVER", line=2.5
    )
    assert again["paper_trade_id"] == over_25.paper_trade_id
