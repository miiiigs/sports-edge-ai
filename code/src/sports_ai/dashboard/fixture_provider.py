"""Provider abstractions for current football fixtures."""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time as daytime, timedelta, timezone
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..data_sources.football_history import DIVISION_TO_COMPETITION
from ..data_sources.football_upcoming import DIVISION_TO_FOOTBALL_DATA_ORG
from ..locking import canonical_hash
from .models import (
    FIXTURE_STATUS_CANCELLED,
    FIXTURE_STATUS_FINISHED,
    FIXTURE_STATUS_HALFTIME,
    FIXTURE_STATUS_LIVE,
    FIXTURE_STATUS_POSTPONED,
    FIXTURE_STATUS_PRE_MATCH,
    FIXTURE_STATUS_SCHEDULED,
    NormalizedFixture,
    build_fixture_id,
    normalize_text_key,
    parse_iso_datetime,
    utc_now_iso,
)
from .provider_budget import (
    ACTION_CACHE_HIT,
    PRIORITY_ESSENTIAL,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    PROVIDER_API_FOOTBALL,
    ProviderRequestBudget,
    classify_provider_response,
    schedule_fixture_policy,
)

API_FOOTBALL_SOURCE_VERSION = "api_football_fixture_adapter_v1"
API_FOOTBALL_PROVIDER = "api-football"
API_FOOTBALL_FIXTURE_STATS_ENDPOINT = "https://v3.football.api-sports.io/fixtures/statistics"
API_FOOTBALL_LEAGUES_ENDPOINT = "https://v3.football.api-sports.io/leagues"
API_FOOTBALL_REFERENCE_CACHE_TTL_SECONDS = 24 * 60 * 60

FOOTBALL_DATA_ORG_SOURCE_VERSION = "football_data_org_fixture_adapter_v1"
FOOTBALL_DATA_ORG_PROVIDER = "football-data.org"
FOOTBALL_DATA_ORG_MATCHES_ENDPOINT = "https://api.football-data.org/v4/matches"
FOOTBALL_DATA_ORG_MATCH_DETAIL_ENDPOINT = "https://api.football-data.org/v4/matches/{match_id}"
FOOTBALL_DATA_ORG_PLAN_NOTE = (
    "football-data.org is the primary fixture source. Live state can be delayed depending on the active plan."
)
DEFAULT_FOOTBALL_DATA_COMPETITION_CODES = tuple(sorted(set(DIVISION_TO_FOOTBALL_DATA_ORG.values())))

_API_FOOTBALL_STATUS_MAP = {
    "NS": FIXTURE_STATUS_PRE_MATCH,
    "TBD": FIXTURE_STATUS_SCHEDULED,
    "PST": FIXTURE_STATUS_POSTPONED,
    "CANC": FIXTURE_STATUS_CANCELLED,
    "ABD": FIXTURE_STATUS_CANCELLED,
    "AWD": FIXTURE_STATUS_CANCELLED,
    "WO": FIXTURE_STATUS_CANCELLED,
    "HT": FIXTURE_STATUS_HALFTIME,
    "FT": FIXTURE_STATUS_FINISHED,
    "AET": FIXTURE_STATUS_FINISHED,
    "PEN": FIXTURE_STATUS_FINISHED,
    "1H": FIXTURE_STATUS_LIVE,
    "2H": FIXTURE_STATUS_LIVE,
    "ET": FIXTURE_STATUS_LIVE,
    "BT": FIXTURE_STATUS_LIVE,
    "INT": FIXTURE_STATUS_LIVE,
    "SUSP": FIXTURE_STATUS_LIVE,
}

_FOOTBALL_DATA_ORG_STATUS_MAP = {
    "SCHEDULED": FIXTURE_STATUS_SCHEDULED,
    "TIMED": FIXTURE_STATUS_PRE_MATCH,
    "IN_PLAY": FIXTURE_STATUS_LIVE,
    "LIVE": FIXTURE_STATUS_LIVE,
    "PAUSED": FIXTURE_STATUS_HALFTIME,
    "FINISHED": FIXTURE_STATUS_FINISHED,
    "POSTPONED": FIXTURE_STATUS_POSTPONED,
    "SUSPENDED": FIXTURE_STATUS_POSTPONED,
    "CANCELLED": FIXTURE_STATUS_CANCELLED,
    "AWARDED": FIXTURE_STATUS_CANCELLED,
}


class FixtureProviderError(RuntimeError):
    """Raised when fixture retrieval fails."""


class FixtureProviderNotConfiguredError(FixtureProviderError):
    """Raised when a provider cannot be used because required config is absent."""


def _coerce_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class AuthoritativeFixtureStatistics:
    fixture_id: str
    provider: str
    provider_fixture_id: str
    home_corners: int | None
    away_corners: int | None
    total_corners: int | None
    source_timestamp: str | None
    captured_at: str


@dataclass(frozen=True)
class FixtureProviderConfig:
    display_timezone: str = "Asia/Manila"
    timeout_seconds: float = 20.0
    league_map_env_var: str = "SPORTS_AI_API_FOOTBALL_LEAGUE_MAP"
    token_env_var: str = "SPORTS_AI_API_FOOTBALL_TOKEN"
    football_data_token_env_var: str = "SPORTS_AI_FOOTBALL_DATA_TOKEN"
    football_data_competitions_env_var: str = "SPORTS_AI_FOOTBALL_DATA_COMPETITIONS"
    live_cache_ttl_seconds: int = 60
    today_cache_ttl_seconds: int = 150
    schedule_cache_ttl_seconds: int = 600
    detail_cache_ttl_seconds: int = 300
    statistics_cache_ttl_seconds: int = 300
    kickoff_match_tolerance_seconds: int = 1800


