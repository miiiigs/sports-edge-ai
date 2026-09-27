"""Bounded, resumable, idempotent API-Football historical backfill orchestrator.

This module is the ONLY place that turns a validated :class:`BackfillSpec` into
provider traffic. It:

* resolves the pinned competition + season to a stable provider league id;
* runs a pre-flight budget/eligibility gate BEFORE any provider call;
* supports a true zero-provider-request dry run;
* always reads the (single) fixture list once per run, then checkpoints
  per-fixture progress so a later resume re-fetches the list but never
  repurchases already-complete statistics;
* writes GAP-ready events idempotently (no duplicate historical events);
* classifies every per-fixture failure as retryable or terminal.

Historical traffic is LOW priority by construction; the provider's own request
budget still enforces the priority on each call.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, Sequence

from ..data_sources.api_football_backfill import (
    FAILURE_MISSING_STATS,
    FAILURE_NORMALIZATION,
    FAILURE_PERSISTENCE,
    FAILURE_QUOTA_BLOCK,
    FAILURE_TERMINAL_GAP,
    FIXTURE_COMPLETE,
    FIXTURE_PENDING,
    FIXTURE_RETRYABLE,
    FIXTURE_TERMINAL,
    JOB_BLOCKED,
    JOB_COMPLETE,
    JOB_IN_PROGRESS,
    JOB_PARTIAL,
    JOB_PENDING,
    BackfillFixtureTarget,
    BackfillSpec,
    BackfillTarget,
    backfill_job_id,
    build_backfill_audit_manifest,
    classify_exception,
    evaluate_backfill_eligibility,
    is_retryable_failure,
    missing_gap_fields,
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


class BackfillProvider(Protocol):
    def get_historical_league_by_id(self, *, league_id: int, season: int) -> list[dict[str, Any]]: ...
    def get_historical_fixtures(self, *, league_id: int, season: int) -> list[dict[str, Any]]: ...
    def get_historical_fixture_statistics(self, *, provider_fixture_id: str) -> list[dict[str, Any]]: ...


class BackfillBudget(Protocol):
    def quota_state(self) -> str: ...
    def effective_remaining(self) -> int | None: ...
    def allow_paid_provider_calls(self) -> bool: ...


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
    projected_request_count: int
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
    projected_request_count: int,
    target_fixture_count: int,
) -> BackfillResult:
    manifest = build_backfill_audit_manifest(
        provider=target.provider,
        provider_league_id=target.provider_league_id,
        competition_id=target.competition_id,
        competition_name=target.competition_name,
        season=spec.season,
        source_version=target.source_version,
        fixture_count=target_fixture_count,
        gap_ready_count=0,
        incomplete_count=target_fixture_count,
        request_count=0,
        request_cap=spec.max_request_cap,
        missing_field_counts={},
        run_state=JOB_BLOCKED,
        dry_run=dry_run,
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
        projected_request_count=projected_request_count,
        max_request_cap=spec.max_request_cap,
        target_fixture_count=target_fixture_count,
        completed_count=0,
        gap_ready_count=0,
        incomplete_count=target_fixture_count,
        retryable_failure_count=0,
        terminal_failure_count=0,
        provider_request_count=0,
        missing_field_counts={},
        completion_state=JOB_BLOCKED,
        manifest=manifest,
    )


def run_backfill(
    provider: BackfillProvider,
    budget: BackfillBudget,
    *,
    spec: BackfillSpec,
    store_conn,
    dry_run: bool = False,
    fixture_count_hint: int | None = None,
    now: datetime | None = None,
) -> BackfillResult:
    """Run (or dry-run) one bounded, resumable league-season backfill."""
    current = now or datetime.now(timezone.utc)
    now_iso = current.isoformat(timespec="seconds")

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
            projected_request_count=0,
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
            projected_request_count=0,
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

    projected = 1 + known_fixture_count
    eligibility = evaluate_backfill_eligibility(
        projected_requests=projected,
        max_request_cap=spec.max_request_cap,
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
            max_request_cap=spec.max_request_cap,
            max_retries_per_fixture=spec.max_retries_per_fixture,
            projected_request_count=projected,
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
            projected_request_count=projected,
            target_fixture_count=known_fixture_count,
        )

    if dry_run:
        completed_count = len(completed_ids)
        incomplete = max(0, known_fixture_count - completed_count)
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
            incomplete_count=incomplete,
            request_count=0,
            request_cap=spec.max_request_cap,
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
            projected_request_count=projected,
            max_request_cap=spec.max_request_cap,
            target_fixture_count=known_fixture_count,
            completed_count=completed_count,
            gap_ready_count=completed_count,
            incomplete_count=incomplete,
            retryable_failure_count=0,
            terminal_failure_count=0,
            provider_request_count=0,
            missing_field_counts={},
            completion_state=run_state,
            manifest=manifest,
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
        max_request_cap=spec.max_request_cap,
        max_retries_per_fixture=spec.max_retries_per_fixture,
        projected_request_count=projected,
        target_fixture_count=known_fixture_count,
        start_timestamp=now_iso,
        completion_state=JOB_IN_PROGRESS,
    )

    try:
        raw_fixtures = provider.get_historical_fixtures(
            league_id=target.provider_league_id,
            season=spec.season,
        )
    except Exception as exc:  # noqa: BLE001
        store.update_job_state(store_conn, job_id=job_id, completion_state=JOB_BLOCKED, updated_at=now_iso)
        return _blocked_result(
            job_id=job_id,
            spec=spec,
            target=target,
            reason=classify_exception(exc),
            dry_run=False,
            projected_request_count=projected,
            target_fixture_count=known_fixture_count,
        )
    if raw_fixtures is None:
        store.update_job_state(store_conn, job_id=job_id, completion_state=JOB_BLOCKED, updated_at=now_iso)
        return _blocked_result(
            job_id=job_id,
            spec=spec,
            target=target,
            reason="MALFORMED_PAYLOAD",
            dry_run=False,
            projected_request_count=projected,
            target_fixture_count=known_fixture_count,
        )

    provider_request_count = 1  # the fixture-list request
    targets = completed_target_fixtures(
        raw_fixtures,
        competition_id=target.competition_id,
        provider_league_id=target.provider_league_id,
        season=spec.season,
    )
    target_fixture_count = len(targets)

    if target_fixture_count == 0:
        store.update_job_state(store_conn, job_id=job_id, completion_state=JOB_BLOCKED, updated_at=now_iso)
        return _blocked_result(
            job_id=job_id,
            spec=spec,
            target=target,
            reason="NOT_FOUND",
            dry_run=False,
            projected_request_count=projected,
            target_fixture_count=0,
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
            retry_count=0,
            event_id=None,
            updated_at=now_iso,
        )

    projected = 1 + target_fixture_count
    post_fetch_eligibility = evaluate_backfill_eligibility(
        projected_requests=projected,
        max_request_cap=spec.max_request_cap,
        budget_state=budget.quota_state(),
        budget_remaining=budget.effective_remaining(),
        paid_provider_calls_allowed=budget.allow_paid_provider_calls(),
    )
    if not post_fetch_eligibility.permitted:
        store.update_job_state(store_conn, job_id=job_id, completion_state=JOB_BLOCKED, updated_at=now_iso)
        manifest = build_backfill_audit_manifest(
            provider=target.provider,
            provider_league_id=target.provider_league_id,
            competition_id=target.competition_id,
            competition_name=target.competition_name,
            season=spec.season,
            source_version=target.source_version,
            fixture_count=target_fixture_count,
            gap_ready_count=len(completed_ids),
            incomplete_count=target_fixture_count - len(completed_ids),
            request_count=provider_request_count,
            request_cap=spec.max_request_cap,
            missing_field_counts={},
            run_state=JOB_BLOCKED,
        )
        return BackfillResult(
            job_id=job_id,
            competition_id=target.competition_id,
            competition_name=target.competition_name,
            season=spec.season,
            provider_league_id=target.provider_league_id,
            dry_run=False,
            permitted=False,
            block_reason=post_fetch_eligibility.reason,
            projected_request_count=projected,
            max_request_cap=spec.max_request_cap,
            target_fixture_count=target_fixture_count,
            completed_count=len(completed_ids),
            gap_ready_count=len(completed_ids),
            incomplete_count=target_fixture_count - len(completed_ids),
            retryable_failure_count=0,
            terminal_failure_count=0,
            provider_request_count=provider_request_count,
            missing_field_counts={},
            completion_state=JOB_BLOCKED,
            manifest=manifest,
        )

    # Refresh progress view after seeding.
    progress_rows = store.fetch_fixture_progress(store_conn, job_id)
    progress_by_canonical = {row["canonical_fixture_id"]: row for row in progress_rows}
    completed_ids = store.completed_canonical_fixture_ids(store_conn, job_id)

    raw_by_provider_id = {tgt.provider_fixture_id: raw for tgt, raw in targets}
    ordered = [
        target_row
        for target_row, _raw in targets
        if target_row.canonical_fixture_id not in completed_ids
        and progress_by_canonical.get(target_row.canonical_fixture_id, {}).get("state") != FIXTURE_TERMINAL
    ]

    missing_field_counts: dict[str, int] = {field: 0 for field in _GAP_FIELDS}

    for target_row in ordered:
        existing = progress_by_canonical.get(target_row.canonical_fixture_id, {})
        retry_count = int(existing.get("retry_count") or 0)

        if retry_count >= spec.max_retries_per_fixture and existing.get("state") == FIXTURE_RETRYABLE:
            _record_failure(
                store_conn,
                job_id=job_id,
                target_row=target_row,
                failure_kind=FAILURE_TERMINAL_GAP,
                message="retry budget exhausted",
                missing_fields=(),
                state=FIXTURE_TERMINAL,
                retry_count=retry_count,
                now_iso=now_iso,
            )
            continue

        if provider_request_count >= spec.max_request_cap:
            break

        provider_request_count += 1
        try:
            statistics = provider.get_historical_fixture_statistics(
                provider_fixture_id=target_row.provider_fixture_id,
            )
        except Exception as exc:  # noqa: BLE001
            kind = classify_exception(exc)
            _record_failure(
                store_conn,
                job_id=job_id,
                target_row=target_row,
                failure_kind=kind,
                message=str(exc),
                missing_fields=(),
                state=FIXTURE_RETRYABLE if is_retryable_failure(kind) else FIXTURE_TERMINAL,
                retry_count=retry_count,
                now_iso=now_iso,
            )
            continue

        if not statistics:
            kind = FAILURE_QUOTA_BLOCK if budget.quota_state() != "HEALTHY" else FAILURE_MISSING_STATS
            _record_failure(
                store_conn,
                job_id=job_id,
                target_row=target_row,
                failure_kind=kind,
                message="provider returned no statistics rows",
                missing_fields=(),
                state=FIXTURE_RETRYABLE if is_retryable_failure(kind) else FIXTURE_TERMINAL,
                retry_count=retry_count,
                now_iso=now_iso,
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
            _record_failure(
                store_conn,
                job_id=job_id,
                target_row=target_row,
                failure_kind=FAILURE_NORMALIZATION,
                message="fixture could not be normalized",
                missing_fields=(),
                state=FIXTURE_TERMINAL,
                retry_count=retry_count,
                now_iso=now_iso,
            )
            continue

        if not event_is_gap_ready(normalized):
            missing = missing_gap_fields(normalized)
            for field in missing:
                missing_field_counts[field] = missing_field_counts.get(field, 0) + 1
            _record_failure(
                store_conn,
                job_id=job_id,
                target_row=target_row,
                failure_kind=FAILURE_MISSING_STATS,
                message="required GAP statistics missing",
                missing_fields=missing,
                state=FIXTURE_TERMINAL,
                retry_count=retry_count,
                now_iso=now_iso,
            )
            continue

        try:
            store.upsert_backfill_event(store_conn, normalized)
        except Exception as exc:  # noqa: BLE001
            _record_failure(
                store_conn,
                job_id=job_id,
                target_row=target_row,
                failure_kind=FAILURE_PERSISTENCE,
                message=str(exc),
                missing_fields=(),
                state=FIXTURE_RETRYABLE,
                retry_count=retry_count,
                now_iso=now_iso,
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
            retry_count=retry_count,
            event_id=normalized.event_id,
            updated_at=now_iso,
        )

    _finalize(store_conn, job_id=job_id, target_fixture_count=target_fixture_count, now_iso=now_iso)
    return _summarize(
        store_conn,
        job_id=job_id,
        target=target,
        spec=spec,
        target_fixture_count=target_fixture_count,
        projected=projected,
        provider_request_count=provider_request_count,
        now_iso=now_iso,
    )


def _record_failure(
    store_conn,
    *,
    job_id: str,
    target_row: BackfillFixtureTarget,
    failure_kind: str,
    message: str | None,
    missing_fields: Sequence[str],
    state: str,
    retry_count: int,
    now_iso: str,
) -> None:
    store.upsert_fixture_progress(
        store_conn,
        job_id=job_id,
        canonical_fixture_id=target_row.canonical_fixture_id,
        provider_fixture_id=target_row.provider_fixture_id,
        match_date=target_row.match_date,
        home_team=target_row.home_team,
        away_team=target_row.away_team,
        state=state,
        failure_kind=failure_kind,
        missing_fields=tuple(missing_fields),
        retry_count=int(retry_count) + (1 if state == FIXTURE_RETRYABLE else 0),
        event_id=None,
        updated_at=now_iso,
    )
    store.record_failure(
        store_conn,
        job_id=job_id,
        canonical_fixture_id=target_row.canonical_fixture_id,
        provider_fixture_id=target_row.provider_fixture_id,
        failure_kind=failure_kind,
        message=message,
        recorded_at=now_iso,
    )


def _finalize(store_conn, *, job_id: str, target_fixture_count: int, now_iso: str) -> None:
    progress = store.fetch_fixture_progress(store_conn, job_id)
    completed = sum(1 for row in progress if row["state"] == FIXTURE_COMPLETE)
    if target_fixture_count > 0 and completed == target_fixture_count:
        store.update_job_state(store_conn, job_id=job_id, completion_state=JOB_COMPLETE, updated_at=now_iso)
    else:
        store.update_job_state(store_conn, job_id=job_id, completion_state=JOB_PARTIAL, updated_at=now_iso)


def _summarize(
    store_conn,
    *,
    job_id: str,
    target: BackfillTarget,
    spec: BackfillSpec,
    target_fixture_count: int,
    projected: int,
    provider_request_count: int,
    now_iso: str,
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

    run_state = (
        JOB_COMPLETE if target_fixture_count > 0 and len(completed) == target_fixture_count else JOB_PARTIAL
    )
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
        missing_field_counts=missing_field_counts,
        run_state=run_state,
        retryable_failure_count=len(retryable),
        terminal_failure_count=len(terminal),
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
        projected_request_count=projected,
        max_request_cap=spec.max_request_cap,
        target_fixture_count=target_fixture_count,
        completed_count=len(completed),
        gap_ready_count=len(completed),
        incomplete_count=incomplete,
        retryable_failure_count=len(retryable),
        terminal_failure_count=len(terminal),
        provider_request_count=provider_request_count,
        missing_field_counts=missing_field_counts,
        completion_state=run_state,
        manifest=manifest,
        fixture_progress=tuple(progress),
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

    print(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

