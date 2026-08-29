"""Focused tests for the provider request budget (gate, cache, governor, breaker).

No live API-Football calls are made in these tests. Provider HTTP paths are
exercised with monkeypatched clients that raise if an HTTP call is attempted.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from sports_ai.dashboard import fixture_provider as fp_module
from sports_ai.dashboard.provider_budget import (
    ACTION_ALLOW,
    ACTION_BLOCKED_CIRCUIT,
    ACTION_BLOCKED_GATE,
    ACTION_BLOCKED_QUOTA,
    ACTION_CACHE_HIT,
    CIRCUIT_AUTH_ERROR,
    CIRCUIT_PROVIDER_ERROR,
    CIRCUIT_QUOTA_EXHAUSTED,
    CIRCUIT_RATE_LIMITED,
    PRIORITY_ESSENTIAL,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    PROVIDER_API_FOOTBALL,
    ProviderRequestBudget,
    classify_provider_response,
    odds_policy_for_kickoff,
    parse_quota_headers,
    request_key,
    schedule_fixture_policy,
)
from sports_ai.market_data import api_football as odds_module
from sports_ai.market_data.providers import NotConfiguredMarketProvider


def _budget(tmp_path: Path, *, allow: bool = True, **kwargs) -> ProviderRequestBudget:
    return ProviderRequestBudget(
        tmp_path / "dashboard.sqlite",
        allow_paid_provider_calls=allow,
        **kwargs,
    )


def _seed_quota(budget: ProviderRequestBudget, *, limit: int, remaining: int) -> None:
    budget._record_quota_from_headers(
        {
            "x-ratelimit-limit": str(limit),
            "x-ratelimit-remaining": str(remaining),
            "x-ratelimit-requests-limit": "10",
            "x-ratelimit-requests-remaining": "10",
        }
    )


def _payload(response_rows: list[dict] | None = None) -> dict:
    return {"response": response_rows or [], "results": len(response_rows or []), "errors": {}}


# ---------------------------------------------------------------- master gate
def test_gate_off_zero_http_calls(tmp_path):
    budget = _budget(tmp_path, allow=False)
    for priority in (PRIORITY_LOW, PRIORITY_NORMAL, PRIORITY_ESSENTIAL):
        decision = budget.issue(
            provider=PROVIDER_API_FOOTBALL,
            endpoint="fixtures",
            params={"date": "2026-08-30", "league": 39},
            ttl_seconds=60,
            priority=priority,
        )
        assert decision.action == ACTION_BLOCKED_GATE
        assert not decision.allow
    assert budget.metrics()["requests_blocked_gate"] == 3
    assert budget.metrics().get("http_requests", 0) == 0


def test_staging_safe_configuration_zero_calls_even_if_automated_sync_enabled(tmp_path):
    # Defense in depth: automated sync "enabled" is irrelevant when the paid
    # provider gate is OFF - every request is still blocked.
    budget = _budget(tmp_path, allow=False)
    decision = budget.issue(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params={"live": "all"},
        ttl_seconds=90,
        priority=PRIORITY_ESSENTIAL,
    )
    assert decision.action == ACTION_BLOCKED_GATE
    assert budget.metrics().get("http_requests", 0) == 0


# ------------------------------------------------------------ persistent cache
def test_same_request_key_within_ttl_one_provider_call(tmp_path):
    budget = _budget(tmp_path)
    params = {"date": "2026-08-30", "league": 39}
    d1 = budget.issue(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures", params=params, ttl_seconds=3600, priority=PRIORITY_NORMAL)
    assert d1.action == ACTION_ALLOW
    budget.finish_success(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params=params,
        payload=_payload([{"fixture": {"id": "1"}}]),
        provider_timestamp=None,
        ttl_seconds=3600,
        response_headers=None,
    )
    d2 = budget.issue(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures", params=params, ttl_seconds=3600, priority=PRIORITY_NORMAL)
    assert d2.action == ACTION_CACHE_HIT
    assert d2.cached_payload == _payload([{"fixture": {"id": "1"}}])
    assert budget.metrics()["http_requests"] == 1
    assert budget.metrics()["requests_cached"] == 1


def test_persistent_cache_survives_new_budget_instance(tmp_path):
    budget = _budget(tmp_path)
    params = {"id": "1557378"}
    d1 = budget.issue(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures", params=params, ttl_seconds=3600, priority=PRIORITY_ESSENTIAL)
    assert d1.action == ACTION_ALLOW
    budget.finish_success(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params=params,
        payload=_payload([{"fixture": {"id": "1557378"}}]),
        provider_timestamp="2026-08-30T00:00:00+00:00",
        ttl_seconds=3600,
        response_headers=None,
    )
    # A brand-new budget object on the same database (restart/redeploy).
    budget2 = _budget(tmp_path)
    d2 = budget2.issue(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures", params=params, ttl_seconds=3600, priority=PRIORITY_ESSENTIAL)
    assert d2.action == ACTION_CACHE_HIT
    # Cumulative persisted counter: only the original request, no new HTTP on restart.
    assert budget2.metrics()["http_requests"] == 1


def test_inflight_coalescing_prevents_double_fetch(tmp_path):
    budget = _budget(tmp_path)
    params = {"date": "2026-08-30", "league": 39}
    owner = budget.issue(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures", params=params, ttl_seconds=3600, priority=PRIORITY_NORMAL)
    assert owner.action == ACTION_ALLOW
    results: dict[str, object] = {}

    def waiter():
        results["decision"] = budget.issue(
            provider=PROVIDER_API_FOOTBALL,
            endpoint="fixtures",
            params=params,
            ttl_seconds=3600,
            priority=PRIORITY_NORMAL,
        )

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.1)  # let the waiter block on the in-flight owner
    budget.finish_success(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params=params,
        payload=_payload([{"fixture": {"id": "1"}}]),
        provider_timestamp=None,
        ttl_seconds=3600,
        response_headers=None,
    )
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert results["decision"].action == ACTION_CACHE_HIT
    assert budget.metrics()["http_requests"] == 1


# --------------------------------------------------------- adaptive lifecycle
def test_far_future_fixture_obeys_long_polling_interval():
    today = date(2026, 8, 30)
    ttl, priority = schedule_fixture_policy(today + timedelta(days=3), today)
    assert ttl == 8 * 60 * 60
    assert priority == PRIORITY_LOW


def test_near_kickoff_fixture_becomes_eligible_sooner():
    now = datetime.now(timezone.utc)
    near_ttl, near_priority = odds_policy_for_kickoff((now + timedelta(minutes=5)).isoformat(), now_utc=now)
    far_ttl, far_priority = odds_policy_for_kickoff((now + timedelta(hours=8)).isoformat(), now_utc=now)
    assert near_ttl < far_ttl
    assert near_priority == PRIORITY_ESSENTIAL
    assert far_priority == PRIORITY_LOW


def test_settled_fixture_does_not_repeat_poll(tmp_path):
    budget = _budget(tmp_path)
    params = {"fixture": "1557378"}
    d1 = budget.issue(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures/statistics", params=params, ttl_seconds=300, priority=PRIORITY_ESSENTIAL)
    assert d1.action == ACTION_ALLOW
    budget.finish_success(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures/statistics",
        params=params,
        payload=_payload([{"team": {"name": "Home"}}]),
        provider_timestamp="2026-08-30T00:00:00+00:00",
        ttl_seconds=300,
        response_headers=None,
    )
    d2 = budget.issue(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures/statistics", params=params, ttl_seconds=300, priority=PRIORITY_ESSENTIAL)
    assert d2.action == ACTION_CACHE_HIT
    assert budget.metrics()["http_requests"] == 1


# ----------------------------------------------------------- quota header + governor
def test_quota_header_parsed_and_persisted(tmp_path):
    budget = _budget(tmp_path)
    params = {"live": "all"}
    d = budget.issue(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures", params=params, ttl_seconds=90, priority=PRIORITY_ESSENTIAL)
    assert d.action == ACTION_ALLOW
    budget.finish_success(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params=params,
        payload=_payload([]),
        provider_timestamp=None,
        ttl_seconds=90,
        response_headers={
            "x-ratelimit-limit": "1000",
            "x-ratelimit-remaining": "850",
            "x-ratelimit-requests-limit": "10",
            "x-ratelimit-requests-remaining": "7",
        },
    )
    snapshot = budget.quota_snapshot()
    assert snapshot["daily_limit"] == 1000
    assert snapshot["daily_remaining"] == 850
    assert snapshot["minute_limit"] == 10
    assert snapshot["minute_remaining"] == 7
    assert snapshot["last_request_at"] is not None
    assert snapshot["quota_reset_at"] is not None
    # requests_used tracked for optional configured-budget fallback
    assert int(snapshot["requests_used"] or 0) == 1


def test_parse_quota_headers_case_insensitive():
    parsed = parse_quota_headers({"X-RateLimit-Limit": "500", "x-ratelimit-remaining": "300"})
    assert parsed["daily_limit"] == 500
    assert parsed["daily_remaining"] == 300


def test_healthy_governor_allows_eligible_request(tmp_path):
    budget = _budget(tmp_path)
    _seed_quota(budget, limit=1000, remaining=700)
    assert budget.quota_state() == "HEALTHY"
    decision = budget.issue(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures", params={"live": "all"}, ttl_seconds=90, priority=PRIORITY_LOW)
    assert decision.action == ACTION_ALLOW


def test_conserve_suppresses_low_priority_request(tmp_path):
    budget = _budget(tmp_path)
    _seed_quota(budget, limit=1000, remaining=300)
    assert budget.quota_state() == "CONSERVE"
    low = budget.issue(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures", params={"date": "2026-09-02", "league": 39}, ttl_seconds=3600, priority=PRIORITY_LOW)
    assert low.action == ACTION_BLOCKED_QUOTA
    normal = budget.issue(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures", params={"date": "2026-08-30", "league": 39}, ttl_seconds=1800, priority=PRIORITY_NORMAL)
    assert normal.action == ACTION_ALLOW


def test_low_suppresses_low_keeps_normal(tmp_path):
    budget = _budget(tmp_path)
    _seed_quota(budget, limit=1000, remaining=150)
    assert budget.quota_state() == "LOW"
    assert budget.issue(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures", params={"x": 1}, ttl_seconds=3600, priority=PRIORITY_LOW).action == ACTION_BLOCKED_QUOTA
    assert budget.issue(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures", params={"x": 2}, ttl_seconds=1800, priority=PRIORITY_NORMAL).action == ACTION_ALLOW
    assert budget.issue(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures", params={"x": 3}, ttl_seconds=600, priority=PRIORITY_ESSENTIAL).action == ACTION_ALLOW


def test_critical_permits_only_essential(tmp_path):
    budget = _budget(tmp_path)
    _seed_quota(budget, limit=1000, remaining=50)
    assert budget.quota_state() == "CRITICAL"
    assert budget.issue(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures", params={"x": 1}, ttl_seconds=1800, priority=PRIORITY_NORMAL).action == ACTION_BLOCKED_QUOTA
    assert budget.issue(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures", params={"x": 2}, ttl_seconds=3600, priority=PRIORITY_LOW).action == ACTION_BLOCKED_QUOTA
    assert budget.issue(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures", params={"x": 3}, ttl_seconds=600, priority=PRIORITY_ESSENTIAL).action == ACTION_ALLOW


def test_exhausted_governor_blocks_all_subsequent_requests(tmp_path):
    budget = _budget(tmp_path)
    _seed_quota(budget, limit=1000, remaining=0)
    assert budget.quota_state() == "EXHAUSTED"
    for priority in (PRIORITY_LOW, PRIORITY_NORMAL, PRIORITY_ESSENTIAL):
        decision = budget.issue(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures", params={"live": "all"}, ttl_seconds=90, priority=priority)
        assert decision.action == ACTION_BLOCKED_QUOTA
    assert budget.metrics().get("http_requests", 0) == 0


def test_configured_daily_budget_used_as_fallback_without_headers(tmp_path):
    budget = _budget(tmp_path, allow=True, daily_budget=1000)
    # No headers captured yet -> configured budget provides the allowance.
    assert budget.quota_state() == "HEALTHY"
    decision = budget.issue(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures", params={"live": "all"}, ttl_seconds=90, priority=PRIORITY_ESSENTIAL)
    assert decision.action == ACTION_ALLOW
    budget.finish_success(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params={"live": "all"},
        payload=_payload([]),
        provider_timestamp=None,
        ttl_seconds=90,
        response_headers=None,
    )
    snapshot = budget.quota_snapshot()
    assert int(snapshot["requests_used"] or 0) == 1
    assert budget.snapshot()["quota"]["configured_daily_budget"] == 1000


# ------------------------------------------------------------- circuit breaker
def test_rate_limited_opens_backoff(tmp_path):
    budget = _budget(tmp_path)
    budget.finish_failure(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params={"live": "all"},
        state=CIRCUIT_RATE_LIMITED,
        message="HTTP 429: rate limited",
        response_headers=None,
    )
    decision = budget.issue(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures", params={"live": "all"}, ttl_seconds=90, priority=PRIORITY_ESSENTIAL)
    assert decision.action == ACTION_BLOCKED_CIRCUIT
    assert decision.reason and "RATE_LIMITED" in decision.reason


def test_circuit_reopens_after_backoff_expiry(tmp_path):
    budget = _budget(tmp_path)
    budget.finish_failure(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params={"live": "all"},
        state=CIRCUIT_RATE_LIMITED,
        message="HTTP 429: rate limited",
        response_headers=None,
    )
    # Simulate backoff expiry by moving blocked_until into the past.
    past = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat(timespec="seconds")
    with budget._connect() as conn:
        conn.execute(
            "UPDATE provider_circuit_state SET blocked_until = ? WHERE provider = ?",
            (past, PROVIDER_API_FOOTBALL),
        )
    decision = budget.issue(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures", params={"live": "all"}, ttl_seconds=90, priority=PRIORITY_ESSENTIAL)
    assert decision.action == ACTION_ALLOW


def test_market_not_available_does_not_open_global_circuit(tmp_path):
    state, message, _ = classify_provider_response(
        http_status=200,
        payload={"errors": {}, "response": [], "results": 0},
        response_headers=None,
    )
    assert state is None
    budget = _budget(tmp_path)
    budget.finish_failure(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="odds",
        params={"fixture": "1557378"},
        state=None,
        message="MARKET_NOT_AVAILABLE",
    )
    decision = budget.issue(provider=PROVIDER_API_FOOTBALL, endpoint="odds", params={"fixture": "1557378"}, ttl_seconds=60, priority=PRIORITY_NORMAL)
    assert decision.action == ACTION_ALLOW


def test_auth_error_and_provider_error_recorded(tmp_path):
    budget = _budget(tmp_path)
    budget.finish_failure(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures", params={"x": 1}, state=CIRCUIT_AUTH_ERROR, message="HTTP 401")
    assert budget.circuit_snapshot()["state"] == CIRCUIT_AUTH_ERROR
    budget2 = _budget(tmp_path, allow=True)
    budget2._ensure_schema()
    with budget2._connect() as conn:
        conn.execute("DELETE FROM provider_circuit_state WHERE provider = ?", (PROVIDER_API_FOOTBALL,))
    budget2.finish_failure(provider=PROVIDER_API_FOOTBALL, endpoint="fixtures", params={"x": 2}, state=CIRCUIT_PROVIDER_ERROR, message="HTTP 500")
    assert budget2.circuit_snapshot()["state"] == CIRCUIT_PROVIDER_ERROR


# ----------------------------------------------------------------- provider gate enforcement
class _FailingClient:
    """Raises if an HTTP request is actually attempted."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("HTTP call attempted while paid provider gate should have blocked it")


