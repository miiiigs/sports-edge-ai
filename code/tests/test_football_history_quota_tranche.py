"""Quota-window tranche / multi-window resume tests for the FRA1 backfill.

These tests are fully deterministic and hermetic: no provider network traffic is
performed. The provider and the quota governor are local fakes that model the
real governor thresholds (HEALTHY at >= 40% of the effective allowance).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sports_ai.data_sources.api_football_backfill import (
    BACKFILL_PROVIDER,
    FAILURE_MISSING_STATS,
    FAILURE_NOT_FOUND,
    FAILURE_TRANSIENT,
    SOURCE_VERSION,
    BackfillProviderError,
    BackfillSpec,
    backfill_job_id,
    historical_tranche_allowance,
    plan_backfill_tranche,
    protected_historical_floor,
)
from sports_ai.experiments.football_api_history_backfill import run_backfill
from sports_ai.storage import football_backfill_store as backfill_store
from sports_ai.storage import football_research_store

FRA1 = "FRA1"
PROTECTED_FLOOR = 40
FRA1_JOB_ID = "0b52d0d1eb9467b02eba6048"


# ---------------------------------------------------------------------------
# Hermetic fakes
# ---------------------------------------------------------------------------
class _GovernedBudget:
    """Deterministic model of the provider quota governor for tranche tests."""

    HEALTHY_RATIO = 0.40

    def __init__(self, *, limit, remaining, allowed=True, circuit="CLOSED"):
        self.limit = limit
        self.remaining = remaining
        self.allowed = allowed
        self.circuit = circuit

    def allow_paid_provider_calls(self):
        return self.allowed

    def effective_limit(self):
        return self.limit

    def effective_remaining(self):
        return None if self.remaining is None else max(0, int(self.remaining))

    def circuit_state(self):
        return self.circuit

    def quota_state(self):
        if self.remaining is None or not self.limit:
            return "UNKNOWN"
        if self.remaining <= 0:
            return "EXHAUSTED"
        ratio = self.remaining / float(self.limit)
        if ratio < 0.10:
            return "CRITICAL"
        if ratio < 0.20:
            return "LOW"
        if ratio < self.HEALTHY_RATIO:
            return "CONSERVE"
        return "HEALTHY"

    def consume(self, n=1):
        self.remaining = max(0, int(self.remaining) - int(n))


class _TrancheProvider:
    def __init__(self, fixtures, stats_by_id=None, *, budget=None, after_stat=None, raise_on=None):
        self.fixtures = list(fixtures)
        self.stats = dict(stats_by_id or {})
        self.budget = budget
        self.after_stat = after_stat
        self.raise_on = dict(raise_on or {})
        self.calls = []
        self.stat_calls = 0

    def get_historical_league_by_id(self, *, league_id, season):
        self.calls.append(("league", league_id, season))
        return []

    def get_historical_fixtures(self, *, league_id, season):
        self.calls.append(("fixtures", league_id, season))
        if self.budget is not None:
            self.budget.consume(1)
        return list(self.fixtures)

    def get_historical_fixture_statistics(self, *, provider_fixture_id):
        self.calls.append(("stats", provider_fixture_id))
        if provider_fixture_id in self.raise_on:
            exc = self.raise_on[provider_fixture_id]
            raise exc() if callable(exc) else exc
        if self.budget is not None:
            self.budget.consume(1)
        self.stat_calls += 1
        if self.after_stat is not None:
            self.after_stat(self)
        return list(self.stats.get(str(provider_fixture_id), []))


def _fixture(fixture_id, date, *, league_id=61, season=2025, status="FT"):
    return {
        "fixture": {"id": fixture_id, "date": date, "status": {"short": status}, "referee": "Ref"},
        "league": {"id": league_id, "season": season},
        "teams": {
            "home": {"id": fixture_id * 10, "name": f"Home {fixture_id}"},
            "away": {"id": fixture_id * 10 + 1, "name": f"Away {fixture_id}"},
        },
        "goals": {"home": 2, "away": 1},
        "score": {"halftime": {"home": 1, "away": 0}},
    }


def _statistics(fixture):
    def rows(team):
        return {
            "team": team,
            "statistics": [
                {"type": "Total Shots", "value": 13},
                {"type": "Shots on Goal", "value": 5},
                {"type": "Corner Kicks", "value": 6},
            ],
        }

    return [rows(fixture["teams"]["home"]), rows(fixture["teams"]["away"])]


def _dates(n):
    base = datetime(2025, 8, 1, 15, 0, 0, tzinfo=timezone.utc)
    return [(base + timedelta(days=i)).isoformat() for i in range(n)]


def _fixtures(n):
    dates = _dates(n)
    return [_fixture(100 + i, dates[i]) for i in range(n)]


def _full_stats(fixtures):
    return {str(f["fixture"]["id"]): _statistics(f) for f in fixtures}


def _spec(cap, max_retries_per_fixture=3):
    return BackfillSpec(FRA1, 2025, cap, max_retries_per_fixture=max_retries_per_fixture)


def _store_conn(tmp_path):
    conn = backfill_store.connect(tmp_path / "backfill.sqlite")
    football_research_store.ensure_schema(conn)
    backfill_store.ensure_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Pure planner
# ---------------------------------------------------------------------------
def test_protected_floor_and_headroom_math():
    assert protected_historical_floor(100) == 40
    assert protected_historical_floor(7500) == 3000
    assert protected_historical_floor(None) is None
    assert protected_historical_floor(0) is None

    assert historical_tranche_allowance(effective_remaining=97, effective_limit=100) == 57
    assert historical_tranche_allowance(effective_remaining=40, effective_limit=100) == 0
    assert historical_tranche_allowance(effective_remaining=39, effective_limit=100) == 0
    assert historical_tranche_allowance(effective_remaining=None, effective_limit=100) is None


def test_plan_backfill_tranche_bounds_full_plan_cap_and_headroom():
    headroom_plan = plan_backfill_tranche(
        full_plan_requests=309, effective_remaining=97, effective_limit=100, max_request_cap=320
    )
    assert headroom_plan.planned_requests == 57
    assert headroom_plan.protected_floor == 40
    assert headroom_plan.historical_headroom == 57
    assert headroom_plan.capped_by == "HEADROOM"

    full_plan = plan_backfill_tranche(
        full_plan_requests=309, effective_remaining=7500, effective_limit=7500, max_request_cap=320
    )
    assert full_plan.planned_requests == 309
    assert full_plan.capped_by == "FULL_PLAN"

    capped = plan_backfill_tranche(
        full_plan_requests=309,
        effective_remaining=7500,
        effective_limit=7500,
        max_request_cap=320,
        requests_consumed=315,
    )
    assert capped.planned_requests == 5
    assert capped.capped_by == "REQUEST_CAP"


# ---------------------------------------------------------------------------
# 1 / 4. Insufficient full-job quota but safe partial quota (free-like 100/day)
# ---------------------------------------------------------------------------
def test_free_plan_scenario_plans_bounded_tranche(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(308)
    budget = _GovernedBudget(limit=100, remaining=97)
    provider = _TrancheProvider(fixtures, _full_stats(fixtures), budget=budget)

    result = run_backfill(provider, budget, spec=_spec(cap=320), store_conn=conn, fixture_count_hint=308)

    assert result.permitted is True
    assert result.protected_floor == 40
    assert result.historical_headroom == 57
    assert result.planned_requests_this_run == 57
    assert result.provider_request_count == 57  # 1 list + 56 stats
    assert result.completed_count == 56
    assert result.completion_state == "BUDGET_PAUSED"
    assert result.pause_reason == "NO_HISTORICAL_HEADROOM"
    assert result.effective_remaining == PROTECTED_FLOOR
    assert result.next_eligible_state == "AWAITING_QUOTA_RESET"
    assert budget.remaining == PROTECTED_FLOOR  # floor untouched


def test_free_plan_resumes_same_job_across_windows(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(308)
    stats = _full_stats(fixtures)

    job_id = None
    total_stats = 0
    windows = 0
    last_consumed = 0
    while True:
        windows += 1
        assert windows <= 10, "job should complete well within ten windows"
        budget = _GovernedBudget(limit=100, remaining=97)
        provider = _TrancheProvider(fixtures, stats, budget=budget)
        result = run_backfill(provider, budget, spec=_spec(cap=320), store_conn=conn, fixture_count_hint=308)

        assert result.permitted is True
        assert budget.remaining >= PROTECTED_FLOOR  # never breach the floor
        assert result.requests_consumed_total >= last_consumed
        last_consumed = result.requests_consumed_total
        total_stats += result.provider_request_count - 1  # exclude the list request

        if job_id is None:
            job_id = result.job_id
        else:
            assert result.job_id == job_id  # one deterministic logical job

        if result.completion_state == "COMPLETE":
            break
        assert result.completion_state == "BUDGET_PAUSED"
        assert result.pause_reason == "NO_HISTORICAL_HEADROOM"

    assert total_stats == 308  # every fixture fetched exactly once
    assert last_consumed <= 320  # lifetime cap respected across windows
    assert windows >= 6  # genuinely multi-window


# ---------------------------------------------------------------------------
# 2 / 3. Zero headroom and exact healthy-floor boundary
# ---------------------------------------------------------------------------
def test_zero_headroom_blocks_with_zero_calls(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(10)
    budget = _GovernedBudget(limit=100, remaining=40)
    provider = _TrancheProvider(fixtures, _full_stats(fixtures), budget=budget)

    result = run_backfill(provider, budget, spec=_spec(cap=320), store_conn=conn, fixture_count_hint=10)

    assert result.permitted is False
    assert result.block_reason == "NO_HISTORICAL_HEADROOM"
    assert result.completion_state == "BUDGET_PAUSED"
    assert result.provider_request_count == 0
    assert provider.calls == []
    assert budget.remaining == PROTECTED_FLOOR


def test_exact_floor_boundary_allows_only_list_then_pauses(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(10)
    budget = _GovernedBudget(limit=100, remaining=41)  # headroom == 1 (list only)
    provider = _TrancheProvider(fixtures, _full_stats(fixtures), budget=budget)

    result = run_backfill(provider, budget, spec=_spec(cap=320), store_conn=conn, fixture_count_hint=10)

    assert result.provider_request_count == 1  # the fixture list only
    assert result.completed_count == 0
    assert result.completion_state == "BUDGET_PAUSED"
    assert budget.remaining == PROTECTED_FLOOR


# ---------------------------------------------------------------------------
# 5. Paid-like 7500/day case plans the whole outstanding job in one run
# ---------------------------------------------------------------------------
def test_paid_plan_full_job_in_one_window(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(308)
    budget = _GovernedBudget(limit=7500, remaining=7500)
    provider = _TrancheProvider(fixtures, _full_stats(fixtures), budget=budget)

    result = run_backfill(provider, budget, spec=_spec(cap=320), store_conn=conn, fixture_count_hint=308)

    assert result.protected_floor == 3000
    assert result.planned_requests_this_run == 309
    assert result.provider_request_count == 309
    assert result.completed_count == 308
    assert result.completion_state == "COMPLETE"
    assert budget.remaining == 7500 - 309
    assert budget.remaining >= result.protected_floor


# ---------------------------------------------------------------------------
# 6. Mid-run live/current traffic preemption
# ---------------------------------------------------------------------------
def test_mid_run_live_traffic_preemption_pauses_cleanly(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(308)

    def live_traffic(provider):
        # Simulate current/live production draining quota down to the floor.
        if provider.stat_calls == 5:
            provider.budget.remaining = PROTECTED_FLOOR

    budget = _GovernedBudget(limit=100, remaining=97)
    provider = _TrancheProvider(fixtures, _full_stats(fixtures), budget=budget, after_stat=live_traffic)

    result = run_backfill(provider, budget, spec=_spec(cap=320), store_conn=conn, fixture_count_hint=308)

    assert result.completed_count == 5
    assert result.completion_state == "BUDGET_PAUSED"
    assert result.pause_reason == "NO_HISTORICAL_HEADROOM"
    assert budget.remaining == PROTECTED_FLOOR  # live traffic was not raced past
    assert result.provider_request_count == 6  # 1 list + 5 stats, then stopped


# ---------------------------------------------------------------------------
# 7 / 8. Circuit opening mid-run
# ---------------------------------------------------------------------------
def test_circuit_opening_pauses_and_preserves_progress(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(20)
    stats = _full_stats(fixtures)

    def open_circuit(provider):
        if provider.stat_calls == 3:
            provider.budget.circuit = "PROVIDER_ERROR"

    budget = _GovernedBudget(limit=7500, remaining=7500)
    provider = _TrancheProvider(fixtures, stats, budget=budget, after_stat=open_circuit)

    result = run_backfill(provider, budget, spec=_spec(cap=320), store_conn=conn, fixture_count_hint=20)

    assert result.completed_count == 3
    assert result.completion_state == "BUDGET_PAUSED"
    assert result.pause_reason == "PROVIDER_CIRCUIT_OPEN"
    assert result.provider_request_count == 4  # 1 list + 3 stats, then stopped

    progress = backfill_store.fetch_fixture_progress(conn, result.job_id)
    assert sum(1 for row in progress if row["state"] == "COMPLETE") == 3

    # Once the circuit recovers the SAME job resumes and does not repurchase.
    recovered = _GovernedBudget(limit=7500, remaining=7500)
    provider2 = _TrancheProvider(fixtures, stats, budget=recovered)
    resumed = run_backfill(provider2, recovered, spec=_spec(cap=320), store_conn=conn)

    assert resumed.job_id == result.job_id
    assert resumed.completed_count == 20
    assert resumed.completion_state == "COMPLETE"
    assert len([c for c in provider2.calls if c[0] == "stats"]) == 17  # only outstanding


# ---------------------------------------------------------------------------
# 9. Global lifetime request cap across resumes
# ---------------------------------------------------------------------------
def test_global_request_cap_across_resumes(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(308)
    stats = _full_stats(fixtures)

    budget1 = _GovernedBudget(limit=7500, remaining=7500)
    provider1 = _TrancheProvider(fixtures, stats, budget=budget1)
    first = run_backfill(provider1, budget1, spec=_spec(cap=10), store_conn=conn, fixture_count_hint=308)

    assert first.provider_request_count == 10
    assert first.requests_consumed_total == 10
    assert first.completion_state == "LIFETIME_CAP_EXHAUSTED"  # not a budget pause
    assert first.pause_reason == "REQUEST_CAP_EXHAUSTED"
    assert first.next_eligible_state == "REQUIRES_NEW_AUTHORIZATION"

    budget2 = _GovernedBudget(limit=7500, remaining=7500)
    provider2 = _TrancheProvider(fixtures, stats, budget=budget2)
    second = run_backfill(provider2, budget2, spec=_spec(cap=10), store_conn=conn, fixture_count_hint=308)

    assert second.permitted is False
    assert second.block_reason == "REQUEST_CAP_EXHAUSTED"
    assert second.provider_request_count == 0
    assert provider2.calls == []
    assert second.requests_consumed_total == 10  # never exceeds the cap across resumes


# ---------------------------------------------------------------------------
# 10. Duplicate avoidance on resume
# ---------------------------------------------------------------------------
def test_resume_does_not_repurchase_completed_fixtures(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(12)
    stats = _full_stats(fixtures)

    budget1 = _GovernedBudget(limit=100, remaining=48)  # headroom 8 -> list + 7 stats
    provider1 = _TrancheProvider(fixtures, stats, budget=budget1)
    first = run_backfill(provider1, budget1, spec=_spec(cap=320), store_conn=conn, fixture_count_hint=12)
    assert first.completed_count == 7

    budget2 = _GovernedBudget(limit=7500, remaining=7500)
    provider2 = _TrancheProvider(fixtures, stats, budget=budget2)
    second = run_backfill(provider2, budget2, spec=_spec(cap=320), store_conn=conn)

    assert second.job_id == first.job_id
    assert second.completed_count == 12
    second_stats = [c[1] for c in provider2.calls if c[0] == "stats"]
    assert len(second_stats) == 5  # 12 - 7 already complete
    assert backfill_store.event_row_count(conn) == 12  # no duplicate events


# ---------------------------------------------------------------------------
# 11. Deterministic job identity independent of window/tranche size
# ---------------------------------------------------------------------------
def test_job_identity_stable_across_windows_and_tranche_sizes(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(50)
    stats = _full_stats(fixtures)

    small = _GovernedBudget(limit=100, remaining=48)
    p_small = _TrancheProvider(fixtures, stats, budget=small)
    first = run_backfill(p_small, small, spec=_spec(cap=320), store_conn=conn, fixture_count_hint=50)

    large = _GovernedBudget(limit=7500, remaining=7500)
    p_large = _TrancheProvider(fixtures, stats, budget=large)
    second = run_backfill(p_large, large, spec=_spec(cap=320), store_conn=conn, fixture_count_hint=50)

    # A different (larger) cap, window and tranche still resolve the same job.
    other_cap = _GovernedBudget(limit=7500, remaining=7500)
    p_other = _TrancheProvider(fixtures, stats, budget=other_cap)
    third = run_backfill(p_other, other_cap, spec=_spec(cap=999), store_conn=conn, fixture_count_hint=50)

    assert first.job_id == "0b52d0d1eb9467b02eba6048"
    assert first.job_id == second.job_id == third.job_id


# ---------------------------------------------------------------------------
# 12. Retryable failure resumes; 13. terminal failure preserved
# ---------------------------------------------------------------------------
def test_retryable_missing_stats_resumes_on_next_window(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(1)
    fid = str(fixtures[0]["fixture"]["id"])

    budget1 = _GovernedBudget(limit=7500, remaining=7500)
    provider1 = _TrancheProvider(fixtures, {fid: []}, budget=budget1)
    first = run_backfill(provider1, budget1, spec=_spec(cap=320), store_conn=conn)
    assert first.retryable_failure_count == 1
    assert first.completed_count == 0

    budget2 = _GovernedBudget(limit=7500, remaining=7500)
    provider2 = _TrancheProvider(fixtures, _full_stats(fixtures), budget=budget2)
    second = run_backfill(provider2, budget2, spec=_spec(cap=320), store_conn=conn)

    assert second.completed_count == 1
    assert second.completion_state == "COMPLETE"
    assert [c for c in provider2.calls if c[0] == "stats"] == [("stats", fid)]


def test_schema_upgrade_adds_lifetime_counter_idempotently(tmp_path):
    # A checkpoint DB created before lifetime request accounting existed must be
    # upgraded in place: the column is added and the upgrade is idempotent.
    conn = backfill_store.connect(tmp_path / "legacy.sqlite")
    football_research_store.ensure_schema(conn)
    backfill_store.ensure_schema(conn)
    conn.execute("ALTER TABLE football_backfill_jobs DROP COLUMN requests_consumed")
    conn.commit()

    backfill_store.ensure_schema(conn)  # upgrade adds the column back
    columns = {row[1] for row in conn.execute("PRAGMA table_info(football_backfill_jobs)")}
    assert "requests_consumed" in columns

    backfill_store.ensure_schema(conn)  # idempotent, no error
    assert backfill_store.load_job(conn, "missing") is None


def test_zero_in_scope_fixtures_blocks_without_crashing(tmp_path):
    # An empty in-scope target set must return a classified block, never raise.
    conn = _store_conn(tmp_path)
    fixtures = [_fixture(1, _dates(1)[0], league_id=78, season=2025)]  # out of scope
    budget = _GovernedBudget(limit=7500, remaining=7500)
    provider = _TrancheProvider(fixtures, {}, budget=budget)

    result = run_backfill(provider, budget, spec=_spec(cap=320), store_conn=conn)

    assert result.permitted is False
    assert result.block_reason == "NOT_FOUND"
    assert result.provider_request_count == 1  # the fixture list was spent


def test_terminal_failure_is_preserved_and_not_repurchased(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(2)
    terminal_id = str(fixtures[0]["fixture"]["id"])
    stats = _full_stats(fixtures)

    budget1 = _GovernedBudget(limit=7500, remaining=7500)
    provider1 = _TrancheProvider(
        fixtures,
        stats,
        budget=budget1,
        raise_on={terminal_id: lambda: BackfillProviderError(FAILURE_NOT_FOUND, "gone")},
    )
    first = run_backfill(provider1, budget1, spec=_spec(cap=320), store_conn=conn)
    assert first.terminal_failure_count == 1

    budget2 = _GovernedBudget(limit=7500, remaining=7500)
    provider2 = _TrancheProvider(fixtures, stats, budget=budget2)
    run_backfill(provider2, budget2, spec=_spec(cap=320), store_conn=conn)

    called = [c[1] for c in provider2.calls if c[0] == "stats"]
    assert terminal_id not in called  # terminal fixture never repurchased
    progress = {row["canonical_fixture_id"]: row for row in backfill_store.fetch_fixture_progress(conn, first.job_id)}
    assert any(row["state"] == "TERMINAL_FAILED" for row in progress.values())
    assert all(row["failure_kind"] != FAILURE_MISSING_STATS for row in progress.values())


# ---------------------------------------------------------------------------
# C. Resume with a HIGHER CLI cap must not raise the pinned lifetime ceiling
# ---------------------------------------------------------------------------
def test_resume_with_higher_cli_cap_does_not_raise_pinned_cap(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(20)
    stats = _full_stats(fixtures)

    budget1 = _GovernedBudget(limit=7500, remaining=7500)
    first = run_backfill(
        _TrancheProvider(fixtures, stats, budget=budget1),
        budget1,
        spec=_spec(cap=8),
        store_conn=conn,
        fixture_count_hint=20,
    )
    assert first.lifetime_cap == 8
    assert first.requests_consumed_total == 8
    assert first.completion_state == "LIFETIME_CAP_EXHAUSTED"
    assert backfill_store.load_job(conn, first.job_id)["max_request_cap"] == 8

    budget2 = _GovernedBudget(limit=7500, remaining=7500)
    provider2 = _TrancheProvider(fixtures, stats, budget=budget2)
    second = run_backfill(provider2, budget2, spec=_spec(cap=500), store_conn=conn, fixture_count_hint=20)

    assert second.job_id == first.job_id
    assert second.lifetime_cap == 8  # pinned ceiling, not 500
    assert second.permitted is False
    assert second.block_reason == "REQUEST_CAP_EXHAUSTED"
    assert provider2.calls == []  # a higher CLI cap does not authorize more spend
    assert second.requests_consumed_total == 8
    assert backfill_store.load_job(conn, first.job_id)["max_request_cap"] == 8


# ---------------------------------------------------------------------------
# D. Resume with a LOWER CLI cap further restricts this run (min semantics)
# ---------------------------------------------------------------------------
def test_resume_with_lower_cli_cap_uses_min(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(30)
    stats = _full_stats(fixtures)

    # Pin cap=20 but only 8 requests are affordable in the first window.
    budget1 = _GovernedBudget(limit=100, remaining=48)  # headroom == 8
    first = run_backfill(
        _TrancheProvider(fixtures, stats, budget=budget1),
        budget1,
        spec=_spec(cap=20),
        store_conn=conn,
        fixture_count_hint=30,
    )
    assert first.lifetime_cap == 20
    assert first.requests_consumed_total == 8

    # Resume with a LOWER CLI cap: the effective ceiling is min(20, 15) == 15.
    budget2 = _GovernedBudget(limit=7500, remaining=7500)
    second = run_backfill(
        _TrancheProvider(fixtures, stats, budget=budget2),
        budget2,
        spec=_spec(cap=15),
        store_conn=conn,
    )
    assert second.lifetime_cap == 15
    assert second.requests_consumed_total == 15
    assert second.completion_state == "LIFETIME_CAP_EXHAUSTED"
    # The persisted authorization is unchanged; the lower cap only restricted this run.
    assert backfill_store.load_job(conn, first.job_id)["max_request_cap"] == 20


# ---------------------------------------------------------------------------
# E. Cap exhaustion with outstanding RETRYABLE fixtures must not be COMPLETE
# ---------------------------------------------------------------------------
def test_cap_exhaustion_with_retryable_fixtures_not_complete(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(3)
    first_id = str(fixtures[0]["fixture"]["id"])

    budget = _GovernedBudget(limit=7500, remaining=7500)
    provider = _TrancheProvider(fixtures, {first_id: []}, budget=budget)
    result = run_backfill(provider, budget, spec=_spec(cap=2), store_conn=conn, fixture_count_hint=3)

    assert result.completion_state == "LIFETIME_CAP_EXHAUSTED"
    assert result.completion_state != "COMPLETE"
    assert result.pause_reason == "REQUEST_CAP_EXHAUSTED"
    assert result.requests_consumed_total == 2
    assert result.retryable_failure_count == 1
    assert result.incomplete_count == 3
    assert result.next_eligible_state == "REQUIRES_NEW_AUTHORIZATION"


# ---------------------------------------------------------------------------
# F. A retry consumes the final available request; the next request is blocked
# ---------------------------------------------------------------------------
def test_retry_consumes_final_request_and_next_is_blocked(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(1)
    fid = str(fixtures[0]["fixture"]["id"])
    spec = _spec(cap=4)

    b1 = _GovernedBudget(limit=7500, remaining=7500)
    r1 = run_backfill(_TrancheProvider(fixtures, {fid: []}, budget=b1), b1, spec=spec, store_conn=conn)
    assert r1.requests_consumed_total == 2  # list + first attempt
    assert r1.retryable_failure_count == 1

    b2 = _GovernedBudget(limit=7500, remaining=7500)
    r2 = run_backfill(_TrancheProvider(fixtures, {fid: []}, budget=b2), b2, spec=spec, store_conn=conn)
    assert r2.requests_consumed_total == 4  # the retry used the final available requests
    assert r2.retryable_failure_count == 1

    b3 = _GovernedBudget(limit=7500, remaining=7500)
    provider3 = _TrancheProvider(fixtures, {fid: []}, budget=b3)
    r3 = run_backfill(provider3, b3, spec=spec, store_conn=conn)
    assert r3.permitted is False
    assert r3.block_reason == "REQUEST_CAP_EXHAUSTED"
    assert provider3.calls == []
    # The retryable fixture remains accurately represented in the checkpoint.
    progress = {
        row["canonical_fixture_id"]: row
        for row in backfill_store.fetch_fixture_progress(conn, FRA1_JOB_ID)
    }
    assert any(row["state"] == "RETRYABLE_FAILED" for row in progress.values())


# ---------------------------------------------------------------------------
# G. A failed provider call is still accounted against the lifetime cap
# ---------------------------------------------------------------------------
def test_provider_failure_still_accounted_against_lifetime_cap(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(2)
    failing_id = str(fixtures[0]["fixture"]["id"])

    budget = _GovernedBudget(limit=7500, remaining=7500)
    provider = _TrancheProvider(
        fixtures,
        _full_stats(fixtures),
        budget=budget,
        raise_on={failing_id: lambda: BackfillProviderError(FAILURE_TRANSIENT, "boom")},
    )
    result = run_backfill(provider, budget, spec=_spec(cap=320), store_conn=conn)

    # list + 2 statistics attempts (one raised): all three are accounted.
    assert result.requests_consumed_total == 3
    assert result.provider_request_count == 3
    assert result.retryable_failure_count == 1
    assert backfill_store.load_job(conn, result.job_id)["requests_consumed"] == 3


# ---------------------------------------------------------------------------
# H. Durability: checkpoint + lifetime counter survive a connection restart
# ---------------------------------------------------------------------------
def test_checkpoint_and_counter_survive_connection_restart(tmp_path):
    db = tmp_path / "durable.sqlite"
    conn = backfill_store.connect(db)
    football_research_store.ensure_schema(conn)
    backfill_store.ensure_schema(conn)

    fixtures = _fixtures(20)
    stats = _full_stats(fixtures)
    budget1 = _GovernedBudget(limit=100, remaining=48)  # headroom == 8
    first = run_backfill(
        _TrancheProvider(fixtures, stats, budget=budget1),
        budget1,
        spec=_spec(cap=320),
        store_conn=conn,
        fixture_count_hint=20,
    )
    assert first.completed_count == 7
    assert first.requests_consumed_total == 8
    conn.close()  # simulate a process exit / crash

    conn2 = backfill_store.connect(db)
    row = backfill_store.load_job(conn2, first.job_id)
    assert row["requests_consumed"] == 8  # durable, not rolled back
    assert row["max_request_cap"] == 320
    progress = backfill_store.fetch_fixture_progress(conn2, first.job_id)
    assert sum(1 for r in progress if r["state"] == "COMPLETE") == 7

    budget2 = _GovernedBudget(limit=7500, remaining=7500)
    second = run_backfill(
        _TrancheProvider(fixtures, stats, budget=budget2),
        budget2,
        spec=_spec(cap=320),
        store_conn=conn2,
    )
    assert second.job_id == first.job_id
    assert second.completed_count == 20
    conn2.close()


# ---------------------------------------------------------------------------
# I. Concurrent second executor fails closed; a stale claim is reclaimable
# ---------------------------------------------------------------------------
def _seed_in_progress_job(conn, *, marker_iso):
    backfill_store.create_job(
        conn,
        job_id=FRA1_JOB_ID,
        provider=BACKFILL_PROVIDER,
        competition_id=FRA1,
        competition_name="Ligue 1",
        division_code="F1",
        season=2025,
        season_code="2526",
        provider_league_id=61,
        source_version=SOURCE_VERSION,
        required_statistics=("result", "shots", "shots_on_target", "corners"),
        max_request_cap=320,
        max_retries_per_fixture=3,
        projected_total_requests=11,
        target_fixture_count=10,
        start_timestamp=marker_iso,
        completion_state="IN_PROGRESS",
    )


def test_second_executor_fails_closed_while_job_in_progress(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(10)
    _seed_in_progress_job(conn, marker_iso="2026-09-29T00:00:00+00:00")

    budget = _GovernedBudget(limit=7500, remaining=7500)
    provider = _TrancheProvider(fixtures, _full_stats(fixtures), budget=budget)
    now = datetime(2026, 9, 29, 0, 10, 0, tzinfo=timezone.utc)  # 10 min later: still live
    result = run_backfill(provider, budget, spec=_spec(cap=320), store_conn=conn, now=now)

    assert result.permitted is False
    assert result.block_reason == "BACKFILL_ALREADY_RUNNING"
    assert result.requests_consumed_total == 0
    assert provider.calls == []  # no provider traffic from the losing executor
    assert backfill_store.load_job(conn, FRA1_JOB_ID)["completion_state"] == "IN_PROGRESS"


def test_stale_in_progress_job_is_reclaimed(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(10)
    _seed_in_progress_job(conn, marker_iso="2026-09-29T00:00:00+00:00")

    budget = _GovernedBudget(limit=7500, remaining=7500)
    provider = _TrancheProvider(fixtures, _full_stats(fixtures), budget=budget)
    now = datetime(2026, 9, 29, 1, 0, 0, tzinfo=timezone.utc)  # 1h later: lease expired
    result = run_backfill(provider, budget, spec=_spec(cap=320), store_conn=conn, now=now)

    assert result.permitted is True
    assert result.completed_count == 10  # reclaimed after staleness and completed


def test_deterministic_job_id_matches_expected():
    assert (
        backfill_job_id(
            provider=BACKFILL_PROVIDER,
            competition_id=FRA1,
            season=2025,
            provider_league_id=61,
            source_version=SOURCE_VERSION,
        )
        == FRA1_JOB_ID
    )


# ---------------------------------------------------------------------------
# Fixture-list accounting
# ---------------------------------------------------------------------------
def test_fixture_list_counts_against_lifetime_cap(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(5)
    budget = _GovernedBudget(limit=7500, remaining=7500)
    result = run_backfill(
        _TrancheProvider(fixtures, _full_stats(fixtures), budget=budget),
        budget,
        spec=_spec(cap=320),
        store_conn=conn,
    )
    assert result.requests_consumed_total == 6  # 1 list + 5 stats
    assert backfill_store.load_job(conn, result.job_id)["requests_consumed"] == 6


def test_failed_fixture_list_does_not_advance_progress(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(3)
    budget = _GovernedBudget(limit=7500, remaining=7500)
    provider = _TrancheProvider(fixtures, _full_stats(fixtures), budget=budget)

    def _boom(**_kwargs):
        raise BackfillProviderError(FAILURE_TRANSIENT, "list failed")

    provider.get_historical_fixtures = _boom  # type: ignore[method-assign]
    result = run_backfill(provider, budget, spec=_spec(cap=320), store_conn=conn, fixture_count_hint=3)

    assert result.provider_request_count == 1  # the failed list attempt is accounted
    assert result.requests_consumed_total == 1
    assert backfill_store.fetch_fixture_progress(conn, result.job_id) == []


def test_repeated_resumes_cannot_exceed_lifetime_cap(tmp_path):
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(50)
    stats = _full_stats(fixtures)
    cap = 12

    total = 0
    for _ in range(6):
        budget = _GovernedBudget(limit=7500, remaining=7500)
        result = run_backfill(
            _TrancheProvider(fixtures, stats, budget=budget),
            budget,
            spec=_spec(cap=cap),
            store_conn=conn,
            fixture_count_hint=50,
        )
        total = result.requests_consumed_total
        if result.completion_state == "COMPLETE":
            break
    assert total == cap  # exhausted once, never exceeded despite repeated resumes


# ---------------------------------------------------------------------------
# Lease heartbeat
# ---------------------------------------------------------------------------
def test_heartbeat_job_advances_updated_at(tmp_path):
    conn = _store_conn(tmp_path)
    _seed_in_progress_job(conn, marker_iso="2026-09-29T00:00:00+00:00")
    backfill_store.heartbeat_job(conn, job_id=FRA1_JOB_ID, now_iso="2026-09-29T00:45:00+00:00")
    assert backfill_store.load_job(conn, FRA1_JOB_ID)["updated_at"] == "2026-09-29T00:45:00+00:00"


def test_active_run_heartbeat_prevents_reclaim_after_30_minutes(tmp_path):
    # Executor A claims at T0 and keeps making progress; wall clock advances ~10
    # minutes per fixture request. After >30 minutes a second executor B must
    # still be refused, because A's per-request heartbeat keeps updated_at fresh.
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(8)
    stats = _full_stats(fixtures)

    t0 = datetime(2026, 9, 29, 0, 0, 0, tzinfo=timezone.utc)
    state = {"now": t0}

    def clock():
        return state["now"]

    claim_attempts: list[bool] = []

    def advance_and_probe(provider):
        state["now"] = state["now"] + timedelta(minutes=10)
        if provider.stat_calls == 5:  # >30 minutes have elapsed since T0
            now = state["now"]
            stale_before = (now - timedelta(seconds=1800)).isoformat(timespec="seconds")
            claim_attempts.append(
                backfill_store.try_claim_job(
                    conn,
                    job_id=FRA1_JOB_ID,
                    now_iso=now.isoformat(timespec="seconds"),
                    stale_before_iso=stale_before,
                )
            )

    budget = _GovernedBudget(limit=7500, remaining=7500)
    provider = _TrancheProvider(fixtures, stats, budget=budget, after_stat=advance_and_probe)
    result = run_backfill(provider, budget, spec=_spec(cap=320), store_conn=conn, clock=clock)

    assert claim_attempts == [False]  # B could NOT reclaim an actively-progressing job
    assert result.completed_count == 8


def test_run_that_stops_heartbeating_is_reclaimable(tmp_path):
    # Executor A stopped (crashed) with its last heartbeat at T0. Once the lease
    # exceeds JOB_CLAIM_STALE_SECONDS, executor B may safely reclaim the job.
    conn = _store_conn(tmp_path)
    fixtures = _fixtures(10)
    _seed_in_progress_job(conn, marker_iso="2026-09-29T00:00:00+00:00")

    budget = _GovernedBudget(limit=7500, remaining=7500)
    provider = _TrancheProvider(fixtures, _full_stats(fixtures), budget=budget)
    now = datetime(2026, 9, 29, 1, 0, 0, tzinfo=timezone.utc)  # 60 min > 30 min lease
    result = run_backfill(provider, budget, spec=_spec(cap=320), store_conn=conn, now=now)

    assert result.permitted is True
    assert result.completed_count == 10  # reclaimed after staleness and completed


def test_increment_is_committed_before_dispatch(tmp_path):
    # The lifetime counter must already include a request (and be durable on a
    # separate connection) BEFORE the provider call is dispatched.
    conn = _store_conn(tmp_path)
    db_path = tmp_path / "backfill.sqlite"
    fixtures = _fixtures(1)
    observed: list[int] = []

    class _ProbeProvider(_TrancheProvider):
        def get_historical_fixture_statistics(self, *, provider_fixture_id):
            probe = backfill_store.connect(db_path)
            try:
                observed.append(backfill_store.load_job(probe, FRA1_JOB_ID)["requests_consumed"])
            finally:
                probe.close()
            return super().get_historical_fixture_statistics(provider_fixture_id=provider_fixture_id)

    budget = _GovernedBudget(limit=7500, remaining=7500)
    provider = _ProbeProvider(fixtures, _full_stats(fixtures), budget=budget)
    run_backfill(provider, budget, spec=_spec(cap=320), store_conn=conn)

    # The list request (1) plus this statistics request (1) were committed first.
    assert observed == [2]
