"""Focused tests for automatic Model Pick settlement from verified truth.

DB-only and no live provider calls. Verifies the two truth contracts (AH score,
Corners authoritative stats), engine delegation (integer/half/quarter lines),
strict LIVE_ISSUED-only filtering, idempotency, conflicts, dry-run zero writes,
and that the paper ledger / market collector are never touched.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sports_ai.dashboard.services as services_mod  # noqa: F401  (pre-import breaks the market_data/dashboard cycle)
from sports_ai.dashboard import model_pick_settlement as mps
from sports_ai.dashboard import storage
from sports_ai.dashboard.models import NormalizedFixture
from sports_ai.dashboard.services import (
    RUNTIME_ROLE_COMBINED,
    DashboardConfig,
    DashboardServices,
)
from sports_ai.market_data.providers import NotConfiguredMarketProvider
from sports_ai.storage import football_model_pick_ledger as ledger

_NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def _pick(fid, market, selection, line, *, origin="LIVE_ISSUED", model_pick_id=None):
    return {
        "model_pick_id": model_pick_id or f"mp_{fid}_{market}_{selection}_{line}",
        "fixture_id": fid,
        "market": market,
        "selection": selection,
        "line": line,
        "record_origin": origin,
        "trade_state": None,
    }


def _fixture(fid, *, status="FINISHED", home=1, away=0, kickoff="2026-10-07T10:00:00+00:00"):
    return {
        "fixture_id": fid,
        "status": status,
        "home_score": home,
        "away_score": away,
        "kickoff_utc": kickoff,
        "provider": "api-football",
        "last_provider_update": "2026-10-07T12:05:00+00:00",
    }


def _result(fid, hc, ac, *, total=None):
    return {
        "fixture_id": fid,
        "home_corners": hc,
        "away_corners": ac,
        "total_corners": (hc + ac) if total is None else total,
        "statistics_provider": "api-football",
        "source_timestamp": "2026-10-07T12:06:00+00:00",
        "captured_at": "2026-10-07T12:07:00+00:00",
    }


def _plan(picks, fixtures, results):
    return mps.build_settlement_plan(open_picks=picks, fixtures=fixtures, results=results, now_utc=_NOW)


# --------------------------------------------------------------------------- AH states
@pytest.mark.parametrize(
    ("selection", "line", "home", "away", "expected"),
    [
        ("HOME", -1.0, 2, 0, "FULL_WIN"),
        ("HOME", -1.0, 1, 0, "PUSH"),  # 1-0 with -1 -> push
        ("HOME", -0.5, 0, 0, "FULL_LOSS"),
        ("AWAY", -0.5, 0, 1, "FULL_WIN"),
        ("HOME", -0.75, 1, 0, "HALF_WIN"),
        ("HOME", -0.25, 0, 0, "HALF_LOSS"),
        ("HOME", 0.0, 1, 1, "PUSH"),
    ],
)
def test_ah_settlement_states(selection, line, home, away, expected):
    plan = _plan([_pick("f1", "asian_handicap", selection, line)], {"f1": _fixture("f1", home=home, away=away)}, {})
    assert len(plan.actions) == 1
    assert plan.actions[0].trade_state == expected
    assert plan.actions[0].settlement_payload["result"]["home_goals"] == home


@pytest.mark.parametrize(
    ("selection", "line", "total", "expected"),
    [
        ("OVER", 10.0, 11, "FULL_WIN"),
        ("UNDER", 10.0, 9, "FULL_WIN"),
        ("OVER", 9.5, 10, "FULL_WIN"),
        ("OVER", 9.75, 10, "HALF_WIN"),
        ("OVER", 10.25, 10, "HALF_LOSS"),
        ("OVER", 10.0, 10, "PUSH"),
        ("UNDER", 10.0, 11, "FULL_LOSS"),
    ],
)
def test_corner_settlement_states(selection, line, total, expected):
    home, away = total - 3, 3
    plan = _plan(
        [_pick("f1", "corner_total", selection, line)],
        {"f1": _fixture("f1", home=2, away=1)},
        {"f1": _result("f1", home, away)},
    )
    assert len(plan.actions) == 1
    assert plan.actions[0].trade_state == expected


# --------------------------------------------------------------------------- eligibility
def test_non_final_fixture_stays_open():
    for status in ("PRE_MATCH", "LIVE", "HALFTIME", "POSTPONED", "CANCELLED", "ABANDONED", "UNKNOWN"):
        plan = _plan([_pick("f1", "asian_handicap", "HOME", -0.5)], {"f1": _fixture("f1", status=status, home=1, away=0)}, {})
        assert plan.actions == ()
        assert plan.ineligible[0]["reason"] in {mps.REASON_NON_FINAL, mps.REASON_UPCOMING}


def test_score_present_live_stays_open():
    plan = _plan([_pick("f1", "asian_handicap", "HOME", -0.5)], {"f1": _fixture("f1", status="LIVE", home=1, away=0)}, {})
    assert plan.actions == ()
    assert plan.ineligible[0]["reason"] == mps.REASON_NON_FINAL


def test_finished_without_score_stays_open():
    plan = _plan([_pick("f1", "asian_handicap", "HOME", -0.5)], {"f1": _fixture("f1", status="FINISHED", home=None, away=None)}, {})
    assert plan.actions == ()
    assert plan.ineligible[0]["reason"] == mps.REASON_FINAL_SCORE_MISSING


def test_upcoming_fixture_reason():
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
    plan = _plan([_pick("f1", "asian_handicap", "HOME", -0.5)], {"f1": _fixture("f1", status="PRE_MATCH", kickoff=future)}, {})
    assert plan.ineligible[0]["reason"] == mps.REASON_UPCOMING


def test_missing_fixture_truth_reason():
    plan = _plan([_pick("f1", "asian_handicap", "HOME", -0.5)], {}, {})
    assert plan.ineligible[0]["reason"] == mps.REASON_IDENTITY_TRUTH_MISSING


def test_corner_truth_missing_stays_open():
    plan = _plan([_pick("f1", "corner_total", "OVER", 9.5)], {"f1": _fixture("f1")}, {})
    assert plan.actions == ()
    assert plan.ineligible[0]["reason"] == mps.REASON_CORNER_TRUTH_MISSING


def test_corner_total_inconsistency_blocks():
    plan = _plan(
        [_pick("f1", "corner_total", "OVER", 9.5)],
        {"f1": _fixture("f1")},
        {"f1": _result("f1", 5, 4, total=99)},
    )
    assert plan.actions == ()
    assert plan.ineligible[0]["reason"] == mps.REASON_CORNER_TRUTH_CONFLICT
    assert plan.conflicts[0]["reason"] == mps.REASON_CORNER_TRUTH_CONFLICT


def test_corner_never_inferred_from_score():
    # FINISHED with score present but no authoritative stats -> corner pick stays open.
    plan = _plan([_pick("f1", "corner_total", "OVER", 9.5)], {"f1": _fixture("f1", home=3, away=1)}, {})
    assert plan.actions == ()
    assert plan.ineligible[0]["reason"] == mps.REASON_CORNER_TRUTH_MISSING


def test_action_hash_is_deterministic():
    picks = [_pick("f1", "asian_handicap", "HOME", -0.5), _pick("f2", "corner_total", "OVER", 9.5)]
    fixtures = {"f1": _fixture("f1", home=2, away=0), "f2": _fixture("f2", home=1, away=1)}
    results = {"f2": _result("f2", 6, 5)}
    h1 = _plan(picks, fixtures, results).action_hash
    h2 = _plan(list(reversed(picks)), fixtures, results).action_hash
    assert h1 == h2


# --------------------------------------------------------------------------- engine conflicts
def test_update_settlement_identical_is_noop(tmp_path):
    conn = ledger.connect(tmp_path / "picks.sqlite")
    ledger.ensure_schema(conn)
    _seed_ledger_row(conn, "mp1", "f1", "asian_handicap", "HOME", -0.5, trade_state="FULL_WIN", payload={"raw_state": "WON", "result": {"home_goals": 2, "away_goals": 0}})
    ledger.update_settlement(conn, model_pick_id_value="mp1", trade_state="FULL_WIN", settlement_payload={"raw_state": "WON", "result": {"home_goals": 2, "away_goals": 0}}, now="2026-10-08T13:00:00+00:00", initialize_schema=False)
    row = dict(conn.execute("SELECT trade_state, updated_at FROM football_model_pick_entries WHERE model_pick_id='mp1'").fetchone())
    assert row["trade_state"] == "FULL_WIN"
    assert row["updated_at"] != "2026-10-08T13:00:00+00:00"  # unchanged
    conn.close()


def test_update_settlement_state_conflict_blocked(tmp_path):
    conn = ledger.connect(tmp_path / "picks.sqlite")
    ledger.ensure_schema(conn)
    _seed_ledger_row(conn, "mp1", "f1", "asian_handicap", "HOME", -0.5, trade_state="FULL_WIN", payload={"result": {"home_goals": 2, "away_goals": 0}})
    with pytest.raises(sqlite3.IntegrityError):
        ledger.update_settlement(conn, model_pick_id_value="mp1", trade_state="FULL_LOSS", settlement_payload={"result": {"home_goals": 0, "away_goals": 2}}, initialize_schema=False)
    assert dict(conn.execute("SELECT trade_state FROM football_model_pick_entries WHERE model_pick_id='mp1'").fetchone())["trade_state"] == "FULL_WIN"
    conn.close()


def test_update_settlement_evidence_conflict_blocked(tmp_path):
    conn = ledger.connect(tmp_path / "picks.sqlite")
    ledger.ensure_schema(conn)
    _seed_ledger_row(conn, "mp1", "f1", "asian_handicap", "HOME", -0.5, trade_state="FULL_WIN", payload={"result": {"home_goals": 2, "away_goals": 0}})
    with pytest.raises(sqlite3.IntegrityError):
        ledger.update_settlement(conn, model_pick_id_value="mp1", trade_state="FULL_WIN", settlement_payload={"result": {"home_goals": 3, "away_goals": 0}}, initialize_schema=False)
    conn.close()


# --------------------------------------------------------------------------- services
class FakeFixtureProvider:
    def __init__(self):
        self.calls = 0

    def provider_name(self):
        return "api-football"

    def configured(self):
        return False

    def get_fixture(self, fixture_id):
        self.calls += 1
        raise AssertionError("settlement must not call a provider")

    def get_live_fixtures(self):
        self.calls += 1
        raise AssertionError("settlement must not call a provider")


class ExplodingStatisticsProvider:
    def provider_name(self):
        return "api-football"

    def configured(self):
        return True

    def get_authoritative_corner_statistics(self, fixture_context):
        raise AssertionError("settlement must not call a statistics provider")


def _config(tmp_path):
    return replace(
        DashboardConfig.default(),
        dashboard_db_path=tmp_path / "football_dashboard.sqlite",
        paper_trading_db_path=tmp_path / "football_paper_trading.sqlite",
        model_pick_db_path=tmp_path / "football_model_picks.sqlite",
        operational_data_dir=tmp_path,
        storage_mode="test_tmpdir",
        persistent_data_dir_configured=False,
        enable_scheduler=False,
        enable_result_sync_scheduler=True,
        enable_market_collection_scheduler=True,
        enable_model_pick_settlement_scheduler=True,
        model_pick_settlement_interval_seconds=600,
        runtime_role=RUNTIME_ROLE_COMBINED,
    )


def _services(config):
    return DashboardServices(
        config,
        fixture_provider=FakeFixtureProvider(),
        market_provider=NotConfiguredMarketProvider(),
        statistics_provider=ExplodingStatisticsProvider(),
    )


def _seed_dashboard_fixture(config, fixture_dict):
    provider, _, pfid = fixture_dict["fixture_id"].partition(":")
    normalized = NormalizedFixture(
        fixture_id=fixture_dict["fixture_id"], provider_fixture_id=pfid or fixture_dict["fixture_id"],
        provider=provider or "api-football", competition_id="ENG1", competition_name="Premier League",
        division_code="E0", season="2026", home_team_id="1", home_team_name="Home", away_team_id="2",
        away_team_name="Away", kickoff_utc=fixture_dict["kickoff_utc"], status=fixture_dict["status"],
        elapsed_minute=None, home_score=fixture_dict["home_score"], away_score=fixture_dict["away_score"],
        last_provider_update=fixture_dict["last_provider_update"], source_version="api_football_v1",
        provider_payload={},
    )
    with storage.connect(config.dashboard_db_path) as conn:
        storage.initialize_database(conn)
        storage.upsert_fixtures(conn, [normalized], updated_at="2026-10-07T12:00:00+00:00", initialize_schema=False)


def _seed_ledger_row(conn, model_pick_id, fixture_id, market, selection, line, *, origin="LIVE_ISSUED", trade_state=None, payload=None):
    conn.execute(
        """
        INSERT INTO football_model_pick_entries (
            model_pick_id, policy_id, policy_version, fixture_id, market, selection, line,
            record_origin, trade_state, settlement_payload_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (model_pick_id, "policy", "v1", fixture_id, market, selection, line, origin, trade_state,
         json.dumps(payload) if payload is not None else None, "2026-10-07T09:00:00+00:00", "2026-10-07T09:00:00+00:00"),
    )