@pytest.fixture
def api_football_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SPORTS_AI_API_FOOTBALL_TOKEN", "test-token")
    league_map = tmp_path / "league_map.json"
    league_map.write_text(json.dumps({"E0": 39, "E1": 40}), encoding="utf-8")
    monkeypatch.setenv("SPORTS_AI_API_FOOTBALL_LEAGUE_MAP", str(league_map))


def test_startup_gate_off_performs_zero_provider_http_calls(tmp_path, monkeypatch, api_football_env):
    monkeypatch.setattr(fp_module.httpx, "Client", _FailingClient)
    budget = _budget(tmp_path, allow=False)
    provider = fp_module.ApiFootballFixtureProvider()
    provider.attach_request_budget(budget)
    live = provider.get_live_fixtures()
    assert live == []
    assert budget.metrics().get("http_requests", 0) == 0
    assert budget.metrics()["requests_blocked_gate"] >= 1


def test_odds_sync_cannot_bypass_paid_provider_gate(tmp_path, monkeypatch, api_football_env):
    monkeypatch.setattr(odds_module.httpx, "Client", _FailingClient)
    budget = _budget(tmp_path, allow=False)
    provider = odds_module.ApiFootballOddsProvider()
    provider.attach_request_budget(budget)
    fixture_context = {
        "fixture_id": "api-football:1557378",
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "division_code": "E0",
        "season": "2026",
        "home_team_name": "Home FC",
        "away_team_name": "Away FC",
        "kickoff_utc": "2026-08-30T14:00:00+00:00",
        "provider": "api-football",
        "provider_fixture_id": "1557378",
    }
    snapshots = provider.get_market_snapshot("api-football:1557378", fixture_context=fixture_context)
    assert snapshots == []
    assert budget.metrics().get("http_requests", 0) == 0


