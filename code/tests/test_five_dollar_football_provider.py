"""Focused tests for the 5DollarFootballAPI secondary current-market provider.

No network is used: every test injects a fake HTTP client.
"""

from __future__ import annotations

import copy
import json

import pytest

# Pre-existing market_data <-> dashboard import cycle: import a dashboard module
# first so the market_data package initializes cleanly (same guard used by the
# existing The Odds API / API-Football provider tests).
import sports_ai.dashboard.services  # noqa: F401
from sports_ai import firewall
from sports_ai.market_data import five_dollar_football as five
from sports_ai.market_data.providers import MarketProviderError

FIXTURE_ID = "evt_api_football_2311636891"
CONTEXT = {
    "competition_id": "FRA1",
    "home_team_name": "Marseille",
    "away_team_name": "Paris Saint Germain",
    "kickoff_utc": "2026-09-20T18:45:00+00:00",
}


def _window(fixtures):
    return {"success": 1, "data": fixtures, "pagination": {"page": 1, "per_page": 100, "has_more": False}}


def _event(fid=2311636891, home="Marseille", away="Paris Saint Germain", kickoff="2026-09-20T18:45:00+00:00"):
    return {
        "id": fid,
        "league": {"id": five.FRA1_LEAGUE_ID, "name": "France Ligue 1"},
        "teams": {"home": {"name": home}, "away": {"name": away}},
        "kickoff_utc": kickoff,
        "status": "finished",
    }


def _odds():
    return {
        "success": 1,
        "data": {
            "fixture_id": 2311636891,
            "league": {"id": five.FRA1_LEAGUE_ID, "name": "France Ligue 1"},
            "teams": {"home": {"name": "Marseille"}, "away": {"name": "Paris Saint Germain"}},
            "kickoff_utc": "2026-09-20T18:45:00+00:00",
            "bookmakers": [
                {
                    "name": "Bet 365",
                    "slug": "bet365",
                    "odds": {
                        "asian_handicap": {
                            "opening": {"line": -0.25, "home": 1.95, "away": 1.85},
                            "closing": {"line": -0.5, "home": 2.02, "away": 1.78},
                            "inplay": {"line": 0, "home": 3.5, "away": 1.3},
                        },
                        "corner_line": {
                            "opening": {"line": 8.5, "over": 1.85, "under": 1.95},
                            "closing": {"line": 9.0, "over": 1.90, "under": 1.80},
                            "inplay": {"line": 10.5, "over": 3.0, "under": 1.375},
                        },
                    },
                },
                {"name": "Pinnacle", "slug": "pinnacle", "odds": {"asian_handicap": {}}},
            ],
        },
    }


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    def __init__(self, router, calls):
        self._router = router
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None, headers=None):
        self._calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        return self._router(url, dict(params or {}))


def _router(fixtures=None, odds=None, fixtures_status=200):
    def route(url, params):
        if url.endswith("/odds"):
            return _Resp(200 if odds is not None else 500, odds if odds is not None else {"success": 0})
        return _Resp(fixtures_status, _window(fixtures or []))

    return route


def _provider(monkeypatch, *, fixtures=None, odds=None, fixtures_status=200, key="test-key", config=None):
    monkeypatch.setenv(five.ENV_API_KEY, key)
    calls: list = []
    router = _router(fixtures, odds, fixtures_status)
    return five.FiveDollarFootballProvider(
        config or five.FiveDollarConfig(),
        client_factory=lambda: _Client(router, calls),
    ), calls, router


# --------------------------------------------------------------------------- credential
def test_missing_credential_is_not_configured(monkeypatch):
    monkeypatch.delenv(five.ENV_API_KEY, raising=False)
    provider = five.FiveDollarFootballProvider(five.FiveDollarConfig())
    assert provider.configured() is False
    assert provider.enabled() is False
    with pytest.raises(MarketProviderError):
        provider.get_fixture_quotes(FIXTURE_ID, fixture_context=CONTEXT)


