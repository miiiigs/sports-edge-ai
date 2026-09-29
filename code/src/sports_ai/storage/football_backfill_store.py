"""Persistent checkpoint/progress store for API-Football historical backfill.

This module gives the backfill orchestrator a durable, resume-safe record of a
single league-season backfill job:

* ``football_backfill_jobs``          -- one row per job (identity + plan).
* ``football_backfill_fixture_progress`` -- one row per canonical target fixture.
* ``football_backfill_failures``      -- an append-only failure ledger.

Fixture progress is keyed by the *canonical* fixture id (competition + home +
away + kickoff); the provider fixture id is retained as provenance. A fixture is
never marked COMPLETE until its GAP-ready event has been persisted; a partially
retrieved fixture stays PENDING / RETRYABLE_FAILED / TERMINAL_FAILED.

The normalized events themselves are written into the existing ``football_events``
research table idempotently (``INSERT OR IGNORE`` on ``event_id``), so reprocessing
a provider fixture cannot create a duplicate historical event.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Sequence

from ..data_sources.football_history import HistoricalEventRow

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS football_backfill_jobs (
        job_id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        competition_id TEXT NOT NULL,
        competition_name TEXT NOT NULL,
        division_code TEXT NOT NULL,
        season INTEGER NOT NULL,
        season_code TEXT NOT NULL,
        provider_league_id INTEGER NOT NULL,
        source_version TEXT NOT NULL,
        required_statistics_json TEXT NOT NULL,
        max_request_cap INTEGER NOT NULL,
        max_retries_per_fixture INTEGER NOT NULL,
        projected_total_requests INTEGER NOT NULL,
        target_fixture_count INTEGER NOT NULL,
        start_timestamp TEXT NOT NULL,
        completion_state TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        requests_consumed INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS football_backfill_fixture_progress (
        job_id TEXT NOT NULL,
        canonical_fixture_id TEXT NOT NULL,
        provider_fixture_id TEXT NOT NULL,
        match_date TEXT,
        home_team TEXT NOT NULL,
        away_team TEXT NOT NULL,
        state TEXT NOT NULL,
        failure_kind TEXT,
        missing_fields_json TEXT NOT NULL,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        event_id TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (job_id, canonical_fixture_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS football_backfill_failures (
        failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL,
        canonical_fixture_id TEXT NOT NULL,
        provider_fixture_id TEXT NOT NULL,
        failure_kind TEXT NOT NULL,
        message TEXT,
        recorded_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_backfill_fixture_progress_job_state
    ON football_backfill_fixture_progress(job_id, state)
    """,
)


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # Autocommit: every checkpoint write (job plan, lifetime request counter,
    # fixture progress, failures) is durable the instant it is issued. Without
    # this a process exit/restart would roll back uncommitted progress and the
    # lifetime request counter, silently losing resume state and under-counting
    # billable provider spend. Each store statement is independently valid, so
    # per-statement durability is safe; the orchestrator always re-reads state
    # before acting.
    conn.isolation_level = None
    # NORMAL keeps committed writes durable across application/process crashes
    # (the threat model here) while avoiding an fsync per checkpoint write. Full
    # power-loss durability is not required for a resumable request checkpoint.
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    with conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
        _ensure_job_columns(conn)


def _ensure_job_columns(conn: sqlite3.Connection) -> None:
    """Additively add columns introduced after the first release.

    ``CREATE TABLE IF NOT EXISTS`` cannot add columns to an existing table, so a
    pre-existing checkpoint database is upgraded in place. Existing rows default
    ``requests_consumed`` to 0, which is the correct value for a job created
    before lifetime request accounting existed. No data is rewritten or dropped.
    """
    existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(football_backfill_jobs)")}
    if "requests_consumed" not in existing:
        conn.execute(
            "ALTER TABLE football_backfill_jobs ADD COLUMN requests_consumed INTEGER NOT NULL DEFAULT 0"
        )


