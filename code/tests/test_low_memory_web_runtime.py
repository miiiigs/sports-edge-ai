from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from sports_ai.dashboard import runtime_supervisor, scheduler_worker
from sports_ai.dashboard import storage as dashboard_storage
from sports_ai.dashboard.app import create_app
from sports_ai.dashboard.low_memory_app import (
    MODEL_SNAPSHOT_STATE_KEY,
    LowMemoryWebDashboardServices,
    PersistedSnapshotView,
)
from sports_ai.dashboard.models import FIXTURE_STATUS_PRE_MATCH, NormalizedFixture
from sports_ai.dashboard.services import DashboardConfig, RUNTIME_ROLE_WEB


def _config(tmp_path: Path) -> DashboardConfig:
    return DashboardConfig(
        repo_root=tmp_path,
        research_db_path=tmp_path / "research.sqlite",
        dashboard_db_path=tmp_path / "dashboard.sqlite",
        paper_trading_db_path=tmp_path / "paper.sqlite",
        performance_output_dir=tmp_path / "performance",
        operational_data_dir=tmp_path,
        storage_mode="test_tmpdir",
        persistent_data_dir_configured=False,
        enable_scheduler=False,
        current_odds_connected=False,
        enable_odds_sync=False,
        current_xg_connected=False,
        allow_paid_provider_calls=False,
        startup_sync_timeout_seconds=0,
        runtime_role=RUNTIME_ROLE_WEB,
    )


def _fixture() -> NormalizedFixture:
    kickoff = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
    return NormalizedFixture(
        fixture_id="fake:cost-test",
        provider_fixture_id="cost-test",
        provider="fake-provider",
        competition_id="ENG1",
        competition_name="Premier League",
        division_code="E0",
        season="2026",
        home_team_id="100",
        home_team_name="Home FC",
        away_team_id="200",
        away_team_name="Away FC",
        kickoff_utc=kickoff,
        status=FIXTURE_STATUS_PRE_MATCH,
        elapsed_minute=None,
        home_score=None,
        away_score=None,
        last_provider_update=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        source_version="fake_v1",
        provider_payload={"fixture_id": "fake:cost-test"},
    )


def _seed_model_state(config: DashboardConfig) -> None:
    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        dashboard_storage.initialize_database(conn)
        dashboard_storage.put_runtime_state(
            conn,
            state_key=MODEL_SNAPSHOT_STATE_KEY,
            state_value={
                "snapshot_id": "snapshot_cost_test",
                "model_as_of": "2026-05-24T23:59:59+00:00",
                "supported_competitions": ["ENG1", "ENG2", "ESP1"],
                "source_match_cutoff": "2026-05-24T23:59:59+00:00",
                "model_version": "route_b_cost_test",
                "route_a_status": "REJECTED",
                "route_a_artifact_id": "route_a_rejected_test",
            },
            updated_at="2026-09-13T02:00:00+00:00",
            initialize_schema=False,
        )


def test_web_runtime_never_builds_full_model_and_uses_exact_scheduler_metadata(tmp_path, monkeypatch):
    def fail_full_snapshot(*args, **kwargs):
        raise AssertionError("web process must not replay the full Football model")

    monkeypatch.setattr("sports_ai.dashboard.services.build_current_football_model_snapshot", fail_full_snapshot)
    config = _config(tmp_path)
    _seed_model_state(config)
    services = LowMemoryWebDashboardServices(config=config)
    asyncio.run(services.initialize())

    assert isinstance(services.snapshot, PersistedSnapshotView)
    assert services.snapshot.snapshot_id == "snapshot_cost_test"
    assert services.snapshot.model_as_of == "2026-05-24T23:59:59+00:00"
    assert services.snapshot.supported_competitions == ("ENG1", "ENG2", "ESP1")
    assert services.snapshot.model_version == "route_b_cost_test"
    assert services._startup_sync_task is None
    assert services._background_task is None

    status = services.get_system_status()
    assert status["current_model_snapshot"]["state"] == "READY"
    assert status["current_model_snapshot"]["snapshot_id"] == "snapshot_cost_test"
    assert status["current_model_snapshot"]["supported_competitions"] == ["ENG1", "ENG2", "ESP1"]
    assert status["current_prediction_pipeline_ready"] == "YES"
    health = services.get_health_payload()
    assert health["prediction_snapshot_ready"] is True
    assert health["startup_sync_task_running"] is False
    assert health["local_scheduler_running"] is False

    asyncio.run(services.shutdown())


