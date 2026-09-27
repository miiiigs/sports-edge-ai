"""Production-safe API-Football historical backfill planning and control.

This module is deliberately pure: no network I/O and no storage I/O. It turns a
bounded backfill request (competition + season + hard request cap) into:

* a validated target spec pinned to one provider league;
* a deterministic request estimate;
* a budget/eligibility decision that runs BEFORE any provider call;
* an explicit failure taxonomy (retryable vs terminal);
* a reproducible audit manifest.

Fail-closed rules:

* a fixture is GAP-ready only when shots, shots-on-target and corners exist for
  both teams (missing is never imputed, and a genuine provider zero is NOT
  missing);
* a backfill never exceeds its explicit hard request cap;
* a backfill never expands beyond the single pinned competition + season.

Historical traffic is always LOW priority; current/live operational requests
always outrank it. The eligibility decision is expressed in terms of the shared
provider budget's quota state so a backfill can be refused before any request is
made.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from ..locking import canonical_hash
from .api_football_history import (
    REQUIRED_GAP_FIELDS,
    estimate_api_football_backfill_requests,
    event_is_gap_ready,
)

BACKFILL_PROVIDER = "api-football"
SOURCE_VERSION = "api_football_history_backfill_v1"

# GAP data required for a fixture to count as model-ready. ``result`` covers
# goals/result; shots, shots-on-target and corners are the genuine statistics.
REQUIRED_STATISTICS: tuple[str, ...] = ("result", "shots", "shots_on_target", "corners")

# Budget states (must match sports_ai.dashboard.provider_budget.QUOTA_*). LOW
# priority traffic is only permitted while the quota state is HEALTHY.
BUDGET_HEALTHY = "HEALTHY"
BUDGET_EXHAUSTED = "EXHAUSTED"

# ---------------------------------------------------------------------------
# Failure taxonomy
# ---------------------------------------------------------------------------
FAILURE_QUOTA_BLOCK = "QUOTA_BLOCK"
FAILURE_TRANSIENT = "TRANSIENT"
FAILURE_NOT_FOUND = "NOT_FOUND"
FAILURE_MALFORMED = "MALFORMED_PAYLOAD"
FAILURE_MISSING_STATS = "MISSING_REQUIRED_STATISTICS"
FAILURE_NORMALIZATION = "NORMALIZATION_FAILURE"
FAILURE_PERSISTENCE = "PERSISTENCE_FAILURE"
FAILURE_TERMINAL_GAP = "TERMINAL_SOURCE_GAP"

ALL_FAILURE_KINDS: frozenset[str] = frozenset(
    {
        FAILURE_QUOTA_BLOCK,
        FAILURE_TRANSIENT,
        FAILURE_NOT_FOUND,
        FAILURE_MALFORMED,
        FAILURE_MISSING_STATS,
        FAILURE_NORMALIZATION,
        FAILURE_PERSISTENCE,
        FAILURE_TERMINAL_GAP,
    }
)

# Failures that can be retried on a later resume without inventing data.
RETRYABLE_FAILURE_KINDS: frozenset[str] = frozenset(
    {FAILURE_QUOTA_BLOCK, FAILURE_TRANSIENT, FAILURE_PERSISTENCE}
)

# Failures that must never be endlessly retried: the source genuinely lacks the
# required inputs, or the payload/normalization can never succeed.
TERMINAL_FAILURE_KINDS: frozenset[str] = ALL_FAILURE_KINDS - RETRYABLE_FAILURE_KINDS


def is_retryable_failure(kind: str) -> bool:
    return kind in RETRYABLE_FAILURE_KINDS


def is_terminal_failure(kind: str) -> bool:
    return kind in TERMINAL_FAILURE_KINDS


class BackfillProviderError(RuntimeError):
    """A provider/source failure with an explicit failure kind.

    The orchestrator translates a raised :class:`BackfillProviderError` into a
    classified per-fixture failure without guessing.
    """

    def __init__(self, failure_kind: str, message: str) -> None:
        super().__init__(message)
        if failure_kind not in ALL_FAILURE_KINDS:
            raise ValueError(f"unknown backfill failure kind {failure_kind!r}")
        self.failure_kind = failure_kind


# ---------------------------------------------------------------------------
# Fixture / job states
# ---------------------------------------------------------------------------
FIXTURE_PENDING = "PENDING"
FIXTURE_COMPLETE = "COMPLETE"
FIXTURE_RETRYABLE = "RETRYABLE_FAILED"
FIXTURE_TERMINAL = "TERMINAL_FAILED"

JOB_PENDING = "PENDING"
JOB_IN_PROGRESS = "IN_PROGRESS"
JOB_COMPLETE = "COMPLETE"
JOB_PARTIAL = "PARTIAL"
JOB_BLOCKED = "BLOCKED"


# ---------------------------------------------------------------------------
# Spec and target
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BackfillSpec:
    """An explicit, bounded backfill invocation.

    ``max_request_cap`` is a hard ceiling on provider requests for one run; it is
    not optional and there is no implicit default. ``max_retries_per_fixture``
    bounds how many times a single retryable failure is re-attempted across
    resumes before the fixture is treated as a terminal source gap.
    """

    competition_id: str
    season: int
    max_request_cap: int
    provider: str = BACKFILL_PROVIDER
    max_retries_per_fixture: int = 3

    def __post_init__(self) -> None:
        object.__setattr__(self, "competition_id", str(self.competition_id or "").strip().upper())
        object.__setattr__(self, "season", int(self.season))
        object.__setattr__(self, "max_request_cap", int(self.max_request_cap))
        object.__setattr__(self, "max_retries_per_fixture", int(self.max_retries_per_fixture))

    def validate(self) -> str | None:
        if not self.competition_id:
            return "COMPETITION_REQUIRED"
        if not (2000 <= self.season <= 2100):
            return "SEASON_OUT_OF_RANGE"
        if self.max_request_cap < 1:
            return "HARD_CAP_INVALID"
        if self.max_retries_per_fixture < 0:
            return "MAX_RETRIES_INVALID"
        return None


@dataclass(frozen=True)
class BackfillTarget:
    """A pinned competition + season + stable provider league identity."""

    competition_id: str
    competition_name: str
    division_code: str
    provider_league_id: int
    season: int
    provider: str = BACKFILL_PROVIDER
    source_version: str = SOURCE_VERSION

    @property
    def season_code(self) -> str:
        year = int(self.season)
        return f"{year % 100:02d}{(year + 1) % 100:02d}"


@dataclass(frozen=True)
class BackfillFixtureTarget:
    """One canonical target fixture in deterministic order."""

    canonical_fixture_id: str
    provider_fixture_id: str
    match_date: str | None
    home_team: str
    away_team: str


def backfill_job_id(*, provider: str, competition_id: str, season: int, provider_league_id: int, source_version: str) -> str:
    """Deterministic job identity (stable across resumes, excludes the request cap)."""
    return canonical_hash(
        {
            "provider": provider,
            "competition_id": str(competition_id).upper(),
            "season": int(season),
            "provider_league_id": int(provider_league_id),
            "source_version": source_version,
        }
    )[:24]


# ---------------------------------------------------------------------------
# Eligibility / budget gate
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class BackfillEligibility:
    permitted: bool
    reason: str | None
    projected_requests: int
    max_request_cap: int
    budget_state: str
    budget_remaining: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_backfill_eligibility(
    *,
    projected_requests: int,
    max_request_cap: int,
    budget_state: str,
    budget_remaining: int | None,
    paid_provider_calls_allowed: bool = True,
) -> BackfillEligibility:
    """Decide, before any provider call, whether a backfill may run.

    LOW-priority historical traffic is only permitted while the shared quota is
    HEALTHY, the paid-provider gate is open, and the remaining budget (when
    known) covers the projected cost.

    The ``max_request_cap`` is a per-invocation execution ceiling, not a
    permit/deny gate: when the projected cost exceeds the cap the run is
    permitted to proceed and will process a bounded prefix (leaving the rest
    resumable). An invalid (zero/negative) cap is rejected outright.
    """
    projected = max(0, int(projected_requests))
    cap = max(0, int(max_request_cap))
    if cap < 1:
        return BackfillEligibility(False, "HARD_CAP_INVALID", projected, cap, budget_state, budget_remaining)
    if not paid_provider_calls_allowed:
        return BackfillEligibility(False, "PAID_PROVIDER_CALLS_DISABLED", projected, cap, budget_state, budget_remaining)
    if budget_state == BUDGET_EXHAUSTED:
        return BackfillEligibility(False, "BUDGET_EXHAUSTED", projected, cap, budget_state, budget_remaining)
    if budget_state != BUDGET_HEALTHY:
        return BackfillEligibility(False, f"LOW_PRIORITY_QUOTA_{budget_state}", projected, cap, budget_state, budget_remaining)
    if budget_remaining is not None and int(budget_remaining) < projected:
        return BackfillEligibility(False, "BUDGET_INSUFFICIENT", projected, cap, budget_state, budget_remaining)
    return BackfillEligibility(True, None, projected, cap, budget_state, budget_remaining)


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------
def classify_exception(exc: BaseException) -> str:
    """Map an arbitrary provider exception to a failure kind.

    A :class:`BackfillProviderError` carries its kind explicitly. Otherwise a
    transport-ish exception is TRANSIENT and anything else is MALFORMED.
    """
    kind = getattr(exc, "failure_kind", None)
    if kind in ALL_FAILURE_KINDS:
        return kind
    name = type(exc).__name__.lower()
    if any(token in name for token in ("timeout", "connection", "network", "transient", "http", "transport")):
        return FAILURE_TRANSIENT
    return FAILURE_MALFORMED


def missing_gap_fields(row: Any) -> tuple[str, ...]:
    """Return the required GAP fields absent (``None``) on a normalized row."""
    return tuple(field for field in REQUIRED_GAP_FIELDS if getattr(row, field, None) is None)


# ---------------------------------------------------------------------------
# Audit manifest
# ---------------------------------------------------------------------------
def build_backfill_audit_manifest(
    *,
    provider: str,
    provider_league_id: int,
    competition_id: str,
    competition_name: str,
    season: int,
    source_version: str,
    fixture_count: int,
    gap_ready_count: int,
    incomplete_count: int,
    request_count: int,
    request_cap: int,
    missing_field_counts: Mapping[str, int],
    run_state: str,
    retryable_failure_count: int = 0,
    terminal_failure_count: int = 0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Build a reproducible, secret-free audit summary.

    The content signature is computed over a stable subset of deterministic
    fields so two identical backfill states produce the same signature.
    """
    missing = {field: int(missing_field_counts.get(field, 0)) for field in REQUIRED_GAP_FIELDS}
    body: dict[str, Any] = {
        "provider": provider,
        "league_id": int(provider_league_id),
        "competition_id": str(competition_id),
        "competition_name": str(competition_name),
        "season": int(season),
        "source_version": source_version,
        "dry_run": bool(dry_run),
        "fixture_count": int(fixture_count),
        "gap_ready_count": int(gap_ready_count),
        "incomplete_count": int(incomplete_count),
        "retryable_failure_count": int(retryable_failure_count),
        "terminal_failure_count": int(terminal_failure_count),
        "request_count": int(request_count),
        "request_cap": int(request_cap),
        "missing_field_counts": missing,
        "run_state": run_state,
    }
    signature_payload = {
        "provider": provider,
        "league_id": int(provider_league_id),
        "competition_id": str(competition_id),
        "season": int(season),
        "source_version": source_version,
        "fixture_count": int(fixture_count),
        "gap_ready_count": int(gap_ready_count),
        "incomplete_count": int(incomplete_count),
        "request_count": int(request_count),
        "request_cap": int(request_cap),
        "missing_field_counts": missing,
        "run_state": run_state,
    }
    body["content_signature"] = canonical_hash(signature_payload)
    return body
