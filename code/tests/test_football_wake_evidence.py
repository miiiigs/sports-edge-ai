from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar

from fastapi.testclient import TestClient
from sports_ai.dashboard import storage
from sports_ai.dashboard.app import create_app
from sports_ai.dashboard.low_memory_app import LowMemoryWebDashboardServices
from sports_ai.dashboard.models import (
    FIXTURE_STATUS_PRE_MATCH,
    PREDICTION_STATUS_READY,
    NormalizedFixture,
    utc_now_iso,
)
from sports_ai.dashboard.services import (
    RUNTIME_ROLE_SCHEDULER,
    RUNTIME_ROLE_WEB,
    WAKE_EVIDENCE_STATE_KEY,
    DashboardConfig,
    DashboardServices,
)


class _FakeFixtureProvider:
    def provider_name(self) -> str:
        return "fake-provider"

    def configured(self) -> bool:
        return True


class _FakeMarketProvider:
    def provider_name(self) -> str:
        return "fake-market-provider"

    def configured(self) -> bool:
        return False


class _FakeBudget:
    def quota_state(self) -> str:
        return "HEALTHY"

    def snapshot(self) -> dict[str, Any]:
        return {
            "paid_provider_calls_allowed": "YES",
            "quota": {
                "state": "HEALTHY",
                "daily_remaining": 7000,
                "effective_remaining": 4000,
                "operational_remaining": 4000,
                "requests_used": 123,
                "captured_at": "2026-10-19T00:00:00+00:00",
            },
            "circuit": {"state": "CLOSED"},
        }


class _StubSnapshot:
    snapshot_id = "snapshot-test"
    model_as_of = "2026-10-14T23:59:59+00:00"
    supported_competitions = ("ENG1",)
    diagnostics: ClassVar[dict[str, Any]] = {"model_as_of": model_as_of}
    model_version = "v-test"
    route_a_artifact = None


def _config(tmp_path: Path, *, role: str) -> DashboardConfig:
    return replace(
        DashboardConfig.default(),
        repo_root=tmp_path,
        research_db_path=tmp_path / "research.sqlite",
        dashboard_db_path=tmp_path / "dashboard.sqlite",
        paper_trading_db_path=tmp_path / "paper.sqlite",
        model_pick_db_path=tmp_path / "model_picks.sqlite",
        performance_output_dir=tmp_path / "performance",
        operational_data_dir=tmp_path,
        storage_mode="test",
        persistent_data_dir_configured=False,
        runtime_role=role,
        enable_scheduler=False,
        startup_sync_timeout_seconds=1,
    )


def _service(tmp_path: Path, *, role: str = RUNTIME_ROLE_SCHEDULER) -> DashboardServices:
    service = DashboardServices(
        config=_config(tmp_path, role=role),
        fixture_provider=_FakeFixtureProvider(),
        market_provider=_FakeMarketProvider(),
    )
    service._provider_budget = _FakeBudget()  # type: ignore[assignment]
    service._edge_desk_available = False
    with storage.connect(service.config.dashboard_db_path) as conn:
        storage.initialize_database(conn)
    return service


def _fixture() -> NormalizedFixture:
    kickoff = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    return NormalizedFixture(
        fixture_id="fake:wake_evidence",
        provider_fixture_id="fixture-wake-1",
        provider="fake-provider",
        competition_id="ENG1",
        competition_name="Premier League",
        division_code="E0",
        season="2026",
        home_team_id="100",
        home_team_name="Arsenal",
        away_team_id="200",
        away_team_name="Chelsea",
        kickoff_utc=kickoff,
        status=FIXTURE_STATUS_PRE_MATCH,
        elapsed_minute=None,
        home_score=None,
        away_score=None,
        last_provider_update=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        source_version="fake_fixture_provider_v1",
        provider_payload={"fixture_id": "fake:wake_evidence"},
    )


def _valid_since() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()


def _completed_result() -> dict[str, Any]:
    return {
        "state": "CONNECTED",
        "last_error": None,
        "last_counts": {"normalized": 3},
        "sync_diagnostics": {"query_count": 2},
    }


