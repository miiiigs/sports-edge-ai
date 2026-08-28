from __future__ import annotations

import asyncio
import json
import math
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sports_ai.dashboard.app import create_app
from sports_ai.dashboard import storage as dashboard_storage
from sports_ai.dashboard.day12_rolling_xg import SOURCE_IDENTIFIER, build_xg_inspection
from sports_ai.dashboard.fixture_provider import ApiFootballFixtureProvider, FixtureProviderError, FootballDataOrgFixtureProvider
from sports_ai.dashboard.models import (
    FIXTURE_STATUS_FINISHED,
    FIXTURE_STATUS_LIVE,
    FIXTURE_STATUS_PRE_MATCH,
    NormalizedFixture,
    normalize_text_key,
)
from sports_ai.dashboard.services import DashboardConfig, DashboardServices, _default_market_provider, _humanize_market_selection
from sports_ai.market_data.providers import NormalizedMarketSnapshot
from sports_ai.storage import football_edge_desk, football_paper_trading


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
        source_timestamp=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds"),
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


def _xg_factory_for_fixtures(*fixtures: NormalizedFixture):
    fixture_map = {fixture.fixture_id: fixture for fixture in fixtures}

    def factory(row, prediction, snapshots):
        fixture = fixture_map[str(row["fixture_id"])]
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
                "competition_id": str(row["competition_id"]),
            }

        rows = {
            (fixture.fixture_id, "HOME"): _row("HOME", expected_home),
            (fixture.fixture_id, "AWAY"): _row("AWAY", expected_away),
        }
        inspection = build_xg_inspection(rows)
        return lambda: (rows, inspection)

    return factory


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


def test_dashboard_services_shadow_edge_desk_records_route_a_and_route_b_without_changing_current_prediction(tmp_path: Path):
    fixture = _fixture()
    config = _dashboard_config(tmp_path)
    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())
    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["prediction"]["prediction_status"] == "READY"
    assert payload["prediction"]["fixture_id"] == fixture.fixture_id
    assert payload["prediction"]["competition_id"] == "ENG1"

    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        active_agents = football_edge_desk.list_active_agents(conn)
        edge_predictions = football_edge_desk.fetch_agent_predictions(conn, fixture_id=fixture.fixture_id)
        edge_consensus = football_edge_desk.fetch_consensus_evaluations(conn, fixture_id=fixture.fixture_id)
        edge_decisions = football_edge_desk.fetch_decision_history(conn, fixture_id=fixture.fixture_id)

    assert edge_predictions
    assert {row["agent_id"] for row in active_agents} == {football_edge_desk.ROUTE_B_COMPOSITE_AGENT_ID}
    assert any(row["agent_id"] == football_edge_desk.ROUTE_B_COMPOSITE_AGENT_ID for row in edge_predictions)
    assert any(row["agent_id"] == "ROUTE_A_DIRECT_CLASSIFIER" for row in edge_predictions)
    assert all(
        row["prediction_status"] == "PREDICTED"
        for row in edge_predictions
        if row["agent_id"] == football_edge_desk.ROUTE_B_COMPOSITE_AGENT_ID
    )
    assert {
        row["market_family"]
        for row in edge_predictions
        if row["agent_id"] == "ROUTE_A_DIRECT_CLASSIFIER"
    } == {"btts", "goals_total"}
    assert all(
        row["prediction_status"] == "BLOCKED"
        and row["block_reason"] == "MARKET_REJECTED"
        for row in edge_predictions
        if row["agent_id"] == "ROUTE_A_DIRECT_CLASSIFIER"
    )
    assert edge_consensus
    assert any(row["market_family"] == "btts" and row["status"] == "INSUFFICIENT_AGENTS" for row in edge_consensus)
    assert any(
        row["market_family"] == "goals_total" and row["line"] == 2.5 and row["status"] == "INSUFFICIENT_AGENTS"
        for row in edge_consensus
    )
    assert all(
        row["status"] == "INSUFFICIENT_AGENTS"
        for row in edge_consensus
    )
    assert edge_decisions
    assert all(row["decision_status"] == "NO_BET" for row in edge_decisions)
    status = services.get_system_status()
    assert status["storage"]["edge_desk_shadow"]["available"] is True
    assert status["storage"]["edge_desk_shadow"]["state"] == "READY"
    assert status["storage"]["edge_desk_shadow"]["last_error"] is None
    asyncio.run(services.shutdown())


def test_dashboard_services_fail_open_when_edge_desk_init_fails(monkeypatch, tmp_path: Path):
    fixture = _fixture()
    config = _dashboard_config(tmp_path)
    shadow_calls = {"count": 0}

    def broken_edge_desk_schema(conn):
        raise RuntimeError("edge desk init failed")

    def record_shadow_call(*args, **kwargs):
        shadow_calls["count"] += 1

    monkeypatch.setattr(football_edge_desk, "ensure_schema", broken_edge_desk_schema)
    monkeypatch.setattr(football_edge_desk, "record_shadow_prediction_bundle", record_shadow_call)

    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())

    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["prediction"]["prediction_status"] == "READY"

    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        prediction = dashboard_storage.fetch_latest_prediction_for_fixture(conn, fixture.fixture_id)
    assert prediction is not None
    assert prediction["prediction_status"] == "READY"

    status = services.get_system_status()
    assert status["storage"]["edge_desk_shadow"]["available"] is False
    assert status["storage"]["edge_desk_shadow"]["state"] == "DEGRADED"
    assert "initialization failure" in (status["storage"]["edge_desk_shadow"]["last_error"] or "")
    assert "edge desk init failed" in (status["storage"]["edge_desk_shadow"]["last_error"] or "")
    assert shadow_calls["count"] == 0

    asyncio.run(services.shutdown())


