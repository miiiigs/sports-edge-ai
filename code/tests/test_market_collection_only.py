"""Focused tests for collection-only market mode and deterministic selection.

No live provider calls are made. These verify that collection-only mode persists
current market snapshots while skipping every value-evaluation / paper-entry /
portfolio / settlement side effect, and that fixture selection is bounded,
deterministic and starvation-free.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sports_ai.dashboard.services as services_mod
from sports_ai.dashboard import market_collection_policy as policy
from sports_ai.dashboard.services import (
    RUNTIME_ROLE_COMBINED,
    DashboardConfig,
    DashboardServices,
)
from sports_ai.market_data.providers import NormalizedMarketSnapshot
from sports_ai.storage import football_model_pick_ledger as ledger
from sports_ai.storage import football_paper_trading as pt


# --------------------------------------------------------------------------- policy
def test_resolve_collection_only_max_fixtures_defaults_and_bounds():
    assert policy.resolve_collection_only_max_fixtures(None) == 2
    assert policy.resolve_collection_only_max_fixtures(0) == 2
    assert policy.resolve_collection_only_max_fixtures(-4) == 2
    assert policy.resolve_collection_only_max_fixtures(1) == 1
    assert policy.resolve_collection_only_max_fixtures(7) == 7
    assert policy.resolve_collection_only_max_fixtures(10_000) == policy.HARD_MAX_FIXTURES_PER_CYCLE


def _candidate(fid, kickoff, *, pick=True, evidence=None):
    return policy.CollectionCandidate(
        fixture_id=fid,
        provider_fixture_id=fid.split(":")[-1],
        kickoff_utc=kickoff,
        has_live_issued_pick=pick,
        latest_source_evidence_at=evidence,
    )


def test_select_collection_fixtures_prioritises_missing_then_oldest_then_kickoff():
    candidates = [
        _candidate("f3", "2026-10-10T12:00:00+00:00", evidence="2026-10-01T00:00:00+00:00"),
        _candidate("f1", "2026-10-09T12:00:00+00:00"),
        _candidate("f2", "2026-10-08T12:00:00+00:00", evidence="2026-09-01T00:00:00+00:00"),
        _candidate("f4", "2026-10-07T12:00:00+00:00"),
    ]
    selected = policy.select_collection_fixtures(candidates, max_fixtures=3)
    # Missing-evidence fixtures first (nearest kickoff), then oldest evidence.
    assert [c.fixture_id for c in selected] == ["f4", "f1", "f2"]


def test_select_collection_fixtures_filters_non_picks_and_missing_identity():
    candidates = [
        _candidate("f1", "2026-10-09T12:00:00+00:00", pick=False),
        policy.CollectionCandidate("f2", "", "2026-10-09T12:00:00+00:00", True, None),
        _candidate("f3", "2026-10-09T12:00:00+00:00", pick=True),
    ]
    selected = policy.select_collection_fixtures(candidates, max_fixtures=5)
    assert [c.fixture_id for c in selected] == ["f3"]


# --------------------------------------------------------------------------- fixtures
class FakeFixtureProvider:
    def provider_name(self) -> str:
        return "fake-fixture-provider"

    def configured(self) -> bool:
        return True


class FakeMarketProvider:
    def __init__(self, snapshots_by_fixture: dict[str, list[NormalizedMarketSnapshot]], *, configured: bool = True):
        self._snapshots = snapshots_by_fixture
        self._configured = configured
        self.calls = 0
        self.called_fixture_ids: list[str] = []

    def provider_name(self) -> str:
        return "api-football"

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


def _config(tmp_path: Path, *, collection_only: bool, max_fixtures: int) -> DashboardConfig:
    return replace(
        DashboardConfig.default(),
        dashboard_db_path=tmp_path / "football_dashboard.sqlite",
        paper_trading_db_path=tmp_path / "football_paper_trading.sqlite",
        model_pick_db_path=tmp_path / "football_model_picks.sqlite",
        operational_data_dir=tmp_path,
        storage_mode="test_tmpdir",
        persistent_data_dir_configured=False,
        enable_scheduler=False,
        current_odds_connected=True,
        enable_odds_sync=True,
        allow_paid_provider_calls=True,
        market_collection_only=collection_only,
        market_collector_max_fixtures_per_cycle=max_fixtures,
        runtime_role=RUNTIME_ROLE_COMBINED,
    )


def _kickoff(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()


def _snapshot(fixture_id: str, market: str, selection: str, line: float | None, odds: float, kickoff: str) -> NormalizedMarketSnapshot:
    return NormalizedMarketSnapshot(
        provider="api-football",
        fixture_id=fixture_id,
        provider_fixture_id=fixture_id.split(":")[-1],
        captured_at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        source_timestamp=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
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
        freshness_state="FRESH",
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


# --------------------------------------------------------------------------- behaviour
def test_collection_only_persists_snapshots_and_skips_value_pipeline(tmp_path, monkeypatch):
    config = _config(tmp_path, collection_only=True, max_fixtures=2)
    kickoffs = {fid: _kickoff(day) for day, fid in zip((1, 2, 3), ("api-football:1", "api-football:2", "api-football:3"))}
    _seed_live_issued_picks(config.model_pick_db_path, list(kickoffs))
    market = FakeMarketProvider({fid: _snapshots_for(fid, ko) for fid, ko in kickoffs.items()})
    services = _services(config, market)

    def _forbidden(*args, **kwargs):
        raise AssertionError("value pipeline must not run in collection-only mode")

    monkeypatch.setattr(services_mod, "build_fixture_market_evaluation", _forbidden)
    for method in (
        "_maybe_create_paper_entries",
        "_sync_current_portfolio_selections",
        "_sync_authoritative_corner_results",
        "_process_paper_trading_updates",
    ):
        monkeypatch.setattr(services, method, _forbidden)

    services._sync_current_market_state([_row(fid, ko) for fid, ko in kickoffs.items()], dashboard_conn=None)

    assert market.calls == 2  # bounded to max_fixtures_per_cycle
    with pt.connect(config.paper_trading_db_path) as conn:
        assert pt.fetch_market_snapshots(conn, initialize_schema=False)
        assert conn.execute("select count(*) from paper_trade_entries").fetchone()[0] == 0
        assert conn.execute("select count(*) from paper_trade_updates").fetchone()[0] == 0
    assert services._market_runtime_state["market_collection_mode"] == "COLLECTION_ONLY"
    assert services._market_runtime_state["value_evaluation"] == "DISABLED"
    assert services._market_runtime_state["state"] == "COLLECTION_ONLY"
    assert services._market_runtime_state["collection_observations"]


def test_collection_only_makes_no_calls_without_live_issued_pick(tmp_path):
    config = _config(tmp_path, collection_only=True, max_fixtures=2)
    _seed_live_issued_picks(config.model_pick_db_path, [])  # no picks
    market = FakeMarketProvider({})
    services = _services(config, market)
    services._sync_current_market_state([_row("api-football:1", _kickoff(1))], dashboard_conn=None)
    assert market.calls == 0


def test_collection_only_rotates_and_eventually_covers_all(tmp_path):
    config = _config(tmp_path, collection_only=True, max_fixtures=2)
    fixture_ids = [f"api-football:{n}" for n in range(1, 5)]
    kickoffs = {fid: _kickoff(n) for n, fid in enumerate(fixture_ids, start=1)}
    _seed_live_issued_picks(config.model_pick_db_path, fixture_ids)
    market = FakeMarketProvider({fid: _snapshots_for(fid, ko) for fid, ko in kickoffs.items()})
    services = _services(config, market)
    rows = [_row(fid, ko) for fid, ko in kickoffs.items()]

    services._sync_current_market_state(rows, dashboard_conn=None)
    first_cycle = list(market.called_fixture_ids)
    assert len(first_cycle) == 2
    services._sync_current_market_state(rows, dashboard_conn=None)
    second_cycle = market.called_fixture_ids[2:]
    assert len(second_cycle) == 2
    assert set(first_cycle) | set(second_cycle) == set(fixture_ids)  # no starvation
    assert first_cycle != second_cycle  # deterministic rotation


def test_legacy_full_mode_still_runs_value_evaluation(tmp_path, monkeypatch):
    config = _config(tmp_path, collection_only=False, max_fixtures=0)
    kickoff = _kickoff(1)
    fixture_id = "api-football:1"
    _seed_live_issued_picks(config.model_pick_db_path, [fixture_id])
    market = FakeMarketProvider({fixture_id: _snapshots_for(fixture_id, kickoff)})
    services = _services(config, market)

    evaluation_calls: list[int] = []

    def _fake_evaluation(**kwargs):
        evaluation_calls.append(1)
        return {
            "counts": {"preliminary_candidates": 0, "passed_safeguards": 0},
            "decision_rows": [],
        }

    monkeypatch.setattr(services_mod, "build_fixture_market_evaluation", _fake_evaluation)
    monkeypatch.setattr(services, "_maybe_create_paper_entries", lambda *a, **k: 0)
    monkeypatch.setattr(services, "_sync_current_portfolio_selections", lambda *a, **k: None)
    monkeypatch.setattr(services, "_sync_authoritative_corner_results", lambda *a, **k: None)
    monkeypatch.setattr(services, "_process_paper_trading_updates", lambda *a, **k: None)

    services._sync_current_market_state([_row(fixture_id, kickoff)], dashboard_conn=None)

    assert evaluation_calls  # full mode unchanged: evaluation runs
    assert services._market_runtime_state["market_collection_mode"] == "FULL"


def test_collection_only_not_enabled_by_default():
    config = DashboardConfig.default()
    assert config.market_collection_only is False
    assert config.market_collector_max_fixtures_per_cycle == 0
