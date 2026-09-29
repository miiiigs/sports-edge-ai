"""Bounded, resumable, idempotent API-Football historical backfill orchestrator.

This module is the ONLY place that turns a validated :class:`BackfillSpec` into
provider traffic. It:

* resolves the pinned competition + season to a stable provider league id;
* distinguishes the informational whole-job cost (``projected_total_requests``)
  from the bounded, resumable per-invocation plan (``planned_requests_this_run``)
  so a partial run is never refused for the cost of future resumptions;
* runs a pre-flight budget/eligibility gate BEFORE any provider call;
* supports a true zero-provider-request dry run;
* re-reads the (single) fixture list once per run, then checkpoints per-fixture
  progress so a later resume re-fetches the list but never repurchases
  already-complete statistics;
* writes GAP-ready events idempotently (no duplicate historical events);
* classifies every per-fixture failure as retryable or terminal, retrying
  missing/incomplete statistics a bounded number of times before declaring a
  terminal source gap.

Multi-window quota handling
---------------------------

LOW-priority historical traffic may not always afford the whole outstanding job
in a single provider quota window (for example a free plan allowing 100 requests
per day). The orchestrator therefore plans a *tranche*: the largest amount of
work that fits inside the current-window historical headroom above a protected
floor reserved for current/live production, the per-invocation cap, and the
remaining LIFETIME cap for the deterministic job. A tranche that stops early is
``BUDGET_PAUSED`` and fully resumable — never a failure — because the same
deterministic job id, the completion checkpoint, the retryable failures and the
terminal failures are all retained.

The protected floor is ``ceil(effective_limit * HISTORICAL_HEALTHY_RATIO)``,
which is exactly the boundary below which the authoritative provider governor
stops permitting LOW priority. Historical traffic is LOW priority by
construction; the governor still enforces priority, circuit and cache on every
single call, and the orchestrator re-checks eligibility before each request so
live traffic preempts historical work without racing the quota past the floor.

``max_request_cap`` is a LIFETIME ceiling for the logical backfill: consumed
requests are counted on the job row and carry across every resume, so splitting
execution across quota windows cannot multiply the allowed provider cost. The cap
is pinned on first creation and never raised by a later resume; a resume may pass
a lower cap to restrict that run further. When the pinned ceiling is fully spent
while fixtures remain, the job state is ``LIFETIME_CAP_EXHAUSTED`` (resuming
requires explicit new authorization, unlike ``BUDGET_PAUSED``).

Concurrency and durability
--------------------------

Exactly one executor may run a given deterministic job: an atomic conditional
claim keyed on ``IN_PROGRESS`` + ``updated_at`` serializes competing processes,
and a second executor fails closed with zero provider traffic (a crashed run is
reclaimable after ``JOB_CLAIM_STALE_SECONDS``). The winning executor heartbeats
``updated_at`` with the CURRENT clock before every provider request, so a run
that lasts longer than the lease stays claimed and is never falsely reclaimed.
The checkpoint store commits every write immediately (``synchronous=NORMAL``
autocommit), and the lifetime request counter is incremented and committed BEFORE
each provider call, so a crash can at worst over-count a request — never silently
under-count and allow the cap to be exceeded.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, Sequence

from ..data_sources.api_football_backfill import (
    FAILURE_MALFORMED,
    FAILURE_MISSING_STATS,
    FAILURE_NORMALIZATION,
    FAILURE_NOT_FOUND,
    FAILURE_PERSISTENCE,
    FAILURE_TERMINAL_GAP,
    FIXTURE_COMPLETE,
    FIXTURE_PENDING,
    FIXTURE_RETRYABLE,
    FIXTURE_TERMINAL,
    HISTORICAL_HEALTHY_RATIO,
    JOB_BLOCKED,
    JOB_BUDGET_PAUSED,
    JOB_COMPLETE,
    JOB_LIFETIME_CAP_EXHAUSTED,
    JOB_PARTIAL,
    JOB_PENDING,
    PAUSE_ALREADY_RUNNING,
    PAUSE_CIRCUIT_OPEN,
    PAUSE_GOVERNOR_BLOCKED,
    PAUSE_NO_HISTORICAL_HEADROOM,
    PAUSE_REQUEST_CAP_EXHAUSTED,
    BackfillFixtureTarget,
    BackfillSpec,
    BackfillTarget,
    backfill_job_id,
    build_backfill_audit_manifest,
    classify_exception,
    evaluate_backfill_eligibility,
    historical_tranche_allowance,
    is_retryable_failure,
    missing_gap_fields,
    plan_backfill_tranche,
)
from ..data_sources.api_football_history import (
    event_is_gap_ready,
    normalize_api_football_historical_event,
)
from ..storage import football_backfill_store as store
from ..storage.football_canonical_fixture import canonical_fixture_id_from_identity
from .football_api_history_probe import target_for_competition

_FINISHED_STATUS_CODES = frozenset({"FT", "AET", "PEN"})
_REQUIRED_STATISTICS = ("result", "shots", "shots_on_target", "corners")
_GAP_FIELDS = (
    "home_shots",
    "away_shots",
    "home_shots_on_target",
    "away_shots_on_target",
    "home_corners",
    "away_corners",
)

logger = logging.getLogger(__name__)

# How long an IN_PROGRESS marker is trusted before another executor may reclaim a
# crashed run. Larger than any bounded tranche, so a live run is never stolen.
JOB_CLAIM_STALE_SECONDS = 1800


class BackfillProvider(Protocol):
    def get_historical_league_by_id(self, *, league_id: int, season: int) -> list[dict[str, Any]]: ...
    def get_historical_fixtures(self, *, league_id: int, season: int) -> list[dict[str, Any]]: ...
    def get_historical_fixture_statistics(self, *, provider_fixture_id: str) -> list[dict[str, Any]]: ...


class BackfillBudget(Protocol):
    def quota_state(self) -> str: ...
    def effective_remaining(self) -> int | None: ...
    def allow_paid_provider_calls(self) -> bool: ...


# Optional, duck-typed budget capabilities. When a budget exposes them the
# orchestrator uses them to compute the headroom tranche and to detect a
# mid-run governor/circuit block. When absent the provider governor still
# enforces every request, so behaviour stays fail-closed.
_OPTIONAL_BUDGET_EFFECTIVE_LIMIT = "effective_limit"
_OPTIONAL_BUDGET_CIRCUIT_STATE = "circuit_state"


@dataclass(frozen=True)
class BackfillResult:
    job_id: str
    competition_id: str
    competition_name: str
    season: int
    provider_league_id: int
    dry_run: bool
    permitted: bool
    block_reason: str | None
    # Whole-job cost (informational): 1 fixture-list + every outstanding statistic.
    projected_total_requests: int
    # Bounded provider requests THIS invocation intends to make (already capped).
    planned_requests_this_run: int
    max_request_cap: int
    target_fixture_count: int
    completed_count: int
    gap_ready_count: int
    incomplete_count: int
    retryable_failure_count: int
    terminal_failure_count: int
    provider_request_count: int
    missing_field_counts: dict[str, int]
    completion_state: str
    manifest: dict[str, Any]
    fixture_progress: tuple[dict[str, Any], ...] = ()
    pause_reason: str | None = None
    protected_floor: int | None = None
    historical_headroom: int | None = None
    requests_consumed_total: int = 0
    planned_capped_by: str | None = None
    effective_remaining: int | None = None
    next_eligible_state: str | None = None
    lifetime_cap: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("fixture_progress", None)
        return payload


def _fixture_payload(fixture: dict[str, Any]) -> dict[str, Any]:
    return (fixture.get("fixture") or {})


def _fixture_status_short(fixture: dict[str, Any]) -> str:
    return str(((_fixture_payload(fixture).get("status") or {}).get("short")) or "").upper()


def _fixture_league(fixture: dict[str, Any]) -> dict[str, Any]:
    return fixture.get("league") or {}


def _fixture_kickoff(fixture: dict[str, Any]) -> str:
    return str(_fixture_payload(fixture).get("date") or "")


def _fixture_date_iso(fixture: dict[str, Any]) -> str | None:
    raw = _fixture_kickoff(fixture).strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).date().isoformat()
    except ValueError:
        return raw[:10] or None


def _in_scope(fixture: dict[str, Any], *, provider_league_id: int, season: int) -> bool:
    league = _fixture_league(fixture)
    league_id = league.get("id")
    if league_id is not None:
        try:
            if int(league_id) != int(provider_league_id):
                return False
        except (TypeError, ValueError):
            return False
    league_season = league.get("season")
    if league_season is not None:
        try:
            if int(league_season) != int(season):
                return False
        except (TypeError, ValueError):
            return False
    return True


def _fixture_identity(fixture: dict[str, Any], *, competition_id: str) -> tuple[str, str, str, str] | None:
    teams = fixture.get("teams") or {}
    home = (teams.get("home") or {}).get("name")
    away = (teams.get("away") or {}).get("name")
    kickoff = _fixture_kickoff(fixture)
    provider_fixture_id = str(_fixture_payload(fixture).get("id") or "").strip()
    if not provider_fixture_id or not home or not away or not kickoff:
        return None
    canonical = canonical_fixture_id_from_identity(
        competition_id=competition_id,
        home_team_name=str(home),
        away_team_name=str(away),
        kickoff_utc=kickoff,
    )
    return canonical, provider_fixture_id, str(home), str(away)


def completed_target_fixtures(
    fixtures: Sequence[dict[str, Any]],
    *,
    competition_id: str,
    provider_league_id: int,
    season: int,
) -> list[tuple[BackfillFixtureTarget, dict[str, Any]]]:
    """Return in-scope, completed fixtures in deterministic canonical order.

    Each entry is ``(BackfillFixtureTarget, raw_fixture_payload)`` so the caller
    can normalize the raw payload without re-fetching it.
    """
    scoped: list[tuple[str, str, dict[str, Any]]] = []
    for fixture in fixtures:
        if _fixture_status_short(fixture) not in _FINISHED_STATUS_CODES:
            continue
        if not _in_scope(fixture, provider_league_id=provider_league_id, season=season):
            continue
        identity = _fixture_identity(fixture, competition_id=competition_id)
        if identity is None:
            continue
        scoped.append((_fixture_kickoff(fixture), identity[1], fixture))
    scoped.sort(key=lambda item: (item[0], item[1]))

    result: list[tuple[BackfillFixtureTarget, dict[str, Any]]] = []
    for _kickoff, _provider_id, fixture in scoped:
        identity = _fixture_identity(fixture, competition_id=competition_id)
        assert identity is not None
        canonical, provider_id, home, away = identity
        result.append(
            (
                BackfillFixtureTarget(
                    canonical_fixture_id=canonical,
                    provider_fixture_id=provider_id,
                    match_date=_fixture_date_iso(fixture),
                    home_team=home,
                    away_team=away,
                ),
                fixture,
            )
        )
    return result


def _blocked_result(
    *,
    job_id: str,
    spec: BackfillSpec,
    target: BackfillTarget,
    reason: str,
    dry_run: bool,
    projected_total_requests: int,
    planned_requests_this_run: int,
    target_fixture_count: int,
    provider_request_count: int = 0,
    completed_count: int = 0,
    completion_state: str = JOB_BLOCKED,
    pause_reason: str | None = None,
    protected_floor: int | None = None,
    historical_headroom: int | None = None,
    requests_consumed_total: int = 0,
    planned_capped_by: str | None = None,
    effective_remaining: int | None = None,
    next_eligible_state: str | None = None,
    lifetime_cap: int | None = None,
) -> BackfillResult:
    incomplete = max(0, target_fixture_count - completed_count)
    manifest = build_backfill_audit_manifest(
        provider=target.provider,
        provider_league_id=target.provider_league_id,
        competition_id=target.competition_id,
        competition_name=target.competition_name,
        season=spec.season,
        source_version=target.source_version,
        fixture_count=target_fixture_count,
        gap_ready_count=completed_count,
        incomplete_count=incomplete,
        request_count=provider_request_count,
        request_cap=spec.max_request_cap,
        projected_total_requests=projected_total_requests,
        planned_requests_this_run=planned_requests_this_run,
        missing_field_counts={},
        run_state=completion_state,
        dry_run=dry_run,
        protected_floor=protected_floor,
        historical_headroom=historical_headroom,
        pause_reason=pause_reason,
        requests_consumed_total=requests_consumed_total,
        effective_remaining=effective_remaining,
        next_eligible_state=next_eligible_state,
        lifetime_cap=lifetime_cap,
    )
    return BackfillResult(
        job_id=job_id,
        competition_id=target.competition_id,
        competition_name=target.competition_name,
        season=spec.season,
        provider_league_id=target.provider_league_id,
        dry_run=dry_run,
        permitted=False,
        block_reason=reason,
        projected_total_requests=projected_total_requests,
        planned_requests_this_run=planned_requests_this_run,
        max_request_cap=spec.max_request_cap,
        target_fixture_count=target_fixture_count,
        completed_count=completed_count,
        gap_ready_count=completed_count,
        incomplete_count=incomplete,
        retryable_failure_count=0,
        terminal_failure_count=0,
        provider_request_count=provider_request_count,
        missing_field_counts={},
        completion_state=completion_state,
        manifest=manifest,
        pause_reason=pause_reason,
        protected_floor=protected_floor,
        historical_headroom=historical_headroom,
        requests_consumed_total=requests_consumed_total,
        planned_capped_by=planned_capped_by,
        effective_remaining=effective_remaining,
        next_eligible_state=next_eligible_state,
        lifetime_cap=lifetime_cap,
    )


def _planned_for_known_count(
    *,
    known_fixture_count: int,
    completed_count: int,
    max_request_cap: int,
) -> int:
    """Bounded planned requests for this invocation from a known/hinted count.

    The fixture list is one request; at most ``max_request_cap - 1`` statistics
    requests follow. Already-complete fixtures need no statistics.
    """
    outstanding = max(0, int(known_fixture_count) - int(completed_count))
    stats = min(outstanding, max(0, int(max_request_cap) - 1))
    return 1 + stats


def _budget_effective_limit(budget: BackfillBudget) -> int | None:
    """Read the optional ``effective_limit`` capability without requiring it."""
    fn = getattr(budget, _OPTIONAL_BUDGET_EFFECTIVE_LIMIT, None)
    if not callable(fn):
        return None
    try:
        value = fn()
    except Exception:  # noqa: BLE001
        return None
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _budget_circuit_state(budget: BackfillBudget) -> str | None:
    """Read the optional ``circuit_state`` capability without requiring it."""
    fn = getattr(budget, _OPTIONAL_BUDGET_CIRCUIT_STATE, None)
    if not callable(fn):
        return None
    try:
        state = fn()
    except Exception:  # noqa: BLE001
        return "UNKNOWN"
    text = str(state or "").strip().upper()
    return text or None


def _historical_request_allowed(
    budget: BackfillBudget,
    *,
    ratio: float = HISTORICAL_HEALTHY_RATIO,
) -> tuple[bool, str | None]:
    """May LOW-priority historical traffic proceed right now?

    The provider governor remains authoritative; this mirrors its rules so the
    orchestrator stops CLEANLY (and checkpoints) instead of racing the shared
    quota toward the protected floor or recording spurious per-fixture failures
    when current/live traffic consumes quota mid-run.
    """
    if not budget.allow_paid_provider_calls():
        return False, PAUSE_GOVERNOR_BLOCKED
    state = budget.quota_state()
    if state != "HEALTHY":
        return False, f"LOW_PRIORITY_QUOTA_{state}"
    circuit = _budget_circuit_state(budget)
    if circuit is not None and circuit != "CLOSED":
        return False, PAUSE_CIRCUIT_OPEN
    headroom = historical_tranche_allowance(
        effective_remaining=budget.effective_remaining(),
        effective_limit=_budget_effective_limit(budget),
        ratio=ratio,
    )
    if headroom is not None and headroom < 1:
        return False, PAUSE_NO_HISTORICAL_HEADROOM
    return True, None


def _next_eligible_state(completion_state: str, pause_reason: str | None) -> str:
    """Human-readable description of what unblocks the next resume."""
    if completion_state == JOB_COMPLETE:
        return "COMPLETE"
    if completion_state == JOB_LIFETIME_CAP_EXHAUSTED or pause_reason == PAUSE_REQUEST_CAP_EXHAUSTED:
        return "REQUIRES_NEW_AUTHORIZATION"
    if pause_reason == PAUSE_CIRCUIT_OPEN:
        return "AWAITING_CIRCUIT_RECOVERY"
    if pause_reason == PAUSE_GOVERNOR_BLOCKED:
        return "AWAITING_PROVIDER_GATE"
    if pause_reason is not None:
        return "AWAITING_QUOTA_RESET"
    return "RESUMABLE"


def _format_utc(value: datetime) -> str:
    """Normalize any datetime to a UTC ``...Z``-style second-resolution string."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _utc_iso(clock) -> str:
    """CURRENT UTC timestamp from the run clock (used for lease heartbeats)."""
    return _format_utc(clock())


