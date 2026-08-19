from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from sports_ai.dashboard.app import create_app
from sports_ai.dashboard.fixture_provider import ApiFootballFixtureProvider, FootballDataOrgFixtureProvider
from sports_ai.dashboard.models import FIXTURE_STATUS_LIVE, FIXTURE_STATUS_PRE_MATCH, NormalizedFixture
from sports_ai.dashboard.services import DashboardConfig, DashboardServices


class FakeFixtureProvider:
    def __init__(
        self,
        fixtures: list[NormalizedFixture] | None = None,
        *,
        configured: bool = True,
        diagnostics: dict | None = None,
    ):
        self.fixtures = fixtures or []
        self._configured = configured
        self._diagnostics = diagnostics or {
            "provider": "fake-provider",
            "query_count": 0,
            "league_ids_queried": [],
            "provider_seasons_queried": [],
            "has_errors": False,
            "last_error": None,
            "queries": [],
        }

    def provider_name(self) -> str:
        return "fake-provider"

    def configured(self) -> bool:
        return self._configured

    def reset_sync_diagnostics(self) -> None:
        return None

    def sync_diagnostics(self) -> dict:
        return dict(self._diagnostics)

    def get_live_fixtures(self) -> list[NormalizedFixture]:
        return [fixture for fixture in self.fixtures if fixture.status == FIXTURE_STATUS_LIVE]

    def get_fixtures_for_date(self, target_date: date) -> list[NormalizedFixture]:
        return [fixture for fixture in self.fixtures if fixture.kickoff_utc[:10] == target_date.isoformat()]

    def get_upcoming_fixtures(
        self,
        *,
        from_utc: datetime,
        to_utc: datetime,
    ) -> list[NormalizedFixture]:
        return [
            fixture
            for fixture in self.fixtures
            if from_utc <= datetime.fromisoformat(fixture.kickoff_utc) <= to_utc
        ]

    def get_fixture(self, fixture_id: str) -> NormalizedFixture | None:
        for fixture in self.fixtures:
            if fixture.fixture_id == fixture_id:
                return fixture
        return None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _dashboard_config(tmp_path: Path) -> DashboardConfig:
    repo_root = _repo_root()
    return DashboardConfig(
        repo_root=repo_root,
        research_db_path=repo_root / "research" / "football_research" / "store" / "football_research.sqlite",
        dashboard_db_path=tmp_path / "football_dashboard.sqlite",
        display_timezone="Asia/Manila",
        upcoming_horizon_days=3,
        scheduler_interval_seconds=9999,
        enable_scheduler=False,
        current_odds_connected=False,
        current_xg_connected=False,
    )


def _fixture(
    *,
    fixture_id: str = "fake:arsenal_chelsea",
    home_team_name: str = "Arsenal",
    away_team_name: str = "Chelsea",
    status: str = FIXTURE_STATUS_PRE_MATCH,
    home_score: int | None = None,
    away_score: int | None = None,
) -> NormalizedFixture:
    return NormalizedFixture(
        fixture_id=fixture_id,
        provider_fixture_id="fixture-1",
        provider="fake-provider",
        competition_id="ENG1",
        competition_name="Premier League",
        division_code="E0",
        season="2026",
        home_team_id="100",
        home_team_name=home_team_name,
        away_team_id="200",
        away_team_name=away_team_name,
        kickoff_utc="2026-08-20T20:00:00+00:00",
        status=status,
        elapsed_minute=63 if status == FIXTURE_STATUS_LIVE else None,
        home_score=home_score,
        away_score=away_score,
        last_provider_update="2026-08-19T12:00:00+00:00",
        source_version="fake_fixture_provider_v1",
        provider_payload={"fixture_id": fixture_id},
    )


