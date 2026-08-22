from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from sports_ai.dashboard.app import create_app
from sports_ai.dashboard import storage as dashboard_storage
from sports_ai.dashboard.day12_rolling_xg import SOURCE_IDENTIFIER, build_xg_inspection
from sports_ai.dashboard.fixture_provider import ApiFootballFixtureProvider, FixtureProviderError, FootballDataOrgFixtureProvider
from sports_ai.dashboard.models import FIXTURE_STATUS_LIVE, FIXTURE_STATUS_PRE_MATCH, NormalizedFixture, normalize_text_key
from sports_ai.dashboard.services import DashboardConfig, DashboardServices
from sports_ai.market_data.providers import NormalizedMarketSnapshot
from sports_ai.storage import football_paper_trading


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


class FakeMarketProvider:
    def __init__(
        self,
        snapshots_by_fixture: dict[str, list[NormalizedMarketSnapshot]] | None = None,
        *,
        configured: bool = True,
    ) -> None:
        self.snapshots_by_fixture = snapshots_by_fixture or {}
        self._configured = configured

    def provider_name(self) -> str:
        return "fake-market-provider"

    def configured(self) -> bool:
        return self._configured

    def get_fixture_markets(
        self,
        fixture_id: str,
        *,
        fixture_context: dict | None = None,
    ) -> list[NormalizedMarketSnapshot]:
        return list(self.snapshots_by_fixture.get(fixture_id, []))

    def get_best_prices(
        self,
        fixture_id: str,
        *,
        fixture_context: dict | None = None,
    ) -> list[NormalizedMarketSnapshot]:
        return list(self.snapshots_by_fixture.get(fixture_id, []))

    def get_market_snapshot(
        self,
        fixture_id: str,
        *,
        fixture_context: dict | None = None,
    ) -> list[NormalizedMarketSnapshot]:
        return list(self.snapshots_by_fixture.get(fixture_id, []))

    def diagnostics(self) -> dict:
        return {
            "provider": self.provider_name(),
            "configured": self._configured,
            "state": "CONNECTED" if self._configured else "NOT_CONFIGURED",
            "current_market_decisions": "READY" if self._configured else "BLOCKED",
            "captured_at": "2026-08-20T00:00:00+00:00",
            "reason": None if self._configured else "Fake market provider disabled for this test.",
        }


class FlakyFixtureProvider(FakeFixtureProvider):
    def __init__(self, fixtures: list[NormalizedFixture]) -> None:
        super().__init__(fixtures)
        self.fail_next = True

    def get_live_fixtures(self) -> list[NormalizedFixture]:
        if self.fail_next:
            self.fail_next = False
            raise FixtureProviderError("transient provider failure")
        return super().get_live_fixtures()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _dashboard_config(tmp_path: Path) -> DashboardConfig:
    repo_root = _repo_root()
    performance_dir = tmp_path / "performance"
    performance_dir.mkdir(parents=True, exist_ok=True)
    (performance_dir / "football_performance_summary.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-08-20T04:18:32+00:00",
                "summary": {
                    "MODEL_VALIDATION": "PROMISING",
                    "PROFITABILITY_PROVEN": "NO",
                    "CLV_PROVEN": "NO",
                    "LIVE_EDGE_PROVEN": "NO",
                    "evaluation_sample": 9664,
                    "one_x_two_accuracy": 0.470509,
                    "one_x_two_brier": 0.625145,
                    "one_x_two_log_loss": 1.039517,
                },
                "status_flags": {
                    "HISTORICAL_BENCHMARK_COMPLETE": "YES",
                },
            }
        ),
        encoding="utf-8",
    )
    (performance_dir / "football_performance_by_market.csv").write_text(
        "market,line,selection,sample_size,hit_rate,mean_predicted_probability,actual_frequency,brier,log_loss,avg_odds,break_even_rate,roi,status,notes\n"
        "1X2,,ALL,9664,0.470509,,,0.625145,1.039517,,,,READY,Test row\n",
        encoding="utf-8",
    )
    (performance_dir / "football_performance_by_competition.csv").write_text(
        "competition_id,fixtures,1x2_accuracy,1x2_brier,1x2_log_loss,goals_mae,corners_mae,candidate_count,candidate_roi,status\n"
        "ENG1,1520,0.520395,0.594798,0.99749,1.329659,2.733234,1910,,PRICE_CONTEXT_CLOSING_ONLY\n",
        encoding="utf-8",
    )
    (performance_dir / "football_performance_by_season.csv").write_text(
        "season_code,fixtures,1x2_accuracy,1x2_brier,1x2_log_loss,goals_mae,corners_mae,status\n"
        "2526,2416,0.472682,0.625951,1.041255,1.225309,2.684581,READY\n",
        encoding="utf-8",
    )
    (performance_dir / "football_calibration.csv").write_text(
        "scope_type,scope_value,market,line,selection,subset,bucket_label,bucket_lo,bucket_hi,predicted_mean_probability,observed_hit_rate,sample_size,calibration_gap\n"
        "overall,ALL,1x2,,HOME,ALL_PREDICTIONS,40-50%,0.4,0.5,0.45,0.47,100,-0.02\n",
        encoding="utf-8",
    )
    return DashboardConfig(
        repo_root=repo_root,
        research_db_path=repo_root / "research" / "football_research" / "store" / "football_research.sqlite",
        dashboard_db_path=tmp_path / "football_dashboard.sqlite",
        paper_trading_db_path=tmp_path / "football_paper_trading.sqlite",
        performance_output_dir=performance_dir,
        operational_data_dir=tmp_path,
        storage_mode="test_tmpdir",
        persistent_data_dir_configured=False,
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
    kickoff_utc = (
        datetime.now(timezone.utc).replace(hour=20, minute=0, second=0, microsecond=0) + timedelta(days=2)
    ).isoformat()
    last_provider_update = (datetime.now(timezone.utc) - timedelta(hours=4)).replace(microsecond=0).isoformat()
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
        kickoff_utc=kickoff_utc,
        status=status,
        elapsed_minute=63 if status == FIXTURE_STATUS_LIVE else None,
        home_score=home_score,
        away_score=away_score,
        last_provider_update=last_provider_update,
        source_version="fake_fixture_provider_v1",
        provider_payload={"fixture_id": fixture_id},
    )