def test_manual_refresh_cannot_bypass_paid_provider_gate(tmp_path, monkeypatch, api_football_env):
    from sports_ai.dashboard.services import DashboardConfig, DashboardServices

    monkeypatch.setattr(fp_module.httpx, "Client", _FailingClient)
    config = DashboardConfig(
        repo_root=tmp_path.parents[1],
        research_db_path=tmp_path / "research.sqlite",
        dashboard_db_path=tmp_path / "dashboard.sqlite",
        paper_trading_db_path=tmp_path / "paper.sqlite",
        performance_output_dir=tmp_path / "performance",
        operational_data_dir=tmp_path,
        storage_mode="test_tmpdir",
        persistent_data_dir_configured=False,
        display_timezone="Asia/Manila",
        enable_scheduler=False,
        allow_paid_provider_calls=False,
        current_odds_connected=False,
        current_xg_connected=False,
    )
    services = DashboardServices(
        config=config,
        fixture_provider=fp_module.ApiFootballFixtureProvider(),
        market_provider=NotConfiguredMarketProvider(provider_label="test"),
    )
    result = asyncio.run(services.manual_refresh())
    assert result["refresh_state"] == "SYNCED"
    assert services._provider_budget.metrics().get("http_requests", 0) == 0
    assert services._provider_state["state"] == "PAID_PROVIDER_CALLS_DISABLED"


