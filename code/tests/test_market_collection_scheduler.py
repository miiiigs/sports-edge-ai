"""Focused tests for the dedicated recurring market-collection scheduler.

No live provider calls are made. These verify the dedicated scheduler is disabled
by default, is independent of the global automated scheduler, only invokes the
collection-only market path, rotates by observation recency (not provider source
timestamp), and never touches fixture sync / predictions / evaluation / paper /
portfolio / settlement / historical paths.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import sports_ai.dashboard.services as services_mod
from sports_ai.dashboard import market_collection_policy as policy
from sports_ai.dashboard.scheduler_worker import _run_market_collection_runtime
from sports_ai.dashboard.services import (
    RUNTIME_ROLE_COMBINED,
    DashboardConfig,
    DashboardServices,
)
from sports_ai.market_data import market_evidence as me
from sports_ai.market_data.providers import NormalizedMarketSnapshot
from sports_ai.storage import football_model_pick_ledger as ledger
from sports_ai.storage import football_paper_trading as pt

FIXED_STALE_SOURCE = "2026-10-07T02:15:46+00:00"


# --------------------------------------------------------------------------- interval
def test_interval_defaults_and_fails_safe():
    assert policy.resolve_market_collection_interval_seconds(None) == 600
    assert policy.resolve_market_collection_interval_seconds(0) == 600
    assert policy.resolve_market_collection_interval_seconds(-30) == 600
    assert policy.resolve_market_collection_interval_seconds("abc") == 600
    assert policy.resolve_market_collection_interval_seconds(120) == policy.MIN_MARKET_COLLECTION_INTERVAL_SECONDS
    assert policy.resolve_market_collection_interval_seconds(600) == 600
    assert policy.resolve_market_collection_interval_seconds(10_000_000) == policy.HARD_MAX_MARKET_COLLECTION_INTERVAL_SECONDS


def test_dedicated_flag_and_interval_default_off(monkeypatch, tmp_path):
    monkeypatch.setenv("SPORTS_AI_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SPORTS_AI_ENABLE_MARKET_COLLECTION_SCHEDULER", raising=False)
    monkeypatch.delenv("SPORTS_AI_MARKET_COLLECTION_INTERVAL_SECONDS", raising=False)
    config = DashboardConfig.default()
    assert config.enable_market_collection_scheduler is False
    assert config.market_collection_interval_seconds == 600


# --------------------------------------------------------------------------- fixtures
class FakeFixtureProvider:
    """Raises if any fixture research/sync call is made."""

    def provider_name(self) -> str:
        return "fake-fixture-provider"

    def configured(self) -> bool:
        return True

    def _forbidden(self, *args, **kwargs):
        raise AssertionError("fixture provider must not be called by the market collector")

    get_live_fixtures = _forbidden
    get_fixtures_for_date = _forbidden
    get_upcoming_fixtures = _forbidden
    get_fixture = _forbidden


class FakeMarketProvider:
    def __init__(self, snapshots_by_fixture, *, provider_name: str = "api-football", configured: bool = True):
        self._snapshots = snapshots_by_fixture
        self._name = provider_name
        self._configured = configured
        self.calls = 0
        self.called_fixture_ids: list[str] = []

    def provider_name(self) -> str:
        return self._name

    def configured(self) -> bool:
        return self._configured

    def get_fixture_markets(self, fixture_id, *, fixture_context=None):
        return self.get_market_snapshot(fixture_id, fixture_context=fixture_context)

    def get_best_prices(self, fixture_id, *, fixture_context=None):
        return self.get_market_snapshot(fixture_id, fixture_context=fixture_context)

    def get_market_snapshot(self, fixture_id, *, fixture_context=None):
        self.calls += 1
        self.called_fixture_ids.append(fixture_id)
        return list(self._snapshots.get(fixture_id, []))

    def diagnostics(self) -> dict:
        return {
            "provider": self.provider_name(),
            "configured": self._configured,
            "state": "CONNECTED",
            "current_market_decisions": "READY",
            "queries": [{"status": "OK"}],
        }


def _config(tmp_path: Path, *, scheduler_enabled: bool, interval: int = 600, max_fixtures: int = 2) -> DashboardConfig:
    return replace(
        DashboardConfig.default(),
        dashboard_db_path=tmp_path / "football_dashboard.sqlite",
        paper_trading_db_path=tmp_path / "football_paper_trading.sqlite",
        model_pick_db_path=tmp_path / "football_model_picks.sqlite",
        operational_data_dir=tmp_path,
        storage_mode="test_tmpdir",
        persistent_data_dir_configured=False,
        enable_scheduler=False,  # global automated scheduler OFF
        current_odds_connected=True,
        enable_odds_sync=True,
        allow_paid_provider_calls=True,
        market_collection_only=True,
        market_collector_max_fixtures_per_cycle=max_fixtures,
        enable_market_collection_scheduler=scheduler_enabled,
        market_collection_interval_seconds=interval,
        runtime_role=RUNTIME_ROLE_COMBINED,
    )


def _kickoff(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()


def _snapshot(fixture_id: str, market: str, selection: str, line, odds: float, kickoff: str, source_timestamp: str = FIXED_STALE_SOURCE) -> NormalizedMarketSnapshot:
    return NormalizedMarketSnapshot(
        provider="api-football",
        fixture_id=fixture_id,
        provider_fixture_id=fixture_id.split(":")[-1],
        captured_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        source_timestamp=source_timestamp,
        market=market,
        selection=selection,
        line=line,
        decimal_odds=odds,
        bookmaker="Pinnacle",
        ingestion_version="api_football_current_market_provider_v1",
        provider_market_id=f"{fixture_id}:1:1:{selection}:{line}",
        is_pre_match=True,
        home_team_name="Home",
        away_team_name="Away",
        competition_id="ENG1",
        competition_name="Premier League",
        kickoff_utc=kickoff,
        freshness_state="STALE",
    )


def _snapshots_for(fixture_id: str, kickoff: str) -> list[NormalizedMarketSnapshot]:
    return [
        _snapshot(fixture_id, "asian_handicap", "HOME", -0.5, 1.9, kickoff),
        _snapshot(fixture_id, "asian_handicap", "AWAY", -0.5, 1.95, kickoff),
        _snapshot(fixture_id, "corner_total", "OVER", 9.5, 1.85, kickoff),
        _snapshot(fixture_id, "corner_total", "UNDER", 9.5, 1.95, kickoff),
    ]


def _row(fixture_id: str, kickoff: str) -> dict:
    return {
        "fixture_id": fixture_id,
        "provider": "api-football",
        "provider_fixture_id": fixture_id.split(":")[-1],
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "division_code": "E0",
        "season": "2026",
        "home_team_name": "Home",
        "away_team_name": "Away",
        "kickoff_utc": kickoff,
        "status": "PRE_MATCH",
        "prediction": {"prediction_status": "READY"},
    }


def _seed_live_issued_picks(path: Path, fixture_ids: list[str]) -> None:
    conn = ledger.connect(path)
    conn.execute(
        """
        CREATE TABLE football_model_pick_entries (
            fixture_id TEXT, market TEXT, record_origin TEXT, kickoff_utc TEXT, selection TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO football_model_pick_entries (fixture_id, market, record_origin, kickoff_utc, selection) VALUES (?, ?, ?, ?, ?)",
        [(fid, "asian_handicap", "LIVE_ISSUED", "", "HOME") for fid in fixture_ids],
    )
    conn.commit()
    conn.close()