def _market_snapshot(
    *,
    fixture: NormalizedFixture,
    bookmaker: str,
    market: str,
    selection: str,
    decimal_odds: float,
    reference_designation: str,
    line: float | None = None,
    provider_market_id: str | None = None,
) -> NormalizedMarketSnapshot:
    return NormalizedMarketSnapshot(
        provider="fake-market-provider",
        fixture_id=fixture.fixture_id,
        provider_fixture_id=fixture.provider_fixture_id,
        captured_at="2026-08-20T10:00:00+00:00",
        source_timestamp="2026-08-20T10:00:00+00:00",
        market=market,
        selection=selection,
        line=line,
        decimal_odds=decimal_odds,
        bookmaker=bookmaker,
        ingestion_version="test_market_ingestion_v1",
        provider_market_id=provider_market_id,
        home_team_name=fixture.home_team_name,
        away_team_name=fixture.away_team_name,
        competition_id=fixture.competition_id,
        competition_name=fixture.competition_name,
        kickoff_utc=fixture.kickoff_utc,
        freshness_state="FRESH",
        reference_designation=reference_designation,
        reference_rank=1 if reference_designation == "reference" else None,
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
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=provider,
        market_provider=FakeMarketProvider(configured=False),
    )

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


def test_dashboard_config_default_uses_configured_data_dir(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SPORTS_AI_DATA_DIR", str(tmp_path))
    config = DashboardConfig.default()

    assert config.operational_data_dir == tmp_path
    assert config.dashboard_db_path == tmp_path / "football_dashboard.sqlite"
    assert config.paper_trading_db_path == tmp_path / "football_paper_trading.sqlite"
    assert config.storage_mode == "persistent"
    assert config.persistent_data_dir_configured is True


def test_dashboard_services_odds_sync_disabled_makes_no_provider_calls(tmp_path: Path):
    fixture = _fixture()
    calls = {"count": 0}

    class RecordingMarketProvider(FakeMarketProvider):
        def get_market_snapshot(
            self,
            fixture_id: str,
            *,
            fixture_context: dict | None = None,
        ) -> list[NormalizedMarketSnapshot]:
            calls["count"] += 1
            return []

    config = replace(_dashboard_config(tmp_path), current_odds_connected=True, enable_odds_sync=False)
    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=RecordingMarketProvider(),
    )

    asyncio.run(services.initialize())
    asyncio.run(services.sync_once())
    assert calls["count"] == 0
    asyncio.run(services.shutdown())