def test_api_football_normalization_maps_status_and_score():
    provider = ApiFootballFixtureProvider()
    payload = {
        "fixture": {
            "id": 12345,
            "date": "2026-08-19T19:00:00+00:00",
            "status": {"short": "1H", "elapsed": 27},
        },
        "league": {
            "id": 39,
            "name": "Premier League",
            "season": 2026,
        },
        "teams": {
            "home": {"id": 1, "name": "Arsenal"},
            "away": {"id": 2, "name": "Chelsea"},
        },
        "goals": {"home": 1, "away": 0},
    }

    fixture = provider._normalize_fixture(payload)

    assert fixture is not None
    assert fixture.fixture_id == "api-football:12345"
    assert fixture.status == "LIVE"
    assert fixture.elapsed_minute == 27
    assert fixture.home_score == 1
    assert fixture.away_score == 0
    assert fixture.competition_id == "ENG1"


def test_football_data_org_normalization_maps_status_and_score():
    provider = FootballDataOrgFixtureProvider()
    payload = {
        "id": 321,
        "utcDate": "2026-08-19T19:00:00Z",
        "status": "IN_PLAY",
        "lastUpdated": "2026-08-19T19:27:00Z",
        "competition": {
            "id": 2021,
            "code": "PL",
            "name": "Premier League",
        },
        "season": {
            "id": 2245,
            "startDate": "2026-08-14",
        },
        "homeTeam": {"id": 57, "shortName": "Arsenal"},
        "awayTeam": {"id": 61, "shortName": "Chelsea"},
        "score": {
            "fullTime": {"home": 1, "away": 0},
        },
    }

    fixture = provider._normalize_fixture(payload)

    assert fixture is not None
    assert fixture.fixture_id == "football-data.org:321"
    assert fixture.status == "LIVE"
    assert fixture.home_score == 1
    assert fixture.away_score == 0
    assert fixture.competition_id == "ENG1"
    assert fixture.season == "2026"


def test_dashboard_services_generate_prediction_and_ignore_live_score(tmp_path: Path):
    fixture = _fixture()
    provider = FakeFixtureProvider([fixture])
    services = DashboardServices(config=_dashboard_config(tmp_path), fixture_provider=provider)

    asyncio.run(services.initialize())
    initial_payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert initial_payload is not None
    assert initial_payload["prediction"]["prediction_status"] == "READY"
    initial_prediction = initial_payload["prediction"]

    provider.fixtures = [
        replace(
            fixture,
            status=FIXTURE_STATUS_LIVE,
            home_score=3,
            away_score=2,
            elapsed_minute=74,
        )
    ]
    asyncio.run(services.sync_once())

    updated_payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert updated_payload is not None
    updated_prediction = updated_payload["prediction"]
    assert updated_prediction["version_key"] == initial_prediction["version_key"]
    assert updated_prediction["expected_home_goals"] == initial_prediction["expected_home_goals"]
    assert updated_prediction["expected_away_goals"] == initial_prediction["expected_away_goals"]
    assert updated_prediction["top_scorelines"] == initial_prediction["top_scorelines"]
    asyncio.run(services.shutdown())


def test_dashboard_services_block_unresolved_team_identity(tmp_path: Path):
    fixture = _fixture(home_team_name="Unknown Town")
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
    )

    asyncio.run(services.initialize())
    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["prediction"]["prediction_status"] == "BLOCKED"
    assert payload["prediction"]["blockers"] == ["PREDICTION_BLOCKED_TEAM_IDENTITY"]
    asyncio.run(services.shutdown())


def test_dashboard_services_do_not_generate_first_prediction_after_kickoff(tmp_path: Path):
    fixture = replace(
        _fixture(),
        fixture_id="fake:past_fixture",
        kickoff_utc="2026-08-18T20:00:00+00:00",
        status=FIXTURE_STATUS_LIVE,
        elapsed_minute=52,
        home_score=1,
        away_score=0,
    )
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
    )

    asyncio.run(services.initialize())
    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["prediction"]["prediction_status"] == "BLOCKED"
    assert payload["prediction"]["blockers"] == ["PREDICTION_LOCKED_AFTER_KICKOFF"]
    assert payload["prediction"]["lock_state"] == "LOCKED_PRE_MATCH_PREDICTION"
    asyncio.run(services.shutdown())


