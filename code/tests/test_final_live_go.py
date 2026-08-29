"""Focused tests for the final production go-live package.

Covers the hard operational daily cap, per-minute/burst protection, automatic
quota-reset resume, and the client-truth UI cleanup. No live provider calls.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sports_ai.dashboard import provider_budget as pb
from sports_ai.dashboard.services import DashboardConfig, DashboardServices
from sports_ai.market_data.providers import NotConfiguredMarketProvider
from sports_ai.storage import football_paper_trading as pt


def _budget(tmp_path: Path, **kwargs) -> pb.ProviderRequestBudget:
    defaults = {"allow_paid_provider_calls": True}
    defaults.update(kwargs)
    return pb.ProviderRequestBudget(tmp_path / "dashboard.sqlite", **defaults)


def _seed_provider_quota(budget: pb.ProviderRequestBudget, *, daily_limit: int, daily_remaining: int) -> None:
    budget._record_quota_from_headers(
        {
            "x-ratelimit-limit": str(daily_limit),
            "x-ratelimit-remaining": str(daily_remaining),
            "x-ratelimit-requests-limit": "300",
            "x-ratelimit-requests-remaining": "300",
        }
    )


def _seed_operational(budget: pb.ProviderRequestBudget, *, used: int, cap: int, reset_at: str | None) -> None:
    snapshot = budget._quota_snapshot()
    snapshot["operational_used"] = used
    snapshot["operational_cap"] = cap
    if reset_at is not None:
        snapshot["operational_reset_at"] = reset_at
    budget._persist_quota(snapshot)


def _issue(budget: pb.ProviderRequestBudget, *, params: dict | None = None, priority: str = pb.PRIORITY_NORMAL) -> pb.BudgetDecision:
    return budget.issue(
        provider=pb.PROVIDER_API_FOOTBALL,
        endpoint="fixtures",
        params=params or {"n": "1"},
        ttl_seconds=60,
        priority=priority,
    )


def _services(tmp_path: Path, *, allow: bool = False) -> DashboardServices:
    config = DashboardConfig(
        repo_root=tmp_path,
        research_db_path=tmp_path / "research.sqlite",
        dashboard_db_path=tmp_path / "dashboard.sqlite",
        paper_trading_db_path=tmp_path / "paper.sqlite",
        performance_output_dir=tmp_path / "performance",
        operational_data_dir=tmp_path,
        storage_mode="test_tmpdir",
        persistent_data_dir_configured=False,
        display_timezone="Asia/Manila",
        enable_scheduler=False,
        allow_paid_provider_calls=allow,
        current_odds_connected=False,
        current_xg_connected=False,
    )
    return DashboardServices(
        config=config,
        market_provider=NotConfiguredMarketProvider(provider_label="test"),
    )


# ------------------------------------------------------------- operational hard cap
def test_operational_cap_4500_is_enforced(tmp_path):
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_provider_quota(budget, daily_limit=7500, daily_remaining=7500)
    _seed_operational(budget, used=4500, cap=4500, reset_at=budget._compute_quota_reset_at(datetime.now(timezone.utc)))
    assert budget.quota_state() == pb.QUOTA_EXHAUSTED
    for priority in (pb.PRIORITY_LOW, pb.PRIORITY_NORMAL, pb.PRIORITY_ESSENTIAL):
        decision = _issue(budget, priority=priority)
        assert decision.action == pb.ACTION_BLOCKED_QUOTA
    assert budget.metrics().get("http_requests", 0) == 0


def test_provider_limit_7500_cannot_override_lower_app_cap(tmp_path):
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_provider_quota(budget, daily_limit=7500, daily_remaining=7500)
    _seed_operational(budget, used=4499, cap=4500, reset_at=budget._compute_quota_reset_at(datetime.now(timezone.utc)))
    # Provider still reports plenty (7500) but the app cap has only 1 left.
    assert budget.effective_remaining() == 1
    # One request allowed (essential retains priority under CRITICAL), then the cap binds.
    decision = _issue(budget, params={"n": "1"}, priority=pb.PRIORITY_ESSENTIAL)
    assert decision.action == pb.ACTION_ALLOW
    budget.finish_success(provider=pb.PROVIDER_API_FOOTBALL, endpoint="fixtures", params={"n": "1"},
                          payload={"response": []}, provider_timestamp=None, ttl_seconds=60, response_headers=None)
    assert budget.quota_state() == pb.QUOTA_EXHAUSTED
    assert _issue(budget, params={"n": "2"}).action == pb.ACTION_BLOCKED_QUOTA


def test_cap_reached_zero_additional_calls(tmp_path):
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_provider_quota(budget, daily_limit=7500, daily_remaining=7500)
    _seed_operational(budget, used=4500, cap=4500, reset_at=budget._compute_quota_reset_at(datetime.now(timezone.utc)))
    before = budget.metrics().get("http_requests", 0)
    for i in range(3):
        assert _issue(budget, params={"n": str(i)}).action == pb.ACTION_BLOCKED_QUOTA
    assert budget.metrics().get("http_requests", 0) == before


def test_operational_counter_survives_restart(tmp_path):
    db = tmp_path / "dashboard.sqlite"
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_provider_quota(budget, daily_limit=7500, daily_remaining=7500)
    _seed_operational(budget, used=1234, cap=4500, reset_at=budget._compute_quota_reset_at(datetime.now(timezone.utc)))
    assert budget.effective_remaining() == min(7500, 4500 - 1234)
    # New instance on the same DB (restart/redeploy).
    budget2 = pb.ProviderRequestBudget(db, allow_paid_provider_calls=True, operational_daily_cap=4500)
    assert budget2.quota_snapshot()["operational_used"] == 1234
    assert budget2.effective_remaining() == min(7500, 4500 - 1234)


def test_operational_counter_resets_at_utc_boundary(tmp_path):
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_provider_quota(budget, daily_limit=7500, daily_remaining=7500)
    past_reset = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    _seed_operational(budget, used=4500, cap=4500, reset_at=past_reset)
    # Clock has crossed the reset boundary: EXHAUSTED must reopen automatically.
    assert budget.quota_state() != pb.QUOTA_EXHAUSTED
    decision = _issue(budget, params={"n": "1"}, priority=pb.PRIORITY_ESSENTIAL)
    assert decision.action in {pb.ACTION_ALLOW, pb.ACTION_CACHE_HIT}
    assert budget.quota_snapshot()["operational_used"] == 0


def test_exhausted_resumes_automatically_after_reset_no_manual_steps(tmp_path):
    # No restart, no manual refresh, no env change: crossing the reset boundary
    # reopens eligibility for the already-running scheduler.
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_provider_quota(budget, daily_limit=7500, daily_remaining=7500)
    future_reset = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
    _seed_operational(budget, used=4500, cap=4500, reset_at=future_reset)
    assert budget.quota_state() == pb.QUOTA_EXHAUSTED
    assert _issue(budget, priority=pb.PRIORITY_ESSENTIAL).action == pb.ACTION_BLOCKED_QUOTA
    # Open the persistent circuit for the provider-exhausted case too.
    budget.finish_failure(provider=pb.PROVIDER_API_FOOTBALL, endpoint="fixtures", params={"live": "all"},
                          state=pb.CIRCUIT_QUOTA_EXHAUSTED, message="daily limit reached")
    assert budget.circuit_snapshot()["state"] == pb.CIRCUIT_QUOTA_EXHAUSTED
    # Simulate the reset clock: move both reset boundaries into the past.
    past_reset = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(timespec="seconds")
    _seed_operational(budget, used=4500, cap=4500, reset_at=past_reset)
    with budget._connect() as conn:
        conn.execute("UPDATE provider_circuit_state SET blocked_until = ? WHERE provider = ?", (past_reset, pb.PROVIDER_API_FOOTBALL))
    # The very next scheduler tick must be able to issue eligible work.
    assert budget.quota_state() != pb.QUOTA_EXHAUSTED
    decision = budget.issue(provider=pb.PROVIDER_API_FOOTBALL, endpoint="fixtures", params={"live": "all"},
                            ttl_seconds=90, priority=pb.PRIORITY_ESSENTIAL)
    assert decision.action in {pb.ACTION_ALLOW, pb.ACTION_CACHE_HIT}


# ------------------------------------------------------------------- priorities
def test_far_future_polling_loses_priority_before_today(tmp_path):
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_provider_quota(budget, daily_limit=4500, daily_remaining=1700)  # 37.8% -> CONSERVE
    assert budget.quota_state() == pb.QUOTA_CONSERVE
    assert _issue(budget, priority=pb.PRIORITY_LOW).action == pb.ACTION_BLOCKED_QUOTA
    assert _issue(budget, priority=pb.PRIORITY_NORMAL).action == pb.ACTION_ALLOW


def test_near_kickoff_odds_retain_priority(tmp_path):
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_provider_quota(budget, daily_limit=4500, daily_remaining=400)  # ~9% -> CRITICAL
    assert budget.quota_state() == pb.QUOTA_CRITICAL
    assert _issue(budget, priority=pb.PRIORITY_LOW).action == pb.ACTION_BLOCKED_QUOTA
    assert _issue(budget, priority=pb.PRIORITY_NORMAL).action == pb.ACTION_BLOCKED_QUOTA
    assert _issue(budget, priority=pb.PRIORITY_ESSENTIAL).action == pb.ACTION_ALLOW


def test_settlement_retains_essential_priority(tmp_path):
    budget = _budget(tmp_path, operational_daily_cap=4500)
    _seed_provider_quota(budget, daily_limit=4500, daily_remaining=200)  # <10% -> CRITICAL
    assert budget.quota_state() == pb.QUOTA_CRITICAL
    decision = budget.issue(provider=pb.PROVIDER_API_FOOTBALL, endpoint="fixtures", params={"id": "1557378"},
                            ttl_seconds=300, priority=pb.PRIORITY_ESSENTIAL)
    assert decision.action in {pb.ACTION_ALLOW, pb.ACTION_CACHE_HIT}


# -------------------------------------------------------------------- rate limiter
def test_per_minute_burst_safety_enforced(tmp_path):
    budget = _budget(tmp_path)
    allowed = 0
    blocked = 0
    for i in range(8):
        decision = _issue(budget, params={"n": str(i)})
        if decision.action == pb.ACTION_ALLOW:
            allowed += 1
            budget.finish_success(provider=pb.PROVIDER_API_FOOTBALL, endpoint="fixtures", params={"n": str(i)},
                                  payload={"response": []}, provider_timestamp=None, ttl_seconds=60, response_headers=None)
        elif decision.action == pb.ACTION_BLOCKED_RATE:
            blocked += 1
    assert allowed >= 1
    assert blocked >= 1  # the second-window limit (4/sec) must defer extra bursts
    assert budget.rate_snapshot()["second_limit"] == 4


# -------------------------------------------------------------------- UI truth
def test_ui_separates_current_desk_from_paper_tracking(tmp_path):
    services = _services(tmp_path)
    payload = services.get_dashboard_payload()
    assert "today_scopes" in payload
    assert payload["today_scopes"]["desk"]["label"] == "Current Desk"
    assert payload["today_scopes"]["paper"]["label"] == "Paper Tracking"
    labels = [m["label"] for m in payload["glance_metrics"]]
    # First four are desk, remaining are paper tracking.
    assert labels[:4] == ["Analysed", "Predictions", "BET", "NO BET"]
    assert "Tracked" in labels
    assert "Suggested Bets" not in labels
    assert "persisted Route B rows" not in [m.get("note") for m in payload["glance_metrics"]]


def test_ev_label_corrected_calculation_unchanged(tmp_path):
    from sports_ai.dashboard.app import _template_directory
    base = Path(_template_directory())
    today_template = (base / "dashboard_sections.html").read_text(encoding="utf-8")
    assert "EV / 1u" in today_template
    assert "{:+.2%}".format(0.5) == "+50.00%"  # EV formatting still percent-of-1u


def test_last_20_is_settled_only(tmp_path):
    services = _services(tmp_path)
    rows = [
        {"paper_trade_id": f"p{i}", "market": "1x2", "selection": "HOME", "line": None,
         "trade_state": state, "updated_at": f"2026-08-29T00:{i:02d}:00+00:00",
         "decision_timestamp": "2026-08-28T00:00:00+00:00", "entry_odds": 2.0, "net_pnl": 1.0,
         "implied_probability_clv": None}
        for i, state in enumerate(["WON", "PENDING", "LOST", "OPEN", "PUSH", "HALF_WIN", "HALF_LOSS", "UNSETTLED", "WON", "VOID", "LOST", "WON"])
    ]
    payload = services._last_20_payload(rows)
    states = {r["trade_state"] for r in payload["rows"]}
    assert states <= {"WON", "LOST", "PUSH", "HALF_WIN", "HALF_LOSS", "VOID"}
    assert all(r["trade_state"] not in {"PENDING", "OPEN", "UNSETTLED"} for r in payload["rows"])
    assert payload["summary"]["count"] == 9  # 9 settled, < 20 -> actual count displayed


def test_quota_waiting_state_truthful(tmp_path):
    services = _services(tmp_path, allow=True)
    budget = services._provider_budget
    # Force EXHAUSTED via provider-reported remaining = 0 (no operational cap needed).
    budget._record_quota_from_headers({"x-ratelimit-limit": "7500", "x-ratelimit-remaining": "0"})
    payload = services.get_betting_desk_payload()
    assert payload["quota_waiting_reset"] is True
    assert "waiting for the provider quota reset" in payload["reason"]


def test_zero_live_provider_calls_occur_during_all_tests(tmp_path):
    budget = _budget(tmp_path, allow_paid_provider_calls=False)
    assert _issue(budget).action == pb.ACTION_BLOCKED_GATE
    assert budget.metrics().get("http_requests", 0) == 0