def _seed_pick(config, fixture_id, market, selection, line, *, origin="LIVE_ISSUED", model_pick_id=None, trade_state=None, payload=None):
    conn = ledger.connect(config.model_pick_db_path)
    ledger.ensure_schema(conn)
    _seed_ledger_row(conn, model_pick_id or f"mp_{fixture_id}_{market}", fixture_id, market, selection, line, origin=origin, trade_state=trade_state, payload=payload)
    conn.commit()
    conn.close()
    return model_pick_id or f"mp_{fixture_id}_{market}"


def _ledger_row(config, model_pick_id):
    conn = ledger.connect(config.model_pick_db_path)
    row = dict(conn.execute("SELECT * FROM football_model_pick_entries WHERE model_pick_id = ?", (model_pick_id,)).fetchone())
    conn.close()
    return row


def test_settlement_cycle_settles_ah_and_corners(tmp_path):
    config = _config(tmp_path)
    _seed_dashboard_fixture(config, _fixture("api-football:1", home=2, away=0))
    _seed_dashboard_fixture(config, _fixture("api-football:2", home=1, away=1))
    _seed_pick(config, "api-football:1", "asian_handicap", "HOME", -0.5)
    _seed_pick(config, "api-football:2", "corner_total", "OVER", 9.5)
    with storage.connect(config.dashboard_db_path) as conn:
        storage.upsert_fixture_result(conn, fixture_id="api-football:2", statistics_provider="api-football", statistics_provider_fixture_id="2", home_corners=6, away_corners=5, total_corners=11, source_timestamp=None, captured_at="2026-10-07T12:07:00+00:00", initialize_schema=False)
    services = _services(config)
    result = services.run_model_pick_settlement_cycle()
    assert result["applied"] == 2
    assert result["applied_states"] == {"FULL_WIN": 2}
    assert _ledger_row(config, "mp_api-football:1_asian_handicap")["trade_state"] == "FULL_WIN"
    assert _ledger_row(config, "mp_api-football:2_corner_total")["trade_state"] == "FULL_WIN"
    assert not Path(config.paper_trading_db_path).exists()


