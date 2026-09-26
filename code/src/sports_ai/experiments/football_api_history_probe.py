"""Bounded, fail-closed API-Football historical coverage probing.

The probe is intentionally small: one league reference lookup, one league-season
fixture list, then at most a few fixture-statistics samples. It never writes to
research storage and never changes model support. Full backfill is authorized
only after this probe demonstrates provider coverage and complete GAP inputs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol, Sequence

from ..data_sources.api_football_history import (
    estimate_api_football_backfill_requests,
    event_is_gap_ready,
    normalize_api_football_historical_event,
)


_FINISHED_STATUS_CODES = frozenset({"FT", "AET", "PEN"})


@dataclass(frozen=True)
class HistoricalProbeTarget:
    competition_id: str
    competition_name: str
    division_code: str
    search: str
    country: str
    competition_type: str
    accepted_provider_names: tuple[str, ...]
    provider_league_id: int | None = None


@dataclass(frozen=True)
class HistoricalProbeResult:
    competition_id: str
    season: int
    state: str
    provider_league_id: int | None
    provider_league_name: str | None
    provider_country: str | None
    statistics_fixture_coverage: bool | None
    total_fixture_count: int
    completed_fixture_count: int
    sampled_fixture_count: int
    sampled_gap_ready_count: int
    projected_backfill_requests: int | None
    bounded_probe_requests: int
    sample_failures: tuple[dict[str, Any], ...]
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HistoricalProbeProvider(Protocol):
    def get_historical_league_candidates(self, *, search: str, season: int) -> list[dict[str, Any]]: ...
    def get_historical_league_by_id(self, *, league_id: int, season: int) -> list[dict[str, Any]]: ...

    def get_historical_fixtures(self, *, league_id: int, season: int) -> list[dict[str, Any]]: ...

    def get_historical_fixture_statistics(self, *, provider_fixture_id: str) -> list[dict[str, Any]]: ...


MISSING_SCOPE_TARGETS: tuple[HistoricalProbeTarget, ...] = (
    HistoricalProbeTarget(
        "FRA1", "Ligue 1", "F1", "Ligue 1", "France", "League", ("Ligue 1",), 61
    ),
    HistoricalProbeTarget(
        "GER1", "Bundesliga", "D1", "Bundesliga", "Germany", "League", ("Bundesliga",), 78
    ),
    HistoricalProbeTarget(
        "ITA1", "Serie A", "I1", "Serie A", "Italy", "League", ("Serie A",)
    ),
    HistoricalProbeTarget(
        "SCO1",
        "Scottish Premiership",
        "SC0",
        "Premiership",
        "Scotland",
        "League",
        ("Premiership",),
    ),
    HistoricalProbeTarget(
        "UCL",
        "Champions League",
        "UCL",
        "UEFA Champions League",
        "World",
        "Cup",
        ("UEFA Champions League", "Champions League"),
    ),
    HistoricalProbeTarget(
        "UEL",
        "Europa League",
        "UEL",
        "UEFA Europa League",
        "World",
        "Cup",
        ("UEFA Europa League", "Europa League"),
    ),
    HistoricalProbeTarget(
        "FAC", "FA Cup", "FAC", "FA Cup", "England", "Cup", ("FA Cup",)
    ),
    HistoricalProbeTarget(
        "EFLC",
        "EFL Cup",
        "EFLC",
        "League Cup",
        "England",
        "Cup",
        ("League Cup", "EFL Cup"),
    ),
)


def target_for_competition(competition_id: str) -> HistoricalProbeTarget:
    for target in MISSING_SCOPE_TARGETS:
        if target.competition_id == competition_id:
            return target
    raise KeyError(f"unsupported historical probe competition {competition_id!r}")


def season_code_from_start_year(season: int) -> str:
    year = int(season)
    return f"{year % 100:02d}{(year + 1) % 100:02d}"


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _matching_season(candidate: dict[str, Any], season: int) -> dict[str, Any] | None:
    for row in candidate.get("seasons") or []:
        try:
            if int(row.get("year")) == int(season):
                return row
        except (TypeError, ValueError):
            continue
    return None


def resolve_historical_league_candidate(
    candidates: Sequence[dict[str, Any]],
    *,
    target: HistoricalProbeTarget,
    season: int,
) -> tuple[dict[str, Any] | None, str | None]:
    accepted_names = {_norm(name) for name in target.accepted_provider_names}
    matches: list[dict[str, Any]] = []
    for candidate in candidates:
        league = candidate.get("league") or {}
        country = candidate.get("country") or {}
        if _norm(league.get("name")) not in accepted_names:
            continue
        if _norm(country.get("name")) != _norm(target.country):
            continue
        if _norm(league.get("type")) != _norm(target.competition_type):
            continue
        if _matching_season(candidate, season) is None:
            continue
        matches.append(candidate)

    if not matches:
        return None, "LEAGUE_REFERENCE_NOT_FOUND"
    if len(matches) > 1:
        return None, "LEAGUE_REFERENCE_AMBIGUOUS"
    return matches[0], None


def _statistics_fixture_coverage(candidate: dict[str, Any], season: int) -> bool | None:
    season_row = _matching_season(candidate, season)
    if season_row is None:
        return None
    coverage = season_row.get("coverage") or {}
    fixtures = coverage.get("fixtures") or {}
    value = fixtures.get("statistics_fixtures")
    return None if value is None else bool(value)


def _completed_fixtures(fixtures: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [
        row
        for row in fixtures
        if str((((row.get("fixture") or {}).get("status") or {}).get("short")) or "").upper()
        in _FINISHED_STATUS_CODES
    ]
    return sorted(
        rows,
        key=lambda row: (
            str((row.get("fixture") or {}).get("date") or ""),
            str((row.get("fixture") or {}).get("id") or ""),
        ),
    )


def _sample_completed_fixtures(
    fixtures: Sequence[dict[str, Any]],
    *,
    sample_size: int,
) -> list[dict[str, Any]]:
    rows = list(fixtures)
    if not rows or sample_size <= 0:
        return []
    if len(rows) <= sample_size:
        return rows
    if sample_size == 1:
        return [rows[len(rows) // 2]]

    indexes = {
        round(index * (len(rows) - 1) / (sample_size - 1))
        for index in range(sample_size)
    }
    return [rows[index] for index in sorted(indexes)]


def run_historical_coverage_probe(
    provider: HistoricalProbeProvider,
    *,
    target: HistoricalProbeTarget,
    season: int,
    sample_size: int = 3,
) -> HistoricalProbeResult:
    if target.provider_league_id is not None:
        candidates = provider.get_historical_league_by_id(
            league_id=int(target.provider_league_id),
            season=int(season),
        )
    else:
        candidates = provider.get_historical_league_candidates(
            search=target.search,
            season=int(season),
        )
    candidate, resolution_error = resolve_historical_league_candidate(
        candidates,
        target=target,
        season=season,
    )
    if candidate is None:
        return HistoricalProbeResult(
            competition_id=target.competition_id,
            season=int(season),
            state="BLOCKED",
            provider_league_id=None,
            provider_league_name=None,
            provider_country=None,
            statistics_fixture_coverage=None,
            total_fixture_count=0,
            completed_fixture_count=0,
            sampled_fixture_count=0,
            sampled_gap_ready_count=0,
            projected_backfill_requests=None,
            bounded_probe_requests=1,
            sample_failures=(),
            reason=resolution_error,
        )

    league = candidate.get("league") or {}
    country = candidate.get("country") or {}
    try:
        league_id = int(league.get("id"))
    except (TypeError, ValueError):
        league_id = 0
    if league_id <= 0:
        return HistoricalProbeResult(
            competition_id=target.competition_id,
            season=int(season),
            state="BLOCKED",
            provider_league_id=None,
            provider_league_name=str(league.get("name") or "") or None,
            provider_country=str(country.get("name") or "") or None,
            statistics_fixture_coverage=None,
            total_fixture_count=0,
            completed_fixture_count=0,
            sampled_fixture_count=0,
            sampled_gap_ready_count=0,
            projected_backfill_requests=None,
            bounded_probe_requests=1,
            sample_failures=(),
            reason="LEAGUE_ID_INVALID",
        )

    statistics_coverage = _statistics_fixture_coverage(candidate, season)
    if statistics_coverage is not True:
        return HistoricalProbeResult(
            competition_id=target.competition_id,
            season=int(season),
            state="BLOCKED",
            provider_league_id=league_id,
            provider_league_name=str(league.get("name") or "") or None,
            provider_country=str(country.get("name") or "") or None,
            statistics_fixture_coverage=statistics_coverage,
            total_fixture_count=0,
            completed_fixture_count=0,
            sampled_fixture_count=0,
            sampled_gap_ready_count=0,
            projected_backfill_requests=None,
            bounded_probe_requests=1,
            sample_failures=(),
            reason="FIXTURE_STATISTICS_COVERAGE_UNAVAILABLE",
        )

    fixtures = provider.get_historical_fixtures(league_id=league_id, season=int(season))
    completed = _completed_fixtures(fixtures)
    if not completed:
        return HistoricalProbeResult(
            competition_id=target.competition_id,
            season=int(season),
            state="BLOCKED",
            provider_league_id=league_id,
            provider_league_name=str(league.get("name") or "") or None,
            provider_country=str(country.get("name") or "") or None,
            statistics_fixture_coverage=True,
            total_fixture_count=len(fixtures),
            completed_fixture_count=0,
            sampled_fixture_count=0,
            sampled_gap_ready_count=0,
            projected_backfill_requests=None,
            bounded_probe_requests=2,
            sample_failures=(),
            reason="NO_COMPLETED_FIXTURES_OR_LOW_PRIORITY_REQUEST_BLOCKED",
        )

    sample = _sample_completed_fixtures(completed, sample_size=max(1, int(sample_size)))
    ready = 0
    failures: list[dict[str, Any]] = []
    season_code = season_code_from_start_year(season)
    for fixture in sample:
        provider_fixture_id = str((fixture.get("fixture") or {}).get("id") or "")
        statistics = provider.get_historical_fixture_statistics(
            provider_fixture_id=provider_fixture_id,
        )
        row = normalize_api_football_historical_event(
            fixture,
            statistics,
            competition_id=target.competition_id,
            competition_name=target.competition_name,
            division_code=target.division_code,
            season_code=season_code,
        )
        if row is not None and event_is_gap_ready(row):
            ready += 1
            continue
        failures.append(
            {
                "provider_fixture_id": provider_fixture_id,
                "kickoff": str((fixture.get("fixture") or {}).get("date") or ""),
                "home_team": str(((fixture.get("teams") or {}).get("home") or {}).get("name") or ""),
                "away_team": str(((fixture.get("teams") or {}).get("away") or {}).get("name") or ""),
                "statistics_rows": len(statistics),
            }
        )

    estimate = estimate_api_football_backfill_requests(
        league_id=league_id,
        season=int(season),
        expected_fixture_count=len(completed),
    )
    sampled_count = len(sample)
    state = "READY_TO_BACKFILL" if sampled_count > 0 and ready == sampled_count else "SAMPLE_INCOMPLETE"
    return HistoricalProbeResult(
        competition_id=target.competition_id,
        season=int(season),
        state=state,
        provider_league_id=league_id,
        provider_league_name=str(league.get("name") or "") or None,
        provider_country=str(country.get("name") or "") or None,
        statistics_fixture_coverage=True,
        total_fixture_count=len(fixtures),
        completed_fixture_count=len(completed),
        sampled_fixture_count=sampled_count,
        sampled_gap_ready_count=ready,
        projected_backfill_requests=1 + estimate.total_requests,
        bounded_probe_requests=2 + sampled_count,
        sample_failures=tuple(failures),
        reason=None if state == "READY_TO_BACKFILL" else "SAMPLED_GAP_INPUTS_INCOMPLETE",
    )