def test_dashboard_routes_render_and_apis_return_current_prediction(tmp_path: Path):
    fixture = _fixture()
    app = create_app(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
    )

    with TestClient(app) as client:
        dashboard_response = client.get("/")
        assert dashboard_response.status_code == 200
        assert "Sports Edge AI" in dashboard_response.text
        assert 'href="/static/dashboard.css"' in dashboard_response.text
        assert 'src="/static/dashboard.js"' in dashboard_response.text
        assert "/matches/static/dashboard.css" not in dashboard_response.text

        matches_response = client.get("/matches")
        assert matches_response.status_code == 200
        assert 'href="/static/dashboard.css"' in matches_response.text
        assert 'src="/static/dashboard.js"' in matches_response.text
        assert "/matches/static/dashboard.css" not in matches_response.text
        assert 'data-auto-submit-filters="true"' in matches_response.text
        assert "Apply Filters" not in matches_response.text

        predictions_page = client.get("/predictions")
        assert predictions_page.status_code == 200
        assert 'href="/static/dashboard.css"' in predictions_page.text
        assert 'src="/static/dashboard.js"' in predictions_page.text

        candidates_page = client.get("/candidates")
        assert candidates_page.status_code == 200
        assert 'href="/static/dashboard.css"' in candidates_page.text
        assert 'src="/static/dashboard.js"' in candidates_page.text

        status_page = client.get("/status")
        assert status_page.status_code == 200
        assert "System Health" in status_page.text
        assert 'href="/static/dashboard.css"' in status_page.text
        assert 'src="/static/dashboard.js"' in status_page.text

        stylesheet_response = client.get("/static/dashboard.css")
        assert stylesheet_response.status_code == 200
        assert "text/css" in stylesheet_response.headers["content-type"]
        assert ":root" in stylesheet_response.text
        assert ".app-shell" in stylesheet_response.text

        status_response = client.get("/api/system/status")
        assert status_response.status_code == 200
        assert status_response.json()["fixture_feed"]["state"] == "CONNECTED"

        upcoming_response = client.get("/api/fixtures/upcoming")
        assert upcoming_response.status_code == 200
        assert upcoming_response.json()["count"] == 1

        next_response = client.get("/api/fixtures/next")
        assert next_response.status_code == 200
        assert next_response.json()["count"] == 1

        predictions_response = client.get("/api/predictions")
        assert predictions_response.status_code == 200
        assert predictions_response.json()["rows"][0]["prediction"]["prediction_status"] == "READY"

        prediction_response = client.get(f"/api/predictions/{fixture.fixture_id}")
        assert prediction_response.status_code == 200
        assert prediction_response.json()["prediction_status"] == "READY"

        refresh_response = client.post("/api/fixtures/refresh")
        assert refresh_response.status_code == 200
        assert refresh_response.json()["refresh_state"] in {"SYNCED", "COOLDOWN"}


def test_dashboard_routes_surface_provider_not_configured(tmp_path: Path):
    app = create_app(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider(configured=False),
    )

    with TestClient(app) as client:
        status_response = client.get("/api/system/status")
        assert status_response.status_code == 200
        payload = status_response.json()
        assert payload["fixture_feed"]["state"] == "NOT_CONFIGURED"
        assert payload["current_fixture_feed_ready"] == "NO"


def test_dashboard_routes_surface_provider_diagnostics_errors(tmp_path: Path):
    fixture = _fixture()
    diagnostics = {
        "provider": "fake-provider",
        "query_count": 1,
        "league_ids_queried": [39],
        "provider_seasons_queried": ["2026"],
        "has_errors": True,
        "last_error": {
            "provider_season": "2026",
            "provider_league_id": 39,
            "errors": {"plan": "current season unavailable on this plan"},
        },
        "queries": [],
    }
    app = create_app(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture], diagnostics=diagnostics),
    )

    with TestClient(app) as client:
        payload = client.get("/api/system/status").json()
        assert payload["fixture_feed"]["state"] == "DEGRADED"
        assert "current season unavailable on this plan" in payload["fixture_feed"]["last_error"]
