"""Automatic forward-only Model Pick issuance and the prediction refresh loop.

No provider calls are made. These pin:

* picks are issued only while the issuance instant is before kickoff, and the
  stored ``created_at`` proves it;
* post-kickoff fixtures are never issued (scheduler and CLI share the rule);
* re-runs, later prediction versions and restarts never duplicate or change a pick;
* a second provider fixture id for the same canonical match is skipped, not merged;
* one conflicting row cannot block the rest;
* the prediction refresh sync never runs the market sync;
* existing retrospective rows are disclosed by ``issuance_timing``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from sports_ai.dashboard import model_pick_issuance as issuance
from sports_ai.dashboard import model_pick_maintenance as maintenance
from sports_ai.dashboard.model_pick_history import build_model_pick_history_payload
from sports_ai.dashboard.scheduler_worker import (
    _run_model_pick_issuance_runtime,
    _run_prediction_refresh_runtime,
)
from sports_ai.dashboard.services import RUNTIME_ROLE_SCHEDULER, RUNTIME_ROLE_WEB, DashboardConfig, DashboardServices
from sports_ai.market_data.providers import NotConfiguredMarketProvider
from sports_ai.storage import football_model_pick_ledger as ledger

KICKOFF = "2026-10-17T14:00:00+00:00"
BEFORE_KICKOFF = "2026-10-16T09:00:00+00:00"
AT_KICKOFF = KICKOFF
AFTER_KICKOFF = "2026-10-17T16:00:00+00:00"


def _mr(family, line, selection, fair, prob):
    return {
        "market_family": family,
        "line": line,
        "selection": selection,
        "fair_decimal_price": fair,
        "effective_win_stake_probability": prob,
    }


def _ah_rows(line_shift: float = 0.0):
    data = [
        (-1.0, 1.25, 4.00, 0.80, 0.25),
        (-0.5, 1.80, 2.10, 0.56, 0.48),
        (-0.25, 1.95, 1.98, 0.512, 0.505),
        (0.0, 2.05, 1.90, 0.49, 0.53),
        (0.5, 2.50, 1.55, 0.40, 0.65),
        (1.0, 4.00, 1.25, 0.25, 0.80),
    ]
    rows = []
    for line, home, away, hp, ap in data:
        rows.append(_mr("asian_handicap", line + line_shift, "HOME", home, hp))
        rows.append(_mr("asian_handicap", line + line_shift, "AWAY", away, ap))
    return rows


def _corner_rows():
    data = [
        (8.5, 1.40, 3.00, 0.71, 0.33),
        (9.5, 1.95, 1.93, 0.512, 0.518),
        (10.5, 3.50, 1.30, 0.29, 0.77),
    ]
    rows = []
    for line, over, under, op, up in data:
        rows.append(_mr("corner_total", line, "OVER", over, op))
        rows.append(_mr("corner_total", line, "UNDER", under, up))
    return rows


def _payload(line_shift: float = 0.0):
    return {
        "model_versions": {"current_snapshot_model_version": "football_dashboard_current_snapshot_v1"},
        "market_rows": _ah_rows(line_shift) + _corner_rows(),
    }


def _dashboard(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dashboard_fixtures (fixture_id TEXT PRIMARY KEY, competition_id TEXT, competition_name TEXT, "
        "home_team_name TEXT, away_team_name TEXT, kickoff_utc TEXT, status TEXT, home_score INTEGER, away_score INTEGER)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dashboard_predictions (prediction_id TEXT PRIMARY KEY, fixture_id TEXT, status TEXT, "
        "generated_at TEXT, model_as_of TEXT, model_snapshot_id TEXT, version_key TEXT, payload_json TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dashboard_fixture_results (fixture_id TEXT PRIMARY KEY, home_corners INTEGER, away_corners INTEGER, total_corners INTEGER)"
    )
    return conn


def _add_fixture(conn, fixture_id, *, kickoff=KICKOFF, home="Leeds", away="Fulham"):
    conn.execute(
        "INSERT INTO dashboard_fixtures VALUES (?,?,?,?,?,?,?,?,?)",
        (fixture_id, "ENG1", "Premier League", home, away, kickoff, "PRE_MATCH", None, None),
    )


def _add_prediction(conn, *, prediction_id, fixture_id, generated_at="2026-10-15T08:00:00+00:00", version_key="v1", line_shift=0.0, status="READY"):
    conn.execute(
        "INSERT INTO dashboard_predictions VALUES (?,?,?,?,?,?,?,?,?)",
        (prediction_id, fixture_id, status, generated_at, "2026-10-14T23:59:59+00:00", "snap1", version_key, json.dumps(_payload(line_shift)), generated_at),
    )


@pytest.fixture
def dbs(tmp_path):
    dash = _dashboard(tmp_path / "dash.sqlite")
    led = ledger.connect(tmp_path / "picks.sqlite")
    yield dash, led
    led.close()
    dash.close()


def _rows(led):
    return ledger.fetch_model_pick_rows(led, initialize_schema=False)


def test_future_fixture_is_issued_forward_with_proof_of_timing(dbs):
    dash, led = dbs
    _add_fixture(dash, "fx1")
    _add_prediction(dash, prediction_id="p1", fixture_id="fx1")
    dash.commit()

    result = issuance.issue_forward_model_picks(dash, led, now=BEFORE_KICKOFF)

    assert result["written"] == 2
    assert result["written_ah"] == 1 and result["written_corners"] == 1
    rows = _rows(led)
    assert {row["market"] for row in rows} == {"asian_handicap", "corner_total"}
    for row in rows:
        assert row["record_origin"] == ledger.RECORD_ORIGIN_LIVE_ISSUED
        assert row["created_at"] == BEFORE_KICKOFF
        assert row["prediction_generated_at"] < row["created_at"] < row["kickoff_utc"]
        assert ledger.issuance_timing(row) == ledger.ISSUANCE_TIMING_FORWARD


@pytest.mark.parametrize("issued_at", [AT_KICKOFF, AFTER_KICKOFF])
def test_post_kickoff_fixture_is_never_issued(dbs, issued_at):
    dash, led = dbs
    _add_fixture(dash, "fx1")
    _add_prediction(dash, prediction_id="p1", fixture_id="fx1")
    dash.commit()

    result = issuance.issue_forward_model_picks(dash, led, now=issued_at)

    assert result["written"] == 0
    assert result["kickoff_passed_not_issued"] == 1
    assert _rows(led) == []


def test_stale_latest_prediction_blocks_issuance_from_older_ready_version(dbs):
    dash, led = dbs
    _add_fixture(dash, "fx1")
    # READY earlier, then the model state went stale and the fixture was re-versioned BLOCKED.
    _add_prediction(dash, prediction_id="p_ready", fixture_id="fx1", generated_at="2026-10-15T06:00:00+00:00", version_key="v1")
    _add_prediction(dash, prediction_id="p_blocked", fixture_id="fx1", generated_at="2026-10-15T08:00:00+00:00", version_key="v2", status="BLOCKED")
    dash.commit()

    result = issuance.issue_forward_model_picks(dash, led, now=BEFORE_KICKOFF)
    assert result["written"] == 0
    assert result["latest_not_ready_not_issued"] == 1
    assert _rows(led) == []

    # Once the current system endorses the fixture again, the first pre-kickoff READY
    # version (p_ready) is the frozen source.
    _add_prediction(dash, prediction_id="p_ready_again", fixture_id="fx1", generated_at="2026-10-16T08:00:00+00:00", version_key="v3")
    dash.commit()
    result = issuance.issue_forward_model_picks(dash, led, now=BEFORE_KICKOFF)
    assert result["written"] == 2
    assert {row["prediction_id"] for row in _rows(led)} == {"p_ready"}


def test_cli_apply_shares_the_forward_only_rule(dbs):
    dash, led = dbs
    _add_fixture(dash, "fx1")
    _add_prediction(dash, prediction_id="p1", fixture_id="fx1")
    dash.commit()

    applied = maintenance.apply_live_issued(dash, led, now=AFTER_KICKOFF)

    assert applied["rows_written"] == 0
    assert applied["kickoff_passed_not_issued"] == 1


def test_rerun_never_duplicates(dbs):
    dash, led = dbs
    _add_fixture(dash, "fx1")
    _add_prediction(dash, prediction_id="p1", fixture_id="fx1")
    dash.commit()

    first = issuance.issue_forward_model_picks(dash, led, now=BEFORE_KICKOFF)
    second = issuance.issue_forward_model_picks(dash, led, now="2026-10-16T10:00:00+00:00")

    assert first["written"] == 2
    assert second["written"] == 0
    assert second["skipped"] == {issuance.SKIP_ALREADY_ISSUED: 2}
    assert len(_rows(led)) == 2


def test_issued_pick_is_frozen_against_later_prediction_versions(dbs):
    dash, led = dbs
    _add_fixture(dash, "fx1")
    _add_prediction(dash, prediction_id="p1", fixture_id="fx1")
    dash.commit()
    issuance.issue_forward_model_picks(dash, led, now=BEFORE_KICKOFF)
    before = {row["market"]: (row["line"], row["selection"], row["prediction_id"]) for row in _rows(led)}

    # The first-issued prediction disappears from the READY set (superseded) and
    # a new version with different lines arrives before kickoff.
    dash.execute("UPDATE dashboard_predictions SET status = 'SUPERSEDED' WHERE prediction_id = 'p1'")
    _add_prediction(dash, prediction_id="p2", fixture_id="fx1", generated_at="2026-10-16T08:00:00+00:00", version_key="v2", line_shift=-0.5)
    dash.commit()
    result = issuance.issue_forward_model_picks(dash, led, now="2026-10-16T12:00:00+00:00")

    assert result["written"] == 0
    assert result["conflicts"] == []
    after = {row["market"]: (row["line"], row["selection"], row["prediction_id"]) for row in _rows(led)}
    assert after == before


def test_canonical_duplicate_fixture_id_is_skipped_not_merged(dbs):
    dash, led = dbs
    # Same match published under two provider fixture ids.
    _add_fixture(dash, "football-data:1")
    _add_fixture(dash, "api-football:9")
    _add_prediction(dash, prediction_id="p_fd", fixture_id="football-data:1")
    _add_prediction(dash, prediction_id="p_af", fixture_id="api-football:9")
    dash.commit()

    result = issuance.issue_forward_model_picks(dash, led, now=BEFORE_KICKOFF)

    assert result["written"] == 2
    assert result["skipped"].get(issuance.SKIP_CANONICAL_DUPLICATE) == 2
    assert len(result["canonical_duplicates"]) == 2
    rows = _rows(led)
    assert len({row["fixture_id"] for row in rows}) == 1
    assert len({row["canonical_fixture_id"] for row in rows}) == 1


def test_one_conflicting_row_does_not_block_others(dbs):
    dash, led = dbs
    _add_fixture(dash, "fx1")
    _add_fixture(dash, "fx2", home="Arsenal", away="Chelsea")
    _add_prediction(dash, prediction_id="p1", fixture_id="fx1")
    _add_prediction(dash, prediction_id="p2", fixture_id="fx2")
    dash.commit()
    real_record = ledger.record_model_pick

    def flaky_record(conn, pick, **kwargs):
        if pick["fixture_id"] == "fx1" and pick["market"] == "asian_handicap":
            raise sqlite3.IntegrityError("MODEL_PICK_IMMUTABLE_CONFLICT: injected")
        return real_record(conn, pick, **kwargs)

    issuance.ledger.record_model_pick = flaky_record
    try:
        result = issuance.issue_forward_model_picks(dash, led, now=BEFORE_KICKOFF)
    finally:
        issuance.ledger.record_model_pick = real_record

    assert result["written"] == 3
    assert result["skipped"] == {issuance.SKIP_IMMUTABLE_CONFLICT: 1}
    assert len(_rows(led)) == 3
    # The next cycle completes the missing pick.
    retry = issuance.issue_forward_model_picks(dash, led, now="2026-10-16T09:10:00+00:00")
    assert retry["written"] == 1
    assert len(_rows(led)) == 4


# --------------------------------------------------------------------------- overlapping issuers
def _two_provider_ids_for_one_match(dash):
    _add_fixture(dash, "football-data:1")
    _add_fixture(dash, "api-football:9")
    _add_prediction(dash, prediction_id="p_fd", fixture_id="football-data:1")
    _add_prediction(dash, prediction_id="p_af", fixture_id="api-football:9")
    dash.commit()


def test_overlapping_issuer_with_stale_snapshot_never_duplicates(dbs, tmp_path, monkeypatch):
    dash, led = dbs
    _two_provider_ids_for_one_match(dash)
    first = issuance.issue_forward_model_picks(dash, led, now=BEFORE_KICKOFF)
    assert first["written"] == 2

    # A second issuer read the ledger before the first one wrote (empty snapshot).
    monkeypatch.setattr(issuance.ledger, "fetch_model_pick_rows", lambda *a, **k: [])
    other = ledger.connect(tmp_path / "picks.sqlite")
    try:
        second = issuance.issue_forward_model_picks(dash, other, now="2026-10-16T09:00:01+00:00")
    finally:
        other.close()
    monkeypatch.undo()

    assert second["written"] == 0
    assert second["skipped"] == {issuance.SKIP_ALREADY_ISSUED: 2, issuance.SKIP_CANONICAL_DUPLICATE: 2}
    assert second["conflicts"] == []
    rows = _rows(led)
    assert len(rows) == 2
    assert len({row["fixture_id"] for row in rows}) == 1


def test_concurrent_cycles_write_each_pick_exactly_once(tmp_path):
    import threading

    dash = _dashboard(tmp_path / "dash.sqlite")
    _two_provider_ids_for_one_match(dash)
    for n in range(4):
        _add_fixture(dash, f"fx{n}", home=f"Home{n}", away=f"Away{n}")
        _add_prediction(dash, prediction_id=f"p{n}", fixture_id=f"fx{n}")
    dash.commit()
    dash.close()
    ledger.ensure_schema(ledger.connect(tmp_path / "picks.sqlite"))

    barrier = threading.Barrier(4)
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker():
        d = sqlite3.connect(f"file:{(tmp_path / 'dash.sqlite').as_posix()}?mode=ro", uri=True)
        d.row_factory = sqlite3.Row
        led = ledger.connect(tmp_path / "picks.sqlite")
        try:
            barrier.wait()
            results.append(issuance.issue_forward_model_picks(d, led, now=BEFORE_KICKOFF))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            led.close()
            d.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == []
    led = ledger.connect(tmp_path / "picks.sqlite")
    rows = _rows(led)
    led.close()
    # 5 matches x (AH + corners); the two provider ids share one match.
    assert len(rows) == 10
    assert sum(r["written"] for r in results) == 10
    assert all(r["conflicts"] == [] for r in results)
    assert len({row["fixture_id"] for row in rows if row["fixture_id"] in ("football-data:1", "api-football:9")}) == 1


def test_cli_apply_skips_match_already_held_by_another_provider_id(dbs):
    dash, led = dbs
    _add_fixture(dash, "football-data:1")
    _add_prediction(dash, prediction_id="p_fd", fixture_id="football-data:1")
    dash.commit()
    issuance.issue_forward_model_picks(dash, led, now=BEFORE_KICKOFF)

    # The provider switch republishes the same match under a new id.
    _add_fixture(dash, "api-football:9")
    _add_prediction(dash, prediction_id="p_af", fixture_id="api-football:9")
    dash.commit()
    applied = maintenance.apply_live_issued(dash, led, now="2026-10-16T10:00:00+00:00")

    assert applied["rows_written"] == 0
    assert len(applied["canonical_duplicates_skipped"]) == 2
    assert {row["fixture_id"] for row in _rows(led)} == {"football-data:1"}


def test_cold_start_creates_ledger_and_issues_once(tmp_path):
    dash = _dashboard(tmp_path / "dash.sqlite")
    _add_fixture(dash, "fx1", kickoff="2099-01-01T15:00:00+00:00")
    _add_prediction(dash, prediction_id="p1", fixture_id="fx1")
    dash.commit()
    dash.close()
    assert not (tmp_path / "picks.sqlite").exists()

    services = _services(tmp_path)
    assert services.run_model_pick_issuance_cycle()["written"] == 2
    assert services.run_model_pick_issuance_cycle()["written"] == 0
    led = ledger.connect(tmp_path / "picks.sqlite")
    rows = _rows(led)
    led.close()
    assert len(rows) == 2
    assert all(row["created_at"] < row["kickoff_utc"] for row in rows)


# --------------------------------------------------------------------------- services
def _services(tmp_path: Path, *, issuance_enabled=True, refresh_enabled=False, role=RUNTIME_ROLE_SCHEDULER, fixture_provider=None):
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
        enable_model_pick_issuance_scheduler=issuance_enabled,
        enable_prediction_refresh_scheduler=refresh_enabled,
        runtime_role=role,
    )
    return DashboardServices(
        config=config,
        fixture_provider=fixture_provider,
        market_provider=NotConfiguredMarketProvider(provider_label="test"),
    )


def test_flags_default_off(monkeypatch, tmp_path):
    monkeypatch.setenv("SPORTS_AI_DATA_DIR", str(tmp_path))
    for name in (
        issuance.ISSUANCE_SCHEDULER_ENV_VAR,
        issuance.ISSUANCE_INTERVAL_ENV_VAR,
        issuance.PREDICTION_REFRESH_SCHEDULER_ENV_VAR,
        issuance.PREDICTION_REFRESH_INTERVAL_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)
    config = DashboardConfig.default()
    assert config.enable_model_pick_issuance_scheduler is False
    assert config.enable_prediction_refresh_scheduler is False
    assert config.model_pick_issuance_interval_seconds == 600
    assert config.prediction_refresh_interval_seconds == 3600
    assert issuance.resolve_prediction_refresh_interval_seconds(60) == issuance.MIN_PREDICTION_REFRESH_INTERVAL_SECONDS


def test_overlapping_cycle_in_same_process_is_skipped(tmp_path):
    services = _services(tmp_path)
    assert services._model_pick_issuance_lock.acquire(blocking=False)
    try:
        assert services.run_model_pick_issuance_cycle() == {"state": "SKIPPED_CYCLE_IN_PROGRESS", "written": 0}
    finally:
        services._model_pick_issuance_lock.release()
    assert services.run_model_pick_issuance_cycle().get("state") != "SKIPPED_CYCLE_IN_PROGRESS"


def test_web_role_never_issues(tmp_path):
    services = _services(tmp_path, role=RUNTIME_ROLE_WEB)
    assert services.model_pick_issuance_scheduler_active() is False
    assert services.prediction_refresh_scheduler_active() is False
    assert services.run_model_pick_issuance_cycle() == {"state": "DISABLED", "written": 0}


def test_restart_recovery_is_idempotent_and_keeps_history(tmp_path):
    dash = _dashboard(tmp_path / "dash.sqlite")
    _add_fixture(dash, "fx1", kickoff="2099-01-01T15:00:00+00:00")
    _add_prediction(dash, prediction_id="p1", fixture_id="fx1")
    dash.commit()
    dash.close()

    first = _services(tmp_path)
    first_result = first.run_model_pick_issuance_cycle()
    assert first_result["written"] == 2

    # A new process (restart / redeploy) loads persisted state and re-runs safely.
    second = _services(tmp_path)
    with sqlite3.connect(tmp_path / "dash.sqlite") as conn:
        conn.row_factory = sqlite3.Row
        second._load_runtime_state(conn)
    assert second._model_pick_issuance_runtime_state["model_pick_issuance_cycles_completed"] == 1
    second_result = second.run_model_pick_issuance_cycle()
    assert second_result["written"] == 0
    assert second._model_pick_issuance_runtime_state["model_pick_issuance_cycles_completed"] == 2
    led = ledger.connect(tmp_path / "picks.sqlite")
    assert len(_rows(led)) == 2
    led.close()
    health = second.get_health_payload()
    assert health["model_pick_issuance_cycles_completed"] == 2
    assert health["model_pick_issuance_written_last_cycle"] == 0


class _EmptyFixtureProvider:
    def provider_name(self) -> str:
        return "empty-fixture-provider"

    def configured(self) -> bool:
        return True

    def get_live_fixtures(self, *args, **kwargs):
        return []

    def get_fixtures_for_date(self, *args, **kwargs):
        return []

    def get_upcoming_fixtures(self, *args, **kwargs):
        return []

    def get_fixture(self, *args, **kwargs):
        return None

    def diagnostics(self) -> dict:
        return {"provider": "empty-fixture-provider", "state": "CONNECTED"}


def test_prediction_refresh_never_runs_market_sync(tmp_path, monkeypatch):
    services = _services(tmp_path, issuance_enabled=False, refresh_enabled=True, fixture_provider=_EmptyFixtureProvider())
    calls: list[str] = []
    monkeypatch.setattr(services, "_run_market_sync", lambda rows, conn: calls.append("market"))

    asyncio.run(services.run_prediction_refresh_cycle())
    assert calls == []
    assert services._model_pick_issuance_runtime_state["prediction_refresh_cycles_completed"] == 1

    # The ordinary sync path still runs the market sync.
    asyncio.run(services.sync_once(sync_kind="manual", include_schedule=True))
    assert calls == ["market"]


# --------------------------------------------------------------------------- worker loops
class _LoopServices:
    def __init__(self, *, issuance_active=True, refresh_active=True, startup_task=None):
        self.config = type("C", (), {"model_pick_issuance_interval_seconds": 3600, "prediction_refresh_interval_seconds": 3600})()
        self._issuance_active = issuance_active
        self._refresh_active = refresh_active
        self._startup_sync_task = startup_task
        self.issuance_calls = 0
        self.refresh_calls = 0

    def model_pick_issuance_scheduler_active(self):
        return self._issuance_active

    def prediction_refresh_scheduler_active(self):
        return self._refresh_active

    def run_model_pick_issuance_cycle(self):
        self.issuance_calls += 1
        return {"written": 0}

    async def run_prediction_refresh_cycle(self):
        self.refresh_calls += 1
        return {}


def test_issuance_loop_waits_for_startup_sync_then_runs_once_per_interval():
    async def scenario():
        startup_done = asyncio.Event()

        async def startup():
            await startup_done.wait()

        task = asyncio.create_task(startup())
        services = _LoopServices(startup_task=task)
        stop = asyncio.Event()
        loop_task = asyncio.create_task(_run_model_pick_issuance_runtime(services, stop))
        await asyncio.sleep(0.05)
        assert services.issuance_calls == 0  # still waiting for startup sync
        startup_done.set()
        await asyncio.sleep(0.05)
        assert services.issuance_calls == 1  # first cycle right after startup
        stop.set()
        await asyncio.wait_for(loop_task, timeout=1)

    asyncio.run(scenario())


def test_disabled_loops_are_noops():
    async def scenario():
        services = _LoopServices(issuance_active=False, refresh_active=False)
        stop = asyncio.Event()
        await asyncio.wait_for(_run_model_pick_issuance_runtime(services, stop), timeout=1)
        await asyncio.wait_for(_run_prediction_refresh_runtime(services, stop), timeout=1)
        assert services.issuance_calls == 0 and services.refresh_calls == 0

    asyncio.run(scenario())


def test_prediction_refresh_loop_skips_first_tick_and_stops_cleanly():
    async def scenario():
        services = _LoopServices()
        stop = asyncio.Event()
        loop_task = asyncio.create_task(_run_prediction_refresh_runtime(services, stop))
        await asyncio.sleep(0.05)
        assert services.refresh_calls == 0  # startup already synced
        stop.set()
        await asyncio.wait_for(loop_task, timeout=1)
        assert services.refresh_calls == 0

    asyncio.run(scenario())


# --------------------------------------------------------------------------- disclosure
def test_existing_retrospective_rows_are_disclosed_not_rewritten(tmp_path):
    led = ledger.connect(tmp_path / "picks.sqlite")
    ledger.ensure_schema(led)
    base = {
        "policy_id": "football_model_pick_balanced_line_v1",
        "policy_version": "v1",
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "kickoff_utc": KICKOFF,
        "line": -0.25,
        "selection": "HOME",
        "model_probability": 0.56,
        "model_fair_price": 1.79,
        "prediction_id": "p",
        "model_snapshot_id": "snap1",
        "model_version": "v",
        "prediction_generated_at": "2026-10-15T08:00:00+00:00",
        "model_as_of": "2026-10-14T23:59:59+00:00",
        "record_origin": ledger.RECORD_ORIGIN_LIVE_ISSUED,
        "source_prediction_version_key": "v1",
        "market": "asian_handicap",
    }
    ledger.record_model_pick(led, {**base, "fixture_id": "forward"}, now=BEFORE_KICKOFF, initialize_schema=False)
    ledger.record_model_pick(led, {**base, "fixture_id": "backfill"}, now=AFTER_KICKOFF, initialize_schema=False)
    led.commit()
    rows = {row["fixture_id"]: row for row in _rows(led)}
    led.close()

    assert rows["backfill"]["record_origin"] == ledger.RECORD_ORIGIN_LIVE_ISSUED  # immutable, untouched
    assert ledger.issuance_timing(rows["forward"]) == ledger.ISSUANCE_TIMING_FORWARD
    assert ledger.issuance_timing(rows["backfill"]) == ledger.ISSUANCE_TIMING_RETROSPECTIVE
    perf = ledger.model_pick_performance(rows.values())
    assert perf["by_issuance_timing"][ledger.ISSUANCE_TIMING_FORWARD]["issued"] == 1
    assert perf["by_issuance_timing"][ledger.ISSUANCE_TIMING_RETROSPECTIVE]["issued"] == 1

    payload = build_model_pick_history_payload(model_pick_db_path=tmp_path / "picks.sqlite")
    assert payload["by_issuance_timing"][ledger.ISSUANCE_TIMING_FORWARD]["issued"] == 1
    assert payload["by_issuance_timing"][ledger.ISSUANCE_TIMING_RETROSPECTIVE]["issued"] == 1
    assert payload["forward_by_market"]["asian_handicap"]["issued"] == 1
