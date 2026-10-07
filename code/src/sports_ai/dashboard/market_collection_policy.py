"""Deterministic, bounded fixture selection for collection-only market sync.

This module is the safety core of market *collection-only* mode. It decides which
fixtures a cycle is allowed to request odds for, so enabling collection can never
fan out across the whole slate by accident.

Selection is strictly deterministic and outcome-independent:

1. only future fixtures with a deterministic provider fixture id;
2. only fixtures with a LIVE_ISSUED Asian Handicap and/or Corner Total Model Pick;
3. prefer fixtures with NO current source evidence;
4. then fixtures with the OLDEST source evidence;
5. then the nearest kickoff;
6. fixture id as the stable final tie-break.

Because a collected fixture receives the newest source evidence, it naturally
moves to the back of the queue on the next cycle, so repeated bounded cycles
eventually cover every eligible fixture (no starvation) without a random or
price-based priority.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

MARKET_COLLECTION_ONLY_ENV_VAR = "SPORTS_AI_MARKET_COLLECTION_ONLY"
MARKET_COLLECTOR_MAX_FIXTURES_ENV_VAR = "SPORTS_AI_MARKET_COLLECTOR_MAX_FIXTURES_PER_CYCLE"

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


@dataclass(frozen=True)
class CollectionCandidate:
    fixture_id: str
    provider_fixture_id: str
    kickoff_utc: str
    has_live_issued_pick: bool
    latest_source_evidence_at: str | None = None

    @property
    def has_source_evidence(self) -> bool:
        return bool(self.latest_source_evidence_at)


def _sort_key(candidate: CollectionCandidate) -> tuple[int, str, str, str]:
    # 0 = no current source evidence (collect first); 1 = has evidence.
    return (
        0 if not candidate.has_source_evidence else 1,
        candidate.latest_source_evidence_at or "",
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


def latest_evidence_by_fixture(
    rows: Iterable[Mapping[str, object]],
    *,
    markets: frozenset[str] = COLLECTION_MARKETS,
) -> dict[str, str | None]:
    """Latest source timestamp per fixture from persisted market snapshot rows.

    ``provider_timestamp`` is authoritative and never replaced by ``captured_at``
    when it is present; ``captured_at`` is only a fallback for rows that lack a
    provider timestamp.
    """
    latest: dict[str, str] = {}
    for row in rows:
        market = str(row.get("market") or "").lower()
        if market not in markets:
            continue
        fixture_id = str(row.get("fixture_id") or "")
        if not fixture_id:
            continue
        source = row.get("provider_timestamp")
        value = str(source) if source else str(row.get("captured_at") or "")
        if not value:
            continue
        current = latest.get(fixture_id)
        if current is None or value > current:
            latest[fixture_id] = value
    return latest