def _services(config: DashboardConfig, market_provider: FakeMarketProvider) -> DashboardServices:
    return DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider(),
        market_provider=market_provider,
    )


# --------------------------------------------------------------------------- activation
def test_active_requires_flag_and_api_football(tmp_path):
    kickoff = _kickoff(1)
    fixture_id = "api-football:1"
    for enabled, provider_name, expected in (
        (False, "api-football", False),
        (True, "api-football", True),
        (True, "the-odds-api", False),
    ):
        config = _config(tmp_path, scheduler_enabled=enabled)
        market = FakeMarketProvider({fixture_id: _snapshots_for(fixture_id, kickoff)}, provider_name=provider_name)
        services = _services(config, market)
        assert services.market_collection_scheduler_active() is expected


def test_global_scheduler_stays_disabled_with_only_dedicated_enabled(tmp_path):
    config = _config(tmp_path, scheduler_enabled=True)
    market = FakeMarketProvider({})
    services = _services(config, market)
    assert services.config.enable_scheduler is False  # global automated sync untouched
    assert services.market_collection_scheduler_active() is True


# --------------------------------------------------------------------------- cycle
def _forbidden_value_methods(monkeypatch, services):
    def _forbidden(*args, **kwargs):
        raise AssertionError("value pipeline must not run in dedicated collection")

    monkeypatch.setattr(services_mod, "build_fixture_market_evaluation", _forbidden)
    for method in (
        "_maybe_create_paper_entries",
        "_sync_current_portfolio_selections",
        "_sync_authoritative_corner_results",
        "_process_paper_trading_updates",
        "ensure_prediction",
        "sync_once",
        "_sync_current_market_state",
        "_refresh_fixtures",
    ):
        if hasattr(services, method):
            monkeypatch.setattr(services, method, _forbidden)