def test_successful_cycle_persists_useful_evidence_and_verifies(tmp_path: Path):
    service = _service(tmp_path)
    service._prediction_state.update({"ready_fixture_count": 3, "blocked_fixture_count": 0})
    started_at = utc_now_iso()
    since = _valid_since()

    service._begin_sync_cycle(sync_kind="startup", started_at=started_at)
    service._record_new_prediction(
        {
            "prediction_id": "prediction-1",
            "fixture_id": "fake:wake_evidence",
            "prediction_status": PREDICTION_STATUS_READY,
        }
    )
    service._finish_sync_cycle(
        started_at=started_at,
        sync_kind="startup",
        result=_completed_result(),
    )

    with storage.connect(service.config.dashboard_db_path) as conn:
        persisted = storage.get_runtime_state(conn, WAKE_EVIDENCE_STATE_KEY)
    assert persisted is not None
    assert persisted["last_cycle_state"] == "COMPLETED"
    assert persisted["useful_cycle"] is True
    assert persisted["forward_predictions_new_last_cycle"] == 1
    assert persisted["forward_predictions_new_ready_last_cycle"] == 1
    assert persisted["forward_predictions_noop_reason"] is None
    assert persisted["fixtures_considered"] == 3
    assert persisted["provider_budget"]["quota_state"] == "HEALTHY"

    payload, status_code = service.get_wake_status(since=since)
    assert status_code == 200
    assert payload["wake_verified"] is True
    assert payload["status"] == "ok"
    assert payload["evidence"]["last_useful_cycle_at"] is not None


def test_web_role_verifies_from_persisted_scheduler_evidence(tmp_path: Path):
    scheduler = _service(tmp_path)
    scheduler._prediction_state.update({"ready_fixture_count": 2, "blocked_fixture_count": 0})
    started_at = utc_now_iso()
    since = _valid_since()
    scheduler._begin_sync_cycle(sync_kind="startup", started_at=started_at)
    scheduler._finish_sync_cycle(
        started_at=started_at,
        sync_kind="startup",
        result=_completed_result(),
    )

    web = _service(tmp_path, role=RUNTIME_ROLE_WEB)
    payload, status_code = web.get_wake_status(since=since)

    assert status_code == 200
    assert payload["wake_verified"] is True
    assert payload["evidence"]["last_cycle_state"] == "COMPLETED"
    assert payload["evidence"]["process_role"] == "web"
    assert payload["evidence"]["evidence_process_role"] == "scheduler"


def test_failed_cycle_is_not_verified_and_reports_failure(tmp_path: Path):
    service = _service(tmp_path)
    service._prediction_state.update({"ready_fixture_count": 3, "blocked_fixture_count": 0})
    started_at = utc_now_iso()
    since = _valid_since()

    service._begin_sync_cycle(sync_kind="startup", started_at=started_at)
    service._finish_sync_cycle_failed(
        started_at=started_at,
        sync_kind="startup",
        exc=TimeoutError("fixture provider read timed out"),
    )

    payload, status_code = service.get_wake_status(since=since)
    assert status_code == 503
    assert payload["wake_verified"] is False
    assert payload["status"] == "failed"
    assert payload["evidence"]["last_cycle_state"] == "FAILED"
    assert "last_cycle_error" not in payload["evidence"]
    assert "TimeoutError" in service.get_wake_evidence_summary()["last_cycle_error"]
    assert payload["evidence"]["last_useful_cycle_at"] is None


def test_running_cycle_is_pending_not_verified(tmp_path: Path):
    service = _service(tmp_path)
    since = _valid_since()
    service._begin_sync_cycle(sync_kind="startup", started_at=utc_now_iso())

    payload, status_code = service.get_wake_status(since=since)

    assert status_code == 503
    assert payload["wake_verified"] is False
    assert payload["status"] == "pending"
    assert payload["evidence"]["last_cycle_state"] == "RUNNING"


