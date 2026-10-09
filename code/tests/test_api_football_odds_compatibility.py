"""Focused compatibility tests for API-Football current-market odds ingestion."""

from __future__ import annotations

import pytest
from sports_ai.dashboard.current_market import build_fixture_market_evaluation
from sports_ai.market_data.api_football import ApiFootballOddsProvider
from sports_ai.market_data.providers import NormalizedMarketSnapshot

FIXTURE_ID = "football-data.org:560546"
KICKOFF = "2026-08-23T14:00:00+00:00"
EVAL = "2026-08-23T09:00:00+00:00"
FRESH_SOURCE = "2026-08-23T06:30:00+00:00"
STALE_SOURCE = "2026-08-23T04:00:00+00:00"

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


def _value(name: str, price: float) -> dict[str, object]:
    return {"value": name, "odd": f"{price:.2f}"}


def _bet(name: str, bet_id: int, *values: dict[str, object]) -> dict[str, object]:
    return {"id": bet_id, "name": name, "values": list(values)}


def _bookmaker(name: str, bookmaker_id: int, *bets: dict[str, object]) -> dict[str, object]:
    return {"id": bookmaker_id, "name": name, "bets": list(bets)}


def _payload(*bookmakers: dict[str, object], source_timestamp: str | None) -> dict[str, object]:
    return {
        "response": [
            {
                "fixture": {"id": "api-football-fixture-1"},
                "update": source_timestamp,
                "bookmakers": list(bookmakers),
            }
        ]
    }


def _provider_snapshots(monkeypatch, *bookmakers: dict[str, object], source_timestamp: str | None = FRESH_SOURCE) -> list[NormalizedMarketSnapshot]:
    monkeypatch.setenv("SPORTS_AI_MARKET_REFERENCE_BOOKMAKERS", "pinnacle, betfair , bet365")
    provider = ApiFootballOddsProvider()
    return provider._snapshot_rows_from_payload(
        fixture_id=FIXTURE_ID,
        fixture_context=FIXTURE_CONTEXT,
        provider_fixture_id="api-football-fixture-1",
        payload=_payload(*bookmakers, source_timestamp=source_timestamp),
    )


