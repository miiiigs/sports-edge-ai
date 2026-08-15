import os

from sports_ai.data_sources.football_upcoming import fetch_api_football_fixtures


class DummyApiFootballClient:
    def get(self, url: str, params: dict[str, object]):
        import httpx

        request = httpx.Request("GET", url, params=params)
        payload = {
            "response": [
                {
                    "fixture": {
                        "id": 12345,
                        "date": "2026-08-15T14:00:00+00:00",
                        "status": {"short": "NS"},
                    },
                    "teams": {
                        "home": {"name": "Alpha FC"},
                        "away": {"name": "Beta FC"},
                    },
                }
            ]
        }
        return httpx.Response(200, request=request, json=payload)


def test_fetch_api_football_fixtures_accepts_league_map_file_path(tmp_path, monkeypatch):
    map_path = tmp_path / "api_football_league_map.json"
    map_path.write_text('{"E0": 39}', encoding="utf-8")
    monkeypatch.setenv("SPORTS_AI_API_FOOTBALL_LEAGUE_MAP", str(map_path))
    monkeypatch.setenv("SPORTS_AI_API_FOOTBALL_TOKEN", "test-token")

    fixtures = fetch_api_football_fixtures(
        division_codes=("E0",),
        target_date="2026-08-15",
        client=DummyApiFootballClient(),
    )

    assert len(fixtures) == 1
    assert fixtures[0].division_code == "E0"
    assert fixtures[0].competition_id == "ENG1"
    assert fixtures[0].source_fixture_id == "12345"