def test_settlement_cycle_is_idempotent(tmp_path):
    config = _config(tmp_path)
    _seed_dashboard_fixture(config, _fixture("api-football:1", home=2, away=0))
    _seed_pick(config, "api-football:1", "asian_handicap", "HOME", -0.5)
    services = _services(config)
    first = services.run_model_pick_settlement_cycle()
    assert first["applied"] == 1
    updated_at = _ledger_row(config, "mp_api-football:1_asian_handicap")["updated_at"]
    second = services.run_model_pick_settlement_cycle()
    assert second["applied"] == 0
    assert _ledger_row(config, "mp_api-football:1_asian_handicap")["updated_at"] == updated_at


def test_current_system_replay_ignored(tmp_path):
    config = _config(tmp_path)
    _seed_dashboard_fixture(config, _fixture("api-football:1", home=2, away=0))
    _seed_pick(config, "api-football:1", "asian_handicap", "HOME", -0.5, origin="CURRENT_SYSTEM_REPLAY")
    services = _services(config)
    result = services.run_model_pick_settlement_cycle()
    assert result["applied"] == 0
    assert result["examined"] == 0
    assert _ledger_row(config, "mp_api-football:1_asian_handicap")["trade_state"] is None


def test_dry_run_zero_writes_and_apply_parity(tmp_path):
    config = _config(tmp_path)
    _seed_dashboard_fixture(config, _fixture("api-football:1", home=2, away=0))
    _seed_pick(config, "api-football:1", "asian_handicap", "HOME", -0.5)
    services = _services(config)
    dry = services.model_pick_settlement_dry_run()
    assert dry["dry_run"] is True
    assert dry["writes"] == 0
    assert dry["eligible_total"] == 1
    assert dry["state_counts"]["FULL_WIN"] == 1
    assert _ledger_row(config, "mp_api-football:1_asian_handicap")["trade_state"] is None  # no write
    applied = services.run_model_pick_settlement_cycle()
    assert applied["action_hash"] == dry["action_hash"]


