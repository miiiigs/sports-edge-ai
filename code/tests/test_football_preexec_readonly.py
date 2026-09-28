from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from sports_ai.diagnostics.football_preexec_readonly import (
    EXPECTED_JOB_ID,
    OPERATIONAL_CAP,
    _readonly_connect,
    run_fra1_preexec_readonly,
)

PROVIDER = "api-football"


def _now_plus(days: int = 0, seconds: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days, seconds=seconds)).isoformat(timespec="seconds")


def _make_budget_db(path, state=None, circuit=None, metrics=None):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE provider_quota_state (provider TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE provider_circuit_state (provider TEXT PRIMARY KEY, current_state TEXT NOT NULL, failure_count INTEGER NOT NULL DEFAULT 0, blocked_until TEXT, last_error TEXT, last_error_at TEXT, updated_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE provider_budget_metrics (provider TEXT NOT NULL, metric TEXT NOT NULL, value INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (provider, metric))")
    if state is not None:
        conn.execute(
            "INSERT INTO provider_quota_state (provider, state_json, updated_at) VALUES (?, ?, ?)",
            (PROVIDER, json.dumps(state), _now_plus()),
        )
    if circuit is not None:
        conn.execute(
            "INSERT INTO provider_circuit_state (provider, current_state, failure_count, blocked_until, last_error, last_error_at, updated_at) VALUES (?, ?, 0, NULL, NULL, NULL, ?)",
            (PROVIDER, circuit, _now_plus()),
        )
    if metrics:
        for key, value in metrics.items():
            conn.execute(
                "INSERT INTO provider_budget_metrics (provider, metric, value) VALUES (?, ?, ?)",
                (PROVIDER, key, value),
            )
    conn.commit()
    conn.close()


def _quota_state(*, daily_limit=7500, daily_remaining=7000, operational_used=0, quota_reset_days=1, operational_reset_days=1):
    return {
        "provider": PROVIDER,
        "daily_limit": daily_limit,
        "daily_remaining": daily_remaining,
        "minute_limit": 10,
        "minute_remaining": 10,
        "captured_at": _now_plus(),
        "quota_reset_at": _now_plus(days=quota_reset_days),
        "requests_used": operational_used,
        "operational_cap": OPERATIONAL_CAP,
        "operational_used": operational_used,
        "operational_reset_at": _now_plus(days=operational_reset_days),
    }


def _make_research_db(path, *, with_tables=False, jobs=None, progress=None, failures=0):
    conn = sqlite3.connect(path)
    if with_tables:
        conn.execute("CREATE TABLE football_backfill_jobs (job_id TEXT PRIMARY KEY, provider TEXT NOT NULL, competition_id TEXT NOT NULL, competition_name TEXT NOT NULL, division_code TEXT NOT NULL, season INTEGER NOT NULL, season_code TEXT NOT NULL, provider_league_id INTEGER NOT NULL, source_version TEXT NOT NULL, required_statistics_json TEXT NOT NULL, max_request_cap INTEGER NOT NULL, max_retries_per_fixture INTEGER NOT NULL, projected_total_requests INTEGER NOT NULL, target_fixture_count INTEGER NOT NULL, start_timestamp TEXT NOT NULL, completion_state TEXT NOT NULL, updated_at TEXT NOT NULL)")
        conn.execute("CREATE TABLE football_backfill_fixture_progress (job_id TEXT NOT NULL, canonical_fixture_id TEXT NOT NULL, provider_fixture_id TEXT NOT NULL, match_date TEXT, home_team TEXT NOT NULL, away_team TEXT NOT NULL, state TEXT NOT NULL, failure_kind TEXT, missing_fields_json TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0, event_id TEXT, updated_at TEXT NOT NULL, PRIMARY KEY (job_id, canonical_fixture_id))")
        conn.execute("CREATE TABLE football_backfill_failures (failure_id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, canonical_fixture_id TEXT NOT NULL, provider_fixture_id TEXT NOT NULL, failure_kind TEXT NOT NULL, message TEXT, recorded_at TEXT NOT NULL)")
    if jobs:
        for job in jobs:
            conn.execute(
                "INSERT INTO football_backfill_jobs (job_id, provider, competition_id, competition_name, division_code, season, season_code, provider_league_id, source_version, required_statistics_json, max_request_cap, max_retries_per_fixture, projected_total_requests, target_fixture_count, start_timestamp, completion_state, updated_at) VALUES (?, 'api-football', 'FRA1', 'Ligue 1', 'F1', 2025, '2526', 61, 'api_football_history_backfill_v1', '[]', 320, 3, 309, 308, '2026-09-28T00:00:00+00:00', ?, '2026-09-28T00:00:00+00:00')",
                (job["job_id"], job.get("state", "IN_PROGRESS")),
            )
    if progress:
        for row in progress:
            conn.execute(
                "INSERT INTO football_backfill_fixture_progress (job_id, canonical_fixture_id, provider_fixture_id, match_date, home_team, away_team, state, failure_kind, missing_fields_json, attempt_count, event_id, updated_at) VALUES (?, ?, 'p', NULL, 'A', 'B', ?, NULL, '[]', 0, NULL, '2026-09-28T00:00:00+00:00')",
                (row["job_id"], row["canonical_fixture_id"], row["state"]),
            )
    if failures:
        for index in range(failures):
            conn.execute(
                "INSERT INTO football_backfill_failures (job_id, canonical_fixture_id, provider_fixture_id, failure_kind, message, recorded_at) VALUES (?, 'f', 'p', 'TRANSIENT', NULL, '2026-09-28T00:00:00+00:00')",
                (EXPECTED_JOB_ID,),
            )
    conn.commit()
    conn.close()


# --------------------------------------------------------------- database safety
def test_readonly_connect_uses_query_only(tmp_path):
    db = tmp_path / "b.sqlite"
    _make_budget_db(db, state=_quota_state())
    conn = _readonly_connect(db)
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] in ("wal", "delete", "truncate", "persist", "memory")
    conn.close()


def test_diagnostic_does_not_create_tables(tmp_path):
    db = tmp_path / "b.sqlite"
    # No tables at all.
    sqlite3.connect(db).close()
    result = run_fra1_preexec_readonly(db, tmp_path / "r.sqlite")
    assert result["diagnostic_state"] == "UNAVAILABLE"
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert tables == set()


def test_diagnostic_makes_no_writes(tmp_path):
    db = tmp_path / "b.sqlite"
    state = _quota_state(daily_remaining=6000, operational_used=10)
    _make_budget_db(db, state=state)
    before = sqlite3.connect(db)
    before.execute("PRAGMA query_only = OFF")
    before_rows = before.execute("SELECT COUNT(*) FROM provider_quota_state").fetchone()[0]
    before.close()

    run_fra1_preexec_readonly(db, tmp_path / "r.sqlite")

    after = sqlite3.connect(db)
    after_rows = after.execute("SELECT COUNT(*) FROM provider_quota_state").fetchone()[0]
    state_json = after.execute("SELECT state_json FROM provider_quota_state WHERE provider = ?", (PROVIDER,)).fetchone()[0]
    after.close()
    assert after_rows == before_rows == 1
    assert json.loads(state_json)["operational_used"] == 10


def test_diagnostic_reads_wal_backed_db(tmp_path):
    db = tmp_path / "b.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("CREATE TABLE provider_quota_state (provider TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated_at TEXT NOT NULL)")
    conn.commit()
    conn.close()
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO provider_quota_state VALUES (?, ?, ?)", (PROVIDER, json.dumps(_quota_state(daily_remaining=5000)), _now_plus()))
    conn.commit()
    conn.close()

    result = run_fra1_preexec_readonly(db, tmp_path / "r.sqlite")
    assert result["diagnostic_state"] == "OK"
    assert result["budget"]["daily_remaining"] == 5000


def test_diagnostic_fails_closed_on_missing_db(tmp_path):
    result = run_fra1_preexec_readonly(tmp_path / "nope.sqlite", tmp_path / "r.sqlite")
    assert result["diagnostic_state"] == "UNAVAILABLE"
    assert result["reason"] == "BUDGET_DB_UNAVAILABLE"
    assert result["side_effects"] == {"provider_calls": 0, "writes": 0}


def test_diagnostic_fails_closed_on_garbage_file(tmp_path):
    bad = tmp_path / "bad.sqlite"
    bad.write_text("this is not a sqlite database")
    result = run_fra1_preexec_readonly(bad, tmp_path / "r.sqlite")
    assert result["diagnostic_state"] == "UNAVAILABLE"


# --------------------------------------------------------------- provider safety
def test_diagnostic_does_not_import_provider_clients():
    code = (
        "import sys\n"
        "from sports_ai.diagnostics.football_preexec_readonly import run_fra1_preexec_readonly\n"
        "bad = [m for m in ('sports_ai.dashboard.fixture_provider', 'sports_ai.market_data.the_odds_api', 'sports_ai.dashboard.provider_budget') if m in sys.modules]\n"
        "print('PROVIDER_MODULES_IMPORTED=' + (','.join(bad) if bad else 'none'))\n"
    )
    out = subprocess.check_output([sys.executable, "-c", code], text=True, cwd=None)
    assert "PROVIDER_MODULES_IMPORTED=none" in out


# ----------------------------------------------------------------- quota cases
def test_quota_healthy_sufficient_headroom(tmp_path):
    db = tmp_path / "b.sqlite"
    _make_budget_db(db, state=_quota_state(daily_limit=4500, daily_remaining=4400, operational_used=100))
    result = run_fra1_preexec_readonly(db, tmp_path / "r.sqlite")
    assert result["diagnostic_state"] == "OK"
    assert result["budget"]["quota_state"] == "HEALTHY"
    assert result["budget"]["effective_limit"] == 4500
    assert result["budget"]["effective_remaining"] == 4400
    assert result["budget"]["healthy_floor"] == 1800
    assert result["budget"]["minimum_safe_pre_run"] == 2109
    assert result["budget"]["post_plan_remaining"] == 4091
    assert result["budget"]["remains_healthy_after_plan"] is True
    assert result["low_priority_eligible"] is True


def test_quota_exactly_2109_remaining_passes(tmp_path):
    db = tmp_path / "b.sqlite"
    _make_budget_db(db, state=_quota_state(daily_limit=4500, daily_remaining=2109, operational_used=0))
    result = run_fra1_preexec_readonly(db, tmp_path / "r.sqlite")
    assert result["budget"]["minimum_safe_pre_run"] == 2109
    assert result["budget"]["post_plan_remaining"] == 1800
    assert result["budget"]["remains_healthy_after_plan"] is True


def test_quota_2108_remaining_rejects(tmp_path):
    db = tmp_path / "b.sqlite"
    _make_budget_db(db, state=_quota_state(daily_limit=4500, daily_remaining=2108, operational_used=0))
    result = run_fra1_preexec_readonly(db, tmp_path / "r.sqlite")
    assert result["budget"]["minimum_safe_pre_run"] == 2109
    assert result["budget"]["post_plan_remaining"] == 1799
    assert result["budget"]["remains_healthy_after_plan"] is False


def test_quota_stale_window_rejects(tmp_path):
    db = tmp_path / "b.sqlite"
    _make_budget_db(db, state=_quota_state(quota_reset_days=-1))
    result = run_fra1_preexec_readonly(db, tmp_path / "r.sqlite")
    assert result["diagnostic_state"] == "UNAVAILABLE"
    assert result["reason"] == "QUOTA_STATE_STALE"
    assert result["budget"]["current_window"] is False
    assert result["budget"]["quota_state"] == "UNKNOWN"


def test_quota_operational_reset_handled_logically_without_write(tmp_path):
    db = tmp_path / "b.sqlite"
    state = _quota_state(daily_remaining=6000, operational_used=100, operational_reset_days=-1)
    _make_budget_db(db, state=state)
    result = run_fra1_preexec_readonly(db, tmp_path / "r.sqlite")
    assert result["budget"]["operational_used_raw"] == 100
    assert result["budget"]["operational_used_logical"] == 0
    assert result["budget"]["operational_remaining"] == OPERATIONAL_CAP
    # No write: raw value still persisted.
    conn = sqlite3.connect(db)
    persisted = json.loads(conn.execute("SELECT state_json FROM provider_quota_state WHERE provider = ?", (PROVIDER,)).fetchone()[0])
    conn.close()
    assert persisted["operational_used"] == 100


def test_quota_provider_limit_below_4500(tmp_path):
    db = tmp_path / "b.sqlite"
    _make_budget_db(db, state=_quota_state(daily_limit=3000, daily_remaining=2900, operational_used=0))
    result = run_fra1_preexec_readonly(db, tmp_path / "r.sqlite")
    assert result["budget"]["effective_limit"] == 3000
    assert result["budget"]["effective_remaining"] == 2900
    assert result["budget"]["healthy_floor"] == 1200
    assert result["budget"]["minimum_safe_pre_run"] == 1509
    assert result["budget"]["quota_state"] == "HEALTHY"


def test_quota_circuit_blocked(tmp_path):
    db = tmp_path / "b.sqlite"
    _make_budget_db(db, state=_quota_state(daily_remaining=6000), circuit="QUOTA_EXHAUSTED")
    result = run_fra1_preexec_readonly(db, tmp_path / "r.sqlite")
    assert result["circuit"]["current_state"] == "QUOTA_EXHAUSTED"
    assert result["low_priority_eligible"] is False


def test_quota_missing_row(tmp_path):
    db = tmp_path / "b.sqlite"
    _make_budget_db(db)  # no quota row
    result = run_fra1_preexec_readonly(db, tmp_path / "r.sqlite")
    assert result["diagnostic_state"] == "UNAVAILABLE"
    assert result["reason"] == "QUOTA_STATE_MISSING"
    assert result["budget"]["row_exists"] is False


# ---------------------------------------------------------------- checkpoint
def test_checkpoint_tables_absent(tmp_path):
    research = tmp_path / "r.sqlite"
    _make_research_db(research, with_tables=False)
    result = run_fra1_preexec_readonly(tmp_path / "b.sqlite", research)
    checkpoint = result["checkpoint"]
    assert checkpoint["tables_exist"] is False
    assert checkpoint["expected_job_exists"] is False


def test_checkpoint_job_absent(tmp_path):
    research = tmp_path / "r.sqlite"
    _make_research_db(research, with_tables=True, jobs=[])
    result = run_fra1_preexec_readonly(tmp_path / "b.sqlite", research)
    assert result["checkpoint"]["tables_exist"] is True
    assert result["checkpoint"]["expected_job_exists"] is False


def test_checkpoint_exact_job_exists_with_partial_progress(tmp_path):
    research = tmp_path / "r.sqlite"
    _make_research_db(
        research,
        with_tables=True,
        jobs=[{"job_id": EXPECTED_JOB_ID, "state": "PARTIAL"}],
        progress=[
            {"job_id": EXPECTED_JOB_ID, "canonical_fixture_id": "c1", "state": "COMPLETE"},
            {"job_id": EXPECTED_JOB_ID, "canonical_fixture_id": "c2", "state": "RETRYABLE_FAILED"},
            {"job_id": EXPECTED_JOB_ID, "canonical_fixture_id": "c3", "state": "TERMINAL_FAILED"},
            {"job_id": EXPECTED_JOB_ID, "canonical_fixture_id": "c4", "state": "PENDING"},
        ],
        failures=2,
    )
    result = run_fra1_preexec_readonly(tmp_path / "b.sqlite", research)
    checkpoint = result["checkpoint"]
    assert checkpoint["expected_job_exists"] is True
    assert checkpoint["job_state"] == "PARTIAL"
    assert checkpoint["complete"] == 1
    assert checkpoint["retryable_failed"] == 1
    assert checkpoint["terminal_failed"] == 1
    assert checkpoint["pending"] == 1
    assert checkpoint["failure_rows"] == 2


def test_checkpoint_conflicting_alternate_fra1_job(tmp_path):
    research = tmp_path / "r.sqlite"
    _make_research_db(
        research,
        with_tables=True,
        jobs=[{"job_id": "deadbeefdeadbeefdeadbeef", "state": "IN_PROGRESS"}],
    )
    result = run_fra1_preexec_readonly(tmp_path / "b.sqlite", research)
    checkpoint = result["checkpoint"]
    assert checkpoint["expected_job_exists"] is False
    assert len(checkpoint["conflicting_jobs"]) == 1
    assert len(checkpoint["conflicting_unresolved"]) == 1


def test_checkpoint_completed_job(tmp_path):
    research = tmp_path / "r.sqlite"
    _make_research_db(
        research,
        with_tables=True,
        jobs=[{"job_id": EXPECTED_JOB_ID, "state": "COMPLETE"}],
    )
    result = run_fra1_preexec_readonly(tmp_path / "b.sqlite", research)
    assert result["checkpoint"]["expected_job_exists"] is True
    assert result["checkpoint"]["job_state"] == "COMPLETE"
