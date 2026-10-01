"""5DollarFootballAPI bounded SECONDARY current-market provider (Football).

This is a Football-only, secondary Asian-Handicap / Corner-Total reference source.
It is deliberately NOT wired into production decision-making and never overrides
API-Football or The Odds API:

* provider identifier: ``5dollar-football-api``
* role: ``SECONDARY_ONLY`` (cross-check / independent Bet365 reference / forward
  snapshot accumulation / diagnostics only)
* FRA1 only (Ligue 1 provider league id ``3614399544``); every other competition
  fails closed with ``MARKET_COMPETITION_UNSUPPORTED``
* Bet365 only on the free plan

Hard provider limits are 60 requests/hour and 20 requests/minute. This adapter
enforces conservative internal caps (default 50/hour, 15/minute), fails closed
before the provider limit, and caches responses so dashboard refresh cycles do
not repeat identical calls.

The API key is read from ``SPORTS_AI_5DOLLAR_FOOTBALL_API_KEY`` and is never
logged, persisted, or placed in URLs or diagnostics.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from threading import Lock
from time import monotonic
from typing import Any

import httpx

from ..storage.football_canonical_fixture import (
    canonical_team_name,
    normalize_kickoff_utc,
)
from ..storage.football_paper_trading import StoredMarketSnapshot
from .providers import MarketProviderError, NormalizedMarketSnapshot

# --------------------------------------------------------------------------- constants
PROVIDER = "5dollar-football-api"
INGESTION_VERSION = "five_dollar_football_current_market_v1"
ROLE = "SECONDARY_ONLY"
BASE_URL = "https://api.5dollarfootballapi.com/v1"

ENV_API_KEY = "SPORTS_AI_5DOLLAR_FOOTBALL_API_KEY"
ENV_ENABLED = "SPORTS_AI_5DOLLAR_ENABLED"
ENV_MAX_PER_HOUR = "SPORTS_AI_5DOLLAR_MAX_REQUESTS_PER_HOUR"
ENV_MAX_PER_MINUTE = "SPORTS_AI_5DOLLAR_MAX_REQUESTS_PER_MINUTE"

# Documented Ligue 1 provider league id. The legacy id 25 is INVALID and must
# never be used (the provider returns league_not_found for it).
FRA1_LEAGUE_ID = 3614399544
LEGACY_INVALID_LEAGUE_ID = 25
COMPETITION_TO_LEAGUE: Mapping[str, int] = {"FRA1": FRA1_LEAGUE_ID}

REFERENCE_BOOKMAKER_SLUG = "bet365"
REFERENCE_BOOKMAKER_NAME = "Bet 365"

MARKET_ASIAN_HANDICAP = "asian_handicap"
MARKET_CORNER_TOTAL = "corner_total"
SELECTION_HOME = "HOME"
SELECTION_AWAY = "AWAY"
SELECTION_OVER = "OVER"
SELECTION_UNDER = "UNDER"

PHASE_OPENING = "opening"
PHASE_CLOSING = "closing"
PHASE_INPLAY = "inplay"
PHASES = (PHASE_OPENING, PHASE_CLOSING, PHASE_INPLAY)
_PRE_MATCH_PHASES = frozenset({PHASE_OPENING, PHASE_CLOSING})

FOCUSED_MARKETS = frozenset({MARKET_ASIAN_HANDICAP, MARKET_CORNER_TOTAL})

# Match classification (never silently drop, never nearest-match guess).
MATCH_MATCHED = "MATCHED"
MATCH_UNMATCHED = "UNMATCHED"
MATCH_AMBIGUOUS = "AMBIGUOUS"

# Provider hard limits (documented) and conservative internal defaults.
PROVIDER_MAX_PER_HOUR = 60
PROVIDER_MAX_PER_MINUTE = 20
DEFAULT_MAX_PER_HOUR = 50
DEFAULT_MAX_PER_MINUTE = 15

DEFAULT_KICKOFF_TOLERANCE_SECONDS = 0
DEFAULT_CACHE_TTL_SECONDS = 300

# Provider-vs-API-Football FRA1 naming differences. Applied AFTER the shared
# normalize/canonical step. Keys/values are alphanumeric-normalized. These are
# reviewed, deterministic equivalences (never fuzzy) and only for FRA1.
PROVIDER_TEAM_ALIASES: Mapping[str, str] = {
    "psg": "parissaintgermain",
    "parissaintgermain": "parissaintgermain",
    "brest": "stadebrestois29",
    "stadebrestois29": "stadebrestois29",
    "troyes": "estactroyes",
    "estactroyes": "estactroyes",
}


def adapter_team_name(value: Any) -> str:
    """Canonical team key with FRA1 provider-alias normalization applied."""
    key = canonical_team_name(value)
    return PROVIDER_TEAM_ALIASES.get(key, key)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def is_pre_match_phase(phase: str) -> bool:
    """Opening and closing are pre-match; in-play is NEVER pre-match."""
    return phase in _PRE_MATCH_PHASES


@dataclass(frozen=True)
class FiveDollarConfig:
    api_key_env_var: str = ENV_API_KEY
    enabled_env_var: str = ENV_ENABLED
    max_per_hour_env_var: str = ENV_MAX_PER_HOUR
    max_per_minute_env_var: str = ENV_MAX_PER_MINUTE
    base_url: str = BASE_URL
    reference_bookmaker_slug: str = REFERENCE_BOOKMAKER_SLUG
    timeout_seconds: float = 20.0
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS
    kickoff_tolerance_seconds: int = DEFAULT_KICKOFF_TOLERANCE_SECONDS
    max_requests_per_hour: int = DEFAULT_MAX_PER_HOUR
    max_requests_per_minute: int = DEFAULT_MAX_PER_MINUTE
    user_agent: str = "sports-edge-ai/0.1 (5dollar-football secondary current market provider)"

    @classmethod
    def from_env(cls) -> FiveDollarConfig:
        return cls(
            max_requests_per_hour=_env_int(ENV_MAX_PER_HOUR, DEFAULT_MAX_PER_HOUR),
            max_requests_per_minute=_env_int(ENV_MAX_PER_MINUTE, DEFAULT_MAX_PER_MINUTE),
        )

    def api_key_present(self) -> bool:
        return bool(os.environ.get(self.api_key_env_var, "").strip())

    def enabled(self) -> bool:
        return _env_bool(self.enabled_env_var, True)


@dataclass(frozen=True)
class FiveDollarQuote:
    """One normalized Bet365 price at an explicit phase/line.

    ``phase`` is first-class so opening / closing / in-play are never treated as
    interchangeable snapshots. ``line`` is the exact provider line (no rounding);
    for asian_handicap it is the canonical HOME-team handicap shared by both
    selections (matching the repo's ``home_handicap`` convention).
    """

    provider: str
    provider_fixture_id: str
    fixture_id: str
    competition_id: str
    bookmaker: str
    market: str
    phase: str
    selection: str
    line: float
    decimal_odds: float
    captured_at: str
    source_timestamp: str | None = None
    kickoff_utc: str | None = None
    home_team_name: str | None = None
    away_team_name: str | None = None
    competition_name: str | None = None
    ingestion_version: str = INGESTION_VERSION

    @property
    def is_pre_match(self) -> bool:
        return is_pre_match_phase(self.phase)

    def provider_market_id(self) -> str:
        # The existing market_snapshots table has no ``phase`` column, so the
        # phase is encoded here (documented gap) while remaining append-only.
        return f"{self.bookmaker}:{self.market}:{self.phase}:{self.line}"

    def to_normalized_snapshot(self) -> NormalizedMarketSnapshot:
        return NormalizedMarketSnapshot(
            provider=self.provider,
            fixture_id=self.fixture_id,
            captured_at=self.captured_at,
            market=self.market,
            selection=self.selection,
            decimal_odds=self.decimal_odds,
            bookmaker=self.bookmaker,
            ingestion_version=self.ingestion_version,
            line=self.line,
            provider_market_id=self.provider_market_id(),
            source_timestamp=self.source_timestamp,
            is_pre_match=self.is_pre_match,
            provider_fixture_id=self.provider_fixture_id,
            home_team_name=self.home_team_name,
            away_team_name=self.away_team_name,
            competition_id=self.competition_id,
            competition_name=self.competition_name,
            kickoff_utc=self.kickoff_utc,
        )

    def to_stored_snapshot(self) -> StoredMarketSnapshot:
        return StoredMarketSnapshot(
            fixture_id=self.fixture_id,
            provider=self.provider,
            bookmaker=self.bookmaker,
            market=self.market,
            selection=self.selection,
            odds=self.decimal_odds,
            ingestion_version=self.ingestion_version,
            line=self.line,
            provider_market_id=self.provider_market_id(),
            provider_timestamp=self.source_timestamp,
            captured_at=self.captured_at,
            is_pre_match=self.is_pre_match,
            provider_fixture_id=self.provider_fixture_id,
            home_team_name=self.home_team_name,
            away_team_name=self.away_team_name,
            competition_id=self.competition_id,
            competition_name=self.competition_name,
            kickoff_utc=self.kickoff_utc,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["is_pre_match"] = self.is_pre_match
        return payload


@dataclass(frozen=True)
class FixtureMatch:
    state: str
    provider_fixture_id: str | None = None
    kickoff_utc: str | None = None
    home_team_name: str | None = None
    away_team_name: str | None = None
    candidate_count: int = 0


class FiveDollarRateGovernor:
    """Conservative request governor below the provider's hard limits."""

    def __init__(self, *, max_per_hour: int, max_per_minute: int, clock: Callable[[], float] = monotonic) -> None:
        self._max_hour = max(0, int(max_per_hour))
        self._max_minute = max(0, int(max_per_minute))
        self._clock = clock
        self._hour: list[float] = []
        self._minute: list[float] = []
        self._lock = Lock()
        self.requests_allowed = 0
        self.blocked_by_hour_cap = 0
        self.blocked_by_minute_cap = 0

    def _prune(self, now: float) -> None:
        self._hour = [t for t in self._hour if t > now - 3600]
        self._minute = [t for t in self._minute if t > now - 60]

    def try_acquire(self) -> bool:
        with self._lock:
            now = self._clock()
            self._prune(now)
            if len(self._hour) >= self._max_hour:
                self.blocked_by_hour_cap += 1
                return False
            if len(self._minute) >= self._max_minute:
                self.blocked_by_minute_cap += 1
                return False
            self._hour.append(now)
            self._minute.append(now)
            self.requests_allowed += 1
            return True

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            now = self._clock()
            self._prune(now)
            return {
                "requests_this_hour": len(self._hour),
                "requests_this_minute": len(self._minute),
                "max_per_hour": self._max_hour,
                "max_per_minute": self._max_minute,
                "requests_allowed": self.requests_allowed,
                "blocked_by_hour_cap": self.blocked_by_hour_cap,
                "blocked_by_minute_cap": self.blocked_by_minute_cap,
            }


class FiveDollarFootballProvider:
    """Bounded secondary provider for FRA1 Bet365 AH + corner totals."""

    def __init__(
        self,
        config: FiveDollarConfig | None = None,
        *,
        client_factory: Callable[[], httpx.Client] | None = None,
        clock: Callable[[], float] = monotonic,
        governor: FiveDollarRateGovernor | None = None,
    ) -> None:
        self._config = config or FiveDollarConfig.from_env()
        self._client_factory = client_factory
        self._now = clock
        self._cache: dict[str, tuple[float, int, dict[str, Any]]] = {}
        self._cache_lock = Lock()
        self._governor = governor or FiveDollarRateGovernor(
            max_per_hour=self._config.max_requests_per_hour,
            max_per_minute=self._config.max_requests_per_minute,
            clock=clock,
        )
        self.cache_hits = 0
        self.requests_avoided = 0
        self.cache_misses = 0
        self.provider_errors = 0
        self.last_status: int | None = None
        self._query_log: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ identity
    def provider_name(self) -> str:
        return PROVIDER

    def role(self) -> str:
        return ROLE

    def configured(self) -> bool:
        return self._config.api_key_present()

    def enabled(self) -> bool:
        return self._config.enabled() and self.configured()

    def league_id(self, competition_id: str) -> int:
        key = str(competition_id or "").strip().upper()
        league_id = COMPETITION_TO_LEAGUE.get(key)
        if league_id is None:
            raise MarketProviderError("MARKET_COMPETITION_UNSUPPORTED")
        return league_id

    def _api_key(self) -> str:
        key = os.environ.get(self._config.api_key_env_var, "").strip()
        if not key:
            raise MarketProviderError("PROVIDER_NOT_CONFIGURED")
        return key

    # ------------------------------------------------------------------ transport
    def _client(self) -> httpx.Client:
        if self._client_factory is not None:
            return self._client_factory()
        return httpx.Client(
            timeout=self._config.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": self._config.user_agent},
        )

    @staticmethod
    def sanitize(text: Any) -> str:
        out = str(text)
        for marker in ("Bearer ", "apiKey=", "X-API-Key"):
            if marker in out:
                out = out.replace(marker, f"{marker}[REDACTED]")
        return out

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        with self._cache_lock:
            entry = self._cache.get(key)
        if entry is None:
            return None
        created, status, payload = entry
        if self._now() - created > self._config.cache_ttl_seconds:
            with self._cache_lock:
                self._cache.pop(key, None)
            return None
        self.cache_hits += 1
        self.requests_avoided += 1
        return {"status": status, "payload": payload, "cache": True}

    def _cache_set(self, key: str, status: int, payload: dict[str, Any]) -> None:
        with self._cache_lock:
            self._cache[key] = (self._now(), status, payload)

    def _request(self, path: str, params: Mapping[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        """Low-level governed + cached request. Never logs the key."""
        params = dict(params or {})
        cache_key = json.dumps({"path": path, "params": params}, sort_keys=True)
        cached = self._cache_get(cache_key)
        if cached is not None:
            self.last_status = int(cached["status"])
            return int(cached["status"]), cached["payload"]

        self.cache_misses += 1
        if not self._governor.try_acquire():
            # Fail closed before the provider limit; do not raise into callers.
            self.provider_errors += 1
            return 429, {"success": 0, "error": {"code": "local_rate_cap"}}

        key = self._api_key()
        url = f"{self._config.base_url}{path}"
        try:
            with self._client() as client:
                response = client.get(url, params=params, headers={"Authorization": f"Bearer {key}"})
                status = int(response.status_code)
                try:
                    payload = response.json()
                except ValueError:
                    payload = {"success": 0, "error": {"code": "non_json"}}
        except httpx.HTTPError as exc:
            self.provider_errors += 1
            self.last_status = None
            self._query_log.append({"path": path, "params": params, "status": "TRANSPORT_ERROR", "error": type(exc).__name__})
            return 0, {"success": 0, "error": {"code": "transport_error"}}

        self.last_status = status
        self._query_log.append(
            {
                "path": path,
                "params": params,
                "status": status,
                "rows": (payload.get("data") if isinstance(payload, dict) else None) is not None,
            }
        )
        if status == 200 and isinstance(payload, dict):
            self._cache_set(cache_key, status, payload)
        return status, payload if isinstance(payload, dict) else {}

    # ------------------------------------------------------------------ matching
    def _fixtures_window(self, fixture_context: Mapping[str, Any]) -> list[dict[str, Any]]:
        league_id = self.league_id(str(fixture_context.get("competition_id") or ""))
        params: dict[str, Any] = {"per_page": 100}
        kickoff = normalize_kickoff_utc(fixture_context.get("kickoff_utc"))
        if kickoff:
            try:
                ts = int(datetime.fromisoformat(kickoff).timestamp())
                params["start_time"] = ts - 86_400
                params["end_time"] = ts + 86_400
            except ValueError:
                pass
        status, payload = self._request(f"/leagues/{league_id}/fixtures", params)
        if status != 200:
            raise MarketProviderError(f"PROVIDER_ERROR status={status}")
        data = payload.get("data")
        return list(data) if isinstance(data, list) else []

    def match_fixture(self, fixture_context: Mapping[str, Any]) -> FixtureMatch:
        """Deterministic MATCHED / UNMATCHED / AMBIGUOUS (no fuzzy guessing)."""
        expected_home = adapter_team_name(fixture_context.get("home_team_name"))
        expected_away = adapter_team_name(fixture_context.get("away_team_name"))
        expected_kickoff = normalize_kickoff_utc(fixture_context.get("kickoff_utc"))
        candidates: list[dict[str, Any]] = []
        for fixture in self._fixtures_window(fixture_context):
            teams = fixture.get("teams") or {}
            home = adapter_team_name((teams.get("home") or {}).get("name"))
            away = adapter_team_name((teams.get("away") or {}).get("name"))
            if home != expected_home or away != expected_away:
                continue
            candidate_kickoff = normalize_kickoff_utc(fixture.get("kickoff_utc"))
            if (
                expected_kickoff
                and candidate_kickoff
                and self._config.kickoff_tolerance_seconds <= 0
                and candidate_kickoff != expected_kickoff
            ):
                continue
            candidates.append(fixture)

        if len(candidates) == 1:
            fixture = candidates[0]
            return FixtureMatch(
                state=MATCH_MATCHED,
                provider_fixture_id=str(fixture.get("id")),
                kickoff_utc=normalize_kickoff_utc(fixture.get("kickoff_utc")),
                home_team_name=((fixture.get("teams") or {}).get("home") or {}).get("name"),
                away_team_name=((fixture.get("teams") or {}).get("away") or {}).get("name"),
                candidate_count=1,
            )
        if not candidates:
            return FixtureMatch(state=MATCH_UNMATCHED, candidate_count=0)
        return FixtureMatch(state=MATCH_AMBIGUOUS, candidate_count=len(candidates))

    # ------------------------------------------------------------------ odds
    @staticmethod
    def _phase_entries(container: Any) -> dict[str, Any]:
        if not isinstance(container, dict):
            return {}
        return {phase: container.get(phase) for phase in PHASES}

    def _quotes_from_event(
        self,
        *,
        fixture_id: str,
        competition_id: str,
        provider_fixture_id: str,
        event: Mapping[str, Any],
        captured_at: str,
    ) -> list[FiveDollarQuote]:
        kickoff = normalize_kickoff_utc(event.get("kickoff_utc"))
        home = ((event.get("teams") or {}).get("home") or {}).get("name")
        away = ((event.get("teams") or {}).get("away") or {}).get("name")
        league = event.get("league") or {}
        quotes: list[FiveDollarQuote] = []
        for bookmaker in event.get("bookmakers") or []:
            slug = str(bookmaker.get("slug") or "").strip().lower()
            if slug != self._config.reference_bookmaker_slug:
                continue
            odds = bookmaker.get("odds") or {}
            # Asian handicap: provider line IS the canonical HOME handicap and is
            # shared by both selections (repo home_handicap convention).
            for phase, entry in self._phase_entries(odds.get("asian_handicap")).items():
                if not isinstance(entry, dict) or entry.get("line") is None:
                    continue
                line = float(entry["line"])
                for selection, price_key in ((SELECTION_HOME, "home"), (SELECTION_AWAY, "away")):
                    price = entry.get(price_key)
                    if price is None:
                        continue
                    quotes.append(
                        FiveDollarQuote(
                            provider=PROVIDER,
                            provider_fixture_id=provider_fixture_id,
                            fixture_id=fixture_id,
                            competition_id=competition_id,
                            bookmaker=REFERENCE_BOOKMAKER_NAME,
                            market=MARKET_ASIAN_HANDICAP,
                            phase=phase,
                            selection=selection,
                            line=line,
                            decimal_odds=float(price),
                            captured_at=captured_at,
                            kickoff_utc=kickoff,
                            home_team_name=home,
                            away_team_name=away,
                            competition_name=league.get("name"),
                        )
                    )
            # Corner total: exact line preserved, OVER/UNDER.
            for phase, entry in self._phase_entries(odds.get("corner_line")).items():
                if not isinstance(entry, dict) or entry.get("line") is None:
                    continue
                line = float(entry["line"])
                for selection, price_key in ((SELECTION_OVER, "over"), (SELECTION_UNDER, "under")):
                    price = entry.get(price_key)
                    if price is None:
                        continue
                    quotes.append(
                        FiveDollarQuote(
                            provider=PROVIDER,
                            provider_fixture_id=provider_fixture_id,
                            fixture_id=fixture_id,
                            competition_id=competition_id,
                            bookmaker=REFERENCE_BOOKMAKER_NAME,
                            market=MARKET_CORNER_TOTAL,
                            phase=phase,
                            selection=selection,
                            line=line,
                            decimal_odds=float(price),
                            captured_at=captured_at,
                            kickoff_utc=kickoff,
                            home_team_name=home,
                            away_team_name=away,
                            competition_name=league.get("name"),
                        )
                    )
        return quotes

    def get_fixture_quotes(
        self,
        fixture_id: str,
        *,
        fixture_context: Mapping[str, Any] | None = None,
    ) -> list[FiveDollarQuote]:
        """Return phase-labelled Bet365 AH + corner quotes for one matched fixture.

        One fixture-list request (windowed) + one odds request (all markets in one
        call). Unsupported competition or unmatched/ambiguous fixture fails closed.
        """
        if not self.enabled():
            raise MarketProviderError("PROVIDER_NOT_CONFIGURED")
        if fixture_context is None:
            raise MarketProviderError("MARKET_FIXTURE_CONTEXT_REQUIRED")
        competition_id = str(fixture_context.get("competition_id") or "")
        self.league_id(competition_id)  # fail closed if unsupported
        match = self.match_fixture(fixture_context)
        if match.state != MATCH_MATCHED or not match.provider_fixture_id:
            raise MarketProviderError(f"MARKET_FIXTURE_{match.state}")

        status, payload = self._request(f"/fixtures/{match.provider_fixture_id}/odds")
        if status != 200:
            raise MarketProviderError(f"PROVIDER_ERROR status={status}")
        event = payload.get("data") or {}
        if not isinstance(event, dict):
            return []
        captured_at = _utc_now()
        quotes = self._quotes_from_event(
            fixture_id=fixture_id,
            competition_id=competition_id,
            provider_fixture_id=match.provider_fixture_id,
            event={**event, "teams": event.get("teams") or {"home": {"name": match.home_team_name}, "away": {"name": match.away_team_name}}, "kickoff_utc": event.get("kickoff_utc") or match.kickoff_utc},
            captured_at=captured_at,
        )
        return quotes

    def get_pre_match_quotes(
        self,
        fixture_id: str,
        *,
        fixture_context: Mapping[str, Any] | None = None,
        markets: Sequence[str] | None = None,
    ) -> list[FiveDollarQuote]:
        """Pre-match-only quotes (in-play is hard-excluded from pre-match use)."""
        wanted = set(markets) if markets else set(FOCUSED_MARKETS)
        return [
            quote
            for quote in self.get_fixture_quotes(fixture_id, fixture_context=fixture_context)
            if quote.is_pre_match and quote.market in wanted
        ]

    # ------------------------------------------------------------------ protocol compat
    def get_fixture_markets(
        self,
        fixture_id: str,
        *,
        fixture_context: Mapping[str, Any] | None = None,
    ) -> list[NormalizedMarketSnapshot]:
        return [q.to_normalized_snapshot() for q in self.get_fixture_quotes(fixture_id, fixture_context=fixture_context)]

    def get_best_prices(
        self,
        fixture_id: str,
        *,
        fixture_context: Mapping[str, Any] | None = None,
    ) -> list[NormalizedMarketSnapshot]:
        best: dict[tuple[str, str, float | None], NormalizedMarketSnapshot] = {}
        for snapshot in self.get_fixture_markets(fixture_id, fixture_context=fixture_context):
            key = (snapshot.market, snapshot.selection, snapshot.line)
            current = best.get(key)
            if current is None or snapshot.decimal_odds > current.decimal_odds:
                best[key] = snapshot
        return list(best.values())

    def get_market_snapshot(
        self,
        fixture_id: str,
        *,
        fixture_context: Mapping[str, Any] | None = None,
    ) -> list[NormalizedMarketSnapshot]:
        return self.get_fixture_markets(fixture_id, fixture_context=fixture_context)

    # ------------------------------------------------------------------ diagnostics
    def diagnostics(self) -> dict[str, Any]:
        return {
            "provider": PROVIDER,
            "role": ROLE,
            "configured": self.configured(),
            "enabled": self.enabled(),
            "reference_bookmaker": REFERENCE_BOOKMAKER_NAME,
            "competitions": sorted(COMPETITION_TO_LEAGUE),
            "league_ids": dict(COMPETITION_TO_LEAGUE),
            "provider_limits": {"per_hour": PROVIDER_MAX_PER_HOUR, "per_minute": PROVIDER_MAX_PER_MINUTE},
            "governor": self._governor.snapshot(),
            "cache": {"hits": self.cache_hits, "misses": self.cache_misses, "avoided": self.requests_avoided},
            "provider_errors": self.provider_errors,
            "last_status": self.last_status,
            "queries": [dict(entry) for entry in self._query_log[-20:]],
        }


# --------------------------------------------------------------------------- helpers
def compare_provider_snapshots(
    primary: Sequence[Mapping[str, Any]],
    secondary: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Read-only cross-provider comparison. Never averages or picks a winner."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, float | None]] = set()
    for snapshot in list(primary) + list(secondary):
        key = (str(snapshot.get("market")), str(snapshot.get("selection")), snapshot.get("line"))
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "market": key[0],
                "selection": key[1],
                "line": key[2],
                "api_football": [s.get("decimal_odds") for s in primary if (str(s.get("market")), str(s.get("selection")), s.get("line")) == key],
                "five_dollar": [s.get("decimal_odds") for s in secondary if (str(s.get("market")), str(s.get("selection")), s.get("line")) == key],
            }
        )
    return rows


def persist_quotes(conn, quotes: Sequence[FiveDollarQuote]) -> list[int]:
    """Append-only persistence into the existing ``market_snapshots`` table.

    The existing table has no ``phase`` column; the phase is encoded in
    ``provider_market_id`` (``bookmaker:market:phase:line``) to avoid creating a
    competing snapshot table. In-play rows are stored with ``is_pre_match=0``.
    """
    from ..storage import football_paper_trading as paper_store

    ids: list[int] = []
    for quote in quotes:
        ids.append(paper_store.record_market_snapshot(conn, quote.to_stored_snapshot()))
    return ids
