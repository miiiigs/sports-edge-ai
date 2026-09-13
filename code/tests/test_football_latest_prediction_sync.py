from __future__ import annotations

from sports_ai.dashboard.live_production_services import LiveProductionDashboardServices
from sports_ai.dashboard.services import DashboardServices


def _ready_row() -> dict[str, object]:
    return {
        "fixture_id": "api-football:latest",
        "prediction": {
            "prediction_status": "READY",
            "generated_at": "2026-09-13T06:00:00+00:00",
            "blockers": [],
            "warnings": [],
        },
    }


def _stale_blocked_row() -> dict[str, object]:
    return {
        "fixture_id": "api-football:latest",
        "prediction": {
            "prediction_status": "BLOCKED",
            "generated_at": "2026-09-12T06:00:00+00:00",
            "blockers": ["AWAY_TEAM_IDENTITY_UNRESOLVED"],
            "warnings": [],
        },
    }


def _bare_live_service(latest_rows: list[dict[str, object]]) -> LiveProductionDashboardServices:
    service = object.__new__(LiveProductionDashboardServices)
    service._prediction_state = {
        "state": "RUNNING",
        "last_prediction_generated_at": None,
        "last_successful_prediction_at": None,
        "last_prediction_error": None,
        "pending_fixture_count": 0,
        "blocked_fixture_count": 0,
        "ready_fixture_count": 0,
    }
    service._provider_state = {
        "last_counts": {
            "predictions_ready": 0,
            "predictions_blocked": 0,
            "predictions_pending": 0,
        }
    }
    service.snapshot = object()
    service._all_fixtures_with_predictions = lambda: list(latest_rows)
    service._future_fixture_rows = lambda rows: list(rows)
    return service


def test_live_prediction_health_reloads_latest_prediction_rows() -> None:
    service = _bare_live_service([_ready_row()])

    service._update_prediction_state([_stale_blocked_row()])

    assert service._prediction_state["ready_fixture_count"] == 1
    assert service._prediction_state["blocked_fixture_count"] == 0
    assert service._prediction_state["pending_fixture_count"] == 0
    assert service._provider_state["last_counts"]["predictions_ready"] == 1
    assert service._provider_state["last_counts"]["predictions_blocked"] == 0


def test_live_market_sync_reloads_latest_prediction_rows(monkeypatch) -> None:
    latest_rows = [_ready_row()]
    service = _bare_live_service(latest_rows)
    captured: dict[str, object] = {}

    def capture_market_sync(self, rows_with_predictions, conn):
        captured["rows"] = list(rows_with_predictions)
        captured["conn"] = conn

    monkeypatch.setattr(DashboardServices, "_run_market_sync", capture_market_sync)
    sentinel_conn = object()

    service._run_market_sync([_stale_blocked_row()], sentinel_conn)

    assert captured["rows"] == latest_rows
    assert captured["conn"] is sentinel_conn
