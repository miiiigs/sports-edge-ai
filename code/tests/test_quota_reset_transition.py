"""Regression tests for the daily quota-reset transition deadlock.

Production entered QUOTA_EXHAUSTED before the 00:00 UTC reset. A stale
previous-day ``daily_remaining`` (e.g. 299) kept the governor in CRITICAL while
the expired circuit reopened, so NORMAL today's-schedule discovery stayed
blocked forever. These tests prove the reset transition invalidates previous-day
provider quota values, reopens the expired circuit, and lets exactly one
controlled NORMAL request probe today's fresh provider headers.

No live API-Football HTTP calls are made in these tests; provider HTTP is only
counted through the budget's own ``http_requests`` metric.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sports_ai.dashboard.provider_budget import (
    ACTION_ALLOW,
    ACTION_BLOCKED_RESET_PROBE,
    ACTION_BLOCKED_QUOTA,
    CIRCUIT_QUOTA_EXHAUSTED,
    PRIORITY_ESSENTIAL,
    PRIORITY_LOW,
    PRIORITY_NORMAL,
    PROVIDER_API_FOOTBALL,
    ProviderRequestBudget,
    parse_iso_datetime,
)


def _budget(tmp_path: Path, *, allow: bool = True, **kwargs) -> ProviderRequestBudget:
    return ProviderRequestBudget(
        tmp_path / "dashboard.sqlite",
        allow_paid_provider_calls=allow,
        **kwargs,
    )


def _past(seconds: int = 60) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _future(seconds: int = 60) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _seed_snapshot(
    budget: ProviderRequestBudget,
    *,
    daily_limit: int | None,
    daily_remaining: int | None,
    minute_limit: int | None = None,
    minute_remaining: int | None = None,
    quota_reset_at: str,
    operational_used: int = 0,
    operational_cap: int | None = None,
    operational_reset_at: str | None = None,
    requests_used: int = 0,
) -> None:
    snapshot = {
        "provider": PROVIDER_API_FOOTBALL,
        "daily_limit": daily_limit,
        "daily_remaining": daily_remaining,
        "minute_limit": minute_limit,
        "minute_remaining": minute_remaining,
        "captured_at": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds"),
        "quota_reset_at": quota_reset_at,
        "operational_used": operational_used,
        "operational_cap": operational_cap,
        "operational_reset_at": operational_reset_at or quota_reset_at,
        "requests_used": requests_used,
        "last_request_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds"),
    }
    with budget._connect() as conn:
        conn.execute(
            """
            INSERT INTO provider_quota_state (provider, state_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at
            """,
            (PROVIDER_API_FOOTBALL, json.dumps(snapshot, sort_keys=True), datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )


def _seed_circuit(
    budget: ProviderRequestBudget,
    *,
    state: str,
    blocked_until: str | None,
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with budget._connect() as conn:
        conn.execute(
            """
            INSERT INTO provider_circuit_state (provider, current_state, failure_count, blocked_until, last_error, last_error_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
                current_state = excluded.current_state,
                failure_count = excluded.failure_count,
                blocked_until = excluded.blocked_until,
                last_error = excluded.last_error,
                last_error_at = excluded.last_error_at,
                updated_at = excluded.updated_at
            """,
            (PROVIDER_API_FOOTBALL, state, 0, blocked_until, "daily request limit reached", now_iso, now_iso),
        )


def _fresh_headers() -> dict[str, str]:
    return {
        "x-ratelimit-requests-limit": "7500",
        "x-ratelimit-requests-remaining": "7499",
        "x-ratelimit-limit": "300",
        "x-ratelimit-remaining": "299",
    }


def _today_params() -> dict[str, object]:
    today = datetime.now(timezone.utc).astimezone().date().isoformat()
    return {"date": today, "league": 39}


# =============================================================== MAIN FAILURE CASE
def test_post_reset_stale_remaining_no_longer_blocks_normal_schedule(tmp_path):
    """The exact production failure: stale 299 remaining must not produce CRITICAL.

    Starts with:
      daily_limit=7500 daily_remaining=299 quota_reset_at=past
      circuit=QUOTA_EXHAUSTED blocked_until=past
      operational_used=1 operational_cap=4500
    After the clock crosses reset: circuit CLOSED, operational_used=0, the stale
    299 no longer governs, the first NORMAL request is allowed, and a fresh
    provider response re-establishes HEALTHY state. No restart required.
    """
    past = _past()
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_circuit(budget, state=CIRCUIT_QUOTA_EXHAUSTED, blocked_until=past)
    _seed_snapshot(
        budget,
        daily_limit=7500,
        daily_remaining=299,
        minute_limit=300,
        minute_remaining=0,
        quota_reset_at=past,
        operational_used=1,
        operational_cap=4500,
        operational_reset_at=past,
        requests_used=4500,
    )

    decision = budget.issue(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params=_today_params(),
        ttl_seconds=3600,
        priority=PRIORITY_NORMAL,
    )
    assert decision.action == ACTION_ALLOW
    assert budget.circuit_snapshot()["state"] == "CLOSED"

    snapshot = budget.quota_snapshot()
    assert int(snapshot["operational_used"] or 0) == 0
    assert snapshot["state"] == "UNKNOWN"
    assert snapshot.get("daily_remaining") is None
    assert snapshot.get("daily_limit") is None

    # The allowed request returns fresh provider headers and the governor
    # immediately resumes normal behavior.
    budget.finish_success(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params=_today_params(),
        payload={"response": [], "results": 0, "errors": {}},
        provider_timestamp=None,
        ttl_seconds=3600,
        response_headers=_fresh_headers(),
    )
    snapshot = budget.quota_snapshot()
    assert snapshot["daily_limit"] == 7500
    assert snapshot["daily_remaining"] == 7499
    assert snapshot["minute_limit"] == 300
    assert snapshot["minute_remaining"] == 299
    assert budget.quota_state() == "HEALTHY"
    assert budget.circuit_snapshot()["state"] == "CLOSED"
    assert budget.metrics()["http_requests"] == 1


# =========================================================== STALE VALUE INVALIDATION
def test_stale_nonzero_remaining_invalidated_after_reset(tmp_path):
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_snapshot(
        budget,
        daily_limit=7500,
        daily_remaining=299,
        minute_limit=300,
        minute_remaining=0,
        quota_reset_at=_past(),
        operational_used=10,
        operational_cap=4500,
        operational_reset_at=_past(),
    )
    assert budget.quota_state() == "UNKNOWN"
    snapshot = budget.quota_snapshot()
    assert snapshot.get("daily_remaining") is None
    assert snapshot.get("daily_limit") is None
    assert parse_iso_datetime(snapshot["quota_reset_at"]) > datetime.now(timezone.utc)


def test_stale_zero_remaining_invalidated_after_reset(tmp_path):
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_snapshot(
        budget,
        daily_limit=7500,
        daily_remaining=0,
        minute_limit=300,
        minute_remaining=0,
        quota_reset_at=_past(),
        operational_used=4500,
        operational_cap=4500,
        operational_reset_at=_past(),
    )
    assert budget.quota_state() == "UNKNOWN"
    snapshot = budget.quota_snapshot()
    assert snapshot.get("daily_remaining") is None
    assert int(snapshot["operational_used"] or 0) == 0


def test_future_quota_reset_does_not_clear_current_state(tmp_path):
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_snapshot(
        budget,
        daily_limit=7500,
        daily_remaining=4000,
        minute_limit=300,
        minute_remaining=200,
        quota_reset_at=_future(3600),
        operational_used=100,
        operational_cap=4500,
        operational_reset_at=_future(3600),
    )
    assert budget.quota_state() == "HEALTHY"
    snapshot = budget.quota_snapshot()
    assert snapshot["daily_remaining"] == 4000
    assert snapshot["daily_limit"] == 7500
    assert int(snapshot["operational_used"] or 0) == 100


# ================================================================ OPERATIONAL CAP
def test_operational_used_resets_to_zero_at_boundary(tmp_path):
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_snapshot(
        budget,
        daily_limit=7500,
        daily_remaining=7500,
        minute_limit=300,
        minute_remaining=300,
        quota_reset_at=_future(3600),
        operational_used=4500,
        operational_cap=4500,
        operational_reset_at=_past(),
    )
    assert int(budget.quota_snapshot()["operational_used"] or 0) == 0
    assert budget.quota_state() != "EXHAUSTED"


def test_operational_4500_hard_cap_remains_active(tmp_path):
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_snapshot(
        budget,
        daily_limit=7500,
        daily_remaining=7499,
        minute_limit=300,
        minute_remaining=299,
        quota_reset_at=_future(3600),
        operational_used=4500,
        operational_cap=4500,
        operational_reset_at=_future(3600),
    )
    assert budget.snapshot()["quota"]["operational_cap"] == 4500
    assert budget.quota_state() == "EXHAUSTED"
    decision = budget.issue(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params={"live": "all"},
        ttl_seconds=90,
        priority=PRIORITY_ESSENTIAL,
    )
    assert decision.action == ACTION_BLOCKED_QUOTA


# ================================================================ SINGLE PROBE
def test_one_post_reset_request_establishes_fresh_provider_headers(tmp_path):
    past = _past()
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_circuit(budget, state=CIRCUIT_QUOTA_EXHAUSTED, blocked_until=past)
    _seed_snapshot(
        budget,
        daily_limit=7500,
        daily_remaining=299,
        minute_limit=300,
        minute_remaining=0,
        quota_reset_at=past,
        operational_used=1,
        operational_cap=4500,
        operational_reset_at=past,
    )
    first = budget.issue(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params=_today_params(),
        ttl_seconds=3600,
        priority=PRIORITY_NORMAL,
    )
    assert first.action == ACTION_ALLOW
    budget.finish_success(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params=_today_params(),
        payload={"response": [], "results": 0, "errors": {}},
        provider_timestamp=None,
        ttl_seconds=3600,
        response_headers=_fresh_headers(),
    )
    snapshot = budget.quota_snapshot()
    assert snapshot["daily_remaining"] == 7499
    assert budget.quota_state() == "HEALTHY"
    assert budget.metrics()["http_requests"] == 1


def test_simultaneous_scheduler_paths_do_not_create_a_request_burst(tmp_path):
    """Concurrent owners across keys coalesce onto the single probe slot."""
    past = _past()
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_circuit(budget, state=CIRCUIT_QUOTA_EXHAUSTED, blocked_until=past)
    _seed_snapshot(
        budget,
        daily_limit=7500,
        daily_remaining=299,
        minute_limit=300,
        minute_remaining=0,
        quota_reset_at=past,
        operational_used=1,
        operational_cap=4500,
        operational_reset_at=past,
    )
    # First request becomes the single post-reset probe owner.
    probe = budget.issue(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params={"live": "all"},
        ttl_seconds=90,
        priority=PRIORITY_ESSENTIAL,
    )
    assert probe.action == ACTION_ALLOW
    # A second scheduler path for a different key must be deferred, not allowed.
    second = budget.issue(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params=_today_params(),
        ttl_seconds=3600,
        priority=PRIORITY_NORMAL,
    )
    assert second.action == ACTION_BLOCKED_RESET_PROBE
    # A third concurrent path is deferred too - no burst.
    third = budget.issue(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params={"date": "2026-08-30", "league": 140},
        ttl_seconds=3600,
        priority=PRIORITY_NORMAL,
    )
    assert third.action == ACTION_BLOCKED_RESET_PROBE
    assert budget.metrics().get("http_requests", 0) == 0

    # The probe completes and captures today's headers.
    budget.finish_success(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params={"live": "all"},
        payload={"response": [], "results": 0, "errors": {}},
        provider_timestamp=None,
        ttl_seconds=90,
        response_headers=_fresh_headers(),
    )
    assert budget.metrics()["http_requests"] == 1
    # Once fresh state is established, normal scheduler work resumes.
    resumed = budget.issue(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params=_today_params(),
        ttl_seconds=3600,
        priority=PRIORITY_NORMAL,
    )
    assert resumed.action == ACTION_ALLOW


# ================================================================ PRIORITY BEHAVIOR
def test_low_far_future_work_deferred_while_probe_pending(tmp_path):
    past = _past()
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_circuit(budget, state=CIRCUIT_QUOTA_EXHAUSTED, blocked_until=past)
    _seed_snapshot(
        budget,
        daily_limit=7500,
        daily_remaining=299,
        minute_limit=300,
        minute_remaining=0,
        quota_reset_at=past,
        operational_used=1,
        operational_cap=4500,
        operational_reset_at=past,
    )
    far_future = (datetime.now(timezone.utc).date() + timedelta(days=3)).isoformat()
    low = budget.issue(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params={"date": far_future, "league": 39},
        ttl_seconds=8 * 3600,
        priority=PRIORITY_LOW,
    )
    assert low.action == ACTION_BLOCKED_QUOTA
    # NORMAL today's work is eligible while the probe is pending.
    normal = budget.issue(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params=_today_params(),
        ttl_seconds=3600,
        priority=PRIORITY_NORMAL,
    )
    assert normal.action == ACTION_ALLOW


def test_second_request_uses_freshly_captured_governor_state(tmp_path):
    past = _past()
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_circuit(budget, state=CIRCUIT_QUOTA_EXHAUSTED, blocked_until=past)
    _seed_snapshot(
        budget,
        daily_limit=7500,
        daily_remaining=299,
        minute_limit=300,
        minute_remaining=0,
        quota_reset_at=past,
        operational_used=1,
        operational_cap=4500,
        operational_reset_at=past,
    )
    assert budget.issue(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params=_today_params(),
        ttl_seconds=3600,
        priority=PRIORITY_NORMAL,
    ).action == ACTION_ALLOW
    budget.finish_success(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params=_today_params(),
        payload={"response": [], "results": 0, "errors": {}},
        provider_timestamp=None,
        ttl_seconds=3600,
        response_headers=_fresh_headers(),
    )
    assert budget.quota_state() == "HEALTHY"
    second = budget.issue(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params={"date": _today_params()["date"], "league": 140},
        ttl_seconds=3600,
        priority=PRIORITY_NORMAL,
    )
    assert second.action == ACTION_ALLOW
    assert second.quota_state == "HEALTHY"


# ================================================================ RESTART / NEXT DAY
def test_restart_after_reset_behaves_consistently(tmp_path):
    past = _past()
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_circuit(budget, state=CIRCUIT_QUOTA_EXHAUSTED, blocked_until=past)
    _seed_snapshot(
        budget,
        daily_limit=7500,
        daily_remaining=299,
        minute_limit=300,
        minute_remaining=0,
        quota_reset_at=past,
        operational_used=1,
        operational_cap=4500,
        operational_reset_at=past,
    )
    # A brand-new process instance on the same DB (restart/redeploy).
    budget2 = _budget(tmp_path, operational_daily_cap=4500)
    assert budget2.circuit_snapshot()["state"] == CIRCUIT_QUOTA_EXHAUSTED
    decision = budget2.issue(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params=_today_params(),
        ttl_seconds=3600,
        priority=PRIORITY_NORMAL,
    )
    assert decision.action == ACTION_ALLOW
    assert budget2.circuit_snapshot()["state"] == "CLOSED"
    budget2.finish_success(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params=_today_params(),
        payload={"response": [], "results": 0, "errors": {}},
        provider_timestamp=None,
        ttl_seconds=3600,
        response_headers=_fresh_headers(),
    )
    assert budget2.quota_state() == "HEALTHY"
    # And the freshly captured state survives another restart.
    budget3 = _budget(tmp_path, operational_daily_cap=4500)
    assert budget3.quota_state() == "HEALTHY"
    decision3 = budget3.issue(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params=_today_params(),
        ttl_seconds=3600,
        priority=PRIORITY_NORMAL,
    )
    # The persistent cache serves the already-captured response; either way the
    # restarted process is not blocked by any stale reset state.
    assert decision3.action in {ACTION_ALLOW, "CACHE_HIT"}


def test_next_day_reset_also_works(tmp_path):
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_snapshot(
        budget,
        daily_limit=7500,
        daily_remaining=7499,
        minute_limit=300,
        minute_remaining=299,
        quota_reset_at=_future(3600),
        operational_used=500,
        operational_cap=4500,
        operational_reset_at=_future(3600),
    )
    assert budget.quota_state() == "HEALTHY"
    # Simulate the next day's reset crossing: boundary now in the past.
    past = _past()
    _seed_snapshot(
        budget,
        daily_limit=7500,
        daily_remaining=7499,
        minute_limit=300,
        minute_remaining=299,
        quota_reset_at=past,
        operational_used=500,
        operational_cap=4500,
        operational_reset_at=past,
    )
    assert budget.quota_state() == "UNKNOWN"
    snapshot = budget.quota_snapshot()
    assert snapshot.get("daily_remaining") is None
    assert parse_iso_datetime(snapshot["quota_reset_at"]) > datetime.now(timezone.utc)
    assert int(snapshot["operational_used"] or 0) == 0
    assert budget.issue(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params=_today_params(),
        ttl_seconds=3600,
        priority=PRIORITY_NORMAL,
    ).action == ACTION_ALLOW


def test_no_provider_http_call_occurs_in_unit_tests(tmp_path):
    """All unit-level proofs run against the budget only; no HTTP is attempted."""
    past = _past()
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_circuit(budget, state=CIRCUIT_QUOTA_EXHAUSTED, blocked_until=past)
    _seed_snapshot(
        budget,
        daily_limit=7500,
        daily_remaining=299,
        minute_limit=300,
        minute_remaining=0,
        quota_reset_at=past,
        operational_used=1,
        operational_cap=4500,
        operational_reset_at=past,
    )
    assert budget.issue(
        provider=PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params=_today_params(),
        ttl_seconds=3600,
        priority=PRIORITY_NORMAL,
    ).action == ACTION_ALLOW
    # Only the explicit finish_success in tests ever counts an http request.
    assert budget.metrics().get("http_requests", 0) == 0