def test_dashboard_services_block_unresolved_team_identity(tmp_path: Path):
    fixture = _fixture(home_team_name="Unknown Town")
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())
    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["prediction"]["prediction_status"] == "BLOCKED"
    assert payload["prediction"]["blockers"] == ["HOME_TEAM_IDENTITY_UNRESOLVED"]
    asyncio.run(services.shutdown())


def test_dashboard_services_do_not_generate_first_prediction_after_kickoff(tmp_path: Path):
    fixture = replace(
        _fixture(),
        fixture_id="fake:past_fixture",
        kickoff_utc=(
            datetime.now(timezone.utc).replace(hour=20, minute=0, second=0, microsecond=0) - timedelta(days=2)
        ).isoformat(),
        status=FIXTURE_STATUS_LIVE,
        elapsed_minute=52,
        home_score=1,
        away_score=0,
    )
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())
    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["prediction"]["prediction_status"] == "BLOCKED"
    assert payload["prediction"]["blockers"] == ["PREDICTION_AFTER_KICKOFF_BLOCKED"]
    assert payload["prediction"]["lock_state"] == "LOCKED_PRE_MATCH_PREDICTION"
    asyncio.run(services.shutdown())


def test_normalize_text_key_preserves_safe_tokens():
    assert normalize_text_key("Derby County FC") == "derby county"
    assert normalize_text_key("Real Racing Club de Santander") == "real racing de santander"


def test_dashboard_services_resolve_alias_and_generate_prediction(tmp_path: Path):
    fixture = _fixture(home_team_name="Brighton Hove", away_team_name="Chelsea")
    fixture = replace(fixture, provider="football-data.org", home_team_id="397")
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())
    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["prediction"]["prediction_status"] == "READY"
    assert payload["prediction"]["team_resolution"]["home"]["canonical_team_name"] == "Brighton"
    assert payload["prediction"]["team_resolution"]["home"]["model_history_available"] is True
    asyncio.run(services.shutdown())


def test_dashboard_services_block_missing_model_history_after_identity_resolution(tmp_path: Path):
    fixture = replace(
        _fixture(home_team_name="West Ham", away_team_name="Leeds"),
        provider="football-data.org",
        competition_id="ENG2",
        competition_name="Championship",
        division_code="E1",
        home_team_id="563",
        away_team_id="341",
    )
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())
    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["prediction"]["prediction_status"] == "BLOCKED"
    assert payload["prediction"]["blockers"] == ["HOME_MODEL_HISTORY_MISSING"]
    assert payload["prediction"]["team_resolution"]["home"]["canonical_team_name"] == "West Ham"
    assert payload["prediction"]["model_history"]["home"]["available"] is False
    status = services.get_system_status()
    assert status["team_resolution"]["model_history_missing"] >= 1
    asyncio.run(services.shutdown())


def test_dashboard_services_resolve_malaga_alias_but_keep_history_block(tmp_path: Path):
    fixture = replace(
        _fixture(home_team_name="Atleti", away_team_name="M\ufffdlaga"),
        provider="football-data.org",
        competition_id="ESP1",
        competition_name="Primera Division",
        division_code="SP1",
        home_team_id="78",
        away_team_id="84",
    )
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())
    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["prediction"]["prediction_status"] == "BLOCKED"
    assert payload["prediction"]["blockers"] == ["AWAY_MODEL_HISTORY_MISSING"]
    assert payload["prediction"]["team_resolution"]["home"]["canonical_team_name"] == "Ath Madrid"
    assert payload["prediction"]["team_resolution"]["away"]["canonical_team_name"] == "Malaga"
    assert payload["prediction"]["model_history"]["away"]["available"] is False
    asyncio.run(services.shutdown())