def run_backfill(
    provider: BackfillProvider,
    budget: BackfillBudget,
    *,
    spec: BackfillSpec,
    store_conn,
    dry_run: bool = False,
    fixture_count_hint: int | None = None,
    now: datetime | None = None,
    clock=None,
) -> BackfillResult:
    """Run (or dry-run) one bounded, resumable league-season backfill.

    ``now`` fixes the run-start instant. ``clock`` supplies the CURRENT instant
    used for lease heartbeats and checkpoint timestamps; it defaults to real UTC,
    or to the fixed ``now`` when one is injected for deterministic tests. Lease
    heartbeats must never reuse the run-start stamp, otherwise a run longer than
    ``JOB_CLAIM_STALE_SECONDS`` would look stale while still making progress.
    """
    clock = clock or (lambda: now if now is not None else datetime.now(timezone.utc))
    current = now or clock()
    now_iso = _format_utc(current)

    def _stamp() -> str:
        return _utc_iso(clock)

    validation_error = spec.validate()
    if validation_error is not None:
        empty_target = BackfillTarget(
            competition_id=spec.competition_id,
            competition_name=spec.competition_id,
            division_code="",
            provider_league_id=0,
            season=spec.season,
        )
        return _blocked_result(
            job_id="",
            spec=spec,
            target=empty_target,
            reason=validation_error,
            dry_run=dry_run,
            projected_total_requests=0,
            planned_requests_this_run=0,
            target_fixture_count=0,
        )

    probe_target = target_for_competition(spec.competition_id)
    league_id = probe_target.provider_league_id
    if league_id is None or int(league_id) <= 0:
        no_id_target = BackfillTarget(
            competition_id=probe_target.competition_id,
            competition_name=probe_target.competition_name,
            division_code=probe_target.division_code,
            provider_league_id=0,
            season=spec.season,
        )
        return _blocked_result(
            job_id="",
            spec=spec,
            target=no_id_target,
            reason="NO_STABLE_LEAGUE_ID",
            dry_run=dry_run,
            projected_total_requests=0,
            planned_requests_this_run=0,
            target_fixture_count=0,
        )

    target = BackfillTarget(
        competition_id=probe_target.competition_id,
        competition_name=probe_target.competition_name,
        division_code=probe_target.division_code,
        provider_league_id=int(league_id),
        season=spec.season,
    )
    job_id = backfill_job_id(
        provider=spec.provider,
        competition_id=target.competition_id,
        season=spec.season,
        provider_league_id=target.provider_league_id,
        source_version=target.source_version,
    )

    store.ensure_schema(store_conn)
    persisted = store.load_job(store_conn, job_id)
    progress_rows = store.fetch_fixture_progress(store_conn, job_id)
    completed_ids = store.completed_canonical_fixture_ids(store_conn, job_id)
    progress_by_canonical = {row["canonical_fixture_id"]: row for row in progress_rows}

    known_fixture_count = 0
    if persisted is not None and int(persisted.get("target_fixture_count") or 0) > 0:
        known_fixture_count = int(persisted["target_fixture_count"])
    elif fixture_count_hint is not None:
        known_fixture_count = max(0, int(fixture_count_hint))

    requests_consumed_before = int((persisted or {}).get("requests_consumed") or 0)

    # The hard cap is a LIFETIME ceiling for the logical job. It is pinned on
    # first creation (football_backfill_jobs.max_request_cap, which create_job
    # never rewrites on conflict). A later resume may LOWER it, but a higher CLI
    # value can never raise the already-authorized ceiling.
    original_cap: int | None = None
    if persisted is not None:
        try:
            candidate = int(persisted.get("max_request_cap"))
            original_cap = candidate if candidate >= 1 else None
        except (TypeError, ValueError):
            original_cap = None
    requested_cap = max(1, int(spec.max_request_cap))
    if original_cap is None:
        # Fresh job, or an old row with no usable pinned cap: pin now, but never
        # below what has already been consumed.
        lifetime_cap = max(requested_cap, requests_consumed_before)
        cap_source = "PINNED_NOW"
    elif requested_cap > original_cap:
        lifetime_cap = original_cap
        cap_source = "PINNED_ORIGINAL"
        logger.warning(
            "BACKFILL_CAP job=%s requested_cap=%s ignored; pinned lifetime cap=%s remains authoritative",
            job_id,
            requested_cap,
            original_cap,
        )
    else:
        lifetime_cap = requested_cap
        cap_source = "PINNED_ORIGINAL"
    logger.info(
        "BACKFILL_CAP job=%s source=%s requested_cap=%s lifetime_cap=%s consumed=%s",
        job_id,
        cap_source,
        requested_cap,
        lifetime_cap,
        requests_consumed_before,
    )

    # Whole-job cost (informational) and bounded outstanding plan this window.
    projected_total_requests = 1 + known_fixture_count
    full_plan_requests = _planned_for_known_count(
        known_fixture_count=known_fixture_count,
        completed_count=len(completed_ids),
        max_request_cap=lifetime_cap,
    )

    # Quota-window tranche: never plan more than the current-window historical
    # headroom, the per-invocation cap, or the remaining LIFETIME cap for this
    # deterministic job. A partial tranche is normal and resumable, not a failure.
    tranche = plan_backfill_tranche(
        full_plan_requests=full_plan_requests,
        effective_remaining=budget.effective_remaining(),
        effective_limit=_budget_effective_limit(budget),
        max_request_cap=lifetime_cap,
        requests_consumed=requests_consumed_before,
    )
    planned_requests_this_run = tranche.planned_requests

    eligibility = evaluate_backfill_eligibility(
        required_requests=planned_requests_this_run,
        max_request_cap=lifetime_cap,
        budget_state=budget.quota_state(),
        budget_remaining=budget.effective_remaining(),
        paid_provider_calls_allowed=budget.allow_paid_provider_calls(),
    )

    if not eligibility.permitted:
        store.create_job(
            store_conn,
            job_id=job_id,
            provider=target.provider,
            competition_id=target.competition_id,
            competition_name=target.competition_name,
            division_code=target.division_code,
            season=spec.season,
            season_code=target.season_code,
            provider_league_id=target.provider_league_id,
            source_version=target.source_version,
            required_statistics=_REQUIRED_STATISTICS,
            max_request_cap=lifetime_cap,
            max_retries_per_fixture=spec.max_retries_per_fixture,
            projected_total_requests=projected_total_requests,
            target_fixture_count=known_fixture_count,
            start_timestamp=now_iso,
            completion_state=JOB_BLOCKED,
        )
        return _blocked_result(
            job_id=job_id,
            spec=spec,
            target=target,
            reason=eligibility.reason,
            dry_run=dry_run,
            projected_total_requests=projected_total_requests,
            planned_requests_this_run=planned_requests_this_run,
            target_fixture_count=known_fixture_count,
            completed_count=len(completed_ids),
            protected_floor=tranche.protected_floor,
            historical_headroom=tranche.historical_headroom,
            requests_consumed_total=requests_consumed_before,
            planned_capped_by=tranche.capped_by,
            effective_remaining=tranche.effective_remaining,
            next_eligible_state=_next_eligible_state(JOB_BLOCKED, eligibility.reason),
            lifetime_cap=lifetime_cap,
        )

    if planned_requests_this_run < 1:
        # Fail closed before ANY provider call. The target has not been read yet,
        # so no checkpoint is created; the deterministic job resumes in a later
        # window when the quota headroom recovers.
        if tranche.capped_by == "REQUEST_CAP":
            reason = PAUSE_REQUEST_CAP_EXHAUSTED
            paused_state = JOB_LIFETIME_CAP_EXHAUSTED
        else:
            reason = PAUSE_NO_HISTORICAL_HEADROOM
            paused_state = JOB_BUDGET_PAUSED
        return _blocked_result(
            job_id=job_id,
            spec=spec,
            target=target,
            reason=reason,
            dry_run=dry_run,
            projected_total_requests=projected_total_requests,
            planned_requests_this_run=0,
            target_fixture_count=known_fixture_count,
            completed_count=len(completed_ids),
            completion_state=paused_state,
            pause_reason=reason,
            protected_floor=tranche.protected_floor,
            historical_headroom=tranche.historical_headroom,
            requests_consumed_total=requests_consumed_before,
            planned_capped_by=tranche.capped_by,
            effective_remaining=tranche.effective_remaining,
            next_eligible_state=_next_eligible_state(paused_state, reason),
            lifetime_cap=lifetime_cap,
        )

    if dry_run:
        completed_count = len(completed_ids)
        outstanding = max(0, known_fixture_count - completed_count)
        if known_fixture_count > 0 and completed_count == known_fixture_count:
            run_state = JOB_COMPLETE
        elif completed_count == 0:
            run_state = JOB_PENDING
        else:
            run_state = JOB_PARTIAL
        manifest = build_backfill_audit_manifest(
            provider=target.provider,
            provider_league_id=target.provider_league_id,
            competition_id=target.competition_id,
            competition_name=target.competition_name,
            season=spec.season,
            source_version=target.source_version,
            fixture_count=known_fixture_count,
            gap_ready_count=completed_count,
            incomplete_count=outstanding,
            request_count=0,
            request_cap=spec.max_request_cap,
            projected_total_requests=projected_total_requests,
            planned_requests_this_run=planned_requests_this_run,
            missing_field_counts={},
            run_state=run_state,
            dry_run=True,
        )
        return BackfillResult(
            job_id=job_id,
            competition_id=target.competition_id,
            competition_name=target.competition_name,
            season=spec.season,
            provider_league_id=target.provider_league_id,
            dry_run=True,
            permitted=True,
            block_reason=None,
            projected_total_requests=projected_total_requests,
            planned_requests_this_run=planned_requests_this_run,
            max_request_cap=spec.max_request_cap,
            target_fixture_count=known_fixture_count,
            completed_count=completed_count,
            gap_ready_count=completed_count,
            incomplete_count=outstanding,
            retryable_failure_count=0,
            terminal_failure_count=0,
            provider_request_count=0,
            missing_field_counts={},
            completion_state=run_state,
            manifest=manifest,
            protected_floor=tranche.protected_floor,
            historical_headroom=tranche.historical_headroom,
            requests_consumed_total=requests_consumed_before,
            planned_capped_by=tranche.capped_by,
            effective_remaining=tranche.effective_remaining,
            next_eligible_state=_next_eligible_state(run_state, None),
            lifetime_cap=lifetime_cap,
        )

    # ---- real run ----------------------------------------------------------
    store.create_job(
        store_conn,
        job_id=job_id,
        provider=target.provider,
        competition_id=target.competition_id,
        competition_name=target.competition_name,
        division_code=target.division_code,
        season=spec.season,
        season_code=target.season_code,
        provider_league_id=target.provider_league_id,
        source_version=target.source_version,
        required_statistics=_REQUIRED_STATISTICS,
        max_request_cap=lifetime_cap,
        max_retries_per_fixture=spec.max_retries_per_fixture,
        projected_total_requests=projected_total_requests,
        target_fixture_count=known_fixture_count,
        start_timestamp=now_iso,
        completion_state=JOB_PENDING,
    )

    # Serialize execution: exactly one executor may run this deterministic job.
    # The claim is one atomic conditional UPDATE keyed on IN_PROGRESS+updated_at,
    # so a second concurrent process fails closed before any provider traffic.
    # A completed/paused run releases the job (its state is not IN_PROGRESS); a
    # crashed run is reclaimable once its updated_at is older than the lease.
    claim_stale_before = (current - timedelta(seconds=JOB_CLAIM_STALE_SECONDS)).isoformat(
        timespec="seconds"
    )
    if not store.try_claim_job(
        store_conn,
        job_id=job_id,
        now_iso=_stamp(),
        stale_before_iso=claim_stale_before,
    ):
        return _blocked_result(
            job_id=job_id,
            spec=spec,
            target=target,
            reason=PAUSE_ALREADY_RUNNING,
            dry_run=False,
            projected_total_requests=projected_total_requests,
            planned_requests_this_run=0,
            target_fixture_count=known_fixture_count,
            completed_count=len(completed_ids),
            completion_state=JOB_BLOCKED,
            pause_reason=PAUSE_ALREADY_RUNNING,
            protected_floor=tranche.protected_floor,
            historical_headroom=tranche.historical_headroom,
            requests_consumed_total=requests_consumed_before,
            planned_capped_by=tranche.capped_by,
            effective_remaining=tranche.effective_remaining,
            next_eligible_state="RESUMABLE",
            lifetime_cap=lifetime_cap,
        )

    # The fixture-list request is accounted for as soon as it is initiated, so a
    # failure after the request is sent still reflects a consumed attempt. It is
    # counted against both this run and the lifetime cap for the logical job, and
    # the lease is refreshed with the CURRENT clock BEFORE dispatch so a long list
    # step cannot let the claim go stale.
    list_stamp = _stamp()
    store.heartbeat_job(store_conn, job_id=job_id, now_iso=list_stamp)
    provider_request_count = 1
    requests_consumed = requests_consumed_before + 1
    store.increment_job_requests(store_conn, job_id=job_id, count=1, updated_at=list_stamp)
    try:
        # The raw fixture payload is required to normalize statistics into events
        # and is not persisted in the checkpoint, so the list is re-read once per
        # run (including resumes) and always counted against the tranche.
        raw_fixtures = provider.get_historical_fixtures(
            league_id=target.provider_league_id,
            season=spec.season,
        )
    except Exception as exc:  # noqa: BLE001
        store.update_job_state(store_conn, job_id=job_id, completion_state=JOB_BLOCKED, updated_at=_stamp())
        return _blocked_result(
            job_id=job_id,
            spec=spec,
            target=target,
            reason=classify_exception(exc),
            dry_run=False,
            projected_total_requests=projected_total_requests,
            planned_requests_this_run=planned_requests_this_run,
            target_fixture_count=known_fixture_count,
            provider_request_count=provider_request_count,
            completed_count=len(completed_ids),
            requests_consumed_total=requests_consumed,
            lifetime_cap=lifetime_cap,
        )
    if raw_fixtures is None:
        store.update_job_state(store_conn, job_id=job_id, completion_state=JOB_BLOCKED, updated_at=_stamp())
        return _blocked_result(
            job_id=job_id,
            spec=spec,
            target=target,
            reason=FAILURE_MALFORMED,
            dry_run=False,
            projected_total_requests=projected_total_requests,
            planned_requests_this_run=planned_requests_this_run,
            target_fixture_count=known_fixture_count,
            provider_request_count=provider_request_count,
            completed_count=len(completed_ids),
            requests_consumed_total=requests_consumed,
            lifetime_cap=lifetime_cap,
        )

    targets = completed_target_fixtures(
        raw_fixtures,
        competition_id=target.competition_id,
        provider_league_id=target.provider_league_id,
        season=spec.season,
    )
    target_fixture_count = len(targets)
    projected_total_requests = 1 + target_fixture_count
    store.update_job_plan(
        store_conn,
        job_id=job_id,
        projected_total_requests=projected_total_requests,
        target_fixture_count=target_fixture_count,
        updated_at=_stamp(),
    )

    if target_fixture_count == 0:
        store.update_job_state(store_conn, job_id=job_id, completion_state=JOB_BLOCKED, updated_at=_stamp())
        return _blocked_result(
            job_id=job_id,
            spec=spec,
            target=target,
            reason=FAILURE_NOT_FOUND,
            dry_run=False,
            projected_total_requests=projected_total_requests,
            planned_requests_this_run=provider_request_count,
            target_fixture_count=0,
            provider_request_count=provider_request_count,
            completed_count=len(completed_ids),
            requests_consumed_total=requests_consumed,
            lifetime_cap=lifetime_cap,
        )

    # Seed PENDING progress rows for any target fixture we have not seen before
    # so resume can enumerate the full remaining canonical set.
    for fixture_target, _raw in targets:
        if fixture_target.canonical_fixture_id in progress_by_canonical:
            continue
        store.upsert_fixture_progress(
            store_conn,
            job_id=job_id,
            canonical_fixture_id=fixture_target.canonical_fixture_id,
            provider_fixture_id=fixture_target.provider_fixture_id,
            match_date=fixture_target.match_date,
            home_team=fixture_target.home_team,
            away_team=fixture_target.away_team,
            state=FIXTURE_PENDING,
            failure_kind=None,
            missing_fields=(),
            attempt_count=0,
            event_id=None,
            updated_at=_stamp(),
        )

    # Refresh progress view after seeding.
    progress_rows = store.fetch_fixture_progress(store_conn, job_id)
    progress_by_canonical = {row["canonical_fixture_id"]: row for row in progress_rows}
    completed_ids = store.completed_canonical_fixture_ids(store_conn, job_id)

    outstanding = [
        target_row
        for target_row, _raw in targets
        if target_row.canonical_fixture_id not in completed_ids
        and progress_by_canonical.get(target_row.canonical_fixture_id, {}).get("state") != FIXTURE_TERMINAL
    ]

    # The fixture-list request is already spent. Bound the statistics requests by
    # the outstanding set, the per-invocation cap, the LIFETIME cap and the
    # current-window historical headroom.
    if not outstanding:
        completion_state = _finalize(
            store_conn,
            job_id=job_id,
            target_fixture_count=target_fixture_count,
            now_iso=_stamp(),
        )
        return _summarize(
            store_conn,
            job_id=job_id,
            target=target,
            spec=spec,
            target_fixture_count=target_fixture_count,
            projected_total_requests=projected_total_requests,
            planned_requests_this_run=planned_requests_this_run,
            provider_request_count=provider_request_count,
            now_iso=_stamp(),
            completion_state=completion_state,
            protected_floor=tranche.protected_floor,
            historical_headroom=tranche.historical_headroom,
            requests_consumed_total=requests_consumed,
            planned_capped_by=tranche.capped_by,
            effective_remaining=budget.effective_remaining(),
            next_eligible_state=_next_eligible_state(completion_state, None),
            lifetime_cap=lifetime_cap,
        )

    allowed_now, pause_reason = _historical_request_allowed(budget)
    headroom_now = historical_tranche_allowance(
        effective_remaining=budget.effective_remaining(),
        effective_limit=_budget_effective_limit(budget),
    )
    lifetime_remaining = max(0, lifetime_cap - requests_consumed)
    per_run_remaining = max(0, spec.max_request_cap - provider_request_count)
    allowed_stats = min(len(outstanding), per_run_remaining, lifetime_remaining)
    if headroom_now is not None:
        allowed_stats = min(allowed_stats, headroom_now)

    if not allowed_now or allowed_stats < 1:
        if lifetime_remaining < 1:
            paused_state = JOB_LIFETIME_CAP_EXHAUSTED
            reason = PAUSE_REQUEST_CAP_EXHAUSTED
        else:
            paused_state = JOB_BUDGET_PAUSED
            reason = pause_reason or PAUSE_NO_HISTORICAL_HEADROOM
        store.update_job_state(store_conn, job_id=job_id, completion_state=paused_state, updated_at=_stamp())
        return _summarize(
            store_conn,
            job_id=job_id,
            target=target,
            spec=spec,
            target_fixture_count=target_fixture_count,
            projected_total_requests=projected_total_requests,
            planned_requests_this_run=provider_request_count,
            provider_request_count=provider_request_count,
            now_iso=_stamp(),
            completion_state=paused_state,
            pause_reason=reason,
            protected_floor=tranche.protected_floor,
            historical_headroom=headroom_now,
            requests_consumed_total=requests_consumed,
            planned_capped_by=tranche.capped_by,
            effective_remaining=budget.effective_remaining(),
            next_eligible_state=_next_eligible_state(paused_state, reason),
            lifetime_cap=lifetime_cap,
        )

    planned_stats_this_run = allowed_stats
    planned_requests_this_run = provider_request_count + planned_stats_this_run

    raw_by_provider_id = {tgt.provider_fixture_id: raw for tgt, raw in targets}

    pause_reason = None
    for target_row in outstanding:
        if requests_consumed >= lifetime_cap:
            pause_reason = PAUSE_REQUEST_CAP_EXHAUSTED
            break

        allowed, reason = _historical_request_allowed(budget)
        if not allowed:
            # The governor (quota/circuit/gate) withheld LOW-priority traffic.
            # Stop cleanly and keep every checkpoint so the job resumes later.
            pause_reason = reason
            break

        existing = progress_by_canonical.get(target_row.canonical_fixture_id, {})
        attempt_count = int(existing.get("attempt_count") or 0)

        # Refresh the lease with the CURRENT clock and account the attempt BEFORE
        # dispatch: a run longer than the stale window stays claimed, and a crash
        # after dispatch is already counted (never silently under-counted).
        request_stamp = _stamp()
        store.heartbeat_job(store_conn, job_id=job_id, now_iso=request_stamp)
        provider_request_count += 1
        requests_consumed += 1
        store.increment_job_requests(store_conn, job_id=job_id, count=1, updated_at=request_stamp)
        try:
            statistics = provider.get_historical_fixture_statistics(
                provider_fixture_id=target_row.provider_fixture_id,
            )
        except Exception as exc:  # noqa: BLE001
            _record_attempt_failure(
                store_conn,
                job_id=job_id,
                target_row=target_row,
                failure_kind=classify_exception(exc),
                message=str(exc),
                missing_fields=(),
                attempt_count=attempt_count,
                max_retries_per_fixture=spec.max_retries_per_fixture,
                now_iso=_stamp(),
            )
            continue

        if not statistics:
            allowed_after, reason_after = _historical_request_allowed(budget)
            if not allowed_after:
                # The governor withheld the request (quota/circuit) rather than
                # the fixture being genuinely empty. Pause cleanly and leave the
                # fixture PENDING so a later window can retry it.
                pause_reason = reason_after
                break
            _record_attempt_failure(
                store_conn,
                job_id=job_id,
                target_row=target_row,
                failure_kind=FAILURE_MISSING_STATS,
                message="provider returned no statistics rows",
                missing_fields=(),
                attempt_count=attempt_count,
                max_retries_per_fixture=spec.max_retries_per_fixture,
                now_iso=_stamp(),
            )
            continue

        raw_fixture = raw_by_provider_id[target_row.provider_fixture_id]
        normalized = normalize_api_football_historical_event(
            raw_fixture,
            statistics,
            competition_id=target.competition_id,
            competition_name=target.competition_name,
            division_code=target.division_code,
            season_code=target.season_code,
        )
        if normalized is None:
            _record_attempt_failure(
                store_conn,
                job_id=job_id,
                target_row=target_row,
                failure_kind=FAILURE_NORMALIZATION,
                message="fixture could not be normalized",
                missing_fields=(),
                attempt_count=attempt_count,
                max_retries_per_fixture=spec.max_retries_per_fixture,
                now_iso=_stamp(),
            )
            continue

        if not event_is_gap_ready(normalized):
            _record_attempt_failure(
                store_conn,
                job_id=job_id,
                target_row=target_row,
                failure_kind=FAILURE_MISSING_STATS,
                message="required GAP statistics missing",
                missing_fields=missing_gap_fields(normalized),
                attempt_count=attempt_count,
                max_retries_per_fixture=spec.max_retries_per_fixture,
                now_iso=_stamp(),
            )
            continue

        try:
            store.upsert_backfill_event(store_conn, normalized)
        except Exception as exc:  # noqa: BLE001
            _record_attempt_failure(
                store_conn,
                job_id=job_id,
                target_row=target_row,
                failure_kind=FAILURE_PERSISTENCE,
                message=str(exc),
                missing_fields=(),
                attempt_count=attempt_count,
                max_retries_per_fixture=spec.max_retries_per_fixture,
                now_iso=_stamp(),
            )
            continue

        store.upsert_fixture_progress(
            store_conn,
            job_id=job_id,
            canonical_fixture_id=target_row.canonical_fixture_id,
            provider_fixture_id=target_row.provider_fixture_id,
            match_date=target_row.match_date,
            home_team=target_row.home_team,
            away_team=target_row.away_team,
            state=FIXTURE_COMPLETE,
            failure_kind=None,
            missing_fields=(),
            attempt_count=attempt_count + 1,
            event_id=normalized.event_id,
            updated_at=_stamp(),
        )

    completion_state = _finalize(
        store_conn,
        job_id=job_id,
        target_fixture_count=target_fixture_count,
        now_iso=_stamp(),
        pause_reason=pause_reason,
    )
    return _summarize(
        store_conn,
        job_id=job_id,
        target=target,
        spec=spec,
        target_fixture_count=target_fixture_count,
        projected_total_requests=projected_total_requests,
        planned_requests_this_run=planned_requests_this_run,
        provider_request_count=provider_request_count,
        now_iso=_stamp(),
        completion_state=completion_state,
        pause_reason=pause_reason,
        protected_floor=tranche.protected_floor,
        historical_headroom=tranche.historical_headroom,
        requests_consumed_total=requests_consumed,
        planned_capped_by=tranche.capped_by,
        effective_remaining=budget.effective_remaining(),
        next_eligible_state=_next_eligible_state(completion_state, pause_reason),
        lifetime_cap=lifetime_cap,
    )


