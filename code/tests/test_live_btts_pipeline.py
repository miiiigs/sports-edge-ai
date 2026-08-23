"""Integration tests: live BTTS through the Day 10/11/12 paper pipeline."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from sports_ai.dashboard import storage as dashboard_storage
from sports_ai.dashboard.current_market import (
    build_fixture_market_evaluation,
    current_snapshot_to_day11_row,
)
from sports_ai.dashboard.day12_rolling_xg import SOURCE_IDENTIFIER, build_xg_inspection
from sports_ai.dashboard.models import FIXTURE_STATUS_PRE_MATCH, NormalizedFixture
from sports_ai.dashboard.services import DashboardConfig, DashboardServices
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


def _btts_snapshot(bookmaker, selection, price, *, reference, source_timestamp):
    return NormalizedMarketSnapshot(
        provider="the-odds-api",
        fixture_id=FIXTURE_ID,
        provider_fixture_id="odds_event_1",
        captured_at=EVAL,
        source_timestamp=source_timestamp,
        market="btts",
        selection=selection,
        line=None,
        decimal_odds=price,
        bookmaker=bookmaker,
        ingestion_version="the_odds_api_current_market_provider_v1",
        provider_market_id=f"odds_event_1:{bookmaker}:btts:{selection}",
        is_pre_match=True,
        home_team_name="Home FC",
        away_team_name="Away FC",
        competition_id="ENG1",
        competition_name="Premier League",
        kickoff_utc=KICKOFF,
        freshness_state="FRESH",
        reference_designation="reference" if reference else "available",
        reference_rank=0 if reference else None,
    )


def _snapshots(*, source_timestamp):
    return [
        _btts_snapshot("pinnacle", "YES", 1.85, reference=True, source_timestamp=source_timestamp),
        _btts_snapshot("pinnacle", "NO", 1.95, reference=True, source_timestamp=source_timestamp),
        _btts_snapshot("bet365", "YES", 2.00, reference=False, source_timestamp=source_timestamp),
        _btts_snapshot("bet365", "NO", 1.80, reference=False, source_timestamp=source_timestamp),
    ]


def _market_row(selection, win, loss, fair, *, competition_id="ENG1", competition_name="Premier League"):
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
        "line": None,
        "market_family": "btts",
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
            _market_row("YES", 0.55, 0.45, 1.85, competition_id=competition_id, competition_name=competition_name),
            _market_row("NO", 0.45, 0.55, 2.05, competition_id=competition_id, competition_name=competition_name),
        ],
    }


def _fixture():
    kickoff_utc = (
        datetime.now(timezone.utc).replace(hour=20, minute=0, second=0, microsecond=0) + timedelta(days=2)
    ).isoformat()
    return NormalizedFixture(
        fixture_id=FIXTURE_ID,
        provider_fixture_id="fixture-1",
        provider="fake-provider",
        competition_id="ENG1",
        competition_name="Premier League",
        division_code="E0",
        season="2026",
        home_team_id="100",
        home_team_name="Home FC",
        away_team_id="200",
        away_team_name="Away FC",
        kickoff_utc=kickoff_utc,
        status=FIXTURE_STATUS_PRE_MATCH,
        elapsed_minute=None,
        home_score=None,
        away_score=None,
        last_provider_update=KICKOFF,
        source_version="fake_fixture_provider_v1",
        provider_payload={"fixture_id": FIXTURE_ID},
    )


def test_provider_btts_canonical_identity_matches_day10_row(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    event = {
        "id": "odds_event_1",
        "sport_key": "soccer_epl",
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
                        "key": "btts",
                        "last_update": FRESH_SOURCE,
                        "outcomes": [
                            {"name": "Yes", "price": 1.85},
                            {"name": "No", "price": 1.95},
                        ],
                    }
                ],
            }
        ],
    }
    headers = {"x-requests-remaining": "495", "x-requests-used": "5", "x-requests-last": "1"}
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=[{"id": "odds_event_1", "commence_time": KICKOFF, "home_team": "Home FC", "away_team": "Away FC", "bookmakers": []}], headers=headers, request=request)
        if request.url.path.endswith("/sports/soccer_epl/odds")
        else httpx.Response(200, json=event, headers=headers, request=request)
    )
    provider = TheOddsApiProvider(client=httpx.Client(transport=transport))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)

    yes = [s for s in snapshots if s.market == "btts" and s.selection == "YES"]
    assert len(yes) == 1
    row = current_snapshot_to_day11_row(yes[0], fixture_context=FIXTURE_CONTEXT)
    assert row["market_family"] == "btts"
    assert row["normalized_line"] is None
    assert row["normalized_selection"] == "YES"
    assert row["canonical_market_identity"] == canonical_market_identity(
        fixture_id=FIXTURE_ID, market_family="btts", normalized_line=None, normalized_selection="YES"
    )


def test_day11_devigs_btts_yes_no_pair(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    rows = [current_snapshot_to_day11_row(s, fixture_context=FIXTURE_CONTEXT) for s in _snapshots(source_timestamp=FRESH_SOURCE)]
    pinnacle = [r for r in rows if r["bookmaker"] == "pinnacle"]
    reference = build_reference_market_booksum(pinnacle, final_method="shin")
    assert reference["market_family"] == "btts"
    assert reference["normalized_line"] is None
    assert abs(reference["booksum"] - 1.0) < 0.06
    assert set(reference["devigged_probabilities_by_selection"]) == {"YES", "NO"}


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


def _yes_candidate(evaluation):
    for row in evaluation.get("decision_rows") or []:
        if row["market_family"] == "btts" and row["selection"] == "YES":
            return row
    return None


def test_btts_day11_positive_edge_candidate(monkeypatch):
    evaluation = _evaluate(monkeypatch, source_timestamp=FRESH_SOURCE)
    yes = _yes_candidate(evaluation)
    assert yes is not None
    # YES @ bet365 2.00 vs model win prob 0.55 -> edge = 0.55*1.0 - 0.45 = 0.10 > 0.05
    assert yes["decision"] == DECISION_PRELIMINARY_CANDIDATE
    assert yes["best_available_bookmaker"] == "bet365"
    assert yes["line"] is None


def test_btts_stale_not_paper_eligible(monkeypatch):
    evaluation = _evaluate(monkeypatch, source_timestamp=STALE_SOURCE)
    yes = _yes_candidate(evaluation)
    assert yes is not None
    assert yes["market_freshness"] == "STALE"
    assert yes["paper_entry_eligible"] is False


def test_btts_fresh_day12_pass_eligible(monkeypatch):
    rows = {
        (FIXTURE_ID, "HOME"): {
            "fixture_id": FIXTURE_ID,
            "team_role": "HOME",
            "recent_xg_for_per_match": 1.6,
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
            "recent_xg_for_per_match": 1.2,
            "matches_in_window": 5,
            "as_of": "2026-05-24T20:00:00+00:00",
            "window_start_at": "2026-04-01T20:00:00+00:00",
            "window_end_at": "2026-05-24T20:00:00+00:00",
            "source": SOURCE_IDENTIFIER,
            "competition_id": "ENG1",
        },
    }
    inspection = build_xg_inspection(rows)
    evaluation = _evaluate(monkeypatch, source_timestamp=FRESH_SOURCE, current_xg_connected=True, xg=(rows, inspection))
    yes = _yes_candidate(evaluation)
    assert yes is not None
    assert yes["market_freshness"] == "FRESH"
    assert yes["day12_state"] == "PASSED"
    assert yes["paper_entry_eligible"] is True


def test_btts_settlement():
    assert settlement_state_for_market(market="btts", selection="YES", line=None, actual_home_goals=1, actual_away_goals=1) == "WON"
    assert settlement_state_for_market(market="btts", selection="NO", line=None, actual_home_goals=1, actual_away_goals=1) == "LOST"
    assert settlement_state_for_market(market="btts", selection="YES", line=None, actual_home_goals=0, actual_away_goals=0) == "LOST"
    assert settlement_state_for_market(market="btts", selection="NO", line=None, actual_home_goals=0, actual_away_goals=0) == "WON"
    assert settlement_state_for_market(market="btts", selection="YES", line=None, actual_home_goals=1, actual_away_goals=0) == "LOST"
    assert settlement_state_for_market(market="btts", selection="NO", line=None, actual_home_goals=2, actual_away_goals=1) == "LOST"


def test_btts_ledger_identity_and_dedup(tmp_path: Path):
    conn = connect(tmp_path / "paper.db")
    common = dict(
        prediction_id="pred_1",
        fixture_id=FIXTURE_ID,
        model_snapshot_id="snap_1",
        model_version="football_day10_market_pricing_v1",
        market="btts",
        model_fair_price=1.85,
        available_market_price=2.00,
        decision="PAPER_CANDIDATE",
        decision_timestamp=EVAL,
        kickoff=KICKOFF,
        safeguard_state="PASSED",
        entry_eligibility="PASSED",
        model_probability=0.55,
        edge=0.10,
    )
    yes = PaperTradeEntry(**common, selection="YES", line=None)
    no = PaperTradeEntry(**common, selection="NO", line=None)
    assert yes.paper_trade_id != no.paper_trade_id

    record_paper_trade_entry(conn, yes)
    record_paper_trade_entry(conn, no)

    found_yes = find_existing_paper_trade_entry(
        conn, prediction_id="pred_1", fixture_id=FIXTURE_ID, market="btts", selection="YES", line=None
    )
    assert found_yes["paper_trade_id"] == yes.paper_trade_id
    assert found_yes["line"] is None

    again = find_existing_paper_trade_entry(
        conn, prediction_id="pred_1", fixture_id=FIXTURE_ID, market="btts", selection="YES", line=None
    )
    assert again["paper_trade_id"] == yes.paper_trade_id
