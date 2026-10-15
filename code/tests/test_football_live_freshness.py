from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sports_ai.dashboard import storage
from sports_ai.dashboard.models import FIXTURE_STATUS_LIVE
from sports_ai.dashboard.production_services import (
    PRODUCTION_RUNTIME_STATE_KEY,
    ProductionDashboardServices,
)
from sports_ai.dashboard.services import DashboardServices


def _live_row(*, fixture_id: str, kickoff_utc: str, last_provider_update: str) -> dict[str, object]:
    return {
        "fixture_id": fixture_id,
        "kickoff_utc": kickoff_utc,
        "last_provider_update": last_provider_update,
        "status": FIXTURE_STATUS_LIVE,
    }


def test_live_bucket_hides_stale_and_invalid_future_live_rows() -> None:
    now_utc = datetime.now(timezone.utc)
    services = DashboardServices.__new__(DashboardServices)
    services.config = SimpleNamespace(display_timezone="Asia/Manila")

    current = _live_row(
        fixture_id="current",
        kickoff_utc=(now_utc - timedelta(hours=1)).isoformat(),
        last_provider_update=(now_utc - timedelta(minutes=20)).isoformat(),
    )
    stale = _live_row(
        fixture_id="stale",
        kickoff_utc=(now_utc - timedelta(days=3)).isoformat(),
        last_provider_update=(now_utc - timedelta(days=3)).isoformat(),
    )
    invalid_future = _live_row(
        fixture_id="future",
        kickoff_utc=(now_utc + timedelta(days=2)).isoformat(),
        last_provider_update=now_utc.isoformat(),
    )

    rows = services._filter_fixture_rows(
        bucket="live",
        fixture_rows=[current, stale, invalid_future],
    )

    assert [row["fixture_id"] for row in rows] == ["current"]


def test_live_bucket_falls_back_to_kickoff_when_update_timestamp_is_missing() -> None:
    now_utc = datetime.now(timezone.utc)
    services = DashboardServices.__new__(DashboardServices)
    services.config = SimpleNamespace(display_timezone="Asia/Manila")

    recent = _live_row(
        fixture_id="recent-fallback",
        kickoff_utc=(now_utc - timedelta(hours=2)).isoformat(),
        last_provider_update="",
    )
    stale = _live_row(
        fixture_id="stale-fallback",
        kickoff_utc=(now_utc - timedelta(hours=8)).isoformat(),
        last_provider_update="",
    )

    rows = services._filter_fixture_rows(
        bucket="live",
        fixture_rows=[recent, stale],
    )

    assert [row["fixture_id"] for row in rows] == ["recent-fallback"]


def test_serverless_cold_start_restores_research_refresh_cadence(tmp_path) -> None:
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    dashboard_db = tmp_path / "football_dashboard.sqlite"
    with storage.connect(dashboard_db) as conn:
        storage.initialize_database(conn)
        storage.put_runtime_state(
            conn,
            state_key=PRODUCTION_RUNTIME_STATE_KEY,
            state_value={
                "research_refresh": {
                    "state": "READY",
                    "last_attempt_at": now_utc.isoformat(),
                    "last_success_at": now_utc.isoformat(),
                    "last_error": None,
                    "season_code": "2627",
                    "successful_divisions": ["E0"],
                    "failed_divisions": {},
                    "changed_rows": 0,
                    "latest_match_date": "2026-09-20",
                }
            },
            updated_at=now_utc.isoformat(),
        )

    services = ProductionDashboardServices.__new__(ProductionDashboardServices)
    services.config = SimpleNamespace(dashboard_db_path=dashboard_db)
    services._last_research_refresh_attempt = None
    services._research_refresh_state = {
        "state": "NOT_RUN",
        "last_attempt_at": None,
        "last_success_at": None,
        "last_error": None,
        "season_code": None,
        "successful_divisions": [],
        "failed_divisions": {},
        "changed_rows": 0,
        "latest_match_date": None,
    }

    services._restore_persisted_research_refresh_state()

    assert services._last_research_refresh_attempt == now_utc
    assert services._research_refresh_state["state"] == "READY"
    assert services._research_refresh_state["season_code"] == "2627"