def test_dashboard_status_recomputes_team_resolution_from_current_fixture_rows(tmp_path: Path):
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([_fixture()]),
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())
    kickoff_utc = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0).isoformat()
    counts = services._team_resolution_counts(
        [
            {
                "fixture_id": "football-data.org:564628",
                "provider": "football-data.org",
                "competition_id": "ESP1",
                "competition_name": "Primera Division",
                "status": FIXTURE_STATUS_PRE_MATCH,
                "home_team_id": "78",
                "home_team_name": "Atleti",
                "away_team_id": "84",
                "away_team_name": "M\ufffdlaga",
                "kickoff_utc": kickoff_utc,
                "prediction": {
                    "prediction_id": "prediction_legacy_malaga",
                    "fixture_id": "football-data.org:564628",
                    "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "prediction_status": "BLOCKED",
                    "blockers": ["PREDICTION_BLOCKED_TEAM_IDENTITY"],
                    "team_resolution": {
                        "home": {
                            "provider": "football-data.org",
                            "competition_id": "ESP1",
                            "provider_team_id": "78",
                            "provider_team_name": "Atleti",
                            "normalized_team_name": "atleti",
                            "canonical_team_id": None,
                            "canonical_team_name": None,
                            "resolution_kind": "unresolved",
                            "confidence": 0.0,
                            "reason": "no deterministic canonical team mapping found",
                        },
                        "away": {
                            "provider": "football-data.org",
                            "competition_id": "ESP1",
                            "provider_team_id": "84",
                            "provider_team_name": "M\ufffdlaga",
                            "normalized_team_name": "mlaga",
                            "canonical_team_id": None,
                            "canonical_team_name": None,
                            "resolution_kind": "unresolved",
                            "confidence": 0.0,
                            "reason": "no deterministic canonical team mapping found",
                        },
                    },
                    "model_history": None,
                },
            }
        ]
    )
    assert counts["unresolved"] == 0
    assert counts["model_history_missing"] >= 1
    assert any(
        example["provider_team_id"] == "84"
        for example in counts["model_history_missing_examples"]
    )
    asyncio.run(services.shutdown())


def test_dashboard_services_leave_bare_deportivo_unresolved_without_context(tmp_path: Path):
    fixture = replace(
        _fixture(home_team_name="Deportivo", away_team_name="Valencia"),
        provider="football-data.org",
        competition_id="ESP1",
        competition_name="Primera Division",
        division_code="SP1",
        home_team_id=None,
        away_team_id="95",
    )
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())
    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["prediction"]["prediction_status"] == "BLOCKED"
    assert payload["prediction"]["blockers"] == ["HOME_TEAM_IDENTITY_UNRESOLVED"]
    assert payload["prediction"]["team_resolution"]["home"]["canonical_team_name"] is None
    asyncio.run(services.shutdown())


def test_dashboard_routes_render_and_apis_return_current_prediction(tmp_path: Path):
    fixture = _fixture()
    app = create_app(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
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

        performance_page = client.get("/performance")
        assert performance_page.status_code == 200
        assert "Performance" in performance_page.text
        assert 'href="/static/dashboard.css"' in performance_page.text
        assert 'src="/static/dashboard.js"' in performance_page.text

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

        performance_response = client.get("/api/performance")
        assert performance_response.status_code == 200
        assert performance_response.json()["summary"]["MODEL_VALIDATION"] == "PROMISING"

        health_response = client.get("/health")
        assert health_response.status_code == 200
        assert health_response.json()["status"] == "ok"

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
        market_provider=FakeMarketProvider(configured=False),
    )

    with TestClient(app) as client:
        status_response = client.get("/api/system/status")
        assert status_response.status_code == 200
        payload = status_response.json()
        assert payload["fixture_feed"]["state"] == "NOT_CONFIGURED"
        assert payload["current_fixture_feed_ready"] == "NO"
        assert payload["storage"]["persistent_path_configured"] == "NO"


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
        market_provider=FakeMarketProvider(configured=False),
    )

    with TestClient(app) as client:
        payload = client.get("/api/system/status").json()
        assert payload["fixture_feed"]["state"] == "DEGRADED"
        assert "current season unavailable on this plan" in payload["fixture_feed"]["last_error"]


def test_dashboard_services_restart_preserves_prediction_without_duplicates(tmp_path: Path):
    fixture = _fixture()
    config = _dashboard_config(tmp_path)

    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )
    asyncio.run(services.initialize())
    asyncio.run(services.shutdown())

    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )
    asyncio.run(services.initialize())

    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        predictions = dashboard_storage.fetch_all_predictions(conn)
        assert len(predictions) == 1
        assert predictions[0]["prediction_status"] == "READY"
    status = services.get_system_status()
    assert status["storage"]["dashboard_db_reachable"] is True
    assert status["prediction_engine"]["last_successful_prediction_at"] is not None
    asyncio.run(services.shutdown())