def test_secret_never_appears_in_diagnostics_or_errors(monkeypatch):
    secret = "fb_live_supersecret_abc123"
    fixtures = [_event()]
    provider, calls, _ = _provider(monkeypatch, fixtures=fixtures, odds=_odds(), key=secret)
    quotes = provider.get_fixture_quotes(FIXTURE_ID, fixture_context=CONTEXT)
    assert quotes
    blob = json.dumps(provider.diagnostics())
    assert secret not in blob
    # the key WAS sent only in the Authorization header (never in URL/params)
    assert any(c["headers"].get("Authorization", "").endswith(secret) for c in calls)
    assert all(secret not in c["url"] for c in calls)
    assert all(secret not in json.dumps(c["params"]) for c in calls)


# --------------------------------------------------------------------------- mapping
def test_fra1_league_id_mapping(monkeypatch):
    monkeypatch.setenv(five.ENV_API_KEY, "k")
    provider = five.FiveDollarFootballProvider(five.FiveDollarConfig())
    assert provider.league_id("FRA1") == 3614399544
    assert five.FRA1_LEAGUE_ID == 3614399544


def test_legacy_invalid_league_id_not_used(monkeypatch):
    monkeypatch.setenv(five.ENV_API_KEY, "k")
    assert five.LEGACY_INVALID_LEAGUE_ID == 25
    assert 25 not in set(five.COMPETITION_TO_LEAGUE.values())
    provider = five.FiveDollarFootballProvider(five.FiveDollarConfig())
    assert provider.league_id("FRA1") == five.FRA1_LEAGUE_ID
    assert provider.league_id("FRA1") != five.LEGACY_INVALID_LEAGUE_ID


def test_unsupported_competition_fails_closed(monkeypatch):
    monkeypatch.setenv(five.ENV_API_KEY, "k")
    provider = five.FiveDollarFootballProvider(five.FiveDollarConfig())
    with pytest.raises(MarketProviderError) as exc:
        provider.league_id("ENG1")
    assert "MARKET_COMPETITION_UNSUPPORTED" in str(exc.value)


# --------------------------------------------------------------------------- matching
def test_match_fixture_matched(monkeypatch):
    provider, _, _ = _provider(monkeypatch, fixtures=[_event()], odds=_odds())
    match = provider.match_fixture(CONTEXT)
    assert match.state == five.MATCH_MATCHED
    assert match.provider_fixture_id == "2311636891"


def test_match_fixture_unmatched(monkeypatch):
    provider, _, _ = _provider(monkeypatch, fixtures=[_event(home="Lyon", away="Rennes")], odds=_odds())
    assert provider.match_fixture(CONTEXT).state == five.MATCH_UNMATCHED


def test_match_fixture_ambiguous(monkeypatch):
    provider, _, _ = _provider(
        monkeypatch, fixtures=[_event(fid=1), _event(fid=2)], odds=_odds()
    )
    match = provider.match_fixture(CONTEXT)
    assert match.state == five.MATCH_AMBIGUOUS
    assert match.candidate_count == 2


# --------------------------------------------------------------------------- AH parsing
def _quotes_by(quotes):
    return {(q.market, q.phase, q.selection): q for q in quotes}


def test_asian_handicap_opening_closing_inplay(monkeypatch):
    provider, _, _ = _provider(monkeypatch, fixtures=[_event()], odds=_odds())
    quotes = provider.get_fixture_quotes(FIXTURE_ID, fixture_context=CONTEXT)
    by = _quotes_by(quotes)
    assert by[(five.MARKET_ASIAN_HANDICAP, "opening", "HOME")].line == -0.25
    assert by[(five.MARKET_ASIAN_HANDICAP, "opening", "HOME")].decimal_odds == 1.95
    assert by[(five.MARKET_ASIAN_HANDICAP, "opening", "AWAY")].line == -0.25
    assert by[(five.MARKET_ASIAN_HANDICAP, "opening", "AWAY")].decimal_odds == 1.85
    assert by[(five.MARKET_ASIAN_HANDICAP, "closing", "HOME")].line == -0.5
    assert by[(five.MARKET_ASIAN_HANDICAP, "inplay", "HOME")].line == 0.0


def test_ah_home_and_away_share_canonical_home_handicap_line(monkeypatch):
    provider, _, _ = _provider(monkeypatch, fixtures=[_event()], odds=_odds())
    quotes = provider.get_fixture_quotes(FIXTURE_ID, fixture_context=CONTEXT)
    opening = {(q.selection, q.line) for q in quotes if q.market == five.MARKET_ASIAN_HANDICAP and q.phase == "opening"}
    assert opening == {("HOME", -0.25), ("AWAY", -0.25)}  # repo home_handicap convention


