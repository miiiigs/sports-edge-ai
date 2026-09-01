from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi.testclient import TestClient

from sports_ai.dashboard.app import create_app
from sports_ai.dashboard.models import FIXTURE_STATUS_LIVE, NormalizedFixture
from sports_ai.dashboard import runtime_supervisor
from sports_ai.dashboard import storage as dashboard_storage
from sports_ai.dashboard.runtime_supervisor import build_child_specs
from sports_ai.dashboard.services import (
    DEFAULT_RUNTIME_ROLE_ENV_VAR,
    DashboardConfig,
    DashboardServices,
    RUNTIME_ROLE_SCHEDULER,
    RUNTIME_ROLE_WEB,
)
from sports_ai.market_data.providers import NormalizedMarketSnapshot


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _performance_dir(tmp_path: Path) -> Path:
    performance_dir = tmp_path / "performance"
    performance_dir.mkdir(parents=True, exist_ok=True)
    (performance_dir / "football_performance_summary.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-08-20T04:18:32+00:00",
                "summary": {"MODEL_VALIDATION": "PROMISING"},
                "status_flags": {"HISTORICAL_BENCHMARK_COMPLETE": "YES"},
            }
        ),
        encoding="utf-8",
    )
    (performance_dir / "football_performance_by_market.csv").write_text(
        "market,line,selection,sample_size,hit_rate,mean_predicted_probability,actual_frequency,brier,log_loss,avg_odds,break_even_rate,roi,status,notes\n",
        encoding="utf-8",
    )
    (performance_dir / "football_performance_by_competition.csv").write_text(
        "competition_id,fixtures,1x2_accuracy,1x2_brier,1x2_log_loss,goals_mae,corners_mae,candidate_count,candidate_roi,status\n",
        encoding="utf-8",
    )
    (performance_dir / "football_performance_by_season.csv").write_text(
        "season_code,fixtures,1x2_accuracy,1x2_brier,1x2_log_loss,goals_mae,corners_mae,status\n",
        encoding="utf-8",
    )
    (performance_dir / "football_calibration.csv").write_text(
        "scope_type,scope_value,market,line,selection,subset,bucket_label,bucket_lo,bucket_hi,predicted_mean_probability,observed_hit_rate,sample_size,calibration_gap\n",
        encoding="utf-8",
    )
    return performance_dir


def _dashboard_config(tmp_path: Path, *, runtime_role: str) -> DashboardConfig:
    repo_root = _repo_root()
    return DashboardConfig(
        repo_root=repo_root,
        research_db_path=repo_root / "research" / "football_research" / "store" / "football_research.sqlite",
        dashboard_db_path=tmp_path / "football_dashboard.sqlite",
        paper_trading_db_path=tmp_path / "football_paper_trading.sqlite",
        performance_output_dir=_performance_dir(tmp_path),
        operational_data_dir=tmp_path,
        storage_mode="test_tmpdir",
        persistent_data_dir_configured=False,
        display_timezone="Asia/Manila",
        upcoming_horizon_days=3,
        scheduler_interval_seconds=9999,
        live_refresh_interval_seconds=15,
        future_refresh_interval_seconds=9999,
        enable_scheduler=True,
        current_odds_connected=False,
        enable_odds_sync=False,
        current_xg_connected=False,
        allow_paid_provider_calls=False,
        startup_sync_timeout_seconds=0,
        runtime_role=runtime_role,
    )


def _fixture() -> NormalizedFixture:
    kickoff_utc = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat()
    return NormalizedFixture(
        fixture_id="fake:isolated_fixture",
        provider_fixture_id="fixture-1",
        provider="fake-provider",
        competition_id="ENG1",
        competition_name="Premier League",
        division_code="E0",
        season="2026",
        home_team_id="100",
        home_team_name="Arsenal",
        away_team_id="200",
        away_team_name="Chelsea",
        kickoff_utc=kickoff_utc,
        status=FIXTURE_STATUS_LIVE,
        elapsed_minute=12,
        home_score=0,
        away_score=0,
        last_provider_update=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        source_version="fake_fixture_provider_v1",
        provider_payload={"fixture_id": "fake:isolated_fixture"},
    )


