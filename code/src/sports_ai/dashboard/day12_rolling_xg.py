"""Leakage-safe rolling pre-match xG profile generation for live Day 12.

The live Day 12 gate must not treat ``SPORTS_AI_CURRENT_XG_CONNECTED`` as proof
that point-in-time xG exists. This module builds a team's recent expected-goals
per match from historical *completed* matches (shots / shots-on-target) using the
frozen Day 8 conversion coefficients, with a strict as-of cutoff so future and
current fixture data can never enter the rolling profile.

The result is Sports Edge's own derived metric, not a third-party xG model.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from ..experiments.football_day12_safeguards import (
    DEFAULT_XG_MIN_WINDOW_MATCHES,
    REASON_XG_DATA_NOT_SUPPLIED,
    REASON_XG_INPUT_PATH_UNCONFIGURED,
)
from .models import normalize_text_key, parse_iso_datetime

SOURCE_IDENTIFIER = "sports_edge_day8_historical_rolling_xg_v1"
DEFAULT_MIN_WINDOW_MATCHES = DEFAULT_XG_MIN_WINDOW_MATCHES
_HISTORICAL_TIMEZONE = "Europe/London"

XgRowsByRole = dict[tuple[str, str], dict[str, Any]]


def _kickoff_utc(match_date: str, kickoff_time: str | None) -> datetime:
    """Combine football_events match_date + kickoff_time (UK local) into UTC."""
    if not match_date:
        raise ValueError("missing match_date")
    parts = match_date.split("-")
    if len(parts) != 3:
        raise ValueError(f"unexpected match_date {match_date!r}")
    year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
    hour, minute = 12, 0
    if kickoff_time:
        time_parts = kickoff_time.strip().split(":")
        hour = int(time_parts[0])
        minute = int(time_parts[1]) if len(time_parts) > 1 else 0
    local = datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(_HISTORICAL_TIMEZONE))
    return local.astimezone(timezone.utc)


def _team_match_xg(row: dict[str, Any], team_key: str, coefficients: dict[str, dict[str, float]]) -> float | None:
    """Expected goals for a team in one historical match using frozen Day 8 coefficients."""
    home_key = normalize_text_key(str(row.get("home_team") or ""))
    away_key = normalize_text_key(str(row.get("away_team") or ""))
    home_coefficients = (coefficients or {}).get("home")
    away_coefficients = (coefficients or {}).get("away")
    if home_coefficients is None or away_coefficients is None:
        return None
    try:
        if team_key == home_key:
            shots = row.get("home_shots")
            shots_on_target = row.get("home_shots_on_target")
            if shots is None or shots_on_target is None:
                return None
            return float(home_coefficients["beta_shots"]) * float(shots) + float(
                home_coefficients["beta_shots_on_target"]
            ) * float(shots_on_target)
        if team_key == away_key:
            shots = row.get("away_shots")
            shots_on_target = row.get("away_shots_on_target")
            if shots is None or shots_on_target is None:
                return None
            return float(away_coefficients["beta_shots"]) * float(shots) + float(
                away_coefficients["beta_shots_on_target"]
            ) * float(shots_on_target)
    except (KeyError, TypeError, ValueError):
        return None
    return None


def _load_competition_events(db_path: str | Path, competition_id: str) -> list[dict[str, Any]]:
    """Load completed historical match rows for a competition.

    Failures (missing DB, missing table, corrupt file) return an empty list so
    the caller fails closed (no observations -> no xG row -> blocked) instead of
    aborting the market sync.
    """
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT event_id, match_date, kickoff_time, home_team, away_team, "
                    "home_shots, away_shots, home_shots_on_target, away_shots_on_target "
                    "FROM football_events WHERE competition_id = ?",
                    (competition_id,),
                )
            ]
        finally:
            conn.close()
        return rows
    except (sqlite3.Error, OSError, ValueError):
        return []


def build_team_rolling_xg_row(
    *,
    db_path: str | Path,
    competition_id: str,
    team: str,
    team_role: str,
    coefficients: dict[str, dict[str, float]],
    decision_as_of: str,
    min_window_matches: int = DEFAULT_MIN_WINDOW_MATCHES,
) -> dict[str, Any] | None:
    """Build one Day 12 xG row for a team from completed prior matches only.

    Any unexpected failure returns None (fail closed -> Day 12 blocks) rather
    than aborting the caller.
    """
    try:
        if not decision_as_of:
            return None
        cutoff = parse_iso_datetime(decision_as_of)
        team_key = normalize_text_key(team)
        if not team_key:
            return None

        observations: list[tuple[datetime, float]] = []
        for row in _load_competition_events(db_path, competition_id):
            try:
                kickoff = _kickoff_utc(str(row.get("match_date") or ""), row.get("kickoff_time"))
            except ValueError:
                continue
            # Leakage guard: only completed matches that kicked off strictly before the cutoff.
            if kickoff >= cutoff:
                continue
            match_xg = _team_match_xg(row, team_key, coefficients)
            if match_xg is None:
                continue
            observations.append((kickoff, match_xg))

        if not observations:
            return None

        observations.sort(key=lambda item: item[0])
        window = observations[-min_window_matches:]
        recent_xg = mean(match_xg for _, match_xg in window)
        return {
            "team_role": team_role,
            "recent_xg_for_per_match": recent_xg,
            "matches_in_window": len(window),
            "as_of": window[-1][0].isoformat(timespec="seconds"),
            "window_start_at": window[0][0].isoformat(timespec="seconds"),
            "window_end_at": window[-1][0].isoformat(timespec="seconds"),
            "source": SOURCE_IDENTIFIER,
            "competition_id": competition_id,
        }
    except Exception:
        return None


def build_xg_inspection(
    rows_by_role: XgRowsByRole,
    *,
    min_window_matches: int = DEFAULT_MIN_WINDOW_MATCHES,
) -> dict[str, Any]:
    """Build the Day 12 xg_input_inspection shape from generated rows."""
    rows = list(rows_by_role.values())
    if not rows:
        return {
            "exists": False,
            "path": None,
            "source_names": [],
            "competitions_covered": [],
            "seasons_covered": [],
            "point_in_time_valid": False,
            "sufficient_for_day12_rule": False,
            "row_count": 0,
            "reason": REASON_XG_DATA_NOT_SUPPLIED,
        }
    point_in_time_valid = all(
        row.get("as_of") and row.get("window_end_at") and row.get("window_start_at") for row in rows
    )
    sufficient = all(
        row.get("team_role") in {"HOME", "AWAY"}
        and row.get("recent_xg_for_per_match") is not None
        and int(row.get("matches_in_window") or 0) >= min_window_matches
        for row in rows
    )
    return {
        "exists": True,
        "path": "in-memory:sports_edge_day8_historical_rolling_xg_v1",
        "source_names": [SOURCE_IDENTIFIER],
        "competitions_covered": sorted({str(row.get("competition_id") or "") for row in rows}),
        "seasons_covered": [],
        "point_in_time_valid": point_in_time_valid,
        "sufficient_for_day12_rule": bool(rows) and point_in_time_valid and sufficient,
        "row_count": len(rows),
        "reason": None,
    }


def missing_xg_inspection(*, reason: str = REASON_XG_INPUT_PATH_UNCONFIGURED) -> dict[str, Any]:
    return {
        "exists": False,
        "path": None,
        "source_names": [],
        "competitions_covered": [],
        "seasons_covered": [],
        "point_in_time_valid": False,
        "sufficient_for_day12_rule": False,
        "row_count": 0,
        "reason": reason,
    }


def build_fixture_xg_rows(
    *,
    db_path: str | Path,
    competition_id: str,
    fixture_id: str,
    home_team: str,
    away_team: str,
    decision_as_of: str,
    coefficients: dict[str, dict[str, float]],
    min_window_matches: int = DEFAULT_MIN_WINDOW_MATCHES,
) -> tuple[XgRowsByRole, dict[str, Any]]:
    """Build HOME/AWAY Day 12 xG rows plus inspection for one fixture.

    Failures fail closed: any unexpected error returns empty rows with a
    missing inspection so Day 12 blocks instead of aborting the market sync.
    """
    try:
        rows_by_role: XgRowsByRole = {}
        home_row = build_team_rolling_xg_row(
            db_path=db_path,
            competition_id=competition_id,
            team=home_team,
            team_role="HOME",
            coefficients=coefficients,
            decision_as_of=decision_as_of,
            min_window_matches=min_window_matches,
        )
        away_row = build_team_rolling_xg_row(
            db_path=db_path,
            competition_id=competition_id,
            team=away_team,
            team_role="AWAY",
            coefficients=coefficients,
            decision_as_of=decision_as_of,
            min_window_matches=min_window_matches,
        )
        for role, row in (("HOME", home_row), ("AWAY", away_row)):
            if row is not None:
                rows_by_role[(fixture_id, role)] = {**row, "fixture_id": fixture_id}
        inspection = build_xg_inspection(rows_by_role, min_window_matches=min_window_matches)
        return rows_by_role, inspection
    except Exception:
        return {}, missing_xg_inspection()