def _market_row(market_family: str, selection: str, *, line: float | None, fair_price: float, win: float, loss: float) -> dict[str, object]:
    return {
        "as_of": "2026-08-23T08:00:00+00:00",
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "division_code": "E0",
        "effective_loss_stake_probability": loss,
        "effective_win_stake_probability": win,
        "event_id": FIXTURE_ID,
        "fair_decimal_price": fair_price,
        "fixture_id": FIXTURE_ID,
        "kickoff_at": KICKOFF,
        "kickoff_time": "14:00",
        "line": line,
        "market_family": market_family,
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


def _prediction(*market_rows: dict[str, object]) -> dict[str, object]:
    return {
        "prediction_status": "READY",
        "fixture_id": FIXTURE_ID,
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "home_team_name": "Home FC",
        "away_team_name": "Away FC",
        "kickoff_utc": KICKOFF,
        "model_as_of": "2026-08-23T08:00:00+00:00",
        "model_snapshot_id": "snapshot_1",
        "expected_goals_home": 1.6,
        "expected_goals_away": 1.2,
        "expected_total_goals": 2.8,
        "market_rows": list(market_rows),
    }


def _evaluate(snapshots: list[NormalizedMarketSnapshot], *market_rows: dict[str, object]) -> dict[str, object]:
    return build_fixture_market_evaluation(
        fixture_context=FIXTURE_CONTEXT,
        prediction=_prediction(*market_rows),
        snapshots=snapshots,
        current_xg_connected=False,
        market_freshness_evaluated_at=EVAL,
    )


def _decision_row(evaluation: dict[str, object], market_family: str, selection: str, *, line: float | None) -> dict[str, object]:
    for row in evaluation.get("decision_rows") or []:
        if row["market_family"] == market_family and row["selection"] == selection and row["line"] == line:
            return row
    raise AssertionError(f"missing decision row for {market_family} {selection} {line}")


def test_lowercase_pinnacle_matches_provider_display_name(monkeypatch):
    snapshots = _provider_snapshots(
        monkeypatch,
        _bookmaker(
            "Pinnacle",
            1,
            _bet("Match Winner", 1, _value("Home", 2.00), _value("Draw", 3.30), _value("Away", 3.80)),
        ),
    )
    assert {snapshot.bookmaker for snapshot in snapshots if snapshot.reference_designation == "reference"} == {"Pinnacle"}


def test_lowercase_betfair_matches_provider_display_name(monkeypatch):
    snapshots = _provider_snapshots(
        monkeypatch,
        _bookmaker(
            "Betfair",
            2,
            _bet("Match Winner", 1, _value("Home", 2.00), _value("Draw", 3.30), _value("Away", 3.80)),
        ),
    )
    assert {snapshot.bookmaker for snapshot in snapshots if snapshot.reference_designation == "reference"} == {"Betfair"}


def test_pinnacle_preferred_when_complete(monkeypatch):
    snapshots = _provider_snapshots(
        monkeypatch,
        _bookmaker("Pinnacle", 1, _bet("Match Winner", 1, _value("Home", 2.00), _value("Draw", 3.30), _value("Away", 3.80))),
        _bookmaker("Betfair", 2, _bet("Match Winner", 1, _value("Home", 2.02), _value("Draw", 3.35), _value("Away", 3.85))),
        _bookmaker("Bet365", 3, _bet("Match Winner", 1, _value("Home", 2.20), _value("Draw", 3.40), _value("Away", 4.10))),
    )
    evaluation = _evaluate(
        snapshots,
        _market_row("1x2", "HOME", line=None, fair_price=1.82, win=0.55, loss=0.45),
        _market_row("1x2", "DRAW", line=None, fair_price=3.70, win=0.24, loss=0.76),
        _market_row("1x2", "AWAY", line=None, fair_price=4.10, win=0.21, loss=0.79),
    )
    home = _decision_row(evaluation, "1x2", "HOME", line=None)
    assert home["reference_bookmaker"] == "Pinnacle"


def test_betfair_fallback_when_pinnacle_missing(monkeypatch):
    snapshots = _provider_snapshots(
        monkeypatch,
        _bookmaker("Betfair", 2, _bet("Match Winner", 1, _value("Home", 2.01), _value("Draw", 3.35), _value("Away", 3.90))),
        _bookmaker("Bet365", 3, _bet("Match Winner", 1, _value("Home", 2.18), _value("Draw", 3.45), _value("Away", 4.05))),
    )
    evaluation = _evaluate(
        snapshots,
        _market_row("1x2", "HOME", line=None, fair_price=1.82, win=0.55, loss=0.45),
        _market_row("1x2", "DRAW", line=None, fair_price=3.70, win=0.24, loss=0.76),
        _market_row("1x2", "AWAY", line=None, fair_price=4.10, win=0.21, loss=0.79),
    )
    home = _decision_row(evaluation, "1x2", "HOME", line=None)
    assert home["reference_bookmaker"] == "Betfair"


def test_bet365_fallback_when_first_two_unavailable(monkeypatch):
    snapshots = _provider_snapshots(
        monkeypatch,
        _bookmaker("Pinnacle", 1, _bet("Match Winner", 1, _value("Home", 2.00), _value("Draw", 3.30))),
        _bookmaker("Betfair", 2, _bet("Match Winner", 1, _value("Home", 2.02), _value("Away", 3.90))),
        _bookmaker("Bet365", 3, _bet("Match Winner", 1, _value("Home", 2.18), _value("Draw", 3.45), _value("Away", 4.05))),
    )
    evaluation = _evaluate(
        snapshots,
        _market_row("1x2", "HOME", line=None, fair_price=1.82, win=0.55, loss=0.45),
        _market_row("1x2", "DRAW", line=None, fair_price=3.70, win=0.24, loss=0.76),
        _market_row("1x2", "AWAY", line=None, fair_price=4.10, win=0.21, loss=0.79),
    )
    home = _decision_row(evaluation, "1x2", "HOME", line=None)
    assert home["reference_bookmaker"] == "Bet365"


def test_no_approved_complete_reference_fails_closed(monkeypatch):
    snapshots = _provider_snapshots(
        monkeypatch,
        _bookmaker("Pinnacle", 1, _bet("Match Winner", 1, _value("Home", 2.00), _value("Draw", 3.30))),
        _bookmaker("Betfair", 2, _bet("Match Winner", 1, _value("Home", 2.02), _value("Away", 3.90))),
        _bookmaker("Bet365", 3, _bet("Match Winner", 1, _value("Draw", 3.45), _value("Away", 4.05))),
    )
    evaluation = _evaluate(
        snapshots,
        _market_row("1x2", "HOME", line=None, fair_price=1.82, win=0.55, loss=0.45),
        _market_row("1x2", "DRAW", line=None, fair_price=3.70, win=0.24, loss=0.76),
        _market_row("1x2", "AWAY", line=None, fair_price=4.10, win=0.21, loss=0.79),
    )
    home = _decision_row(evaluation, "1x2", "HOME", line=None)
    assert home["decision"] == "MISSING_REQUIRED_MARKET_DATA"
    assert home["reason_codes"] == ["INCOMPLETE_REFERENCE_MARKET"]


def test_cross_book_reference_mixing_is_blocked(monkeypatch):
    snapshots = _provider_snapshots(
        monkeypatch,
        _bookmaker("Pinnacle", 1, _bet("Match Winner", 1, _value("Home", 2.00), _value("Draw", 3.30))),
        _bookmaker("Betfair", 2, _bet("Match Winner", 1, _value("Away", 3.90))),
        _bookmaker("Bet365", 3, _bet("Match Winner", 1, _value("Home", 2.18))),
    )
    evaluation = _evaluate(
        snapshots,
        _market_row("1x2", "HOME", line=None, fair_price=1.82, win=0.55, loss=0.45),
        _market_row("1x2", "DRAW", line=None, fair_price=3.70, win=0.24, loss=0.76),
        _market_row("1x2", "AWAY", line=None, fair_price=4.10, win=0.21, loss=0.79),
    )
    away = _decision_row(evaluation, "1x2", "AWAY", line=None)
    assert away["decision"] == "MISSING_REQUIRED_MARKET_DATA"
    assert away["reason_codes"] == ["INCOMPLETE_REFERENCE_MARKET"]


def test_btts_fallback_uses_complete_same_book_pair(monkeypatch):
    snapshots = _provider_snapshots(
        monkeypatch,
        _bookmaker("Pinnacle", 1, _bet("Both Teams Score", 8, _value("Yes", 1.85))),
        _bookmaker("Betfair", 2, _bet("Both Teams Score", 8, _value("Yes", 1.90), _value("No", 1.92))),
        _bookmaker("Bet365", 3, _bet("Both Teams Score", 8, _value("Yes", 2.10), _value("No", 1.78))),
    )
    evaluation = _evaluate(
        snapshots,
        _market_row("btts", "YES", line=None, fair_price=1.82, win=0.55, loss=0.45),
        _market_row("btts", "NO", line=None, fair_price=2.20, win=0.45, loss=0.55),
    )
    yes = _decision_row(evaluation, "btts", "YES", line=None)
    assert yes["reference_bookmaker"] == "Betfair"


def test_goals_reference_requires_exact_line_pairing(monkeypatch):
    snapshots = _provider_snapshots(
        monkeypatch,
        _bookmaker("Pinnacle", 1, _bet("Goals Over/Under", 5, _value("Over 2.5", 1.90))),
        _bookmaker("Betfair", 2, _bet("Goals Over/Under", 5, _value("Over 2.5", 1.91), _value("Under 3.0", 1.93))),
        _bookmaker("Bet365", 3, _bet("Goals Over/Under", 5, _value("Over 2.5", 2.12), _value("Under 2.5", 1.79))),
    )
    evaluation = _evaluate(
        snapshots,
        _market_row("goals_total", "OVER", line=2.5, fair_price=1.88, win=0.56, loss=0.44),
        _market_row("goals_total", "UNDER", line=2.5, fair_price=2.08, win=0.44, loss=0.56),
    )
    over = _decision_row(evaluation, "goals_total", "OVER", line=2.5)
    assert over["reference_bookmaker"] == "Bet365"
    assert over["best_available_price"] != pytest.approx(1.91)


def test_corners_reference_requires_exact_line_pairing(monkeypatch):
    snapshots = _provider_snapshots(
        monkeypatch,
        _bookmaker("Pinnacle", 1, _bet("Corners Over Under", 45, _value("Over 9.5", 1.94))),
        _bookmaker("Betfair", 2, _bet("Corners Over Under", 45, _value("Over 9.5", 1.95), _value("Under 10.0", 1.86))),
        _bookmaker("Bet365", 3, _bet("Corners Over Under", 45, _value("Over 9.5", 2.18), _value("Under 9.5", 1.74))),
    )
    evaluation = _evaluate(
        snapshots,
        _market_row("corner_total", "OVER", line=9.5, fair_price=1.86, win=0.57, loss=0.43),
        _market_row("corner_total", "UNDER", line=9.5, fair_price=2.16, win=0.43, loss=0.57),
    )
    over = _decision_row(evaluation, "corner_total", "OVER", line=9.5)
    assert over["reference_bookmaker"] == "Bet365"


def test_home_asian_handicap_normalization(monkeypatch):
    snapshots = _provider_snapshots(
        monkeypatch,
        _bookmaker("Pinnacle", 1, _bet("Asian Handicap", 4, _value("Home -0.25", 1.95))),
    )
    home = next(snapshot for snapshot in snapshots if snapshot.market == "asian_handicap")
    assert home.selection == "HOME"
    assert home.line == pytest.approx(-0.25)


def test_away_asian_handicap_keeps_home_handicap_line(monkeypatch):
    # Proven API-Football convention (HOME_HANDICAP): the raw numeric label is the
    # canonical HOME handicap for both sides and is NEVER sign-flipped.
    snapshots = _provider_snapshots(
        monkeypatch,
        _bookmaker("Pinnacle", 1, _bet("Asian Handicap", 4, _value("Away -0.25", 2.60), _value("Away +0.25", 4.25))),
    )
    lines = {(s.selection, s.line) for s in snapshots if s.market == "asian_handicap"}
    assert lines == {("AWAY", -0.25), ("AWAY", 0.25)}


def test_negative_quarter_line_ah_pair_shares_canonical_line(monkeypatch):
    snapshots = _provider_snapshots(
        monkeypatch,
        _bookmaker("Pinnacle", 1, _bet("Asian Handicap", 4, _value("Home -0.25", 1.48), _value("Away -0.25", 2.60))),
    )
    lines = {(snapshot.selection, snapshot.line) for snapshot in snapshots if snapshot.market == "asian_handicap"}
    assert lines == {("HOME", -0.25), ("AWAY", -0.25)}


def test_positive_quarter_line_ah_pair_shares_canonical_line(monkeypatch):
    snapshots = _provider_snapshots(
        monkeypatch,
        _bookmaker("Pinnacle", 1, _bet("Asian Handicap", 4, _value("Home +0.25", 1.21), _value("Away +0.25", 4.25))),
    )
    lines = {(snapshot.selection, snapshot.line) for snapshot in snapshots if snapshot.market == "asian_handicap"}
    assert lines == {("HOME", 0.25), ("AWAY", 0.25)}


def test_quarter_line_precision_preserved(monkeypatch):
    snapshots = _provider_snapshots(
        monkeypatch,
        _bookmaker("Pinnacle", 1, _bet("Asian Handicap", 4, _value("Home -0.75", 1.88), _value("Away -0.75", 1.98))),
    )
    lines = {(snapshot.selection, snapshot.line) for snapshot in snapshots if snapshot.market == "asian_handicap"}
    assert lines == {("HOME", -0.75), ("AWAY", -0.75)}


@pytest.mark.parametrize("line", ["-0.75", "-0.5", "-0.25", "0", "+0.25", "+0.5", "+0.75", "-1", "-2"])
def test_ah_home_handicap_convention_matrix(monkeypatch, line):
    snapshots = _provider_snapshots(
        monkeypatch,
        _bookmaker("Pinnacle", 1, _bet("Asian Handicap", 4, _value(f"Home {line}", 1.90), _value(f"Away {line}", 1.95))),
    )
    expected = float(line)
    by_sel = {snapshot.selection: snapshot.line for snapshot in snapshots if snapshot.market == "asian_handicap"}
    assert set(by_sel) == {"HOME", "AWAY"}
    assert by_sel["HOME"] == pytest.approx(expected)
    assert by_sel["AWAY"] == pytest.approx(expected)


def test_ah_snapshots_share_provider_market_group_identity(monkeypatch):
    snapshots = _provider_snapshots(
        monkeypatch,
        _bookmaker("Pinnacle", 1, _bet("Asian Handicap", 4, _value("Home -0.25", 1.48), _value("Away -0.25", 2.60))),
    )
    ah = [s for s in snapshots if s.market == "asian_handicap"]
    assert len({s.provider_market_id for s in ah}) == 1
    assert all(s.ingestion_version == "api_football_current_market_provider_v2" for s in ah)


# Sanitized raw production ladder (fixture 1563802, Bet365, bet id 4) captured by a
# controlled probe. No secrets. Reproduces the previously impossible pairing.
_RAW_BET365_1563802_AH = [
    ("Home -1.25", "2.42"), ("Away -1.25", "1.52"), ("Home -1", "2.15"), ("Away -1", "1.68"),
    ("Home -0.75", "1.88"), ("Away -0.75", "1.98"), ("Home -0.5", "1.68"), ("Away -0.5", "2.15"),
    ("Home -0.25", "1.48"), ("Away -0.25", "2.60"), ("Home +0", "1.26"), ("Away +0", "3.70"),
    ("Home +0.25", "1.21"), ("Away +0.25", "4.25"), ("Home +0.5", "1.18"), ("Away +0.5", "4.65"),
    ("Home +0.75", "1.12"), ("Away +0.75", "6.40"), ("Home -2", "4.65"), ("Away -2", "1.18"),
    ("Home -1.75", "3.45"), ("Away -1.75", "1.30"), ("Home -1.5", "2.75"), ("Away -1.5", "1.42"),
    ("Home -2.5", "5.75"), ("Away -2.5", "1.14"), ("Home -2.25", "5.25"), ("Away -2.25", "1.16"),
    ("Home -2.75", "7.00"), ("Away -2.75", "1.10"),
]


def test_raw_production_ladder_pairs_validly_under_home_handicap(monkeypatch):
    values = [_value(label, float(odd)) for label, odd in _RAW_BET365_1563802_AH]
    snapshots = _provider_snapshots(monkeypatch, _bookmaker("Bet365", 8, _bet("Asian Handicap", 4, *values)))
    by_line: dict[float, dict[str, float]] = {}
    for snapshot in snapshots:
        if snapshot.market != "asian_handicap":
            continue
        by_line.setdefault(snapshot.line, {})[snapshot.selection] = snapshot.decimal_odds
    assert by_line, "no AH snapshots produced"
    for line, sides in by_line.items():
        assert set(sides) == {"HOME", "AWAY"}, (line, sides)
        assert 1.0 / sides["HOME"] + 1.0 / sides["AWAY"] >= 1.0 - 1e-9, (line, sides)
    # The old NAMED_TEAM sign-flip wrongly paired Home -0.25 (1.48) with Away +0.25 (4.25).
    assert 1.0 / 1.48 + 1.0 / 4.25 < 1.0


def test_invalid_api_football_odds_values_are_dropped(monkeypatch):
    snapshots = _provider_snapshots(
        monkeypatch,
        _bookmaker(
            "Pinnacle",
            1,
            _bet("Both Teams Score", 8, {"value": "Yes", "odd": "bad"}, _value("No", 1.00), _value("Yes", 1.92)),
        ),
    )
    assert [(snapshot.selection, snapshot.decimal_odds) for snapshot in snapshots if snapshot.market == "btts"] == [("YES", 1.92)]


def test_api_football_fixture_context_reuses_provider_fixture_id(monkeypatch):
    provider = ApiFootballOddsProvider()
    fixture_context = {
        **FIXTURE_CONTEXT,
        "provider": "api-football",
        "provider_fixture_id": "1557378",
    }
    assert provider._resolve_provider_fixture_id(fixture_context) == "1557378"


def test_api_football_fresh_snapshot_passes_provider_threshold(monkeypatch):
    monkeypatch.delenv("SPORTS_AI_API_FOOTBALL_MARKET_MAX_AGE_MINUTES", raising=False)
    provider = ApiFootballOddsProvider()
    assert provider._freshness_state(
        source_timestamp="2026-08-23T06:30:00+00:00",
        captured_at="2026-08-23T09:00:00+00:00",
        kickoff_utc=KICKOFF,
    ) == "FRESH"


def test_api_football_stale_snapshot_fails_closed(monkeypatch):
    monkeypatch.delenv("SPORTS_AI_API_FOOTBALL_MARKET_MAX_AGE_MINUTES", raising=False)
    provider = ApiFootballOddsProvider()
    assert provider._freshness_state(
        source_timestamp="2026-08-23T04:00:00+00:00",
        captured_at="2026-08-23T09:00:00+00:00",
        kickoff_utc=KICKOFF,
    ) == "STALE"


def test_api_football_missing_timestamp_fails_closed(monkeypatch):
    provider = ApiFootballOddsProvider()
    assert provider._freshness_state(
        source_timestamp=None,
        captured_at="2026-08-23T09:00:00+00:00",
        kickoff_utc=KICKOFF,
    ) == "UNKNOWN"


def test_reference_odds_remain_separate_from_best_offer(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_API_FOOTBALL_MARKET_MAX_AGE_MINUTES", "100000")
    snapshots = _provider_snapshots(
        monkeypatch,
        _bookmaker("Pinnacle", 1, _bet("Both Teams Score", 8, _value("Yes", 1.85))),
        _bookmaker("Betfair", 2, _bet("Both Teams Score", 8, _value("Yes", 1.90), _value("No", 1.92))),
        _bookmaker("Bet365", 3, _bet("Both Teams Score", 8, _value("Yes", 2.10), _value("No", 1.80))),
    )
    evaluation = _evaluate(
        snapshots,
        _market_row("btts", "YES", line=None, fair_price=1.82, win=0.55, loss=0.45),
        _market_row("btts", "NO", line=None, fair_price=2.20, win=0.45, loss=0.55),
    )
    yes = _decision_row(evaluation, "btts", "YES", line=None)
    assert yes["reference_bookmaker"] == "Betfair"
    assert yes["best_available_bookmaker"] == "Bet365"
    assert yes["best_available_price"] == pytest.approx(2.10)