def test_dedicated_cycle_persists_snapshots_and_skips_value_pipeline(tmp_path, monkeypatch):
    config = _config(tmp_path, scheduler_enabled=True)
    kickoffs = {fid: _kickoff(day) for day, fid in enumerate(("api-football:1", "api-football:2", "api-football:3"), start=1)}
    _seed_live_issued_picks(config.model_pick_db_path, list(kickoffs))
    market = FakeMarketProvider({fid: _snapshots_for(fid, ko) for fid, ko in kickoffs.items()})
    services = _services(config, market)
    _forbidden_value_methods(monkeypatch, services)
    rows = [_row(fid, ko) for fid, ko in kickoffs.items()]
    monkeypatch.setattr(services, "_all_fixtures_with_predictions", lambda: rows)

    summary = services.run_market_collection_only_cycle()

    assert market.calls == 2  # bounded
    assert summary["snapshot_count"] > 0
    with pt.connect(config.paper_trading_db_path) as conn:
        assert pt.fetch_market_snapshots(conn, initialize_schema=False)
        assert conn.execute("select count(*) from paper_trade_entries").fetchone()[0] == 0
        assert conn.execute("select count(*) from paper_trade_updates").fetchone()[0] == 0
    state = services._market_runtime_state
    assert state["market_collection_scheduler_enabled"] is True
    assert state["market_collection_cycles_completed"] == 1
    assert state["market_collection_last_error"] is None
    assert len(state["market_collection_selected_last_cycle"]) == 2
    assert state["market_collection_snapshots_last_cycle"] > 0


def test_dedicated_cycle_requires_api_football(tmp_path):
    config = _config(tmp_path, scheduler_enabled=True)
    market = FakeMarketProvider({}, provider_name="the-odds-api")
    services = _services(config, market)
    result = services.run_market_collection_only_cycle()
    assert result["state"] == "PROVIDER_NOT_API_FOOTBALL"
    assert market.calls == 0


def test_unchanged_stale_source_timestamp_still_rotates_fixtures(tmp_path):
    # Provider source timestamp is FIXED/stale for every fixture; rotation must
    # still happen via last_observed_at.
    config = _config(tmp_path, scheduler_enabled=True, max_fixtures=2)
    fixture_ids = [f"api-football:{n}" for n in range(1, 5)]
    kickoffs = {fid: _kickoff(n) for n, fid in enumerate(fixture_ids, start=1)}
    _seed_live_issued_picks(config.model_pick_db_path, fixture_ids)
    market = FakeMarketProvider({fid: _snapshots_for(fid, ko) for fid, ko in kickoffs.items()})
    services = _services(config, market)
    rows = [_row(fid, ko) for fid, ko in kickoffs.items()]

    services._run_collection_only_market_cycle(rows, dashboard_conn=None)
    first = list(market.called_fixture_ids)
    services._run_collection_only_market_cycle(rows, dashboard_conn=None)
    second = market.called_fixture_ids[len(first):]
    assert len(first) == 2 and len(second) == 2
    assert set(first) != set(second)
    assert set(first) | set(second) == set(fixture_ids)


