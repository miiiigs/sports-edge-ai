from __future__ import annotations

import pytest

from sports_ai.dashboard.current_market import build_fixture_market_evaluation
from sports_ai.dashboard.football_context_veto import (
    CONTEXT_BLOCKED,
    CONTEXT_FLAG,
    CONTEXT_VETO,
    REASON_EUROPE_CONGESTION,
    REASON_TWO_EXPECTED_STARTERS_MISSING,
)
from sports_ai.experiments.football_day11_manager_core import DECISION_PRELIMINARY_CANDIDATE
from sports_ai.market_data.providers import NormalizedMarketSnapshot

FIXTURE_ID = "football-data.org:context-1"
MODEL_AS_OF = "2026-09-12T20:00:00+00:00"
KICKOFF = "2026-09-13T14:00:00+00:00"
EVAL = "2026-09-13T09:00:00+00:00"
FRESH_SOURCE = "2026-09-13T08:50:00+00:00"

FIXTURE_CONTEXT = {
    "fixture_id": FIXTURE_ID,
    "competition_id": "ENG3",
    "competition_name": "League One",
    "division_code": "E2",
    "season": "2026",
    "home_team_name": "Home FC",
    "away_team_name": "Away FC",
    "kickoff_utc": KICKOFF,
}


def _snapshot(bookmaker: str, selection: str, odds: float, *, reference: bool) -> NormalizedMarketSnapshot:
    return NormalizedMarketSnapshot(
        provider="the-odds-api",
        fixture_id=FIXTURE_ID,
        provider_fixture_id="context-event-1",
        captured_at=EVAL,
        source_timestamp=FRESH_SOURCE,
        market="btts",
        selection=selection,
        line=None,
        decimal_odds=odds,
        bookmaker=bookmaker,
        ingestion_version="context_test_v1",
        provider_market_id=f"context-event-1:{bookmaker}:btts:{selection}",
        is_pre_match=True,
        home_team_name="Home FC",
        away_team_name="Away FC",
        competition_id="ENG3",
        competition_name="League One",
        kickoff_utc=KICKOFF,
        freshness_state="FRESH",
        reference_designation="reference" if reference else "available",
        reference_rank=0 if reference else None,
    )


def _snapshots() -> list[NormalizedMarketSnapshot]:
    return [
        _snapshot("pinnacle", "YES", 1.85, reference=True),
        _snapshot("pinnacle", "NO", 1.95, reference=True),
        _snapshot("bet365", "YES", 1.94, reference=False),
        _snapshot("bet365", "NO", 1.84, reference=False),
    ]


def _market_row(selection: str, win: float, loss: float, fair: float) -> dict:
    return {
        "as_of": MODEL_AS_OF,
        "competition_id": "ENG3",
        "competition_name": "League One",
        "division_code": "E2",
        "effective_loss_stake_probability": loss,
        "effective_win_stake_probability": win,
        "event_id": FIXTURE_ID,
        "fair_decimal_price": fair,
        "fixture_id": FIXTURE_ID,
        "kickoff_at": KICKOFF,
        "kickoff_time": "14:00",
        "line": None,
        "market_family": "btts",
        "match_date": "2026-09-13",
        "model_versions": {"conversion_model_version": "context_test_v1"},
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


def _prediction() -> dict:
    return {
        "prediction_status": "READY",
        "fixture_id": FIXTURE_ID,
        "competition_id": "ENG3",
        "competition_name": "League One",
        "home_team_name": "Home FC",
        "away_team_name": "Away FC",
        "kickoff_utc": KICKOFF,
        "model_as_of": MODEL_AS_OF,
        "model_snapshot_id": "snapshot-context-1",
        "expected_goals_home": 1.5,
        "expected_goals_away": 1.2,
        "market_rows": [
            _market_row("YES", 0.55, 0.45, 1.85),
            _market_row("NO", 0.45, 0.55, 2.05),
        ],
    }


def _evaluate(monkeypatch, *, context_provider=None, strict=False):
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    return build_fixture_market_evaluation(
        fixture_context=FIXTURE_CONTEXT,
        prediction=_prediction(),
        snapshots=_snapshots(),
        current_xg_connected=False,
        market_freshness_evaluated_at=EVAL,
        context_signals_provider=context_provider,
        strict_context_evidence=strict,
    )


def _yes_row(evaluation: dict) -> dict:
    return next(
        row
        for row in evaluation["decision_rows"]
        if row["market_family"] == "btts" and row["selection"] == "YES"
    )


def test_existing_behavior_is_unchanged_when_context_layer_is_not_connected(monkeypatch):
    row = _yes_row(_evaluate(monkeypatch))

    assert row["decision"] == DECISION_PRELIMINARY_CANDIDATE
    assert row["paper_entry_eligible"] is True
    assert row["context_status"] == "NOT_CONNECTED"
    assert row["context_reason_codes"] == []


def test_context_veto_kills_an_otherwise_eligible_candidate(monkeypatch):
    row = _yes_row(
        _evaluate(
            monkeypatch,
            context_provider=lambda _context: {"missing_expected_starters": 2},
        )
    )

    assert row["decision"] == DECISION_PRELIMINARY_CANDIDATE
    assert row["context_status"] == CONTEXT_VETO
    assert REASON_TWO_EXPECTED_STARTERS_MISSING in row["context_reason_codes"]
    assert row["paper_entry_eligible"] is False


def test_european_congestion_widens_threshold_without_repricing(monkeypatch):
    row = _yes_row(
        _evaluate(
            monkeypatch,
            context_provider=lambda _context: {"hours_since_european_fixture": 72},
        )
    )

    # ENG3 threshold is 6%. YES @ 1.94 yields ~6.7% settlement-aware edge:
    # enough for the manager candidate, but below the congestion-adjusted 7%.
    assert row["decision"] == DECISION_PRELIMINARY_CANDIDATE
    assert row["settlement_aware_edge"] == pytest.approx(0.067)
    assert row["competition_threshold"] == pytest.approx(0.06)
    assert row["context_status"] == CONTEXT_FLAG
    assert REASON_EUROPE_CONGESTION in row["context_reason_codes"]
    assert row["context_threshold_adjustment"] == pytest.approx(0.01)
    assert row["context_adjusted_threshold"] == pytest.approx(0.07)
    assert row["context_threshold_gate_passed"] is False
    assert row["model_probability"] == pytest.approx(0.55)
    assert row["paper_entry_eligible"] is False


def test_context_provider_failure_fails_closed(monkeypatch):
    def broken_provider(_context):
        raise RuntimeError("lineup provider unavailable")

    row = _yes_row(_evaluate(monkeypatch, context_provider=broken_provider))

    assert row["decision"] == DECISION_PRELIMINARY_CANDIDATE
    assert row["context_status"] == CONTEXT_BLOCKED
    assert row["paper_entry_eligible"] is False
    assert "lineup provider unavailable" in row["context_diagnostics"]["context_provider_error"]