def test_dashboard_services_runtime_edge_desk_failure_degrades_status_and_skips_future_writes(
    monkeypatch,
    tmp_path: Path,
):
    fixture = _fixture()
    second_fixture = _fixture(
        fixture_id="fake:liverpool_everton",
        home_team_name="Liverpool",
        away_team_name="Everton",
    )
    config = _dashboard_config(tmp_path)
    shadow_calls = {"count": 0}
    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([fixture, second_fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())

    healthy_status = services.get_system_status()
    assert healthy_status["storage"]["edge_desk_shadow"]["available"] is True
    assert healthy_status["storage"]["edge_desk_shadow"]["state"] == "READY"
    assert healthy_status["storage"]["edge_desk_shadow"]["last_error"] is None

    def broken_edge_desk_write(*args, **kwargs):
        shadow_calls["count"] += 1
        raise RuntimeError("edge desk runtime write failed")

    monkeypatch.setattr(football_edge_desk, "record_shadow_prediction_bundle", broken_edge_desk_write)

    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        edge_prediction_count_before = len(football_edge_desk.fetch_agent_predictions(conn, fixture_id=fixture.fixture_id))
        second_edge_prediction_count_before = len(
            football_edge_desk.fetch_agent_predictions(conn, fixture_id=second_fixture.fixture_id)
        )

    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        first_payload = services.ensure_prediction(conn, fixture)
    assert first_payload["prediction_status"] == "READY"

    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        first_prediction = dashboard_storage.fetch_latest_prediction_for_fixture(conn, fixture.fixture_id)
        persisted_runtime_state = dashboard_storage.get_runtime_state(conn, "storage_runtime_state")
        edge_predictions = football_edge_desk.fetch_agent_predictions(conn, fixture_id=fixture.fixture_id)

    assert first_prediction is not None
    assert first_prediction["prediction_status"] == "READY"
    assert len(edge_predictions) == edge_prediction_count_before
    assert persisted_runtime_state is not None
    assert persisted_runtime_state["edge_desk_shadow"]["available"] is False
    assert persisted_runtime_state["edge_desk_shadow"]["state"] == "DEGRADED"
    assert "runtime shadow-write failure" in (persisted_runtime_state["edge_desk_shadow"]["last_error"] or "")
    assert "edge desk runtime write failed" in (persisted_runtime_state["edge_desk_shadow"]["last_error"] or "")

    degraded_status = services.get_system_status()
    assert degraded_status["storage"]["edge_desk_shadow"]["available"] is False
    assert degraded_status["storage"]["edge_desk_shadow"]["state"] == "DEGRADED"
    assert "runtime shadow-write failure" in (degraded_status["storage"]["edge_desk_shadow"]["last_error"] or "")
    assert "edge desk runtime write failed" in (degraded_status["storage"]["edge_desk_shadow"]["last_error"] or "")

    reload_payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert reload_payload is not None
    assert reload_payload["prediction"]["prediction_status"] == "READY"
    assert reload_payload["prediction"]["fixture_id"] == fixture.fixture_id
    assert "consensus_rows" not in reload_payload["prediction"]

    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        second_payload = services.ensure_prediction(conn, second_fixture)
    assert second_payload["prediction_status"] == "READY"

    second_detail_payload = services.get_fixture_detail_payload(second_fixture.fixture_id)
    assert second_detail_payload is not None
    assert second_detail_payload["prediction"]["prediction_status"] == "READY"
    assert second_detail_payload["prediction"]["fixture_id"] == second_fixture.fixture_id

    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        second_prediction = dashboard_storage.fetch_latest_prediction_for_fixture(conn, second_fixture.fixture_id)
        second_runtime_state = dashboard_storage.get_runtime_state(conn, "storage_runtime_state")
        second_edge_predictions = football_edge_desk.fetch_agent_predictions(conn, fixture_id=second_fixture.fixture_id)

    assert second_prediction is not None
    assert second_prediction["prediction_status"] == "READY"
    assert second_runtime_state is not None
    assert second_runtime_state["edge_desk_shadow"]["available"] is False
    assert second_runtime_state["edge_desk_shadow"]["state"] == "DEGRADED"
    assert len(second_edge_predictions) == second_edge_prediction_count_before
    assert shadow_calls["count"] == 1

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
        assert "EDGE DESK" in dashboard_response.text
        assert 'href="/static/dashboard.css"' in dashboard_response.text
        assert 'src="/static/dashboard.js"' in dashboard_response.text
        assert "/matches/static/dashboard.css" not in dashboard_response.text
        assert 'href="/status#read-api"' in dashboard_response.text

        matches_response = client.get("/matches")
        assert matches_response.status_code == 200
        assert "Matches &amp; Live Monitor" in matches_response.text
        assert 'href="/static/dashboard.css"' in matches_response.text
        assert 'src="/static/dashboard.js"' in matches_response.text
        assert "/matches/static/dashboard.css" not in matches_response.text
        assert 'data-auto-submit-filters="true"' in matches_response.text
        assert "Apply Filters" not in matches_response.text

        match_detail_response = client.get(f"/matches/{fixture.fixture_id}")
        assert match_detail_response.status_code == 200
        assert fixture.home_team_name in match_detail_response.text
        assert fixture.away_team_name in match_detail_response.text
        assert "Fixture And Prediction State" in match_detail_response.text

        predictions_page = client.get("/predictions")
        assert predictions_page.status_code == 200
        assert "Current Football Predictions" in predictions_page.text
        assert 'href="/static/dashboard.css"' in predictions_page.text
        assert 'src="/static/dashboard.js"' in predictions_page.text

        candidates_page = client.get("/candidates")
        assert candidates_page.status_code == 200
        assert "Current-Market Candidates" in candidates_page.text
        assert 'href="/static/dashboard.css"' in candidates_page.text
        assert 'src="/static/dashboard.js"' in candidates_page.text

        performance_page = client.get("/performance")
        assert performance_page.status_code == 200
        assert "Historical validation and live paper betting results." in performance_page.text
        assert 'href="/static/dashboard.css"' in performance_page.text
        assert 'src="/static/dashboard.js"' in performance_page.text

        status_page = client.get("/status")
        assert status_page.status_code == 200
        assert "System health, feeds and model readiness." in status_page.text
        assert "Route B Composite" in status_page.text
        assert "Route A Direct Classifier" in status_page.text
        assert "NOT IMPLEMENTED" in status_page.text
        assert "/api/system/status" in status_page.text
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


def test_dashboard_services_current_market_candidate_creates_idempotent_paper_entry(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    fixture = _fixture()
    config = replace(
        _dashboard_config(tmp_path),
        current_odds_connected=True,
        current_xg_connected=True,
        enable_odds_sync=True,
    )
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
    assert performance_payload["recent_paper_bets"] == []
    assert len(performance_payload["open_paper_positions"]) == 1
    assert performance_payload["live_paper_kpis"][0] == ("Bets", "1")

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

    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        edge_decisions = football_edge_desk.fetch_decision_history(conn, fixture_id=fixture.fixture_id)
        edge_consensus = football_edge_desk.fetch_consensus_evaluations(conn, fixture_id=fixture.fixture_id)

    assert edge_consensus
    assert any(
        row["market_family"] == "1x2" and row["status"] == "INSUFFICIENT_AGENTS"
        for row in edge_consensus
    )
    assert any(
        row["market_family"] == "btts" and row["status"] == "INSUFFICIENT_AGENTS"
        for row in edge_consensus
    )
    assert edge_decisions
    assert any(
        row["market_family"] == "1x2" and row["decision_status"] == "NO_BET"
        for row in edge_decisions
    )
    assert any(
        row["market_family"] in {"btts", "goals_total"} and row["decision_status"] == "NO_BET"
        for row in edge_decisions
    )

    asyncio.run(services.shutdown())


def test_dashboard_services_settles_finished_paper_trade_idempotently_and_reports_live_results(tmp_path: Path):
    kickoff_utc = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(microsecond=0).isoformat()
    pending_fixture = replace(
        _fixture(),
        kickoff_utc=kickoff_utc,
        status=FIXTURE_STATUS_PRE_MATCH,
    )
    config = replace(
        _dashboard_config(tmp_path),
        current_odds_connected=True,
        enable_odds_sync=True,
    )
    provider = FakeFixtureProvider([pending_fixture])
    services = DashboardServices(
        config=config,
        fixture_provider=provider,
        market_provider=FakeMarketProvider(configured=True),
    )

    asyncio.run(services.initialize())
    with football_paper_trading.connect(config.paper_trading_db_path) as conn:
        football_paper_trading.record_paper_trade_entry(
            conn,
            football_paper_trading.PaperTradeEntry(
                prediction_id="pred_settle_1",
                fixture_id=pending_fixture.fixture_id,
                model_snapshot_id="snapshot_settle_1",
                model_version="football_day11_manager_core_v1",
                market="1x2",
                selection="HOME",
                model_probability=0.52,
                model_fair_price=1.92,
                available_market_price=2.2,
                edge=0.06,
                minimum_acceptable_price=2.0,
                decision="PAPER_CANDIDATE",
                decision_timestamp=(datetime.now(timezone.utc) - timedelta(hours=4)).replace(microsecond=0).isoformat(),
                kickoff=kickoff_utc,
                safeguard_state="READY",
                entry_eligibility="ELIGIBLE",
                bookmaker="Pinnacle",
                provider="fake-market-provider",
                entry_odds=2.2,
                entry_captured_at=(datetime.now(timezone.utc) - timedelta(hours=4)).replace(microsecond=0).isoformat(),
            ),
        )

    asyncio.run(services.sync_once())
    initial_performance = services.get_performance_payload()
    assert initial_performance["current_paper_summary"]["settled_positions"] == 0
    assert initial_performance["current_paper_summary"]["pending_positions"] == 1
    assert initial_performance["recent_paper_bets"] == []
    assert len(initial_performance["open_paper_positions"]) == 1

    provider.fixtures = [
        replace(
            pending_fixture,
            status=FIXTURE_STATUS_FINISHED,
            home_score=2,
            away_score=1,
            last_provider_update=(datetime.now(timezone.utc) - timedelta(minutes=2)).replace(microsecond=0).isoformat(),
        )
    ]

    asyncio.run(services.sync_once())
    asyncio.run(services.sync_once())

    with football_paper_trading.connect(config.paper_trading_db_path) as conn:
        paper_rows = football_paper_trading.fetch_paper_trade_rows(conn)
        updates = conn.execute(
            "SELECT trade_state, payload_json FROM paper_trade_updates ORDER BY paper_trade_update_id"
        ).fetchall()

    assert len(paper_rows) == 1
    assert paper_rows[0]["trade_state"] == "WON"
    assert paper_rows[0]["entry_odds"] == pytest.approx(2.2)
    assert len(updates) == 1
    assert updates[0]["trade_state"] == "WON"
    settlement_context = json.loads(str(updates[0]["payload_json"]))["settlement_context"]
    assert settlement_context["provider"] == "fake-provider"
    assert settlement_context["provider_fixture_id"] == "fixture-1"
    assert settlement_context["final_status"] == "FINISHED"
    assert settlement_context["actual_home_goals"] == 2
    assert settlement_context["actual_away_goals"] == 1

    performance_payload = services.get_performance_payload()
    assert performance_payload["current_paper_summary"]["settled_positions"] == 1
    assert performance_payload["current_paper_summary"]["wins"] == 1
    assert performance_payload["current_paper_summary"]["pending_positions"] == 0
    assert performance_payload["current_paper_summary"]["strike_rate"] == pytest.approx(1.0)
    assert performance_payload["current_paper_summary"]["net_units"] == pytest.approx(1.2)
    assert performance_payload["current_paper_summary"]["roi"] == pytest.approx(1.2)
    assert performance_payload["live_paper_market_rows"][0]["market"] == "1x2"
    assert performance_payload["live_paper_market_rows"][0]["wins"] == 1
    assert performance_payload["recent_paper_bets"][0]["final_score"] == "2-1"
    assert performance_payload["recent_paper_bets"][0]["fixture_label"] == "Arsenal vs Chelsea"
    assert performance_payload["recent_paper_bets"][0]["result_provider"] == "fake-provider"
    assert performance_payload["open_paper_positions"] == []

    app = create_app(
        config=config,
        fixture_provider=provider,
        market_provider=FakeMarketProvider(configured=True),
    )
    with TestClient(app) as client:
        response = client.get("/performance")
        api_response = client.get("/api/performance")

    assert response.status_code == 200
    assert "Recent Settled Bets" in response.text
    assert "Current Open Paper Bets" in response.text
    assert "Arsenal vs Chelsea" in response.text
    assert "2-1" in response.text
    assert "WON" in response.text
    assert api_response.status_code == 200
    assert api_response.json()["summary"]["MODEL_VALIDATION"] == "PROMISING"
    assert api_response.json()["current_paper_summary"]["roi"] == pytest.approx(1.2)

    asyncio.run(services.shutdown())


def test_best_pick_market_labels_are_human_readable() -> None:
    assert _humanize_market_selection(
        market_family="1x2",
        selection="HOME",
        line=None,
        home_team_name="Arsenal",
        away_team_name="Chelsea",
    )["bet_label"] == "Arsenal to Win"
    assert _humanize_market_selection(
        market_family="btts",
        selection="YES",
        line=None,
        home_team_name="Arsenal",
        away_team_name="Chelsea",
    )["bet_label"] == "Both Teams to Score - Yes"
    assert _humanize_market_selection(
        market_family="goals_total",
        selection="OVER",
        line=2.5,
        home_team_name="Arsenal",
        away_team_name="Chelsea",
    )["bet_label"] == "Over 2.5 Goals"
    assert _humanize_market_selection(
        market_family="corner_total",
        selection="OVER",
        line=9.5,
        home_team_name="Arsenal",
        away_team_name="Chelsea",
    )["bet_label"] == "Over 9.5 Corners"
    assert _humanize_market_selection(
        market_family="asian_handicap",
        selection="HOME",
        line=-0.5,
        home_team_name="Arsenal",
        away_team_name="Chelsea",
    )["bet_label"] == "Arsenal -0.5"


def test_odds_runtime_requirements_include_corner_total(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    config = replace(_dashboard_config(tmp_path), current_odds_connected=True)
    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([]),
        market_provider=_default_market_provider(config),
    )

    requirements = services._odds_feed_runtime_requirements()
    assert "corner_total" in requirements["supported_markets"]


def test_dashboard_services_best_picks_use_qualified_current_truth(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    qualified_fixture = _fixture(fixture_id="fake:arsenal_chelsea")
    config = replace(
        _dashboard_config(tmp_path),
        current_odds_connected=True,
        current_xg_connected=True,
        enable_odds_sync=True,
    )
    market_provider = FakeMarketProvider()
    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([qualified_fixture]),
        market_provider=market_provider,
        xg_profile_factory=_xg_factory_for_fixtures(qualified_fixture),
    )

    asyncio.run(services.initialize())
    market_provider.snapshots_by_fixture[qualified_fixture.fixture_id] = [
        _market_snapshot(
            fixture=qualified_fixture,
            bookmaker="Pinnacle",
            market="1x2",
            selection="HOME",
            decimal_odds=2.05,
            reference_designation="reference",
            provider_market_id="pin-home-1",
        ),
        _market_snapshot(
            fixture=qualified_fixture,
            bookmaker="Pinnacle",
            market="1x2",
            selection="DRAW",
            decimal_odds=3.35,
            reference_designation="reference",
            provider_market_id="pin-draw-1",
        ),
        _market_snapshot(
            fixture=qualified_fixture,
            bookmaker="Pinnacle",
            market="1x2",
            selection="AWAY",
            decimal_odds=3.95,
            reference_designation="reference",
            provider_market_id="pin-away-1",
        ),
        _market_snapshot(
            fixture=qualified_fixture,
            bookmaker="bet365",
            market="1x2",
            selection="HOME",
            decimal_odds=10.0,
            reference_designation="available",
            provider_market_id="b365-home-1",
        ),
    ]
    asyncio.run(services.sync_once())

    dashboard_payload = services.get_dashboard_payload()
    candidates_payload = services.get_candidates_payload()

    assert dashboard_payload["best_picks_summary"]["count"] == 1
    assert {row["fixture_id"] for row in dashboard_payload["best_picks"]} == {qualified_fixture.fixture_id}
    assert candidates_payload["preliminary_candidates"]

    candidate = candidates_payload["preliminary_candidates"][0]
    best_pick = dashboard_payload["best_picks"][0]
    assert best_pick["bet_label"] == "Arsenal to Win"
    assert best_pick["model_probability"] == candidate["model_probability"]
    assert best_pick["market_odds"] == candidate["best_available_price"]
    assert best_pick["fair_odds"] == candidate["model_fair_decimal_price"]
    assert best_pick["edge"] == candidate["settlement_aware_edge"]
    assert best_pick["paper_qualification_state"] == "QUALIFIED"

    asyncio.run(services.shutdown())


def test_dashboard_services_today_working_rows_keep_bet_and_no_bet_truth(
    tmp_path: Path,
    monkeypatch,
) -> None:
    kickoff_one = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(minute=0, second=0, microsecond=0).isoformat()
    kickoff_two = (datetime.now(timezone.utc) + timedelta(hours=5)).replace(minute=0, second=0, microsecond=0).isoformat()
    bet_fixture = replace(_fixture(fixture_id="fake:bet_row"), kickoff_utc=kickoff_one)
    no_bet_fixture = replace(
        _fixture(
            fixture_id="fake:no_bet_row",
            home_team_name="Sevilla",
            away_team_name="Betis",
        ),
        kickoff_utc=kickoff_two,
    )
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([bet_fixture, no_bet_fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )
    fixture_rows = [
        bet_fixture.to_dict(timezone_name="UTC"),
        no_bet_fixture.to_dict(timezone_name="UTC"),
    ]

    monkeypatch.setattr(
        services,
        "_filter_fixture_rows",
        lambda *, bucket, fixture_rows: fixture_rows if bucket == "today" else [],
    )
    monkeypatch.setattr(
        services,
        "_market_evaluation_map",
        lambda: {
            "bet": {
                "decision_rows": [
                    {
                        "fixture_id": bet_fixture.fixture_id,
                        "decision": "PRELIMINARY_CANDIDATE",
                        "market_family": "1x2",
                        "selection": "HOME",
                        "line": None,
                        "model_probability": 0.63,
                        "best_available_price": 2.05,
                        "settlement_aware_edge": 0.042,
                        "paper_entry_eligible": True,
                        "market_freshness": "FRESH",
                        "day12_state": "PASS",
                        "reason_codes": [],
                    },
                    {
                        "fixture_id": no_bet_fixture.fixture_id,
                        "market_family": "1x2",
                        "selection": "HOME",
                        "line": None,
                        "model_probability": 0.71,
                        "best_available_price": 1.30,
                        "settlement_aware_edge": -0.059,
                        "paper_entry_eligible": False,
                        "market_freshness": "FRESH",
                        "day12_state": "PASS",
                        "decision": "NO_BET",
                        "reason_codes": ["PRICE_NO_VALUE"],
                    },
                ],
            }
        },
    )

    rows = services._build_today_working_rows(fixture_rows)

    assert [row["desk_label"] for row in rows] == ["BET", "NO BET"]
    assert rows[0]["is_actionable"] is True
    assert rows[0]["is_featured"] is True
    assert rows[0]["bet_label"] == "Arsenal to Win"
    assert rows[0]["market_odds"] == 2.05
    assert math.isclose(rows[0]["implied_probability"], 1 / 2.05, rel_tol=0.0, abs_tol=1e-9)
    assert rows[1]["is_actionable"] is False
    assert rows[1]["desk_note"] == "Price No Value"
    assert rows[1]["bet_label"] == "Sevilla to Win"

def test_dashboard_payload_current_slate_counts_use_persisted_paper_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    kickoff_one = (datetime.now(timezone.utc) + timedelta(hours=18)).replace(minute=0, second=0, microsecond=0).isoformat()
    kickoff_two = (datetime.now(timezone.utc) + timedelta(hours=42)).replace(minute=0, second=0, microsecond=0).isoformat()
    first_fixture = replace(_fixture(fixture_id="fake:slate_one"), kickoff_utc=kickoff_one)
    second_fixture = replace(
        _fixture(
            fixture_id="fake:slate_two",
            home_team_name="Sevilla",
            away_team_name="Betis",
        ),
        kickoff_utc=kickoff_two,
    )
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([first_fixture, second_fixture]),
        market_provider=FakeMarketProvider(configured=True),
    )
    fixture_rows = [
        first_fixture.to_dict(timezone_name="UTC"),
        second_fixture.to_dict(timezone_name="UTC"),
    ]
    first_bet_row = {
        "fixture_id": first_fixture.fixture_id,
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "decision": "PRELIMINARY_CANDIDATE",
        "current_decision_state": "BET",
        "market_family": "1x2",
        "selection": "HOME",
        "line": None,
        "model_probability": 0.63,
        "model_fair_decimal_price": 1.59,
        "best_available_price": 2.05,
        "best_available_bookmaker": "bet365",
        "best_available_source_timestamp": "2026-08-28T08:28:07Z",
        "settlement_aware_edge": 0.2915,
        "paper_entry_eligible": True,
        "paper_trade_id": "paper_current_1",
        "market_freshness": "FRESH",
        "day12_state": "PASSED",
        "reason_codes": [],
    }
    second_bet_row = {
        "fixture_id": second_fixture.fixture_id,
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "decision": "PRELIMINARY_CANDIDATE",
        "current_decision_state": "BET",
        "market_family": "goals_total",
        "selection": "OVER",
        "line": 2.5,
        "model_probability": 0.58,
        "model_fair_decimal_price": 1.72,
        "best_available_price": 1.95,
        "best_available_bookmaker": "matchbook",
        "best_available_source_timestamp": "2026-08-28T08:28:49Z",
        "settlement_aware_edge": 0.1311,
        "paper_entry_eligible": True,
        "paper_trade_id": "paper_current_2",
        "market_freshness": "FRESH",
        "day12_state": "PASSED",
        "reason_codes": [],
    }
    second_no_bet_row = {
        "fixture_id": second_fixture.fixture_id,
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "decision": "NO_BET",
        "current_decision_state": "NO_BET_VALUE",
        "market_family": "btts",
        "selection": "YES",
        "line": None,
        "model_probability": 0.54,
        "model_fair_decimal_price": 1.85,
        "best_available_price": 1.72,
        "best_available_bookmaker": "bet365",
        "best_available_source_timestamp": "2026-08-28T08:28:07Z",
        "settlement_aware_edge": -0.0712,
        "paper_entry_eligible": False,
        "market_freshness": "FRESH",
        "day12_state": "PASSED",
        "reason_codes": ["PRICE_NO_VALUE"],
    }
    paper_rows = [
        {"paper_trade_id": "paper_current_1", "market": "1x2", "trade_state": "PENDING", "entry_odds": 2.05},
        {"paper_trade_id": "paper_current_2", "market": "goals_total", "trade_state": "OPEN", "entry_odds": 1.95},
        {"paper_trade_id": "paper_old", "market": "1x2", "trade_state": "OPEN", "entry_odds": 2.20},
    ]

    monkeypatch.setattr(services, "_all_fixtures_with_predictions", lambda: fixture_rows)
    monkeypatch.setattr(services, "_future_fixture_rows", lambda rows: fixture_rows)
    monkeypatch.setattr(
        services,
        "_filter_fixture_rows",
        lambda *, bucket, fixture_rows: [] if bucket in {"today", "live", "upcoming"} else [],
    )
    monkeypatch.setattr(
        services,
        "_market_evaluation_map",
        lambda: {
            first_fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [first_bet_row],
                "display_rows": [first_bet_row],
            },
            second_fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [second_bet_row, second_no_bet_row],
                "display_rows": [second_bet_row, second_no_bet_row],
            },
        },
    )
    monkeypatch.setattr(
        services,
        "_paper_positions",
        lambda: (paper_rows, football_paper_trading.paper_trading_status(paper_rows)),
    )

    payload = services.get_dashboard_payload()
    metrics = {item["label"]: item for item in payload["glance_metrics"]}
    funnel = {item["label"]: item for item in payload["desk_funnel"]}

    assert payload["best_picks_summary"]["count"] == 2
    assert payload["current_slate_summary"]["fixture_count"] == 2
    assert payload["current_slate_summary"]["count"] == 3
    assert payload["current_slate_summary"]["bet_count"] == 2
    assert payload["current_slate_summary"]["no_bet_count"] == 1
    assert payload["current_slate_summary"]["paper_entry_count"] == 2
    assert payload["current_slate_summary"]["pending_count"] == 2
    assert payload["system_status"]["current_fixture_state"]["today_count"] == 0
    assert metrics["Analysed"] == {"label": "Analysed", "value": "2", "note": "current slate fixtures"}
    assert metrics["Approved"] == {"label": "Approved", "value": "2", "note": "qualified"}
    assert metrics["Suggested Bets"] == {"label": "Suggested Bets", "value": "3", "note": "all paper bets"}
    assert metrics["Settled"] == {"label": "Settled", "value": "0", "note": "final results"}
    assert metrics["Won"] == {"label": "Won", "value": "0", "note": "full wins"}
    assert metrics["Lost"] == {"label": "Lost", "value": "0", "note": "full losses"}
    assert metrics["Push"] == {"label": "Push", "value": "0", "note": "stake returned"}
    assert metrics["Pending"] == {"label": "Pending", "value": "3", "note": "open paper"}
    assert metrics["Strike Rate"] == {"label": "Strike Rate", "value": "NOT AVAILABLE", "note": "pushes excluded"}
    assert metrics["P/L"] == {"label": "P/L", "value": "+0.00", "note": "net units"}
    assert metrics["ROI"] == {"label": "ROI", "value": "NOT AVAILABLE", "note": "settled stake only"}
    assert funnel["Desk rows"]["value"] == "3"
    assert funnel["Suggested bets"]["value"] == "2"
    assert funnel["BET rows"]["value"] == "2"
    assert funnel["NO BET rows"]["value"] == "1"


