"""Focused tests for the The Odds API 1X2 provider foundation (mocked HTTP)."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from sports_ai.dashboard.services import DashboardConfig, _default_market_provider
from sports_ai.market_data.providers import MarketProviderError, NotConfiguredMarketProvider
from sports_ai.market_data.the_odds_api import (
    COMPETITION_TO_SPORT,
    STATE_AUTH_ERROR,
    STATE_QUOTA_EXHAUSTED,
    STATE_QUOTA_GUARD,
    STATE_RATE_LIMITED,
    TheOddsApiProvider,
    TheOddsApiProviderConfig,
    sanitize_url,
)

FIXTURE_ID = "football_data_fixture_1"
KICKOFF = "2026-08-23T14:00:00+00:00"

FIXTURE_CONTEXT = {
    "fixture_id": FIXTURE_ID,
    "competition_id": "ENG1",
    "competition_name": "Premier League",
    "division_code": "E0",
    "season": "2026",
    "home_team_name": "Manchester United",
    "away_team_name": "Liverpool",
    "kickoff_utc": KICKOFF,
}

EVENT = {
    "id": "epl_event_123",
    "sport_key": "soccer_epl",
    "sport_title": "EPL",
    "commence_time": KICKOFF,
    "home_team": "Manchester United",
    "away_team": "Liverpool",
    "bookmakers": [
        {
            "key": "pinnacle",
            "title": "Pinnacle",
            "last_update": "2026-08-22T12:00:00+00:00",
            "markets": [
                {
                    "key": "h2h",
                    "last_update": "2026-08-22T12:00:00+00:00",
                    "outcomes": [
                        {"name": "Manchester United", "price": 2.10},
                        {"name": "Draw", "price": 3.50},
                        {"name": "Liverpool", "price": 3.20},
                    ],
                }
            ],
        }
    ],
}

QUOTA_HEADERS = {"x-requests-remaining": "495", "x-requests-used": "5", "x-requests-last": "1"}


TOTALS_MARKET_2_5 = {
    "key": "totals",
    "last_update": "2026-08-22T12:00:00+00:00",
    "outcomes": [
        {"name": "Over", "price": 1.95, "point": 2.5},
        {"name": "Under", "price": 1.91, "point": 2.5},
    ],
}

EVENT_WITH_TOTALS = {
    "id": "epl_event_123",
    "sport_key": "soccer_epl",
    "sport_title": "EPL",
    "commence_time": KICKOFF,
    "home_team": "Manchester United",
    "away_team": "Liverpool",
    "bookmakers": [
        {
            "key": "pinnacle",
            "title": "Pinnacle",
            "last_update": "2026-08-22T12:00:00+00:00",
            "markets": [
                {
                    "key": "h2h",
                    "last_update": "2026-08-22T12:00:00+00:00",
                    "outcomes": [
                        {"name": "Manchester United", "price": 2.10},
                        {"name": "Draw", "price": 3.50},
                        {"name": "Liverpool", "price": 3.20},
                    ],
                },
                TOTALS_MARKET_2_5,
            ],
        }
    ],
}


def _make_provider(handler, config=None):
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    return TheOddsApiProvider(config=config, client=client)


def _odds_handler(calls, events=None, headers=None, status=200):
    def handler(request):
        calls["count"] += 1
        if request.url.path.endswith("/sports/soccer_epl/odds"):
            return httpx.Response(status, json=events if events is not None else [EVENT], headers=headers or QUOTA_HEADERS, request=request)
        return httpx.Response(404, request=request)

    return handler


def test_provider_missing_key_not_configured(monkeypatch):
    monkeypatch.delenv("SPORTS_AI_THE_ODDS_API_KEY", raising=False)
    provider = TheOddsApiProvider()
    assert provider.configured() is False
    diagnostics = provider.diagnostics()
    assert diagnostics["state"] == "NOT_CONFIGURED"
    assert diagnostics["configured"] is False


def test_provider_configured_with_key(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    provider = TheOddsApiProvider()
    assert provider.configured() is True


def test_configured_but_unpolled_reports_idle(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    provider = TheOddsApiProvider()
    diagnostics = provider.diagnostics()
    assert diagnostics["configured"] is True
    assert diagnostics["state"] == "IDLE"


def test_provider_selection_prefers_the_odds_api(monkeypatch, tmp_path):
    monkeypatch.setenv("SPORTS_AI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    config = replace(DashboardConfig.default(), current_odds_connected=True)
    provider = _default_market_provider(config)
    assert isinstance(provider, TheOddsApiProvider)
    assert provider.provider_name() == "the-odds-api"


def test_provider_selection_does_not_fall_back_to_api_football(monkeypatch, tmp_path):
    monkeypatch.setenv("SPORTS_AI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPORTS_AI_API_FOOTBALL_TOKEN", "unrelated-token")
    monkeypatch.delenv("SPORTS_AI_THE_ODDS_API_KEY", raising=False)
    config = replace(DashboardConfig.default(), current_odds_connected=True)
    provider = _default_market_provider(config)
    assert isinstance(provider, NotConfiguredMarketProvider)
    assert provider.provider_name() == "no-odds-provider-key"


def test_provider_selection_not_configured_without_approval(monkeypatch, tmp_path):
    monkeypatch.setenv("SPORTS_AI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    config = DashboardConfig.default()
    assert config.current_odds_connected is False
    provider = _default_market_provider(config)
    assert isinstance(provider, NotConfiguredMarketProvider)


def test_odds_sync_defaults_off(monkeypatch, tmp_path):
    monkeypatch.setenv("SPORTS_AI_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SPORTS_AI_ENABLE_ODDS_SYNC", raising=False)
    config = DashboardConfig.default()
    assert config.enable_odds_sync is False


def test_odds_sync_env_enables(monkeypatch, tmp_path):
    monkeypatch.setenv("SPORTS_AI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPORTS_AI_ENABLE_ODDS_SYNC", "true")
    config = DashboardConfig.default()
    assert config.enable_odds_sync is True


def test_h2h_normalization_home_draw_away(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    provider = _make_provider(_odds_handler({"count": 0}))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    by_selection = {snapshot.selection: snapshot for snapshot in snapshots}
    assert set(by_selection) == {"HOME", "DRAW", "AWAY"}
    assert by_selection["HOME"].decimal_odds == pytest.approx(2.10)
    assert by_selection["DRAW"].decimal_odds == pytest.approx(3.50)
    assert by_selection["AWAY"].decimal_odds == pytest.approx(3.20)
    assert all(snapshot.market == "1x2" for snapshot in snapshots)
    assert all(snapshot.provider == "the-odds-api" for snapshot in snapshots)
    assert all(snapshot.provider_fixture_id == "epl_event_123" for snapshot in snapshots)
    assert all(snapshot.bookmaker == "pinnacle" for snapshot in snapshots)


def test_soccer_draw_preserved(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    provider = _make_provider(_odds_handler({"count": 0}))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    draws = [snapshot for snapshot in snapshots if snapshot.selection == "DRAW"]
    assert len(draws) == 1
    assert draws[0].decimal_odds == pytest.approx(3.50)
    assert draws[0].decimal_odds > 1.0


def test_one_region_configuration(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    monkeypatch.setenv("SPORTS_AI_ODDS_REGION", "eu")
    captured = {}

    def handler(request):
        captured["regions"] = request.url.params.get("regions")
        captured["markets"] = request.url.params.get("markets")
        captured["oddsFormat"] = request.url.params.get("oddsFormat")
        return httpx.Response(200, json=[EVENT], headers=QUOTA_HEADERS, request=request)

    provider = _make_provider(handler)
    provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    assert captured["regions"] == "eu"
    assert captured["markets"] == "h2h,totals"
    assert captured["oddsFormat"] == "decimal"


def test_default_region_is_uk(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    monkeypatch.delenv("SPORTS_AI_ODDS_REGION", raising=False)
    captured = {}

    def handler(request):
        captured["regions"] = request.url.params.get("regions")
        return httpx.Response(200, json=[EVENT], headers=QUOTA_HEADERS, request=request)

    provider = _make_provider(handler)
    provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    assert captured["regions"] == "uk"


def test_sanitize_url_redacts_api_key():
    url = "https://api.the-odds-api.com/v4/sports/soccer_epl/odds?apiKey=SECRET123&regions=uk"
    sanitized = sanitize_url(url)
    assert "SECRET123" not in sanitized
    assert "apiKey=REDACTED" in sanitized


def test_diagnostics_do_not_leak_key(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-secret-key")
    provider = _make_provider(_odds_handler({"count": 0}))
    provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    serialized = json.dumps(provider.diagnostics())
    assert "test-secret-key" not in serialized


def test_short_cache_prevents_duplicate_fetches(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    calls = {"count": 0}
    provider = _make_provider(_odds_handler(calls))
    ctx_2 = dict(FIXTURE_CONTEXT, fixture_id="football_data_fixture_2")
    provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    provider.get_market_snapshot("football_data_fixture_2", fixture_context=ctx_2)
    assert calls["count"] == 1


def test_auth_error_state(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")

    def handler(request):
        return httpx.Response(401, request=request)

    provider = _make_provider(handler)
    with pytest.raises(MarketProviderError):
        provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    assert provider.diagnostics()["state"] == STATE_AUTH_ERROR


def test_rate_limited_state(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")

    def handler(request):
        return httpx.Response(429, request=request)

    config = TheOddsApiProviderConfig(max_retries=1, retry_backoff_seconds=0)
    provider = _make_provider(handler, config=config)
    with pytest.raises(MarketProviderError):
        provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    assert provider.diagnostics()["state"] == STATE_RATE_LIMITED


def test_quota_headers_captured_and_guard(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    headers = {"x-requests-remaining": "5", "x-requests-used": "95", "x-requests-last": "1"}
    config = TheOddsApiProviderConfig(min_quota_reserve=10)
    provider = _make_provider(_odds_handler({"count": 0}, headers=headers), config=config)
    provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    diagnostics = provider.diagnostics()
    assert diagnostics["state"] == STATE_QUOTA_GUARD
    assert diagnostics["quota"]["remaining"] == 5
    assert diagnostics["quota"]["used"] == 95
    assert diagnostics["quota"]["last_request_cost"] == 1


def test_quota_exhausted_state(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    headers = {"x-requests-remaining": "0", "x-requests-used": "100", "x-requests-last": "1"}
    provider = _make_provider(_odds_handler({"count": 0}, headers=headers))
    provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    assert provider.diagnostics()["state"] == STATE_QUOTA_EXHAUSTED


def test_ambiguous_event_protection(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    duplicate = dict(EVENT, id="epl_event_999")
    provider = _make_provider(_odds_handler({"count": 0}, events=[EVENT, duplicate]))
    with pytest.raises(MarketProviderError) as exc:
        provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    assert "AMBIGUOUS" in str(exc.value)


def test_unmatched_event(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    other = dict(EVENT, id="epl_event_456", home_team="Arsenal", away_team="Chelsea")
    provider = _make_provider(_odds_handler({"count": 0}, events=[other]))
    with pytest.raises(MarketProviderError) as exc:
        provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    assert "UNRESOLVED" in str(exc.value)


def test_team_alias_differences_match_via_canonical(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    provider = _make_provider(_odds_handler({"count": 0}))
    context = dict(FIXTURE_CONTEXT, home_team_name="Manchester Utd")
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=context)
    assert {snapshot.selection for snapshot in snapshots} == {"HOME", "DRAW", "AWAY"}


def test_kickoff_tolerance_out_of_window(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    shifted = dict(EVENT, commence_time="2026-08-23T15:30:00+00:00")
    provider = _make_provider(_odds_handler({"count": 0}, events=[shifted]))
    with pytest.raises(MarketProviderError) as exc:
        provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    assert "UNRESOLVED" in str(exc.value)


def test_competition_mapping_verified_and_unverified():
    assert COMPETITION_TO_SPORT["ENG1"].verified is True
    assert COMPETITION_TO_SPORT["ENG1"].enabled is True
    assert COMPETITION_TO_SPORT["ENG1"].sport_key == "soccer_epl"
    for competition in ("ENG2", "ENG3", "ENG4", "ESP1"):
        assert COMPETITION_TO_SPORT[competition].verified is False
        assert COMPETITION_TO_SPORT[competition].enabled is False


def test_unverified_competition_raises(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    provider = TheOddsApiProvider()
    context = dict(FIXTURE_CONTEXT, competition_id="ESP1")
    with pytest.raises(MarketProviderError) as exc:
        provider.get_market_snapshot(FIXTURE_ID, fixture_context=context)
    assert "UNVERIFIED" in str(exc.value)


# ---------------------------------------------------------------------------
# Goals totals normalization
# ---------------------------------------------------------------------------


def test_request_includes_h2h_and_totals(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    captured = {}

    def handler(request):
        captured["markets"] = request.url.params.get("markets")
        return httpx.Response(200, json=[EVENT_WITH_TOTALS], headers=QUOTA_HEADERS, request=request)

    provider = _make_provider(handler)
    provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    assert captured["markets"] == "h2h,totals"


def test_h2h_and_totals_coexist(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    provider = _make_provider(_odds_handler({"count": 0}, events=[EVENT_WITH_TOTALS]))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    by_market = {snapshot.market: [s for s in snapshots if s.market == snapshot.market] for snapshot in snapshots}
    assert set(by_market) == {"1x2", "goals_total"}
    assert {s.selection for s in by_market["1x2"]} == {"HOME", "DRAW", "AWAY"}
    assert {s.selection for s in by_market["goals_total"]} == {"OVER", "UNDER"}


def test_totals_over_normalization(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    provider = _make_provider(_odds_handler({"count": 0}, events=[EVENT_WITH_TOTALS]))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    over = [s for s in snapshots if s.market == "goals_total" and s.selection == "OVER"]
    assert len(over) == 1
    assert over[0].line == 2.5
    assert over[0].decimal_odds == pytest.approx(1.95)
    assert over[0].bookmaker == "pinnacle"
    assert over[0].provider == "the-odds-api"


def test_totals_under_normalization(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    provider = _make_provider(_odds_handler({"count": 0}, events=[EVENT_WITH_TOTALS]))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    under = [s for s in snapshots if s.market == "goals_total" and s.selection == "UNDER"]
    assert len(under) == 1
    assert under[0].line == 2.5
    assert under[0].decimal_odds == pytest.approx(1.91)
    assert under[0].bookmaker == "pinnacle"


def test_totals_distinct_lines_remain_distinct(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    event = {
        **EVENT_WITH_TOTALS,
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": "2026-08-22T12:00:00+00:00",
                "markets": [
                    {
                        "key": "totals",
                        "last_update": "2026-08-22T12:00:00+00:00",
                        "outcomes": [
                            {"name": "Over", "price": 1.95, "point": 2.5},
                            {"name": "Over", "price": 1.80, "point": 3.5},
                        ],
                    },
                ],
            }
        ],
    }
    provider = _make_provider(_odds_handler({"count": 0}, events=[event]))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    overs = [s for s in snapshots if s.market == "goals_total" and s.selection == "OVER"]
    assert sorted(s.line for s in overs) == [2.5, 3.5]
    ids = {s.provider_market_id for s in overs}
    assert len(ids) == 2
    assert any("2.5" in market_id for market_id in ids)
    assert any("3.5" in market_id for market_id in ids)


def test_totals_integer_line_preserved(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    event = {
        **EVENT_WITH_TOTALS,
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": "2026-08-22T12:00:00+00:00",
                "markets": [
                    {
                        "key": "totals",
                        "last_update": "2026-08-22T12:00:00+00:00",
                        "outcomes": [{"name": "Over", "price": 2.02, "point": 3}],
                    },
                ],
            }
        ],
    }
    provider = _make_provider(_odds_handler({"count": 0}, events=[event]))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    over = [s for s in snapshots if s.market == "goals_total" and s.selection == "OVER"]
    assert len(over) == 1
    assert over[0].line == 3.0
    assert over[0].decimal_odds == pytest.approx(2.02)


def test_totals_bookmaker_identity_preserved(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    event = {
        **EVENT_WITH_TOTALS,
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": "2026-08-22T12:00:00+00:00",
                "markets": [
                    {
                        "key": "totals",
                        "last_update": "2026-08-22T12:00:00+00:00",
                        "outcomes": [
                            {"name": "Over", "price": 1.95, "point": 2.5},
                            {"name": "Under", "price": 1.91, "point": 2.5},
                        ],
                    },
                ],
            },
            {
                "key": "bet365",
                "title": "Bet365",
                "last_update": "2026-08-22T12:00:00+00:00",
                "markets": [
                    {
                        "key": "totals",
                        "last_update": "2026-08-22T12:00:00+00:00",
                        "outcomes": [
                            {"name": "Over", "price": 2.00, "point": 2.5},
                            {"name": "Under", "price": 1.85, "point": 2.5},
                        ],
                    },
                ],
            },
        ],
    }
    provider = _make_provider(_odds_handler({"count": 0}, events=[event]))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    goals = [s for s in snapshots if s.market == "goals_total"]
    assert {s.bookmaker for s in goals} == {"pinnacle", "bet365"}


def test_totals_timestamps_preserved(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    provider = _make_provider(_odds_handler({"count": 0}, events=[EVENT_WITH_TOTALS]))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    goals = [s for s in snapshots if s.market == "goals_total"]
    assert all(s.source_timestamp == "2026-08-22T12:00:00+00:00" for s in goals)
    assert len({s.captured_at for s in goals}) == 1
    assert all(s.captured_at for s in goals)


def test_totals_missing_point_fails_safely(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    event = {
        **EVENT_WITH_TOTALS,
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": "2026-08-22T12:00:00+00:00",
                "markets": [
                    {
                        "key": "totals",
                        "last_update": "2026-08-22T12:00:00+00:00",
                        "outcomes": [{"name": "Over", "price": 1.95}],
                    },
                ],
            }
        ],
    }
    provider = _make_provider(_odds_handler({"count": 0}, events=[event]))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    assert not any(s.market == "goals_total" for s in snapshots)


def test_totals_malformed_point_fails_safely(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    event = {
        **EVENT_WITH_TOTALS,
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": "2026-08-22T12:00:00+00:00",
                "markets": [
                    {
                        "key": "totals",
                        "last_update": "2026-08-22T12:00:00+00:00",
                        "outcomes": [{"name": "Over", "price": 1.95, "point": "not-a-number"}],
                    },
                ],
            }
        ],
    }
    provider = _make_provider(_odds_handler({"count": 0}, events=[event]))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    assert not any(s.market == "goals_total" for s in snapshots)


def test_totals_negative_point_fails_safely(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    event = {
        **EVENT_WITH_TOTALS,
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": "2026-08-22T12:00:00+00:00",
                "markets": [
                    {
                        "key": "totals",
                        "last_update": "2026-08-22T12:00:00+00:00",
                        "outcomes": [{"name": "Over", "price": 1.95, "point": -2.5}],
                    },
                ],
            }
        ],
    }
    provider = _make_provider(_odds_handler({"count": 0}, events=[event]))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    assert not any(s.market == "goals_total" for s in snapshots)


def test_totals_non_quarter_step_line_skipped(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    event = {
        **EVENT_WITH_TOTALS,
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": "2026-08-22T12:00:00+00:00",
                "markets": [
                    {
                        "key": "totals",
                        "last_update": "2026-08-22T12:00:00+00:00",
                        "outcomes": [{"name": "Over", "price": 1.95, "point": 2.7}],
                    },
                ],
            }
        ],
    }
    provider = _make_provider(_odds_handler({"count": 0}, events=[event]))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    assert not any(s.market == "goals_total" for s in snapshots)


def test_totals_unknown_outcome_ignored(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    event = {
        **EVENT_WITH_TOTALS,
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": "2026-08-22T12:00:00+00:00",
                "markets": [
                    {
                        "key": "totals",
                        "last_update": "2026-08-22T12:00:00+00:00",
                        "outcomes": [
                            {"name": "Over", "price": 1.95, "point": 2.5},
                            {"name": "Draw", "price": 4.50, "point": 2.5},
                        ],
                    },
                ],
            }
        ],
    }
    provider = _make_provider(_odds_handler({"count": 0}, events=[event]))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    goals = [s for s in snapshots if s.market == "goals_total"]
    assert {s.selection for s in goals} == {"OVER"}


def test_totals_incomplete_pair_not_fabricated(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    event = {
        **EVENT_WITH_TOTALS,
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": "2026-08-22T12:00:00+00:00",
                "markets": [
                    {
                        "key": "totals",
                        "last_update": "2026-08-22T12:00:00+00:00",
                        "outcomes": [{"name": "Over", "price": 1.95, "point": 2.5}],
                    },
                ],
            }
        ],
    }
    provider = _make_provider(_odds_handler({"count": 0}, events=[event]))
    snapshots = provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    goals = [s for s in snapshots if s.market == "goals_total"]
    assert {s.selection for s in goals} == {"OVER"}
    assert not any(s.selection == "UNDER" for s in goals)


def test_day12_constants_unchanged():
    from sports_ai.experiments.football_day12_safeguards import (
        DAY12_BLOCKED_MISSING_XG_DATA,
        XG_APPLICABLE_COMPETITIONS,
    )

    assert DAY12_BLOCKED_MISSING_XG_DATA == "BLOCKED_MISSING_XG_DATA"
    assert "ENG1" in XG_APPLICABLE_COMPETITIONS
    assert "ENG2" in XG_APPLICABLE_COMPETITIONS
    assert "ESP1" in XG_APPLICABLE_COMPETITIONS
