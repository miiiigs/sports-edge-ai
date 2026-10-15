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

    def __post_init__(self) -> None:
        # Hard caps: lower values allowed, oversized/invalid fail safe to the max.
        object.__setattr__(
            self,
            "max_provider_calls_per_cycle",
            max(0, min(int(self.max_provider_calls_per_cycle), DEFAULT_MAX_PROVIDER_CALLS_PER_CYCLE)),
        )
        object.__setattr__(
            self,
            "max_provider_calls_per_hour",
            max(0, min(int(self.max_provider_calls_per_hour), DEFAULT_MAX_PROVIDER_CALLS_PER_HOUR)),
        )

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
        "fixtures_selected": 0,
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
        "fixtures_skipped_no_fixture_id": 0,
        "fixtures_skipped_recently_collected": 0,
        "discovery_errors": 0,
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
            identical = paper.find_identical_market_snapshot(self._conn, stored) is not None
            # For an identical price state this only advances the row's
            # last_observed_at (captured_at/provider_timestamp stay authoritative);
            # the runtime's recency rotation relies on that re-observation time.
            paper.record_market_snapshot(self._conn, stored)
            if identical:
                reused += 1
            else:
                written += 1
        return written, reused

    # ------------------------------------------------------------------ budgeted provider calls
    def _accounted(self, fn, *args, **kwargs):
        """Run a provider operation and ALWAYS account its real call delta.

        The delta is taken from the provider governor's ``requests_allowed`` in a
        ``finally`` block, so a call consumed by a failure/exception can never
        escape ShadowBudget or the provider_calls counter.
        """
        before = self._provider_calls()
        try:
            return fn(*args, **kwargs)
        finally:
            delta = max(0, self._provider_calls() - before)
            if delta:
                self._budget.record(delta)
                self._counters["provider_calls"] += delta

    # ------------------------------------------------------------------ alias evidence
    def _record_alias_evidence(self, fixture_context: Mapping[str, Any]) -> None:
        self._counters["alias_misses"] += 1
        # Prefer already-fetched/cached match evidence; when a network call is
        # genuinely required it is pre-checked against the budget AND accounted
        # exactly like any other provider operation. A failure never triggers a
        # second (unbudgeted) attempt.
        if not self._budget.can_spend(1):
            self._counters["rate_blocked"] += 1
            return
        try:
            window = self._accounted(self._provider._fixtures_window, fixture_context)
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
    def begin_cycle(self) -> bool:
        """Reset per-cycle state. Returns True only when a cycle may proceed."""
        self._counters = _empty_counters()
        self._alias_candidates = []
        self._budget.start_cycle()
        return self.shadow_enabled()

    def collect(self, fixtures: Iterable[Mapping[str, Any]], *, persist: bool = True) -> dict[str, Any]:
        if not self.begin_cycle():
            return self._result()
        return self._collect_rows(fixtures, persist=persist)

    def run_cycle(
        self,
        *,
        discover: Callable[[], Iterable[Mapping[str, Any]]],
        persist: bool = True,
    ) -> dict[str, Any]:
        """One full cycle: budgeted discovery + deterministic selection + collection.

        ``discover`` runs at most once per cycle and its provider cost is charged to
        the SAME ShadowBudget, so discovery can never escape the cycle/hour caps.
        """
        if not self.begin_cycle():
            return self._result()
        if not self._budget.can_spend(1):
            self._counters["rate_blocked"] += 1
            return self._result()
        try:
            fixtures = list(self._accounted(discover) or [])
        except Exception:  # noqa: BLE001
            self._counters["discovery_errors"] += 1
            return self._result()
        return self._collect_rows(fixtures, persist=persist)

    def _collect_rows(self, fixtures: Iterable[Mapping[str, Any]], *, persist: bool = True) -> dict[str, Any]:
        max_cycle = self._config.max_provider_calls_per_cycle
        for row in fixtures:
            competition = str(row.get("competition_id") or "").strip().upper()
            if competition not in SHADOW_COMPETITIONS:
                continue  # no provider call for unsupported competitions
            if not self.eligible(row):
                continue

            # A canonical/internal fixture id is REQUIRED for pipeline rows. Never
            # synthesise one; skip/fail closed with zero provider calls.
            fixture_id = str(row.get("fixture_id") or "").strip()
            if not fixture_id:
                self._counters["fixtures_skipped_no_fixture_id"] += 1
                continue

            self._counters["fixtures_considered"] += 1

            # A fixture needs at most two provider calls (window/alias + odds).
            if not self._budget.can_spend(2):
                self._counters["rate_blocked"] += 1
                continue

            context = {
                "competition_id": competition,
                "home_team_name": row.get("home_team_name"),
                "away_team_name": row.get("away_team_name"),
                "kickoff_utc": row.get("kickoff_utc"),
            }
            cache_before = self._cache_hits()

            try:
                match = self._accounted(self._provider.match_fixture, context)
            except MarketProviderError:
                self._counters["fixtures_unmatched"] += 1
                self._record_alias_evidence(context)
                if self._budget.snapshot()["cycle_calls"] >= max_cycle:
                    break
                continue

            state = getattr(match, "state", "UNMATCHED")
            if state != "MATCHED":
                if state == "AMBIGUOUS":
                    self._counters["fixtures_ambiguous"] += 1
                else:
                    self._counters["fixtures_unmatched"] += 1
                    self._record_alias_evidence(context)
                if self._budget.snapshot()["cycle_calls"] >= max_cycle:
                    break
                continue

            self._counters["fixtures_matched"] += 1

            if not self._budget.can_spend(1):
                self._counters["rate_blocked"] += 1
                continue

            try:
                quotes = self._accounted(self._provider.get_fixture_quotes, fixture_id, fixture_context=context)
            except MarketProviderError:
                self._counters["fixtures_unmatched"] += 1
                if self._budget.snapshot()["cycle_calls"] >= max_cycle:
                    break
                continue

            self._counters["cache_hits"] += max(0, self._cache_hits() - cache_before)
            self._counters["ah_quotes"] += sum(1 for q in quotes if q.market == MARKET_ASIAN_HANDICAP)
            self._counters["corner_quotes"] += sum(1 for q in quotes if q.market == MARKET_CORNER_TOTAL)

            if persist and self._conn is not None and quotes:
                written, reused = self._persist(quotes)
                self._counters["snapshots_written"] += written
                self._counters["snapshots_reused"] += reused

            if self._budget.snapshot()["cycle_calls"] >= max_cycle:
                break

        return self._result()

    # ------------------------------------------------------------------ comparison (read-only)
    def compare_with_api_football(self, fixture_id: str, *, include_inplay: bool = False) -> list[dict[str, Any]]:
        """Pre-match-only, observation-only comparison.

        By default only pre-match rows are compared (``is_pre_match == 1``), so an
        in-play 5Dollar quote can never be compared against a pre-match
        API-Football quote. Phase is preserved for 5Dollar rows, so opening and
        closing remain separate observations. No averaging, no winner, no
        replacement. Strictly read-only (no schema creation/migration/backfill).
        """
        if self._conn is None:
            return []
        from ..storage import football_paper_trading as paper

        rows = paper.fetch_market_snapshots(self._conn, fixture_id=fixture_id, initialize_schema=False)

        def _phase(row: Mapping[str, Any]) -> str | None:
            parts = str(row.get("provider_market_id") or "").split(":")
            return parts[3] if len(parts) >= 6 else None

        five = [
            r
            for r in rows
            if r.get("provider") == _FIVE_DOLLAR_PROVIDER and (include_inplay or r.get("is_pre_match") == 1)
        ]
        api_by_key = {
            self._key(r): r
            for r in rows
            if r.get("provider") == _API_FOOTBALL_PROVIDER and (include_inplay or r.get("is_pre_match") == 1)
        }
        out: list[dict[str, Any]] = []
        key_fn = lambda r: (
            str(r.get("market")),
            str(r.get("selection")),
            r.get("line") if r.get("line") is not None else 0.0,
            str(_phase(r)),
        )
        for f in sorted(five, key=key_fn):
            a = api_by_key.get(self._key(f))
            out.append(
                {
                    "market": f.get("market"),
                    "selection": f.get("selection"),
                    "line": f.get("line"),
                    "five_dollar_phase": _phase(f),
                    "five_dollar_price": f.get("odds"),
                    "five_dollar_captured_at": f.get("captured_at"),
                    "api_football_price": a.get("odds") if a else None,
                    "api_football_captured_at": a.get("captured_at") if a else None,
                    "difference": (f.get("odds") - a.get("odds")) if a else None,
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