def test_dashboard_home_published_pick_uses_frozen_entry_truth_and_no_bet_has_no_paper_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = _fixture(home_team_name="Aston Villa", away_team_name="Arsenal")
    no_bet_fixture = replace(_fixture(fixture_id="fake:no_bet", home_team_name="Sevilla", away_team_name="Betis"))
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture, no_bet_fixture]),
        market_provider=FakeMarketProvider(configured=True),
    )
    fixture_rows = [
        fixture.to_dict(timezone_name="UTC"),
        no_bet_fixture.to_dict(timezone_name="UTC"),
    ]
    published_bet_row = {
        "fixture_id": fixture.fixture_id,
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "decision": "PRELIMINARY_CANDIDATE",
        "current_decision_state": "BET",
        "market_family": "1x2",
        "selection": "HOME",
        "line": None,
        "model_probability": 0.4012,
        "model_fair_decimal_price": 2.49,
        "best_available_price": 6.8,
        "best_available_bookmaker": "bet365",
        "best_available_source_timestamp": "2026-08-28T09:10:00Z",
        "settlement_aware_edge": 1.1466,
        "paper_entry_eligible": True,
        "paper_trade_id": "paper_published_1",
        "market_freshness": "FRESH",
        "day12_state": "PASSED",
        "reason_codes": [],
    }
    no_bet_row = {
        "fixture_id": no_bet_fixture.fixture_id,
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "decision": "NO_BET",
        "current_decision_state": "NO_BET_VALUE",
        "market_family": "1x2",
        "selection": "HOME",
        "line": None,
        "model_probability": 0.55,
        "model_fair_decimal_price": 1.82,
        "best_available_price": 1.61,
        "best_available_bookmaker": "Pinnacle",
        "best_available_source_timestamp": "2026-08-28T09:10:00Z",
        "settlement_aware_edge": -0.114,
        "paper_entry_eligible": False,
        "market_freshness": "FRESH",
        "day12_state": "PASSED",
        "reason_codes": ["PRICE_NO_VALUE"],
    }
    paper_rows = [
        {
            "paper_trade_id": "paper_published_1",
            "fixture_id": fixture.fixture_id,
            "market": "1x2",
            "selection": "HOME",
            "line": None,
            "model_probability": 0.315679,
            "model_fair_price": 3.1678,
            "entry_odds": 7.0,
            "edge": 1.2098,
            "bookmaker": "Pinnacle",
            "entry_captured_at": "2026-08-28T08:45:00Z",
            "decision_timestamp": "2026-08-28T08:45:00Z",
            "trade_state": "PENDING",
            "latest_update_payload": None,
        }
    ]

    monkeypatch.setattr(services, "_all_fixtures_with_predictions", lambda: fixture_rows)
    monkeypatch.setattr(services, "_future_fixture_rows", lambda rows: fixture_rows)
    monkeypatch.setattr(
        services,
        "_filter_fixture_rows",
        lambda *, bucket, fixture_rows: fixture_rows if bucket == "today" else [],
    )
    monkeypatch.setattr(
        services,
        "_market_evaluation_map",
        lambda: {
            fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [published_bet_row],
                "display_rows": [published_bet_row],
            },
            no_bet_fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [no_bet_row],
                "display_rows": [no_bet_row],
            },
        },
    )
    monkeypatch.setattr(
        services,
        "_paper_positions",
        lambda: (paper_rows, football_paper_trading.paper_trading_status(paper_rows)),
    )

    payload = services.get_dashboard_payload()
    published_pick = payload["best_picks"][0]

    assert published_pick["entry_odds"] == pytest.approx(7.0)
    assert published_pick["market_odds"] == pytest.approx(7.0)
    assert published_pick["current_market_odds"] == pytest.approx(6.8)
    assert published_pick["entry_edge"] == pytest.approx(1.2098)
    assert published_pick["edge"] == pytest.approx(1.2098)
    assert published_pick["current_market_edge"] == pytest.approx(1.1466)
    assert published_pick["entry_model_probability"] == pytest.approx(0.315679)
    assert published_pick["model_probability"] == pytest.approx(0.315679)
    assert published_pick["entry_fair_odds"] == pytest.approx(3.1678)
    assert published_pick["fair_odds"] == pytest.approx(3.1678)
    assert published_pick["paper_trade_state"] == "PENDING"
    assert payload["other_decision_rows"][0]["desk_label"] == "NO BET"

    app = create_app(services=services)
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Entry 7.00" not in response.text
    assert "Current odds 6.80" in response.text
    assert "Current edge +114.66%" in response.text
    assert "7.00" in response.text
    assert "+120.98%" in response.text
    assert "PENDING" in response.text
    other_section = response.text.split("Other Desk Decisions", 1)[1]
    assert "NO BET" in other_section
    assert "PENDING" not in other_section


