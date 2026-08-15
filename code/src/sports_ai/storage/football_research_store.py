"""Local research store for historical football intake.

This is intentionally a lightweight persistence layer for the current phase.
It is not the final production Postgres security architecture. It gives the
project a real local store so exported football intake data can be queried and
reused without staying as loose files only.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from ..data_sources.football_history import HistoricalIngestBundle, fetch_historical_bundle

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS football_ingest_runs (
        ingest_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        source_url TEXT NOT NULL,
        season_code TEXT NOT NULL,
        division_code TEXT NOT NULL,
        competition_id TEXT NOT NULL,
        competition_name TEXT NOT NULL,
        event_count INTEGER NOT NULL,
        market_row_count INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS football_events (
        event_id TEXT PRIMARY KEY,
        competition_id TEXT NOT NULL,
        competition_name TEXT NOT NULL,
        season_code TEXT NOT NULL,
        division_code TEXT NOT NULL,
        match_date TEXT NOT NULL,
        kickoff_time TEXT,
        home_team TEXT NOT NULL,
        away_team TEXT NOT NULL,
        home_goals INTEGER,
        away_goals INTEGER,
        result_code TEXT,
        half_time_home_goals INTEGER,
        half_time_away_goals INTEGER,
        half_time_result_code TEXT,
        referee TEXT,
        home_shots INTEGER,
        away_shots INTEGER,
        home_shots_on_target INTEGER,
        away_shots_on_target INTEGER,
        home_fouls INTEGER,
        away_fouls INTEGER,
        home_corners INTEGER,
        away_corners INTEGER,
        home_yellow_cards INTEGER,
        away_yellow_cards INTEGER,
        home_red_cards INTEGER,
        away_red_cards INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS football_market_quotes (
        quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL,
        competition_id TEXT NOT NULL,
        season_code TEXT NOT NULL,
        division_code TEXT NOT NULL,
        source TEXT NOT NULL,
        market_type TEXT NOT NULL,
        bookmaker TEXT NOT NULL,
        selection_prices_json TEXT NOT NULL,
        UNIQUE(event_id, source, bookmaker, market_type),
        FOREIGN KEY(event_id) REFERENCES football_events(event_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS football_coverage_snapshots (
        coverage_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
        imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        source_url TEXT NOT NULL,
        season_code TEXT NOT NULL,
        division_code TEXT NOT NULL,
        competition_id TEXT NOT NULL,
        event_count INTEGER NOT NULL,
        market_row_count INTEGER NOT NULL,
        first_match_date TEXT,
        last_match_date TEXT,
        missing_profile_fields_json TEXT NOT NULL,
        key_blind_field_coverage_json TEXT NOT NULL,
        available_closing_columns_json TEXT NOT NULL
    )
    """,
)


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open the local research store."""

    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create required tables."""

    with conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)


def store_bundle(conn: sqlite3.Connection, bundle: HistoricalIngestBundle) -> dict[str, int]:
    """Store one historical bundle into the local research DB."""

    ensure_schema(conn)
    inserted_events = 0
    inserted_quotes = 0

    with conn:
        conn.execute(
            """
            INSERT INTO football_ingest_runs (
                source_url, season_code, division_code, competition_id,
                competition_name, event_count, market_row_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bundle.source_url,
                bundle.season_code,
                bundle.division_code,
                bundle.competition_id,
                bundle.competition_name,
                bundle.coverage_summary.event_count,
                bundle.coverage_summary.market_row_count,
            ),
        )

        for row in bundle.sports_rows:
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
            inserted_events += result.rowcount

        for row in bundle.market_rows:
            result = conn.execute(
                """
                INSERT OR IGNORE INTO football_market_quotes (
                    event_id, competition_id, season_code, division_code,
                    source, market_type, bookmaker, selection_prices_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.event_id,
                    row.competition_id,
                    row.season_code,
                    row.division_code,
                    row.source,
                    row.market_type,
                    row.bookmaker,
                    json.dumps(row.selection_prices, sort_keys=True),
                ),
            )
            inserted_quotes += result.rowcount

        conn.execute(
            """
            INSERT INTO football_coverage_snapshots (
                source_url, season_code, division_code, competition_id, event_count,
                market_row_count, first_match_date, last_match_date,
                missing_profile_fields_json, key_blind_field_coverage_json,
                available_closing_columns_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bundle.source_url,
                bundle.season_code,
                bundle.division_code,
                bundle.competition_id,
                bundle.coverage_summary.event_count,
                bundle.coverage_summary.market_row_count,
                bundle.coverage_summary.first_match_date,
                bundle.coverage_summary.last_match_date,
                json.dumps(bundle.coverage_summary.missing_profile_fields),
                json.dumps(bundle.coverage_summary.key_blind_field_coverage, sort_keys=True),
                json.dumps(bundle.coverage_summary.available_closing_columns),
            ),
        )

    return {
        "inserted_events": inserted_events,
        "inserted_market_quotes": inserted_quotes,
        "event_count": bundle.coverage_summary.event_count,
        "market_row_count": bundle.coverage_summary.market_row_count,
    }


def store_fetched_bundle(
    db_path: str | Path,
    *,
    season_code: str,
    division_code: str,
) -> dict[str, Any]:
    """Fetch a bundle, store it, and return a compact summary."""

    bundle = fetch_historical_bundle(season_code, division_code)
    with connect(db_path) as conn:
        summary = store_bundle(conn, bundle)
    return {
        "db_path": str(Path(db_path)),
        "season_code": season_code,
        "division_code": division_code,
        "competition_id": bundle.competition_id,
        "competition_name": bundle.competition_name,
        **summary,
    }


def snapshot_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Return a simple table-count summary."""

    return {
        "football_ingest_runs": conn.execute("SELECT COUNT(*) FROM football_ingest_runs").fetchone()[0],
        "football_events": conn.execute("SELECT COUNT(*) FROM football_events").fetchone()[0],
        "football_market_quotes": conn.execute("SELECT COUNT(*) FROM football_market_quotes").fetchone()[0],
        "football_coverage_snapshots": conn.execute("SELECT COUNT(*) FROM football_coverage_snapshots").fetchone()[0],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CLI for the local football research store."""

    import argparse

    parser = argparse.ArgumentParser(description="Store a historical football bundle in a local SQLite research DB.")
    parser.add_argument("--season", required=True, help="Season code like 2526 or 2627.")
    parser.add_argument("--division", required=True, help="Division code like E0 or SC0.")
    parser.add_argument("--db-path", required=True, help="Target SQLite database path.")
    args = parser.parse_args(argv)

    result = store_fetched_bundle(args.db_path, season_code=args.season, division_code=args.division)
    with connect(args.db_path) as conn:
        result["table_counts"] = snapshot_counts(conn)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