def test_production_initialize_primes_freshness_before_startup_sync(monkeypatch) -> None:
    events: list[str] = []
    services = ProductionDashboardServices.__new__(ProductionDashboardServices)

    monkeypatch.setattr(
        ProductionDashboardServices,
        "_restore_persisted_research_refresh_state",
        lambda self: events.append("restore"),
    )
    monkeypatch.setattr(
        ProductionDashboardServices,
        "_refresh_research_if_due",
        lambda self, *, force, rebuild_snapshot: events.append("research"),
    )
    monkeypatch.setattr(
        ProductionDashboardServices,
        "_refresh_competition_freshness",
        lambda self: events.append("freshness"),
    )
    monkeypatch.setattr(
        ProductionDashboardServices,
        "_log_production_freshness",
        lambda self: events.append("log"),
    )
    monkeypatch.setattr(
        ProductionDashboardServices,
        "_persist_production_runtime_state",
        lambda self: events.append("persist"),
    )

    async def fake_base_initialize(self) -> None:
        events.append("base_initialize")

    monkeypatch.setattr(DashboardServices, "initialize", fake_base_initialize)

    asyncio.run(ProductionDashboardServices.initialize(services))

    assert events == [
        "restore",
        "research",
        "freshness",
        "base_initialize",
        "freshness",
        "log",
        "persist",
    ]


def _freshness_services(*, supported: tuple[str, ...] | None) -> ProductionDashboardServices:
    services = ProductionDashboardServices.__new__(ProductionDashboardServices)
    services.snapshot = None if supported is None else SimpleNamespace(supported_competitions=supported)
    services._competition_model_freshness = {
        "ENG1": {"latest_complete_match_date": "2026-10-12", "age_days": 2, "max_age_days": 21, "state": "FRESH"},
        "FRA1": {"latest_complete_match_date": "2026-05-29", "age_days": 138, "max_age_days": 21, "state": "STALE"},
    }
    return services


def test_unsupported_stale_competition_does_not_degrade_readiness() -> None:
    services = _freshness_services(supported=("ENG1",))

    summary = services._freshness_summary()

    assert summary == {
        "state": "READY",
        "stale_competitions": [],
        "unsupported_stale_competitions": ["FRA1"],
    }


def test_supported_stale_competition_still_degrades_readiness() -> None:
    services = _freshness_services(supported=("ENG1", "FRA1"))

    summary = services._freshness_summary()

    assert summary["state"] == "DEGRADED"
    assert summary["stale_competitions"] == ["FRA1"]
    assert summary["unsupported_stale_competitions"] == []


def test_freshness_fails_closed_without_snapshot_or_supported_rows() -> None:
    assert _freshness_services(supported=None)._freshness_summary()["state"] == "DEGRADED"
    assert _freshness_services(supported=("ESP1",))._freshness_summary() == {
        "state": "DEGRADED",
        "stale_competitions": [],
        "unsupported_stale_competitions": ["FRA1"],
    }


def test_system_status_and_health_ignore_unsupported_stale_competition(monkeypatch) -> None:
    services = _freshness_services(supported=("ENG1",))
    services._research_refresh_state = {"state": "READY"}
    services._context_mode = "off"
    services._context_provider = None
    monkeypatch.setattr(
        DashboardServices,
        "_build_system_status",
        lambda self, **kwargs: {"current_prediction_pipeline_ready": "YES", "live_paper_pipeline_ready": "YES"},
    )
    monkeypatch.setattr(DashboardServices, "get_health_payload", lambda self: {"status": "ok"})

    status = services._build_system_status()
    health = services.get_health_payload()

    assert status["current_prediction_pipeline_ready"] == "YES"
    assert status["live_paper_pipeline_ready"] == "YES"
    assert status["production_hardening"]["state"] == "READY"
    assert status["production_hardening"]["unsupported_stale_competitions"] == ["FRA1"]
    assert "FRA1" in status["production_hardening"]["competition_model_freshness"]
    assert health["model_state_freshness"]["state"] == "READY"
    assert health["model_state_freshness"]["stale_competitions"] == []
    assert health["model_state_freshness"]["unsupported_stale_competitions"] == ["FRA1"]