def test_dashboard_home_best_picks_empty_state_and_agent_truth(tmp_path: Path) -> None:
    fixture = _fixture()
    app = create_app(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Current Slate Best Picks" in response.text
    assert "Current qualified Football selections." in response.text
    assert "Other Desk Decisions" in response.text
    assert "Desk Funnel" in response.text
    assert "No qualified picks currently meet the model, value and risk requirements." in response.text
    assert "No current market decision rows are available" in response.text
    assert "Route B Composite" in response.text
    assert "Route A Direct Classifier" in response.text
    assert "REJECTED" in response.text
    assert "Agent 3" in response.text
    assert "NOT IMPLEMENTED" in response.text
    assert "Agent 1" not in response.text
    assert "Agent 2" not in response.text


def test_dashboard_home_uses_market_unavailable_when_provider_is_connected(tmp_path: Path) -> None:
    fixture = _fixture()
    app = create_app(
        config=replace(
            _dashboard_config(tmp_path),
            current_odds_connected=True,
            enable_odds_sync=True,
        ),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=True),
    )

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "MARKET_UNAVAILABLE" in response.text
    assert "MARKET_DATA_NOT_CONNECTED" not in response.text


def test_dashboard_performance_empty_state_uses_neutral_paper_copy(tmp_path: Path) -> None:
    fixture = _fixture()
    app = create_app(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=True),
    )

    with TestClient(app) as client:
        response = client.get("/performance")

    assert response.status_code == 200
    assert "No settled paper bets yet." in response.text
    assert "No open paper positions right now." in response.text
    assert "current odds provider is still not configured" not in response.text


