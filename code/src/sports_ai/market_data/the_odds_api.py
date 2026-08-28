"""The Odds API current market provider (soccer h2h + spreads + totals).

Free-tier-safe foundation:

- Credential is read from ``SPORTS_AI_THE_ODDS_API_KEY`` and never logged.
- CURRENT h2h (soccer 1X2), spreads (Asian handicap line snapshots), and totals
  (goals Over/Under) decimal odds are supported. Totals and handicap lines
  preserve exact provider point identity under the repo's existing canonical
  line conventions (no rounding or approximation).
- Event-level add-on markets are fetched quota-consciously for matched fixtures
  only. BTTS and corner totals preserve exact provider market identity.
- One bookmaker region via ``SPORTS_AI_ODDS_REGION``.
- Events are fetched once per sport key and reused through a short in-process
  cache so repeated fixture matching does not repeat provider requests.
- Quota headers (x-requests-remaining / used / last) are captured into
  diagnostics so callers can guard free-tier consumption.

This provider never begins polling on its own; automatic odds sync is gated
elsewhere (``SPORTS_AI_ENABLE_ODDS_SYNC``).
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from time import monotonic, sleep
from typing import Any

import httpx

from ..dashboard.models import normalize_text_key, parse_iso_datetime
from .freshness import (
    FRESHNESS_STATE_FRESH,
    FRESHNESS_STATE_STALE,
    FRESHNESS_STATE_UNKNOWN,
    classify_freshness_state,
    resolve_market_max_age_minutes,
)
from .providers import MarketProviderError, NormalizedMarketSnapshot

THE_ODDS_API_PROVIDER = "the-odds-api"
THE_ODDS_API_INGESTION_VERSION = "the_odds_api_current_market_provider_v1"
THE_ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"

THE_ODDS_MARKET_H2H = "h2h"
THE_ODDS_MARKET_SPREADS = "spreads"
THE_ODDS_MARKET_TOTALS = "totals"
THE_ODDS_MARKET_BTTS = "btts"
THE_ODDS_MARKET_CORNERS = "alternate_totals_corners"
THE_ODDS_MARKETS_REQUESTED = f"{THE_ODDS_MARKET_H2H},{THE_ODDS_MARKET_SPREADS},{THE_ODDS_MARKET_TOTALS}"
THE_ODDS_EVENT_MARKETS_REQUESTED = f"{THE_ODDS_MARKET_BTTS},{THE_ODDS_MARKET_CORNERS}"
_SUPPORTED_MARKET_KEYS = frozenset({THE_ODDS_MARKET_H2H, THE_ODDS_MARKET_SPREADS, THE_ODDS_MARKET_TOTALS})

MARKET_1X2 = "1x2"
MARKET_ASIAN_HANDICAP = "asian_handicap"
MARKET_GOALS_TOTAL = "goals_total"
MARKET_BTTS = "btts"
MARKET_CORNER_TOTAL = "corner_total"
TOTALS_SELECTION_OVER = "OVER"
TOTALS_SELECTION_UNDER = "UNDER"
BTTS_SELECTION_YES = "YES"
BTTS_SELECTION_NO = "NO"

STATE_NOT_CONFIGURED = "NOT_CONFIGURED"
STATE_IDLE = "IDLE"
STATE_CONNECTED = "CONNECTED"
STATE_DEGRADED = "DEGRADED"
STATE_AUTH_ERROR = "AUTH_ERROR"
STATE_RATE_LIMITED = "RATE_LIMITED"
STATE_QUOTA_GUARD = "QUOTA_GUARD"
STATE_QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
STATE_NO_CURRENT_EVENTS = "NO_CURRENT_EVENTS"

# Provider states that stop further event-level add-on market fan-out for a
# sync. These requests are additive to the league-wide h2h,totals request, so
# once a global failure/guard condition is observed the fan-out must halt to
# protect quota.
_ADDITIONAL_MARKET_BLOCKING_STATES = frozenset(
    {
        STATE_AUTH_ERROR,
        STATE_RATE_LIMITED,
        STATE_DEGRADED,
        STATE_QUOTA_GUARD,
        STATE_QUOTA_EXHAUSTED,
    }
)

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sanitize_url(url: str) -> str:
    """Redact the apiKey value from a provider URL for diagnostics/logging."""
    return re.sub(r"(apiKey=)[^&\s]+", r"\1REDACTED", url)


def _parse_env_list(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True)
class OddsSportMapping:
    """A Sports Edge competition mapped to a The Odds API sport key."""

    sport_key: str
    enabled: bool
    verified: bool
    note: str


# Explicit competition -> sport key mapping. Only VERIFIED + ENABLED entries are
# usable at runtime. Unverified keys are recorded for later discovery but remain
# disabled so we never guess against the live provider.
COMPETITION_TO_SPORT = {
    "ENG1": OddsSportMapping(
        sport_key="soccer_epl",
        enabled=True,
        verified=True,
        note="Premier League (E0) - confirmed mapping",
    ),
    "ENG2": OddsSportMapping(
        sport_key="soccer_england_championship",
        enabled=False,
        verified=False,
        note="UNVERIFIED: Championship (E1) - requires live /sports discovery",
    ),
    "ENG3": OddsSportMapping(
        sport_key="soccer_england_league1",
        enabled=False,
        verified=False,
        note="UNVERIFIED: League One (E2) - requires live /sports discovery",
    ),
    "ENG4": OddsSportMapping(
        sport_key="soccer_england_league2",
        enabled=False,
        verified=False,
        note="UNVERIFIED: League Two (E3) - requires live /sports discovery",
    ),
    "ESP1": OddsSportMapping(
        sport_key="soccer_spain_la_liga",
        enabled=False,
        verified=False,
        note="UNVERIFIED: La Liga (SP1) - requires live /sports discovery",
    ),
}


@dataclass(frozen=True)
class TheOddsApiProviderConfig:
    api_key_env_var: str = "SPORTS_AI_THE_ODDS_API_KEY"
    region_env_var: str = "SPORTS_AI_ODDS_REGION"
    default_region: str = "uk"
    reference_bookmakers_env_var: str = "SPORTS_AI_MARKET_REFERENCE_BOOKMAKERS"
    freshness_max_age_minutes_env_var: str = "SPORTS_AI_MARKET_MAX_AGE_MINUTES"
    timeout_seconds: float = 20.0
    user_agent: str = "sports-edge-ai/0.1 (the-odds-api current market provider)"
    odds_cache_ttl_seconds: int = 120
    kickoff_match_tolerance_seconds: int = 1800
    min_quota_reserve: int = 10
    max_retries: int = 2
    retry_backoff_seconds: float = 1.0
    # Legacy field name retained for compatibility with existing config/tests.
    # This caps event-level add-on market requests per league-wide sync window.
    max_btts_event_requests: int = 10


class TheOddsApiProvider:
    """Current market provider backed by The Odds API V4 h2h (1X2) odds."""

    def __init__(
        self,
        config: TheOddsApiProviderConfig | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._config = TheOddsApiProviderConfig() if config is None else config
        self._client = client or httpx.Client(
            timeout=self._config.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": self._config.user_agent},
        )
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_lock = Lock()
        self._quota: dict[str, Any] = {
            "remaining": None,
            "used": None,
            "last_request_cost": None,
        }
        self._quota_lock = Lock()
        self._stats: dict[str, Any] = {
            "events_fetched": 0,
            "matched": 0,
            "unmatched": 0,
            "ambiguous": 0,
            "bookmakers": {},
            "btts_event_requests": 0,
            "btts_truncated": 0,
            "additional_event_requests": 0,
            "additional_markets_truncated": 0,
        }
        self._stats_lock = Lock()
        self._state = STATE_NOT_CONFIGURED
        self._last_error: str | None = None
        self._last_attempt_at: str | None = None
        self._last_success_at: str | None = None
        self._request_count = 0
        self._additional_event_requests_this_window = 0

    def provider_name(self) -> str:
        return THE_ODDS_API_PROVIDER

    def configured(self) -> bool:
        return bool(os.environ.get(self._config.api_key_env_var, "").strip())

    def _api_key(self) -> str:
        key = os.environ.get(self._config.api_key_env_var, "").strip()
        if not key:
            raise MarketProviderError(
                f"missing required environment variable {self._config.api_key_env_var}"
            )
        return key

    def _region(self) -> str:
        raw = os.environ.get(self._config.region_env_var, "").strip()
        return raw if raw else self._config.default_region

    def _reference_bookmakers(self) -> tuple[str, ...]:
        raw = os.environ.get(self._config.reference_bookmakers_env_var, "").strip()
        if not raw:
            return ("pinnacle",)
        return tuple(name.lower() for name in _parse_env_list(raw))

    def _freshness_threshold_minutes(self) -> int | None:
        return resolve_market_max_age_minutes(self._config.freshness_max_age_minutes_env_var)

    def _set_state(self, state: str, error: str | None) -> None:
        self._state = state
        if error is not None:
            self._last_error = error

    def _capture_quota(self, headers: httpx.Headers) -> None:
        def _int(name: str) -> int | None:
            raw = headers.get(name)
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None

        remaining = _int("x-requests-remaining")
        used = _int("x-requests-used")
        last = _int("x-requests-last")
        with self._quota_lock:
            if remaining is not None:
                self._quota["remaining"] = remaining
            if used is not None:
                self._quota["used"] = used
            if last is not None:
                self._quota["last_request_cost"] = last

    def _quota_state(self) -> str:
        remaining = self._quota.get("remaining")
        if remaining is None:
            return STATE_CONNECTED
        if remaining <= 0:
            return STATE_QUOTA_EXHAUSTED
        if remaining <= self._config.min_quota_reserve:
            return STATE_QUOTA_GUARD
        return STATE_CONNECTED

    def _request_json(
        self,
        *,
        endpoint: str,
        params: dict[str, Any] | None,
    ) -> Any:
        url = f"{THE_ODDS_API_BASE_URL}/{endpoint.lstrip('/')}"
        url_params = dict(params or {})
        url_params["apiKey"] = self._api_key()
        self._last_attempt_at = _utc_now()
        self._request_count += 1

        for attempt in range(self._config.max_retries + 1):
            try:
                response = self._client.get(url, params=url_params)
            except httpx.HTTPError as exc:
                self._set_state(STATE_DEGRADED, str(exc))
                if attempt < self._config.max_retries:
                    sleep(self._config.retry_backoff_seconds * (attempt + 1))
                    continue
                raise MarketProviderError(
                    f"{THE_ODDS_API_PROVIDER} request failed: {exc}"
                ) from exc

            self._capture_quota(response.headers)
            status = response.status_code
            if status == 401:
                self._set_state(STATE_AUTH_ERROR, "authentication failed (401)")
                raise MarketProviderError(f"{THE_ODDS_API_PROVIDER} authentication failed (401)")
            if status == 429:
                self._set_state(STATE_RATE_LIMITED, "rate limited (429)")
                if attempt < self._config.max_retries:
                    sleep(self._config.retry_backoff_seconds * (attempt + 1))
                    continue
                raise MarketProviderError(f"{THE_ODDS_API_PROVIDER} rate limited (429)")
            if status >= 500:
                self._set_state(STATE_DEGRADED, f"upstream error ({status})")
                if attempt < self._config.max_retries:
                    sleep(self._config.retry_backoff_seconds * (attempt + 1))
                    continue
                raise MarketProviderError(f"{THE_ODDS_API_PROVIDER} upstream error ({status})")
            if status != 200:
                self._set_state(STATE_DEGRADED, f"unexpected status ({status})")
                raise MarketProviderError(f"{THE_ODDS_API_PROVIDER} unexpected status ({status})")

            self._last_error = None
            self._last_success_at = _utc_now()
            self._set_state(self._quota_state(), None)
            try:
                return response.json()
            except ValueError as exc:  # pragma: no cover - defensive
                raise MarketProviderError(
                    f"{THE_ODDS_API_PROVIDER} returned non-JSON payload"
                ) from exc

        raise MarketProviderError(f"{THE_ODDS_API_PROVIDER} request exhausted retries")

    def _sport_key(self, competition_id: str) -> str:
        mapping = COMPETITION_TO_SPORT.get(competition_id)
        if mapping is None:
            raise MarketProviderError(
                f"MARKET_COMPETITION_UNSUPPORTED: no The Odds API mapping for {competition_id}"
            )
        if not mapping.enabled or not mapping.verified:
            raise MarketProviderError(
                f"MARKET_COMPETITION_UNVERIFIED: {competition_id} -> {mapping.sport_key} not verified"
            )
        return mapping.sport_key

    def _sport_odds_payload(self, sport_key: str) -> list[dict[str, Any]]:
        cache_key = f"odds:{sport_key}:{self._region()}"
        now = monotonic()
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None and now - cached[0] < self._config.odds_cache_ttl_seconds:
                return cached[1]

        # A fresh league-wide fetch starts a new sync window; reset the
        # event-level add-on market budget so the fan-out cap is per-window,
        # not cumulative.
        self._additional_event_requests_this_window = 0

        payload = self._request_json(
            endpoint=f"sports/{sport_key}/odds",
            params={
                "regions": self._region(),
                "markets": THE_ODDS_MARKETS_REQUESTED,
                "oddsFormat": "decimal",
            },
        )
        if not isinstance(payload, list):
            payload = []
        with self._cache_lock:
            self._cache[cache_key] = (now, payload)
        with self._stats_lock:
            self._stats["events_fetched"] = max(int(self._stats["events_fetched"]), len(payload))
        return payload

    def _match_events(
        self,
        fixture_context: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        kickoff = parse_iso_datetime(str(fixture_context["kickoff_utc"]))
        expected_home = normalize_text_key(str(fixture_context.get("home_team_name") or ""))
        expected_away = normalize_text_key(str(fixture_context.get("away_team_name") or ""))
        matches: list[dict[str, Any]] = []
        for event in events:
            commence_raw = event.get("commence_time")
            if not commence_raw:
                continue
            try:
                commence = parse_iso_datetime(str(commence_raw))
            except ValueError:
                continue
            if abs((commence - kickoff).total_seconds()) > self._config.kickoff_match_tolerance_seconds:
                continue
            home = normalize_text_key(str(event.get("home_team") or ""))
            away = normalize_text_key(str(event.get("away_team") or ""))
            if home != expected_home or away != expected_away:
                continue
            matches.append(event)
        return matches

    @staticmethod
    def _classify_h2h_selection(home_team: str, away_team: str, outcome_name: str) -> str | None:
        normalized = normalize_text_key(outcome_name)
        if normalized == "draw":
            return "DRAW"
        if normalized == normalize_text_key(home_team):
            return "HOME"
        if normalized == normalize_text_key(away_team):
            return "AWAY"
        return None

    @staticmethod
    def _classify_totals_selection(outcome_name: str) -> str | None:
        normalized = normalize_text_key(outcome_name)
        if normalized == "over":
            return TOTALS_SELECTION_OVER
        if normalized == "under":
            return TOTALS_SELECTION_UNDER
        return None

    @staticmethod
    def _classify_btts_selection(outcome_name: str) -> str | None:
        normalized = normalize_text_key(outcome_name)
        if normalized == "yes":
            return BTTS_SELECTION_YES
        if normalized == "no":
            return BTTS_SELECTION_NO
        return None

    @staticmethod
    def _normalize_quarter_step_line(point: Any, *, allow_negative: bool) -> float | None:
        """Return a canonical point line, or None to fail closed."""
        if point is None:
            return None
        try:
            line = float(point)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(line):
            return None
        if not allow_negative and line < 0:
            return None
        if not (line * 4).is_integer():
            return None
        return line

    @classmethod
    def _normalize_totals_line(cls, point: Any) -> float | None:
        """Return a canonical totals line, or None to fail closed.

        A totals line must be numeric, finite, non-negative, and expressed on an
        integer / half / quarter step so the exact line identity Day 11 requires
        is preserved. Missing, malformed, negative, or non-canonical points are
        dropped instead of being emitted as an unsafe snapshot.
        """
        return cls._normalize_quarter_step_line(point, allow_negative=False)

    @classmethod
    def _normalize_asian_handicap_line(cls, point: Any, *, selection: str) -> float | None:
        """Convert a provider handicap point into the repo's home-handicap line.

        The Odds API spread outcomes are expressed from the selected team's point
        of view, while the Route B / Day 10 market rows use a shared
        ``home_handicap`` convention for both HOME and AWAY selections. For
        example, provider outcomes `HOME -0.5` and `AWAY +0.5` must both map to
        internal line `-0.5` so the opposing pair can share one exact market
        identity group without losing signed display semantics.
        """
        raw_line = cls._normalize_quarter_step_line(point, allow_negative=True)
        if raw_line is None:
            return None
        if selection == "HOME":
            return raw_line
        if selection == "AWAY":
            return -raw_line
        return None

    def _freshness_state(
        self,
        *,
        source_timestamp: str | None,
        captured_at: str,
        kickoff_utc: str | None,
    ) -> str:
        return classify_freshness_state(
            source_timestamp=source_timestamp,
            evaluated_at=captured_at,
            threshold_minutes=self._freshness_threshold_minutes(),
            kickoff_utc=kickoff_utc,
        )

    def _snapshots_from_event(
        self,
        *,
        fixture_id: str,
        fixture_context: dict[str, Any],
        event: dict[str, Any],
    ) -> list[NormalizedMarketSnapshot]:
        captured_at = _utc_now()
        event_id = str(event.get("id") or "")
        commence_time = str(event.get("commence_time") or "") or None
        home_team = str(event.get("home_team") or "")
        away_team = str(event.get("away_team") or "")
        reference_bookmakers = self._reference_bookmakers()
        ref_priority = {name: index for index, name in enumerate(reference_bookmakers)}
        snapshots: list[NormalizedMarketSnapshot] = []

        for bookmaker in event.get("bookmakers") or []:
            bookmaker_key = str(bookmaker.get("key") or "").strip()
            bookmaker_title = str(bookmaker.get("title") or "").strip()
            if not bookmaker_key:
                continue
            key_lower = bookmaker_key.lower()
            with self._stats_lock:
                self._stats["bookmakers"][key_lower] = bookmaker_title
            reference_rank = ref_priority.get(key_lower)
            reference_designation = "reference" if reference_rank is not None else "available"

            for market in bookmaker.get("markets") or []:
                market_key = str(market.get("key") or "").strip()
                if market_key not in _SUPPORTED_MARKET_KEYS:
                    continue
                source_timestamp = (
                    str(market.get("last_update") or "").strip()
                    or str(bookmaker.get("last_update") or "").strip()
                    or None
                )
                for outcome in market.get("outcomes") or []:
                    outcome_name = str(outcome.get("name") or "")
                    price_raw = outcome.get("price")
                    if price_raw is None:
                        continue
                    try:
                        price = float(price_raw)
                    except (TypeError, ValueError):
                        continue
                    if price <= 1.0:
                        continue
                    if market_key == THE_ODDS_MARKET_TOTALS:
                        selection = self._classify_totals_selection(outcome_name)
                        if selection is None:
                            continue
                        snapshot_line = self._normalize_totals_line(outcome.get("point"))
                        if snapshot_line is None:
                            continue
                        snapshot_market = MARKET_GOALS_TOTAL
                        snapshot_selection = selection
                    elif market_key == THE_ODDS_MARKET_SPREADS:
                        selection = self._classify_h2h_selection(home_team, away_team, outcome_name)
                        if selection not in {"HOME", "AWAY"}:
                            continue
                        raw_point = outcome.get("point")
                        snapshot_line = self._normalize_asian_handicap_line(raw_point, selection=selection)
                        if snapshot_line is None:
                            continue
                        snapshot_market = MARKET_ASIAN_HANDICAP
                        snapshot_selection = selection
                    else:
                        selection = self._classify_h2h_selection(home_team, away_team, outcome_name)
                        if selection is None:
                            continue
                        snapshot_market = MARKET_1X2
                        snapshot_selection = selection
                        snapshot_line = None
                    provider_market_id = f"{event_id}:{key_lower}:{market_key}:{snapshot_selection}"
                    if snapshot_line is not None:
                        provider_market_id = f"{provider_market_id}:{outcome.get('point')}:{snapshot_line}"
                    snapshots.append(
                        NormalizedMarketSnapshot(
                            provider=self.provider_name(),
                            fixture_id=fixture_id,
                            provider_fixture_id=event_id,
                            captured_at=captured_at,
                            source_timestamp=source_timestamp,
                            market=snapshot_market,
                            selection=snapshot_selection,
                            line=snapshot_line,
                            decimal_odds=price,
                            bookmaker=key_lower,
                            ingestion_version=THE_ODDS_API_INGESTION_VERSION,
                            provider_market_id=provider_market_id,
                            is_pre_match=True,
                            home_team_name=str(fixture_context.get("home_team_name") or ""),
                            away_team_name=str(fixture_context.get("away_team_name") or ""),
                            competition_id=str(fixture_context.get("competition_id") or ""),
                            competition_name=str(fixture_context.get("competition_name") or ""),
                            kickoff_utc=commence_time,
                            freshness_state=self._freshness_state(
                                source_timestamp=source_timestamp,
                                captured_at=captured_at,
                                kickoff_utc=commence_time,
                            ),
                            reference_designation=reference_designation,
                            reference_rank=reference_rank,
                        )
                    )
        return snapshots

    def _event_odds_payload(self, sport_key: str, event_id: str) -> dict[str, Any] | None:
        """Fetch a single event's add-on market odds (for example BTTS/corners).

        Returns the event payload on success, or None when the fan-out cap has
        been reached (fail closed without a request). Raises MarketProviderError
        on transport/provider failures, which the caller isolates so the
        league-wide h2h/totals data is preserved.
        """
        cache_key = f"event-markets:{sport_key}:{self._region()}:{event_id}"
        now = monotonic()
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None and now - cached[0] < self._config.odds_cache_ttl_seconds:
                return cached[1]

        if self._additional_event_requests_this_window >= self._config.max_btts_event_requests:
            with self._stats_lock:
                self._stats["btts_truncated"] = int(self._stats["btts_truncated"]) + 1
                self._stats["additional_markets_truncated"] = int(self._stats["additional_markets_truncated"]) + 1
            return None

        payload = self._request_json(
            endpoint=f"sports/{sport_key}/events/{event_id}/odds",
            params={
                "regions": self._region(),
                "markets": THE_ODDS_EVENT_MARKETS_REQUESTED,
                "oddsFormat": "decimal",
            },
        )
        self._additional_event_requests_this_window += 1
        with self._stats_lock:
            self._stats["btts_event_requests"] = int(self._stats["btts_event_requests"]) + 1
            self._stats["additional_event_requests"] = int(self._stats["additional_event_requests"]) + 1
        if not isinstance(payload, dict):
            payload = {}
        with self._cache_lock:
            self._cache[cache_key] = (now, payload)
        return payload

    def _additional_market_snapshots_from_event(
        self,
        *,
        fixture_id: str,
        fixture_context: dict[str, Any],
        event: dict[str, Any],
    ) -> list[NormalizedMarketSnapshot]:
        captured_at = _utc_now()
        event_id = str(event.get("id") or "")
        commence_time = str(event.get("commence_time") or "") or None
        reference_bookmakers = self._reference_bookmakers()
        ref_priority = {name: index for index, name in enumerate(reference_bookmakers)}
        snapshots: list[NormalizedMarketSnapshot] = []

        for bookmaker in event.get("bookmakers") or []:
            bookmaker_key = str(bookmaker.get("key") or "").strip()
            bookmaker_title = str(bookmaker.get("title") or "").strip()
            if not bookmaker_key:
                continue
            key_lower = bookmaker_key.lower()
            if bookmaker_title:
                with self._stats_lock:
                    self._stats["bookmakers"][key_lower] = bookmaker_title
            reference_rank = ref_priority.get(key_lower)
            reference_designation = "reference" if reference_rank is not None else "available"
            for market in bookmaker.get("markets") or []:
                market_key = str(market.get("key") or "").strip()
                if market_key not in {THE_ODDS_MARKET_BTTS, THE_ODDS_MARKET_CORNERS}:
                    continue
                source_timestamp = (
                    str(market.get("last_update") or "").strip()
                    or str(bookmaker.get("last_update") or "").strip()
                    or None
                )
                for outcome in market.get("outcomes") or []:
                    outcome_name = str(outcome.get("name") or "")
                    price_raw = outcome.get("price")
                    if price_raw is None:
                        continue
                    try:
                        price = float(price_raw)
                    except (TypeError, ValueError):
                        continue
                    if price <= 1.0:
                        continue
                    if market_key == THE_ODDS_MARKET_BTTS:
                        selection = self._classify_btts_selection(outcome_name)
                        if selection is None:
                            continue
                        snapshot_market = MARKET_BTTS
                        snapshot_line = None
                    else:
                        selection = self._classify_totals_selection(outcome_name)
                        if selection is None:
                            continue
                        snapshot_line = self._normalize_totals_line(outcome.get("point"))
                        if snapshot_line is None:
                            continue
                        snapshot_market = MARKET_CORNER_TOTAL
                    provider_market_id = f"{event_id}:{key_lower}:{market_key}:{selection}"
                    if snapshot_line is not None:
                        provider_market_id = f"{provider_market_id}:{snapshot_line}"
                    snapshots.append(
                        NormalizedMarketSnapshot(
                            provider=self.provider_name(),
                            fixture_id=fixture_id,
                            provider_fixture_id=event_id,
                            captured_at=captured_at,
                            source_timestamp=source_timestamp,
                            market=snapshot_market,
                            selection=selection,
                            line=snapshot_line,
                            decimal_odds=price,
                            bookmaker=key_lower,
                            ingestion_version=THE_ODDS_API_INGESTION_VERSION,
                            provider_market_id=provider_market_id,
                            is_pre_match=True,
                            home_team_name=str(fixture_context.get("home_team_name") or ""),
                            away_team_name=str(fixture_context.get("away_team_name") or ""),
                            competition_id=str(fixture_context.get("competition_id") or ""),
                            competition_name=str(fixture_context.get("competition_name") or ""),
                            kickoff_utc=commence_time,
                            freshness_state=self._freshness_state(
                                source_timestamp=source_timestamp,
                                captured_at=captured_at,
                                kickoff_utc=commence_time,
                            ),
                            reference_designation=reference_designation,
                            reference_rank=reference_rank,
                        )
                    )
        return snapshots

    def _fetch_additional_market_snapshots(
        self,
        *,
        fixture_id: str,
        fixture_context: dict[str, Any],
        sport_key: str,
        event: dict[str, Any],
    ) -> list[NormalizedMarketSnapshot]:
        """Fetch and normalize add-on market snapshots for one matched fixture event.

        Fails closed: returns [] on a missing event id, a global blocking
        provider state, a fan-out cap hit, or a request error, so the existing
        h2h/totals snapshots are always preserved.
        """
        event_id = str(event.get("id") or "")
        if not event_id:
            return []
        if self._state in _ADDITIONAL_MARKET_BLOCKING_STATES:
            return []
        try:
            event_payload = self._event_odds_payload(sport_key, event_id)
        except MarketProviderError:
            return []
        if not event_payload:
            return []
        return self._additional_market_snapshots_from_event(
            fixture_id=fixture_id,
            fixture_context=fixture_context,
            event=event_payload,
        )

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
        competition_id = str(fixture_context.get("competition_id") or "")
        sport_key = self._sport_key(competition_id)
        events = self._sport_odds_payload(sport_key)
        if not events:
            self._set_state(STATE_NO_CURRENT_EVENTS, None)
            return []
        matches = self._match_events(fixture_context, events)
        if len(matches) == 0:
            with self._stats_lock:
                self._stats["unmatched"] = int(self._stats["unmatched"]) + 1
            raise MarketProviderError("MARKET_FIXTURE_UNRESOLVED")
        if len(matches) > 1:
            with self._stats_lock:
                self._stats["ambiguous"] = int(self._stats["ambiguous"]) + 1
            raise MarketProviderError("MARKET_FIXTURE_AMBIGUOUS")
        event = matches[0]
        with self._stats_lock:
            self._stats["matched"] = int(self._stats["matched"]) + 1
        snapshots = self._snapshots_from_event(
            fixture_id=fixture_id,
            fixture_context=fixture_context,
            event=event,
        )
        snapshots.extend(
            self._fetch_additional_market_snapshots(
                fixture_id=fixture_id,
                fixture_context=fixture_context,
                sport_key=sport_key,
                event=event,
            )
        )
        return snapshots

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
        with self._stats_lock:
            stats = {key: value for key, value in self._stats.items()}
        with self._quota_lock:
            quota = dict(self._quota)
        state = STATE_NOT_CONFIGURED
        if self.configured():
            state = STATE_IDLE if self._state == STATE_NOT_CONFIGURED else self._state
        return {
            "provider": self.provider_name(),
            "configured": self.configured(),
            "state": state,
            "current_market_decisions": "READY" if self.configured() else "BLOCKED",
            "captured_at": _utc_now(),
            "region": self._region(),
            "reason": None
            if self.configured()
            else f"missing required environment variable {self._config.api_key_env_var}",
            "quota": quota,
            "last_attempt_at": self._last_attempt_at,
            "last_success_at": self._last_success_at,
            "last_error": self._last_error,
            "request_count": self._request_count,
            "events_fetched": stats["events_fetched"],
            "fixtures_matched": stats["matched"],
            "fixtures_unmatched": stats["unmatched"],
            "fixtures_ambiguous": stats["ambiguous"],
            "btts_event_requests": int(stats.get("btts_event_requests", 0)),
            "btts_truncated": int(stats.get("btts_truncated", 0)),
            "additional_event_requests": int(stats.get("additional_event_requests", 0)),
            "additional_markets_truncated": int(stats.get("additional_markets_truncated", 0)),
            "bookmakers": dict(stats["bookmakers"]),
            "competition_mappings": {
                key: {
                    "sport_key": mapping.sport_key,
                    "enabled": mapping.enabled,
                    "verified": mapping.verified,
                    "note": mapping.note,
                }
                for key, mapping in COMPETITION_TO_SPORT.items()
            },
        }
