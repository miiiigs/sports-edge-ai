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


# ---------------------------------------------------------------- behavioural safeguards
from sports_ai.dashboard import prediction_service as ps  # noqa: E402


@pytest.mark.parametrize(
    "model_as_of,kickoff,expected",
    [
        ("2026-05-24T23:59:59+00:00", "2026-05-25T11:30:00+00:00", True),
        ("2026-05-24T23:59:59+00:00", "2026-05-24T19:00:00+00:00", False),  # same day fails closed
        ("2026-05-24T23:59:59+00:00", "2026-05-23T15:00:00+00:00", False),
        (None, "2026-05-25T11:30:00+00:00", False),
    ],
)
def test_model_state_must_predate_kickoff_date(model_as_of, kickoff, expected):
    assert ps.model_state_predates_kickoff(model_as_of, kickoff) is expected


def test_fixture_on_or_before_the_research_cutoff_is_blocked(snapshot):
    same_day = snapshot.model_as_of[:10] + "T20:00:00+00:00"
    prediction = build_prediction_for_fixture(PredictionContext(fixture=_fixture(kickoff=same_day), snapshot=snapshot))
    assert prediction["prediction_status"] == "BLOCKED"
    assert prediction["blockers"] == ["MODEL_STATE_NOT_BEFORE_KICKOFF"]
    assert not prediction.get("market_rows")


@pytest.mark.parametrize("raw,expected", [(None, 1), ("", 1), ("2", 2), ("0", 1), ("-3", 1), ("99", 10), ("x", 1)])
def test_min_venue_history_resolver(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(ps.MIN_VENUE_HISTORY_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(ps.MIN_VENUE_HISTORY_ENV_VAR, raw)
    assert ps.resolve_min_venue_history_matches() == expected


def _with_history(snapshot, monkeypatch, home_count):
    original = snapshot.team_model_history_status

    def fake(*, competition_id, canonical_team_id, venue):
        status = original(competition_id=competition_id, canonical_team_id=canonical_team_id, venue=venue)
        if venue == "home":
            status = {**status, "matches_observed_by_statistic": {k: home_count for k in status["matches_observed_by_statistic"]}}
        return status

    monkeypatch.setattr(type(snapshot), "team_model_history_status", lambda self, **kw: fake(**kw))


def test_default_keeps_historical_behaviour_for_one_venue_match(snapshot, monkeypatch):
    monkeypatch.delenv(ps.MIN_VENUE_HISTORY_ENV_VAR, raising=False)
    _with_history(snapshot, monkeypatch, home_count=1)
    prediction = build_prediction_for_fixture(PredictionContext(fixture=_fixture(), snapshot=snapshot))
    assert prediction["prediction_status"] == "READY"


def test_min_venue_history_blocks_insufficient_history_when_enabled(snapshot, monkeypatch):
    monkeypatch.setenv(ps.MIN_VENUE_HISTORY_ENV_VAR, "2")
    _with_history(snapshot, monkeypatch, home_count=1)
    prediction = build_prediction_for_fixture(PredictionContext(fixture=_fixture(), snapshot=snapshot))
    assert prediction["prediction_status"] == "BLOCKED"
    assert prediction["blockers"] == ["HOME_MODEL_HISTORY_INSUFFICIENT"]
    _with_history(snapshot, monkeypatch, home_count=2)
    assert build_prediction_for_fixture(PredictionContext(fixture=_fixture(), snapshot=snapshot))["prediction_status"] == "READY"