def test_match_detail_separates_current_market_from_published_entry_and_uses_current_gate_truth(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = _fixture(home_team_name="Aston Villa", away_team_name="Arsenal")
    config = replace(
        _dashboard_config(tmp_path),
        current_odds_connected=True,
        current_xg_connected=True,
        enable_odds_sync=True,
    )
    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=True),
    )

    asyncio.run(services.initialize())
    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        prediction = dashboard_storage.fetch_latest_prediction_for_fixture(conn, fixture.fixture_id)
        assert prediction is not None
        prediction["safeguard_status"] = "BLOCKED"
        prediction["safeguard_reason"] = "POINT-IN-TIME XG NOT AVAILABLE"
        dashboard_storage.save_prediction(conn, prediction, updated_at="2026-08-28T09:12:00+00:00")

    bet_row = {
        "fixture_id": fixture.fixture_id,
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "decision": "PRELIMINARY_CANDIDATE",
        "current_decision_state": "BET",
        "market_family": "1x2",
        "selection": "HOME",
        "line": None,
        "model_probability": 0.4012,
        "model_fair_decimal_price": 2.49,
        "best_available_price": 6.8,
        "best_available_bookmaker": "bet365",
        "best_available_source_timestamp": "2026-08-28T09:10:00Z",
        "settlement_aware_edge": 1.1466,
        "minimum_acceptable_price": 2.41,
        "paper_entry_eligible": True,
        "paper_trade_id": "paper_detail_1",
        "market_freshness": "FRESH",
        "day12_state": "PASSED",
        "xg_status": "PASS",
        "reason_codes": [],
    }
    paper_rows = [
        {
            "paper_trade_id": "paper_detail_1",
            "fixture_id": fixture.fixture_id,
            "market": "1x2",
            "selection": "HOME",
            "line": None,
            "model_probability": 0.315679,
            "model_fair_price": 3.1678,
            "entry_odds": 7.0,
            "edge": 1.2098,
            "bookmaker": "Pinnacle",
            "entry_captured_at": "2026-08-28T08:45:00Z",
            "decision_timestamp": "2026-08-28T08:45:00Z",
            "trade_state": "PENDING",
            "latest_update_payload": None,
        }
    ]
    monkeypatch.setattr(
        services,
        "_market_evaluation_map",
        lambda: {
            fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [bet_row],
                "display_rows": [bet_row],
                "counts": {"preliminary_candidates": 1, "passed_safeguards": 1},
                "market_snapshot_count": 1,
                "freshness": "FRESH",
            }
        },
    )
    monkeypatch.setattr(
        services,
        "_paper_positions",
        lambda: (paper_rows, football_paper_trading.paper_trading_status(paper_rows)),
    )

    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["decision_gates"]["current_market"] == "BET"
    assert payload["decision_gates"]["safeguard"] == "PASS"
    assert payload["decision_gates"]["safeguard_reason"] == "PASSED"
    assert payload["decision_gates"]["point_in_time_xg"] == "PASS"
    assert payload["paper_entries"][0]["entry_odds"] == pytest.approx(7.0)
    assert payload["paper_entries"][0]["current_market_odds"] == pytest.approx(6.8)
    assert payload["paper_entries"][0]["edge"] == pytest.approx(1.2098)
    assert payload["paper_entries"][0]["current_market_edge"] == pytest.approx(1.1466)

    app = create_app(services=services)
    with TestClient(app) as client:
        response = client.get(f"/matches/{fixture.fixture_id}")

    assert response.status_code == 200
    assert "Paper Entry" in response.text
    assert "Current-Market Evaluation" in response.text
    assert "Current odds 6.80" in response.text
    assert "Current edge +114.66%" in response.text
    assert "7.00" in response.text
    assert "+120.98%" in response.text
    assert "<span>Safeguard</span><strong>PASS</strong>" in response.text
    assert "<span>Point-In-Time xG</span><strong>PASS</strong>" in response.text
    assert "<span>Safeguard Reason</span><strong>PASSED</strong>" in response.text

    asyncio.run(services.shutdown())
