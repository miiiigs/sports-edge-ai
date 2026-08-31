"""SQLite persistence for the operational football dashboard."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .models import NormalizedFixture

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS dashboard_fixtures (
        fixture_id TEXT PRIMARY KEY,
        provider_fixture_id TEXT,
        provider TEXT NOT NULL,
        competition_id TEXT NOT NULL,
        competition_name TEXT NOT NULL,
        division_code TEXT,
        season TEXT NOT NULL,
        home_team_id TEXT,
        home_team_name TEXT NOT NULL,
        away_team_id TEXT,
        away_team_name TEXT NOT NULL,
        kickoff_utc TEXT NOT NULL,
        status TEXT NOT NULL,
        elapsed_minute INTEGER,
        home_score INTEGER,
        away_score INTEGER,
        last_provider_update TEXT NOT NULL,
        source_version TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dashboard_predictions (
        prediction_id TEXT PRIMARY KEY,
        fixture_id TEXT NOT NULL,
        status TEXT NOT NULL,
        generated_at TEXT NOT NULL,
        model_as_of TEXT,
        model_snapshot_id TEXT,
        version_key TEXT NOT NULL UNIQUE,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(fixture_id) REFERENCES dashboard_fixtures(fixture_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dashboard_fixture_results (
        fixture_id TEXT PRIMARY KEY,
        statistics_provider TEXT NOT NULL,
        statistics_provider_fixture_id TEXT,
        home_corners INTEGER,
        away_corners INTEGER,
        total_corners INTEGER,
        source_timestamp TEXT,
        captured_at TEXT NOT NULL,
        FOREIGN KEY(fixture_id) REFERENCES dashboard_fixtures(fixture_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dashboard_team_mappings (
        mapping_key TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        competition_id TEXT NOT NULL,
        provider_team_id TEXT,
        provider_team_name TEXT NOT NULL,
        normalized_team_name TEXT NOT NULL,
        canonical_team_id TEXT,
        canonical_team_name TEXT,
        resolution_kind TEXT NOT NULL,
        confidence REAL NOT NULL,
        reason TEXT,
        last_seen_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dashboard_runtime_state (
        state_key TEXT PRIMARY KEY,
        state_value_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS one_shot_market_sync_claims (
        token_hash TEXT PRIMARY KEY,
        token_id TEXT NOT NULL,
        state TEXT NOT NULL,
        claimed_at TEXT NOT NULL,
        completed_at TEXT,
        provider_requests INTEGER NOT NULL DEFAULT 0,
        snapshots INTEGER NOT NULL DEFAULT 0,
        decisions INTEGER NOT NULL DEFAULT 0,
        paper_entries INTEGER NOT NULL DEFAULT 0,
        error TEXT
    )
    """,
)

SQLITE_BUSY_TIMEOUT_MS = 5_000


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize_database(conn: sqlite3.Connection) -> None:
    journal_mode = str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
    if journal_mode != "wal":
        raise sqlite3.OperationalError(f"expected WAL journal mode, got {journal_mode!r}")
    ensure_schema(conn)


def ensure_schema(conn: sqlite3.Connection) -> None:
    with conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)


def _ensure_schema_if_requested(conn: sqlite3.Connection, *, initialize_schema: bool) -> None:
    if initialize_schema:
        ensure_schema(conn)


def _merge_fixture_result(payload: dict[str, Any], result_row: dict[str, Any] | None) -> dict[str, Any]:
    if result_row is None:
        return payload
    merged = dict(payload)
    merged.update(
        {
            "statistics_provider": result_row.get("statistics_provider"),
            "statistics_provider_fixture_id": result_row.get("statistics_provider_fixture_id"),
            "home_corners": result_row.get("home_corners"),
            "away_corners": result_row.get("away_corners"),
            "total_corners": result_row.get("total_corners"),
            "corner_result_source_timestamp": result_row.get("source_timestamp"),
            "corner_result_captured_at": result_row.get("captured_at"),
        }
    )
    return merged