def test_cache_hit_advances_recency_without_becoming_fresh():
    conn = pt.connect(":memory:")
    pt.ensure_schema(conn)
    snap = pt.StoredMarketSnapshot(
        fixture_id="api-football:1",
        provider="api-football",
        bookmaker="Pinnacle",
        market="asian_handicap",
        selection="HOME",
        odds=1.9,
        ingestion_version="api_football_current_market_provider_v1",
        line=-0.5,
        provider_timestamp=FIXED_STALE_SOURCE,
        captured_at="2026-10-07T09:00:00+00:00",
        kickoff_utc=_kickoff(2),
    )
    pt.record_market_snapshot(conn, snap)
    # Cache hit: same provider_timestamp, later capture (re-observation).
    pt.record_market_snapshot(conn, replace(snap, captured_at="2026-10-07T09:10:00+00:00"))
    row = pt.fetch_market_snapshots(conn, fixture_id="api-football:1")[0]
    assert row["provider_timestamp"] == FIXED_STALE_SOURCE  # immutable
    assert row["last_observed_at"] == "2026-10-07T09:10:00+00:00"  # recency advanced
    assert row["captured_at"] == "2026-10-07T09:00:00+00:00"  # immutable
    state = me.classify_quote_state(
        captured_at=row["captured_at"],
        source_timestamp=row["provider_timestamp"],
        kickoff_utc=row["kickoff_utc"],
        now_utc="2026-10-07T09:20:00+00:00",
        actionable_max_age_minutes=240,
        collector_health_max_age_minutes=360,
    )
    assert state.state == me.QUOTE_STATE_STALE_PREMATCH  # cache hit never becomes fresh


def test_latest_observation_uses_last_observed_at_fallback():
    rows = [
        {"fixture_id": "f1", "market": "asian_handicap", "last_observed_at": "2026-10-07T09:00:00+00:00", "captured_at": "2026-10-07T08:00:00+00:00"},
        {"fixture_id": "f2", "market": "corner_total", "last_observed_at": None, "captured_at": "2026-10-07T07:00:00+00:00"},
        {"fixture_id": "f3", "market": "1x2", "last_observed_at": "2026-10-07T10:00:00+00:00", "captured_at": "2026-10-07T10:00:00+00:00"},
    ]
    latest = policy.latest_observation_by_fixture(rows)
    assert latest == {"f1": "2026-10-07T09:00:00+00:00", "f2": "2026-10-07T07:00:00+00:00"}


# --------------------------------------------------------------------------- loop
def _async_run(coro):
    return asyncio.run(coro)


def test_runner_loop_disabled_is_noop():
    calls = {"n": 0}

    class FakeServices:
        config = SimpleNamespace(market_collection_interval_seconds=600)

        def market_collection_scheduler_active(self):
            return False

        def run_market_collection_only_cycle(self):
            calls["n"] += 1
            return {}

    async def _main():
        await asyncio.wait_for(_run_market_collection_runtime(FakeServices(), asyncio.Event()), timeout=2)

    _async_run(_main())
    assert calls["n"] == 0


