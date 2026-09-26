from __future__ import annotations

from types import SimpleNamespace

import pytest

from sports_ai.dashboard import production_services, storage
from sports_ai.dashboard.production_services import (
    ProductionDashboardServices,
    _parse_history_probe_once_spec,
)


def test_history_probe_once_spec_validation() -> None:
    assert _parse_history_probe_once_spec("") is None
    assert _parse_history_probe_once_spec("fra1:2025") == ("FRA1", 2025, 3)
    assert _parse_history_probe_once_spec("GER1:2024:1") == ("GER1", 2024, 1)

    with pytest.raises(ValueError):
        _parse_history_probe_once_spec("UNKNOWN:2025")
    with pytest.raises(ValueError):
        _parse_history_probe_once_spec("FRA1:not-a-year")
    with pytest.raises(ValueError):
        _parse_history_probe_once_spec("FRA1:2025:4")


def test_history_probe_once_is_persistent_and_not_replayed(tmp_path, monkeypatch) -> None:
    dashboard_db = tmp_path / "football_dashboard.sqlite"
    with storage.connect(dashboard_db) as conn:
        storage.initialize_database(conn)

    calls = {"count": 0}

    class FakeResult:
        def to_dict(self):
            return {
                "competition_id": "FRA1",
                "season": 2025,
                "state": "READY_TO_BACKFILL",
                "bounded_probe_requests": 5,
                "projected_backfill_requests": 310,
                "sampled_gap_ready_count": 3,
                "sampled_fixture_count": 3,
                "reason": None,
            }

    def fake_probe(provider, *, target, season, sample_size):
        calls["count"] += 1
        assert target.competition_id == "FRA1"
        assert season == 2025
        assert sample_size == 3
        return FakeResult()

    monkeypatch.setattr(production_services, "run_historical_coverage_probe", fake_probe)

    service = ProductionDashboardServices.__new__(ProductionDashboardServices)
    service.config = SimpleNamespace(dashboard_db_path=dashboard_db)
    service.fixture_provider = object()
    service._history_probe_once_spec = "FRA1:2025"
    service._history_probe_state = {
        "configured": True,
        "state": "PENDING",
        "token_id": None,
        "result": None,
        "error_code": None,
    }

    service._run_history_probe_once()
    first = dict(service._history_probe_state)
    service._run_history_probe_once()
    second = dict(service._history_probe_state)

    assert calls["count"] == 1
    assert first["state"] == "COMPLETED"
    assert first["result"]["state"] == "READY_TO_BACKFILL"
    assert second == first
