"""Long-lived, isolated FRA1 shadow runtime for the dormant 5Dollar provider.

This module is the ONLY runtime entry point for shadow collection. It is
deliberately separated from the application decision flow:

* it owns exactly ONE :class:`FiveDollarFootballProvider` and ONE
  :class:`~sports_ai.market_data.five_dollar_shadow.FiveDollarShadowCollector`,
  constructed once and reused for the whole process lifetime, so the collector's
  rolling hourly request ledger never resets between cycles;
* collection requires BOTH ``SPORTS_AI_5DOLLAR_ENABLED`` and
  ``SPORTS_AI_5DOLLAR_SHADOW_ENABLED``; when either is absent/false it performs
  zero provider calls and writes nothing;
* FRA1 fixtures are discovered from the 5Dollar provider's own league-fixture
  window (the production fixture sync intentionally excludes Ligue 1). A
  deterministic, provider-independent canonical ``fixture_id`` is derived for
  each fixture -- no synthetic ids, and no other league is ever queried;
* it is NEVER wired as the dashboard decision market provider, never calls any
  market-evaluation builder, never creates paper entries, and never alters any
  selection, staking, threshold, or model state.

The runtime only appends Bet365 AH + corner observations to the existing
``market_snapshots`` table (idempotently) and emits sanitized structured logs.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Any

from .five_dollar_football import (
    FiveDollarConfig,
    FiveDollarFootballProvider,
    normalize_kickoff_utc,
)
from .five_dollar_shadow import FiveDollarShadowCollector, ShadowConfig

logger = logging.getLogger(__name__)

ENV_MAX_FIXTURES_PER_CYCLE = "SPORTS_AI_5DOLLAR_SHADOW_MAX_FIXTURES_PER_CYCLE"
ENV_INTERVAL_SECONDS = "SPORTS_AI_5DOLLAR_SHADOW_INTERVAL_SECONDS"
ENV_LOOKAHEAD_HOURS = "SPORTS_AI_5DOLLAR_SHADOW_LOOKAHEAD_HOURS"
ENV_RECENT_SKIP_SECONDS = "SPORTS_AI_5DOLLAR_SHADOW_RECENT_SKIP_SECONDS"

DEFAULT_MAX_FIXTURES_PER_CYCLE = 2
HARD_MAX_FIXTURES_PER_CYCLE = 2
DEFAULT_INTERVAL_SECONDS = 900
MIN_INTERVAL_SECONDS = 60
DEFAULT_LOOKAHEAD_HOURS = 48
DEFAULT_RECENT_SKIP_SECONDS = 3600

# FRA1 only. Never enumerate or query any other league.
FRA1 = "FRA1"

SQLITE_BUSY_TIMEOUT_MS = 30_000


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class ShadowRuntimeConfig:
    """Bounded runtime configuration. All values are hard-clamped."""

    max_fixtures_per_cycle: int = DEFAULT_MAX_FIXTURES_PER_CYCLE
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    lookahead_hours: int = DEFAULT_LOOKAHEAD_HOURS
    recent_skip_seconds: int = DEFAULT_RECENT_SKIP_SECONDS

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "max_fixtures_per_cycle",
            max(0, min(int(self.max_fixtures_per_cycle), HARD_MAX_FIXTURES_PER_CYCLE)),
        )
        object.__setattr__(self, "interval_seconds", max(MIN_INTERVAL_SECONDS, int(self.interval_seconds)))
        object.__setattr__(self, "lookahead_hours", max(1, int(self.lookahead_hours)))
        object.__setattr__(self, "recent_skip_seconds", max(0, int(self.recent_skip_seconds)))

    @classmethod
    def from_env(cls) -> ShadowRuntimeConfig:
        return cls(
            max_fixtures_per_cycle=_env_int(ENV_MAX_FIXTURES_PER_CYCLE, DEFAULT_MAX_FIXTURES_PER_CYCLE),
            interval_seconds=_env_int(ENV_INTERVAL_SECONDS, DEFAULT_INTERVAL_SECONDS),
            lookahead_hours=_env_int(ENV_LOOKAHEAD_HOURS, DEFAULT_LOOKAHEAD_HOURS),
            recent_skip_seconds=_env_int(ENV_RECENT_SKIP_SECONDS, DEFAULT_RECENT_SKIP_SECONDS),
        )


def connect_paper_db(db_path: str | Path) -> sqlite3.Connection:
    """Open the shared paper DB for the isolated shadow worker.

    ``check_same_thread=False`` is required because cycles run in a worker thread;
    access is serialized (exactly one cycle at a time) and WAL + busy_timeout keep
    concurrent dashboard writers safe.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return conn