def test_runner_loop_sequential_and_uses_collection_method():
    lock = threading.Lock()
    calls = {"n": 0, "max_concurrent": 0, "concurrent": 0}

    class FakeServices:
        config = SimpleNamespace(market_collection_interval_seconds=1)

        def market_collection_scheduler_active(self):
            return True

        def run_market_collection_only_cycle(self):
            with lock:
                calls["concurrent"] += 1
                calls["max_concurrent"] = max(calls["max_concurrent"], calls["concurrent"])
            time.sleep(0.05)
            with lock:
                calls["concurrent"] -= 1
                calls["n"] += 1
            return {}

    async def _main():
        stop = asyncio.Event()
        task = asyncio.create_task(_run_market_collection_runtime(FakeServices(), stop))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, timeout=3)

    _async_run(_main())
    assert calls["n"] >= 1
    assert calls["max_concurrent"] == 1  # never overlapping


def test_runner_loop_cancels_cleanly():
    class FakeServices:
        config = SimpleNamespace(market_collection_interval_seconds=600)

        def market_collection_scheduler_active(self):
            return True

        def run_market_collection_only_cycle(self):
            return {}

    async def _main():
        task = asyncio.create_task(_run_market_collection_runtime(FakeServices(), asyncio.Event()))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _async_run(_main())


def test_no_historical_or_fixture_endpoints_in_scheduler_sources():
    import inspect

    from sports_ai.dashboard import scheduler_worker

    for module in (inspect.getmodule(_run_market_collection_runtime), scheduler_worker):
        source = inspect.getsource(module)
        assert "?date=" not in source
        assert "odds?date" not in source


# ----------------------------------------------- attempt recency / starvation
class _ClockedMarketProvider(FakeMarketProvider):
    """Returns snapshots captured 'now' (per a shared fake clock); [] for empty fixtures."""

    def __init__(self, kickoffs, *, empty_fixture_ids, clock):
        super().__init__({})
        self._kickoffs = kickoffs
        self._empty = set(empty_fixture_ids)
        self._clock = clock

    def get_market_snapshot(self, fixture_id, *, fixture_context=None):
        self.calls += 1
        self.called_fixture_ids.append(fixture_id)
        if fixture_id in self._empty:
            return []
        captured = self._clock()
        return [
            replace(snapshot, captured_at=captured)
            for snapshot in _snapshots_for(fixture_id, self._kickoffs[fixture_id])
        ]


def _fake_clock(monkeypatch):
    base = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
    ticks = {"n": 0}

    def now_iso() -> str:
        ticks["n"] += 1
        return (base + timedelta(seconds=ticks["n"])).isoformat(timespec="seconds")

    monkeypatch.setattr(services_mod, "utc_now_iso", now_iso)
    return now_iso


def test_fixture_without_odds_does_not_permanently_hold_a_slot(tmp_path, monkeypatch):
    # Regression: an attempt that writes no snapshot rows used to leave the
    # fixture "never attempted", so it was re-selected FIRST every cycle and
    # permanently consumed one of the bounded slots (fixtures=2 matched=1).
    clock = _fake_clock(monkeypatch)
    config = _config(tmp_path, scheduler_enabled=True, max_fixtures=2)
    fixture_ids = ["api-football:1", "api-football:2", "api-football:3"]
    kickoffs = {fid: _kickoff(n) for n, fid in enumerate(fixture_ids, start=1)}
    _seed_live_issued_picks(config.model_pick_db_path, fixture_ids)
    market = _ClockedMarketProvider(kickoffs, empty_fixture_ids={"api-football:1"}, clock=clock)
    services = _services(config, market)
    rows = [_row(fid, ko) for fid, ko in kickoffs.items()]

    for _ in range(3):
        services._run_collection_only_market_cycle(rows, dashboard_conn=None)

    assert market.calls == 6  # never more than the per-cycle cap
    assert market.called_fixture_ids.count("api-football:1") < 3
    assert set(market.called_fixture_ids) == set(fixture_ids)
    attempts = services._market_runtime_state["market_collection_attempts"]
    assert set(attempts) == set(fixture_ids)