def test_low_memory_web_serves_core_dashboard_routes_without_model_replay(tmp_path, monkeypatch):
    def fail_full_snapshot(*args, **kwargs):
        raise AssertionError("web process must not replay the full Football model")

    monkeypatch.setattr("sports_ai.dashboard.services.build_current_football_model_snapshot", fail_full_snapshot)
    monkeypatch.setenv("SPORTS_AI_AUTH_ENABLED", "false")
    config = _config(tmp_path)
    _seed_model_state(config)
    services = LowMemoryWebDashboardServices(config=config)
    app = create_app(services=services)

    with TestClient(app) as client:
        for route in ("/health", "/status", "/today", "/matches", "/betting-desk", "/performance"):
            response = client.get(route)
            assert response.status_code == 200, (route, response.text)

    assert services._startup_sync_task is None
    assert services._background_task is None


def test_prediction_evidence_fallback_preserves_existing_dashboard_data(tmp_path, monkeypatch):
    def fail_full_snapshot(*args, **kwargs):
        raise AssertionError("web process must not replay the full Football model")

    monkeypatch.setattr("sports_ai.dashboard.services.build_current_football_model_snapshot", fail_full_snapshot)
    services = LowMemoryWebDashboardServices(config=_config(tmp_path))
    asyncio.run(services.initialize())
    assert services.snapshot is None

    fixture = _fixture()
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with dashboard_storage.connect(services.config.dashboard_db_path) as conn:
        dashboard_storage.upsert_fixtures(conn, [fixture], updated_at=generated_at, initialize_schema=False)
        dashboard_storage.save_prediction(
            conn,
            {
                "prediction_id": "prediction_cost_test",
                "fixture_id": fixture.fixture_id,
                "prediction_status": "READY",
                "generated_at": generated_at,
                "model_as_of": "2026-05-24T23:59:59+00:00",
                "model_snapshot_id": "snapshot_cost_test",
                "version_key": "snapshot_cost_test|fake:cost-test",
            },
            updated_at=generated_at,
            initialize_schema=False,
        )

    services._refresh_persisted_runtime_state_for_web()
    assert isinstance(services.snapshot, PersistedSnapshotView)
    assert services.snapshot.snapshot_id == "snapshot_cost_test"
    assert services.snapshot.supported_competitions == ("ENG1",)

    with dashboard_storage.connect(services.config.dashboard_db_path) as conn:
        rows = dashboard_storage.fetch_all_predictions(conn, initialize_schema=False)
    assert len(rows) == 1
    assert rows[0]["model_snapshot_id"] == "snapshot_cost_test"

    asyncio.run(services.shutdown())


def test_scheduler_publishes_full_snapshot_metadata_without_prediction_changes(tmp_path):
    services = LowMemoryWebDashboardServices(config=_config(tmp_path))
    asyncio.run(services.initialize())
    services.snapshot = SimpleNamespace(
        snapshot_id="scheduler_snapshot",
        model_as_of="2026-05-24T23:59:59+00:00",
        model_version="route_b_v1",
        supported_competitions=("ENG1", "ENG2", "ESP1"),
        diagnostics={
            "model_as_of": "2026-05-24T23:59:59+00:00",
            "route_a": {"overall_status": "REJECTED", "artifact_id": "route_a_1"},
        },
    )

    scheduler_worker._persist_model_snapshot_state(services)
    with dashboard_storage.connect(services.config.dashboard_db_path) as conn:
        state = dashboard_storage.get_runtime_state(
            conn,
            scheduler_worker.MODEL_SNAPSHOT_STATE_KEY,
            initialize_schema=False,
        )
    assert state == {
        "snapshot_id": "scheduler_snapshot",
        "model_as_of": "2026-05-24T23:59:59+00:00",
        "supported_competitions": ["ENG1", "ENG2", "ESP1"],
        "source_match_cutoff": "2026-05-24T23:59:59+00:00",
        "model_version": "route_b_v1",
        "route_a_status": "REJECTED",
        "route_a_artifact_id": "route_a_1",
    }
    asyncio.run(services.shutdown())


def test_supervisor_uses_low_memory_web_entrypoint():
    specs = runtime_supervisor.build_child_specs(port="9129")
    assert specs[0].name == "web"
    assert "sports_ai.dashboard.low_memory_app:app" in specs[0].argv
    assert specs[1].name == "scheduler"
