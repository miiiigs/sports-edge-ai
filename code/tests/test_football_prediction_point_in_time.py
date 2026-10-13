"""Point-in-time integrity, provenance and probability/settlement semantics of live predictions.

Built on the real live snapshot from the tracked research DB, so these pin production behaviour.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sports_ai.dashboard.services  # noqa: F401  (pre-import breaks the market_data/dashboard cycle)
import pytest
from sports_ai.dashboard.models import NormalizedFixture
from sports_ai.dashboard.prediction_service import (
    MODEL_PARAMETER_FIT_PROTOCOL,
    PredictionContext,
    build_prediction_for_fixture,
)
from sports_ai.dashboard.snapshot_service import build_current_football_model_snapshot
from sports_ai.storage.football_paper_trading import settlement_state_for_market

REPO = Path(__file__).resolve().parents[2]
RESEARCH_DB = REPO / "research" / "football_research" / "store" / "football_research.sqlite"
_STAKE = {"WON": (1.0, 0.0), "HALF_WIN": (0.5, 0.0), "PUSH": (0.0, 0.0), "HALF_LOSS": (0.0, 0.5), "LOST": (0.0, 1.0)}


@pytest.fixture(scope="module")
def snapshot():
    return build_current_football_model_snapshot(research_db_path=RESEARCH_DB)


def _fixture(home="Arsenal", away="Chelsea", kickoff="2026-08-22T14:00:00+00:00", competition="ENG1"):
    return NormalizedFixture(
        fixture_id="api-football:990001", provider_fixture_id="990001", provider="api-football",
        competition_id=competition, competition_name="Premier League", division_code="E0", season="2026",
        home_team_id="1", home_team_name=home, away_team_id="2", away_team_name=away,
        kickoff_utc=kickoff, status="SCHEDULED", elapsed_minute=None, home_score=None, away_score=None,
        last_provider_update=kickoff, source_version="api_football_v1", provider_payload={},
    )


@pytest.fixture(scope="module")
def ready(snapshot):
    prediction = build_prediction_for_fixture(PredictionContext(fixture=_fixture(), snapshot=snapshot))
    assert prediction["prediction_status"] == "READY", prediction.get("blockers")
    return prediction


def test_prediction_inputs_predate_kickoff(snapshot, ready):
    conn = sqlite3.connect(f"file:{RESEARCH_DB.as_posix()}?mode=ro", uri=True)
    latest = conn.execute("SELECT max(match_date) FROM football_events").fetchone()[0]
    conn.close()
    assert snapshot.model_as_of.startswith(latest)
    assert ready["model_as_of"] == snapshot.model_as_of < ready["kickoff_utc"]
    assert ready["model_parameters"]["research_cutoff"] < ready["kickoff_utc"]


def test_ready_prediction_records_reproducible_parameters(snapshot, ready):
    params = ready["model_parameters"]
    assert params["fit_protocol"] == MODEL_PARAMETER_FIT_PROTOCOL
    assert params["model_snapshot_id"] == snapshot.snapshot_id == ready["model_snapshot_id"]
    assert params["conversion_coefficients"] == snapshot.conversion_coefficients["ENG1"]
    assert params["rho"] == snapshot.rho_by_competition["ENG1"] == ready["scoreline_object"]["rho"]
    assert params["corner_model"] == snapshot.corner_models["ENG1"]
    assert params["corner_model"]["dispersion"] == ready["corner_object"]["dispersion"]
    assert set(ready["model_versions"]) >= {"scoreline_model_version", "conversion_model_version", "current_snapshot_model_version"}


def test_model_history_is_venue_specific_and_counts_only_prior_matches(snapshot, ready):
    conn = sqlite3.connect(f"file:{RESEARCH_DB.as_posix()}?mode=ro", uri=True)
    home = conn.execute(
        "SELECT count(*) FROM football_events WHERE competition_id='ENG1' AND home_team='Arsenal' AND match_date < ?",
        (ready["kickoff_utc"][:10],),
    ).fetchone()[0]
    away = conn.execute(
        "SELECT count(*) FROM football_events WHERE competition_id='ENG1' AND away_team='Chelsea' AND match_date < ?",
        (ready["kickoff_utc"][:10],),
    ).fetchone()[0]
    conn.close()
    assert ready["model_history"]["home"]["venue"] == "home"
    assert ready["model_history"]["away"]["venue"] == "away"
    assert set(ready["model_history"]["home"]["matches_observed_by_statistic"].values()) == {home}
    assert set(ready["model_history"]["away"]["matches_observed_by_statistic"].values()) == {away}


def test_probability_definitions_are_consistent(ready):
    rows = [r for r in ready["market_rows"] if r["market_family"] in {"asian_handicap", "corner_total", "goals_total"}]
    assert rows
    by_key = {(r["market_family"], r["line"], r["selection"]): r for r in rows}
    for r in rows:
        sp = r["settlement_probabilities"]
        w = sp["full_win_probability"] + 0.5 * sp["half_win_probability"]
        lo = sp["full_loss_probability"] + 0.5 * sp["half_loss_probability"]
        assert r["effective_win_stake_probability"] == pytest.approx(w)  # unconditional (push in denominator)
        assert 1.0 / r["fair_decimal_price"] == pytest.approx(w / (w + lo))  # q = push-excluded
        partner = {"HOME": "AWAY", "AWAY": "HOME", "OVER": "UNDER", "UNDER": "OVER"}[r["selection"]]
        other = by_key[(r["market_family"], r["line"], partner)]
        assert 1.0 / r["fair_decimal_price"] + 1.0 / other["fair_decimal_price"] == pytest.approx(1.0)


def test_asian_handicap_pricing_matches_the_paper_settlement_engine(ready):
    """Summing the scoreline matrix through settlement_state_for_market reproduces each AH row."""
    matrix = ready["scoreline_object"]["matrix"]
    total = sum(sum(row) for row in matrix)
    for r in (r for r in ready["market_rows"] if r["market_family"] == "asian_handicap"):
        won = lost = push = 0.0
        for h, row in enumerate(matrix):
            for a, p in enumerate(row):
                state = settlement_state_for_market(
                    market="asian_handicap", selection=r["selection"], line=r["line"], actual_home_goals=h, actual_away_goals=a
                )
                w, lo = _STAKE[state]
                won += p * w
                lost += p * lo
                push += p * (state == "PUSH")
        sp = r["settlement_probabilities"]
        assert won / total == pytest.approx(sp["full_win_probability"] + 0.5 * sp["half_win_probability"], abs=1e-9)
        assert lost / total == pytest.approx(sp["full_loss_probability"] + 0.5 * sp["half_loss_probability"], abs=1e-9)
        assert push / total == pytest.approx(sp["push_probability"], abs=1e-9)
