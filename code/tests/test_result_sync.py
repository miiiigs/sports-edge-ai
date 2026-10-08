"""Focused tests for the dedicated current-result (sporting truth) sync.

No live provider calls are made. These verify the canonical status contract,
conservative final/corner merging, deterministic selection/rotation, terminal
stop, and that the dedicated result-sync path never touches the market
collector, paper ledger, Value Bets, portfolio, settlement or historical APIs.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import sports_ai.dashboard.services as services_mod
from sports_ai.dashboard import result_sync as rs
from sports_ai.dashboard import storage
from sports_ai.dashboard.models import NormalizedFixture
from sports_ai.dashboard.scheduler_worker import _run_result_sync_runtime
from sports_ai.dashboard.services import (
    RUNTIME_ROLE_COMBINED,
    DashboardConfig,
    DashboardServices,
)
from sports_ai.market_data.providers import NotConfiguredMarketProvider
from sports_ai.storage import football_model_pick_ledger as ledger


# --------------------------------------------------------------------------- contract
def test_status_normalization_and_unknown_fail_closed():
    assert rs.normalize_api_football_status("NS") == rs.STATE_PRE_MATCH
    assert rs.normalize_api_football_status("1H") == rs.STATE_LIVE
    assert rs.normalize_api_football_status("HT") == rs.STATE_HALFTIME
    assert rs.normalize_api_football_status("FT") == rs.STATE_FINISHED
    assert rs.normalize_api_football_status("AET") == rs.STATE_FINISHED
    assert rs.normalize_api_football_status("PST") == rs.STATE_POSTPONED
    assert rs.normalize_api_football_status("ABD") == rs.STATE_ABANDONED
    assert rs.normalize_api_football_status("WO") == rs.STATE_CANCELLED
    assert rs.normalize_api_football_status("MADE_UP") == rs.STATE_UNKNOWN
    assert rs.normalize_api_football_status(None) == rs.STATE_UNKNOWN


def test_canonical_status_prefers_raw_provider_short():
    payload = {"fixture": {"status": {"short": "ABD"}}}
    assert rs.canonical_status_for_fixture(normalized_status="FINISHED", provider_payload=payload) == rs.STATE_ABANDONED
    assert rs.canonical_status_for_fixture(normalized_status="FINISHED", provider_payload={}) == rs.STATE_FINISHED


def test_finished_verified_requires_status_and_both_scores():
    assert rs.is_finished_verified(status=rs.STATE_FINISHED, home_score=1, away_score=0)
    assert not rs.is_finished_verified(status=rs.STATE_FINISHED, home_score=1, away_score=None)
    assert not rs.is_finished_verified(status=rs.STATE_LIVE, home_score=1, away_score=0)


def test_progression_merges_are_allowed():
    for existing, incoming in (
        (rs.STATE_PRE_MATCH, rs.STATE_LIVE),
        (rs.STATE_LIVE, rs.STATE_HALFTIME),
        (rs.STATE_HALFTIME, rs.STATE_FINISHED),
        (rs.STATE_LIVE, rs.STATE_FINISHED),
    ):
        decision = rs.merge_fixture_truth(
            existing_status=existing, existing_home_score=None, existing_away_score=None,
            incoming_status=incoming, incoming_home_score=(2 if incoming == rs.STATE_FINISHED else None),
            incoming_away_score=(1 if incoming == rs.STATE_FINISHED else None),
        )
        assert decision.action == "APPLY", (existing, incoming, decision)


def test_final_status_without_score_rejected():
    decision = rs.merge_fixture_truth(
        existing_status=rs.STATE_LIVE, existing_home_score=None, existing_away_score=None,
        incoming_status=rs.STATE_FINISHED, incoming_home_score=None, incoming_away_score=None,
    )
    assert decision.action == "REJECT"


def test_score_presence_without_final_status_is_not_final():
    decision = rs.merge_fixture_truth(
        existing_status=rs.STATE_PRE_MATCH, existing_home_score=None, existing_away_score=None,
        incoming_status=rs.STATE_LIVE, incoming_home_score=2, incoming_away_score=1,
    )
    assert decision.action == "APPLY"
    assert not rs.is_finished_verified(status=rs.STATE_LIVE, home_score=2, away_score=1)


def test_postponed_cancelled_abandoned_are_not_finished():
    for state in (rs.STATE_POSTPONED, rs.STATE_CANCELLED, rs.STATE_ABANDONED):
        assert not rs.is_finished_verified(status=state, home_score=1, away_score=0)
        assert rs.needs_result_sync(status=state, home_score=1, away_score=0) is (state == rs.STATE_POSTPONED)


def test_unknown_status_rejected():
    decision = rs.merge_fixture_truth(
        existing_status=rs.STATE_PRE_MATCH, existing_home_score=None, existing_away_score=None,
        incoming_status=rs.STATE_UNKNOWN, incoming_home_score=None, incoming_away_score=None,
    )
    assert decision.action == "REJECT"


def test_same_final_idempotent_and_conflict_blocked():
    same = rs.merge_fixture_truth(
        existing_status=rs.STATE_FINISHED, existing_home_score=2, existing_away_score=1,
        incoming_status=rs.STATE_FINISHED, incoming_home_score=2, incoming_away_score=1,
    )
    assert same.action == "NOOP"
    conflict = rs.merge_fixture_truth(
        existing_status=rs.STATE_FINISHED, existing_home_score=2, existing_away_score=1,
        incoming_status=rs.STATE_FINISHED, incoming_home_score=3, incoming_away_score=1,
    )
    assert conflict.action == "CONFLICT"
    reject = rs.merge_fixture_truth(
        existing_status=rs.STATE_FINISHED, existing_home_score=2, existing_away_score=1,
        incoming_status=rs.STATE_LIVE, incoming_home_score=2, incoming_away_score=1,
    )
    assert reject.action == "REJECT"


def test_corner_merging_requires_both_and_blocks_conflict():
    assert rs.merge_corner_truth(existing_home_corners=None, existing_away_corners=None, incoming_home_corners=5, incoming_away_corners=None).action == "NOOP"
    assert rs.merge_corner_truth(existing_home_corners=None, existing_away_corners=None, incoming_home_corners=5, incoming_away_corners=4).action == "APPLY"
    assert rs.merge_corner_truth(existing_home_corners=5, existing_away_corners=4, incoming_home_corners=5, incoming_away_corners=4).action == "NOOP"
    assert rs.merge_corner_truth(existing_home_corners=5, existing_away_corners=4, incoming_home_corners=6, incoming_away_corners=4).action == "CONFLICT"


def test_terminal_fixtures_stop_polling():
    assert rs.needs_result_sync(status=rs.STATE_FINISHED, home_score=2, away_score=1) is False
    assert rs.needs_result_sync(status=rs.STATE_CANCELLED, home_score=None, away_score=None) is False
    assert rs.needs_result_sync(status=rs.STATE_ABANDONED, home_score=None, away_score=None) is False
    assert rs.needs_result_sync(status=rs.STATE_LIVE, home_score=1, away_score=0) is True


# --------------------------------------------------------------------------- selection
def _cand(fid, status, *, kickoff=None, provider_fixture_id=None, pick=True, attempt=None, hs=None, as_=None):
    return rs.ResultFixtureCandidate(
        fixture_id=fid,
        provider_fixture_id=provider_fixture_id if provider_fixture_id is not None else fid.split(":")[-1],
        kickoff_utc=kickoff or "2026-10-08T10:00:00+00:00",
        status=status,
        home_score=hs,
        away_score=as_,
        has_live_issued_pick=pick,
        last_result_sync_attempt_at=attempt,
    )


def test_selection_priority_and_bounds():
    candidates = [
        _cand("f-pm", rs.STATE_PRE_MATCH, kickoff="2026-10-08T09:00:00+00:00"),
        _cand("f-live", rs.STATE_LIVE, kickoff="2026-10-08T09:30:00+00:00"),
        _cand("f-ht", rs.STATE_HALFTIME, kickoff="2026-10-08T09:20:00+00:00"),
        _cand("f-fin", rs.STATE_FINISHED, hs=1, as_=0),
    ]
    selected = rs.select_result_sync_fixtures(candidates, max_fixtures=2)
    assert [c.fixture_id for c in selected] == ["f-live", "f-ht"]  # live/ht priority; finished excluded
    assert len(selected) == 2


def test_selection_requires_pick_and_identity():
    candidates = [
        _cand("f1", rs.STATE_LIVE, pick=False),
        _cand("f2", rs.STATE_LIVE, provider_fixture_id=""),
        _cand("f3", rs.STATE_LIVE),
    ]
    assert [c.fixture_id for c in rs.select_result_sync_fixtures(candidates, max_fixtures=5)] == ["f3"]


def test_selection_rotation_by_attempt_recency():
    candidates = [
        _cand("a", rs.STATE_PRE_MATCH, attempt="2026-10-08T09:00:00+00:00"),
        _cand("b", rs.STATE_PRE_MATCH, attempt=None),
        _cand("c", rs.STATE_PRE_MATCH, attempt="2026-10-08T08:00:00+00:00"),
    ]
    selected = rs.select_result_sync_fixtures(candidates, max_fixtures=2)
    assert [x.fixture_id for x in selected] == ["b", "c"]  # never-attempted first, then oldest attempt


def test_lookback_window_and_postponed_extension():
    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    assert rs.within_result_lookback(kickoff_utc="2026-10-08T10:00:00+00:00", status=rs.STATE_LIVE, now_utc=now)
    assert not rs.within_result_lookback(kickoff_utc="2026-10-01T10:00:00+00:00", status=rs.STATE_LIVE, now_utc=now)
    assert rs.within_result_lookback(kickoff_utc="2026-10-01T10:00:00+00:00", status=rs.STATE_POSTPONED, now_utc=now)
    assert not rs.within_result_lookback(kickoff_utc="2026-10-08T14:00:00+00:00", status=rs.STATE_LIVE, now_utc=now)


def test_interval_and_max_fixture_resolvers():
    assert rs.resolve_result_sync_interval_seconds(None) == 600
    assert rs.resolve_result_sync_interval_seconds(0) == 600
    assert rs.resolve_result_sync_interval_seconds(120) == rs.MIN_RESULT_SYNC_INTERVAL_SECONDS
    assert rs.resolve_result_sync_max_fixtures(None) == 2
    assert rs.resolve_result_sync_max_fixtures(100) == rs.HARD_MAX_RESULT_SYNC_MAX_FIXTURES


# --------------------------------------------------------------------------- services
class FakeFixtureProvider:
    def __init__(self, fixtures_by_id, *, name="api-football"):
        self._fixtures = fixtures_by_id
        self._name = name
        self.calls = 0
        self.called_ids: list[str] = []

    def provider_name(self) -> str:
        return self._name

    def configured(self) -> bool:
        return True

    def get_fixture(self, fixture_id):
        self.calls += 1
        self.called_ids.append(fixture_id)
        return self._fixtures.get(fixture_id)


class FakeStatsProvider:
    def __init__(self, corners_by_id):
        self._corners = corners_by_id
        self.calls = 0

    def provider_name(self) -> str:
        return "api-football"

    def configured(self) -> bool:
        return True

    def get_authoritative_corner_statistics(self, fixture_context):
        self.calls += 1
        fixture_id = str(fixture_context.get("fixture_id") or "")
        from sports_ai.dashboard.fixture_provider import AuthoritativeFixtureStatistics

        values = self._corners.get(fixture_id)
        if values is None:
            return AuthoritativeFixtureStatistics(
                fixture_id=fixture_id, provider="api-football",
                provider_fixture_id=str(fixture_context.get("provider_fixture_id") or ""),
                home_corners=None, away_corners=None, total_corners=None,
                source_timestamp="2026-10-08T11:00:00+00:00", captured_at="2026-10-08T11:01:00+00:00",
            )
        home, away = values
        return AuthoritativeFixtureStatistics(
            fixture_id=fixture_id, provider="api-football",
            provider_fixture_id=str(fixture_context.get("provider_fixture_id") or ""),
            home_corners=home, away_corners=away, total_corners=home + away,
            source_timestamp="2026-10-08T11:00:00+00:00", captured_at="2026-10-08T11:01:00+00:00",
        )


def _normalized(fixture_id, *, status, home_score, away_score, kickoff, provider_payload=None):
    provider, _, pfid = fixture_id.partition(":")
    return NormalizedFixture(
        fixture_id=fixture_id, provider_fixture_id=pfid, provider=provider or "api-football",
        competition_id="ENG1", competition_name="Premier League", division_code="E0", season="2026",
        home_team_id="1", home_team_name="Home", away_team_id="2", away_team_name="Away",
        kickoff_utc=kickoff, status=status, elapsed_minute=None,
        home_score=home_score, away_score=away_score, last_provider_update="2026-10-08T11:00:00+00:00",
        source_version="api_football_v1", provider_payload=provider_payload or {},
    )


def _config(tmp_path):
    return replace(
        DashboardConfig.default(),
        dashboard_db_path=tmp_path / "football_dashboard.sqlite",
        paper_trading_db_path=tmp_path / "football_paper_trading.sqlite",
        model_pick_db_path=tmp_path / "football_model_picks.sqlite",
        operational_data_dir=tmp_path,
        storage_mode="test_tmpdir",
        persistent_data_dir_configured=False,
        enable_scheduler=False,
        enable_market_collection_scheduler=False,
        enable_result_sync_scheduler=True,
        result_sync_interval_seconds=600,
        result_sync_max_fixtures_per_cycle=2,
        runtime_role=RUNTIME_ROLE_COMBINED,
    )


def _seed_pick(path, fixture_id, market="asian_handicap"):
    conn = ledger.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS football_model_pick_entries (fixture_id TEXT, market TEXT, record_origin TEXT, kickoff_utc TEXT, selection TEXT)")
    conn.execute("INSERT INTO football_model_pick_entries (fixture_id, market, record_origin, kickoff_utc, selection) VALUES (?,?,?,?,?)", (fixture_id, market, "LIVE_ISSUED", "", "HOME"))
    conn.commit()
    conn.close()


def _seed_fixture(config, fixture):
    with storage.connect(config.dashboard_db_path) as conn:
        storage.initialize_database(conn)
        storage.upsert_fixtures(conn, [fixture], updated_at="2026-10-08T10:00:00+00:00", initialize_schema=False)


def _rows_for(fixtures):
    return [
        {
            "fixture_id": f.fixture_id, "provider": f.provider, "provider_fixture_id": f.provider_fixture_id,
            "status": f.status, "home_score": f.home_score, "away_score": f.away_score,
            "kickoff_utc": f.kickoff_utc, "provider_payload": f.provider_payload,
            "competition_id": f.competition_id, "home_team_name": f.home_team_name, "away_team_name": f.away_team_name,
        }
        for f in fixtures
    ]


def _past_kickoff(hours=1):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def test_result_sync_active_requires_flag_and_provider(tmp_path):
    config = _config(tmp_path)
    services = DashboardServices(config, fixture_provider=FakeFixtureProvider({}), market_provider=NotConfiguredMarketProvider(), statistics_provider=FakeStatsProvider({}))
    assert services.result_sync_scheduler_active() is True
    services.config = replace(config, enable_result_sync_scheduler=False)
    assert services.result_sync_scheduler_active() is False


def test_result_sync_persists_status_score_and_corners(tmp_path, monkeypatch):
    config = _config(tmp_path)
    fid = "api-football:111"
    kickoff = _past_kickoff()
    existing = _normalized(fid, status="LIVE", home_score=1, away_score=0, kickoff=kickoff)
    fetched = _normalized(fid, status="FINISHED", home_score=2, away_score=0, kickoff=kickoff)
    _seed_fixture(config, existing)
    _seed_pick(config.model_pick_db_path, fid, market="corner_total")
    fixture_provider = FakeFixtureProvider({fid: fetched})
    stats_provider = FakeStatsProvider({fid: (7, 3)})
    services = DashboardServices(config, fixture_provider=fixture_provider, market_provider=NotConfiguredMarketProvider(), statistics_provider=stats_provider)
    monkeypatch.setattr(services, "_all_fixtures_with_predictions", lambda: _rows_for([existing]))

    def _forbidden(*a, **k):
        raise AssertionError("forbidden path")

    for method in ("_maybe_create_paper_entries", "_sync_current_portfolio_selections", "_process_paper_trading_updates"):
        monkeypatch.setattr(services, method, _forbidden)
    monkeypatch.setattr(services, "sync_once", _forbidden)
    monkeypatch.setattr(services_mod, "build_fixture_market_evaluation", _forbidden)

    result = services.run_current_result_sync_cycle()

    assert result["results_updated"] == 1
    assert result["corners_updated"] == 1
    assert fixture_provider.calls == 1
    with storage.connect(config.dashboard_db_path) as conn:
        row = storage.fetch_fixture(conn, fid, initialize_schema=False)
        assert row["status"] == "FINISHED"
        assert row["home_score"] == 2 and row["away_score"] == 0
        corner = storage.fetch_fixture_result(conn, fid, initialize_schema=False)
        assert corner["total_corners"] == 10
    # Result sync never opens the paper ledger at all.
    assert not Path(config.paper_trading_db_path).exists()
    assert services._result_sync_runtime_state["result_sync_cycles_completed"] == 1
    assert services._result_sync_runtime_state["result_sync_last_error"] is None


def test_result_sync_skips_finished_verified(tmp_path, monkeypatch):
    config = _config(tmp_path)
    fid = "api-football:333"
    kickoff = _past_kickoff(1)
    finished = _normalized(fid, status="FINISHED", home_score=1, away_score=0, kickoff=kickoff)
    _seed_fixture(config, finished)
    _seed_pick(config.model_pick_db_path, fid)
    provider = FakeFixtureProvider({fid: finished})
    services = DashboardServices(config, fixture_provider=provider, market_provider=NotConfiguredMarketProvider(), statistics_provider=FakeStatsProvider({}))
    monkeypatch.setattr(services, "_all_fixtures_with_predictions", lambda: _rows_for([finished]))
    result = services.run_current_result_sync_cycle()
    assert result["selected"] == []
    assert provider.calls == 0


# ------------------------------------------------ corner-truth safety (non-final)
@pytest.mark.parametrize(
    "status",
    ["PRE_MATCH", "LIVE", "HALFTIME", "POSTPONED", "CANCELLED", "ABANDONED", "MADE_UP"],
)
def test_non_final_fixtures_never_request_or_persist_corners(tmp_path, monkeypatch, status):
    config = _config(tmp_path)
    fid = f"api-football:{status}"
    kickoff = _past_kickoff(1)
    fixture = _normalized(fid, status=status, home_score=(1 if status in {"LIVE", "HALFTIME"} else None), away_score=(0 if status in {"LIVE", "HALFTIME"} else None), kickoff=kickoff)
    _seed_fixture(config, fixture)
    _seed_pick(config.model_pick_db_path, fid, market="corner_total")
    stats = FakeStatsProvider({fid: (4, 3)})
    provider = FakeFixtureProvider({})  # status sync may or may not run; corners must not
    services = DashboardServices(config, fixture_provider=provider, market_provider=NotConfiguredMarketProvider(), statistics_provider=stats)
    monkeypatch.setattr(services, "_all_fixtures_with_predictions", lambda: _rows_for([fixture]))
    services.run_current_result_sync_cycle()
    assert stats.calls == 0  # never request /fixtures/statistics for a non-final fixture
    with storage.connect(config.dashboard_db_path) as conn:
        assert storage.fetch_fixture_result(conn, fid, initialize_schema=False) is None


def test_finished_corner_only_uses_statistics_not_status(tmp_path, monkeypatch):
    config = _config(tmp_path)
    fid = "api-football:555"
    kickoff = _past_kickoff(2)
    finished = _normalized(fid, status="FINISHED", home_score=2, away_score=1, kickoff=kickoff)
    _seed_fixture(config, finished)
    _seed_pick(config.model_pick_db_path, fid, market="corner_total")
    provider = FakeFixtureProvider({fid: finished})
    stats = FakeStatsProvider({fid: (8, 5)})
    services = DashboardServices(config, fixture_provider=provider, market_provider=NotConfiguredMarketProvider(), statistics_provider=stats)
    monkeypatch.setattr(services, "_all_fixtures_with_predictions", lambda: _rows_for([finished]))
    result = services.run_current_result_sync_cycle()
    assert result["corner_selected"] == [fid]
    assert result["corners_updated"] == 1
    assert provider.calls == 0  # already verified final: no /fixtures?id= re-fetch
    assert stats.calls == 1
    with storage.connect(config.dashboard_db_path) as conn:
        corner = storage.fetch_fixture_result(conn, fid, initialize_schema=False)
        assert corner["total_corners"] == 13


def test_finished_with_corners_present_makes_no_statistics_request(tmp_path, monkeypatch):
    config = _config(tmp_path)
    fid = "api-football:556"
    kickoff = _past_kickoff(2)
    finished = _normalized(fid, status="FINISHED", home_score=1, away_score=1, kickoff=kickoff)
    _seed_fixture(config, finished)
    _seed_pick(config.model_pick_db_path, fid, market="corner_total")
    with storage.connect(config.dashboard_db_path) as conn:
        storage.upsert_fixture_result(conn, fixture_id=fid, statistics_provider="api-football", statistics_provider_fixture_id="556", home_corners=6, away_corners=6, total_corners=12, source_timestamp=None, captured_at="2026-10-08T10:00:00+00:00", initialize_schema=False)
    provider = FakeFixtureProvider({fid: finished})
    stats = FakeStatsProvider({fid: (9, 9)})
    services = DashboardServices(config, fixture_provider=provider, market_provider=NotConfiguredMarketProvider(), statistics_provider=stats)
    monkeypatch.setattr(services, "_all_fixtures_with_predictions", lambda: _rows_for([finished]))
    services.run_current_result_sync_cycle()
    assert provider.calls == 0
    assert stats.calls == 0  # terminal stop: nothing left to do


def test_ah_only_finished_does_not_request_corners(tmp_path, monkeypatch):
    config = _config(tmp_path)
    fid = "api-football:557"
    kickoff = _past_kickoff(2)
    finished = _normalized(fid, status="FINISHED", home_score=3, away_score=0, kickoff=kickoff)
    _seed_fixture(config, finished)
    _seed_pick(config.model_pick_db_path, fid, market="asian_handicap")
    provider = FakeFixtureProvider({fid: finished})
    stats = FakeStatsProvider({fid: (5, 5)})
    services = DashboardServices(config, fixture_provider=provider, market_provider=NotConfiguredMarketProvider(), statistics_provider=stats)
    monkeypatch.setattr(services, "_all_fixtures_with_predictions", lambda: _rows_for([finished]))
    services.run_current_result_sync_cycle()
    assert stats.calls == 0


def test_live_to_finished_persists_corners_same_cycle(tmp_path, monkeypatch):
    config = _config(tmp_path)
    fid = "api-football:558"
    kickoff = _past_kickoff(1)
    live = _normalized(fid, status="HALFTIME", home_score=1, away_score=0, kickoff=kickoff)
    finished = _normalized(fid, status="FINISHED", home_score=3, away_score=1, kickoff=kickoff)
    _seed_fixture(config, live)
    _seed_pick(config.model_pick_db_path, fid, market="corner_total")
    stats = FakeStatsProvider({fid: (9, 4)})
    services = DashboardServices(config, fixture_provider=FakeFixtureProvider({fid: finished}), market_provider=NotConfiguredMarketProvider(), statistics_provider=stats)
    monkeypatch.setattr(services, "_all_fixtures_with_predictions", lambda: _rows_for([live]))
    result = services.run_current_result_sync_cycle()
    assert result["results_updated"] == 1
    assert result["corners_updated"] == 1
    assert stats.calls == 1
    with storage.connect(config.dashboard_db_path) as conn:
        row = storage.fetch_fixture(conn, fid, initialize_schema=False)
        assert row["status"] == "FINISHED"
        assert storage.fetch_fixture_result(conn, fid, initialize_schema=False)["total_corners"] == 13


def test_plan_result_sync_slots_fairness():
    status_c = [_cand("s1", rs.STATE_LIVE), _cand("s2", rs.STATE_LIVE)]
    corner_c = [_cand_corner("c1"), _cand_corner("c2")]
    status_sel, corner_sel = rs.plan_result_sync_slots(status_candidates=status_c, corner_candidates=corner_c, max_fixtures=2)
    assert [c.fixture_id for c in status_sel] == ["s1"]
    assert [c.fixture_id for c in corner_sel] == ["c1"]
    # only one queue -> use both slots
    status_only, corner_none = rs.plan_result_sync_slots(status_candidates=status_c, corner_candidates=[], max_fixtures=2)
    assert [c.fixture_id for c in status_only] == ["s1", "s2"]
    assert corner_none == []


def _cand_corner(fid, *, attempt=None, kickoff=None):
    return rs.CornerFixtureCandidate(
        fixture_id=fid,
        provider_fixture_id=fid,
        kickoff_utc=kickoff or "2026-10-08T10:00:00+00:00",
        status=rs.STATE_FINISHED,
        home_score=1,
        away_score=0,
        has_corner_pick=True,
        corner_result_present=False,
        last_corner_sync_attempt_at=attempt,
    )


def test_corner_selection_requires_final_and_rotation():
    now = "2026-10-08T10:00:00+00:00"
    eligible = [
        _cand_corner("a", attempt="2026-10-08T09:00:00+00:00"),
        _cand_corner("b", attempt=None),
        rs.CornerFixtureCandidate("c", "c", now, rs.STATE_LIVE, 1, 0, True, False, None),
        rs.CornerFixtureCandidate("d", "d", now, rs.STATE_FINISHED, 1, 0, True, True, None),
    ]
    selected = rs.select_corner_sync_fixtures(eligible, max_fixtures=5)
    assert [c.fixture_id for c in selected] == ["b", "a"]  # never-attempted first, oldest attempt, non-final/present excluded


def test_both_queues_served_in_one_cycle(tmp_path, monkeypatch):
    config = _config(tmp_path)
    live_id = "api-football:601"
    corner_id = "api-football:602"
    kickoff = _past_kickoff(1)
    live = _normalized(live_id, status="LIVE", home_score=1, away_score=0, kickoff=kickoff)
    finished = _normalized(corner_id, status="FINISHED", home_score=2, away_score=2, kickoff=_past_kickoff(2))
    _seed_fixture(config, live)
    _seed_fixture(config, finished)
    _seed_pick(config.model_pick_db_path, live_id, market="asian_handicap")
    _seed_pick(config.model_pick_db_path, corner_id, market="corner_total")
    provider = FakeFixtureProvider({live_id: _normalized(live_id, status="FINISHED", home_score=2, away_score=0, kickoff=kickoff)})
    stats = FakeStatsProvider({corner_id: (7, 2)})
    services = DashboardServices(config, fixture_provider=provider, market_provider=NotConfiguredMarketProvider(), statistics_provider=stats)
    monkeypatch.setattr(services, "_all_fixtures_with_predictions", lambda: _rows_for([live, finished]))
    result = services.run_current_result_sync_cycle()
    assert live_id in result["selected"]
    assert corner_id in result["corner_selected"]
    assert provider.calls == 1 and stats.calls == 1


# --------------------------------------------------------------------------- loop
def _async_run(coro):
    return asyncio.run(coro)


def test_result_runner_disabled_is_noop():
    calls = {"n": 0}

    class FakeServices:
        config = SimpleNamespace(result_sync_interval_seconds=600)

        def result_sync_scheduler_active(self):
            return False

        def run_current_result_sync_cycle(self):
            calls["n"] += 1
            return {}

    _async_run(asyncio.wait_for(_run_result_sync_runtime(FakeServices(), asyncio.Event()), timeout=2))
    assert calls["n"] == 0


def test_result_runner_sequential_and_cancels_cleanly():
    seq = {"n": 0, "max": 0, "cur": 0}

    class FakeServices:
        config = SimpleNamespace(result_sync_interval_seconds=1)

        def result_sync_scheduler_active(self):
            return True

        def run_current_result_sync_cycle(self):
            seq["cur"] += 1
            seq["max"] = max(seq["max"], seq["cur"])
            time.sleep(0.03)
            seq["cur"] -= 1
            seq["n"] += 1
            return {}

    async def _main():
        stop = asyncio.Event()
        task = asyncio.create_task(_run_result_sync_runtime(FakeServices(), stop))
        await asyncio.sleep(0.15)
        stop.set()
        await asyncio.wait_for(task, timeout=3)
        assert seq["max"] == 1
        assert seq["n"] >= 1
        # clean cancellation
        task2 = asyncio.create_task(_run_result_sync_runtime(FakeServices(), asyncio.Event()))
        await asyncio.sleep(0.05)
        task2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task2

    _async_run(_main())


def test_result_sync_source_has_no_historical_or_market_coupling():
    import inspect

    from sports_ai.dashboard import scheduler_worker

    source = inspect.getsource(rs) + inspect.getsource(scheduler_worker)
    assert "?date=" not in source
    assert "historical" not in source.lower() or "never" in source.lower()
    # result sync must not reference the market collector method
    assert "run_market_collection_only_cycle" not in inspect.getsource(rs)
