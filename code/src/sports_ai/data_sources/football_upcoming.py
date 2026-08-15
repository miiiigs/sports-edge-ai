"""Upcoming football fixture intake with provider diagnostics and fallbacks."""

from __future__ import annotations

import csv
import io
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import httpx

from .football_data_co_uk import build_csv_url
from .football_history import DIVISION_TO_COMPETITION

DIVISION_TO_FOOTBALL_DATA_ORG = {
    "E0": "PL",
    "E1": "ELC",
    "SP1": "PD",
    "D1": "BL1",
    "F1": "FL1",
    "I1": "SA",
}
DIVISION_TO_LOCAL_TIMEZONE = {
    "E0": "Europe/London",
    "E1": "Europe/London",
    "E2": "Europe/London",
    "E3": "Europe/London",
    "SP1": "Europe/Madrid",
    "D1": "Europe/Berlin",
    "F1": "Europe/Paris",
    "I1": "Europe/Rome",
}


@dataclass(frozen=True)
class UpcomingFixture:
    event_id: str
    match_date: str
    kickoff_time: str | None
    home_team: str
    away_team: str
    division_code: str
    competition_id: str
    competition_name: str
    source_kind: str
    source_ref: str
    kickoff_at_utc: str | None = None
    source_fixture_id: str | None = None
    retrieved_at: str | None = None


@dataclass(frozen=True)
class FixtureProviderDiagnostic:
    provider: str
    division_code: str
    requested: bool
    status: str
    fixtures_returned: int
    competitions_returned: int
    failure_reason: str | None = None
    source_ref: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(text: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in text).strip("_")


def _build_event_id(match_date: str, home_team: str, away_team: str) -> str:
    return f"evt_football_{match_date.replace('-', '')}_{_slug(home_team)}_{_slug(away_team)}"


def _competition_identity(division_code: str) -> tuple[str, str]:
    return DIVISION_TO_COMPETITION.get(division_code, (division_code, division_code))


def _normalize_date(raw_date: str) -> str:
    parts = raw_date.strip().split("/")
    if len(parts) != 3:
        return raw_date.strip()
    day, month, year = parts
    return f"{year}-{month.zfill(2)}-{day.zfill(2)}"


def _fixture_key(fixture: UpcomingFixture) -> tuple[str, str, str, str]:
    return (
        fixture.division_code,
        fixture.match_date,
        fixture.home_team.lower(),
        fixture.away_team.lower(),
    )