def test_immutable_fields_unchanged_after_settlement(tmp_path):
    config = _config(tmp_path)
    _seed_dashboard_fixture(config, _fixture("api-football:1", home=2, away=0))
    _seed_pick(config, "api-football:1", "asian_handicap", "HOME", -0.5)
    before = _ledger_row(config, "mp_api-football:1_asian_handicap")
    _services(config).run_model_pick_settlement_cycle()
    after = _ledger_row(config, "mp_api-football:1_asian_handicap")
    for field in ledger._IMMUTABLE_FIELDS:
        assert before[field] == after[field]
    assert after["trade_state"] == "FULL_WIN"


def test_settlement_active_flag_and_interval():
    assert mps.resolve_settlement_interval_seconds(None) == 600
    assert mps.resolve_settlement_interval_seconds(120) == mps.MIN_SETTLEMENT_INTERVAL_SECONDS
    config = DashboardConfig.default()
    assert config.enable_model_pick_settlement_scheduler is False
    assert config.model_pick_settlement_interval_seconds == 600


def test_plan_never_reads_market_or_paper(tmp_path):
    config = _config(tmp_path)
    _seed_dashboard_fixture(config, _fixture("api-football:1", home=2, away=0))
    _seed_pick(config, "api-football:1", "asian_handicap", "HOME", -0.5)
    services = _services(config)
    services.run_model_pick_settlement_cycle()
    # market provider (NotConfigured) and statistics provider (exploding) were never called.
    assert not Path(config.paper_trading_db_path).exists()


def test_module_has_no_provider_or_historical_references():
    import inspect

    source = inspect.getsource(mps)
    for forbidden in ("httpx", "requests", "urllib", "market_snapshots", "?date=", "the-odds-api", "5dollar"):
        assert forbidden not in source