def test_attempt_recency_is_persisted_and_restored(tmp_path, monkeypatch):
    clock = _fake_clock(monkeypatch)
    config = _config(tmp_path, scheduler_enabled=True, max_fixtures=1)
    kickoffs = {"api-football:1": _kickoff(1), "api-football:2": _kickoff(2)}
    _seed_live_issued_picks(config.model_pick_db_path, list(kickoffs))
    market = _ClockedMarketProvider(kickoffs, empty_fixture_ids={"api-football:1"}, clock=clock)
    services = _services(config, market)
    rows = [_row(fid, ko) for fid, ko in kickoffs.items()]
    monkeypatch.setattr(services, "_all_fixtures_with_predictions", lambda: rows)
    services.run_market_collection_only_cycle()
    assert market.called_fixture_ids == ["api-football:1"]

    # A fresh process (container wake) must restore attempt recency from the DB.
    market2 = _ClockedMarketProvider(kickoffs, empty_fixture_ids={"api-football:1"}, clock=clock)
    services2 = _services(config, market2)
    from sports_ai.dashboard import storage as dash_storage

    with dash_storage.connect(config.dashboard_db_path) as conn:
        services2._load_runtime_state(conn)  # what initialize() does on startup
    monkeypatch.setattr(services2, "_all_fixtures_with_predictions", lambda: rows)
    services2.run_market_collection_only_cycle()
    assert market2.called_fixture_ids == ["api-football:2"]


def test_merge_and_prune_attempt_recency():
    merged = policy.merge_attempt_recency(
        {"a": "2026-10-12T10:00:00+00:00", "b": None},
        {"a": "2026-10-12T09:00:00+00:00", "b": "2026-10-12T11:00:00+00:00", "c": "garbage", "d": 5},
    )
    assert merged == {"a": "2026-10-12T10:00:00+00:00", "b": "2026-10-12T11:00:00+00:00"}
    pruned = policy.prune_attempt_recency(
        {"a": "2026-10-12T10:00:00+00:00", "gone": "2026-10-12T10:00:00+00:00"}, keep_fixture_ids=["a"]
    )
    assert pruned == {"a": "2026-10-12T10:00:00+00:00"}


# ------------------------------------------------------ closing-window tier
def _cand(fid, kickoff, observed=None):
    return policy.CollectionCandidate(
        fixture_id=fid, provider_fixture_id=fid, kickoff_utc=kickoff,
        has_live_issued_pick=True, last_observed_at=observed,
    )


def test_closing_window_disabled_by_default_keeps_rotation_order():
    now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    near = _cand("near", "2026-10-12T12:30:00+00:00", observed="2026-10-12T11:30:00+00:00")
    far = _cand("far", "2026-10-15T12:00:00+00:00", observed="2026-10-12T11:00:00+00:00")
    assert policy.resolve_closing_window_minutes(None) == 0
    assert policy.resolve_closing_window_minutes(-5) == 0
    assert policy.resolve_closing_window_minutes(10_000) == policy.HARD_MAX_CLOSING_WINDOW_MINUTES
    selected = policy.select_collection_fixtures([near, far], max_fixtures=1, now_utc=now)
    assert [c.fixture_id for c in selected] == ["far"]  # oldest observation first


def test_closing_window_prioritizes_near_kickoff_within_same_cap():
    now = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    near = _cand("near", "2026-10-12T12:30:00+00:00", observed="2026-10-12T11:30:00+00:00")
    nearer_recent = _cand("recent", "2026-10-12T12:10:00+00:00", observed="2026-10-12T11:55:00+00:00")
    started = _cand("started", "2026-10-12T11:59:00+00:00")
    far = _cand("far", "2026-10-15T12:00:00+00:00")  # never attempted
    selected = policy.select_collection_fixtures(
        [far, started, nearer_recent, near], max_fixtures=1, now_utc=now, closing_window_minutes=60
    )
    # 'recent' was observed 5 min ago (< refresh floor) and 'started' is past kickoff.
    assert [c.fixture_id for c in selected] == ["near"]
    selected2 = policy.select_collection_fixtures(
        [far, nearer_recent, near], max_fixtures=2, now_utc=now, closing_window_minutes=60
    )
    assert [c.fixture_id for c in selected2] == ["near", "far"]  # cap unchanged