def upsert_fixtures(
    conn: sqlite3.Connection,
    fixtures: Iterable[NormalizedFixture],
    *,
    updated_at: str,
    initialize_schema: bool = True,
) -> None:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    with conn:
        for fixture in fixtures:
            payload_json = json.dumps(fixture.to_dict(timezone_name="UTC"), sort_keys=True)
            conn.execute(
                """
                INSERT INTO dashboard_fixtures (
                    fixture_id, provider_fixture_id, provider, competition_id, competition_name,
                    division_code, season, home_team_id, home_team_name, away_team_id, away_team_name,
                    kickoff_utc, status, elapsed_minute, home_score, away_score,
                    last_provider_update, source_version, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fixture_id) DO UPDATE SET
                    provider_fixture_id=excluded.provider_fixture_id,
                    provider=excluded.provider,
                    competition_id=excluded.competition_id,
                    competition_name=excluded.competition_name,
                    division_code=excluded.division_code,
                    season=excluded.season,
                    home_team_id=excluded.home_team_id,
                    home_team_name=excluded.home_team_name,
                    away_team_id=excluded.away_team_id,
                    away_team_name=excluded.away_team_name,
                    kickoff_utc=excluded.kickoff_utc,
                    status=excluded.status,
                    elapsed_minute=excluded.elapsed_minute,
                    home_score=excluded.home_score,
                    away_score=excluded.away_score,
                    last_provider_update=excluded.last_provider_update,
                    source_version=excluded.source_version,
                    payload_json=excluded.payload_json,
                    updated_at=excluded.updated_at
                """,
                (
                    fixture.fixture_id,
                    fixture.provider_fixture_id,
                    fixture.provider,
                    fixture.competition_id,
                    fixture.competition_name,
                    fixture.division_code,
                    fixture.season,
                    fixture.home_team_id,
                    fixture.home_team_name,
                    fixture.away_team_id,
                    fixture.away_team_name,
                    fixture.kickoff_utc,
                    fixture.status,
                    fixture.elapsed_minute,
                    fixture.home_score,
                    fixture.away_score,
                    fixture.last_provider_update,
                    fixture.source_version,
                    payload_json,
                    updated_at,
                ),
            )


def fetch_all_fixtures(conn: sqlite3.Connection, *, initialize_schema: bool = True) -> list[dict[str, Any]]:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    result_rows = fetch_fixture_results(conn, initialize_schema=False)
    result_map = {str(row["fixture_id"]): row for row in result_rows}
    rows = conn.execute(
        """
        SELECT payload_json
        FROM dashboard_fixtures
        ORDER BY kickoff_utc, fixture_id
        """
    ).fetchall()
    merged_rows: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(str(row["payload_json"]))
        merged_rows.append(_merge_fixture_result(payload, result_map.get(str(payload.get("fixture_id") or ""))))
    return merged_rows


