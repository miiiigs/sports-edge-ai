"""Production refresh of Football model-state events.

The fitted model coefficients remain frozen. This module only advances the
point-in-time team state by ingesting completed current-season matches from the
same football-data.co.uk sports source used by the historical research store.
Market/odds columns from that source are deliberately not copied into the model
state refresh path.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..data_sources.football_history import HistoricalIngestBundle, fetch_historical_bundle
from ..storage import football_research_store

DEFAULT_PRODUCTION_DIVISIONS: tuple[str, ...] = ("E0", "E1", "E2", "E3", "SP1")
PRODUCTION_RESEARCH_FILENAME = "football_research_live.sqlite"


@dataclass(frozen=True)
class ResearchRefreshResult:
    season_code: str
    attempted_divisions: tuple[str, ...]
    successful_divisions: tuple[str, ...]
    failed_divisions: dict[str, str]
    changed_rows: int
    latest_match_date: str | None
    refreshed_at: str

    @property
    def fully_successful(self) -> bool:
        return not self.failed_divisions


def active_season_code(as_of: date | None = None) -> str:
    """Return football-data.co.uk season code, e.g. 2627 for 2026/27."""
    day = as_of or datetime.now(timezone.utc).date()
    start_year = day.year if day.month >= 7 else day.year - 1
    return f"{start_year % 100:02d}{(start_year + 1) % 100:02d}"


def prepare_persistent_research_db(*, base_db_path: Path, operational_data_dir: Path) -> Path:
    """Seed a persistent research DB from the immutable bundled baseline once."""
    operational_data_dir.mkdir(parents=True, exist_ok=True)
    target = operational_data_dir / PRODUCTION_RESEARCH_FILENAME
    if not target.exists():
        if not base_db_path.exists():
            raise FileNotFoundError(f"bundled Football research DB not found: {base_db_path}")
        shutil.copy2(base_db_path, target)
    with football_research_store.connect(target) as conn:
        football_research_store.ensure_schema(conn)
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity.lower() != "ok":
            raise sqlite3.DatabaseError(f"persistent Football research DB integrity check failed: {integrity}")
    return target


def _upsert_event_bundle(conn: sqlite3.Connection, bundle: HistoricalIngestBundle) -> int:
    """Upsert only sports/event truth. Market columns stay outside model state."""
    before = conn.total_changes
    with conn:
        for row in bundle.sports_rows:
            conn.execute(
                """
                INSERT INTO football_events (
                    event_id, competition_id, competition_name, season_code, division_code,
                    match_date, kickoff_time, home_team, away_team, home_goals, away_goals,
                    result_code, half_time_home_goals, half_time_away_goals,
                    half_time_result_code, referee, home_shots, away_shots,
                    home_shots_on_target, away_shots_on_target, home_fouls, away_fouls,
                    home_corners, away_corners, home_yellow_cards, away_yellow_cards,
                    home_red_cards, away_red_cards
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    competition_id = excluded.competition_id,
                    competition_name = excluded.competition_name,
                    season_code = excluded.season_code,
                    division_code = excluded.division_code,
                    match_date = excluded.match_date,
                    kickoff_time = excluded.kickoff_time,
                    home_team = excluded.home_team,
                    away_team = excluded.away_team,
                    home_goals = excluded.home_goals,
                    away_goals = excluded.away_goals,
                    result_code = excluded.result_code,
                    half_time_home_goals = excluded.half_time_home_goals,
                    half_time_away_goals = excluded.half_time_away_goals,
                    half_time_result_code = excluded.half_time_result_code,
                    referee = excluded.referee,
                    home_shots = excluded.home_shots,
                    away_shots = excluded.away_shots,
                    home_shots_on_target = excluded.home_shots_on_target,
                    away_shots_on_target = excluded.away_shots_on_target,
                    home_fouls = excluded.home_fouls,
                    away_fouls = excluded.away_fouls,
                    home_corners = excluded.home_corners,
                    away_corners = excluded.away_corners,
                    home_yellow_cards = excluded.home_yellow_cards,
                    away_yellow_cards = excluded.away_yellow_cards,
                    home_red_cards = excluded.home_red_cards,
                    away_red_cards = excluded.away_red_cards
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
    return conn.total_changes - before


def latest_complete_match_date(db_path: Path) -> str | None:
    if not db_path.exists():
        return None
    with football_research_store.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT MAX(match_date) AS latest_match_date
            FROM football_events
            WHERE home_goals IS NOT NULL
              AND away_goals IS NOT NULL
              AND home_shots IS NOT NULL
              AND away_shots IS NOT NULL
              AND home_shots_on_target IS NOT NULL
              AND away_shots_on_target IS NOT NULL
              AND home_corners IS NOT NULL
              AND away_corners IS NOT NULL
            """
        ).fetchone()
    return None if row is None or row["latest_match_date"] is None else str(row["latest_match_date"])


def refresh_current_season_events(
    db_path: Path,
    *,
    as_of: date | None = None,
    divisions: Iterable[str] = DEFAULT_PRODUCTION_DIVISIONS,
    retries: int = 2,
    retry_delay_seconds: float = 0.5,
) -> ResearchRefreshResult:
    """Refresh completed current-season sports rows with bounded per-league retries."""
    season_code = active_season_code(as_of)
    attempted = tuple(dict.fromkeys(str(value).strip() for value in divisions if str(value).strip()))
    successful: list[str] = []
    failures: dict[str, str] = {}
    changed_rows = 0

    with football_research_store.connect(db_path) as conn:
        football_research_store.ensure_schema(conn)
        for division in attempted:
            last_error: Exception | None = None
            bundle: HistoricalIngestBundle | None = None
            for attempt in range(max(1, retries)):
                try:
                    bundle = fetch_historical_bundle(season_code, division)
                    last_error = None
                    break
                except Exception as exc:  # network/provider failures must not corrupt existing state
                    last_error = exc
                    if attempt + 1 < max(1, retries) and retry_delay_seconds > 0:
                        time.sleep(retry_delay_seconds)
            if bundle is None:
                assert last_error is not None
                failures[division] = f"{type(last_error).__name__}: {last_error}"
                continue
            changed_rows += _upsert_event_bundle(conn, bundle)
            successful.append(division)

        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity.lower() != "ok":
            raise sqlite3.DatabaseError(f"Football research DB integrity check failed after refresh: {integrity}")

    return ResearchRefreshResult(
        season_code=season_code,
        attempted_divisions=attempted,
        successful_divisions=tuple(successful),
        failed_divisions=failures,
        changed_rows=changed_rows,
        latest_match_date=latest_complete_match_date(db_path),
        refreshed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def model_age_days(model_as_of: str, *, now: datetime | None = None) -> float:
    value = str(model_as_of).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() / 86400.0)