@pytest.mark.parametrize("line", [-0.25, -0.5, -0.75, 0.25, 0.5, 0.75, 1.0, 1.25])
def test_ah_quarter_and_half_lines_preserved(monkeypatch, line):
    odds = copy.deepcopy(_odds())
    odds["data"]["bookmakers"][0]["odds"]["asian_handicap"]["opening"] = {"line": line, "home": 1.9, "away": 1.9}
    provider, _, _ = _provider(monkeypatch, fixtures=[_event()], odds=odds)
    quotes = provider.get_fixture_quotes(FIXTURE_ID, fixture_context=CONTEXT)
    home = next(q for q in quotes if q.market == five.MARKET_ASIAN_HANDICAP and q.phase == "opening" and q.selection == "HOME")
    assert home.line == line  # exact, no rounding


# --------------------------------------------------------------------------- corner parsing
def test_corner_opening_closing_and_lines(monkeypatch):
    provider, _, _ = _provider(monkeypatch, fixtures=[_event()], odds=_odds())
    quotes = provider.get_fixture_quotes(FIXTURE_ID, fixture_context=CONTEXT)
    by = _quotes_by(quotes)
    assert by[(five.MARKET_CORNER_TOTAL, "opening", "OVER")].line == 8.5
    assert by[(five.MARKET_CORNER_TOTAL, "opening", "OVER")].decimal_odds == 1.85
    assert by[(five.MARKET_CORNER_TOTAL, "closing", "OVER")].line == 9.0  # integer preserved
    assert by[(five.MARKET_CORNER_TOTAL, "inplay", "UNDER")].line == 10.5


def test_corner_half_line_preserved(monkeypatch):
    odds = copy.deepcopy(_odds())
    odds["data"]["bookmakers"][0]["odds"]["corner_line"]["opening"] = {"line": 9.5, "over": 1.8, "under": 2.0}
    provider, _, _ = _provider(monkeypatch, fixtures=[_event()], odds=odds)
    quotes = provider.get_fixture_quotes(FIXTURE_ID, fixture_context=CONTEXT)
    over = next(q for q in quotes if q.market == five.MARKET_CORNER_TOTAL and q.phase == "opening" and q.selection == "OVER")
    assert over.line == 9.5


def test_bet365_normalization_and_other_books_dropped(monkeypatch):
    provider, _, _ = _provider(monkeypatch, fixtures=[_event()], odds=_odds())
    quotes = provider.get_fixture_quotes(FIXTURE_ID, fixture_context=CONTEXT)
    assert quotes
    assert all(q.bookmaker == "Bet 365" for q in quotes)
    assert all(q.provider == "5dollar-football-api" for q in quotes)


# --------------------------------------------------------------------------- inplay guard
def test_inplay_phase_never_pre_match(monkeypatch):
    assert five.is_pre_match_phase("opening") is True
    assert five.is_pre_match_phase("closing") is True
    assert five.is_pre_match_phase("inplay") is False
    provider, _, _ = _provider(monkeypatch, fixtures=[_event()], odds=_odds())
    pre = provider.get_pre_match_quotes(FIXTURE_ID, fixture_context=CONTEXT)
    assert pre and all(q.phase in {"opening", "closing"} for q in pre)
    assert all(q.is_pre_match for q in pre)


# --------------------------------------------------------------------------- rate safety
def test_hourly_governor_guard():
    gov = five.FiveDollarRateGovernor(max_per_hour=2, max_per_minute=10, clock=lambda: 1000.0)
    assert gov.try_acquire() is True
    assert gov.try_acquire() is True
    assert gov.try_acquire() is False
    snap = gov.snapshot()
    assert snap["blocked_by_hour_cap"] == 1
    assert snap["requests_this_hour"] == 2


def test_minute_burst_guard():
    gov = five.FiveDollarRateGovernor(max_per_hour=100, max_per_minute=1, clock=lambda: 1000.0)
    assert gov.try_acquire() is True
    assert gov.try_acquire() is False
    assert gov.snapshot()["blocked_by_minute_cap"] == 1