class CountingFixtureProvider:
    def __init__(self) -> None:
        self.calls = 0

    def provider_name(self) -> str:
        return "counting-fixture-provider"

    def configured(self) -> bool:
        return True

    def reset_sync_diagnostics(self) -> None:
        return None

    def sync_diagnostics(self) -> dict:
        return {"provider": self.provider_name(), "query_count": self.calls, "has_errors": False, "queries": []}

    def get_live_fixtures(self) -> list[NormalizedFixture]:
        self.calls += 1
        return [_fixture()]

    def get_fixtures_for_date(self, target_date: date) -> list[NormalizedFixture]:
        self.calls += 1
        timezone_name = ZoneInfo("Asia/Manila")
        fixture = _fixture()
        return [fixture] if datetime.fromisoformat(fixture.kickoff_utc).astimezone(timezone_name).date() == target_date else []

    def get_upcoming_fixtures(self, *, from_utc: datetime, to_utc: datetime) -> list[NormalizedFixture]:
        self.calls += 1
        return [_fixture()]

    def get_fixture(self, fixture_id: str) -> NormalizedFixture | None:
        self.calls += 1
        return _fixture() if fixture_id == "fake:isolated_fixture" else None


class CountingMarketProvider:
    def __init__(self) -> None:
        self.calls = 0

    def provider_name(self) -> str:
        return "counting-market-provider"

    def configured(self) -> bool:
        return True

    def get_fixture_markets(self, fixture_id: str, *, fixture_context: dict | None = None) -> list[NormalizedMarketSnapshot]:
        self.calls += 1
        return []

    def get_best_prices(self, fixture_id: str, *, fixture_context: dict | None = None) -> list[NormalizedMarketSnapshot]:
        self.calls += 1
        return []

    def get_market_snapshot(self, fixture_id: str, *, fixture_context: dict | None = None) -> list[NormalizedMarketSnapshot]:
        self.calls += 1
        return []

    def diagnostics(self) -> dict:
        return {
            "provider": self.provider_name(),
            "configured": True,
            "state": "CONNECTED",
            "current_market_decisions": "READY",
            "reason": None,
        }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_url(url: str, *, timeout_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=1.0)
            if response.status_code == 200:
                return
        except Exception as exc:
            last_error = exc
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {url}: {last_error}")


def test_web_role_initializes_without_startup_sync_scheduler_or_provider_calls(tmp_path: Path) -> None:
    fixture_provider = CountingFixtureProvider()
    market_provider = CountingMarketProvider()
    services = DashboardServices(
        config=_dashboard_config(tmp_path, runtime_role=RUNTIME_ROLE_WEB),
        fixture_provider=fixture_provider,
        market_provider=market_provider,
    )
    asyncio.run(services.initialize())

    assert services._startup_sync_task is None
    assert services._background_task is None
    assert fixture_provider.calls == 0
    assert market_provider.calls == 0

    app = create_app(services=services)
    with TestClient(app) as client:
        for route in ("/status", "/today", "/betting-desk", "/performance", "/matches", "/member-card"):
            response = client.get(route)
            assert response.status_code == 200
        refresh_response = client.post("/api/fixtures/refresh")
        assert refresh_response.status_code == 200
        assert refresh_response.json()["refresh_state"] == "DISABLED_IN_WEB_ROLE"

    assert fixture_provider.calls == 0
    assert market_provider.calls == 0
    health = services.get_health_payload()
    assert health["runtime_role"] == RUNTIME_ROLE_WEB
    assert health["scheduler_execution_mode"] == "EXTERNAL_PROCESS"
    assert health["startup_sync_task_running"] is False
    assert health["local_scheduler_running"] is False
    assert health["scheduler_running"] is None
    asyncio.run(services.shutdown())


