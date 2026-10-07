"""Focused tests for explicit current-market provider selection and API-Football safety.

No live provider calls are made. These tests exercise the explicit
``SPORTS_AI_CURRENT_MARKET_PROVIDER`` selector, fail-closed behavior, the shared
provider request budget, deterministic fixture identity (no mapping fan-out) and
the single-fixture API-Football current-odds normalization path.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sports_ai.dashboard import (
    services,  # noqa: F401  (pre-import breaks the market_data/dashboard cycle)
)
from sports_ai.dashboard.provider_budget import (
    ACTION_BLOCKED_QUOTA,
    BudgetDecision,
    ProviderRequestBudget,
)
from sports_ai.dashboard.services import (
    CURRENT_MARKET_PROVIDER_INVALID,
    DashboardConfig,
    _default_market_provider,
    resolve_current_market_provider_name,
)
from sports_ai.market_data.api_football import ApiFootballOddsProvider
from sports_ai.market_data.providers import (
    MarketProviderError,
    NotConfiguredMarketProvider,
)
from sports_ai.market_data.the_odds_api import TheOddsApiProvider
from sports_ai.storage import football_paper_trading as pt

FIXTURE_ID = "api-football:999"
KICKOFF = "2026-10-21T18:00:00+00:00"
FIXTURE_CONTEXT = {
    "fixture_id": FIXTURE_ID,
    "provider": "api-football",
    "provider_fixture_id": "999",
    "competition_id": "ESP1",
    "competition_name": "Primera Division",
    "division_code": "SP1",
    "season": "2026",
    "home_team_name": "Levante",
    "away_team_name": "Athletic Club",
    "kickoff_utc": KICKOFF,
}

_ODDS_PAYLOAD = {
    "response": [
        {
            "fixture": {"id": "999"},
            "update": "2026-10-21T12:00:00+00:00",
            "bookmakers": [
                {
                    "id": 1,
                    "name": "Pinnacle",
                    "bets": [
                        {
                            "id": 1,
                            "name": "Asian Handicap",
                            "values": [
                                {"value": "Home -0.5", "odd": "1.90"},
                                {"value": "Away +0.5", "odd": "1.95"},
                            ],
                        },
                        {
                            "id": 2,
                            "name": "Corners Over Under",
                            "values": [
                                {"value": "Over 9.5", "odd": "1.85"},
                                {"value": "Under 9.5", "odd": "1.95"},
                            ],
                        },
                    ],
                }
            ],
        }
    ]
}


def _config(tmp_path) -> DashboardConfig:
    return replace(
        DashboardConfig.default(),
        dashboard_db_path=tmp_path / "dashboard.sqlite",
        paper_trading_db_path=tmp_path / "paper.sqlite",
    )


# --------------------------------------------------------------------------- selection
def test_explicit_api_football_selection(monkeypatch, tmp_path):
    monkeypatch.setenv("SPORTS_AI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPORTS_AI_CURRENT_MARKET_PROVIDER", "api-football")
    monkeypatch.setenv("SPORTS_AI_API_FOOTBALL_TOKEN", "test-token")
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    provider = _default_market_provider(_config(tmp_path))
    assert isinstance(provider, ApiFootballOddsProvider)
    assert provider.provider_name() == "api-football"


def test_explicit_api_football_never_falls_through_to_the_odds_api(monkeypatch, tmp_path):
    monkeypatch.setenv("SPORTS_AI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPORTS_AI_CURRENT_MARKET_PROVIDER", "api-football")
    monkeypatch.delenv("SPORTS_AI_API_FOOTBALL_TOKEN", raising=False)
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    provider = _default_market_provider(_config(tmp_path))
    assert isinstance(provider, NotConfiguredMarketProvider)
    assert provider.provider_name() == "api-football-not-configured"
    assert provider.configured() is False


def test_invalid_provider_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("SPORTS_AI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPORTS_AI_CURRENT_MARKET_PROVIDER", "definitely-not-a-provider")
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    monkeypatch.setenv("SPORTS_AI_API_FOOTBALL_TOKEN", "test-token")
    assert resolve_current_market_provider_name() == CURRENT_MARKET_PROVIDER_INVALID
    provider = _default_market_provider(_config(tmp_path))
    assert isinstance(provider, NotConfiguredMarketProvider)
    assert provider.configured() is False
    with pytest.raises(MarketProviderError, match="NOT_CONFIGURED"):
        provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)


def test_off_provider_makes_zero_requests(monkeypatch, tmp_path):
    monkeypatch.setenv("SPORTS_AI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPORTS_AI_CURRENT_MARKET_PROVIDER", "off")
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    monkeypatch.setenv("SPORTS_AI_API_FOOTBALL_TOKEN", "test-token")
    provider = _default_market_provider(_config(tmp_path))
    assert isinstance(provider, NotConfiguredMarketProvider)
    assert provider.configured() is False
    with pytest.raises(MarketProviderError, match="NOT_CONFIGURED"):
        provider.get_market_snapshot(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)


def test_the_odds_api_still_selectable_explicitly(monkeypatch, tmp_path):
    monkeypatch.setenv("SPORTS_AI_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SPORTS_AI_CURRENT_MARKET_PROVIDER", "the-odds-api")
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    provider = _default_market_provider(_config(tmp_path))
    assert isinstance(provider, TheOddsApiProvider)


# --------------------------------------------------------------------------- budget gates
def _forbid_network(monkeypatch):
    from sports_ai.market_data import api_football

    def _raise(*args, **kwargs):
        raise AssertionError("unexpected network call")

    monkeypatch.setattr(api_football.httpx, "Client", _raise)


def test_paid_call_gate_blocks_api_football_request(monkeypatch, tmp_path):
    monkeypatch.setenv("SPORTS_AI_API_FOOTBALL_TOKEN", "test-token")
    budget = ProviderRequestBudget(tmp_path / "dash.sqlite", allow_paid_provider_calls=False)
    provider = ApiFootballOddsProvider()
    provider.attach_request_budget(budget)
    _forbid_network(monkeypatch)
    snapshots = provider.get_fixture_markets(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    assert snapshots == []


def test_quota_gate_blocks_api_football_request(monkeypatch, tmp_path):
    monkeypatch.setenv("SPORTS_AI_API_FOOTBALL_TOKEN", "test-token")
    budget = ProviderRequestBudget(tmp_path / "dash.sqlite", allow_paid_provider_calls=True)
    monkeypatch.setattr(
        budget,
        "issue",
        lambda **kwargs: BudgetDecision(
            allow=False,
            action=ACTION_BLOCKED_QUOTA,
            reason="quota state EXHAUSTED",
            quota_state="EXHAUSTED",
        ),
    )
    provider = ApiFootballOddsProvider()
    provider.attach_request_budget(budget)
    _forbid_network(monkeypatch)
    snapshots = provider.get_fixture_markets(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    assert snapshots == []


# --------------------------------------------------------------------------- identity
def test_deterministic_provider_id_avoids_mapping_fanout(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_API_FOOTBALL_TOKEN", "test-token")
    provider = ApiFootballOddsProvider()
    mapping_calls: list[int] = []
    monkeypatch.setattr(provider, "_mapping_rows", lambda: mapping_calls.append(1) or [])
    requests: list[dict] = []

    def _fake_request_json(**kwargs):
        requests.append(kwargs)
        return _ODDS_PAYLOAD

    monkeypatch.setattr(provider, "_request_json", _fake_request_json)
    snapshots = provider.get_fixture_markets(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    assert mapping_calls == []  # deterministic id => no /odds/mapping fan-out
    assert len(requests) == 1  # exactly one /odds?fixture=... request
    assert requests[0]["params"] == {"fixture": "999"}
    assert "date" not in requests[0]["params"]  # never a historical date param

    markets = {(s.market, s.selection, s.line) for s in snapshots}
    assert ("asian_handicap", "HOME", -0.5) in markets
    assert ("asian_handicap", "AWAY", -0.5) in markets
    assert ("corner_total", "OVER", 9.5) in markets
    assert ("corner_total", "UNDER", 9.5) in markets
    assert all(s.provider == "api-football" for s in snapshots)
    assert all(s.provider_fixture_id == "999" for s in snapshots)
    assert all(s.bookmaker == "Pinnacle" for s in snapshots)


def test_missing_identity_raises_without_mapping_fanout(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_API_FOOTBALL_TOKEN", "test-token")
    monkeypatch.delenv("SPORTS_AI_API_FOOTBALL_LEAGUE_MAP", raising=False)
    provider = ApiFootballOddsProvider()
    mapping_calls: list[int] = []
    monkeypatch.setattr(provider, "_mapping_rows", lambda: mapping_calls.append(1) or [])
    missing_identity_context = {k: v for k, v in FIXTURE_CONTEXT.items() if k not in {"provider", "provider_fixture_id"}}
    with pytest.raises(MarketProviderError, match="MARKET_FIXTURE_UNRESOLVED"):
        provider.get_fixture_markets(FIXTURE_ID, fixture_context=missing_identity_context)
    assert mapping_calls == []


def test_bookmaker_filter_is_deterministic(monkeypatch):
    monkeypatch.setenv("SPORTS_AI_API_FOOTBALL_TOKEN", "test-token")
    monkeypatch.setenv("SPORTS_AI_MARKET_REFERENCE_BOOKMAKERS", "pinnacle, betfair , bet365")
    provider = ApiFootballOddsProvider()
    payload = {
        "response": [
            {
                "fixture": {"id": "999"},
                "update": "2026-10-21T12:00:00+00:00",
                "bookmakers": [
                    {
                        "id": 7,
                        "name": "Some Other Book",
                        "bets": [
                            {
                                "id": 1,
                                "name": "Asian Handicap",
                                "values": [{"value": "Home -0.5", "odd": "1.90"}],
                            }
                        ],
                    },
                    {
                        "id": 1,
                        "name": "Pinnacle",
                        "bets": [
                            {
                                "id": 1,
                                "name": "Asian Handicap",
                                "values": [{"value": "Home -0.5", "odd": "1.85"}],
                            }
                        ],
                    },
                ],
            }
        ]
    }
    monkeypatch.setattr(provider, "_request_json", lambda **kwargs: payload)
    snapshots = provider.get_fixture_markets(FIXTURE_ID, fixture_context=FIXTURE_CONTEXT)
    ah = [s for s in snapshots if s.market == "asian_handicap"]
    assert {s.bookmaker for s in ah} == {"Some Other Book", "Pinnacle"}
    reference = {s.bookmaker for s in ah if s.reference_designation == "reference"}
    assert reference == {"Pinnacle"}  # deterministic reference designation by configured priority


# --------------------------------------------------------------------------- persistence
def test_new_provider_timestamp_persists_and_first_observed_initialised(monkeypatch):
    conn = pt.connect(":memory:")
    pt.ensure_schema(conn)
    snapshot = pt.StoredMarketSnapshot(
        fixture_id=FIXTURE_ID,
        provider="api-football",
        bookmaker="Pinnacle",
        market="asian_handicap",
        selection="HOME",
        odds=1.9,
        ingestion_version="api_football_current_market_provider_v1",
        line=-0.5,
        provider_timestamp="2026-10-21T12:00:00+00:00",
        captured_at="2026-10-21T12:04:00+00:00",
        kickoff_utc=KICKOFF,
    )
    pt.record_market_snapshot(conn, snapshot)
    row = pt.fetch_market_snapshots(conn, fixture_id=FIXTURE_ID)[0]
    assert row["provider_timestamp"] == "2026-10-21T12:00:00+00:00"
    assert row["captured_at"] == "2026-10-21T12:04:00+00:00"
    assert row["first_observed_at"] == "2026-10-21T12:04:00+00:00"
    assert row["last_observed_at"] == "2026-10-21T12:04:00+00:00"
    # Re-observation only advances last_observed_at.
    reobserved = replace(snapshot, captured_at="2026-10-21T12:34:00+00:00")
    assert pt.record_market_snapshot(conn, reobserved) == row["snapshot_id"]
    row2 = pt.fetch_market_snapshots(conn, fixture_id=FIXTURE_ID)[0]
    assert row2["first_observed_at"] == "2026-10-21T12:04:00+00:00"
    assert row2["last_observed_at"] == "2026-10-21T12:34:00+00:00"
    assert row2["captured_at"] == "2026-10-21T12:04:00+00:00"


def test_api_football_provider_uses_no_historical_endpoint():
    import inspect

    from sports_ai.market_data import api_football

    source = inspect.getsource(api_football)
    assert "?date=" not in source
    assert "odds?date" not in source
