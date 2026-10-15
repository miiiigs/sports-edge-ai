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

Optional closing-window tier (OFF by default): when
``SPORTS_AI_MARKET_COLLECTION_CLOSING_WINDOW_MINUTES`` is a positive value, a
fixture kicking off within that window whose latest observation/attempt is at
least :data:`CLOSING_WINDOW_MIN_REFRESH_MINUTES` old is ordered BEFORE every
other fixture (nearest kickoff first). It only reorders the same bounded
selection; it never raises the per-cycle cap and never touches freshness or
value eligibility.

Attempt recency: an attempt that yields no snapshot rows (provider error,
identity unresolved, market not offered) writes nothing to ``market_snapshots``.
The caller therefore persists per-fixture attempt timestamps and merges them via
:func:`merge_attempt_recency`, so a fixture that never returns odds cannot stay
"never attempted" forever and permanently occupy a bounded slot.

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
from datetime import datetime, timedelta, timezone

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

MARKET_COLLECTION_CLOSING_WINDOW_ENV_VAR = "SPORTS_AI_MARKET_COLLECTION_CLOSING_WINDOW_MINUTES"
HARD_MAX_CLOSING_WINDOW_MINUTES = 360
# A fixture already observed within this many minutes is not re-prioritized by
# the closing-window tier (it falls back to normal rotation).
CLOSING_WINDOW_MIN_REFRESH_MINUTES = 15
# Bound on persisted attempt-recency entries.
MAX_TRACKED_COLLECTION_ATTEMPTS = 500


def resolve_closing_window_minutes(configured: int | None) -> int:
    """Closing-window tier width in minutes; 0 (disabled) unless explicitly positive."""
    if configured is None:
        return 0
    try:
        value = int(configured)
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    return min(value, HARD_MAX_CLOSING_WINDOW_MINUTES)


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


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _in_closing_window(
    candidate: CollectionCandidate,
    *,
    now_utc: datetime | None,
    closing_window_minutes: int,
) -> bool:
    if now_utc is None or closing_window_minutes <= 0:
        return False
    kickoff = _parse_utc(candidate.kickoff_utc)
    if kickoff is None or kickoff <= now_utc:
        return False
    if kickoff - now_utc > timedelta(minutes=closing_window_minutes):
        return False
    observed = _parse_utc(candidate.last_observed_at)
    if observed is None:
        return True
    return now_utc - observed >= timedelta(minutes=CLOSING_WINDOW_MIN_REFRESH_MINUTES)


def _sort_key(
    candidate: CollectionCandidate,
    *,
    now_utc: datetime | None = None,
    closing_window_minutes: int = 0,
) -> tuple[int, int, str, str, str]:
    if _in_closing_window(candidate, now_utc=now_utc, closing_window_minutes=closing_window_minutes):
        # Closing tier: nearest kickoff first, regardless of observation recency.
        return (0, 0, candidate.kickoff_utc, "", candidate.fixture_id)
    # 0 = never attempted (collect first); 1 = has an observation.
    return (
        1,
        0 if not candidate.has_observation else 1,
        candidate.last_observed_at or "",
        candidate.kickoff_utc,
        candidate.fixture_id,
    )


def select_collection_fixtures(
    candidates: Sequence[CollectionCandidate],
    *,
    max_fixtures: int,
    now_utc: datetime | None = None,
    closing_window_minutes: int = 0,
) -> list[CollectionCandidate]:
    """Return the deterministic, bounded fixture selection for one cycle."""
    eligible = [
        candidate
        for candidate in candidates
        if candidate.has_live_issued_pick and str(candidate.provider_fixture_id or "").strip()
    ]
    window = resolve_closing_window_minutes(closing_window_minutes)
    ordered = sorted(
        eligible,
        key=lambda candidate: _sort_key(candidate, now_utc=now_utc, closing_window_minutes=window),
    )
    return ordered[: max(0, int(max_fixtures))]


def merge_attempt_recency(
    observation: Mapping[str, str | None],
    attempts: Mapping[str, object] | None,
) -> dict[str, str | None]:
    """Latest of persisted snapshot observation and recorded collection attempt.

    Timestamps are ISO-8601 UTC strings from the same clock, so string order is
    chronological. Invalid attempt values are ignored.
    """
    merged: dict[str, str | None] = dict(observation)
    for fixture_id, value in (attempts or {}).items():
        if not isinstance(value, str) or not value.strip() or _parse_utc(value) is None:
            continue
        key = str(fixture_id)
        current = merged.get(key)
        if current is None or value > current:
            merged[key] = value
    return merged


def prune_attempt_recency(
    attempts: Mapping[str, object] | None,
    *,
    keep_fixture_ids: Iterable[str],
) -> dict[str, str]:
    """Keep only attempts for still-relevant fixtures, newest first, bounded."""
    keep = {str(fixture_id) for fixture_id in keep_fixture_ids}
    kept = [
        (str(fixture_id), value)
        for fixture_id, value in (attempts or {}).items()
        if str(fixture_id) in keep and isinstance(value, str) and value.strip()
    ]
    kept.sort(key=lambda item: item[1], reverse=True)
    return dict(kept[:MAX_TRACKED_COLLECTION_ATTEMPTS])


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
