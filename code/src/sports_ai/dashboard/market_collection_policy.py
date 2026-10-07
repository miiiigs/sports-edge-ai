"""Deterministic, bounded fixture selection for collection-only market sync.

This module is the safety core of market *collection-only* mode. It decides which
fixtures a cycle is allowed to request odds for, so enabling collection can never
fan out across the whole slate by accident.

Selection is strictly deterministic and outcome-independent:

1. only future fixtures with a deterministic provider fixture id;
2. only fixtures with a LIVE_ISSUED Asian Handicap and/or Corner Total Model Pick;
3. prefer fixtures that have NEVER been attempted;
4. then fixtures with the OLDEST observation/attempt recency;
5. then the nearest kickoff;
6. fixture id as the stable final tie-break.

Rotation uses **observation/attempt recency** (``last_observed_at``), not the
provider source timestamp. This matters when API-Football repeatedly returns the
same stale provider timestamp: the collected fixture still advances its
``last_observed_at`` (even on a cache hit) and therefore moves to the back of the
queue, so repeated bounded cycles eventually cover every eligible fixture without
starvation. Provider ``source_timestamp`` remains authoritative for freshness
only and is never overwritten to achieve rotation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

MARKET_COLLECTION_ONLY_ENV_VAR = "SPORTS_AI_MARKET_COLLECTION_ONLY"
MARKET_COLLECTOR_MAX_FIXTURES_ENV_VAR = "SPORTS_AI_MARKET_COLLECTOR_MAX_FIXTURES_PER_CYCLE"

# Dedicated recurring market-collection scheduler (independent of the global
# automated fixture/prediction scheduler).
MARKET_COLLECTION_SCHEDULER_ENV_VAR = "SPORTS_AI_ENABLE_MARKET_COLLECTION_SCHEDULER"
MARKET_COLLECTION_INTERVAL_ENV_VAR = "SPORTS_AI_MARKET_COLLECTION_INTERVAL_SECONDS"
DEFAULT_MARKET_COLLECTION_INTERVAL_SECONDS = 600
MIN_MARKET_COLLECTION_INTERVAL_SECONDS = 300
HARD_MAX_MARKET_COLLECTION_INTERVAL_SECONDS = 86_400

# Hard safety ceiling regardless of configuration.
HARD_MAX_FIXTURES_PER_CYCLE = 50

# Fail-closed default when collection-only mode has no explicit bound.
DEFAULT_COLLECTION_ONLY_MAX_FIXTURES = 2

COLLECTION_MARKETS = frozenset({"asian_handicap", "corner_total"})


def resolve_collection_only_max_fixtures(configured: int | None) -> int:
    """Bound the per-cycle fixture count for collection-only mode.

    A missing, zero or negative bound fails closed to
    :data:`DEFAULT_COLLECTION_ONLY_MAX_FIXTURES`; any value is clamped to
    ``[1, HARD_MAX_FIXTURES_PER_CYCLE]``.
    """
    if configured is None:
        return DEFAULT_COLLECTION_ONLY_MAX_FIXTURES
    try:
        value = int(configured)
    except (TypeError, ValueError):
        return DEFAULT_COLLECTION_ONLY_MAX_FIXTURES
    if value <= 0:
        return DEFAULT_COLLECTION_ONLY_MAX_FIXTURES
    return max(1, min(value, HARD_MAX_FIXTURES_PER_CYCLE))


def resolve_market_collection_interval_seconds(configured: int | None) -> int:
    """Bound the dedicated market-collection interval (seconds).

    Missing/invalid/too-small values fail closed to the 600-second default; any
    value is clamped to ``[MIN_MARKET_COLLECTION_INTERVAL_SECONDS,
    HARD_MAX_MARKET_COLLECTION_INTERVAL_SECONDS]`` so accidental high-frequency
    polling is impossible.
    """
    if configured is None:
        return DEFAULT_MARKET_COLLECTION_INTERVAL_SECONDS
    try:
        value = int(configured)
    except (TypeError, ValueError):
        return DEFAULT_MARKET_COLLECTION_INTERVAL_SECONDS
    if value < MIN_MARKET_COLLECTION_INTERVAL_SECONDS:
        return DEFAULT_MARKET_COLLECTION_INTERVAL_SECONDS if value <= 0 else MIN_MARKET_COLLECTION_INTERVAL_SECONDS
    return min(value, HARD_MAX_MARKET_COLLECTION_INTERVAL_SECONDS)


@dataclass(frozen=True)
class CollectionCandidate:
    fixture_id: str
    provider_fixture_id: str
    kickoff_utc: str
    has_live_issued_pick: bool
    last_observed_at: str | None = None

    @property
    def has_observation(self) -> bool:
        return bool(self.last_observed_at)


def _sort_key(candidate: CollectionCandidate) -> tuple[int, str, str, str]:
    # 0 = never attempted (collect first); 1 = has an observation.
    return (
        0 if not candidate.has_observation else 1,
        candidate.last_observed_at or "",
        candidate.kickoff_utc,
        candidate.fixture_id,
    )


def select_collection_fixtures(
    candidates: Sequence[CollectionCandidate],
    *,
    max_fixtures: int,
) -> list[CollectionCandidate]:
    """Return the deterministic, bounded fixture selection for one cycle."""
    eligible = [
        candidate
        for candidate in candidates
        if candidate.has_live_issued_pick and str(candidate.provider_fixture_id or "").strip()
    ]
    ordered = sorted(eligible, key=_sort_key)
    return ordered[: max(0, int(max_fixtures))]


def latest_observation_by_fixture(
    rows: Iterable[Mapping[str, object]],
    *,
    markets: frozenset[str] = COLLECTION_MARKETS,
) -> dict[str, str | None]:
    """Latest observation/attempt recency per fixture from persisted snapshot rows.

    Uses ``last_observed_at`` (which advances on re-observation, including a
    provider cache hit) and falls back to ``captured_at`` for rows written before
    the chronology columns existed. ``provider_timestamp`` is deliberately NOT
    used here; it remains authoritative for freshness only.
    """
    latest: dict[str, str] = {}
    for row in rows:
        market = str(row.get("market") or "").lower()
        if market not in markets:
            continue
        fixture_id = str(row.get("fixture_id") or "")
        if not fixture_id:
            continue
        value = row.get("last_observed_at") or row.get("captured_at")
        if not value:
            continue
        text = str(value)
        current = latest.get(fixture_id)
        if current is None or text > current:
            latest[fixture_id] = text
    return latest
