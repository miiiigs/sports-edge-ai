"""Scheduler hook for the high-confidence AH shadow lane (disabled by default).

No provider calls are made. These pin:

* the hook is OFF unless ``SPORTS_AI_ENABLE_HIGH_CONFIDENCE_SHADOW`` is set, and never
  runs on the web role;
* when enabled, a cycle writes only the separate shadow DB: the dashboard, paper
  and Model Pick DBs are byte-identical afterwards;
* re-runs and restarts never duplicate a shadow entry, and cycle history survives;
* the worker loop waits for the startup sync and stops cleanly.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from sports_ai.dashboard import high_confidence_shadow as hc
from sports_ai.dashboard.scheduler_worker import _run_high_confidence_shadow_runtime
from sports_ai.dashboard.services import RUNTIME_ROLE_SCHEDULER, RUNTIME_ROLE_WEB, DashboardConfig, DashboardServices
from sports_ai.market_data.providers import NotConfiguredMarketProvider

KICKOFF = "2099-01-01T15:00:00+00:00"


def _payload():
    rows = []
    for line, q_home in {-0.5: 0.52, 0.0: 0.62, 0.5: 0.73, 1.0: 0.81}.items():
        for sel, q in (("HOME", q_home), ("AWAY", 1 - q_home)):
            rows.append({"market_family": "asian_handicap", "line": line, "selection": sel,
                         "fair_decimal_price": 1 / q, "effective_win_stake_probability": q * 0.95})

    def hist(venue):
        return {"available": True, "venue": venue,
                "matches_observed_by_statistic": {"shots": 10, "shots_on_target": 10, "corners": 10}}

    return {
        "blockers": [],
        "team_resolution": {side: {"canonical_team_id": f"eng1_{side}"} for side in ("home", "away")},
        "model_history": {"home": hist("home"), "away": hist("away")},
        "model_versions": {"current_snapshot_model_version": "football_dashboard_current_snapshot_v1"},
        "market_rows": rows,
    }


def _seed(tmp_path: Path) -> None:
    dash = sqlite3.connect(tmp_path / "dash.sqlite")
    dash.execute("CREATE TABLE dashboard_fixtures (fixture_id TEXT PRIMARY KEY, competition_id TEXT, home_team_name TEXT, "
                 "away_team_name TEXT, kickoff_utc TEXT, status TEXT, home_score INTEGER, away_score INTEGER)")
    dash.execute("CREATE TABLE dashboard_predictions (prediction_id TEXT PRIMARY KEY, fixture_id TEXT, status TEXT, "
                 "generated_at TEXT, model_as_of TEXT, model_snapshot_id TEXT, payload_json TEXT)")
    dash.execute("INSERT INTO dashboard_fixtures VALUES (?,?,?,?,?,?,?,?)",
                 ("api-football:1", "ENG1", "Leeds", "Fulham", KICKOFF, "PRE_MATCH", None, None))
    dash.execute("INSERT INTO dashboard_predictions VALUES (?,?,?,?,?,?,?)",
                 ("p1", "api-football:1", "READY", "2026-10-15T08:00:00+00:00", "2026-10-12T23:59:59+00:00",
                  "snap1", json.dumps(_payload())))
    dash.commit()
    dash.close()
    paper = sqlite3.connect(tmp_path / "paper.sqlite")
    paper.execute("CREATE TABLE market_snapshots (fixture_id TEXT, provider TEXT, bookmaker TEXT, market TEXT, selection TEXT, "
                  "line REAL, odds REAL, captured_at TEXT, provider_timestamp TEXT, is_pre_match INTEGER, ingestion_version TEXT)")
    paper.commit()
    paper.close()


def _services(tmp_path: Path, *, role=RUNTIME_ROLE_SCHEDULER):
    config = replace(
        DashboardConfig.default(),
        repo_root=tmp_path,
        dashboard_db_path=tmp_path / "dash.sqlite",
        paper_trading_db_path=tmp_path / "paper.sqlite",
        model_pick_db_path=tmp_path / "picks.sqlite",
        performance_output_dir=tmp_path / "performance",
        operational_data_dir=tmp_path,
        storage_mode="test_tmpdir",
        persistent_data_dir_configured=False,
        enable_scheduler=False,
        allow_paid_provider_calls=False,
        current_odds_connected=False,
        runtime_role=role,
    )
    return DashboardServices(
        config=config,
        fixture_provider=None,
        market_provider=NotConfiguredMarketProvider(provider_label="test"),
    )


def test_hook_is_off_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv(hc.ENABLE_ENV_VAR, raising=False)
    services = _services(tmp_path)
    assert services.high_confidence_shadow_scheduler_active() is False
    assert services.run_high_confidence_shadow_cycle() == {"state": "DISABLED", "issued": 0}
    assert not (tmp_path / hc.DEFAULT_DB_FILENAME).exists()
    assert services.get_health_payload()["high_confidence_shadow_enabled"] is False


def test_web_role_never_runs_even_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv(hc.ENABLE_ENV_VAR, "1")
    services = _services(tmp_path, role=RUNTIME_ROLE_WEB)
    assert services.high_confidence_shadow_scheduler_active() is False
    assert services.run_high_confidence_shadow_cycle()["state"] == "DISABLED"


def test_enabled_cycle_writes_only_the_shadow_db_and_is_idempotent_across_restart(monkeypatch, tmp_path):
    monkeypatch.setenv(hc.ENABLE_ENV_VAR, "1")
    monkeypatch.delenv(hc.DB_PATH_ENV_VAR, raising=False)
    _seed(tmp_path)
    paper_before = (tmp_path / "paper.sqlite").read_bytes()

    first = _services(tmp_path)
    result = first.run_high_confidence_shadow_cycle()
    assert result["error"] is None
    assert result["issue"]["issued"] == 1
    assert (tmp_path / "paper.sqlite").read_bytes() == paper_before
    assert not (tmp_path / "picks.sqlite").exists()  # Model Pick ledger never touched

    shadow = hc.connect(tmp_path / hc.DEFAULT_DB_FILENAME)
    (entry,) = [dict(r) for r in shadow.execute("SELECT * FROM hc_shadow_entries")]
    shadow.close()
    assert entry["issued_at"] < entry["kickoff_utc"]
    assert (entry["line"], entry["selection"]) == (0.5, "HOME")

    second = _services(tmp_path)
    with sqlite3.connect(tmp_path / "dash.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        second._load_runtime_state(conn)
    assert second._high_confidence_shadow_runtime_state["high_confidence_shadow_cycles_completed"] == 1
    again = second.run_high_confidence_shadow_cycle()
    assert again["issue"]["issued"] == 0 and again["issue"]["already_issued"] == 1
    health = second.get_health_payload()
    assert health["high_confidence_shadow_enabled"] is True
    assert health["high_confidence_shadow_cycles_completed"] == 2
    assert health["high_confidence_shadow_issued_last_cycle"] == 0


def test_cycle_error_is_recorded_not_raised(monkeypatch, tmp_path):
    monkeypatch.setenv(hc.ENABLE_ENV_VAR, "1")
    _seed(tmp_path)
    services = _services(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(hc, "issue_high_confidence_shadow", boom)
    result = services.run_high_confidence_shadow_cycle()
    assert result["error"] == "RuntimeError: synthetic"
    state = services._high_confidence_shadow_runtime_state
    assert state["high_confidence_shadow_last_error"] == "RuntimeError: synthetic"
    assert state["high_confidence_shadow_last_success_at"] is None


class _LoopServices:
    def __init__(self, *, active=True, startup_task=None):
        self.config = type("C", (), {"model_pick_issuance_interval_seconds": 3600})()
        self._active = active
        self._startup_sync_task = startup_task
        self.calls = 0

    def high_confidence_shadow_scheduler_active(self):
        return self._active

    def run_high_confidence_shadow_cycle(self):
        self.calls += 1
        return {}


def test_loop_is_noop_when_disabled():
    async def scenario():
        services = _LoopServices(active=False)
        await asyncio.wait_for(_run_high_confidence_shadow_runtime(services, asyncio.Event()), timeout=1)
        assert services.calls == 0

    asyncio.run(scenario())


def test_loop_waits_for_startup_sync_then_runs_and_stops_cleanly():
    async def scenario():
        startup_done = asyncio.Event()

        async def startup():
            await startup_done.wait()

        services = _LoopServices(startup_task=asyncio.create_task(startup()))
        stop = asyncio.Event()
        loop_task = asyncio.create_task(_run_high_confidence_shadow_runtime(services, stop))
        await asyncio.sleep(0.05)
        assert services.calls == 0
        startup_done.set()
        await asyncio.sleep(0.05)
        assert services.calls == 1
        stop.set()
        await asyncio.wait_for(loop_task, timeout=1)

    asyncio.run(scenario())
