"""Canonical current-result (sporting truth) contracts for Football.

This module owns the *sporting truth* contract only: provider status
normalization, conservative final-result merging, and deterministic, bounded
selection of current/recent fixtures that still need a verified result.

It never settles bets, never touches the paper ledger, never creates Value Bets,
never refreshes market odds and never runs a historical replay. Model Pick
settlement is a separate, later consumer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# --------------------------------------------------------------------------- states
STATE_PRE_MATCH = "PRE_MATCH"
STATE_LIVE = "LIVE"
STATE_HALFTIME = "HALFTIME"
STATE_FINISHED = "FINISHED"
STATE_POSTPONED = "POSTPONED"
STATE_CANCELLED = "CANCELLED"
STATE_ABANDONED = "ABANDONED"
STATE_UNKNOWN = "UNKNOWN"

CANONICAL_RESULT_STATES = (
    STATE_PRE_MATCH,
    STATE_LIVE,
    STATE_HALFTIME,
    STATE_FINISHED,
    STATE_POSTPONED,
    STATE_CANCELLED,
    STATE_ABANDONED,
    STATE_UNKNOWN,
)

# Deterministic API-Football `fixture.status.short` -> canonical result state.
# Unknown shorts fail closed to UNKNOWN (never FINISHED).
API_FOOTBALL_RESULT_STATUS_MAP: Mapping[str, str] = {
    "NS": STATE_PRE_MATCH,
    "TBD": STATE_PRE_MATCH,
    "PST": STATE_POSTPONED,
    "CANC": STATE_CANCELLED,
    "ABD": STATE_ABANDONED,
    "AWD": STATE_CANCELLED,
    "WO": STATE_CANCELLED,
    "HT": STATE_HALFTIME,
    "FT": STATE_FINISHED,
    "AET": STATE_FINISHED,
    "PEN": STATE_FINISHED,
    "1H": STATE_LIVE,
    "2H": STATE_LIVE,
    "ET": STATE_LIVE,
    "BT": STATE_LIVE,
    "INT": STATE_LIVE,
    "SUSP": STATE_LIVE,
}

# Already-normalized dashboard fixture statuses -> canonical result state.
DASHBOARD_RESULT_STATUS_MAP: Mapping[str, str] = {
    "SCHEDULED": STATE_PRE_MATCH,
    "PRE_MATCH": STATE_PRE_MATCH,
    "LIVE": STATE_LIVE,
    "HALFTIME": STATE_HALFTIME,
    "FINISHED": STATE_FINISHED,
    "FT": STATE_FINISHED,
    "AET": STATE_FINISHED,
    "PEN": STATE_FINISHED,
    "POSTPONED": STATE_POSTPONED,
    "CANCELLED": STATE_CANCELLED,
    "ABANDONED": STATE_ABANDONED,
}

# States after which status polling should stop (verified terminal truth).
TERMINAL_STOP_STATES = frozenset({STATE_FINISHED, STATE_CANCELLED, STATE_ABANDONED})

# How long after kickoff a fixture remains eligible for current-result sync.
RECENT_RESULT_LOOKBACK_HOURS = 72
# POSTPONED fixtures stay eligible for a longer, separately-bounded window.
POSTPONED_LOOKBACK_HOURS = 24 * 14

RESULT_SYNC_SCHEDULER_ENV_VAR = "SPORTS_AI_ENABLE_RESULT_SYNC_SCHEDULER"
RESULT_SYNC_INTERVAL_ENV_VAR = "SPORTS_AI_RESULT_SYNC_INTERVAL_SECONDS"
RESULT_SYNC_MAX_FIXTURES_ENV_VAR = "SPORTS_AI_RESULT_SYNC_MAX_FIXTURES_PER_CYCLE"

DEFAULT_RESULT_SYNC_INTERVAL_SECONDS = 600
MIN_RESULT_SYNC_INTERVAL_SECONDS = 300
HARD_MAX_RESULT_SYNC_INTERVAL_SECONDS = 86_400
DEFAULT_RESULT_SYNC_MAX_FIXTURES = 2
HARD_MAX_RESULT_SYNC_MAX_FIXTURES = 10


def resolve_result_sync_interval_seconds(configured: int | None) -> int:
    if configured is None:
        return DEFAULT_RESULT_SYNC_INTERVAL_SECONDS
    try:
        value = int(configured)
    except (TypeError, ValueError):
        return DEFAULT_RESULT_SYNC_INTERVAL_SECONDS
    if value <= 0:
        return DEFAULT_RESULT_SYNC_INTERVAL_SECONDS
    if value < MIN_RESULT_SYNC_INTERVAL_SECONDS:
        return MIN_RESULT_SYNC_INTERVAL_SECONDS
    return min(value, HARD_MAX_RESULT_SYNC_INTERVAL_SECONDS)


def resolve_result_sync_max_fixtures(configured: int | None) -> int:
    if configured is None:
        return DEFAULT_RESULT_SYNC_MAX_FIXTURES
    try:
        value = int(configured)
    except (TypeError, ValueError):
        return DEFAULT_RESULT_SYNC_MAX_FIXTURES
    if value <= 0:
        return DEFAULT_RESULT_SYNC_MAX_FIXTURES
    return max(1, min(value, HARD_MAX_RESULT_SYNC_MAX_FIXTURES))


def normalize_api_football_status(short: Any) -> str:
    return API_FOOTBALL_RESULT_STATUS_MAP.get(str(short or "").strip().upper(), STATE_UNKNOWN)


def normalize_dashboard_status(status: Any) -> str:
    return DASHBOARD_RESULT_STATUS_MAP.get(str(status or "").strip().upper(), STATE_UNKNOWN)


def canonical_status_for_fixture(*, normalized_status: Any, provider_payload: Any) -> str:
    """Canonical status preferring the raw API-Football short when present."""
    raw_short = None
    if isinstance(provider_payload, Mapping):
        fixture_payload = provider_payload.get("fixture")
        if isinstance(fixture_payload, Mapping):
            status_payload = fixture_payload.get("status")
            if isinstance(status_payload, Mapping):
                raw_short = status_payload.get("short")
    if raw_short not in {None, ""}:
        return normalize_api_football_status(raw_short)
    return normalize_dashboard_status(normalized_status)


def is_finished_verified(*, status: str, home_score: Any, away_score: Any) -> bool:
    """FINISHED truth requires authoritative final status AND both scores."""
    return (
        status == STATE_FINISHED
        and home_score is not None
        and away_score is not None
    )


def _parse_kickoff(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def within_result_lookback(*, kickoff_utc: Any, status: str, now_utc: datetime) -> bool:
    kickoff = _parse_kickoff(kickoff_utc)
    if kickoff is None or kickoff > now_utc:
        return False
    hours = POSTPONED_LOOKBACK_HOURS if status == STATE_POSTPONED else RECENT_RESULT_LOOKBACK_HOURS
    return (now_utc - kickoff).total_seconds() <= hours * 3600


def needs_result_sync(*, status: str, home_score: Any, away_score: Any) -> bool:
    """True unless already terminal (finished-verified, cancelled, abandoned)."""
    if status in {STATE_CANCELLED, STATE_ABANDONED}:
        return False
    return not is_finished_verified(status=status, home_score=home_score, away_score=away_score)


# --------------------------------------------------------------------------- selection
@dataclass(frozen=True)
class ResultFixtureCandidate:
    fixture_id: str
    provider_fixture_id: str
    kickoff_utc: str
    status: str
    home_score: int | None
    away_score: int | None
    has_live_issued_pick: bool
    last_result_sync_attempt_at: str | None = None


def _priority_rank(status: str) -> int:
    if status in {STATE_LIVE, STATE_HALFTIME}:
        return 0
    if status == STATE_PRE_MATCH:
        return 1
    return 2


def _sort_key(candidate: ResultFixtureCandidate) -> tuple[int, str, float, str]:
    kickoff = _parse_kickoff(candidate.kickoff_utc)
    recency = -kickoff.timestamp() if kickoff is not None else 0.0  # nearest recent first
    return (
        _priority_rank(candidate.status),
        candidate.last_result_sync_attempt_at or "",
        recency,
        candidate.fixture_id,
    )


def select_result_sync_fixtures(
    candidates: Sequence[ResultFixtureCandidate],
    *,
    max_fixtures: int,
) -> list[ResultFixtureCandidate]:
    eligible = [
        candidate
        for candidate in candidates
        if candidate.has_live_issued_pick
        and str(candidate.provider_fixture_id or "").strip()
        and needs_result_sync(
            status=candidate.status,
            home_score=candidate.home_score,
            away_score=candidate.away_score,
        )
    ]
    return sorted(eligible, key=_sort_key)[: max(0, int(max_fixtures))]


# --------------------------------------------------------------------------- merging
@dataclass(frozen=True)
class MergeDecision:
    action: str  # NOOP | APPLY | CONFLICT | REJECT
    reason: str


def merge_fixture_truth(
    *,
    existing_status: str,
    existing_home_score: Any,
    existing_away_score: Any,
    incoming_status: str,
    incoming_home_score: Any,
    incoming_away_score: Any,
) -> MergeDecision:
    """Conservative final-result merge.

    Same final (status+score) -> NOOP; proven final -> conflicting score CONFLICT;
    proven final -> non-final REJECT; terminal (cancelled/abandoned) not overwritten;
    incoming FINISHED without both scores -> REJECT (never infer completion from a
    score or a status alone).
    """
    if existing_status in {STATE_CANCELLED, STATE_ABANDONED}:
        return MergeDecision("NOOP" if incoming_status == existing_status else "REJECT", "existing terminal truth")
    if existing_status == STATE_FINISHED and existing_home_score is not None and existing_away_score is not None:
        if incoming_status != STATE_FINISHED:
            return MergeDecision("REJECT", "existing finished truth")
        if incoming_home_score is None or incoming_away_score is None:
            return MergeDecision("REJECT", "existing finished truth; incoming score missing")
        if (int(existing_home_score), int(existing_away_score)) == (int(incoming_home_score), int(incoming_away_score)):
            return MergeDecision("NOOP", "identical final result")
        return MergeDecision("CONFLICT", "conflicting final score")
    if incoming_status == STATE_UNKNOWN:
        return MergeDecision("REJECT", "unknown provider status")
    if incoming_status == STATE_FINISHED and (incoming_home_score is None or incoming_away_score is None):
        return MergeDecision("REJECT", "final status without complete score")
    return MergeDecision("APPLY", "non-terminal or verified progression")


def merge_corner_truth(
    *,
    existing_home_corners: Any,
    existing_away_corners: Any,
    incoming_home_corners: Any,
    incoming_away_corners: Any,
) -> MergeDecision:
    """Conservative corner-truth merge (never inferred from the football score)."""
    if incoming_home_corners is None or incoming_away_corners is None:
        return MergeDecision("NOOP", "incomplete corner statistics")
    if existing_home_corners is not None and existing_away_corners is not None:
        if (int(existing_home_corners), int(existing_away_corners)) == (
            int(incoming_home_corners),
            int(incoming_away_corners),
        ):
            return MergeDecision("NOOP", "identical corner truth")
        return MergeDecision("CONFLICT", "conflicting corner truth")
    return MergeDecision("APPLY", "new corner truth")


def latest_result_attempts(
    attempts: Iterable[Mapping[str, object]],
) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in attempts:
        fixture_id = str(row.get("fixture_id") or "")
        value = str(row.get("last_result_sync_attempt_at") or "")
        if not fixture_id or not value:
            continue
        current = out.get(fixture_id)
        if current is None or value > current:
            out[fixture_id] = value
    return out