def _record_attempt_failure(
    store_conn,
    *,
    job_id: str,
    target_row: BackfillFixtureTarget,
    failure_kind: str,
    message: str | None,
    missing_fields: Sequence[str],
    attempt_count: int,
    max_retries_per_fixture: int,
    now_iso: str,
) -> None:
    """Record one failed provider attempt, deciding retryable vs terminal.

    ``attempt_count`` is the number of attempts ALREADY made before this one.
    A retryable failure becomes terminal only once the configured retry allowance
    (``max_retries_per_fixture`` retries after the initial attempt) is exhausted.
    Terminal failures (NOT_FOUND / MALFORMED / NORMALIZATION) stay terminal
    immediately.
    """
    new_attempt_count = int(attempt_count) + 1
    if is_retryable_failure(failure_kind):
        retries_used = new_attempt_count - 1
        if retries_used >= int(max_retries_per_fixture):
            state = FIXTURE_TERMINAL
            kind = FAILURE_TERMINAL_GAP
        else:
            state = FIXTURE_RETRYABLE
            kind = failure_kind
    else:
        state = FIXTURE_TERMINAL
        kind = failure_kind

    store.upsert_fixture_progress(
        store_conn,
        job_id=job_id,
        canonical_fixture_id=target_row.canonical_fixture_id,
        provider_fixture_id=target_row.provider_fixture_id,
        match_date=target_row.match_date,
        home_team=target_row.home_team,
        away_team=target_row.away_team,
        state=state,
        failure_kind=kind,
        missing_fields=tuple(missing_fields),
        attempt_count=new_attempt_count,
        event_id=None,
        updated_at=now_iso,
    )
    store.record_failure(
        store_conn,
        job_id=job_id,
        canonical_fixture_id=target_row.canonical_fixture_id,
        provider_fixture_id=target_row.provider_fixture_id,
        failure_kind=kind,
        message=message,
        recorded_at=now_iso,
    )


