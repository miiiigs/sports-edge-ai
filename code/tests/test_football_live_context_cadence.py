from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sports_ai.dashboard.live_production_services import LiveProductionDashboardServices


class _FakeContextProvider:
    def __init__(self) -> None:
        self.injury_calls = 0
        self.lineup_calls = 0

    def configured(self) -> bool:
        return True

    def get_injuries(self, provider_fixture_id: str) -> dict:
        self.injury_calls += 1
        return {
            "provider_fixture_id": provider_fixture_id,
            "request_state": "OK",
            "evidence_state": "EMPTY",
            "injuries": [],
        }

    def get_lineups(self, provider_fixture_id: str) -> dict:
        self.lineup_calls += 1
        return {
            "provider_fixture_id": provider_fixture_id,
            "request_state": "OK",
            "evidence_state": "AVAILABLE",
            "lineups": [{"team_id": 1}, {"team_id": 2}],
        }


def _service(tmp_path) -> LiveProductionDashboardServices:
    service = object.__new__(LiveProductionDashboardServices)
    service.config = SimpleNamespace(paper_trading_db_path=tmp_path / "paper.sqlite")
    service._context_provider = _FakeContextProvider()
    service._context_mode = "shadow"
    service._context_lookahead_hours = 24.0
    service._lineup_window_minutes = 90.0
    service._require_final_lineup = False
    service._european_congestion_hours = lambda _fixture: None
    service._provider_fixture_id_for_context = lambda _fixture: "12345"
    return service


def _candidate(hours_to_kickoff: float) -> dict:
    kickoff = (datetime.now(timezone.utc) + timedelta(hours=hours_to_kickoff)).isoformat()
    return {
        "fixture_context": {
            "fixture_id": "fixture-1",
            "kickoff_utc": kickoff,
            "provider": "api-football",
            "provider_fixture_id": "12345",
        },
        "model_row": {
            "fixture_id": "fixture-1",
            "market_family": "1x2",
            "selection": "HOME",
            "line": None,
        },
        "best_offer": {"decimal_odds": 2.05},
    }


def test_live_context_fetches_injuries_but_not_lineups_before_final_window(tmp_path):
    service = _service(tmp_path)

    signals = service._context_signals(_candidate(2.0))

    assert service._context_provider.injury_calls == 1
    assert service._context_provider.lineup_calls == 0
    assert signals["available_evidence"]["injuries"] is True
    assert "lineups" not in signals["required_evidence"]


def test_live_context_fetches_lineups_once_inside_final_window(tmp_path):
    service = _service(tmp_path)

    signals = service._context_signals(_candidate(0.5))

    assert service._context_provider.injury_calls == 1
    assert service._context_provider.lineup_calls == 1
    assert signals["available_evidence"]["lineups"] is True
    assert signals["confirmed_lineup_team_count"] == 2