def create_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    provider: str,
    competition_id: str,
    competition_name: str,
    division_code: str,
    season: int,
    season_code: str,
    provider_league_id: int,
    source_version: str,
    required_statistics: Sequence[str],
    max_request_cap: int,
    max_retries_per_fixture: int,
    projected_total_requests: int,
    target_fixture_count: int,
    start_timestamp: str,
        completion_state: str,
) -> None:
    # Create the job once. On an existing job this only refreshes the plan; it
    # deliberately does NOT touch ``max_request_cap`` (the pinned lifetime cap)
    # or ``updated_at`` (the concurrency-claim lease marker), which must survive
    # every resume unchanged.
    conn.execute(
        """
        INSERT INTO football_backfill_jobs (
            job_id, provider, competition_id, competition_name, division_code,
            season, season_code, provider_league_id, source_version,
            required_statistics_json, max_request_cap, max_retries_per_fixture,
            projected_total_requests, target_fixture_count, start_timestamp,
            completion_state, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
            projected_total_requests = excluded.projected_total_requests,
            target_fixture_count = excluded.target_fixture_count
        """,
        (
            job_id,
            provider,
            competition_id,
            competition_name,
            division_code,
            int(season),
            season_code,
            int(provider_league_id),
            source_version,
            json.dumps(list(required_statistics), sort_keys=True),
            int(max_request_cap),
            int(max_retries_per_fixture),
            int(projected_total_requests),
            int(target_fixture_count),
            start_timestamp,
            completion_state,
            start_timestamp,
        ),
    )


def load_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM football_backfill_jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    payload = dict(row)
    payload["required_statistics"] = json.loads(str(payload.pop("required_statistics_json")))
    return payload


def update_job_state(conn: sqlite3.Connection, *, job_id: str, completion_state: str, updated_at: str) -> None:
    conn.execute(
        """
        UPDATE football_backfill_jobs
        SET completion_state = ?, updated_at = ?
        WHERE job_id = ?
        """,
        (completion_state, updated_at, job_id),
    )


def update_job_plan(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    projected_total_requests: int,
    target_fixture_count: int,
    updated_at: str,
) -> None:
    """Persist the authoritative plan once the fixture list has been read."""
    conn.execute(
        """
        UPDATE football_backfill_jobs
        SET projected_total_requests = ?, target_fixture_count = ?, updated_at = ?
        WHERE job_id = ?
        """,
        (int(projected_total_requests), int(target_fixture_count), updated_at, job_id),
    )


def increment_job_requests(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    count: int,
    updated_at: str,
) -> None:
    """Add ``count`` provider request attempts to the lifetime job counter.

    This makes ``max_request_cap`` a lifetime ceiling for the logical backfill:
    the counter is persisted on the job row and preserved across every resume and
    quota window, so split execution cannot silently multiply the allowed cost.
    """
    conn.execute(
        """
        UPDATE football_backfill_jobs
        SET requests_consumed = COALESCE(requests_consumed, 0) + ?, updated_at = ?
        WHERE job_id = ?
        """,
        (int(count), updated_at, job_id),
    )


def try_claim_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    now_iso: str,
    stale_before_iso: str,
) -> bool:
    """Atomically claim exclusive execution of one deterministic backfill job.

    The job is claimed only when it is not already ``IN_PROGRESS`` or when its
    last-progress marker (``updated_at``) is older than ``stale_before_iso``. The
    conditional ``UPDATE`` is a single SQL statement, so SQLite's write
    serialization guarantees exactly one concurrent executor wins. Returns
    ``True`` for the winner and ``False`` for a second executor, which must then
    exit without any provider traffic.

    Using ``IN_PROGRESS`` + ``updated_at`` as a self-expiring lease means no extra
    columns are needed, a completed/paused run releases the job immediately (its
    terminal-for-now state is not ``IN_PROGRESS``), and a crashed run is
    reclaimable after the staleness window. The orchestrator refreshes
    ``updated_at`` on every claim and request, so a live run stays claimed.
    """
    cursor = conn.execute(
        """
        UPDATE football_backfill_jobs
        SET completion_state = 'IN_PROGRESS', updated_at = ?
        WHERE job_id = ?
          AND (completion_state <> 'IN_PROGRESS' OR updated_at <= ?)
        """,
        (now_iso, job_id, stale_before_iso),
    )
    return cursor.rowcount == 1


