"""API-Football current odds provider."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any

import httpx

from ..dashboard.models import normalize_text_key, parse_iso_datetime
from ..locking import canonical_hash
from .freshness import (
    FRESHNESS_STATE_FRESH,
    FRESHNESS_STATE_STALE,
    FRESHNESS_STATE_UNKNOWN,
    classify_freshness_state,
    resolve_market_max_age_minutes,
)
from .providers import MarketProviderError, NormalizedMarketSnapshot

API_FOOTBALL_MARKET_PROVIDER = "api-football"
API_FOOTBALL_ODDS_VERSION = "api_football_current_market_provider_v1"
API_FOOTBALL_ODDS_ENDPOINT = "https://v3.football.api-sports.io/odds"
API_FOOTBALL_ODDS_MAPPING_ENDPOINT = "https://v3.football.api-sports.io/odds/mapping"
API_FOOTBALL_FIXTURE_DETAIL_ENDPOINT = "https://v3.football.api-sports.io/fixtures"

_MARKET_NAME_MAP = {
    "Match Winner": "1x2",
    "Both Teams Score": "btts",
    "Goals Over/Under": "goals_total",
    "Asian Handicap": "asian_handicap",
    "Corners Over Under": "corner_total",
    "Corners Over/Under": "corner_total",
}


@dataclass(frozen=True)
class ApiFootballOddsProviderConfig:
    token_env_var: str = "SPORTS_AI_API_FOOTBALL_TOKEN"
    league_map_env_var: str = "SPORTS_AI_API_FOOTBALL_LEAGUE_MAP"
    reference_bookmakers_env_var: str = "SPORTS_AI_MARKET_REFERENCE_BOOKMAKERS"
    freshness_max_age_minutes_env_var: str = "SPORTS_AI_API_FOOTBALL_MARKET_MAX_AGE_MINUTES"
    timeout_seconds: float = 20.0
    mapping_cache_ttl_seconds: int = 300
    fixture_cache_ttl_seconds: int = 600
    odds_cache_ttl_seconds: int = 90
    kickoff_match_tolerance_seconds: int = 1800


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_env_list(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _normalize_bookmaker_key(value: str) -> str:
    return normalize_text_key(value).replace(" ", "")


def _parse_decimal_line(raw_line: str) -> float:
    try:
        return float(raw_line)
    except ValueError as exc:  # pragma: no cover - defensive
        raise MarketProviderError(f"invalid market line {raw_line!r}") from exc


def _normalize_1x2_selection(value: str) -> str | None:
    mapping = {
        "home": "HOME",
        "draw": "DRAW",
        "away": "AWAY",
    }
    return mapping.get(value.strip().lower())


def _normalize_yes_no_selection(value: str) -> str | None:
    mapping = {
        "yes": "YES",
        "no": "NO",
    }
    return mapping.get(value.strip().lower())


def _normalize_total_value(value: str) -> tuple[str, float] | None:
    parts = value.split()
    if len(parts) != 2:
        return None
    side, line = parts
    normalized_side = side.strip().lower()
    if normalized_side not in {"over", "under"}:
        return None
    return normalized_side.upper(), _parse_decimal_line(line)


def _normalize_asian_handicap_value(value: str) -> tuple[str, float] | None:
    parts = value.split()
    if len(parts) != 2:
        return None
    team_side, line = parts
    normalized_side = team_side.strip().lower()
    if normalized_side not in {"home", "away"}:
        return None
    raw_line = _parse_decimal_line(line)
    selection = normalized_side.upper()
    canonical_line = raw_line if selection == "HOME" else -raw_line
    return selection, canonical_line


class ApiFootballOddsProvider:
    """Current market provider backed by the API-Football odds endpoints."""

    def __init__(self, config: ApiFootballOddsProviderConfig | None = None) -> None:
        self._config = ApiFootballOddsProviderConfig() if config is None else config
        self._response_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._response_cache_lock = Lock()
        self._league_map_cache: dict[str, int] | None = None
        self._mapping_cache: dict[str, dict[str, Any]] = {}
        self._mapping_cache_lock = Lock()
        self._query_log: list[dict[str, Any]] = []
        self._query_log_lock = Lock()
        self._last_error: str | None = None

    def provider_name(self) -> str:
        return API_FOOTBALL_MARKET_PROVIDER

    def configured(self) -> bool:
        return bool(os.environ.get(self._config.token_env_var, "").strip())

    def _token(self) -> str:
        token = os.environ.get(self._config.token_env_var, "").strip()
        if not token:
            raise MarketProviderError(f"missing required environment variable {self._config.token_env_var}")
        return token

    def _league_map(self) -> dict[str, int]:
        if self._league_map_cache is not None:
            return dict(self._league_map_cache)
        path_text = os.environ.get(self._config.league_map_env_var, "").strip()
        if not path_text:
            self._league_map_cache = {}
            return {}
        path = Path(path_text)
        if not path.exists():
            repo_relative = Path(__file__).resolve().parents[4] / path_text
            path = repo_relative
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self._league_map_cache = {str(key): int(value) for key, value in payload.items()}
        return dict(self._league_map_cache)

    def _reference_bookmakers(self) -> tuple[str, ...]:
        raw = os.environ.get(self._config.reference_bookmakers_env_var, "").strip()
        if not raw:
            return ("pinnacle", "betfair", "bet365")
        return tuple(_normalize_bookmaker_key(item) for item in _parse_env_list(raw))

    def _freshness_threshold_minutes(self) -> int | None:
        return resolve_market_max_age_minutes(
            self._config.freshness_max_age_minutes_env_var,
            default_when_unset=240,
        )

    def _cache_key(self, endpoint: str, params: dict[str, Any] | None) -> str:
        return json.dumps({"endpoint": endpoint, "params": params or {}}, sort_keys=True)

    def _cache_get(self, key: str, *, ttl_seconds: int) -> dict[str, Any] | None:
        with self._response_cache_lock:
            cached = self._response_cache.get(key)
        if cached is None:
            return None
        created_at, payload = cached
        if monotonic() - created_at > ttl_seconds:
            with self._response_cache_lock:
                self._response_cache.pop(key, None)
            return None
        return payload

    def _cache_set(self, key: str, payload: dict[str, Any]) -> None:
        with self._response_cache_lock:
            self._response_cache[key] = (monotonic(), payload)

    def _record_query(self, *, query_kind: str, params: dict[str, Any] | None, status: str, results: int, errors: Any) -> None:
        entry = {
            "captured_at": _utc_now(),
            "provider": API_FOOTBALL_MARKET_PROVIDER,
            "query_kind": query_kind,
            "params": params or {},
            "status": status,
            "results": results,
            "errors": errors,
        }
        with self._query_log_lock:
            self._query_log.append(entry)

    def _request_json(
        self,
        *,
        endpoint: str,
        params: dict[str, Any] | None,
        ttl_seconds: int,
        query_kind: str,
    ) -> dict[str, Any]:
        cache_key = self._cache_key(endpoint, params)
        cached = self._cache_get(cache_key, ttl_seconds=ttl_seconds)
        if cached is not None:
            self._record_query(
                query_kind=query_kind,
                params=params,
                status="CACHE_HIT",
                results=int(cached.get("results") or 0),
                errors=cached.get("errors") or [],
            )
            return cached

        try:
            with httpx.Client(
                timeout=self._config.timeout_seconds,
                follow_redirects=True,
                headers={"x-apisports-key": self._token()},
            ) as client:
                response = client.get(endpoint, params=params)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:  # pragma: no cover - network defensive
            self._last_error = str(exc)
            self._record_query(query_kind=query_kind, params=params, status="ERROR", results=0, errors=str(exc))
            raise MarketProviderError(f"API-Football market request failed: {exc}") from exc

        self._last_error = None
        self._cache_set(cache_key, payload)
        self._record_query(
            query_kind=query_kind,
            params=params,
            status="OK",
            results=int(payload.get("results") or 0),
            errors=payload.get("errors") or [],
        )
        return payload

    def _mapping_rows(self) -> list[dict[str, Any]]:
        payload = self._request_json(
            endpoint=API_FOOTBALL_ODDS_MAPPING_ENDPOINT,
            params=None,
            ttl_seconds=self._config.mapping_cache_ttl_seconds,
            query_kind="odds_mapping_page_1",
        )
        total_pages = int(((payload.get("paging") or {}).get("total")) or 1)
        rows = list(payload.get("response") or [])
        for page in range(2, total_pages + 1):
            page_payload = self._request_json(
                endpoint=API_FOOTBALL_ODDS_MAPPING_ENDPOINT,
                params={"page": page},
                ttl_seconds=self._config.mapping_cache_ttl_seconds,
                query_kind=f"odds_mapping_page_{page}",
            )
            rows.extend(page_payload.get("response") or [])
        return rows

    def _fixture_detail(self, provider_fixture_id: str) -> dict[str, Any]:
        payload = self._request_json(
            endpoint=API_FOOTBALL_FIXTURE_DETAIL_ENDPOINT,
            params={"id": provider_fixture_id},
            ttl_seconds=self._config.fixture_cache_ttl_seconds,
            query_kind="fixture_detail",
        )
        rows = payload.get("response") or []
        if not rows:
            raise MarketProviderError(f"fixture detail not found for provider fixture {provider_fixture_id}")
        return dict(rows[0])

    def _mapping_cache_key(self, fixture_context: dict[str, Any]) -> str:
        return canonical_hash(
            {
                "competition_id": fixture_context.get("competition_id"),
                "division_code": fixture_context.get("division_code"),
                "kickoff_utc": fixture_context.get("kickoff_utc"),
                "home_team_name": fixture_context.get("home_team_name"),
                "away_team_name": fixture_context.get("away_team_name"),
            }
        )

    def _resolve_provider_fixture_id(self, fixture_context: dict[str, Any]) -> str:
        provider_name = str(fixture_context.get("provider") or "").strip().lower()
        provider_fixture_id = str(fixture_context.get("provider_fixture_id") or "").strip()
        if provider_name == API_FOOTBALL_MARKET_PROVIDER and provider_fixture_id and not provider_fixture_id.startswith("generated:"):
            return provider_fixture_id

        cache_key = self._mapping_cache_key(fixture_context)
        with self._mapping_cache_lock:
            cached = self._mapping_cache.get(cache_key)
        if cached is not None:
            return str(cached["provider_fixture_id"])

        division_code = str(fixture_context.get("division_code") or "").strip()
        league_id = self._league_map().get(division_code)
        if league_id is None:
            raise MarketProviderError("MARKET_FIXTURE_UNRESOLVED: missing API-Football league mapping")

        kickoff = parse_iso_datetime(str(fixture_context["kickoff_utc"]))
        expected_home = normalize_text_key(str(fixture_context["home_team_name"]))
        expected_away = normalize_text_key(str(fixture_context["away_team_name"]))
        candidates = [
            row
            for row in self._mapping_rows()
            if int(((row.get("league") or {}).get("id")) or 0) == league_id
        ]
        matches: list[dict[str, Any]] = []
        for candidate in candidates:
            provider_fixture_id = str(((candidate.get("fixture") or {}).get("id")) or "")
            if not provider_fixture_id:
                continue
            fixture_date = str(((candidate.get("fixture") or {}).get("date")) or "")
            if not fixture_date:
                continue
            candidate_kickoff = parse_iso_datetime(fixture_date)
            if abs((candidate_kickoff - kickoff).total_seconds()) > self._config.kickoff_match_tolerance_seconds:
                continue
            detail = self._fixture_detail(provider_fixture_id)
            home_name = normalize_text_key(str((((detail.get("teams") or {}).get("home") or {}).get("name")) or ""))
            away_name = normalize_text_key(str((((detail.get("teams") or {}).get("away") or {}).get("name")) or ""))
            if home_name != expected_home or away_name != expected_away:
                continue
            matches.append(
                {
                    "provider_fixture_id": provider_fixture_id,
                    "detail": detail,
                }
            )

        if len(matches) != 1:
            raise MarketProviderError("MARKET_FIXTURE_UNRESOLVED")

        resolved = matches[0]
        with self._mapping_cache_lock:
            self._mapping_cache[cache_key] = resolved
        return str(resolved["provider_fixture_id"])

    def _freshness_state(self, *, source_timestamp: str | None, captured_at: str, kickoff_utc: str | None) -> str:
        return classify_freshness_state(
            source_timestamp=source_timestamp,
            evaluated_at=captured_at,
            threshold_minutes=self._freshness_threshold_minutes(),
            kickoff_utc=kickoff_utc,
        )

    def _normalize_bet_values(
        self,
        *,
        market_name: str,
        values: list[dict[str, Any]],
    ) -> list[tuple[str, str, float | None, float]]:
        normalized_rows: list[tuple[str, str, float | None, float]] = []
        market_family = _MARKET_NAME_MAP.get(market_name)
        if market_family is None:
            return normalized_rows
        for value in values:
            raw_value = str(value.get("value") or "")
            price_raw = value.get("odd")
            if price_raw is None:
                continue
            try:
                decimal_odds = float(price_raw)
            except (TypeError, ValueError):
                continue
            if decimal_odds <= 1.0:
                continue
            if market_family == "1x2":
                selection = _normalize_1x2_selection(raw_value)
                if selection is None:
                    continue
                normalized_rows.append((market_family, selection, None, decimal_odds))
            elif market_family == "btts":
                selection = _normalize_yes_no_selection(raw_value)
                if selection is None:
                    continue
                normalized_rows.append((market_family, selection, None, decimal_odds))
            elif market_family in {"goals_total", "corner_total"}:
                parsed = _normalize_total_value(raw_value)
                if parsed is None:
                    continue
                selection, line = parsed
                normalized_rows.append((market_family, selection, line, decimal_odds))
            elif market_family == "asian_handicap":
                parsed = _normalize_asian_handicap_value(raw_value)
                if parsed is None:
                    continue
                selection, line = parsed
                normalized_rows.append((market_family, selection, line, decimal_odds))
        return normalized_rows

    def _snapshot_rows_from_payload(
        self,
        *,
        fixture_id: str,
        fixture_context: dict[str, Any],
        provider_fixture_id: str,
        payload: dict[str, Any],
    ) -> list[NormalizedMarketSnapshot]:
        rows = payload.get("response") or []
        if not rows:
            return []
        fixture_payload = dict(rows[0])
        source_timestamp = str(fixture_payload.get("update") or "").strip() or None
        captured_at = _utc_now()
        bookmaker_priority = {
            name: index
            for index, name in enumerate(self._reference_bookmakers())
        }
        snapshots: list[NormalizedMarketSnapshot] = []
        for bookmaker in fixture_payload.get("bookmakers") or []:
            bookmaker_name = str(bookmaker.get("name") or "").strip()
            if not bookmaker_name:
                continue
            reference_rank = bookmaker_priority.get(_normalize_bookmaker_key(bookmaker_name))
            reference_designation = "reference" if reference_rank is not None else "available"
            for bet in bookmaker.get("bets") or []:
                market_name = str(bet.get("name") or "").strip()
                normalized_values = self._normalize_bet_values(
                    market_name=market_name,
                    values=list(bet.get("values") or []),
                )
                for market_family, selection, line, decimal_odds in normalized_values:
                    snapshots.append(
                        NormalizedMarketSnapshot(
                            provider=self.provider_name(),
                            fixture_id=fixture_id,
                            provider_fixture_id=provider_fixture_id,
                            captured_at=captured_at,
                            source_timestamp=source_timestamp,
                            market=market_family,
                            selection=selection,
                            line=line,
                            decimal_odds=decimal_odds,
                            bookmaker=bookmaker_name,
                            ingestion_version=API_FOOTBALL_ODDS_VERSION,
                            provider_market_id=f"{provider_fixture_id}:{bookmaker.get('id')}:{bet.get('id')}:{selection}:{line}",
                            is_pre_match=True,
                            home_team_name=str(fixture_context.get("home_team_name") or ""),
                            away_team_name=str(fixture_context.get("away_team_name") or ""),
                            competition_id=str(fixture_context.get("competition_id") or ""),
                            competition_name=str(fixture_context.get("competition_name") or ""),
                            kickoff_utc=str(fixture_context.get("kickoff_utc") or ""),
                            freshness_state=self._freshness_state(
                                source_timestamp=source_timestamp,
                                captured_at=captured_at,
                                kickoff_utc=None if fixture_context.get("kickoff_utc") is None else str(fixture_context["kickoff_utc"]),
                            ),
                            reference_designation=reference_designation,
                            reference_rank=reference_rank,
                        )
                    )
        return snapshots

    def get_fixture_markets(
        self,
        fixture_id: str,
        *,
        fixture_context: dict[str, Any] | None = None,
    ) -> list[NormalizedMarketSnapshot]:
        if not self.configured():
            raise MarketProviderError("CURRENT_ODDS_PROVIDER = NOT_CONFIGURED")
        if fixture_context is None:
            raise MarketProviderError("fixture_context is required for cross-provider market mapping")
        provider_fixture_id = self._resolve_provider_fixture_id(fixture_context)
        payload = self._request_json(
            endpoint=API_FOOTBALL_ODDS_ENDPOINT,
            params={"fixture": provider_fixture_id},
            ttl_seconds=self._config.odds_cache_ttl_seconds,
            query_kind="fixture_odds",
        )
        return self._snapshot_rows_from_payload(
            fixture_id=fixture_id,
            fixture_context=fixture_context,
            provider_fixture_id=provider_fixture_id,
            payload=payload,
        )

    def get_best_prices(
        self,
        fixture_id: str,
        *,
        fixture_context: dict[str, Any] | None = None,
    ) -> list[NormalizedMarketSnapshot]:
        rows = self.get_fixture_markets(fixture_id, fixture_context=fixture_context)
        best: dict[tuple[str, str, float | None], NormalizedMarketSnapshot] = {}
        for row in rows:
            key = (row.market, row.selection, row.line)
            current = best.get(key)
            if current is None or row.decimal_odds > current.decimal_odds:
                best[key] = row
        return list(best.values())

    def get_market_snapshot(
        self,
        fixture_id: str,
        *,
        fixture_context: dict[str, Any] | None = None,
    ) -> list[NormalizedMarketSnapshot]:
        return self.get_fixture_markets(fixture_id, fixture_context=fixture_context)

    def diagnostics(self) -> dict[str, Any]:
        with self._query_log_lock:
            query_log = [dict(entry) for entry in self._query_log]
        reference_bookmakers = list(self._reference_bookmakers())
        state = "CONNECTED" if self.configured() else "NOT_CONFIGURED"
        if self.configured() and self._last_error:
            state = "DEGRADED"
        return {
            "provider": self.provider_name(),
            "configured": self.configured(),
            "state": state,
            "current_market_decisions": "READY" if self.configured() else "BLOCKED",
            "captured_at": _utc_now(),
            "reason": None if self.configured() else f"missing required environment variable {self._config.token_env_var}",
            "reference_bookmakers": reference_bookmakers,
            "freshness_max_age_minutes": self._freshness_threshold_minutes(),
            "last_error": self._last_error,
            "query_count": len(query_log),
            "queries": query_log[-20:],
        }
