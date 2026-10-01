"""FRA1-only shadow collection for the dormant 5DollarFootballProvider.

This module is an ISOLATED observational collector. It is deliberately kept out
of the application decision flow:

* it is enabled only when BOTH ``SPORTS_AI_5DOLLAR_ENABLED`` and
  ``SPORTS_AI_5DOLLAR_SHADOW_ENABLED`` are explicitly truthy;
* it is never wired as the application's decision market provider;
* it never imports or calls any market-evaluation, paper-entry creation, or
  decision path; the only storage it touches is ``market_snapshots``;
* it does not create paper entries, candidates, or bet decisions;
* it does not average prices, pick a provider, or act as a fallback.

It only discovers eligible FRA1 fixtures, reads Bet365 AH + corner quotes (all
phases), appends them to the existing ``market_snapshots`` table idempotently,
and produces read-only diagnostics (including a comparison against any
API-Football observations that already exist).
"""

from __future__ import annotations

import os
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from time import monotonic
from typing import Any

from .five_dollar_football import (
    MARKET_ASIAN_HANDICAP,
    MARKET_CORNER_TOTAL,
    FiveDollarConfig,
    FiveDollarFootballProvider,
    adapter_team_name,
)
from .providers import MarketProviderError

ENV_SHADOW_ENABLED = "SPORTS_AI_5DOLLAR_SHADOW_ENABLED"

# FRA1 only. Any other competition is skipped WITHOUT any provider call.
SHADOW_COMPETITIONS = frozenset({"FRA1"})

DEFAULT_MAX_PROVIDER_CALLS_PER_CYCLE = 5
DEFAULT_MAX_PROVIDER_CALLS_PER_HOUR = 20

# Fixture statuses considered suitable for shadow collection ('' = unknown, allow).
_ELIGIBLE_STATUSES = frozenset(
    {"", "scheduled", "upcoming", "pre", "prematch", "pre_match", "finished", "ft"}
)

_FIVE_DOLLAR_PROVIDER = "5dollar-football-api"
_API_FOOTBALL_PROVIDER = "api-football"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ShadowConfig:
    shadow_enabled_env_var: str = ENV_SHADOW_ENABLED
    max_provider_calls_per_cycle: int = DEFAULT_MAX_PROVIDER_CALLS_PER_CYCLE
    max_provider_calls_per_hour: int = DEFAULT_MAX_PROVIDER_CALLS_PER_HOUR

    def shadow_flag(self) -> bool:
        return _env_bool(self.shadow_enabled_env_var, False)


class ShadowBudget:
    """Stricter-than-provider shadow request budget (per cycle + per hour)."""

    def __init__(self, *, max_per_cycle: int, max_per_hour: int, clock: Callable[[], float] = monotonic) -> None:
        self._max_cycle = max(0, int(max_per_cycle))
        self._max_hour = max(0, int(max_per_hour))
        self._clock = clock
        self._events: deque[tuple[float, int]] = deque()
        self._cycle_calls = 0

    def start_cycle(self) -> None:
        self._cycle_calls = 0

    def _prune(self, now: float) -> None:
        while self._events and self._events[0][0] <= now - 3600:
            self._events.popleft()

    def hourly_calls(self) -> int:
        self._prune(self._clock())
        return sum(n for _t, n in self._events)

    def can_spend(self, n: int) -> bool:
        self._prune(self._clock())
        return (self._cycle_calls + n) <= self._max_cycle and (self.hourly_calls() + n) <= self._max_hour

    def record(self, n: int) -> None:
        if n <= 0:
            return
        self._events.append((self._clock(), int(n)))
        self._cycle_calls += int(n)

    def snapshot(self) -> dict[str, int]:
        return {
            "cycle_calls": self._cycle_calls,
            "hourly_calls": self.hourly_calls(),
            "max_per_cycle": self._max_cycle,
            "max_per_hour": self._max_hour,
        }


def _empty_counters() -> dict[str, int]:
    return {
        "fixtures_considered": 0,
        "fixtures_matched": 0,
        "fixtures_unmatched": 0,
        "fixtures_ambiguous": 0,
        "provider_calls": 0,
        "cache_hits": 0,
        "snapshots_written": 0,
        "snapshots_reused": 0,
        "ah_quotes": 0,
        "corner_quotes": 0,
        "alias_misses": 0,
        "rate_blocked": 0,
    }