def test_repeated_cycle_for_existing_prediction_is_explicit_noop(tmp_path: Path):
    service = _service(tmp_path)
    service.snapshot = _StubSnapshot()
    fixture = _fixture()
    version_key = service._prediction_version_key(fixture, service.snapshot)
    existing_payload = {
        "prediction_id": "prediction-existing",
        "fixture_id": fixture.fixture_id,
        "prediction_status": PREDICTION_STATUS_READY,
        "generated_at": utc_now_iso(),
        "model_as_of": service.snapshot.model_as_of,
        "model_snapshot_id": service.snapshot.snapshot_id,
        "version_key": version_key,
    }
    with storage.connect(service.config.dashboard_db_path) as conn:
        storage.upsert_fixtures(conn, [fixture], updated_at=utc_now_iso())
        storage.save_prediction(conn, existing_payload, updated_at=utc_now_iso())

    service._prediction_state.update({"ready_fixture_count": 1, "blocked_fixture_count": 0})
    service._begin_sync_cycle(sync_kind="startup", started_at=utc_now_iso())
    with storage.connect(service.config.dashboard_db_path) as conn:
        first = service.ensure_prediction(conn, fixture)
        second = service.ensure_prediction(conn, fixture)
        prediction_count = conn.execute("SELECT COUNT(*) FROM dashboard_predictions").fetchone()[0]
    service._finish_sync_cycle(
        started_at=service._wake_evidence_state["last_cycle_started_at"],
        sync_kind="startup",
        result=_completed_result(),
    )

    assert first["prediction_id"] == "prediction-existing"
    assert second["prediction_id"] == "prediction-existing"
    assert prediction_count == 1
    assert service.get_wake_evidence_summary()["forward_predictions_new_last_cycle"] == 0
    assert (
        service.get_wake_evidence_summary()["forward_predictions_noop_reason"]
        == "FORWARD_PREDICTIONS_ALREADY_PERSISTED"
    )
    assert service.get_wake_evidence_summary()["useful_cycle"] is True


def test_health_wake_route_returns_503_until_cycle_is_verified(tmp_path: Path):
    service = LowMemoryWebDashboardServices(config=_config(tmp_path, role=RUNTIME_ROLE_WEB))
    service._provider_budget = _FakeBudget()  # type: ignore[assignment]
    service._edge_desk_available = False
    with storage.connect(service.config.dashboard_db_path) as conn:
        storage.initialize_database(conn)

    app = create_app(services=service)
    with TestClient(app) as client:
        pending = client.get("/health/wake")
        assert pending.status_code == 503
        assert pending.json()["wake_verified"] is False

        with storage.connect(service.config.dashboard_db_path) as conn:
            storage.put_runtime_state(
                conn,
                state_key=WAKE_EVIDENCE_STATE_KEY,
                state_value={
                    **service._wake_evidence_state,
                    "last_cycle_state": "COMPLETED",
                    "last_cycle_completed_at": utc_now_iso(),
                    "useful_cycle": True,
                    "last_useful_cycle_at": utc_now_iso(),
                },
                updated_at=utc_now_iso(),
            )

        verified = client.get("/health/wake")
        assert verified.status_code == 200
        assert verified.json()["wake_verified"] is True

        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert "wake_evidence" in health.json()


def test_invalid_since_is_rejected(tmp_path: Path):
    service = _service(tmp_path)

    payload, status_code = service.get_wake_status(since="not-a-timestamp")

    assert status_code == 400
    assert payload["error"] == "invalid_since"
    assert payload["wake_verified"] is False