def test_web_role_refreshes_scheduler_persisted_runtime_state_without_side_effects(tmp_path: Path, monkeypatch) -> None:
    fixture_provider = CountingFixtureProvider()
    market_provider = CountingMarketProvider()
    services = DashboardServices(
        config=_dashboard_config(tmp_path, runtime_role=RUNTIME_ROLE_WEB),
        fixture_provider=fixture_provider,
        market_provider=market_provider,
    )
    asyncio.run(services.initialize())

    assert services._startup_sync_task is None
    assert services._background_task is None
    assert fixture_provider.calls == 0
    assert market_provider.calls == 0

    refreshed_at = "2026-09-01T12:34:56+00:00"
    with dashboard_storage.connect(services.config.dashboard_db_path) as conn:
        dashboard_storage.put_runtime_state(
            conn,
            state_key="provider_state",
            state_value={
                "state": "CONNECTED",
                "last_sync_at": refreshed_at,
                "last_attempted_sync_at": refreshed_at,
                "last_successful_sync_at": refreshed_at,
                "last_sync_kind": "scheduler",
                "last_counts": {"normalized": 77, "supported": 66, "live": 5, "today": 9, "upcoming": 11, "next": 3},
            },
            updated_at=refreshed_at,
            initialize_schema=False,
        )
        dashboard_storage.put_runtime_state(
            conn,
            state_key="prediction_state",
            state_value={
                "state": "READY",
                "last_prediction_generated_at": refreshed_at,
                "last_successful_prediction_at": refreshed_at,
                "last_prediction_error": None,
                "pending_fixture_count": 1,
                "blocked_fixture_count": 2,
                "ready_fixture_count": 42,
            },
            updated_at=refreshed_at,
            initialize_schema=False,
        )
        dashboard_storage.put_runtime_state(
            conn,
            state_key="market_runtime_state",
            state_value={
                "state": "SYNCED",
                "automatic_odds_sync": "DISABLED",
                "last_sync_at": refreshed_at,
                "last_successful_snapshot_at": refreshed_at,
                "last_error": None,
                "fixtures_considered": 12,
                "fixtures_matched": 10,
                "market_snapshots": 8,
                "markets_covered": ["1x2"],
                "freshness": "CURRENT",
                "unresolved_fixture_count": 0,
                "stale_market_count": 0,
                "candidate_count": 6,
                "paper_entry_count": 4,
                "settlement_queue_count": 2,
                "decision_count": 7,
                "evaluations": {},
            },
            updated_at=refreshed_at,
            initialize_schema=False,
        )

    side_effect_counts = {"initialize_database": 0, "ensure_schema": 0, "put_runtime_state": 0}

    def fail_initialize_database(conn) -> None:
        side_effect_counts["initialize_database"] += 1
        raise AssertionError("GET must not initialize schema or WAL")

    def fail_ensure_schema(conn) -> None:
        side_effect_counts["ensure_schema"] += 1
        raise AssertionError("GET must not run dashboard DDL")

    def fail_put_runtime_state(*args, **kwargs) -> None:
        side_effect_counts["put_runtime_state"] += 1
        raise AssertionError("GET must not write runtime state")

    monkeypatch.setattr(dashboard_storage, "initialize_database", fail_initialize_database)
    monkeypatch.setattr(dashboard_storage, "ensure_schema", fail_ensure_schema)
    monkeypatch.setattr(dashboard_storage, "put_runtime_state", fail_put_runtime_state)

    system_status = services.get_system_status()
    health = services.get_health_payload()

    assert system_status["fixture_feed"]["last_sync_at"] == refreshed_at
    assert system_status["fixture_feed"]["counts"]["supported"] == 66
    assert system_status["prediction_engine"]["ready_fixture_count"] == 42
    assert system_status["market_sync"]["state"] == "SYNCED"
    assert health["last_fixture_sync_at"] == refreshed_at
    assert health["market_sync_state"] == "SYNCED"
    assert health["last_market_sync_at"] == refreshed_at
    assert fixture_provider.calls == 0
    assert market_provider.calls == 0
    assert side_effect_counts == {"initialize_database": 0, "ensure_schema": 0, "put_runtime_state": 0}

    asyncio.run(services.shutdown())


def test_scheduler_role_runs_existing_startup_sync_and_scheduler(tmp_path: Path, monkeypatch) -> None:
    services = DashboardServices(
        config=_dashboard_config(tmp_path, runtime_role=RUNTIME_ROLE_SCHEDULER),
        fixture_provider=CountingFixtureProvider(),
        market_provider=CountingMarketProvider(),
    )
    calls: list[tuple[str, bool]] = []

    async def fake_sync_once(*, sync_kind: str = "manual", include_schedule: bool = True) -> dict[str, str]:
        calls.append((sync_kind, include_schedule))
        return {"state": "ok"}

    monkeypatch.setattr(services, "sync_once", fake_sync_once)

    async def scenario() -> None:
        await services.initialize()
        assert services._startup_sync_task is not None
        await asyncio.wait_for(services._startup_sync_task, timeout=1)
        assert calls == [("startup", True)]
        assert services._background_task is not None
        assert services._background_task.done() is False
        await services.shutdown()

    asyncio.run(scenario())


def test_supervisor_defines_one_web_child_and_one_scheduler_child(monkeypatch) -> None:
    monkeypatch.setenv("PORT", "9123")
    specs = build_child_specs()
    assert [spec.name for spec in specs] == ["web", "scheduler"]
    assert specs[0].env[DEFAULT_RUNTIME_ROLE_ENV_VAR] == RUNTIME_ROLE_WEB
    assert specs[1].env[DEFAULT_RUNTIME_ROLE_ENV_VAR] == RUNTIME_ROLE_SCHEDULER
    assert specs[0].argv[:3] == [sys.executable, "-m", "uvicorn"]
    assert specs[0].argv[-1] == "9123"
    assert specs[1].argv == [sys.executable, "-m", "sports_ai.dashboard.scheduler_worker"]