def test_dashboard_services_recovers_after_provider_failure(tmp_path: Path):
    fixture = _fixture()
    provider = FlakyFixtureProvider([fixture])
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=provider,
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())
    first_status = services.get_system_status()
    assert first_status["fixture_feed"]["state"] == "DEGRADED"
    assert "transient provider failure" in (first_status["fixture_feed"]["last_error"] or "")

    asyncio.run(services.sync_once())
    second_status = services.get_system_status()
    assert second_status["fixture_feed"]["state"] == "CONNECTED"
    assert second_status["current_fixture_state"]["fixtures_discovered"] == 1
    asyncio.run(services.shutdown())


def test_dashboard_services_current_market_candidate_creates_idempotent_paper_entry(tmp_path: Path):
    fixture = _fixture()
    config = replace(_dashboard_config(tmp_path), current_xg_connected=True, enable_odds_sync=True)
    market_provider = FakeMarketProvider()

    def xg_factory(row, prediction, snapshots):
        expected_home = float(prediction["expected_home_goals"])
        expected_away = float(prediction["expected_away_goals"])

        def _row(role, recent):
            return {
                "fixture_id": fixture.fixture_id,
                "team_role": role,
                "recent_xg_for_per_match": recent,
                "matches_in_window": 5,
                "as_of": "2026-05-24T20:00:00+00:00",
                "window_start_at": "2026-04-01T20:00:00+00:00",
                "window_end_at": "2026-05-24T20:00:00+00:00",
                "source": SOURCE_IDENTIFIER,
                "competition_id": "ENG1",
            }

        rows = {
            (fixture.fixture_id, "HOME"): _row("HOME", expected_home),
            (fixture.fixture_id, "AWAY"): _row("AWAY", expected_away),
        }
        inspection = build_xg_inspection(rows)
        return lambda: (rows, inspection)

    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=market_provider,
        xg_profile_factory=xg_factory,
    )

    asyncio.run(services.initialize())
    market_provider.snapshots_by_fixture[fixture.fixture_id] = [
        _market_snapshot(
            fixture=fixture,
            bookmaker="Pinnacle",
            market="1x2",
            selection="HOME",
            decimal_odds=2.05,
            reference_designation="reference",
            provider_market_id="pin-home",
        ),
        _market_snapshot(
            fixture=fixture,
            bookmaker="Pinnacle",
            market="1x2",
            selection="DRAW",
            decimal_odds=3.35,
            reference_designation="reference",
            provider_market_id="pin-draw",
        ),
        _market_snapshot(
            fixture=fixture,
            bookmaker="Pinnacle",
            market="1x2",
            selection="AWAY",
            decimal_odds=3.95,
            reference_designation="reference",
            provider_market_id="pin-away",
        ),
        _market_snapshot(
            fixture=fixture,
            bookmaker="bet365",
            market="1x2",
            selection="HOME",
            decimal_odds=10.0,
            reference_designation="available",
            provider_market_id="b365-home",
        ),
    ]

    asyncio.run(services.sync_once())
    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["market_evaluation"]["state"] == "READY"
    assert payload["market_evaluation"]["counts"]["preliminary_candidates"] >= 1
    assert payload["fixture"]["market_signal"]["decision"] == "PRELIMINARY_CANDIDATE"

    candidates_payload = services.get_candidates_payload()
    assert candidates_payload["connected"] is True
    assert candidates_payload["preliminary_candidates"]
    assert candidates_payload["preliminary_candidates"][0]["paper_entry_eligible"] is True

    performance_payload = services.get_performance_payload()
    assert performance_payload["current_paper_summary"]["positions"] == 1
    assert performance_payload["current_paper_summary"]["open_positions"] == 1

    with football_paper_trading.connect(config.paper_trading_db_path) as conn:
        paper_rows = football_paper_trading.fetch_paper_trade_rows(conn)
        assert len(paper_rows) == 1
        assert paper_rows[0]["entry_snapshot_id"] is not None
        first_trade_id = str(paper_rows[0]["paper_trade_id"])

    asyncio.run(services.sync_once())

    with football_paper_trading.connect(config.paper_trading_db_path) as conn:
        paper_rows = football_paper_trading.fetch_paper_trade_rows(conn)
        assert len(paper_rows) == 1
        assert str(paper_rows[0]["paper_trade_id"]) == first_trade_id

    asyncio.run(services.shutdown())