def _normalize_utc_timestamp(raw_value: str | None) -> str | None:
    text = (raw_value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _derive_kickoff_at_utc(
    *,
    match_date: str,
    kickoff_time: str | None,
    division_code: str,
) -> str | None:
    if not kickoff_time:
        return None
    timezone_name = DIVISION_TO_LOCAL_TIMEZONE.get(division_code)
    if timezone_name is None:
        return None
    try:
        local_dt = datetime.fromisoformat(f"{match_date}T{kickoff_time}")
    except ValueError:
        return None
    zoned = local_dt.replace(tzinfo=ZoneInfo(timezone_name))
    return zoned.astimezone(timezone.utc).isoformat(timespec="seconds")


def _load_json_mapping(raw_value: str | None) -> dict[str, Any]:
    if not raw_value:
        return {}
    raw_value = raw_value.strip()
    candidate_path = Path(raw_value)
    if candidate_path.exists():
        try:
            payload = json.loads(candidate_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid JSON mapping payload") from exc
        if not isinstance(payload, dict):
            raise ValueError("mapping payload must be a JSON object")
        return payload
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON mapping payload") from exc
    if not isinstance(payload, dict):
        raise ValueError("mapping payload must be a JSON object")
    return payload


def _read_manual_rows(path: str | Path) -> list[dict[str, Any]]:
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(target)
    text = target.read_text(encoding="utf-8-sig")
    if target.suffix.lower() == ".json":
        payload = json.loads(text)
        if isinstance(payload, dict):
            if "fixtures" in payload and isinstance(payload["fixtures"], list):
                return list(payload["fixtures"])
            if "rows" in payload and isinstance(payload["rows"], list):
                return list(payload["rows"])
            raise ValueError("manual JSON must contain a fixtures or rows list")
        if isinstance(payload, list):
            return payload
        raise ValueError("manual JSON must be a list or contain a fixtures list")
    return list(csv.DictReader(io.StringIO(text)))


def fetch_csv_upcoming_fixtures(
    *,
    season_code: str,
    division_code: str,
    target_date: str,
    client: httpx.Client | None = None,
    timeout: float = 20.0,
) -> list[UpcomingFixture]:
    """Fetch upcoming fixtures directly from football-data.co.uk CSV rows."""

    source_url = build_csv_url(season_code, division_code)
    close_client = False
    if client is None:
        client = httpx.Client(follow_redirects=True, timeout=timeout)
        close_client = True
    try:
        response = client.get(source_url)
        response.raise_for_status()
        text = response.text.lstrip("\ufeff")
    finally:
        if close_client:
            client.close()

    rows = list(csv.DictReader(io.StringIO(text)))
    competition_id, competition_name = _competition_identity(division_code)
    retrieved_at = _utc_now()
    fixtures: list[UpcomingFixture] = []
    for row in rows:
        match_date = _normalize_date(row.get("Date", ""))
        result_code = (row.get("FTR") or "").strip()
        if match_date == target_date and not result_code:
            home_team = (row.get("HomeTeam") or "").strip()
            away_team = (row.get("AwayTeam") or "").strip()
            fixtures.append(
                UpcomingFixture(
                    event_id=_build_event_id(match_date, home_team, away_team),
                    match_date=match_date,
                    kickoff_time=(row.get("Time") or "").strip() or None,
                    home_team=home_team,
                    away_team=away_team,
                    division_code=division_code,
                    competition_id=competition_id,
                    competition_name=competition_name,
                    source_kind="football-data-co-uk-csv",
                    source_ref=source_url,
                    kickoff_at_utc=_derive_kickoff_at_utc(
                        match_date=match_date,
                        kickoff_time=(row.get("Time") or "").strip() or None,
                        division_code=division_code,
                    ),
                    retrieved_at=retrieved_at,
                )
            )
    return fixtures


def fetch_football_data_org_fixtures(
    *,
    division_codes: Sequence[str],
    target_date: str,
    api_token: str | None = None,
    client: httpx.Client | None = None,
    timeout: float = 20.0,
) -> list[UpcomingFixture]:
    """Fetch upcoming fixtures from football-data.org."""

    api_token = api_token or os.environ.get("SPORTS_AI_FOOTBALL_DATA_TOKEN", "")
    if not api_token:
        raise ValueError("football-data.org fixture fetch requires SPORTS_AI_FOOTBALL_DATA_TOKEN")

    competition_codes = [
        DIVISION_TO_FOOTBALL_DATA_ORG[division]
        for division in division_codes
        if division in DIVISION_TO_FOOTBALL_DATA_ORG
    ]
    if not competition_codes:
        return []

    close_client = False
    if client is None:
        client = httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={"X-Auth-Token": api_token},
        )
        close_client = True
    try:
        response = client.get(
            "https://api.football-data.org/v4/matches",
            params={
                "competitions": ",".join(competition_codes),
                "dateFrom": target_date,
                "dateTo": target_date,
            },
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if close_client:
            client.close()

    reverse_map = {value: key for key, value in DIVISION_TO_FOOTBALL_DATA_ORG.items()}
    fixtures: list[UpcomingFixture] = []
    retrieved_at = _utc_now()
    for match in payload.get("matches", []):
        status = match.get("status")
        if status not in {"SCHEDULED", "TIMED"}:
            continue
        competition_code = (match.get("competition") or {}).get("code")
        division_code = reverse_map.get(competition_code)
        if not division_code:
            continue
        competition_id, competition_name = _competition_identity(division_code)
        utc_date = (match.get("utcDate") or "")[:10]
        home_team = (
            (match.get("homeTeam") or {}).get("shortName")
            or (match.get("homeTeam") or {}).get("name")
            or ""
        ).strip()
        away_team = (
            (match.get("awayTeam") or {}).get("shortName")
            or (match.get("awayTeam") or {}).get("name")
            or ""
        ).strip()
        fixtures.append(
            UpcomingFixture(
                event_id=_build_event_id(utc_date, home_team, away_team),
                match_date=utc_date,
                kickoff_time=(match.get("utcDate") or "")[11:16] or None,
                home_team=home_team,
                away_team=away_team,
                division_code=division_code,
                competition_id=competition_id,
                competition_name=competition_name,
                source_kind="football-data-org-api",
                source_ref="https://api.football-data.org/v4/matches",
                kickoff_at_utc=_normalize_utc_timestamp(match.get("utcDate")),
                source_fixture_id=str((match.get("id") or "")) or None,
                retrieved_at=retrieved_at,
            )
        )
    return fixtures


def fetch_api_football_fixtures(
    *,
    division_codes: Sequence[str],
    target_date: str,
    api_token: str | None = None,
    division_to_league_map: dict[str, int] | None = None,
    client: httpx.Client | None = None,
    timeout: float = 20.0,
) -> list[UpcomingFixture]:
    """Fetch upcoming fixtures from API-Football if configured."""

    api_token = api_token or os.environ.get("SPORTS_AI_API_FOOTBALL_TOKEN", "")
    if not api_token:
        raise ValueError("API-Football fixture fetch requires SPORTS_AI_API_FOOTBALL_TOKEN")

    if division_to_league_map is None:
        division_to_league_map = {
            key: int(value)
            for key, value in _load_json_mapping(os.environ.get("SPORTS_AI_API_FOOTBALL_LEAGUE_MAP")).items()
        }
    target_map = {division: division_to_league_map[division] for division in division_codes if division in division_to_league_map}
    if not target_map:
        raise ValueError("API-Football fixture fetch requires a division-to-league mapping")

    close_client = False
    if client is None:
        client = httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            headers={"x-apisports-key": api_token},
        )
        close_client = True
    fixtures: list[UpcomingFixture] = []
    retrieved_at = _utc_now()
    try:
        for division_code, league_id in target_map.items():
            response = client.get(
                "https://v3.football.api-sports.io/fixtures",
                params={
                    "date": target_date,
                    "league": league_id,
                    "season": int(target_date[:4]),
                },
            )
            response.raise_for_status()
            payload = response.json()
            competition_id, competition_name = _competition_identity(division_code)
            for match in payload.get("response", []):
                fixture_payload = match.get("fixture") or {}
                teams = match.get("teams") or {}
                utc_date = (fixture_payload.get("date") or "")[:10]
                if utc_date != target_date:
                    continue
                status_short = ((fixture_payload.get("status") or {}).get("short") or "").upper()
                if status_short not in {"NS", "TBD", "PST"}:
                    continue
                home_team = ((teams.get("home") or {}).get("name") or "").strip()
                away_team = ((teams.get("away") or {}).get("name") or "").strip()
                fixtures.append(
                    UpcomingFixture(
                        event_id=_build_event_id(utc_date, home_team, away_team),
                        match_date=utc_date,
                        kickoff_time=(fixture_payload.get("date") or "")[11:16] or None,
                        home_team=home_team,
                        away_team=away_team,
                        division_code=division_code,
                        competition_id=competition_id,
                        competition_name=competition_name,
                        source_kind="api-football-fixtures",
                        source_ref="https://v3.football.api-sports.io/fixtures",
                        kickoff_at_utc=_normalize_utc_timestamp(fixture_payload.get("date")),
                        source_fixture_id=str((fixture_payload.get("id") or "")) or None,
                        retrieved_at=retrieved_at,
                    )
                )
    finally:
        if close_client:
            client.close()
    return fixtures


def fetch_manual_upcoming_fixtures(
    *,
    file_path: str | Path,
    target_date: str,
    division_codes: Sequence[str],
) -> list[UpcomingFixture]:
    """Load upcoming fixtures from a manual CSV or JSON import."""

    rows = _read_manual_rows(file_path)
    allowed_divisions = set(division_codes)
    fixtures: list[UpcomingFixture] = []
    source_ref = str(Path(file_path))
    retrieved_at = _utc_now()
    for row in rows:
        division_code = str(row.get("division_code") or row.get("competition") or "").strip()
        if division_code not in allowed_divisions:
            continue
        match_date = str(row.get("match_date") or row.get("date") or "").strip()
        if match_date != target_date:
            continue
        home_team = str(row.get("home_team") or row.get("HomeTeam") or "").strip()
        away_team = str(row.get("away_team") or row.get("AwayTeam") or "").strip()
        if not home_team or not away_team:
            continue
        competition_id, competition_name = _competition_identity(division_code)
        fixtures.append(
            UpcomingFixture(
                event_id=str(row.get("event_id") or _build_event_id(match_date, home_team, away_team)),
                match_date=match_date,
                kickoff_time=str(row.get("kickoff_time") or row.get("time") or "").strip() or None,
                home_team=home_team,
                away_team=away_team,
                division_code=division_code,
                competition_id=str(row.get("competition_id") or competition_id),
                competition_name=str(row.get("competition_name") or competition_name),
                source_kind="manual-fixture-import",
                source_ref=source_ref,
                kickoff_at_utc=(
                    _normalize_utc_timestamp(
                        str(
                            row.get("kickoff_at_utc")
                            or row.get("kickoff_utc")
                            or row.get("utc_kickoff")
                            or ""
                        )
                    )
                    or _derive_kickoff_at_utc(
                        match_date=match_date,
                        kickoff_time=str(row.get("kickoff_time") or row.get("time") or "").strip() or None,
                        division_code=division_code,
                    )
                ),
                source_fixture_id=str(row.get("source_fixture_id") or row.get("fixture_id") or "").strip() or None,
                retrieved_at=retrieved_at,
            )
        )
    return fixtures


def discover_upcoming_fixtures(
    *,
    season_code: str,
    division_codes: Sequence[str],
    target_date: str,
    provider_priority: Sequence[str],
    manual_fixtures_path: str | Path | None = None,
    football_data_token: str | None = None,
    api_football_token: str | None = None,
    api_football_league_map: dict[str, int] | None = None,
    client: httpx.Client | None = None,
) -> tuple[list[UpcomingFixture], list[dict[str, Any]]]:
    """Discover fixtures across providers and return canonical deduplicated rows."""

    diagnostics: list[dict[str, Any]] = []
    canonical: dict[tuple[str, str, str, str], UpcomingFixture] = {}

    def add_fixtures(provider: str, division_code: str, fixtures: list[UpcomingFixture], source_ref: str | None) -> None:
        diagnostics.append(
            FixtureProviderDiagnostic(
                provider=provider,
                division_code=division_code,
                requested=True,
                status="OK" if fixtures else "EMPTY",
                fixtures_returned=len(fixtures),
                competitions_returned=len({fixture.competition_id for fixture in fixtures}),
                source_ref=source_ref,
            ).as_dict()
        )
        for fixture in fixtures:
            canonical.setdefault(_fixture_key(fixture), fixture)

    for provider in provider_priority:
        if provider == "api-football":
            try:
                api_fixtures = fetch_api_football_fixtures(
                    division_codes=division_codes,
                    target_date=target_date,
                    api_token=api_football_token,
                    division_to_league_map=api_football_league_map,
                    client=client,
                )
                by_division: dict[str, list[UpcomingFixture]] = {division: [] for division in division_codes}
                for fixture in api_fixtures:
                    by_division.setdefault(fixture.division_code, []).append(fixture)
                for division_code in division_codes:
                    add_fixtures("api-football", division_code, by_division.get(division_code, []), "https://v3.football.api-sports.io/fixtures")
            except Exception as exc:
                for division_code in division_codes:
                    diagnostics.append(
                        FixtureProviderDiagnostic(
                            provider="api-football",
                            division_code=division_code,
                            requested=True,
                            status="FAILED",
                            fixtures_returned=0,
                            competitions_returned=0,
                            failure_reason=f"{type(exc).__name__}: {exc}",
                            source_ref="https://v3.football.api-sports.io/fixtures",
                        ).as_dict()
                    )
        elif provider == "football-data-org":
            try:
                api_fixtures = fetch_football_data_org_fixtures(
                    division_codes=division_codes,
                    target_date=target_date,
                    api_token=football_data_token,
                    client=client,
                )
                by_division: dict[str, list[UpcomingFixture]] = {division: [] for division in division_codes}
                for fixture in api_fixtures:
                    by_division.setdefault(fixture.division_code, []).append(fixture)
                for division_code in division_codes:
                    add_fixtures("football-data-org", division_code, by_division.get(division_code, []), "https://api.football-data.org/v4/matches")
            except Exception as exc:
                for division_code in division_codes:
                    diagnostics.append(
                        FixtureProviderDiagnostic(
                            provider="football-data-org",
                            division_code=division_code,
                            requested=True,
                            status="FAILED",
                            fixtures_returned=0,
                            competitions_returned=0,
                            failure_reason=f"{type(exc).__name__}: {exc}",
                            source_ref="https://api.football-data.org/v4/matches",
                        ).as_dict()
                    )
        elif provider == "csv":
            for division_code in division_codes:
                source_url = build_csv_url(season_code, division_code)
                try:
                    fixtures = fetch_csv_upcoming_fixtures(
                        season_code=season_code,
                        division_code=division_code,
                        target_date=target_date,
                        client=client,
                    )
                    add_fixtures("football-data-co-uk", division_code, fixtures, source_url)
                except Exception as exc:
                    diagnostics.append(
                        FixtureProviderDiagnostic(
                            provider="football-data-co-uk",
                            division_code=division_code,
                            requested=True,
                            status="FAILED",
                            fixtures_returned=0,
                            competitions_returned=0,
                            failure_reason=f"{type(exc).__name__}: {exc}",
                            source_ref=source_url,
                        ).as_dict()
                    )
        elif provider == "manual":
            for division_code in division_codes:
                if not manual_fixtures_path:
                    diagnostics.append(
                        FixtureProviderDiagnostic(
                            provider="manual",
                            division_code=division_code,
                            requested=False,
                            status="SKIPPED",
                            fixtures_returned=0,
                            competitions_returned=0,
                            failure_reason="no manual fixture path configured",
                        ).as_dict()
                    )
                else:
                    try:
                        fixtures = [
                            fixture
                            for fixture in fetch_manual_upcoming_fixtures(
                                file_path=manual_fixtures_path,
                                target_date=target_date,
                                division_codes=division_codes,
                            )
                            if fixture.division_code == division_code
                        ]
                        add_fixtures("manual", division_code, fixtures, str(Path(manual_fixtures_path)))
                    except Exception as exc:
                        diagnostics.append(
                            FixtureProviderDiagnostic(
                                provider="manual",
                                division_code=division_code,
                                requested=True,
                                status="FAILED",
                                fixtures_returned=0,
                                competitions_returned=0,
                                failure_reason=f"{type(exc).__name__}: {exc}",
                                source_ref=str(Path(manual_fixtures_path)),
                            ).as_dict()
                        )

    fixtures = list(canonical.values())
    fixtures.sort(key=lambda fixture: (fixture.match_date, fixture.division_code, fixture.home_team, fixture.away_team))
    return fixtures, diagnostics