def test_supervisor_terminates_peer_when_child_exits(monkeypatch) -> None:
    class FakeChild:
        def __init__(self, return_code: int | None) -> None:
            self.return_code = return_code
            self.terminated = False
            self.killed = False

        def poll(self) -> int | None:
            return self.return_code

        def terminate(self) -> None:
            self.terminated = True
            self.return_code = 0

        def kill(self) -> None:
            self.killed = True
            self.return_code = -9

    launched_children = [FakeChild(1), FakeChild(None)]
    pending_children = list(launched_children)

    monkeypatch.setattr(
        runtime_supervisor,
        "build_child_specs",
        lambda: [
            runtime_supervisor.ChildSpec("web", ["web"], {}),
            runtime_supervisor.ChildSpec("scheduler", ["scheduler"], {}),
        ],
    )
    monkeypatch.setattr(runtime_supervisor.subprocess, "Popen", lambda *args, **kwargs: pending_children.pop(0))
    monkeypatch.setattr(runtime_supervisor.time, "sleep", lambda _seconds: None)

    assert runtime_supervisor.main() == 1
    assert pending_children == []
    assert launched_children[1].terminated is True
    assert launched_children[1].killed is False


def test_supervisor_launches_scheduler_only_after_web_readiness(monkeypatch) -> None:
    class FakeChild:
        def __init__(self, name: str, return_code: int | None) -> None:
            self.name = name
            self.return_code = return_code
            self.terminated = False
            self.killed = False

        def poll(self) -> int | None:
            return self.return_code

        def terminate(self) -> None:
            self.terminated = True
            self.return_code = 0

        def kill(self) -> None:
            self.killed = True
            self.return_code = -9

    launched: list[str] = []

    def fake_popen(argv, env):
        name = str(argv[0])
        launched.append(name)
        return FakeChild(name, None if name == "web" else 1)

    def fake_wait(child, readiness_url):
        assert child.name == "web"
        assert launched == ["web"]
        assert readiness_url == "http://127.0.0.1:9124/health"
        return True

    monkeypatch.setattr(
        runtime_supervisor,
        "build_child_specs",
        lambda: [
            runtime_supervisor.ChildSpec("web", ["web"], {}, "http://127.0.0.1:9124/health"),
            runtime_supervisor.ChildSpec("scheduler", ["scheduler"], {}),
        ],
    )
    monkeypatch.setattr(runtime_supervisor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(runtime_supervisor, "_wait_for_web_readiness", fake_wait)
    monkeypatch.setattr(runtime_supervisor.time, "sleep", lambda _seconds: None)

    assert runtime_supervisor.main() == 1
    assert launched == ["web", "scheduler"]


def test_supervisor_does_not_launch_scheduler_when_web_exits_before_readiness(monkeypatch) -> None:
    class FakeChild:
        def __init__(self) -> None:
            self.terminated = False
            self.killed = False

        def poll(self) -> int:
            return 1

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

    launched: list[str] = []
    web_child = FakeChild()

    def fake_popen(argv, env):
        launched.append(str(argv[0]))
        return web_child

    monkeypatch.setattr(
        runtime_supervisor,
        "build_child_specs",
        lambda: [
            runtime_supervisor.ChildSpec("web", ["web"], {}, "http://127.0.0.1:9125/health"),
            runtime_supervisor.ChildSpec("scheduler", ["scheduler"], {}),
        ],
    )
    monkeypatch.setattr(runtime_supervisor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(runtime_supervisor.time, "monotonic", lambda: 0.0)

    assert runtime_supervisor.main() == 1
    assert launched == ["web"]
    assert web_child.terminated is False


def test_supervisor_does_not_launch_scheduler_when_web_readiness_times_out(monkeypatch) -> None:
    class FakeChild:
        def __init__(self) -> None:
            self.return_code: int | None = None
            self.terminated = False
            self.killed = False

        def poll(self) -> int | None:
            return self.return_code

        def terminate(self) -> None:
            self.terminated = True
            self.return_code = 0

        def kill(self) -> None:
            self.killed = True
            self.return_code = -9

    launched: list[str] = []
    web_child = FakeChild()

    def fake_popen(argv, env):
        launched.append(str(argv[0]))
        return web_child

    monkeypatch.setattr(
        runtime_supervisor,
        "build_child_specs",
        lambda: [
            runtime_supervisor.ChildSpec("web", ["web"], {}, "http://127.0.0.1:9126/health"),
            runtime_supervisor.ChildSpec("scheduler", ["scheduler"], {}),
        ],
    )
    monkeypatch.setattr(runtime_supervisor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(runtime_supervisor, "_wait_for_web_readiness", lambda child, readiness_url: False)

    assert runtime_supervisor.main() == 1
    assert launched == ["web"]
    assert web_child.terminated is True
    assert web_child.killed is False


def test_supervisor_signal_cleanup_returns_success(monkeypatch) -> None:
    class FakeChild:
        def __init__(self, name: str) -> None:
            self.name = name
            self.return_code: int | None = None
            self.terminated = False
            self.killed = False

        def poll(self) -> int | None:
            return self.return_code

        def terminate(self) -> None:
            self.terminated = True
            self.return_code = 0

        def kill(self) -> None:
            self.killed = True
            self.return_code = -9

    launched_children: list[FakeChild] = []
    signal_handlers = {}

    def fake_popen(argv, env):
        child = FakeChild(str(argv[0]))
        launched_children.append(child)
        return child

    def fake_sleep(_seconds):
        signal_handlers[signal.SIGTERM](signal.SIGTERM, None)

    monkeypatch.setattr(
        runtime_supervisor,
        "build_child_specs",
        lambda: [
            runtime_supervisor.ChildSpec("web", ["web"], {}, "http://127.0.0.1:9127/health"),
            runtime_supervisor.ChildSpec("scheduler", ["scheduler"], {}),
        ],
    )
    monkeypatch.setattr(runtime_supervisor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(runtime_supervisor, "_wait_for_web_readiness", lambda child, readiness_url: True)
    monkeypatch.setattr(runtime_supervisor.signal, "signal", lambda signum, handler: signal_handlers.setdefault(signum, handler))
    monkeypatch.setattr(runtime_supervisor.time, "sleep", fake_sleep)

    assert runtime_supervisor.main() == 0
    assert [child.name for child in launched_children] == ["web", "scheduler"]
    assert all(child.terminated for child in launched_children)
    assert not any(child.killed for child in launched_children)


def test_web_and_scheduler_processes_are_pid_isolated_and_web_stays_responsive(tmp_path: Path) -> None:
    port = _free_port()
    src_path = str(_repo_root() / "code" / "src")
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": src_path,
            "SPORTS_AI_DATA_DIR": str(tmp_path / "data"),
            "SPORTS_AI_RUNTIME_ROLE": RUNTIME_ROLE_WEB,
            "SPORTS_AI_ENABLE_AUTOMATED_SYNC": "1",
            "SPORTS_AI_STARTUP_SYNC_TIMEOUT_SECONDS": "0",
            "SPORTS_AI_CURRENT_ODDS_CONNECTED": "0",
            "SPORTS_AI_ENABLE_ODDS_SYNC": "0",
            "SPORTS_AI_ALLOW_PAID_PROVIDER_CALLS": "0",
        }
    )
    for key in ("SPORTS_AI_API_FOOTBALL_TOKEN", "SPORTS_AI_FOOTBALL_DATA_TOKEN", "SPORTS_AI_THE_ODDS_API_KEY"):
        env.pop(key, None)

    web = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "sports_ai.dashboard.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(_repo_root() / "code"),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    scheduler: subprocess.Popen | None = None
    try:
        _wait_for_url(f"http://127.0.0.1:{port}/health")
        scheduler_env = {**env, "SPORTS_AI_RUNTIME_ROLE": RUNTIME_ROLE_SCHEDULER, "SPORTS_AI_SCHEDULER_WORKER_TEST_BLOCK_SECONDS": "5"}
        scheduler = subprocess.Popen(
            [sys.executable, "-m", "sports_ai.dashboard.scheduler_worker"],
            cwd=str(_repo_root() / "code"),
            env=scheduler_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert web.pid != scheduler.pid

        status_started = time.perf_counter()
        status_response = httpx.get(f"http://127.0.0.1:{port}/status", timeout=3.0)
        status_elapsed = time.perf_counter() - status_started

        today_started = time.perf_counter()
        today_response = httpx.get(f"http://127.0.0.1:{port}/today", timeout=3.0)
        today_elapsed = time.perf_counter() - today_started

        assert scheduler.poll() is None
        assert status_response.status_code == 200
        assert today_response.status_code == 200
        assert status_elapsed < 3.0
        assert today_elapsed < 3.0
    finally:
        for process in (web, scheduler):
            if process is None:
                continue
            if process.poll() is None:
                process.terminate()
        for process in (web, scheduler):
            if process is None:
                continue
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