class FiveDollarShadowRuntime:
    """One long-lived shadow runtime: one provider, one collector, reused cycles."""

    def __init__(
        self,
        *,
        provider: Any | None = None,
        config: ShadowRuntimeConfig | None = None,
        shadow_config: ShadowConfig | None = None,
        conn: Any | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._provider = provider or FiveDollarFootballProvider(FiveDollarConfig.from_env())
        self._config = config or ShadowRuntimeConfig.from_env()
        self._shadow_config = shadow_config
        self._clock = clock
        self._conn = conn
        self._owns_conn = False
        self._collector = FiveDollarShadowCollector(
            provider=self._provider,
            config=shadow_config,
            conn=conn,
            clock=clock,
        )
        self._last_collected: dict[str, float] = {}
        self._considered = 0
        self._selected: list[dict[str, Any]] = []
        self._skipped_recently = 0
        self.last_result: dict[str, Any] = {}

    # ------------------------------------------------------------------ wiring
    @classmethod
    def from_path(
        cls,
        paper_db_path: str | Path,
        *,
        provider: Any | None = None,
        config: ShadowRuntimeConfig | None = None,
        shadow_config: ShadowConfig | None = None,
        clock: Callable[[], float] = monotonic,
    ) -> FiveDollarShadowRuntime:
        runtime = cls(provider=provider, config=config, shadow_config=shadow_config, clock=clock)
        runtime.attach_connection(paper_db_path)
        return runtime

    def attach_connection(self, paper_db_path: str | Path) -> FiveDollarShadowRuntime:
        """Attach the shared paper DB and rebuild the single collector with it.

        Called exactly once at process start, and ONLY when the runtime is enabled,
        so a disabled runtime never opens/creates the database (zero writes).
        """
        if self._owns_conn and self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
        self._conn = connect_paper_db(paper_db_path)
        self._owns_conn = True
        self._collector = FiveDollarShadowCollector(
            provider=self._provider,
            config=self._shadow_config,
            conn=self._conn,
            clock=self._clock,
        )
        return self

    def close(self) -> None:
        if self._owns_conn and self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    # ------------------------------------------------------------------ enablement
    def enabled(self) -> bool:
        """True only when BOTH the provider flag and the shadow flag are set."""
        return self._collector.shadow_enabled()

    @property
    def interval_seconds(self) -> int:
        return self._config.interval_seconds

    @property
    def max_fixtures_per_cycle(self) -> int:
        return self._config.max_fixtures_per_cycle

    # ------------------------------------------------------------------ cycle
    def run_cycle(self, *, persist: bool = True) -> dict[str, Any]:
        self._considered = 0
        self._selected = []
        self._skipped_recently = 0

        result = self._collector.run_cycle(
            discover=self._discover_and_select if self.enabled() else (list),
            persist=persist,
        )

        counters = result.setdefault("counters", {})
        counters["fixtures_considered"] = self._considered
        counters["fixtures_selected"] = len(self._selected)
        counters["fixtures_skipped_recently_collected"] = self._skipped_recently
        result["state"] = "enabled" if result.get("shadow_enabled") else "disabled"
        result["runtime"] = {
            "selected": [self._public_row(row) for row in self._selected],
            "interval_seconds": self._config.interval_seconds,
            "lookahead_hours": self._config.lookahead_hours,
            "max_fixtures_per_cycle": self._config.max_fixtures_per_cycle,
            "recent_skip_seconds": self._config.recent_skip_seconds,
        }
        self.last_result = result
        self._log(result)
        return result

    # ------------------------------------------------------------------ discovery + selection
    def _discover_and_select(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        window = self._provider.list_league_fixtures(
            FRA1,
            from_utc=now - timedelta(minutes=15),
            to_utc=now + timedelta(hours=self._config.lookahead_hours),
        )
        self._considered = len(window)
        candidates = self._candidates(window)

        now_mono = self._clock()
        selected: list[dict[str, Any]] = []
        for row in candidates:
            fixture_id = row["fixture_id"]
            recent = self._last_collected.get(fixture_id)
            if recent is not None and (now_mono - recent) < self._config.recent_skip_seconds:
                self._skipped_recently += 1
                continue
            selected.append(row)
            if len(selected) >= self._config.max_fixtures_per_cycle:
                break

        for row in selected:
            self._last_collected[row["fixture_id"]] = now_mono
        self._selected = selected
        return selected

    def _candidates(self, window: Any) -> list[dict[str, Any]]:
        from ..storage.football_canonical_fixture import (
            canonical_fixture_id_from_identity,
        )

        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for fixture in window or []:
            teams = fixture.get("teams") or {}
            home = (teams.get("home") or {}).get("name")
            away = (teams.get("away") or {}).get("name")
            kickoff = normalize_kickoff_utc(fixture.get("kickoff_utc"))
            provider_fixture_id = str(fixture.get("id") or "").strip()
            if not (home and away and kickoff and provider_fixture_id):
                continue
            canonical_id = canonical_fixture_id_from_identity(
                competition_id=FRA1,
                home_team_name=home,
                away_team_name=away,
                kickoff_utc=kickoff,
            )
            if canonical_id in seen:
                continue
            seen.add(canonical_id)
            out.append(
                {
                    "competition_id": FRA1,
                    "fixture_id": canonical_id,
                    "home_team_name": home,
                    "away_team_name": away,
                    "kickoff_utc": kickoff,
                    # Status is normalized downstream; the provider vocabulary is
                    # not the dashboard vocabulary, so leave it unknown (allowed).
                    "status": "",
                }
            )
        # Deterministic: earliest kickoff first, canonical id as stable tie-break.
        out.sort(key=lambda row: (row["kickoff_utc"], row["fixture_id"]))
        return out

    # ------------------------------------------------------------------ reporting
    @staticmethod
    def _public_row(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "fixture_id": row.get("fixture_id"),
            "home_team_name": row.get("home_team_name"),
            "away_team_name": row.get("away_team_name"),
            "kickoff_utc": row.get("kickoff_utc"),
        }

    def _log(self, result: Mapping[str, Any]) -> None:
        counters = result.get("counters") or {}
        budget = result.get("budget") or {}
        logger.info(
            "5dollar shadow: state=%s fixtures_considered=%d fixtures_selected=%d "
            "matched=%d unmatched=%d ambiguous=%d skipped_recent=%d calls=%d "
            "cache_hits=%d written=%d reused=%d alias_misses=%d rate_blocked=%d "
            "cycle_calls=%d hourly_calls=%d",
            result.get("state"),
            counters.get("fixtures_considered", 0),
            counters.get("fixtures_selected", 0),
            counters.get("fixtures_matched", 0),
            counters.get("fixtures_unmatched", 0),
            counters.get("fixtures_ambiguous", 0),
            counters.get("fixtures_skipped_recently_collected", 0),
            counters.get("provider_calls", 0),
            counters.get("cache_hits", 0),
            counters.get("snapshots_written", 0),
            counters.get("snapshots_reused", 0),
            counters.get("alias_misses", 0),
            counters.get("rate_blocked", 0),
            budget.get("cycle_calls", 0),
            budget.get("hourly_calls", 0),
        )
