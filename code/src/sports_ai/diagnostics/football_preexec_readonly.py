"""Read-only FRA1 pre-execution readiness diagnostic.

Emits a single sanitized structured log record during normal application
startup so operational readiness for the FRA1 2025 historical backfill can be
assessed without waking production, without making any provider request, and
without mutating any production database.

Safety invariants enforced here:

* every SQLite connection is opened with ``mode=ro`` (read-only URI) and
  immediately sets ``PRAGMA query_only = ON``;
* only SELECT statements are issued (no schema creation, no migrations, no
  UPDATE/INSERT/DELETE, no ``wal_checkpoint``, no journal-mode change);
* no provider client is instantiated or called;
* any failure fails closed (``diagnostic_state=UNAVAILABLE``) and never raises
  into the application startup path.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DIAGNOSTIC_PREFIX = "FRA1_PREEXEC_READONLY"
DIAGNOSTIC_VERSION = 1

PROVIDER = "api-football"
COMPETITION = "FRA1"
SEASON = 2025
EXPECTED_JOB_ID = "0b52d0d1eb9467b02eba6048"
OPERATIONAL_CAP = 4500
PLANNED_REQUESTS = 309
HEALTHY_RATIO = 0.40

_BUDGET_TABLES = ("provider_quota_state", "provider_budget_metrics", "provider_circuit_state")
_BACKFILL_TABLES = (
    "football_backfill_jobs",
    "football_backfill_fixture_progress",
    "football_backfill_failures",
)
_CIRCUIT_CLOSED = "CLOSED"


def _readonly_connect(path: str | Path) -> sqlite3.Connection:
    uri = f"file:{Path(path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row["name"]) for row in rows}


def _parse_ts(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _read_quota_state(path: str | Path) -> dict[str, Any] | None:
    with _readonly_connect(path) as conn:
        if "provider_quota_state" not in _table_names(conn):
            return None
        row = conn.execute(
            "SELECT state_json, updated_at FROM provider_quota_state WHERE provider = ?",
            (PROVIDER,),
        ).fetchone()
        if row is None:
            return None
        try:
            state = json.loads(str(row["state_json"]))
        except (TypeError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        state["_updated_at"] = row["updated_at"]
        return state


def _read_circuit(path: str | Path) -> dict[str, Any] | None:
    with _readonly_connect(path) as conn:
        if "provider_circuit_state" not in _table_names(conn):
            return None
        row = conn.execute(
            "SELECT current_state, failure_count, blocked_until, last_error_at "
            "FROM provider_circuit_state WHERE provider = ?",
            (PROVIDER,),
        ).fetchone()
        if row is None:
            return None
        return {
            "current_state": row["current_state"],
            "failure_count": int(row["failure_count"] or 0),
            "blocked_until": row["blocked_until"],
            "last_error_at": row["last_error_at"],
        }


def _read_budget_metrics(path: str | Path) -> dict[str, int]:
    with _readonly_connect(path) as conn:
        if "provider_budget_metrics" not in _table_names(conn):
            return {}
        rows = conn.execute(
            "SELECT metric, value FROM provider_budget_metrics WHERE provider = ?",
            (PROVIDER,),
        ).fetchall()
        return {str(row["metric"]): int(row["value"]) for row in rows}


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _quota_state_label(ratio: float) -> str:
    if ratio >= HEALTHY_RATIO:
        return "HEALTHY"
    if ratio >= 0.20:
        return "CONSERVE"
    if ratio >= 0.10:
        return "LOW"
    if ratio > 0:
        return "CRITICAL"
    return "EXHAUSTED"


def _read_checkpoint(path: str | Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "tables_exist": False,
        "expected_job_exists": False,
        "matching_fra1_2025_jobs": [],
        "complete": 0,
        "retryable_failed": 0,
        "terminal_failed": 0,
        "pending": 0,
        "failure_rows": 0,
        "job_state": None,
        "target_fixture_count": None,
    }
    with _readonly_connect(path) as conn:
        tables = _table_names(conn)
        present = [t for t in _BACKFILL_TABLES if t in tables]
        if not present:
            return result
        result["tables_exist"] = True

        if "football_backfill_jobs" in tables:
            jobs = conn.execute(
                "SELECT job_id, competition_id, season, provider_league_id, completion_state, "
                "target_fixture_count, projected_total_requests, max_request_cap, "
                "max_retries_per_fixture, start_timestamp, updated_at "
                "FROM football_backfill_jobs WHERE competition_id = ? AND season = ?",
                (COMPETITION, SEASON),
            ).fetchall()
            for job in jobs:
                result["matching_fra1_2025_jobs"].append(
                    {
                        "job_id": str(job["job_id"]),
                        "completion_state": job["completion_state"],
                        "provider_league_id": job["provider_league_id"],
                        "target_fixture_count": job["target_fixture_count"],
                    }
                )
                if str(job["job_id"]) == EXPECTED_JOB_ID:
                    result["expected_job_exists"] = True
                    result["job_state"] = job["completion_state"]
                    result["target_fixture_count"] = job["target_fixture_count"]

        if "football_backfill_fixture_progress" in tables and result["expected_job_exists"]:
            progress = conn.execute(
                "SELECT state, COUNT(*) AS n FROM football_backfill_fixture_progress "
                "WHERE job_id = ? GROUP BY state",
                (EXPECTED_JOB_ID,),
            ).fetchall()
            for row in progress:
                state = str(row["state"])
                count = int(row["n"])
                if state == "COMPLETE":
                    result["complete"] = count
                elif state == "RETRYABLE_FAILED":
                    result["retryable_failed"] = count
                elif state == "TERMINAL_FAILED":
                    result["terminal_failed"] = count
                elif state == "PENDING":
                    result["pending"] = count

        if "football_backfill_failures" in tables and result["expected_job_exists"]:
            result["failure_rows"] = int(
                conn.execute(
                    "SELECT COUNT(*) FROM football_backfill_failures WHERE job_id = ?",
                    (EXPECTED_JOB_ID,),
                ).fetchone()[0]
            )

    return result


def _checkpoint_section(checkpoint: dict[str, Any]) -> dict[str, Any]:
    conflicting_jobs = [
        j for j in checkpoint.get("matching_fra1_2025_jobs", []) if j.get("job_id") != EXPECTED_JOB_ID
    ]
    conflicting_unresolved = [
        j for j in conflicting_jobs if j.get("completion_state") in ("IN_PROGRESS", "PENDING", "BLOCKED", "PARTIAL")
    ]
    return {
        "tables_exist": checkpoint.get("tables_exist"),
        "expected_job_id": EXPECTED_JOB_ID,
        "expected_job_exists": checkpoint.get("expected_job_exists"),
        "matching_fra1_2025_jobs": checkpoint.get("matching_fra1_2025_jobs"),
        "conflicting_jobs": conflicting_jobs,
        "conflicting_unresolved": conflicting_unresolved,
        "complete": checkpoint.get("complete", 0),
        "retryable_failed": checkpoint.get("retryable_failed", 0),
        "terminal_failed": checkpoint.get("terminal_failed", 0),
        "pending": checkpoint.get("pending", 0),
        "failure_rows": checkpoint.get("failure_rows", 0),
        "job_state": checkpoint.get("job_state"),
        "target_fixture_count": checkpoint.get("target_fixture_count"),
    }


def run_fra1_preexec_readonly(budget_db_path: str | Path, research_db_path: str | Path) -> dict[str, Any]:
    """Perform a strictly read-only FRA1 pre-execution diagnostic.

    Returns a sanitized dictionary ready for structured logging. Any failure
    is caught and reported as ``diagnostic_state=UNAVAILABLE`` with a reason.
    """
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat(timespec="seconds")

    payload: dict[str, Any] = {
        "version": DIAGNOSTIC_VERSION,
        "diagnostic_state": "OK",
        "reason": None,
        "provider": PROVIDER,
        "competition": COMPETITION,
        "season": SEASON,
        "evaluated_at_utc": now_iso,
        "planned_requests": PLANNED_REQUESTS,
        "operational_cap": OPERATIONAL_CAP,
        "side_effects": {"provider_calls": 0, "writes": 0},
    }

    def _read_checkpoint_safe() -> dict[str, Any]:
        try:
            return _read_checkpoint(research_db_path)
        except Exception as exc:  # noqa: BLE001
            return {
                "tables_exist": False,
                "expected_job_exists": False,
                "matching_fra1_2025_jobs": [],
                "read_error": type(exc).__name__,
            }

    try:
        budget = _read_quota_state(budget_db_path)
    except Exception as exc:  # noqa: BLE001
        payload["diagnostic_state"] = "UNAVAILABLE"
        payload["reason"] = "BUDGET_DB_UNAVAILABLE"
        payload["error_kind"] = type(exc).__name__
        payload["checkpoint"] = _checkpoint_section(_read_checkpoint_safe())
        return payload

    if budget is None:
        payload["diagnostic_state"] = "UNAVAILABLE"
        payload["reason"] = "QUOTA_STATE_MISSING"
        payload["budget"] = {"row_exists": False}
        payload["checkpoint"] = _checkpoint_section(_read_checkpoint_safe())
        return payload

    updated_at = budget.get("_updated_at")
    captured_at = budget.get("captured_at")
    quota_reset_at = budget.get("quota_reset_at")
    operational_reset_at = budget.get("operational_reset_at")

    quota_reset_dt = _parse_ts(quota_reset_at)
    current_window = quota_reset_dt is not None and quota_reset_dt > now_dt

    provider_daily_limit = _as_int(budget.get("daily_limit"))
    provider_daily_remaining = _as_int(budget.get("daily_remaining"))

    operational_used_raw = _as_int(budget.get("operational_used")) or 0
    operational_cap = _as_int(budget.get("operational_cap")) or OPERATIONAL_CAP

    operational_reset_dt = _parse_ts(operational_reset_at)
    operational_reset_passed = operational_reset_dt is not None and operational_reset_dt <= now_dt
    operational_used_logical = 0 if operational_reset_passed else operational_used_raw
    operational_remaining = max(0, operational_cap - operational_used_logical)

    provider_allowance_known = provider_daily_limit is not None or provider_daily_remaining is not None

    budget_section: dict[str, Any] = {
        "row_exists": True,
        "updated_at": updated_at,
        "captured_at": captured_at,
        "quota_reset_at": quota_reset_at,
        "current_window": current_window,
        "daily_limit": provider_daily_limit,
        "daily_remaining": provider_daily_remaining,
        "operational_cap": operational_cap,
        "operational_used_raw": operational_used_raw,
        "operational_used_logical": operational_used_logical,
        "operational_remaining": operational_remaining,
        "operational_reset_at": operational_reset_at,
        "requests_used": _as_int(budget.get("requests_used")),
        "last_request_at": budget.get("last_request_at"),
        "last_success_at": budget.get("last_success_at"),
    }

    effective_limit: int | None = None
    effective_remaining: int | None = None
    quota_state = "UNKNOWN"
    healthy_floor: int | None = None
    minimum_safe_pre_run: int | None = None
    post_plan_remaining: int | None = None
    remains_healthy_after_plan: bool | None = None

    if current_window and provider_allowance_known:
        if provider_daily_limit is not None and provider_daily_remaining is not None and provider_daily_limit > 0:
            effective_limit = min(provider_daily_limit, operational_cap)
            effective_remaining = min(provider_daily_remaining, operational_remaining)
        else:
            # Provider allowance partially known: fall back to operational side
            # only if the provider side is truly unavailable is that UNKNOWN.
            effective_limit = None
            effective_remaining = None
        if effective_limit is not None and effective_remaining is not None:
            ratio = effective_remaining / float(effective_limit)
            quota_state = _quota_state_label(ratio)
            healthy_floor = math.ceil(effective_limit * HEALTHY_RATIO)
            minimum_safe_pre_run = healthy_floor + PLANNED_REQUESTS
            post_plan_remaining = effective_remaining - PLANNED_REQUESTS
            remains_healthy_after_plan = post_plan_remaining >= healthy_floor
    else:
        if not current_window:
            budget_section["current_window"] = False
            payload["reason"] = "QUOTA_STATE_STALE"
            payload["diagnostic_state"] = "UNAVAILABLE"

    budget_section["effective_limit"] = effective_limit
    budget_section["effective_remaining"] = effective_remaining
    budget_section["quota_state"] = quota_state
    budget_section["healthy_floor"] = healthy_floor
    budget_section["minimum_safe_pre_run"] = minimum_safe_pre_run
    budget_section["post_plan_remaining"] = post_plan_remaining
    budget_section["remains_healthy_after_plan"] = remains_healthy_after_plan

    circuit: dict[str, Any]
    try:
        circuit_row = _read_circuit(budget_db_path)
        circuit = circuit_row if circuit_row is not None else {"current_state": _CIRCUIT_CLOSED, "failure_count": 0, "blocked_until": None, "last_error_at": None}
    except Exception as exc:  # noqa: BLE001
        circuit = {"current_state": "UNKNOWN", "failure_count": None, "blocked_until": None, "last_error_at": None, "read_error": type(exc).__name__}

    try:
        metrics = _read_budget_metrics(budget_db_path)
    except Exception:  # noqa: BLE001
        metrics = {}

    circuit_blocking = circuit.get("current_state") != _CIRCUIT_CLOSED
    low_priority_eligible = (quota_state == "HEALTHY") and not circuit_blocking

    checkpoint: dict[str, Any]
    try:
        checkpoint = _read_checkpoint(research_db_path)
    except Exception as exc:  # noqa: BLE001
        checkpoint = {
            "tables_exist": False,
            "expected_job_exists": False,
            "matching_fra1_2025_jobs": [],
            "read_error": type(exc).__name__,
        }

    payload["budget"] = budget_section
    payload["circuit"] = circuit
    payload["budget_metrics"] = metrics
    payload["low_priority_eligible"] = low_priority_eligible
    payload["checkpoint"] = _checkpoint_section(checkpoint)
    payload["side_effects"] = {"provider_calls": 0, "writes": 0}

    return payload


def log_fra1_preexec_readonly(budget_db_path: str | Path, research_db_path: str | Path) -> None:
    """Emit one sanitized ``FRA1_PREEXEC_READONLY`` structured log record.

    Never raises: any unexpected failure is logged as ``UNAVAILABLE`` so the
    application startup continues normally.
    """
    try:
        payload = run_fra1_preexec_readonly(budget_db_path, research_db_path)
    except Exception as exc:  # noqa: BLE001
        payload = {
            "version": DIAGNOSTIC_VERSION,
            "diagnostic_state": "UNAVAILABLE",
            "reason": "UNEXPECTED",
            "error_kind": type(exc).__name__,
            "provider": PROVIDER,
            "competition": COMPETITION,
            "season": SEASON,
            "side_effects": {"provider_calls": 0, "writes": 0},
        }
    try:
        line = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        line = json.dumps({"diagnostic_state": "UNAVAILABLE", "reason": "SERIALIZATION"})
    logger.info("%s %s", DIAGNOSTIC_PREFIX, line)