def upsert_fixture_progress(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    canonical_fixture_id: str,
    provider_fixture_id: str,
    match_date: str | None,
    home_team: str,
    away_team: str,
    state: str,
    failure_kind: str | None,
    missing_fields: Sequence[str],
    attempt_count: int,
    event_id: str | None,
    updated_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO football_backfill_fixture_progress (
            job_id, canonical_fixture_id, provider_fixture_id, match_date,
            home_team, away_team, state, failure_kind, missing_fields_json,
            attempt_count, event_id, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id, canonical_fixture_id) DO UPDATE SET
            provider_fixture_id = excluded.provider_fixture_id,
            match_date = excluded.match_date,
            home_team = excluded.home_team,
            away_team = excluded.away_team,
            state = excluded.state,
            failure_kind = excluded.failure_kind,
            missing_fields_json = excluded.missing_fields_json,
            attempt_count = excluded.attempt_count,
            event_id = excluded.event_id,
            updated_at = excluded.updated_at
        """,
        (
            job_id,
            canonical_fixture_id,
            provider_fixture_id,
            match_date,
            home_team,
            away_team,
            state,
            failure_kind,
            json.dumps(list(missing_fields), sort_keys=True),
            int(attempt_count),
            event_id,
            updated_at,
        ),
    )


def fetch_fixture_progress(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT canonical_fixture_id, provider_fixture_id, match_date, home_team,
               away_team, state, failure_kind, missing_fields_json, attempt_count,
               event_id, updated_at
        FROM football_backfill_fixture_progress
        WHERE job_id = ?
        ORDER BY match_date, canonical_fixture_id
        """,
        (job_id,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row)
        payload["missing_fields"] = json.loads(str(payload.pop("missing_fields_json")))
        result.append(payload)
    return result


def completed_canonical_fixture_ids(conn: sqlite3.Connection, job_id: str) -> set[str]:
    rows = conn.execute(
        """
        SELECT canonical_fixture_id
        FROM football_backfill_fixture_progress
        WHERE job_id = ? AND state = 'COMPLETE'
        """,
        (job_id,),
    ).fetchall()
    return {str(row["canonical_fixture_id"]) for row in rows}


def record_failure(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    canonical_fixture_id: str,
    provider_fixture_id: str,
    failure_kind: str,
    message: str | None,
    recorded_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO football_backfill_failures (
            job_id, canonical_fixture_id, provider_fixture_id, failure_kind, message, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (job_id, canonical_fixture_id, provider_fixture_id, failure_kind, message, recorded_at),
    )


def fetch_failures(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT canonical_fixture_id, provider_fixture_id, failure_kind, message, recorded_at
        FROM football_backfill_failures
        WHERE job_id = ?
        ORDER BY failure_id
        """,
        (job_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def upsert_backfill_event(conn: sqlite3.Connection, row: HistoricalEventRow) -> bool:
    """Idempotently persist one GAP-ready historical event.

    Returns ``True`` when the event was newly inserted and ``False`` when it was
    already present (i.e. a duplicate provider fixture produced no new row).
    """
    result = conn.execute(
        """
        INSERT OR IGNORE INTO football_events (
            event_id, competition_id, competition_name, season_code, division_code,
            match_date, kickoff_time, home_team, away_team, home_goals, away_goals,
            result_code, half_time_home_goals, half_time_away_goals,
            half_time_result_code, referee, home_shots, away_shots,
            home_shots_on_target, away_shots_on_target, home_fouls, away_fouls,
            home_corners, away_corners, home_yellow_cards, away_yellow_cards,
            home_red_cards, away_red_cards
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.event_id,
            row.competition_id,
            row.competition_name,
            row.season_code,
            row.division_code,
            row.match_date,
            row.kickoff_time,
            row.home_team,
            row.away_team,
            row.home_goals,
            row.away_goals,
            row.result_code,
            row.half_time_home_goals,
            row.half_time_away_goals,
            row.half_time_result_code,
            row.referee,
            row.home_shots,
            row.away_shots,
            row.home_shots_on_target,
            row.away_shots_on_target,
            row.home_fouls,
            row.away_fouls,
            row.home_corners,
            row.away_corners,
            row.home_yellow_cards,
            row.away_yellow_cards,
            row.home_red_cards,
            row.away_red_cards,
        ),
    )
    return result.rowcount == 1


def event_row_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM football_events").fetchone()[0])
