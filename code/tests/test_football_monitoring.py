"""Read-only Football monitoring contract (football_monitoring_v1).

Pins:

* the headline is forward LIVE_ISSUED only; retrospective LIVE_ISSUED and replay
  rows are counted separately and never pooled;
* rates are None (not 0) with an empty denominator, with Wilson CIs and
  insufficient-sample flags;
* qualified betting counts value-pipeline rows only, never Model Picks or legacy
  paper positions, and makes no profitability claim;
* operational alerts fire for stale schedulers, settlement lag, READY fixtures
  without a pick, and stale odds;
* every DB is byte-identical afterwards, and missing DBs degrade, not crash.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import sports_ai.dashboard.services  # noqa: F401  (pre-import breaks the market_data/dashboard cycle)
import pytest

from sports_ai.dashboard import football_monitoring as mon
from sports_ai.storage import football_model_pick_ledger as ledger
from sports_ai.storage import football_paper_trading as paper_store

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def _pick(conn, pick_id, *, state, fair=1.8, origin="LIVE_ISSUED", created="2026-10-10T09:00:00+00:00",
          kickoff="2026-10-10T15:00:00+00:00", market="asian_handicap", competition="ENG1", model_version="m1"):
    now = "2026-10-12T00:00:00+00:00"
    conn.execute(
        "INSERT INTO football_model_pick_entries (model_pick_id, policy_id, policy_version, fixture_id, competition_id, "
        "kickoff_utc, market, line, selection, model_fair_price, model_version, record_origin, trade_state, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pick_id, "pol", "v1", f"fx-{pick_id}", competition, kickoff, market, 0.0, "HOME", fair, model_version,
         origin, state, created, now),
    )


@pytest.fixture
def paths(tmp_path):
    picks = tmp_path / "picks.sqlite"
    conn = ledger.connect(picks)
    ledger.ensure_schema(conn)
    # forward: 2 full wins, 1 full loss (q=0.8 -> high-confidence miss), 1 half win, 1 push, 1 open
    _pick(conn, "w1", state="FULL_WIN")
    _pick(conn, "w2", state="FULL_WIN", market="corner_total", competition="ESP1")
    _pick(conn, "l1", state="FULL_LOSS", fair=1.25)
    _pick(conn, "hw", state="HALF_WIN")
    _pick(conn, "p", state="PUSH")
    _pick(conn, "open_old", state=None, kickoff="2026-10-12T15:00:00+00:00", created="2026-10-12T09:00:00+00:00")
    # retrospective LIVE_ISSUED (created after kickoff) and replay rows: never in the headline
    _pick(conn, "late", state="FULL_LOSS", created="2026-10-10T18:00:00+00:00")
    _pick(conn, "replay", state="FULL_WIN", origin="CURRENT_SYSTEM_REPLAY")
    conn.commit()
    conn.close()

    dash = tmp_path / "dash.sqlite"
    d = sqlite3.connect(dash)
    d.execute("CREATE TABLE dashboard_fixtures (fixture_id TEXT PRIMARY KEY, kickoff_utc TEXT, status TEXT)")
    d.execute("CREATE TABLE dashboard_predictions (prediction_id TEXT PRIMARY KEY, fixture_id TEXT, status TEXT, generated_at TEXT)")
    d.executemany("INSERT INTO dashboard_fixtures VALUES (?,?,?)", [
        ("up_ready", "2026-10-14T15:00:00+00:00", "PRE_MATCH"),
        ("up_blocked", "2026-10-14T17:00:00+00:00", "PRE_MATCH"),
        ("up_none", "2026-10-15T17:00:00+00:00", "PRE_MATCH"),
        ("far", "2026-11-30T17:00:00+00:00", "PRE_MATCH"),
    ])
    d.executemany("INSERT INTO dashboard_predictions VALUES (?,?,?,?)", [
        ("a1", "up_ready", "BLOCKED", "2026-10-12T08:00:00+00:00"),
        ("a2", "up_ready", "READY", "2026-10-13T08:00:00+00:00"),
        ("b1", "up_blocked", "BLOCKED", "2026-10-13T08:00:00+00:00"),
    ])
    d.commit()
    d.close()

    paper = tmp_path / "paper.sqlite"
    p = paper_store.connect(paper)
    paper_store.ensure_schema(p)
    p.execute(
        "INSERT INTO market_snapshots (fixture_id, provider, bookmaker, market, selection, line, odds, captured_at, "
        "is_pre_match, ingestion_version) VALUES ('x','api_football','b','asian_handicap','HOME',0.0,1.9,?,1,'v2')",
        ("2026-10-13T06:00:00+00:00",),
    )
    p.commit()
    p.close()
    return {"dash": dash, "paper": paper, "picks": picks, "shadow": tmp_path / "shadow.sqlite"}


def _build(paths, status=None, **kw):
    return mon.build_football_monitoring_payload(
        dashboard_db_path=paths["dash"], paper_db_path=paths["paper"], model_pick_db_path=paths["picks"],
        shadow_db_path=paths["shadow"], runtime_status=status or {}, now=NOW, **kw,
    )


def test_headline_is_forward_only_with_ci_and_separate_exclusions(paths):
    perf = _build(paths)["model_performance"]
    fwd = perf["forward"]
    assert fwd["issued"] == 6 and fwd["settled"] == 5 and fwd["open"] == 1
    # won = 2 + 0.5 = 2.5 ; lost = 1 -> 2.5 / 3.5
    assert fwd["win_share_stake_weighted"] == pytest.approx(0.7143, abs=1e-4)
    lo, hi = fwd["ci95"]
    assert lo < fwd["win_share_stake_weighted"] < hi
    assert fwd["insufficient_sample"] is True
    assert perf["excluded_from_headline"] == {"live_issued_not_forward": 1, "current_system_replay": 1}
    assert perf["historical_replay"]["issued"] == 1
    assert set(perf["forward_by_market"]) == {"asian_handicap", "corner_total"}
    assert set(perf["forward_by_competition"]) == {"ENG1", "ESP1"}
    assert perf["forward_by_confidence_band"]["q>=0.70"]["states"]["FULL_LOSS"] == 1
    assert [m["model_pick_id"] for m in perf["high_confidence_misses"]] == ["l1"]
    assert perf["market_benchmark"]["forward_available"] is False


def test_empty_denominator_reports_none_not_zero():
    cohort = mon.accuracy_cohort([])
    assert cohort["win_share_stake_weighted"] is None
    assert cohort["ci95"] == [None, None]
    assert cohort["calibration_gap"] is None
    assert cohort["insufficient_sample"] is True


def test_operational_coverage_backlog_and_alerts(paths):
    status = {
        "model_pick_issuance_scheduler_enabled": True,
        "model_pick_issuance_last_success_at": "2026-10-11T00:00:00+00:00",
        "result_sync_scheduler_enabled": False,
        "fixture_provider_state": "DEGRADED",
        "result_sync_last_error": "OperationalError: /data/secret/path.sqlite",
    }
    op = _build(paths, status)["operational"]
    cov = op["prediction_coverage"]
    assert cov["upcoming_fixtures"] == 3  # 'far' is outside the 72h window
    assert cov["with_prediction"] == 2 and cov["latest_ready"] == 1
    assert cov["ready_without_model_pick"] == 1
    assert cov["latest_not_ready_by_status"] == {"BLOCKED": 1}
    assert op["settlement_backlog"]["open_past_kickoff"] == 1
    assert op["schedulers"]["model_pick_issuance"]["stale"] is True
    assert op["schedulers"]["result_sync"]["stale"] is False
    assert op["schedulers"]["result_sync"]["last_error_present"] is True
    assert "/data/secret" not in json.dumps(op)
    codes = {a["code"] for a in op["alerts"]}
    assert {"SCHEDULER_STALE", "READY_FIXTURES_WITHOUT_PICK", "SETTLEMENT_LAG", "ALL_ODDS_STALE",
            "FIXTURE_PROVIDER_NOT_CONNECTED", "SCHEDULER_LAST_CYCLE_ERROR"} <= codes
    (odds,) = op["odds_freshness"]
    assert odds["age_minutes"] == 360.0 and odds["stale"] is True


def test_qualified_betting_counts_value_pipeline_rows_only(paths):
    p = paper_store.connect(paths["paper"])
    for trade_id, pipeline, stake in (("vb1", "frozen_value_v1", 0.015), ("legacy", None, 0.01)):
        p.execute(
            "INSERT INTO paper_trade_entries (paper_trade_id, prediction_id, fixture_id, model_snapshot_id, model_version, market, "
            "selection, line, model_fair_price, available_market_price, decision, decision_timestamp, kickoff, bookmaker, provider, "
            "safeguard_state, entry_eligibility, suggested_stake_fraction, entry_signature, value_pipeline_version, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, "p", "fx", "s", "m", "corner_total", "OVER", 9.5, 1.8, 2.0, "BET", "2026-10-12T00:00:00+00:00",
             "2026-10-14T15:00:00+00:00", "bk", "api_football", "OK", "ELIGIBLE", stake, f"sig-{trade_id}", pipeline,
             "2026-10-12T00:00:00+00:00"),
        )
    p.commit()
    p.close()
    qb = _build(paths)["qualified_betting"]
    assert qb["available"] is True
    assert qb["value_bets"] == 1 and qb["legacy_paper_positions_excluded"] == 1
    assert qb["roi"] is None  # nothing settled
    assert qb["stake_cap_breaches"] == 0 and qb["max_suggested_stake_fraction"] == 0.015
    assert qb["open_exposure_stake_fraction"] == 0.015
    assert qb["profitability_claim"] == "NONE"
    assert qb["staking_policy"]["max_stake_fraction"] == 0.02
    assert qb["value_bet_issuance_enabled"] is False


# --------------------------------------------------------------------------- unified-dashboard adapter contract
CONTRACT_DIR = Path(__file__).resolve().parents[2] / "docs" / "contracts"
GOLDEN_STATUS = {
    "model_pick_issuance_scheduler_enabled": True,
    "model_pick_issuance_last_success_at": "2026-10-11T00:00:00+00:00",
    "result_sync_scheduler_enabled": True,
    "result_sync_last_success_at": "2026-10-13T11:50:00+00:00",
    "fixture_provider_state": "CONNECTED",
}
_JSON_TYPES = {
    "string": str, "boolean": bool, "array": list, "object": dict, "null": type(None),
    "integer": int, "number": (int, float),
}


def _is_type(value, name):
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _JSON_TYPES[name])


def _golden(paths):
    return _build(paths, GOLDEN_STATUS, deployed_commit="0000000000000000000000000000000000000000")


def test_payload_satisfies_consumed_field_contract(paths, tmp_path):
    empty = mon.build_football_monitoring_payload(
        dashboard_db_path=tmp_path / "nope.sqlite", paper_db_path=None, model_pick_db_path=None, shadow_db_path=None, now=NOW,
    )
    for payload in (_golden(paths), empty):
        for dotted, types in mon.CONSUMED_FIELDS.items():
            value = mon._get(payload, dotted)
            assert any(_is_type(value, t) for t in types), (dotted, value)


def test_golden_contract_files_are_current(paths):
    """docs/contracts/*.json are the fixtures the Tennis adapter tests against.

    Regenerate with SPORTS_AI_UPDATE_MONITORING_GOLDEN=1 after an intended change.
    """
    import os

    payload = json.loads(json.dumps(_golden(paths)))
    card = mon.to_unified_card(payload)
    files = {
        "football_monitoring_v1.example.json": payload,
        "football_monitoring_v1.unified_card.expected.json": card,
        "football_monitoring_v1.consumed_fields.json": {k: list(v) for k, v in mon.CONSUMED_FIELDS.items()},
    }
    if os.environ.get("SPORTS_AI_UPDATE_MONITORING_GOLDEN") == "1":
        CONTRACT_DIR.mkdir(parents=True, exist_ok=True)
        for name, doc in files.items():
            (CONTRACT_DIR / name).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for name, doc in files.items():
        assert json.loads((CONTRACT_DIR / name).read_text(encoding="utf-8")) == doc, name


def test_unified_card_semantics(paths):
    card = mon.to_unified_card(_golden(paths))
    mp = card["model_picks"]
    assert card["card_schema_version"] == "sports_edge_monitoring_card_v1"
    assert card["health"] == "RED"  # issuance heartbeat is stale and settlement lags
    assert {"SCHEDULER_STALE", "SETTLEMENT_LAG"} <= set(card["alerts"])
    # forward only: 6 issued, 5 settled incl. 1 push -> 4 scored; retro/replay excluded
    assert (mp["picks"], mp["scored"], mp["pushes"], mp["pending"]) == (6, 4, 1, 1)
    assert mp["hit_rate_basis"] == "STAKE_WEIGHTED_PUSH_EXCLUDED"
    assert mp["excluded_from_headline"] == {"live_issued_not_forward": 1, "current_system_replay": 1}
    assert card["value_bets"]["profitability_claim"] == "NONE"
    assert card["value_bets"]["issuance_enabled"] is False

    empty = mon.to_unified_card(mon.build_football_monitoring_payload(
        dashboard_db_path=Path("missing.sqlite"), paper_db_path=None, model_pick_db_path=None, shadow_db_path=None, now=NOW,
    ))
    assert empty["initialized"] is False and empty["health"] == "UNAVAILABLE"
    assert empty["model_picks"]["observed_hit_rate"] is None  # never rendered as 0%
    assert mon.to_unified_card({"schema_version": "football_monitoring_v2"})["initialized"] is False


def test_never_writes_and_degrades_when_dbs_missing(paths, tmp_path):
    before = {k: Path(v).read_bytes() for k, v in paths.items() if Path(v).exists()}
    payload = _build(paths)
    json.dumps(payload)  # JSON-serialisable contract
    assert {k: Path(v).read_bytes() for k, v in paths.items() if Path(v).exists()} == before
    assert not paths["shadow"].exists()
    assert payload["high_confidence_shadow"]["available"] is False

    empty = mon.build_football_monitoring_payload(
        dashboard_db_path=tmp_path / "nope.sqlite", paper_db_path=None, model_pick_db_path=None,
        shadow_db_path=None, now=NOW,
    )
    assert empty["sources"] == {"dashboard_db": False, "paper_db": False, "model_pick_db": False}
    assert empty["model_performance"]["forward"]["win_share_stake_weighted"] is None
    assert empty["qualified_betting"]["available"] is False
    assert not (tmp_path / "nope.sqlite").exists()