def test_governor_blocks_fail_closed_without_provider_call(monkeypatch):
    monkeypatch.setenv(five.ENV_API_KEY, "k")
    calls: list = []
    router = _router([_event()], _odds())
    gov = five.FiveDollarRateGovernor(max_per_hour=0, max_per_minute=0, clock=lambda: 1000.0)
    provider = five.FiveDollarFootballProvider(
        five.FiveDollarConfig(), client_factory=lambda: _Client(router, calls), governor=gov
    )
    with pytest.raises(MarketProviderError):
        provider.get_fixture_quotes(FIXTURE_ID, fixture_context=CONTEXT)
    assert calls == []  # no provider traffic once the local cap is hit


def test_response_cache_avoids_repeat_calls(monkeypatch):
    provider, calls, _ = _provider(monkeypatch, fixtures=[_event()], odds=_odds())
    provider.get_fixture_quotes(FIXTURE_ID, fixture_context=CONTEXT)
    calls_after_first = len(calls)
    provider.get_fixture_quotes(FIXTURE_ID, fixture_context=CONTEXT)
    assert len(calls) == calls_after_first  # second call served from cache
    assert provider.diagnostics()["cache"]["avoided"] >= 1


# --------------------------------------------------------------------------- errors / role / firewall
def test_provider_error_fails_closed(monkeypatch):
    provider, _, _ = _provider(monkeypatch, fixtures=[_event()], odds=None)  # odds router -> 500
    with pytest.raises(MarketProviderError) as exc:
        provider.get_fixture_quotes(FIXTURE_ID, fixture_context=CONTEXT)
    assert "PROVIDER_ERROR" in str(exc.value)


def test_primary_provider_remains_primary(monkeypatch):
    assert five.ROLE == "SECONDARY_ONLY"
    monkeypatch.setenv(five.ENV_API_KEY, "k")
    provider = five.FiveDollarFootballProvider(five.FiveDollarConfig())
    diag = provider.diagnostics()
    assert diag["role"] == "SECONDARY_ONLY"
    # the API-Football provider module must not import/reference the secondary adapter
    import pathlib

    api_football = pathlib.Path(five.__file__).with_name("api_football.py").read_text(encoding="utf-8")
    assert "five_dollar" not in api_football
    assert "5dollar" not in api_football


def test_market_snapshot_firewall_blocks_odds_in_blind_payload():
    # The blind sports-model firewall rejects odds-like fields.
    with pytest.raises(firewall.FirewallViolation):
        firewall.scan_for_market_contamination({"decimal_odds": 1.95})
    # a 5Dollar normalized snapshot is explicitly market data (carries odds)
    quote = five.FiveDollarQuote(
        provider=five.PROVIDER,
        provider_fixture_id="1",
        fixture_id=FIXTURE_ID,
        competition_id="FRA1",
        bookmaker="Bet 365",
        market=five.MARKET_ASIAN_HANDICAP,
        phase="opening",
        selection="HOME",
        line=-0.25,
        decimal_odds=1.95,
        captured_at="2026-09-20T00:00:00+00:00",
    )
    snap = quote.to_normalized_snapshot()
    assert snap.market == "asian_handicap"
    assert snap.decimal_odds == 1.95


def test_phase_encoded_in_provider_market_id(monkeypatch):
    provider, _, _ = _provider(monkeypatch, fixtures=[_event()], odds=_odds())
    quotes = provider.get_fixture_quotes(FIXTURE_ID, fixture_context=CONTEXT)
    opening = next(q for q in quotes if q.phase == "opening" and q.market == five.MARKET_ASIAN_HANDICAP and q.selection == "HOME")
    assert opening.provider_market_id() == "Bet 365:asian_handicap:opening:-0.25"


def test_compare_provider_snapshots_is_read_only(monkeypatch):
    primary = [{"market": "asian_handicap", "selection": "HOME", "line": -0.25, "decimal_odds": 2.0}]
    secondary = [{"market": "asian_handicap", "selection": "HOME", "line": -0.25, "decimal_odds": 1.95}]
    rows = five.compare_provider_snapshots(primary, secondary)
    assert rows and rows[0]["api_football"] == [2.0] and rows[0]["five_dollar"] == [1.95]