class FiveDollarShadowCollector:
    """Isolated FRA1-only shadow collector. Observational only."""

    def __init__(
        self,
        *,
        provider: Any | None = None,
        config: ShadowConfig | None = None,
        conn: Any | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._provider = provider or FiveDollarFootballProvider(FiveDollarConfig.from_env())
        self._config = config or ShadowConfig()
        self._conn = conn
        self._clock = clock
        self._budget = ShadowBudget(
            max_per_cycle=self._config.max_provider_calls_per_cycle,
            max_per_hour=self._config.max_provider_calls_per_hour,
            clock=clock,
        )
        self._counters = _empty_counters()
        self._alias_candidates: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ enablement
    def shadow_enabled(self) -> bool:
        """True only when BOTH the provider flag and the shadow flag are set."""
        return self._config.shadow_flag() and bool(self._provider.enabled())

    # ------------------------------------------------------------------ eligibility
    @staticmethod
    def eligible(fixture_row: Mapping[str, Any]) -> bool:
        competition = str(fixture_row.get("competition_id") or "").strip().upper()
        if competition not in SHADOW_COMPETITIONS:
            return False
        if not fixture_row.get("home_team_name") or not fixture_row.get("away_team_name"):
            return False
        if not fixture_row.get("kickoff_utc"):
            return False
        status = str(fixture_row.get("status") or "").strip().lower()
        return status in _ELIGIBLE_STATUSES

    # ------------------------------------------------------------------ provider introspection
    def _provider_diagnostics(self) -> dict[str, Any]:
        try:
            return self._provider.diagnostics()
        except Exception:  # noqa: BLE001
            return {}

    def _provider_calls(self) -> int:
        return int((self._provider_diagnostics().get("governor") or {}).get("requests_allowed") or 0)

    def _cache_hits(self) -> int:
        return int((self._provider_diagnostics().get("cache") or {}).get("hits") or 0)

    # ------------------------------------------------------------------ persistence
    def _persist(self, quotes: Sequence[Any]) -> tuple[int, int]:
        from ..storage import football_paper_trading as paper

        written = reused = 0
        for quote in quotes:
            stored = quote.to_stored_snapshot()
            if paper.find_identical_market_snapshot(self._conn, stored) is not None:
                reused += 1
            else:
                paper.record_market_snapshot(self._conn, stored)
                written += 1
        return written, reused

    # ------------------------------------------------------------------ alias evidence
    def _record_alias_evidence(self, fixture_context: Mapping[str, Any]) -> None:
        self._counters["alias_misses"] += 1
        try:
            window = self._provider._fixtures_window(fixture_context)  # cached; no extra call
        except Exception:  # noqa: BLE001
            return
        expected = {adapter_team_name(fixture_context.get("home_team_name")), adapter_team_name(fixture_context.get("away_team_name"))}
        kickoff = str(fixture_context.get("kickoff_utc") or "")
        for fixture in window or []:
            teams = fixture.get("teams") or {}
            names = [(teams.get("home") or {}).get("name"), (teams.get("away") or {}).get("name")]
            provider_names = {adapter_team_name(names[0]), adapter_team_name(names[1])}
            if provider_names & expected and str(fixture.get("kickoff_utc") or "") == kickoff:
                self._alias_candidates.append(
                    {
                        "expected": [fixture_context.get("home_team_name"), fixture_context.get("away_team_name")],
                        "provider": names,
                        "kickoff_utc": fixture.get("kickoff_utc"),
                    }
                )

    # ------------------------------------------------------------------ collection
    def collect(self, fixtures: Iterable[Mapping[str, Any]], *, persist: bool = True) -> dict[str, Any]:
        self._counters = _empty_counters()
        self._alias_candidates = []
        self._budget.start_cycle()

        if not self.shadow_enabled():
            return self._result()

        for row in fixtures:
            competition = str(row.get("competition_id") or "").strip().upper()
            if competition not in SHADOW_COMPETITIONS:
                continue  # no provider call for unsupported competitions
            if not self.eligible(row):
                continue
            self._counters["fixtures_considered"] += 1

            if not self._budget.can_spend(2):
                self._counters["rate_blocked"] += 1
                continue

            context = {
                "competition_id": competition,
                "home_team_name": row.get("home_team_name"),
                "away_team_name": row.get("away_team_name"),
                "kickoff_utc": row.get("kickoff_utc"),
            }
            fixture_id = str(row.get("fixture_id") or "")

            calls_before = self._provider_calls()
            cache_before = self._cache_hits()
            try:
                match = self._provider.match_fixture(context)
            except MarketProviderError:
                self._counters["fixtures_unmatched"] += 1
                self._record_alias_evidence(context)
                continue

            state = getattr(match, "state", "UNMATCHED")
            if state != "MATCHED":
                if state == "AMBIGUOUS":
                    self._counters["fixtures_ambiguous"] += 1
                else:
                    self._counters["fixtures_unmatched"] += 1
                    self._record_alias_evidence(context)
                continue

            self._counters["fixtures_matched"] += 1
            provider_fixture_id = str(getattr(match, "provider_fixture_id", "") or "")
            if not fixture_id:
                fixture_id = f"{_FIVE_DOLLAR_PROVIDER}:{provider_fixture_id}"
            try:
                quotes = self._provider.get_fixture_quotes(fixture_id, fixture_context=context)
            except MarketProviderError:
                self._counters["fixtures_unmatched"] += 1
                continue

            delta_calls = max(0, self._provider_calls() - calls_before)
            self._budget.record(delta_calls)
            self._counters["provider_calls"] += delta_calls
            self._counters["cache_hits"] += max(0, self._cache_hits() - cache_before)
            self._counters["ah_quotes"] += sum(1 for q in quotes if q.market == MARKET_ASIAN_HANDICAP)
            self._counters["corner_quotes"] += sum(1 for q in quotes if q.market == MARKET_CORNER_TOTAL)

            if persist and self._conn is not None and quotes:
                written, reused = self._persist(quotes)
                self._counters["snapshots_written"] += written
                self._counters["snapshots_reused"] += reused

        return self._result()

    # ------------------------------------------------------------------ comparison (read-only)
    def compare_with_api_football(self, fixture_id: str) -> list[dict[str, Any]]:
        """Observation-only comparison. No averaging, no replacement, no winner."""
        if self._conn is None:
            return []
        from ..storage import football_paper_trading as paper

        rows = paper.fetch_market_snapshots(self._conn, fixture_id=fixture_id)
        api = {self._key(r): r for r in rows if r.get("provider") == _API_FOOTBALL_PROVIDER}
        five = {self._key(r): r for r in rows if r.get("provider") == _FIVE_DOLLAR_PROVIDER}
        out: list[dict[str, Any]] = []
        for key in sorted(set(api) | set(five), key=lambda k: (str(k[0]), str(k[1]), k[2] if k[2] is not None else 0.0)):
            a = api.get(key)
            f = five.get(key)
            out.append(
                {
                    "market": key[0],
                    "selection": key[1],
                    "line": key[2],
                    "five_dollar_price": f.get("odds") if f else None,
                    "api_football_price": a.get("odds") if a else None,
                    "difference": (f.get("odds") - a.get("odds")) if (f and a) else None,
                    "five_dollar_captured_at": f.get("captured_at") if f else None,
                    "api_football_captured_at": a.get("captured_at") if a else None,
                }
            )
        return out

    @staticmethod
    def _key(row: Mapping[str, Any]) -> tuple[str, str, float | None]:
        return (str(row.get("market")), str(row.get("selection")), row.get("line"))

    # ------------------------------------------------------------------ reporting
    def _result(self) -> dict[str, Any]:
        return {
            "shadow_enabled": self.shadow_enabled(),
            "competitions": sorted(SHADOW_COMPETITIONS),
            "counters": dict(self._counters),
            "alias_candidates": list(self._alias_candidates),
            "budget": self._budget.snapshot(),
        }

    def last_result(self) -> dict[str, Any]:
        return self._result()

    def diagnostics(self) -> dict[str, Any]:
        payload = self._result()
        payload["provider"] = self._provider.provider_name() if hasattr(self._provider, "provider_name") else "unknown"
        return payload

    def to_dict(self) -> dict[str, Any]:
        return self.diagnostics()


@dataclass(frozen=True)
class ShadowRun:
    """Small convenience wrapper for callers that want a typed result."""

    shadow_enabled: bool
    counters: Mapping[str, int]
    alias_candidates: tuple[dict[str, Any], ...]
    budget: Mapping[str, int]

    @classmethod
    def from_result(cls, result: Mapping[str, Any]) -> ShadowRun:
        return cls(
            shadow_enabled=bool(result.get("shadow_enabled")),
            counters=dict(result.get("counters") or {}),
            alias_candidates=tuple(result.get("alias_candidates") or ()),
            budget=dict(result.get("budget") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["alias_candidates"] = list(self.alias_candidates)
        return payload
