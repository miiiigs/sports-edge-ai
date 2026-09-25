from __future__ import annotations

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
