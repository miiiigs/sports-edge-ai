"""Pure API-Football historical normalization and backfill planning.

This module deliberately performs no network I/O. Production/provider code can
supply fixture + fixture-statistics payloads after applying the shared request
budget. The normalizer then converts those payloads into the existing
HistoricalEventRow contract used by the Football research store.

The key rule is fail closed: a row is GAP-ready only when both teams have
shots, shots-on-target, and corners. Missing provider statistics are preserved
as missing; they are never imputed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .football_history import HistoricalEventRow

REQUIRED_GAP_FIELDS = (
    "home_shots",
    "away_shots",
    "home_shots_on_target",
    "away_shots_on_target",
    "home_corners",
    "away_corners",
)

_STATISTIC_TO_FIELD = {
    "total shots": "shots",
    "shots on goal": "shots_on_target",
    "fouls": "fouls",
    "corner kicks": "corners",
    "yellow cards": "yellow_cards",
    "red cards": "red_cards",
}

_FINISHED_STATUS_CODES = frozenset({"FT", "AET", "PEN"})


@dataclass(frozen=True)
class ApiFootballHistoryCoverage:
    fixture_count: int
    completed_fixture_count: int
    normalized_event_count: int
    gap_ready_count: int
    gap_ready_fraction: float
    missing_required_fields: dict[str, int]
    rejected_fixture_ids: tuple[str, ...]


@dataclass(frozen=True)
class ApiFootballBackfillEstimate:
    league_id: int
    season: int
    expected_fixture_count: int
    fixture_list_requests: int
    fixture_statistics_requests: int
    total_requests: int

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _result_code(home_goals: int | None, away_goals: int | None) -> str | None:
    if home_goals is None or away_goals is None:
        return None
    if home_goals > away_goals:
        return "H"
    if away_goals > home_goals:
        return "A"
    return "D"


def _kickoff_parts(raw: str) -> tuple[str, str | None]:
    if not raw:
        return "", None
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    return parsed.date().isoformat(), parsed.strftime("%H:%M")


def _team_statistics(
    statistics_rows: Sequence[dict[str, Any]],
    *,
    team_id: int | None,
    team_name: str,
) -> dict[str, int | None]:
    values: dict[str, int | None] = {
        "shots": None,
        "shots_on_target": None,
        "fouls": None,
        "corners": None,
        "yellow_cards": None,
        "red_cards": None,
    }
    normalized_name = team_name.strip().casefold()
    matched: list[dict[str, Any]] = []
    for row in statistics_rows:
        team = row.get("team") or {}
        candidate_id = _int_or_none(team.get("id"))
        candidate_name = str(team.get("name") or "").strip().casefold()
        if team_id is not None and candidate_id == team_id:
            matched.append(row)
        elif team_id is None and normalized_name and candidate_name == normalized_name:
            matched.append(row)
    if len(matched) != 1:
        return values

    for statistic in matched[0].get("statistics") or []:
        raw_type = str(statistic.get("type") or "").strip().casefold()
        field = _STATISTIC_TO_FIELD.get(raw_type)
        if field is not None:
            values[field] = _int_or_none(statistic.get("value"))
    return values


def normalize_api_football_historical_event(
    fixture_payload: dict[str, Any],
    statistics_rows: Sequence[dict[str, Any]],
    *,
    competition_id: str,
    competition_name: str,
    division_code: str,
    season_code: str,
) -> HistoricalEventRow | None:
    """Convert one completed API-Football fixture + statistics into research truth."""
    fixture = fixture_payload.get("fixture") or {}
    status = fixture.get("status") or {}
    status_short = str(status.get("short") or "").strip().upper()
    if status_short not in _FINISHED_STATUS_CODES:
        return None

    provider_fixture_id = str(fixture.get("id") or "").strip()
    kickoff_raw = str(fixture.get("date") or "").strip()
    teams = fixture_payload.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    home_name = str(home.get("name") or "").strip()
    away_name = str(away.get("name") or "").strip()
    if not provider_fixture_id or not kickoff_raw or not home_name or not away_name:
        return None

    goals = fixture_payload.get("goals") or {}
    home_goals = _int_or_none(goals.get("home"))
    away_goals = _int_or_none(goals.get("away"))
    if home_goals is None or away_goals is None:
        return None

    score = fixture_payload.get("score") or {}
    halftime = score.get("halftime") or {}
    home_half = _int_or_none(halftime.get("home"))
    away_half = _int_or_none(halftime.get("away"))
    match_date, kickoff_time = _kickoff_parts(kickoff_raw)

    home_stats = _team_statistics(
        statistics_rows,
        team_id=_int_or_none(home.get("id")),
        team_name=home_name,
    )
    away_stats = _team_statistics(
        statistics_rows,
        team_id=_int_or_none(away.get("id")),
        team_name=away_name,
    )

    return HistoricalEventRow(
        event_id=f"evt_api_football_{provider_fixture_id}",
        competition_id=competition_id,
        competition_name=competition_name,
        season_code=season_code,
        division_code=division_code,
        match_date=match_date,
        kickoff_time=kickoff_time,
        home_team=home_name,
        away_team=away_name,
        home_goals=home_goals,
        away_goals=away_goals,
        result_code=_result_code(home_goals, away_goals),
        half_time_home_goals=home_half,
        half_time_away_goals=away_half,
        half_time_result_code=_result_code(home_half, away_half),
        referee=str(fixture.get("referee") or "").strip() or None,
        home_shots=home_stats["shots"],
        away_shots=away_stats["shots"],
        home_shots_on_target=home_stats["shots_on_target"],
        away_shots_on_target=away_stats["shots_on_target"],
        home_fouls=home_stats["fouls"],
        away_fouls=away_stats["fouls"],
        home_corners=home_stats["corners"],
        away_corners=away_stats["corners"],
        home_yellow_cards=home_stats["yellow_cards"],
        away_yellow_cards=away_stats["yellow_cards"],
        home_red_cards=home_stats["red_cards"],
        away_red_cards=away_stats["red_cards"],
    )


def event_is_gap_ready(row: HistoricalEventRow) -> bool:
    return all(getattr(row, field) is not None for field in REQUIRED_GAP_FIELDS)


def summarize_api_football_history_coverage(
    fixtures: Sequence[dict[str, Any]],
    normalized_rows: Sequence[HistoricalEventRow],
) -> ApiFootballHistoryCoverage:
    completed_ids = {
        str((fixture.get("fixture") or {}).get("id") or "")
        for fixture in fixtures
        if str(((fixture.get("fixture") or {}).get("status") or {}).get("short") or "").upper()
        in _FINISHED_STATUS_CODES
    }
    completed_ids.discard("")
    normalized_fixture_ids = {
        row.event_id.removeprefix("evt_api_football_") for row in normalized_rows
    }
    missing = {field: 0 for field in REQUIRED_GAP_FIELDS}
    gap_ready = 0
    for row in normalized_rows:
        ready = True
        for field in REQUIRED_GAP_FIELDS:
            if getattr(row, field) is None:
                missing[field] += 1
                ready = False
        if ready:
            gap_ready += 1

    normalized_count = len(normalized_rows)
    completed_count = len(completed_ids)
    return ApiFootballHistoryCoverage(
        fixture_count=len(fixtures),
        completed_fixture_count=completed_count,
        normalized_event_count=normalized_count,
        gap_ready_count=gap_ready,
        gap_ready_fraction=0.0 if completed_count == 0 else round(gap_ready / completed_count, 4),
        missing_required_fields=missing,
        rejected_fixture_ids=tuple(sorted(completed_ids - normalized_fixture_ids)),
    )


def estimate_api_football_backfill_requests(
    *,
    league_id: int,
    season: int,
    expected_fixture_count: int,
) -> ApiFootballBackfillEstimate:
    """Estimate the minimum provider calls for one league-season backfill.

    API-Football can return a league-season fixture list in one request, while
    the existing authoritative statistics endpoint is fixture-scoped. The
    estimate is therefore one fixture-list request plus one statistics request
    per fixture. This intentionally assumes no cache hits.
    """
    fixture_count = max(0, int(expected_fixture_count))
    return ApiFootballBackfillEstimate(
        league_id=int(league_id),
        season=int(season),
        expected_fixture_count=fixture_count,
        fixture_list_requests=1,
        fixture_statistics_requests=fixture_count,
        total_requests=1 + fixture_count,
    )


def backfill_meets_gap_coverage_gate(
    coverage: ApiFootballHistoryCoverage,
    *,
    min_gap_ready_fraction: float = 0.95,
    min_gap_ready_rows: int = 100,
) -> bool:
    if coverage.gap_ready_count < max(1, int(min_gap_ready_rows)):
        return False
    return coverage.gap_ready_fraction >= float(min_gap_ready_fraction)