def _finalize(
    store_conn,
    *,
    job_id: str,
    target_fixture_count: int,
    now_iso: str,
    pause_reason: str | None = None,
) -> str:
    """Persist the terminal-for-now job state and return it.

    A fully populated target is COMPLETE. A stop caused by the provider governor
    or quota headroom is BUDGET_PAUSED and stays resumable after a quota reset. A
    stop caused by the hard lifetime request cap is LIFETIME_CAP_EXHAUSTED and is
    NOT cleared by a quota reset (it requires explicit new authorization). Any
    other incomplete stop is PARTIAL.
    """
    progress = store.fetch_fixture_progress(store_conn, job_id)
    completed = sum(1 for row in progress if row["state"] == FIXTURE_COMPLETE)
    if target_fixture_count > 0 and completed == target_fixture_count:
        completion_state = JOB_COMPLETE
    elif pause_reason == PAUSE_REQUEST_CAP_EXHAUSTED:
        completion_state = JOB_LIFETIME_CAP_EXHAUSTED
    elif pause_reason is not None:
        completion_state = JOB_BUDGET_PAUSED
    else:
        completion_state = JOB_PARTIAL
    store.update_job_state(store_conn, job_id=job_id, completion_state=completion_state, updated_at=now_iso)
    return completion_state


