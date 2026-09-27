"""Focused tests for the production-safe API-Football historical backfill.

No live provider calls are made in these tests. The provider and budget are
fake objects; the orchestrator is exercised end-to-end against a temporary
SQLite store.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from sports_ai.data_sources.api_football_backfill import (
    FAILURE_MALFORMED,
    FAILURE_MISSING_STATS,
    FAILURE_NOT_FOUND,
    FAILURE_TRANSIENT,
    FIXTURE_COMPLETE,
    FIXTURE_RETRYABLE,
    FIXTURE_TERMINAL,
    BackfillProviderError,
    BackfillSpec,
    build_backfill_audit_manifest,
    classify_exception,
    evaluate_backfill_eligibility,
    is_retryable_failure,
    is_terminal_failure,
)
from sports_ai.data_sources.api_football_history import (
    estimate_api_football_backfill_requests,
    event_is_gap_ready,
    normalize_api_football_historical_event,
)
from sports_ai.experiments.football_api_history_backfill import (
    completed_target_fixtures,
    run_backfill,
)
from sports_ai.storage import football_backfill_store as backfill_store
from sports_ai.storage import football_research_store
from sports_ai.storage.football_canonical_fixture import canonical_fixture_id_from_identity

FRA1 = "FRA1"


class _Budget:
    def __init__(self, state="HEALTHY", remaining=None, allowed=True):
        self.state = state
        self.remaining = remaining
        self.allowed = allowed

    def quota_state(self):
        return self.state

    def effective_remaining(self):
        return self.remaining

    def allow_paid_provider_calls(self):
        return self.allowed


class _Provider:
    def __init__(self, fixtures, stats_by_id=None, raise_on=None):
        self.fixtures = list(fixtures)
        self.stats = dict(stats_by_id or {})
        self.raise_on = dict(raise_on or {})
        self.calls = []

    def get_historical_league_by_id(self, *, league_id, season):
        self.calls.append(("league", league_id, season))
        return []

    def get_historical_fixtures(self, *, league_id, season):
        self.calls.append(("fixtures", league_id, season))
        return list(self.fixtures)

    def get_historical_fixture_statistics(self, *, provider_fixture_id):
        self.calls.append(("stats", provider_fixture_id))
        if provider_fixture_id in self.raise_on:
            exc = self.raise_on[provider_fixture_id]
            raise exc() if callable(exc) else exc
        return list(self.stats.get(str(provider_fixture_id), []))


def _fixture(fixture_id, date, *, league_id=61, season=2025, status="FT", home="Home", away="Away", home_goals=2, away_goals=1):
    return {
        "fixture": {"id": fixture_id, "date": date, "status": {"short": status}, "referee": "Ref"},
        "league": {"id": league_id, "season": season},
        "teams": {
            "home": {"id": fixture_id * 10, "name": f"{home} {fixture_id}"},
            "away": {"id": fixture_id * 10 + 1, "name": f"{away} {fixture_id}"},
        },
        "goals": {"home": home_goals, "away": away_goals},
        "score": {"halftime": {"home": 1, "away": 0}},
    }


def _statistics(
    fixture,
    *,
    home_shots=13,
    away_shots=9,
    home_sot=5,
    away_sot=3,
    home_corners=6,
    away_corners=4,
    drop=(),
):
    def team_rows(team, shots, sot, corners):
        rows = [
            {"type": "Total Shots", "value": shots},
            {"type": "Shots on Goal", "value": sot},
            {"type": "Corner Kicks", "value": corners},
        ]
        if drop:
            normalized_drop = {d.strip().lower() for d in drop}
            rows = [r for r in rows if r["type"].lower() not in normalized_drop]
        return {"team": team, "statistics": rows}

    return [
        team_rows(fixture["teams"]["home"], home_shots, home_sot, home_corners),
        team_rows(fixture["teams"]["away"], away_shots, away_sot, away_corners),
    ]


def _full_stats(fixtures):
    return {str(f["fixture"]["id"]): _statistics(f) for f in fixtures}


def _store_conn(tmp_path):
    db = tmp_path / "backfill.sqlite"
    conn = backfill_store.connect(db)
    football_research_store.ensure_schema(conn)
    backfill_store.ensure_schema(conn)
    return conn


def _dates(n):
    return [f"2025-08-{i + 1:02d}T15:00:00+00:00" for i in range(n)]


def _fixtures(n, *, league_id=61, season=2025):
    return [_fixture(100 + i, _dates(n)[i], league_id=league_id, season=season) for i in range(n)]


def _spec(cap=100, competition_id=FRA1, season=2025):
    return BackfillSpec(competition_id, season, cap)


# ---------------------------------------------------------------------------
# 1. dry run makes zero provider calls
# ---------------------------------------------------------------------------
def test_dry_run_makes_zero_provider_calls(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(3)
    provider = _Provider(fixtures, _full_stats(fixtures))
    budget = _Budget(state="HEALTHY", remaining=1000, allowed=True)

    result = run_backfill(
        provider,
        budget,
        spec=_spec(cap=100),
        store_conn=conn,
        dry_run=True,
        fixture_count_hint=3,
    )

    assert result.dry_run is True
    assert result.provider_request_count == 0
    assert provider.calls == []
    assert result.completed_count == 0
    assert result.incomplete_count == 3


# ---------------------------------------------------------------------------
# 2. hard request cap
# ---------------------------------------------------------------------------
def test_hard_request_cap_bounds_provider_requests(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(5)
    provider = _Provider(fixtures, _full_stats(fixtures))
    budget = _Budget(state="HEALTHY", remaining=1000, allowed=True)

    result = run_backfill(provider, budget, spec=_spec(cap=3), store_conn=conn)

    stats_calls = [c for c in provider.calls if c[0] == "stats"]
    assert result.provider_request_count == 3
    assert len(stats_calls) == 2
    assert result.completed_count == 2
    assert result.incomplete_count == 3
    assert result.completion_state == "PARTIAL"
    assert result.projected_request_count == 6  # 1 list + 5 stats


def test_invalid_hard_cap_blocks_run(tmp_path):
    conn = _store_conn(tmp_path)
    provider = _Provider(_fixtures(5), {})
    budget = _Budget(state="HEALTHY", remaining=1000, allowed=True)

    result = run_backfill(provider, budget, spec=_spec(cap=0), store_conn=conn, fixture_count_hint=5)

    assert result.permitted is False
    assert result.block_reason == "HARD_CAP_INVALID"
    assert provider.calls == []


# ---------------------------------------------------------------------------
# 3. budget-blocked run makes zero calls
# ---------------------------------------------------------------------------
def test_budget_blocked_run_makes_zero_calls(tmp_path):
    conn = _store_conn(tmp_path)
    provider = _Provider(_fixtures(3), {})
    budget = _Budget(state="CONSERVE", remaining=10, allowed=True)

    result = run_backfill(provider, budget, spec=_spec(cap=100), store_conn=conn, fixture_count_hint=3)

    assert result.permitted is False
    assert result.block_reason == "LOW_PRIORITY_QUOTA_CONSERVE"
    assert provider.calls == []
    assert result.provider_request_count == 0


def test_paid_provider_gate_blocked_makes_zero_calls(tmp_path):
    conn = _store_conn(tmp_path)
    provider = _Provider(_fixtures(3), {})
    budget = _Budget(state="HEALTHY", remaining=1000, allowed=False)

    result = run_backfill(provider, budget, spec=_spec(cap=100), store_conn=conn, fixture_count_hint=3)

    assert result.permitted is False
    assert result.block_reason == "PAID_PROVIDER_CALLS_DISABLED"
    assert provider.calls == []


# ---------------------------------------------------------------------------
# 4. interruption after partial progress resumes remaining
# ---------------------------------------------------------------------------
def test_interruption_after_partial_progress_resumes_remaining(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(5)
    provider1 = _Provider(fixtures, _full_stats(fixtures))
    budget = _Budget(state="HEALTHY", remaining=1000, allowed=True)

    first = run_backfill(provider1, budget, spec=_spec(cap=3), store_conn=conn)
    assert first.completed_count == 2

    provider2 = _Provider(fixtures, _full_stats(fixtures))
    second = run_backfill(provider2, budget, spec=_spec(cap=100), store_conn=conn)

    assert second.completed_count == 5
    # Remaining 3 fixtures are fetched; the 2 already-complete fixtures are NOT repurchased.
    stats_calls = [c[1] for c in provider2.calls if c[0] == "stats"]
    assert sorted(stats_calls) == ["102", "103", "104"]


# ---------------------------------------------------------------------------
# 5. resume skips completed fixture
# ---------------------------------------------------------------------------
def test_resume_skips_completed_fixture(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(3)
    provider1 = _Provider(fixtures, _full_stats(fixtures))
    budget = _Budget(state="HEALTHY", remaining=1000, allowed=True)

    first = run_backfill(provider1, budget, spec=_spec(cap=100), store_conn=conn)
    assert first.completed_count == 3

    provider2 = _Provider(fixtures, _full_stats(fixtures))
    second = run_backfill(provider2, budget, spec=_spec(cap=100), store_conn=conn)

    assert second.completed_count == 3
    assert [c for c in provider2.calls if c[0] == "stats"] == []
    # The fixture list is re-fetched (cheap) but no statistics are repurchased.
    assert [c for c in provider2.calls if c[0] == "fixtures"] == [("fixtures", 61, 2025)]


# ---------------------------------------------------------------------------
# 6. duplicate provider fixture does not duplicate data
# ---------------------------------------------------------------------------
def test_duplicate_provider_fixture_does_not_duplicate_data(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(1)
    provider = _Provider(fixtures, _full_stats(fixtures))
    budget = _Budget(state="HEALTHY", remaining=1000, allowed=True)

    first = run_backfill(provider, budget, spec=_spec(cap=100), store_conn=conn)
    assert first.completed_count == 1

    provider2 = _Provider(fixtures, _full_stats(fixtures))
    second = run_backfill(provider2, budget, spec=_spec(cap=100), store_conn=conn)
    assert second.completed_count == 1

    assert backfill_store.event_row_count(conn) == 1


def test_upsert_event_is_idempotent(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(1)
    fixture = fixtures[0]
    row = normalize_api_football_historical_event(
        fixture,
        _statistics(fixture),
        competition_id=FRA1,
        competition_name="Ligue 1",
        division_code="F1",
        season_code="2526",
    )
    assert row is not None
    assert backfill_store.upsert_backfill_event(conn, row) is True
    assert backfill_store.upsert_backfill_event(conn, row) is False
    assert backfill_store.event_row_count(conn) == 1


# ---------------------------------------------------------------------------
# 7. canonical fixture identity
# ---------------------------------------------------------------------------
def test_canonical_identity_is_provider_independent():
    fixtures = _fixtures(2)
    a = fixtures[0]
    b = {
        **a,
        "fixture": {**a["fixture"], "id": 999999},
    }
    targets_a = completed_target_fixtures([a], competition_id=FRA1, provider_league_id=61, season=2025)
    targets_b = completed_target_fixtures([b], competition_id=FRA1, provider_league_id=61, season=2025)
    assert targets_a[0][0].canonical_fixture_id == targets_b[0][0].canonical_fixture_id
    assert targets_a[0][0].provider_fixture_id != targets_b[0][0].provider_fixture_id


def test_backfill_progress_keyed_by_canonical_id(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(1)
    provider = _Provider(fixtures, _full_stats(fixtures))
    budget = _Budget(state="HEALTHY", remaining=1000, allowed=True)
    result = run_backfill(provider, budget, spec=_spec(cap=100), store_conn=conn)

    progress = backfill_store.fetch_fixture_progress(conn, result.job_id)
    assert len(progress) == 1
    assert progress[0]["canonical_fixture_id"] != progress[0]["provider_fixture_id"]


# ---------------------------------------------------------------------------
# 8-10. missing shots / SOT / corners remain incomplete
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("drop", "expected_field"),
    [
        (("Total Shots",), "home_shots"),
        (("Shots on Goal",), "home_shots_on_target"),
        (("Corner Kicks",), "home_corners"),
    ],
)
def test_missing_required_statistics_remain_incomplete(tmp_path, drop, expected_field):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(1)
    stats = {str(fixtures[0]["fixture"]["id"]): _statistics(fixtures[0], drop=drop)}
    provider = _Provider(fixtures, stats)
    budget = _Budget(state="HEALTHY", remaining=1000, allowed=True)

    result = run_backfill(provider, budget, spec=_spec(cap=100), store_conn=conn)

    assert result.completed_count == 0
    assert result.gap_ready_count == 0
    assert result.terminal_failure_count == 1
    assert result.missing_field_counts[expected_field] == 1
    assert backfill_store.event_row_count(conn) == 0


# ---------------------------------------------------------------------------
# 11. genuine zero is distinct from missing
# ---------------------------------------------------------------------------
def test_genuine_zero_is_distinct_from_missing():
    fixture = _fixtures(1)[0]
    zero_row = normalize_api_football_historical_event(
        fixture,
        _statistics(fixture, home_shots=0, away_shots=0, home_sot=0, away_sot=0, home_corners=0, away_corners=0),
        competition_id=FRA1,
        competition_name="Ligue 1",
        division_code="F1",
        season_code="2526",
    )
    assert zero_row is not None
    assert zero_row.home_shots == 0
    assert event_is_gap_ready(zero_row) is True

    missing_row = normalize_api_football_historical_event(
        fixture,
        _statistics(fixture, drop=("Total Shots",)),
        competition_id=FRA1,
        competition_name="Ligue 1",
        division_code="F1",
        season_code="2526",
    )
    assert missing_row is not None
    assert missing_row.home_shots is None
    assert event_is_gap_ready(missing_row) is False


# ---------------------------------------------------------------------------
# 12. partial fixture not marked complete
# ---------------------------------------------------------------------------
def test_partial_fixture_not_marked_complete(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(2)
    stats = _full_stats(fixtures)
    stats[str(fixtures[1]["fixture"]["id"])] = _statistics(fixtures[1], drop=("Corner Kicks",))
    provider = _Provider(fixtures, stats)
    budget = _Budget(state="HEALTHY", remaining=1000, allowed=True)

    result = run_backfill(provider, budget, spec=_spec(cap=100), store_conn=conn)

    progress = backfill_store.fetch_fixture_progress(conn, result.job_id)
    by_canonical = {row["canonical_fixture_id"]: row for row in progress}
    complete = [row for row in progress if row["state"] == FIXTURE_COMPLETE]
    assert len(complete) == 1
    partial = [row for row in by_canonical.values() if row["state"] != FIXTURE_COMPLETE]
    assert len(partial) == 1
    assert partial[0]["state"] == FIXTURE_TERMINAL
    assert backfill_store.event_row_count(conn) == 1


# ---------------------------------------------------------------------------
# 13. malformed payload
# ---------------------------------------------------------------------------
def test_malformed_payload_is_terminal(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(1)
    provider = _Provider(
        fixtures,
        {},
        raise_on={str(fixtures[0]["fixture"]["id"]): lambda: BackfillProviderError(FAILURE_MALFORMED, "bad payload")},
    )
    budget = _Budget(state="HEALTHY", remaining=1000, allowed=True)

    result = run_backfill(provider, budget, spec=_spec(cap=100), store_conn=conn)

    assert result.completed_count == 0
    assert result.terminal_failure_count == 1
    progress = backfill_store.fetch_fixture_progress(conn, result.job_id)
    assert progress[0]["failure_kind"] == FAILURE_MALFORMED
    assert progress[0]["state"] == FIXTURE_TERMINAL


def test_classify_exception_maps_unknown_to_malformed():
    assert classify_exception(ValueError("bad")) == FAILURE_MALFORMED
    assert classify_exception(ConnectionError("down")) == FAILURE_TRANSIENT
    assert classify_exception(BackfillProviderError(FAILURE_NOT_FOUND, "nope")) == FAILURE_NOT_FOUND


# ---------------------------------------------------------------------------
# 14. transient failure remains retryable
# ---------------------------------------------------------------------------
def test_transient_failure_remains_retryable(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(1)
    provider = _Provider(
        fixtures,
        {},
        raise_on={str(fixtures[0]["fixture"]["id"]): lambda: BackfillProviderError(FAILURE_TRANSIENT, "timeout")},
    )
    budget = _Budget(state="HEALTHY", remaining=1000, allowed=True)

    result = run_backfill(provider, budget, spec=_spec(cap=100), store_conn=conn)

    assert result.retryable_failure_count == 1
    assert result.terminal_failure_count == 0
    progress = backfill_store.fetch_fixture_progress(conn, result.job_id)
    assert progress[0]["state"] == FIXTURE_RETRYABLE
    assert is_retryable_failure(FAILURE_TRANSIENT) is True


# ---------------------------------------------------------------------------
# 15. terminal source failure classification
# ---------------------------------------------------------------------------
def test_terminal_source_failure_classified_terminal(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(1)
    provider = _Provider(
        fixtures,
        {},
        raise_on={str(fixtures[0]["fixture"]["id"]): lambda: BackfillProviderError(FAILURE_NOT_FOUND, "missing")},
    )
    budget = _Budget(state="HEALTHY", remaining=1000, allowed=True)

    result = run_backfill(provider, budget, spec=_spec(cap=100), store_conn=conn)

    assert result.terminal_failure_count == 1
    progress = backfill_store.fetch_fixture_progress(conn, result.job_id)
    assert progress[0]["state"] == FIXTURE_TERMINAL
    assert is_terminal_failure(FAILURE_NOT_FOUND) is True
    assert is_terminal_failure(FAILURE_MISSING_STATS) is True


def test_retry_budget_exhaustion_becomes_terminal(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(1)
    spec = BackfillSpec(FRA1, 2025, 100, max_retries_per_fixture=2)

    for _ in range(2):
        provider = _Provider(
            fixtures,
            {},
            raise_on={str(fixtures[0]["fixture"]["id"]): lambda: BackfillProviderError(FAILURE_TRANSIENT, "timeout")},
        )
        budget = _Budget(state="HEALTHY", remaining=1000, allowed=True)
        run_backfill(provider, budget, spec=spec, store_conn=conn)

    provider3 = _Provider(fixtures, _full_stats(fixtures))
    budget = _Budget(state="HEALTHY", remaining=1000, allowed=True)
    result = run_backfill(provider3, budget, spec=spec, store_conn=conn)

    assert result.terminal_failure_count == 1
    assert result.completed_count == 0
    progress = backfill_store.fetch_fixture_progress(conn, result.job_id)
    assert progress[0]["state"] == FIXTURE_TERMINAL


# ---------------------------------------------------------------------------
# 16. manifest reproducibility
# ---------------------------------------------------------------------------
def test_manifest_reproducible():
    kwargs = dict(
        provider="api-football",
        provider_league_id=61,
        competition_id=FRA1,
        competition_name="Ligue 1",
        season=2025,
        source_version="api_football_history_backfill_v1",
        fixture_count=306,
        gap_ready_count=306,
        incomplete_count=0,
        request_count=307,
        request_cap=320,
        missing_field_counts={},
        run_state="COMPLETE",
    )
    m1 = build_backfill_audit_manifest(**kwargs)
    m2 = build_backfill_audit_manifest(**kwargs)
    assert m1 == m2
    assert m1["content_signature"] == m2["content_signature"]

    m3 = build_backfill_audit_manifest(**{**kwargs, "fixture_count": 307})
    assert m3["content_signature"] != m1["content_signature"]


# ---------------------------------------------------------------------------
# 17. request estimate
# ---------------------------------------------------------------------------
def test_request_estimate_is_fixture_scoped():
    estimate = estimate_api_football_backfill_requests(
        league_id=61,
        season=2025,
        expected_fixture_count=306,
    )
    assert estimate.fixture_list_requests == 1
    assert estimate.fixture_statistics_requests == 306
    assert estimate.total_requests == 307


def test_backfill_projected_request_count_matches_target(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(4)
    provider = _Provider(fixtures, _full_stats(fixtures))
    budget = _Budget(state="HEALTHY", remaining=1000, allowed=True)

    result = run_backfill(provider, budget, spec=_spec(cap=100), store_conn=conn)

    assert result.target_fixture_count == 4
    assert result.projected_request_count == 5
    assert result.provider_request_count == 5


# ---------------------------------------------------------------------------
# 18. FRA1 target cannot expand to another league
# ---------------------------------------------------------------------------
def test_fra1_target_cannot_expand_to_another_league():
    fixtures = [
        _fixture(100, _dates(1)[0], league_id=78, season=2025),  # Bundesliga
        _fixture(101, _dates(2)[0], league_id=61, season=2025),  # Ligue 1
        _fixture(102, _dates(3)[0], league_id=61, season=2024),  # wrong season
    ]
    targets = completed_target_fixtures(fixtures, competition_id=FRA1, provider_league_id=61, season=2025)
    assert len(targets) == 1
    assert targets[0][0].provider_fixture_id == "101"


# ---------------------------------------------------------------------------
# 19. production supported-league list remains unchanged
# ---------------------------------------------------------------------------
def test_production_supported_league_list_unchanged():
    repo_root = Path(__file__).resolve().parents[2]
    research_root = repo_root / "research" / "football_research"
    patterns = {
        "shots": "football_gap_shots_walkforward_fitted_*_manifest.json",
        "shots_on_target": "football_gap_shots_on_target_walkforward_fitted_*_manifest.json",
        "corners": "football_gap_corners_walkforward_fitted_*_manifest.json",
    }
    competition_ids: set[str] = set()
    for pattern in patterns.values():
        manifests = sorted(research_root.glob(pattern))
        assert manifests, f"missing fitted manifest for {pattern}"
        manifest = json.loads(manifests[-1].read_text(encoding="utf-8"))
        for detail in manifest.get("baseline_details") or []:
            competition_ids.add(str(detail["competition_id"]))

    assert competition_ids == {"ENG1", "ENG2", "ENG3", "ENG4", "ESP1"}
    assert "FRA1" not in competition_ids


def test_evaluate_backfill_eligibility_invalid_cap():
    decision = evaluate_backfill_eligibility(
        projected_requests=10,
        max_request_cap=0,
        budget_state="HEALTHY",
        budget_remaining=1000,
    )
    assert decision.permitted is False
    assert decision.reason == "HARD_CAP_INVALID"


def test_evaluate_backfill_eligibility_budget_insufficient():
    decision = evaluate_backfill_eligibility(
        projected_requests=10,
        max_request_cap=100,
        budget_state="HEALTHY",
        budget_remaining=5,
    )
    assert decision.permitted is False
    assert decision.reason == "BUDGET_INSUFFICIENT"