class FixtureProvider(ABC):
    """Normalized fixture provider contract."""

    @abstractmethod
    def provider_name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def configured(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def get_live_fixtures(self) -> list[NormalizedFixture]:
        raise NotImplementedError

    @abstractmethod
    def get_fixtures_for_date(self, target_date: date) -> list[NormalizedFixture]:
        raise NotImplementedError

    @abstractmethod
    def get_upcoming_fixtures(
        self,
        *,
        from_utc: datetime,
        to_utc: datetime,
    ) -> list[NormalizedFixture]:
        raise NotImplementedError

    @abstractmethod
    def get_fixture(self, fixture_id: str) -> NormalizedFixture | None:
        raise NotImplementedError


class FootballDataOrgFixtureProvider(FixtureProvider):
    """Current fixture provider backed by football-data.org."""

    def __init__(self, config: FixtureProviderConfig | None = None):
        self._config = FixtureProviderConfig() if config is None else config
        self._query_log: list[dict[str, Any]] = []
        self._query_log_lock = Lock()
        self._response_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._response_cache_lock = Lock()
        self._fixture_cache: dict[str, NormalizedFixture] = {}
        self._fixture_cache_lock = Lock()
        self._division_reverse_map = {
            competition_code: division_code
            for division_code, competition_code in DIVISION_TO_FOOTBALL_DATA_ORG.items()
        }

    def provider_name(self) -> str:
        return FOOTBALL_DATA_ORG_PROVIDER

    def configured(self) -> bool:
        return bool(os.environ.get(self._config.football_data_token_env_var, "").strip())

    def _token(self) -> str:
        token = os.environ.get(self._config.football_data_token_env_var, "").strip()
        if not token:
            raise FixtureProviderNotConfiguredError(
                f"missing required environment variable {self._config.football_data_token_env_var}"
            )
        return token

    def _client(self) -> httpx.Client:
        return httpx.Client(
            follow_redirects=True,
            timeout=self._config.timeout_seconds,
            headers={"X-Auth-Token": self._token()},
        )

    def _configured_competition_codes(self) -> tuple[str, ...]:
        raw_value = os.environ.get(self._config.football_data_competitions_env_var, "").strip()
        if not raw_value:
            return DEFAULT_FOOTBALL_DATA_COMPETITION_CODES
        values = [item.strip().upper() for item in raw_value.split(",") if item.strip()]
        mapped: list[str] = []
        for value in values:
            mapped.append(DIVISION_TO_FOOTBALL_DATA_ORG.get(value, value))
        deduped = tuple(sorted(set(mapped)))
        return deduped or DEFAULT_FOOTBALL_DATA_COMPETITION_CODES

    def reset_sync_diagnostics(self) -> None:
        with self._query_log_lock:
            self._query_log = []

    def sync_diagnostics(self) -> dict[str, Any]:
        with self._query_log_lock:
            queries = [dict(entry) for entry in self._query_log]
        errors = [entry for entry in queries if entry.get("errors")]
        competition_codes = sorted(
            {
                code
                for entry in queries
                for code in entry.get("competition_codes", [])
                if code
            }
        )
        with self._response_cache_lock:
            cache_entries = len(self._response_cache)
        return {
            "provider": FOOTBALL_DATA_ORG_PROVIDER,
            "query_count": len(queries),
            "competition_codes_queried": competition_codes,
            "provider_seasons_queried": [],
            "has_errors": bool(errors),
            "last_error": None if not errors else dict(errors[-1]),
            "queries": queries,
            "plan_note": FOOTBALL_DATA_ORG_PLAN_NOTE,
            "live_status_note": "Live and in-play status can be delayed on lower football-data.org plans.",
            "cache_entries": cache_entries,
        }

    def _cache_key(self, endpoint: str, params: dict[str, Any] | None) -> str:
        normalized_params = {}
        for key, value in sorted((params or {}).items()):
            normalized_params[str(key)] = value
        return json.dumps({"endpoint": endpoint, "params": normalized_params}, sort_keys=True)

    def _cache_get(self, key: str, *, ttl_seconds: int) -> dict[str, Any] | None:
        with self._response_cache_lock:
            entry = self._response_cache.get(key)
        if entry is None:
            return None
        created_at, payload = entry
        if monotonic() - created_at > ttl_seconds:
            with self._response_cache_lock:
                self._response_cache.pop(key, None)
            return None
        return payload

    def _cache_set(self, key: str, payload: dict[str, Any]) -> None:
        with self._response_cache_lock:
            self._response_cache[key] = (monotonic(), payload)

    def _store_normalized_fixtures(self, fixtures: list[NormalizedFixture]) -> None:
        with self._fixture_cache_lock:
            for fixture in fixtures:
                self._fixture_cache[fixture.fixture_id] = fixture

    def _record_query(
        self,
        *,
        query_kind: str,
        endpoint: str,
        params: dict[str, Any] | None,
        competition_codes: tuple[str, ...],
        raw_fixture_count: int,
        response_status: str,
        http_status: int,
        errors: dict[str, Any] | None,
        cache_hit: bool,
    ) -> None:
        entry = {
            "query_timestamp": utc_now_iso(),
            "provider": FOOTBALL_DATA_ORG_PROVIDER,
            "query_kind": query_kind,
            "endpoint": endpoint,
            "http_status": http_status,
            "response_status": response_status,
            "competition_codes": list(competition_codes),
            "params": {} if params is None else {str(key): value for key, value in params.items()},
            "response_results": raw_fixture_count,
            "raw_fixture_count": raw_fixture_count,
            "errors": errors or {},
            "cache_hit": cache_hit,
        }
        with self._query_log_lock:
            self._query_log.append(entry)

    def _request_json(
        self,
        *,
        endpoint: str,
        params: dict[str, Any] | None,
        query_kind: str,
        cache_ttl_seconds: int,
        competition_codes: tuple[str, ...],
    ) -> dict[str, Any]:
        cache_key = self._cache_key(endpoint, params)
        cached = self._cache_get(cache_key, ttl_seconds=cache_ttl_seconds)
        if cached is not None:
            raw_count = len(cached.get("matches", [])) if isinstance(cached.get("matches"), list) else 1
            self._record_query(
                query_kind=query_kind,
                endpoint=endpoint,
                params=params,
                competition_codes=competition_codes,
                raw_fixture_count=raw_count,
                response_status="OK",
                http_status=200,
                errors=None,
                cache_hit=True,
            )
            return cached

        try:
            with self._client() as client:
                response = client.get(endpoint, params=params)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            details = self._http_error_details(exc.response)
            self._record_query(
                query_kind=query_kind,
                endpoint=endpoint,
                params=params,
                competition_codes=competition_codes,
                raw_fixture_count=0,
                response_status="ERROR",
                http_status=int(exc.response.status_code),
                errors=details,
                cache_hit=False,
            )
            message = details.get("message") or f"HTTP {exc.response.status_code}"
            raise FixtureProviderError(f"football-data.org request failed: {message}") from exc
        except httpx.HTTPError as exc:
            details = {"message": str(exc)}
            self._record_query(
                query_kind=query_kind,
                endpoint=endpoint,
                params=params,
                competition_codes=competition_codes,
                raw_fixture_count=0,
                response_status="ERROR",
                http_status=0,
                errors=details,
                cache_hit=False,
            )
            raise FixtureProviderError(f"football-data.org request failed: {exc}") from exc

        raw_count = len(payload.get("matches", [])) if isinstance(payload.get("matches"), list) else 1
        self._cache_set(cache_key, payload)
        self._record_query(
            query_kind=query_kind,
            endpoint=endpoint,
            params=params,
            competition_codes=competition_codes,
            raw_fixture_count=raw_count,
            response_status="OK",
            http_status=int(response.status_code),
            errors=None,
            cache_hit=False,
        )
        return payload

    @staticmethod
    def _http_error_details(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        message = None
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("error")
        return {
            "message": str(message or response.text[:200] or f"HTTP {response.status_code}"),
            "status_code": int(response.status_code),
        }

    def _request_matches(
        self,
        *,
        params: dict[str, Any],
        query_kind: str,
        cache_ttl_seconds: int,
    ) -> list[dict[str, Any]]:
        competition_codes = self._configured_competition_codes()
        merged_params = {
            "competitions": ",".join(competition_codes),
            **params,
        }
        payload = self._request_json(
            endpoint=FOOTBALL_DATA_ORG_MATCHES_ENDPOINT,
            params=merged_params,
            query_kind=query_kind,
            cache_ttl_seconds=cache_ttl_seconds,
            competition_codes=competition_codes,
        )
        matches = payload.get("matches")
        if not isinstance(matches, list):
            raise FixtureProviderError("unexpected football-data.org matches response payload")
        return matches

    def _request_match_detail(self, provider_fixture_id: str) -> dict[str, Any] | None:
        payload = self._request_json(
            endpoint=FOOTBALL_DATA_ORG_MATCH_DETAIL_ENDPOINT.format(match_id=provider_fixture_id),
            params=None,
            query_kind="fixture_detail",
            cache_ttl_seconds=self._config.detail_cache_ttl_seconds,
            competition_codes=(),
        )
        return payload if isinstance(payload, dict) else None

    def _season_text(self, payload: dict[str, Any], kickoff_utc: str) -> str:
        season_payload = payload.get("season") or {}
        start_date = str(season_payload.get("startDate") or "").strip()
        if len(start_date) >= 4 and start_date[:4].isdigit():
            return start_date[:4]
        season_id = str(season_payload.get("id") or "").strip()
        return season_id or kickoff_utc[:4]

    @staticmethod
    def _score_value(score_payload: dict[str, Any], *paths: tuple[str, str]) -> int | None:
        for bucket, team_key in paths:
            bucket_payload = score_payload.get(bucket)
            if isinstance(bucket_payload, dict):
                value = bucket_payload.get(team_key)
                if value is not None:
                    return int(value)
        return None

    def _normalize_fixture(self, payload: dict[str, Any]) -> NormalizedFixture | None:
        competition_payload = payload.get("competition") or {}
        competition_code = str(competition_payload.get("code") or "").strip().upper()
        division_code = self._division_reverse_map.get(competition_code)
        competition_id, default_competition_name = DIVISION_TO_COMPETITION.get(
            division_code,
            (
                competition_code or str(competition_payload.get("id") or "UNKNOWN"),
                str(competition_payload.get("name") or competition_code or "Unknown Competition"),
            ),
        )

        raw_kickoff = str(payload.get("utcDate") or "").strip()
        if not raw_kickoff:
            return None
        kickoff_utc = raw_kickoff.replace("Z", "+00:00")
        status_raw = str(payload.get("status") or "").strip().upper()
        normalized_status = _FOOTBALL_DATA_ORG_STATUS_MAP.get(status_raw, FIXTURE_STATUS_PRE_MATCH)

        home_team = payload.get("homeTeam") or {}
        away_team = payload.get("awayTeam") or {}
        home_name = str(home_team.get("shortName") or home_team.get("tla") or home_team.get("name") or "").strip()
        away_name = str(away_team.get("shortName") or away_team.get("tla") or away_team.get("name") or "").strip()
        provider_fixture_id = str(payload.get("id") or "").strip() or None
        fixture_id = build_fixture_id(
            provider=FOOTBALL_DATA_ORG_PROVIDER,
            provider_fixture_id=provider_fixture_id,
            kickoff_utc=kickoff_utc,
            home_team_name=home_name,
            away_team_name=away_name,
        )
        score_payload = payload.get("score") or {}
        last_updated = str(payload.get("lastUpdated") or "").strip().replace("Z", "+00:00") or utc_now_iso()

        return NormalizedFixture(
            fixture_id=fixture_id,
            provider_fixture_id=provider_fixture_id,
            provider=FOOTBALL_DATA_ORG_PROVIDER,
            competition_id=str(competition_id),
            competition_name=str(competition_payload.get("name") or default_competition_name),
            division_code=division_code,
            season=self._season_text(payload, kickoff_utc),
            home_team_id=str(home_team.get("id") or "").strip() or None,
            home_team_name=home_name,
            away_team_id=str(away_team.get("id") or "").strip() or None,
            away_team_name=away_name,
            kickoff_utc=kickoff_utc,
            status=normalized_status,
            elapsed_minute=None,
            home_score=self._score_value(
                score_payload,
                ("fullTime", "home"),
                ("regularTime", "home"),
                ("halfTime", "home"),
            ),
            away_score=self._score_value(
                score_payload,
                ("fullTime", "away"),
                ("regularTime", "away"),
                ("halfTime", "away"),
            ),
            last_provider_update=last_updated,
            source_version=FOOTBALL_DATA_ORG_SOURCE_VERSION,
            provider_payload=payload,
        )

    @staticmethod
    def _dedupe(fixtures: list[NormalizedFixture]) -> list[NormalizedFixture]:
        deduped: dict[str, NormalizedFixture] = {}
        for fixture in fixtures:
            deduped[fixture.fixture_id] = fixture
        return sorted(deduped.values(), key=lambda item: (item.kickoff_utc, item.fixture_id))

    def _normalize_rows(self, rows: list[dict[str, Any]]) -> list[NormalizedFixture]:
        fixtures = [fixture for row in rows if (fixture := self._normalize_fixture(row)) is not None]
        fixtures = self._dedupe(fixtures)
        self._store_normalized_fixtures(fixtures)
        return fixtures

    def _local_date_for_fixture(self, fixture: NormalizedFixture) -> date:
        timezone_name = ZoneInfo(self._config.display_timezone)
        return datetime.fromisoformat(fixture.kickoff_utc).astimezone(timezone_name).date()

    def _today_local(self) -> date:
        return datetime.now(timezone.utc).astimezone(ZoneInfo(self._config.display_timezone)).date()

    def get_live_fixtures(self) -> list[NormalizedFixture]:
        if not self.configured():
            raise FixtureProviderNotConfiguredError("football-data.org is not configured")
        now_utc = datetime.now(timezone.utc)
        rows = self._request_matches(
            params={
                "dateFrom": (now_utc - timedelta(days=1)).date().isoformat(),
                "dateTo": (now_utc + timedelta(days=1)).date().isoformat(),
            },
            query_kind="live_window",
            cache_ttl_seconds=self._config.live_cache_ttl_seconds,
        )
        fixtures = self._normalize_rows(rows)
        return [
            fixture
            for fixture in fixtures
            if fixture.status in {FIXTURE_STATUS_LIVE, FIXTURE_STATUS_HALFTIME}
        ]

    def get_fixtures_for_date(self, target_date: date) -> list[NormalizedFixture]:
        if not self.configured():
            raise FixtureProviderNotConfiguredError("football-data.org is not configured")
        timezone_name = ZoneInfo(self._config.display_timezone)
        local_start = datetime.combine(target_date, daytime.min, tzinfo=timezone_name)
        local_end = datetime.combine(target_date + timedelta(days=1), daytime.min, tzinfo=timezone_name)
        ttl_seconds = (
            self._config.today_cache_ttl_seconds
            if target_date == self._today_local()
            else self._config.schedule_cache_ttl_seconds
        )
        rows = self._request_matches(
            params={
                "dateFrom": local_start.astimezone(timezone.utc).date().isoformat(),
                "dateTo": (local_end - timedelta(seconds=1)).astimezone(timezone.utc).date().isoformat(),
            },
            query_kind="fixture_date",
            cache_ttl_seconds=ttl_seconds,
        )
        fixtures = self._normalize_rows(rows)
        return [
            fixture
            for fixture in fixtures
            if self._local_date_for_fixture(fixture) == target_date
        ]

    def get_upcoming_fixtures(
        self,
        *,
        from_utc: datetime,
        to_utc: datetime,
    ) -> list[NormalizedFixture]:
        if not self.configured():
            raise FixtureProviderNotConfiguredError("football-data.org is not configured")
        lower = from_utc.astimezone(timezone.utc)
        upper = to_utc.astimezone(timezone.utc)
        rows = self._request_matches(
            params={
                "dateFrom": lower.date().isoformat(),
                "dateTo": upper.date().isoformat(),
            },
            query_kind="upcoming_window",
            cache_ttl_seconds=self._config.schedule_cache_ttl_seconds,
        )
        fixtures = self._normalize_rows(rows)
        return [
            fixture
            for fixture in fixtures
            if lower <= datetime.fromisoformat(fixture.kickoff_utc) <= upper
            and fixture.status in {FIXTURE_STATUS_SCHEDULED, FIXTURE_STATUS_PRE_MATCH}
        ]

    def get_fixture(self, fixture_id: str) -> NormalizedFixture | None:
        if not fixture_id.startswith(f"{FOOTBALL_DATA_ORG_PROVIDER}:"):
            return None
        with self._fixture_cache_lock:
            cached = self._fixture_cache.get(fixture_id)
        if cached is not None:
            return cached
        provider_fixture_id = fixture_id.split(":", 1)[1]
        if provider_fixture_id.startswith("generated:"):
            return None
        payload = self._request_match_detail(provider_fixture_id)
        if payload is None:
            return None
        fixture = self._normalize_fixture(payload)
        if fixture is not None:
            self._store_normalized_fixtures([fixture])
        return fixture


class ApiFootballFixtureProvider(FixtureProvider):
    """Legacy fixture provider backed by API-Football."""

    def __init__(self, config: FixtureProviderConfig | None = None):
        self._config = FixtureProviderConfig() if config is None else config
        self._cached_league_map: dict[str, int] | None = None
        self._query_log: list[dict[str, Any]] = []
        self._query_log_lock = Lock()
        self._statistics_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._statistics_cache_lock = Lock()
        self._budget: ProviderRequestBudget | None = None

    def attach_request_budget(self, budget: ProviderRequestBudget) -> None:
        """Attach the shared provider request budget (gate + cache + governor)."""
        self._budget = budget

    def provider_name(self) -> str:
        return API_FOOTBALL_PROVIDER

    def configured(self) -> bool:
        return bool(os.environ.get(self._config.token_env_var, "").strip()) and bool(self._league_map_path())

    def _league_map_path(self) -> Path | None:
        raw_value = os.environ.get(self._config.league_map_env_var, "").strip()
        if not raw_value:
            return None
        path = Path(raw_value)
        if path.exists():
            return path
        repo_relative = Path(__file__).resolve().parents[4] / raw_value
        return repo_relative if repo_relative.exists() else None

    def _token(self) -> str:
        token = os.environ.get(self._config.token_env_var, "").strip()
        if not token:
            raise FixtureProviderNotConfiguredError(
                f"missing required environment variable {self._config.token_env_var}"
            )
        return token

    def _league_map(self) -> dict[str, int]:
        if self._cached_league_map is not None:
            return dict(self._cached_league_map)
        path = self._league_map_path()
        if path is None:
            raise FixtureProviderNotConfiguredError(
                f"missing readable league map from {self._config.league_map_env_var}"
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise FixtureProviderError("API-Football league map must be a JSON object")
        self._cached_league_map = {
            str(key): int(value)
            for key, value in payload.items()
        }
        return dict(self._cached_league_map)

    def _client(self) -> httpx.Client:
        return httpx.Client(
            follow_redirects=True,
            timeout=self._config.timeout_seconds,
            headers={"x-apisports-key": self._token()},
        )

    def reset_sync_diagnostics(self) -> None:
        with self._query_log_lock:
            self._query_log = []

    def sync_diagnostics(self) -> dict[str, Any]:
        with self._query_log_lock:
            queries = [dict(entry) for entry in self._query_log]
        errors = [entry for entry in queries if entry.get("errors")]
        league_ids = sorted(
            {
                int(entry["provider_league_id"])
                for entry in queries
                if entry.get("provider_league_id") is not None
            }
        )
        seasons = sorted(
            {
                str(entry["provider_season"])
                for entry in queries
                if entry.get("provider_season") not in {None, ""}
            }
        )
        return {
            "provider": API_FOOTBALL_PROVIDER,
            "query_count": len(queries),
            "league_ids_queried": league_ids,
            "provider_seasons_queried": seasons,
            "has_errors": bool(errors),
            "last_error": None if not errors else dict(errors[-1]),
            "queries": queries,
        }

    def _fixture_request_policy(self, params: dict[str, Any]) -> tuple[int, str]:
        """Endpoint-specific TTL + priority for a fixtures request by lifecycle."""
        if "live" in params:
            return 90, PRIORITY_ESSENTIAL
        if "id" in params:
            return 300, PRIORITY_ESSENTIAL
        raw_date = params.get("date")
        if raw_date:
            try:
                target_date = date.fromisoformat(str(raw_date))
            except ValueError:
                target_date = self._today_local()
            return schedule_fixture_policy(target_date, self._today_local())
        return 300, PRIORITY_NORMAL

    def _record_fixture_query(
        self,
        *,
        params: dict[str, Any],
        payload: dict[str, Any],
        http_status: int,
        response_status: str,
        errors: dict[str, Any],
    ) -> None:
        rows = payload.get("response")
        diagnostic_entry = {
            "query_timestamp": utc_now_iso(),
            "provider": API_FOOTBALL_PROVIDER,
            "http_status": int(http_status),
            "response_status": response_status,
            "provider_league_id": params.get("league"),
            "provider_season": params.get("season"),
            "params": {str(key): value for key, value in params.items()},
            "response_results": int(payload.get("results") or 0) if isinstance(payload, dict) else 0,
            "raw_fixture_count": len(rows) if isinstance(rows, list) else 0,
            "errors": errors,
        }
        with self._query_log_lock:
            self._query_log.append(diagnostic_entry)

    def _request_fixtures(
        self,
        *,
        params: dict[str, Any],
        priority_override: str | None = None,
    ) -> list[dict[str, Any]]:
        ttl_seconds, priority = self._fixture_request_policy(params)
        if priority_override is not None:
            priority = priority_override
        if self._budget is not None:
            decision = self._budget.issue(
                provider=PROVIDER_API_FOOTBALL,
                endpoint="fixtures",
                params=params,
                ttl_seconds=ttl_seconds,
                priority=priority,
                wait_for_rate_slot=True,
            )
            if decision.action == ACTION_CACHE_HIT and decision.cached_payload is not None:
                self._record_fixture_query(
                    params=params,
                    payload=decision.cached_payload,
                    http_status=200,
                    response_status="CACHE_HIT",
                    errors={},
                )
                rows = decision.cached_payload.get("response")
                return list(rows) if isinstance(rows, list) else []
            if not decision.allow:
                blocked_reason = decision.reason or "request blocked"
                self._record_fixture_query(
                    params=params,
                    payload={"response": [], "results": 0},
                    http_status=0,
                    response_status=decision.action,
                    errors={params.get("league") or "fixtures": blocked_reason},
                )
                return []

        with self._client() as client:
            response = client.get("https://v3.football.api-sports.io/fixtures", params=params)
            try:
                payload = response.json()
            except ValueError:
                payload = {"errors": {"message": response.text[:200]}, "response": [], "results": 0}
        errors_payload = payload.get("errors")
        errors = errors_payload if isinstance(errors_payload, dict) else {}
        failure_state, failure_message, _ = classify_provider_response(
            http_status=int(response.status_code),
            payload=payload,
            response_headers=response.headers,
        )
        if self._budget is not None:
            if failure_state is not None:
                self._budget.finish_failure(
                    provider=PROVIDER_API_FOOTBALL,
                    endpoint="fixtures",
                    params=params,
                    state=failure_state,
                    message=failure_message,
                    response_headers=response.headers,
                )
            else:
                provider_timestamp = None
                rows_payload = payload.get("response")
                if isinstance(rows_payload, list) and rows_payload:
                    fixture_payload = rows_payload[0].get("fixture") or {}
                    provider_timestamp = str(fixture_payload.get("update") or "").strip() or None
                self._budget.finish_success(
                    provider=PROVIDER_API_FOOTBALL,
                    endpoint="fixtures",
                    params=params,
                    payload=payload,
                    provider_timestamp=provider_timestamp,
                    ttl_seconds=ttl_seconds,
                    response_headers=response.headers,
                )
        rows = payload.get("response")
        if not isinstance(rows, list):
            raise FixtureProviderError("unexpected API-Football fixtures response payload")
        self._record_fixture_query(
            params=params,
            payload=payload,
            http_status=int(response.status_code),
            response_status="OK" if not errors else "ERROR",
            errors=errors,
        )
        return rows

    def _request_fixture_statistics(
        self,
        *,
        provider_fixture_id: str,
        priority_override: str | None = None,
    ) -> list[dict[str, Any]]:
        cache_key = str(provider_fixture_id)
        if self._budget is None:
            with self._statistics_cache_lock:
                cached = self._statistics_cache.get(cache_key)
            if cached is not None and monotonic() - cached[0] <= self._config.statistics_cache_ttl_seconds:
                return list(cached[1])
        else:
            decision = self._budget.issue(
                provider=PROVIDER_API_FOOTBALL,
                endpoint="fixtures/statistics",
                params={"fixture": provider_fixture_id},
                ttl_seconds=self._config.statistics_cache_ttl_seconds,
                priority=priority_override or PRIORITY_ESSENTIAL,
                wait_for_rate_slot=priority_override == PRIORITY_LOW,
            )
            if decision.action == ACTION_CACHE_HIT and decision.cached_payload is not None:
                rows = decision.cached_payload.get("response")
                if isinstance(rows, list):
                    return list(rows)
            if not decision.allow:
                return []

        with self._client() as client:
            response = client.get(API_FOOTBALL_FIXTURE_STATS_ENDPOINT, params={"fixture": provider_fixture_id})
            try:
                payload = response.json()
            except ValueError:
                payload = {"errors": {"message": response.text[:200]}, "response": [], "results": 0}
        errors_payload = payload.get("errors")
        errors = errors_payload if isinstance(errors_payload, dict) else {}
        failure_state, failure_message, _ = classify_provider_response(
            http_status=int(response.status_code),
            payload=payload,
            response_headers=response.headers,
        )
        if self._budget is not None:
            if failure_state is not None:
                self._budget.finish_failure(
                    provider=PROVIDER_API_FOOTBALL,
                    endpoint="fixtures/statistics",
                    params={"fixture": provider_fixture_id},
                    state=failure_state,
                    message=failure_message,
                    response_headers=response.headers,
                )
            else:
                provider_timestamp = None
                rows_payload = payload.get("response")
                if isinstance(rows_payload, list) and rows_payload:
                    provider_timestamp = str((rows_payload[0].get("update") or "")).strip() or None
                self._budget.finish_success(
                    provider=PROVIDER_API_FOOTBALL,
                    endpoint="fixtures/statistics",
                    params={"fixture": provider_fixture_id},
                    payload=payload,
                    provider_timestamp=provider_timestamp,
                    ttl_seconds=self._config.statistics_cache_ttl_seconds,
                    response_headers=response.headers,
                )
        rows = payload.get("response")
        if not isinstance(rows, list):
            raise FixtureProviderError("unexpected API-Football fixture statistics response payload")
        diagnostic_entry = {
            "query_timestamp": utc_now_iso(),
            "provider": API_FOOTBALL_PROVIDER,
            "http_status": int(response.status_code),
            "response_status": "OK" if not errors else "ERROR",
            "provider_fixture_id": provider_fixture_id,
            "params": {"fixture": provider_fixture_id},
            "response_results": int(payload.get("results") or 0),
            "raw_fixture_count": len(rows),
            "errors": errors,
        }
        with self._query_log_lock:
            self._query_log.append(diagnostic_entry)
        with self._statistics_cache_lock:
            self._statistics_cache[cache_key] = (monotonic(), list(rows))
        return list(rows)

    def _request_league_reference_rows(self, *, search: str, season: int) -> list[dict[str, Any]]:
        params = {"search": str(search).strip(), "season": int(season)}
        if not params["search"]:
            raise FixtureProviderError("history coverage search must not be empty")

        if self._budget is not None:
            decision = self._budget.issue(
                provider=PROVIDER_API_FOOTBALL,
                endpoint="leagues",
                params=params,
                ttl_seconds=API_FOOTBALL_REFERENCE_CACHE_TTL_SECONDS,
                priority=PRIORITY_LOW,
                wait_for_rate_slot=True,
            )
            if decision.action == ACTION_CACHE_HIT and decision.cached_payload is not None:
                rows = decision.cached_payload.get("response")
                return list(rows) if isinstance(rows, list) else []
            if not decision.allow:
                return []

        with self._client() as client:
            response = client.get(API_FOOTBALL_LEAGUES_ENDPOINT, params=params)
            try:
                payload = response.json()
            except ValueError:
                payload = {"errors": {"message": response.text[:200]}, "response": [], "results": 0}

        errors_payload = payload.get("errors")
        errors = errors_payload if isinstance(errors_payload, dict) else {}
        failure_state, failure_message, _ = classify_provider_response(
            http_status=int(response.status_code),
            payload=payload,
            response_headers=response.headers,
        )
        if self._budget is not None:
            if failure_state is not None:
                self._budget.finish_failure(
                    provider=PROVIDER_API_FOOTBALL,
                    endpoint="leagues",
                    params=params,
                    state=failure_state,
                    message=failure_message,
                    response_headers=response.headers,
                )
            else:
                self._budget.finish_success(
                    provider=PROVIDER_API_FOOTBALL,
                    endpoint="leagues",
                    params=params,
                    payload=payload,
                    provider_timestamp=None,
                    ttl_seconds=API_FOOTBALL_REFERENCE_CACHE_TTL_SECONDS,
                    response_headers=response.headers,
                )

        rows = payload.get("response")
        if not isinstance(rows, list):
            raise FixtureProviderError("unexpected API-Football leagues response payload")
        with self._query_log_lock:
            self._query_log.append(
                {
                    "query_timestamp": utc_now_iso(),
                    "provider": API_FOOTBALL_PROVIDER,
                    "query_kind": "historical_league_reference",
                    "endpoint": "leagues",
                    "http_status": int(response.status_code),
                    "response_status": "OK" if not errors else "ERROR",
                    "params": params,
                    "response_results": int(payload.get("results") or 0),
                    "errors": errors,
                }
            )
        return list(rows)

    def get_historical_league_candidates(self, *, search: str, season: int) -> list[dict[str, Any]]:
        """Low-priority reference lookup for an offline historical coverage probe."""
        return self._request_league_reference_rows(search=search, season=season)

    def get_historical_fixtures(self, *, league_id: int, season: int) -> list[dict[str, Any]]:
        """Low-priority raw fixture list for one league-season backfill probe."""
        return self._request_fixtures(
            params={"league": int(league_id), "season": int(season)},
            priority_override=PRIORITY_LOW,
        )

    def get_historical_fixture_statistics(self, *, provider_fixture_id: str) -> list[dict[str, Any]]:
        """Low-priority fixture statistics read for offline historical backfill."""
        return self._request_fixture_statistics(
            provider_fixture_id=str(provider_fixture_id),
            priority_override=PRIORITY_LOW,
        )

    def _mapping_cache_key(self, fixture_context: dict[str, Any]) -> str:
        return canonical_hash(
            {
                "fixture_id": fixture_context.get("fixture_id"),
                "competition_id": fixture_context.get("competition_id"),
                "division_code": fixture_context.get("division_code"),
                "season": fixture_context.get("season"),
                "kickoff_utc": fixture_context.get("kickoff_utc"),
                "home_team_name": fixture_context.get("home_team_name"),
                "away_team_name": fixture_context.get("away_team_name"),
            }
        )

    def _resolve_statistics_fixture_id(self, fixture_context: dict[str, Any]) -> str:
        persisted_provider = str(fixture_context.get("statistics_provider") or "").strip().lower()
        persisted_fixture_id = str(fixture_context.get("statistics_provider_fixture_id") or "").strip()
        if persisted_provider == API_FOOTBALL_PROVIDER and persisted_fixture_id:
            return persisted_fixture_id
        provider_name = str(fixture_context.get("provider") or "").strip().lower()
        provider_fixture_id = str(fixture_context.get("provider_fixture_id") or "").strip()
        if provider_name == API_FOOTBALL_PROVIDER and provider_fixture_id and not provider_fixture_id.startswith("generated:"):
            return provider_fixture_id

        division_code = str(fixture_context.get("division_code") or "").strip()
        league_id = self._league_map().get(division_code)
        if league_id is None:
            raise FixtureProviderError("STATS_FIXTURE_UNMATCHED")
        kickoff = parse_iso_datetime(str(fixture_context["kickoff_utc"]))
        season_value = _coerce_int(fixture_context.get("season")) or kickoff.year
        expected_home = normalize_text_key(str(fixture_context.get("home_team_name") or ""))
        expected_away = normalize_text_key(str(fixture_context.get("away_team_name") or ""))
        rows = self._request_fixtures(
            params={
                "date": kickoff.date().isoformat(),
                "league": league_id,
                "season": season_value,
            }
        )
        matches: list[str] = []
        for row in rows:
            fixture_payload = row.get("fixture") or {}
            teams_payload = row.get("teams") or {}
            candidate_fixture_id = str(fixture_payload.get("id") or "").strip()
            fixture_date = str(fixture_payload.get("date") or "").strip()
            if not candidate_fixture_id or not fixture_date:
                continue
            candidate_kickoff = parse_iso_datetime(fixture_date)
            if abs((candidate_kickoff - kickoff).total_seconds()) > self._config.kickoff_match_tolerance_seconds:
                continue
            home_name = normalize_text_key(str(((teams_payload.get("home") or {}).get("name")) or ""))
            away_name = normalize_text_key(str(((teams_payload.get("away") or {}).get("name")) or ""))
            if home_name != expected_home or away_name != expected_away:
                continue
            matches.append(candidate_fixture_id)
        if not matches:
            raise FixtureProviderError("STATS_FIXTURE_UNMATCHED")
        if len(matches) > 1:
            raise FixtureProviderError("STATS_FIXTURE_AMBIGUOUS")
        return matches[0]

    @staticmethod
    def _extract_corner_kicks(row: dict[str, Any]) -> int | None:
        for statistic in row.get("statistics") or []:
            stat_type = normalize_text_key(str(statistic.get("type") or ""))
            if stat_type == "corner kicks":
                return _coerce_int(statistic.get("value"))
        return None

    def get_authoritative_corner_statistics(
        self,
        fixture_context: dict[str, Any],
    ) -> AuthoritativeFixtureStatistics:
        provider_fixture_id = self._resolve_statistics_fixture_id(fixture_context)
        rows = self._request_fixture_statistics(provider_fixture_id=provider_fixture_id)
        expected_home = normalize_text_key(str(fixture_context.get("home_team_name") or ""))
        expected_away = normalize_text_key(str(fixture_context.get("away_team_name") or ""))
        home_corners = None
        away_corners = None
        source_timestamp = None
        for row in rows:
            team_name = normalize_text_key(str(((row.get("team") or {}).get("name")) or ""))
            if source_timestamp is None:
                source_timestamp = (
                    str(row.get("update") or "").strip()
                    or str(((row.get("fixture") or {}).get("update")) or "").strip()
                    or None
                )
            corner_kicks = self._extract_corner_kicks(row)
            if team_name == expected_home:
                home_corners = corner_kicks
            elif team_name == expected_away:
                away_corners = corner_kicks
        total_corners = None if home_corners is None or away_corners is None else home_corners + away_corners
        return AuthoritativeFixtureStatistics(
            fixture_id=str(fixture_context.get("fixture_id") or ""),
            provider=API_FOOTBALL_PROVIDER,
            provider_fixture_id=provider_fixture_id,
            home_corners=home_corners,
            away_corners=away_corners,
            total_corners=total_corners,
            source_timestamp=source_timestamp,
            captured_at=utc_now_iso(),
        )

    def _division_code_for_league_id(self, league_id: int) -> str | None:
        for division_code, configured_id in self._league_map().items():
            if configured_id == league_id:
                return division_code
        return None

    def _local_date_for_fixture(self, fixture: NormalizedFixture) -> date:
        timezone_name = ZoneInfo(self._config.display_timezone)
        return datetime.fromisoformat(fixture.kickoff_utc).astimezone(timezone_name).date()

    def _today_local(self) -> date:
        return datetime.now(timezone.utc).astimezone(ZoneInfo(self._config.display_timezone)).date()

    def _normalize_fixture(self, payload: dict[str, Any]) -> NormalizedFixture | None:
        fixture_payload = payload.get("fixture") or {}
        league_payload = payload.get("league") or {}
        teams_payload = payload.get("teams") or {}
        goals_payload = payload.get("goals") or {}

        league_id = league_payload.get("id")
        if league_id is None:
            return None
        division_code = self._division_code_for_league_id(int(league_id))
        if division_code is None:
            return None

        competition_id, default_competition_name = DIVISION_TO_COMPETITION.get(
            division_code,
            (division_code, division_code),
        )
        raw_kickoff = str(fixture_payload.get("date") or "").strip()
        kickoff_utc = raw_kickoff.replace("Z", "+00:00")
        status_payload = fixture_payload.get("status") or {}
        status_short = str(status_payload.get("short") or "").upper()
        normalized_status = _API_FOOTBALL_STATUS_MAP.get(status_short, FIXTURE_STATUS_PRE_MATCH)

        home_team = teams_payload.get("home") or {}
        away_team = teams_payload.get("away") or {}
        home_name = str(home_team.get("name") or "").strip()
        away_name = str(away_team.get("name") or "").strip()
        provider_fixture_id = str(fixture_payload.get("id") or "").strip() or None
        fixture_id = build_fixture_id(
            provider=API_FOOTBALL_PROVIDER,
            provider_fixture_id=provider_fixture_id,
            kickoff_utc=kickoff_utc,
            home_team_name=home_name,
            away_team_name=away_name,
        )
        score_home = goals_payload.get("home")
        score_away = goals_payload.get("away")

        return NormalizedFixture(
            fixture_id=fixture_id,
            provider_fixture_id=provider_fixture_id,
            provider=API_FOOTBALL_PROVIDER,
            competition_id=str(competition_id),
            competition_name=str(league_payload.get("name") or default_competition_name),
            division_code=division_code,
            season=str(league_payload.get("season") or ""),
            home_team_id=str(home_team.get("id") or "").strip() or None,
            home_team_name=home_name,
            away_team_id=str(away_team.get("id") or "").strip() or None,
            away_team_name=away_name,
            kickoff_utc=kickoff_utc,
            status=normalized_status,
            elapsed_minute=None
            if status_payload.get("elapsed") is None
            else int(status_payload.get("elapsed")),
            home_score=None if score_home is None else int(score_home),
            away_score=None if score_away is None else int(score_away),
            last_provider_update=utc_now_iso(),
            source_version=API_FOOTBALL_SOURCE_VERSION,
            provider_payload=payload,
        )

    @staticmethod
    def _dedupe(fixtures: list[NormalizedFixture]) -> list[NormalizedFixture]:
        deduped: dict[str, NormalizedFixture] = {}
        for fixture in fixtures:
            deduped[fixture.fixture_id] = fixture
        return sorted(deduped.values(), key=lambda item: (item.kickoff_utc, item.fixture_id))

    def get_live_fixtures(self) -> list[NormalizedFixture]:
        if not self.configured():
            raise FixtureProviderNotConfiguredError("API-Football is not configured")
        league_ids = set(self._league_map().values())
        rows = self._request_fixtures(params={"live": "all"})
        fixtures = []
        for row in rows:
            league_id = int(((row.get("league") or {}).get("id") or 0))
            if league_id not in league_ids:
                continue
            normalized = self._normalize_fixture(row)
            if normalized is not None and normalized.status in {
                FIXTURE_STATUS_LIVE,
                FIXTURE_STATUS_HALFTIME,
            }:
                fixtures.append(normalized)
        return self._dedupe(fixtures)

    def get_fixtures_for_date(self, target_date: date) -> list[NormalizedFixture]:
        if not self.configured():
            raise FixtureProviderNotConfiguredError("API-Football is not configured")
        timezone_name = ZoneInfo(self._config.display_timezone)
        local_start = datetime.combine(target_date, daytime.min, tzinfo=timezone_name)
        local_end = datetime.combine(target_date + timedelta(days=1), daytime.min, tzinfo=timezone_name)
        utc_dates: list[date] = []
        current_utc_date = local_start.astimezone(timezone.utc).date()
        final_utc_date = (local_end - timedelta(seconds=1)).astimezone(timezone.utc).date()
        while current_utc_date <= final_utc_date:
            utc_dates.append(current_utc_date)
            current_utc_date += timedelta(days=1)
        fixtures: list[NormalizedFixture] = []
        league_items = sorted(self._league_map().items())

        def fetch_league_rows(item: tuple[str, int, date]) -> tuple[str, date, list[dict[str, Any]]]:
            division_code, league_id, query_date = item
            rows = self._request_fixtures(
                params={
                    "date": query_date.isoformat(),
                    "league": league_id,
                    "season": target_date.year,
                }
            )
            return division_code, query_date, rows

        fetch_items = [(division_code, league_id, query_date) for division_code, league_id in league_items for query_date in utc_dates]
        # Keep cold-start schedule fan-out comfortably below the provider
        # budget's 4 req/sec safety ceiling. The live-fixtures request and
        # successive league/date tasks share the same process-wide limiter, so
        # two schedule workers avoid starvation while remaining fast enough for
        # an hourly serverless refresh.
        with ThreadPoolExecutor(max_workers=min(2, max(1, len(fetch_items)))) as executor:
            for division_code, _, rows in executor.map(fetch_league_rows, fetch_items):
                for row in rows:
                    normalized = self._normalize_fixture(row)
                    if normalized is None:
                        continue
                    if normalized.division_code != division_code:
                        continue
                    if self._local_date_for_fixture(normalized) != target_date:
                        continue
                    fixtures.append(normalized)
        return self._dedupe(fixtures)

    def get_upcoming_fixtures(
        self,
        *,
        from_utc: datetime,
        to_utc: datetime,
    ) -> list[NormalizedFixture]:
        if not self.configured():
            raise FixtureProviderNotConfiguredError("API-Football is not configured")
        fixtures: list[NormalizedFixture] = []
        current_date = from_utc.astimezone(timezone.utc).date()
        final_date = to_utc.astimezone(timezone.utc).date()
        while current_date <= final_date:
            fixtures.extend(self.get_fixtures_for_date(current_date))
            current_date += timedelta(days=1)
        lower = from_utc.astimezone(timezone.utc)
        upper = to_utc.astimezone(timezone.utc)
        filtered = [
            fixture
            for fixture in fixtures
            if lower <= datetime.fromisoformat(fixture.kickoff_utc) <= upper
            and fixture.status in {
                FIXTURE_STATUS_SCHEDULED,
                FIXTURE_STATUS_PRE_MATCH,
            }
        ]
        return self._dedupe(filtered)

    def get_fixture(self, fixture_id: str) -> NormalizedFixture | None:
        if not fixture_id.startswith(f"{API_FOOTBALL_PROVIDER}:"):
            return None
        provider_fixture_id = fixture_id.split(":", 1)[1]
        if provider_fixture_id.startswith("generated:"):
            return None
        rows = self._request_fixtures(params={"id": provider_fixture_id})
        if not rows:
            return None
        return self._normalize_fixture(rows[0])