# ------------------------------------------- HTTP integration (gate ON, fake client)
class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, endpoint, params=None):
        self.calls += 1
        return self.responses.pop(0)


def _response(payload: dict, status_code: int = 200) -> "object":
    import httpx

    request = httpx.Request("GET", "https://v3.football.api-sports.io/odds")
    return httpx.Response(status_code, json=payload, headers={"x-ratelimit-limit": "1000", "x-ratelimit-remaining": "999"}, request=request)


def test_odds_provider_single_call_then_persistent_cache_hit(tmp_path, monkeypatch, api_football_env):
    import httpx

    payload = {
        "response": [
            {
                "fixture": {"id": "1557378"},
                "update": "2026-08-30T00:00:00+00:00",
                "bookmakers": [
                    {"id": 1, "name": "Pinnacle", "bets": [{"id": 1, "name": "Match Winner", "values": [{"value": "Home", "odd": "2.00"}]}]}
                ],
            }
        ],
        "results": 1,
        "errors": {},
    }
    fake = _FakeClient([_response(payload)])
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: fake)
    budget = _budget(tmp_path, allow=True)
    provider = odds_module.ApiFootballOddsProvider()
    provider.attach_request_budget(budget)
    near_kickoff = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    fixture_context = {
        "fixture_id": "api-football:1557378",
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "division_code": "E0",
        "season": "2026",
        "home_team_name": "Home FC",
        "away_team_name": "Away FC",
        "kickoff_utc": near_kickoff,
        "provider": "api-football",
        "provider_fixture_id": "1557378",
    }
    snapshots = provider.get_market_snapshot("api-football:1557378", fixture_context=fixture_context)
    assert len(snapshots) == 1
    assert fake.calls == 1
    assert budget.quota_snapshot()["daily_remaining"] == 999
    # Second call must be served from the persistent cache - no HTTP.
    snapshots2 = provider.get_market_snapshot("api-football:1557378", fixture_context=fixture_context)
    assert len(snapshots2) == 1
    assert fake.calls == 1
    assert budget.metrics()["http_requests"] == 1


def test_fixture_provider_gate_on_single_live_call(tmp_path, monkeypatch, api_football_env):
    import httpx

    payload = {
        "response": [
            {
                "fixture": {"id": "1557399", "date": "2026-08-30T12:00:00+00:00", "status": {"short": "1H", "elapsed": 23}},
                "league": {"id": 39, "name": "Premier League", "season": 2026},
                "teams": {"home": {"id": 1, "name": "Home FC"}, "away": {"id": 2, "name": "Away FC"}},
                "goals": {"home": 1, "away": 0},
            }
        ],
        "results": 1,
        "errors": {},
    }
    fake = _FakeClient([_response(payload)])
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: fake)
    budget = _budget(tmp_path, allow=True)
    provider = fp_module.ApiFootballFixtureProvider()
    provider.attach_request_budget(budget)
    live = provider.get_live_fixtures()
    assert len(live) == 1
    assert fake.calls == 1
    live2 = provider.get_live_fixtures()
    assert len(live2) == 1
    assert fake.calls == 1
    assert budget.metrics()["http_requests"] == 1
