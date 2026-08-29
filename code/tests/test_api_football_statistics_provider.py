from __future__ import annotations

import json

import pytest

from sports_ai.dashboard.fixture_provider import (
    API_FOOTBALL_PROVIDER,
    ApiFootballFixtureProvider,
    AuthoritativeFixtureStatistics,
    FixtureProviderError,
)

FIXTURE_CONTEXT = {
    "fixture_id": "football-data.org:560557",
    "provider": "football-data.org",
    "provider_fixture_id": "560557",
    "competition_id": "ENG1",
    "competition_name": "Premier League",
    "division_code": "E0",
    "season": "2026",
    "home_team_name": "Arsenal",
    "away_team_name": "Chelsea",
    "kickoff_utc": "2026-08-23T14:00:00+00:00",
    "status": "FINISHED",
}


def _provider(tmp_path, monkeypatch):
    map_path = tmp_path / "league_map.json"
    map_path.write_text(json.dumps({"E0": 39}), encoding="utf-8")
    monkeypatch.setenv("SPORTS_AI_API_FOOTBALL_TOKEN", "test-token")
    monkeypatch.setenv("SPORTS_AI_API_FOOTBALL_LEAGUE_MAP", str(map_path))
    return ApiFootballFixtureProvider()


def test_authoritative_corner_statistics_normalize_and_attribute_home_away(tmp_path, monkeypatch):
    provider = _provider(tmp_path, monkeypatch)
    monkeypatch.setattr(
        provider,
        "_request_fixtures",
        lambda **kwargs: [
            {
                "fixture": {"id": 9001, "date": "2026-08-23T14:00:00+00:00"},
                "teams": {
                    "home": {"name": "Arsenal"},
                    "away": {"name": "Chelsea"},
                },
            }
        ],
    )
    monkeypatch.setattr(
        provider,
        "_request_fixture_statistics",
        lambda **kwargs: [
            {
                "team": {"name": "Chelsea"},
                "statistics": [{"type": "Corner Kicks", "value": "5"}],
            },
            {
                "team": {"name": "Arsenal"},
                "statistics": [{"type": "Corner Kicks", "value": 6}],
            },
        ],
    )

    result = provider.get_authoritative_corner_statistics(FIXTURE_CONTEXT)

    assert isinstance(result, AuthoritativeFixtureStatistics)
    assert result.fixture_id == "football-data.org:560557"
    assert result.provider == API_FOOTBALL_PROVIDER
    assert result.provider_fixture_id == "9001"
    assert result.home_corners == 6
    assert result.away_corners == 5
    assert result.total_corners == 11


def test_authoritative_corner_statistics_ambiguous_mapping_fails_closed(tmp_path, monkeypatch):
    provider = _provider(tmp_path, monkeypatch)
    monkeypatch.setattr(
        provider,
        "_request_fixtures",
        lambda **kwargs: [
            {
                "fixture": {"id": 9001, "date": "2026-08-23T14:00:00+00:00"},
                "teams": {"home": {"name": "Arsenal"}, "away": {"name": "Chelsea"}},
            },
            {
                "fixture": {"id": 9002, "date": "2026-08-23T14:00:00+00:00"},
                "teams": {"home": {"name": "Arsenal"}, "away": {"name": "Chelsea"}},
            },
        ],
    )

    with pytest.raises(FixtureProviderError, match="STATS_FIXTURE_AMBIGUOUS"):
        provider.get_authoritative_corner_statistics(FIXTURE_CONTEXT)
