from types import SimpleNamespace

from sports_ai.dashboard.fixture_provider import ApiFootballFixtureProvider
from sports_ai.dashboard.provider_budget import ACTION_BLOCKED_QUOTA, PRIORITY_LOW


class _BlockingBudget:
    def __init__(self):
        self.calls = []

    def issue(self, **kwargs):
        self.calls.append(dict(kwargs))
        return SimpleNamespace(
            action=ACTION_BLOCKED_QUOTA,
            cached_payload=None,
            allow=False,
            reason="test low-priority block",
        )


def _provider_with_budget():
    provider = ApiFootballFixtureProvider()
    budget = _BlockingBudget()
    provider.attach_request_budget(budget)
    return provider, budget


def test_historical_fixture_list_uses_low_priority_and_never_falls_through_to_network():
    provider, budget = _provider_with_budget()

    assert provider.get_historical_fixtures(league_id=61, season=2025) == []

    assert len(budget.calls) == 1
    call = budget.calls[0]
    assert call["endpoint"] == "fixtures"
    assert call["params"] == {"league": 61, "season": 2025}
    assert call["priority"] == PRIORITY_LOW
    assert call["wait_for_rate_slot"] is True


def test_historical_fixture_statistics_use_low_priority_bounded_rate_wait():
    provider, budget = _provider_with_budget()

    assert provider.get_historical_fixture_statistics(provider_fixture_id="12345") == []

    assert len(budget.calls) == 1
    call = budget.calls[0]
    assert call["endpoint"] == "fixtures/statistics"
    assert call["params"] == {"fixture": "12345"}
    assert call["priority"] == PRIORITY_LOW
    assert call["wait_for_rate_slot"] is True


def test_historical_league_reference_uses_low_priority_and_daily_cache():
    provider, budget = _provider_with_budget()

    assert provider.get_historical_league_candidates(search="Ligue 1", season=2025) == []

    assert len(budget.calls) == 1
    call = budget.calls[0]
    assert call["endpoint"] == "leagues"
    assert call["params"] == {"search": "Ligue 1", "season": 2025}
    assert call["priority"] == PRIORITY_LOW
    assert call["wait_for_rate_slot"] is True
    assert call["ttl_seconds"] == 24 * 60 * 60


def test_historical_league_reference_prefers_country_and_season_when_available():
    provider, budget = _provider_with_budget()

    assert provider.get_historical_league_candidates(
        search="Ligue 1",
        season=2025,
        country="France",
    ) == []

    assert len(budget.calls) == 1
    call = budget.calls[0]
    assert call["endpoint"] == "leagues"
    assert call["params"] == {"country": "France", "season": 2025}
    assert call["priority"] == PRIORITY_LOW
    assert call["wait_for_rate_slot"] is True
    assert call["ttl_seconds"] == 24 * 60 * 60