def test_public_wake_and_health_never_publish_raw_failures_or_provider_budget(tmp_path: Path):
    scheduler = _service(tmp_path)
    scheduler._wake_evidence_state.update({
        "last_cycle_state": "FAILED",
        "last_cycle_completed_at": utc_now_iso(),
        "last_cycle_error": "API_TOKEN=private-token server=https://internal",
        "startup_sync_last_error": "PASSWORD=private-password",
        "provider_budget": {"secret_quota": "sensitive-private-budget"},
    })
    scheduler._persist_wake_evidence()
    # The public routes execute in the lightweight web child. Using the
    # full scheduler here would require an unrelated research football_events DB.
    web = LowMemoryWebDashboardServices(config=_config(tmp_path, role=RUNTIME_ROLE_WEB))
    web._provider_budget = _FakeBudget()  # type: ignore[assignment]
    web._edge_desk_available = False
    app = create_app(services=web)
    with TestClient(app) as client:
        status = client.get("/health/wake?since=" + _valid_since())
        assert status.status_code == 503
        health = client.get("/health")
        assert health.status_code == 200
        for response in (status, health):
            body = response.text
            assert "private-token" not in body
            assert "private-password" not in body
            assert "sensitive-private-budget" not in body
            assert "last_cycle_error" not in response.json()["evidence"] if response is status else (
                "last_cycle_error" not in response.json()["wake_evidence"]
            )


def test_later_failed_cycle_cannot_be_masked_by_earlier_verified_success(tmp_path: Path):
    scheduler = _service(tmp_path)
    scheduler._prediction_state.update({"ready_fixture_count": 3, "blocked_fixture_count": 0})
    since = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    started_at = utc_now_iso()
    scheduler._begin_sync_cycle(sync_kind="startup", started_at=started_at)
    scheduler._finish_sync_cycle(
        started_at=started_at, sync_kind="startup", result=_completed_result()
    )
    # Explicit separation avoids relying on wall-clock subsecond timing.
    scheduler._wake_evidence_state["last_useful_cycle_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=2)
    ).isoformat()
    scheduler._persist_wake_evidence()
    failed_start = utc_now_iso()
    scheduler._begin_sync_cycle(sync_kind="startup", started_at=failed_start)
    scheduler._finish_sync_cycle_failed(
        started_at=failed_start, sync_kind="startup",
        exc=TimeoutError("provider unavailable"),
    )
    payload, status_code = scheduler.get_wake_status(since=since)
    assert status_code == 503
    assert payload["wake_verified"] is False
    assert payload["status"] == "failed"


def test_railway_dockerfile_uses_repository_root_context():
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "ops/football_wake/Dockerfile").read_text(encoding="utf-8")
    assert "COPY ops/football_wake/wake_and_verify.sh" in dockerfile
    assert (root / "ops/football_wake/wake_and_verify.sh").exists()


def test_wake_shell_does_not_log_public_health_response_bodies():
    root = Path(__file__).resolve().parents[2]
    source = (root / "ops/football_wake/wake_and_verify.sh").read_text(
        encoding="utf-8"
    )
    assert "HEALTH_OUTPUT" not in source
    assert 'body=${WAKE_BODY}' not in source
    assert "wake-http-failed" in source
    assert "wake-failed" in source


def test_public_health_allowlist_does_not_bypass_auth_for_prefix_lookalikes(tmp_path: Path, monkeypatch):
    """Only the real wake endpoints are unauthenticated, not all /health* URLs."""
    monkeypatch.setenv("SPORTS_AI_AUTH_ENABLED", "1")
    monkeypatch.setenv("SPORTS_AI_AUTH_DB_PATH", str(tmp_path / "auth.sqlite"))
    monkeypatch.setenv("SPORTS_AI_AUTH_COOKIE_SECURE", "0")
    service = LowMemoryWebDashboardServices(config=_config(tmp_path, role=RUNTIME_ROLE_WEB))
    service._provider_budget = _FakeBudget()  # type: ignore[assignment]
    service._edge_desk_available = False
    with storage.connect(service.config.dashboard_db_path) as conn:
        storage.initialize_database(conn)
    app = create_app(services=service)
    with TestClient(app, follow_redirects=False) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/health/wake").status_code == 503
        for spoofed_path in ("/health-internal", "/healthcheck", "/health/private"):
            response = client.get(spoofed_path)
            assert response.status_code in (302, 303, 307)
            assert response.headers["location"].startswith("/login")