def _summarize(
    store_conn,
    *,
    job_id: str,
    target: BackfillTarget,
    spec: BackfillSpec,
    target_fixture_count: int,
    projected_total_requests: int,
    planned_requests_this_run: int,
    provider_request_count: int,
    now_iso: str,
    completion_state: str,
    pause_reason: str | None = None,
    protected_floor: int | None = None,
    historical_headroom: int | None = None,
    requests_consumed_total: int = 0,
    planned_capped_by: str | None = None,
    effective_remaining: int | None = None,
    next_eligible_state: str | None = None,
    lifetime_cap: int | None = None,
) -> BackfillResult:
    progress = store.fetch_fixture_progress(store_conn, job_id)
    completed = [row for row in progress if row["state"] == FIXTURE_COMPLETE]
    retryable = [row for row in progress if row["state"] == FIXTURE_RETRYABLE]
    terminal = [row for row in progress if row["state"] == FIXTURE_TERMINAL]
    incomplete = target_fixture_count - len(completed)

    missing_field_counts: dict[str, int] = {}
    for row in progress:
        for field in row.get("missing_fields") or []:
            missing_field_counts[field] = missing_field_counts.get(field, 0) + 1

    manifest = build_backfill_audit_manifest(
        provider=target.provider,
        provider_league_id=target.provider_league_id,
        competition_id=target.competition_id,
        competition_name=target.competition_name,
        season=spec.season,
        source_version=target.source_version,
        fixture_count=target_fixture_count,
        gap_ready_count=len(completed),
        incomplete_count=incomplete,
        request_count=provider_request_count,
        request_cap=spec.max_request_cap,
        projected_total_requests=projected_total_requests,
        planned_requests_this_run=planned_requests_this_run,
        missing_field_counts=missing_field_counts,
        run_state=completion_state,
        retryable_failure_count=len(retryable),
        terminal_failure_count=len(terminal),
        protected_floor=protected_floor,
        historical_headroom=historical_headroom,
        pause_reason=pause_reason,
        requests_consumed_total=requests_consumed_total,
        effective_remaining=effective_remaining,
        next_eligible_state=next_eligible_state,
        lifetime_cap=lifetime_cap,
    )
    logger.info(
        "BACKFILL_TRANCHE job=%s competition=%s season=%s state=%s pause=%s "
        "requests_this_run=%s requests_total=%s completed=%s outstanding=%s "
        "effective_remaining=%s floor=%s headroom=%s lifetime_cap=%s next=%s",
        job_id,
        target.competition_id,
        spec.season,
        completion_state,
        pause_reason,
        provider_request_count,
        requests_consumed_total,
        len(completed),
        incomplete,
        effective_remaining,
        protected_floor,
        historical_headroom,
        lifetime_cap if lifetime_cap is not None else spec.max_request_cap,
        next_eligible_state,
    )
    return BackfillResult(
        job_id=job_id,
        competition_id=target.competition_id,
        competition_name=target.competition_name,
        season=spec.season,
        provider_league_id=target.provider_league_id,
        dry_run=False,
        permitted=True,
        block_reason=None,
        projected_total_requests=projected_total_requests,
        planned_requests_this_run=planned_requests_this_run,
        max_request_cap=spec.max_request_cap,
        target_fixture_count=target_fixture_count,
        completed_count=len(completed),
        gap_ready_count=len(completed),
        incomplete_count=incomplete,
        retryable_failure_count=len(retryable),
        terminal_failure_count=len(terminal),
        provider_request_count=provider_request_count,
        missing_field_counts=missing_field_counts,
        completion_state=completion_state,
        manifest=manifest,
        fixture_progress=tuple(progress),
        pause_reason=pause_reason,
        protected_floor=protected_floor,
        historical_headroom=historical_headroom,
        requests_consumed_total=requests_consumed_total,
        planned_capped_by=planned_capped_by,
        effective_remaining=effective_remaining,
        next_eligible_state=next_eligible_state,
        lifetime_cap=lifetime_cap,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Safe CLI entry point for one controlled league-season backfill.

    Defaults to a zero-provider-request dry run. A real execution requires an
    explicit ``--execute`` flag AND the existing paid-provider env gate
    (``SPORTS_AI_ALLOW_PAID_PROVIDER_CALLS=1``) AND a HEALTHY provider quota
    state, all of which the orchestrator still re-validates before any call.
    """
    import argparse
    import json

    from ..dashboard.fixture_provider import ApiFootballFixtureProvider
    from ..dashboard.provider_budget import ProviderRequestBudget

    parser = argparse.ArgumentParser(
        description="Run (or dry-run) one bounded, resumable API-Football historical backfill."
    )
    parser.add_argument("--competition", required=True, help="Competition id, e.g. FRA1.")
    parser.add_argument("--season", required=True, type=int, help="Season start year, e.g. 2025.")
    parser.add_argument("--max-request-cap", required=True, type=int, help="Hard ceiling on provider requests for one run.")
    parser.add_argument("--db-path", required=True, help="Research/checkpoint SQLite database path.")
    parser.add_argument("--budget-db-path", required=True, help="Operational dashboard SQLite path for the shared request budget.")
    parser.add_argument("--fixture-count-hint", type=int, default=None, help="Known completed fixture count from a prior probe.")
    parser.add_argument("--execute", action="store_true", help="Perform real (budget-gated) provider calls; omit for a dry run.")
    args = parser.parse_args(argv)

    spec = BackfillSpec(args.competition, args.season, args.max_request_cap)

    provider = ApiFootballFixtureProvider()
    budget = ProviderRequestBudget(db_path=args.budget_db_path)
    provider.attach_request_budget(budget)

    conn = store.connect(args.db_path)
    try:
        from ..storage import football_research_store

        football_research_store.ensure_schema(conn)
        store.ensure_schema(conn)
        result = run_backfill(
            provider,
            budget,
            spec=spec,
            store_conn=conn,
            dry_run=not args.execute,
            fixture_count_hint=args.fixture_count_hint,
        )
    finally:
        conn.close()

    logger.info(
        "BACKFILL_RESULT job=%s state=%s pause=%s permitted=%s requests_total=%s "
        "floor=%s headroom=%s completed=%s outstanding=%s",
        result.job_id,
        result.completion_state,
        result.pause_reason,
        result.permitted,
        result.requests_consumed_total,
        result.protected_floor,
        result.historical_headroom,
        result.completed_count,
        result.incomplete_count,
    )
    print(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