def fetch_fixture(conn: sqlite3.Connection, fixture_id: str, *, initialize_schema: bool = True) -> dict[str, Any] | None:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    row = conn.execute(
        """
        SELECT payload_json
        FROM dashboard_fixtures
        WHERE fixture_id = ?
        """,
        (fixture_id,),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(str(row["payload_json"]))
    return _merge_fixture_result(payload, fetch_fixture_result(conn, fixture_id, initialize_schema=False))


def fetch_fixtures_by_ids(
    conn: sqlite3.Connection,
    fixture_ids: Iterable[str],
    *,
    initialize_schema: bool = True,
) -> dict[str, dict[str, Any]]:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    unique_fixture_ids = sorted({str(fixture_id) for fixture_id in fixture_ids if str(fixture_id)})
    if not unique_fixture_ids:
        return {}
    rows: list[sqlite3.Row] = []
    chunk_size = 900
    for start in range(0, len(unique_fixture_ids), chunk_size):
        chunk = unique_fixture_ids[start : start + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        rows.extend(
            conn.execute(
                f"""
                SELECT fixture_id, payload_json
                FROM dashboard_fixtures
                WHERE fixture_id IN ({placeholders})
                """,
                chunk,
            ).fetchall()
        )
    return {
        str(row["fixture_id"]): json.loads(str(row["payload_json"]))
        for row in rows
    }


def upsert_fixture_result(
    conn: sqlite3.Connection,
    *,
    fixture_id: str,
    statistics_provider: str,
    statistics_provider_fixture_id: str | None,
    home_corners: int | None,
    away_corners: int | None,
    total_corners: int | None,
    source_timestamp: str | None,
    captured_at: str,
    initialize_schema: bool = True,
) -> None:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    with conn:
        conn.execute(
            """
            INSERT INTO dashboard_fixture_results (
                fixture_id, statistics_provider, statistics_provider_fixture_id,
                home_corners, away_corners, total_corners, source_timestamp, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fixture_id) DO UPDATE SET
                statistics_provider=excluded.statistics_provider,
                statistics_provider_fixture_id=excluded.statistics_provider_fixture_id,
                home_corners=excluded.home_corners,
                away_corners=excluded.away_corners,
                total_corners=excluded.total_corners,
                source_timestamp=excluded.source_timestamp,
                captured_at=excluded.captured_at
            """,
            (
                fixture_id,
                statistics_provider,
                statistics_provider_fixture_id,
                home_corners,
                away_corners,
                total_corners,
                source_timestamp,
                captured_at,
            ),
        )


def fetch_fixture_result(conn: sqlite3.Connection, fixture_id: str, *, initialize_schema: bool = True) -> dict[str, Any] | None:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    row = conn.execute(
        """
        SELECT fixture_id, statistics_provider, statistics_provider_fixture_id,
               home_corners, away_corners, total_corners, source_timestamp, captured_at
        FROM dashboard_fixture_results
        WHERE fixture_id = ?
        """,
        (fixture_id,),
    ).fetchone()
    return None if row is None else dict(row)


def fetch_fixture_results(conn: sqlite3.Connection, *, initialize_schema: bool = True) -> list[dict[str, Any]]:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    rows = conn.execute(
        """
        SELECT fixture_id, statistics_provider, statistics_provider_fixture_id,
               home_corners, away_corners, total_corners, source_timestamp, captured_at
        FROM dashboard_fixture_results
        ORDER BY fixture_id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def save_prediction(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    updated_at: str,
    initialize_schema: bool = True,
) -> None:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    with conn:
        conn.execute(
            """
            INSERT INTO dashboard_predictions (
                prediction_id, fixture_id, status, generated_at, model_as_of,
                model_snapshot_id, version_key, payload_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(version_key) DO UPDATE SET
                prediction_id=excluded.prediction_id,
                fixture_id=excluded.fixture_id,
                status=excluded.status,
                generated_at=excluded.generated_at,
                model_as_of=excluded.model_as_of,
                model_snapshot_id=excluded.model_snapshot_id,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                str(payload["prediction_id"]),
                str(payload["fixture_id"]),
                str(payload["prediction_status"]),
                str(payload["generated_at"]),
                None if payload.get("model_as_of") is None else str(payload["model_as_of"]),
                None if payload.get("model_snapshot_id") is None else str(payload["model_snapshot_id"]),
                str(payload["version_key"]),
                json.dumps(payload, sort_keys=True),
                updated_at,
            ),
        )


def fetch_prediction_by_version_key(
    conn: sqlite3.Connection,
    version_key: str,
    *,
    initialize_schema: bool = True,
) -> dict[str, Any] | None:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    row = conn.execute(
        """
        SELECT payload_json
        FROM dashboard_predictions
        WHERE version_key = ?
        """,
        (version_key,),
    ).fetchone()
    if row is None:
        return None
    return json.loads(str(row["payload_json"]))


def fetch_latest_prediction_for_fixture(
    conn: sqlite3.Connection,
    fixture_id: str,
    *,
    initialize_schema: bool = True,
) -> dict[str, Any] | None:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    row = conn.execute(
        """
        SELECT payload_json
        FROM dashboard_predictions
        WHERE fixture_id = ?
        ORDER BY generated_at DESC, prediction_id DESC
        LIMIT 1
        """,
        (fixture_id,),
    ).fetchone()
    if row is None:
        return None
    return json.loads(str(row["payload_json"]))


def fetch_all_predictions(conn: sqlite3.Connection, *, initialize_schema: bool = True) -> list[dict[str, Any]]:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    rows = conn.execute(
        """
        SELECT payload_json
        FROM dashboard_predictions
        ORDER BY generated_at DESC, fixture_id
        """
    ).fetchall()
    return [json.loads(str(row["payload_json"])) for row in rows]


def record_team_resolution(
    conn: sqlite3.Connection,
    resolution: dict[str, Any],
    *,
    updated_at: str,
    initialize_schema: bool = True,
) -> None:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    mapping_key = "|".join(
        [
            str(resolution["provider"]),
            str(resolution["competition_id"]),
            str(resolution.get("provider_team_id") or ""),
            str(resolution["normalized_team_name"]),
        ]
    )
    with conn:
        conn.execute(
            """
            INSERT INTO dashboard_team_mappings (
                mapping_key, provider, competition_id, provider_team_id, provider_team_name,
                normalized_team_name, canonical_team_id, canonical_team_name, resolution_kind,
                confidence, reason, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mapping_key) DO UPDATE SET
                provider_team_name=excluded.provider_team_name,
                canonical_team_id=excluded.canonical_team_id,
                canonical_team_name=excluded.canonical_team_name,
                resolution_kind=excluded.resolution_kind,
                confidence=excluded.confidence,
                reason=excluded.reason,
                last_seen_at=excluded.last_seen_at
            """,
            (
                mapping_key,
                str(resolution["provider"]),
                str(resolution["competition_id"]),
                None if resolution.get("provider_team_id") is None else str(resolution["provider_team_id"]),
                str(resolution["provider_team_name"]),
                str(resolution["normalized_team_name"]),
                None if resolution.get("canonical_team_id") is None else str(resolution["canonical_team_id"]),
                None if resolution.get("canonical_team_name") is None else str(resolution["canonical_team_name"]),
                str(resolution["resolution_kind"]),
                float(resolution["confidence"]),
                None if resolution.get("reason") is None else str(resolution["reason"]),
                updated_at,
            ),
        )


def put_runtime_state(
    conn: sqlite3.Connection,
    *,
    state_key: str,
    state_value: dict[str, Any],
    updated_at: str,
    initialize_schema: bool = True,
) -> None:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    with conn:
        conn.execute(
            """
            INSERT INTO dashboard_runtime_state (
                state_key, state_value_json, updated_at
            ) VALUES (?, ?, ?)
            ON CONFLICT(state_key) DO UPDATE SET
                state_value_json=excluded.state_value_json,
                updated_at=excluded.updated_at
            """,
            (
                state_key,
                json.dumps(state_value, sort_keys=True),
                updated_at,
            ),
        )


def get_runtime_state(conn: sqlite3.Connection, state_key: str, *, initialize_schema: bool = True) -> dict[str, Any] | None:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    row = conn.execute(
        """
        SELECT state_value_json
        FROM dashboard_runtime_state
        WHERE state_key = ?
        """,
        (state_key,),
    ).fetchone()
    if row is None:
        return None
    return json.loads(str(row["state_value_json"]))


def claim_one_shot_market_sync_token(
    conn: sqlite3.Connection,
    *,
    token_hash: str,
    token_id: str,
    claimed_at: str,
    initialize_schema: bool = True,
) -> bool:
    """Atomically claim a one-shot market-sync token.

    Returns True when this process is the winner (a token never seen before) and
    False when the token has already been claimed/consumed. The claim is
    committed before any provider request, so a crash between claim and execution
    cannot cause the same token to run more than once.
    """
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO one_shot_market_sync_claims (
                    token_hash, token_id, state, claimed_at, completed_at,
                    provider_requests, snapshots, decisions, paper_entries, error
                ) VALUES (?, ?, 'CLAIMED', ?, NULL, 0, 0, 0, 0, NULL)
                """,
                (token_hash, token_id, claimed_at),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def get_one_shot_market_sync_claim(
    conn: sqlite3.Connection,
    token_hash: str,
    *,
    initialize_schema: bool = True,
) -> dict[str, Any] | None:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    row = conn.execute(
        """
        SELECT token_hash, token_id, state, claimed_at, completed_at,
               provider_requests, snapshots, decisions, paper_entries, error
        FROM one_shot_market_sync_claims
        WHERE token_hash = ?
        """,
        (token_hash,),
    ).fetchone()
    return None if row is None else dict(row)


def update_one_shot_market_sync_claim(
    conn: sqlite3.Connection,
    *,
    token_hash: str,
    state: str,
    completed_at: str,
    provider_requests: int,
    snapshots: int,
    decisions: int,
    paper_entries: int,
    error: str | None,
    initialize_schema: bool = True,
) -> None:
    _ensure_schema_if_requested(conn, initialize_schema=initialize_schema)
    with conn:
        conn.execute(
            """
            UPDATE one_shot_market_sync_claims
            SET state = ?, completed_at = ?, provider_requests = ?, snapshots = ?,
                decisions = ?, paper_entries = ?, error = ?
            WHERE token_hash = ?
            """,
            (
                state,
                completed_at,
                provider_requests,
                snapshots,
                decisions,
                paper_entries,
                error,
                token_hash,
            ),
        )
