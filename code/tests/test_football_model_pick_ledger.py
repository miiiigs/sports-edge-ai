"""Focused tests for the model-only pick policy + LIVE_ISSUED ledger."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

# Import-cycle guard (same convention as other football tests).
import sports_ai.dashboard.services  # noqa: F401
from sports_ai.dashboard import model_pick_policy as policy
from sports_ai.dashboard import model_pick_reconciliation as recon
from sports_ai.storage import football_model_pick_ledger as ledger


def _mr(family, line, selection, fair, prob):
    return {
        "market_family": family,
        "line": line,
        "selection": selection,
        "fair_decimal_price": fair,
        "effective_win_stake_probability": prob,
    }


def _ah_rows():
    data = [
        (-1.0, 1.25, 4.00, 0.80, 0.25),
        (-0.75, 1.60, 2.40, 0.63, 0.42),
        (-0.5, 1.80, 2.10, 0.56, 0.48),
        (-0.25, 1.95, 1.98, 0.512, 0.505),
        (0.0, 2.05, 1.90, 0.49, 0.53),
        (0.25, 2.20, 1.75, 0.45, 0.57),
        (0.5, 2.50, 1.55, 0.40, 0.65),
        (0.75, 3.20, 1.35, 0.31, 0.74),
        (1.0, 4.00, 1.25, 0.25, 0.80),  # extreme: AWAY has the largest win prob
    ]
    rows = []
    for line, home, away, hp, ap in data:
        rows.append(_mr("asian_handicap", line, "HOME", home, hp))
        rows.append(_mr("asian_handicap", line, "AWAY", away, ap))
    return rows


def _corner_rows():
    data = [
        (8.5, 1.40, 3.00, 0.71, 0.33),
        (9.0, 1.70, 2.20, 0.59, 0.45),
        (9.25, 1.85, 2.00, 0.54, 0.50),
        (9.5, 1.95, 1.93, 0.512, 0.518),
        (9.75, 2.10, 1.80, 0.48, 0.56),
        (10.0, 2.50, 1.55, 0.40, 0.645),
        (10.5, 3.50, 1.30, 0.29, 0.77),  # extreme: UNDER has the largest win prob
    ]
    rows = []
    for line, over, under, op, up in data:
        rows.append(_mr("corner_total", line, "OVER", over, op))
        rows.append(_mr("corner_total", line, "UNDER", under, up))
    return rows


# --------------------------------------------------------------------------- policy
def test_balanced_ah_line_is_deterministic_and_not_max_probability():
    result = policy.select_model_picks(_ah_rows())
    ah = result["asian_handicap"]
    assert ah["market"] == "asian_handicap"
    assert ah["line"] == -0.25
    assert ah["selection"] == "HOME"  # lower fair price at the balanced line
    # The max-probability extreme (AWAY +1.0) must NOT be selected.
    assert not (ah["line"] == 1.0 and ah["selection"] == "AWAY")


def test_balanced_corner_line_is_deterministic_and_not_max_probability():
    result = policy.select_model_picks(_corner_rows())
    corner = result["corner_total"]
    assert corner["line"] == 9.5
    assert corner["selection"] == "UNDER"  # 1.93 < 1.95
    assert not (corner["line"] == 10.5 and corner["selection"] == "UNDER")


def test_line_tie_break_uses_catalog_order():
    rows = [
        _mr("asian_handicap", -0.5, "HOME", 1.90, 0.52),
        _mr("asian_handicap", -0.5, "AWAY", 2.00, 0.49),
        _mr("asian_handicap", 0.5, "HOME", 2.00, 0.49),
        _mr("asian_handicap", 0.5, "AWAY", 1.90, 0.52),
    ]
    result = policy.select_model_picks(rows)
    assert result["asian_handicap"]["line"] == -0.5  # catalog order


def test_side_tie_break_prefers_home_and_over():
    ah = policy.select_model_picks(
        [_mr("asian_handicap", 0.0, "HOME", 1.95, 0.51), _mr("asian_handicap", 0.0, "AWAY", 1.95, 0.51)]
    )
    assert ah["asian_handicap"]["selection"] == "HOME"
    corner = policy.select_model_picks(
        [_mr("corner_total", 9.5, "OVER", 1.95, 0.51), _mr("corner_total", 9.5, "UNDER", 1.95, 0.51)]
    )
    assert corner["corner_total"]["selection"] == "OVER"


def test_incomplete_pairs_produce_no_pick_with_diagnostic():
    rows = [_mr("asian_handicap", -0.5, "HOME", 1.9, 0.52)]  # AWAY missing
    result = policy.select_model_picks(rows)
    assert result["asian_handicap"] is None
    assert result["diagnostics"]["asian_handicap"] == "NO_COMPLETE_PAIRED_LINE"


@pytest.mark.parametrize("forbidden", ["odds", "edge", "bookmaker", "best_available_price", "closing", "settlement_result"])
def test_policy_rejects_odds_like_inputs(forbidden):
    rows = _ah_rows() + _corner_rows()
    rows[0][forbidden] = 1.0
    with pytest.raises(ValueError):
        policy.select_model_picks(rows)


# --------------------------------------------------------------------------- ledger storage
def _ledger_conn(tmp_path):
    conn = ledger.connect(tmp_path / "model_picks.sqlite")
    ledger.ensure_schema(conn)
    return conn


def _pick(fixture_id="fx1", market="asian_handicap", **over):
    base = {
        "policy_id": policy.POLICY_ID,
        "policy_version": policy.POLICY_VERSION,
        "fixture_id": fixture_id,
        "canonical_fixture_id": "canon1",
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "kickoff_utc": "2026-09-20T15:00:00+00:00",
        "market": market,
        "line": -0.25,
        "selection": "HOME",
        "model_probability": 0.512,
        "model_fair_price": 1.95,
        "prediction_id": "prediction_x",
        "model_snapshot_id": "snap1",
        "model_version": "football_dashboard_current_snapshot_v1",
        "prediction_generated_at": "2026-09-19T10:00:00+00:00",
        "model_as_of": "2026-09-18T23:59:59+00:00",
        "record_origin": ledger.RECORD_ORIGIN_LIVE_ISSUED,
        "source_prediction_version_key": "vk1",
    }
    base.update(over)
    return base


def test_record_model_pick_is_idempotent(tmp_path):
    conn = _ledger_conn(tmp_path)
    ledger.record_model_pick(conn, _pick(), initialize_schema=False)
    ledger.record_model_pick(conn, _pick(), initialize_schema=False)
    conn.commit()
    assert conn.execute("select count(*) from football_model_pick_entries").fetchone()[0] == 1
    conn.close()


def test_invalid_record_origin_rejected(tmp_path):
    conn = _ledger_conn(tmp_path)
    with pytest.raises(ValueError):
        ledger.record_model_pick(conn, _pick(record_origin="NOT_A_REAL_ORIGIN"), initialize_schema=False)


def test_quarter_line_ah_settlement(tmp_path):
    conn = _ledger_conn(tmp_path)
    ledger.record_model_pick(conn, _pick(market="asian_handicap", line=-0.25, selection="HOME"), initialize_schema=False)
    ledger.record_model_pick(conn, _pick(market="asian_handicap", line=-0.25, selection="AWAY", fixture_id="fx2"), initialize_schema=False)
    conn.commit()
    # home 1 - away 1: HOME -0.25 -> half loss; AWAY -0.25 -> half win
    ledger.settle_rows(conn, {"fx1": {"home_goals": 1, "away_goals": 1}, "fx2": {"home_goals": 1, "away_goals": 1}}, initialize_schema=False)
    conn.commit()
    states = {r["fixture_id"]: r["trade_state"] for r in ledger.fetch_model_pick_rows(conn, initialize_schema=False)}
    assert states["fx1"] == "HALF_LOSS"
    assert states["fx2"] == "HALF_WIN"
    conn.close()


def test_quarter_line_corner_settlement(tmp_path):
    conn = _ledger_conn(tmp_path)
    ledger.record_model_pick(conn, _pick(market="corner_total", line=9.25, selection="OVER"), initialize_schema=False)
    ledger.record_model_pick(conn, _pick(market="corner_total", line=9.5, selection="OVER", fixture_id="fx2"), initialize_schema=False)
    conn.commit()
    ledger.settle_rows(
        conn,
        {"fx1": {"total_corners": 9}, "fx2": {"total_corners": 10}},
        initialize_schema=False,
    )
    conn.commit()
    states = {r["fixture_id"]: r["trade_state"] for r in ledger.fetch_model_pick_rows(conn, initialize_schema=False)}
    assert states["fx1"] == "HALF_LOSS"  # 9.25 split -> push at 9, loss at 9.5
    assert states["fx2"] == "FULL_WIN"  # 10 > 9.5
    conn.close()


def test_model_pick_performance_excludes_pushes_from_rate(tmp_path):
    conn = _ledger_conn(tmp_path)
    ledger.record_model_pick(conn, _pick(fixture_id="f1", line=-0.5, selection="HOME"), initialize_schema=False)
    ledger.record_model_pick(conn, _pick(fixture_id="f2", line=0.0, selection="HOME"), initialize_schema=False)
    conn.commit()
    ledger.settle_rows(conn, {"f1": {"home_goals": 3, "away_goals": 1}, "f2": {"home_goals": 1, "away_goals": 1}}, initialize_schema=False)
    conn.commit()
    perf = ledger.model_pick_performance(ledger.fetch_model_pick_rows(conn, initialize_schema=False))
    assert perf["overall"]["issued"] == 2
    assert perf["overall"]["full_wins"] == 1  # f1 AH -0.5 HOME wins
    assert perf["overall"]["pushes"] == 1  # f2 AH 0.0 with a draw pushes
    assert perf["overall"]["weighted_success_rate"] == 1.0  # pushes excluded
    assert "roi" not in {k.lower() for k in perf["overall"]}
    conn.close()


# --------------------------------------------------------------------------- reconciliation
def _dashboard_conn(tmp_path):
    conn = sqlite3.connect(tmp_path / "dash.sqlite")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE dashboard_fixtures (fixture_id TEXT PRIMARY KEY, competition_id TEXT, competition_name TEXT, "
        "home_team_name TEXT, away_team_name TEXT, kickoff_utc TEXT, status TEXT, home_score INTEGER, away_score INTEGER)"
    )
    conn.execute(
        "CREATE TABLE dashboard_predictions (prediction_id TEXT PRIMARY KEY, fixture_id TEXT, status TEXT, "
        "generated_at TEXT, model_as_of TEXT, model_snapshot_id TEXT, version_key TEXT, payload_json TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE dashboard_fixture_results (fixture_id TEXT PRIMARY KEY, home_corners INTEGER, away_corners INTEGER, total_corners INTEGER)"
    )
    return conn


def _payload():
    return {
        "model_versions": {"current_snapshot_model_version": "football_dashboard_current_snapshot_v1"},
        "market_rows": _ah_rows() + _corner_rows(),
    }


def _add_prediction(conn, *, prediction_id, fixture_id, status, generated_at, model_as_of, version_key, payload=None):
    conn.execute(
        "INSERT INTO dashboard_predictions VALUES (?,?,?,?,?,?,?,?,?)",
        (
            prediction_id,
            fixture_id,
            status,
            generated_at,
            model_as_of,
            "snap1",
            version_key,
            json.dumps(payload if payload is not None else _payload()),
            generated_at,
        ),
    )


def test_first_issued_prediction_chosen_and_post_kickoff_rejected(tmp_path):
    dash = _dashboard_conn(tmp_path)
    dash.execute(
        "INSERT INTO dashboard_fixtures VALUES (?,?,?,?,?,?,?,?,?)",
        ("fx1", "ENG1", "Premier League", "Home", "Away", "2026-09-20T15:00:00+00:00", "PRE_MATCH", None, None),
    )
    _add_prediction(dash, prediction_id="p_early", fixture_id="fx1", status="READY", generated_at="2026-09-19T10:00:00+00:00", model_as_of="2026-09-18T23:59:59+00:00", version_key="a")
    _add_prediction(dash, prediction_id="p_post", fixture_id="fx1", status="READY", generated_at="2026-09-20T16:00:00+00:00", model_as_of="2026-09-20T15:30:00+00:00", version_key="b")
    dash.commit()
    manifest = recon.build_live_issued_ledger(dash, dry_run=True)
    assert manifest["pre_kickoff_ready"] == 1
    assert manifest["post_kickoff_excluded"] == 1
    assert manifest["proposed_ah_model_picks"] == 1
    chosen = next(p for p in manifest["proposed_picks"] if p["market"] == "asian_handicap")
    assert chosen["prediction_id"] == "p_early"
    assert chosen["record_origin"] == "LIVE_ISSUED"
    dash.close()


def test_blocked_prediction_is_not_a_pick(tmp_path):
    dash = _dashboard_conn(tmp_path)
    dash.execute(
        "INSERT INTO dashboard_fixtures VALUES (?,?,?,?,?,?,?,?,?)",
        ("fx1", "ENG1", "Premier League", "Home", "Away", "2026-09-20T15:00:00+00:00", "PRE_MATCH", None, None),
    )
    _add_prediction(dash, prediction_id="p_blocked", fixture_id="fx1", status="BLOCKED", generated_at="2026-09-19T10:00:00+00:00", model_as_of=None, version_key="b", payload={"market_rows": []})
    dash.commit()
    manifest = recon.build_live_issued_ledger(dash, dry_run=True)
    assert manifest["blocked_predictions"] == 1
    assert manifest["proposed_ah_model_picks"] == 0
    assert manifest["proposed_corner_model_picks"] == 0
    dash.close()


def test_dry_run_writes_nothing_and_write_is_idempotent(tmp_path):
    dash = _dashboard_conn(tmp_path)
    dash.execute(
        "INSERT INTO dashboard_fixtures VALUES (?,?,?,?,?,?,?,?,?)",
        ("fx1", "ENG1", "Premier League", "Home", "Away", "2026-09-20T15:00:00+00:00", "PRE_MATCH", None, None),
    )
    _add_prediction(dash, prediction_id="p1", fixture_id="fx1", status="READY", generated_at="2026-09-19T10:00:00+00:00", model_as_of="2026-09-18T23:59:59+00:00", version_key="a")
    dash.commit()
    led = _ledger_conn(tmp_path)
    recon.build_live_issued_ledger(dash, dry_run=True, ledger_conn=led)
    assert led.execute("select count(*) from football_model_pick_entries").fetchone()[0] == 0
    recon.build_live_issued_ledger(dash, dry_run=False, ledger_conn=led)
    recon.build_live_issued_ledger(dash, dry_run=False, ledger_conn=led)
    assert led.execute("select count(*) from football_model_pick_entries").fetchone()[0] == 2  # AH + corners, deduped
    led.close()
    dash.close()


def test_timeline_includes_zero_fixture_and_replay_pending_days():
    fixtures = [
        {"fixture_id": "fx1", "kickoff_utc": "2026-09-20T15:00:00+00:00"},
        {"fixture_id": "fx2", "kickoff_utc": "2026-09-22T15:00:00+00:00"},  # no prediction -> replay pending
    ]
    predictions = [{"fixture_id": "fx1", "status": "READY"}]
    ledger_rows = [
        {"fixture_id": "fx1", "market": "asian_handicap", "selection": "HOME", "kickoff_utc": "2026-09-20T15:00:00+00:00", "prediction_generated_at": "2026-09-19T10:00:00+00:00", "trade_state": "FULL_WIN"},
    ]
    timeline = recon.build_daily_timeline(
        ledger_rows=ledger_rows,
        predictions=predictions,
        fixtures=fixtures,
        first_day="2026-09-19",
        last_day="2026-09-22",
    )
    days = {d["date"]: d for d in timeline["days"]}
    assert len(timeline["days"]) == 4  # every calendar day retained, incl. 09-21 with nothing
    assert days["2026-09-19"]["origin_status"] == "NO_FIXTURES"
    assert days["2026-09-20"]["origin_status"] == "LIVE_ISSUED"
    assert days["2026-09-21"]["origin_status"] == "NO_FIXTURES"
    assert days["2026-09-22"]["origin_status"] == "REPLAY_PENDING"
    assert days["2026-09-20"]["market_evidence"] == "NO_MARKET_EVIDENCE"
    assert timeline["labels"]["latest_model_pick_issued_day"] == "2026-09-19"
    assert timeline["labels"]["latest_model_pick_scheduled_kickoff_day"] == "2026-09-20"
    assert timeline["labels"]["latest_settled_model_pick_fixture_day"] == "2026-09-20"


def _pick_payload_rows():
    return _ah_rows() + _corner_rows()


def test_immutable_identical_retry_is_noop_and_conflicts_raise(tmp_path):
    conn = _ledger_conn(tmp_path)
    ledger.record_model_pick(conn, _pick(), initialize_schema=False)
    conn.commit()
    # identical retry -> noop
    ledger.record_model_pick(conn, _pick(), initialize_schema=False)
    assert conn.execute("select count(*) from football_model_pick_entries").fetchone()[0] == 1
    # changed line
    with pytest.raises(sqlite3.IntegrityError, match="MODEL_PICK_IMMUTABLE_CONFLICT"):
        ledger.record_model_pick(conn, _pick(line=-0.5), initialize_schema=False)
    # changed selection
    with pytest.raises(sqlite3.IntegrityError, match="MODEL_PICK_IMMUTABLE_CONFLICT"):
        ledger.record_model_pick(conn, _pick(selection="AWAY"), initialize_schema=False)
    # changed lineage
    with pytest.raises(sqlite3.IntegrityError, match="MODEL_PICK_IMMUTABLE_CONFLICT"):
        ledger.record_model_pick(conn, _pick(prediction_id="prediction_other"), initialize_schema=False)
    # changed origin
    with pytest.raises(sqlite3.IntegrityError, match="MODEL_PICK_IMMUTABLE_CONFLICT"):
        ledger.record_model_pick(conn, _pick(record_origin=ledger.RECORD_ORIGIN_CURRENT_SYSTEM_REPLAY), initialize_schema=False)
    # existing row untouched
    row = ledger.fetch_model_pick_rows(conn, initialize_schema=False)[0]
    assert row["line"] == -0.25
    assert row["selection"] == "HOME"
    conn.close()


def test_settlement_is_one_way(tmp_path):
    conn = _ledger_conn(tmp_path)
    ledger.record_model_pick(conn, _pick(), initialize_schema=False)
    conn.commit()
    ledger.settle_rows(conn, {"fx1": {"home_goals": 3, "away_goals": 1}}, initialize_schema=False)
    conn.commit()
    state = ledger.fetch_model_pick_rows(conn, initialize_schema=False)[0]["trade_state"]
    assert state == "FULL_WIN"
    # identical settlement -> noop
    ledger.settle_rows(conn, {"fx1": {"home_goals": 3, "away_goals": 1}}, initialize_schema=False)
    assert ledger.fetch_model_pick_rows(conn, initialize_schema=False)[0]["trade_state"] == "FULL_WIN"
    # conflicting settlement -> error, not silent overwrite
    with pytest.raises(sqlite3.IntegrityError, match="MODEL_PICK_SETTLEMENT_CONFLICT"):
        ledger.settle_rows(conn, {"fx1": {"home_goals": 0, "away_goals": 3}}, initialize_schema=False)
    assert ledger.fetch_model_pick_rows(conn, initialize_schema=False)[0]["trade_state"] == "FULL_WIN"
    conn.close()


def test_timeline_ends_at_today_and_lists_upcoming_separately(tmp_path):
    fixtures = [
        {"fixture_id": "fx_today", "kickoff_utc": "2026-10-05T15:00:00+00:00"},
        {"fixture_id": "fx_future", "kickoff_utc": "2026-10-21T15:00:00+00:00"},
    ]
    ledger_rows = [
        {"fixture_id": "fx_future", "market": "asian_handicap", "selection": "HOME", "kickoff_utc": "2026-10-21T15:00:00+00:00", "prediction_generated_at": "2026-10-05T09:00:00+00:00", "trade_state": None},
    ]
    timeline = recon.build_daily_timeline(
        ledger_rows=ledger_rows,
        predictions=[{"fixture_id": "fx_future", "status": "READY"}],
        fixtures=fixtures,
        first_day="2026-10-05",
        last_day="2026-10-21",
        today=date(2026, 10, 5),
    )
    assert timeline["days"][-1]["date"] == "2026-10-05"  # historical ends today
    assert timeline["upcoming_kickoff_days"] == ["2026-10-21"]
    assert timeline["labels"]["latest_model_pick_issued_day"] == "2026-10-05"
    assert timeline["labels"]["latest_model_pick_scheduled_kickoff_day"] == "2026-10-21"


def test_market_evidence_is_not_inferred_from_bet_count():
    fixtures = [{"fixture_id": "fx1", "kickoff_utc": "2026-09-20T15:00:00+00:00"}]
    # A) evidence available, 0 bets -> NO_BET (NOT NO_MARKET_EVIDENCE)
    a = recon.build_daily_timeline(
        ledger_rows=[], predictions=[], fixtures=fixtures,
        value_bets_by_day={}, market_evidence_by_day={"2026-09-20": recon.MARKET_EVIDENCE_AVAILABLE},
        first_day="2026-09-20", last_day="2026-09-20", today=date(2026, 9, 20),
    )["days"][0]
    assert a["market_evidence"] == recon.MARKET_EVIDENCE_AVAILABLE
    assert a["value_bet_status"] == "NO_BET"
    # B) no evidence, 0 bets -> NO_MARKET_EVIDENCE / NOT_EVALUATED
    b = recon.build_daily_timeline(
        ledger_rows=[], predictions=[], fixtures=fixtures,
        first_day="2026-09-20", last_day="2026-09-20", today=date(2026, 9, 20),
    )["days"][0]
    assert b["market_evidence"] == recon.NO_MARKET_EVIDENCE
    assert b["value_bet_status"] == "NOT_EVALUATED"
    # C) evidence available, 1 bet -> QUALIFIED
    c = recon.build_daily_timeline(
        ledger_rows=[], predictions=[], fixtures=fixtures,
        value_bets_by_day={"2026-09-20": 1}, market_evidence_by_day={"2026-09-20": recon.MARKET_EVIDENCE_AVAILABLE},
        first_day="2026-09-20", last_day="2026-09-20", today=date(2026, 9, 20),
    )["days"][0]
    assert c["market_evidence"] == recon.MARKET_EVIDENCE_AVAILABLE
    assert c["value_bet_status"] == "QUALIFIED"


def test_daily_totals_count_fixtures_not_prediction_versions():
    fixtures = [{"fixture_id": "fx1", "kickoff_utc": "2026-09-20T15:00:00+00:00"}]
    predictions = [
        {"fixture_id": "fx1", "status": "READY"},
        {"fixture_id": "fx1", "status": "READY"},  # second version, same fixture
        {"fixture_id": "fx1", "status": "BLOCKED"},
    ]
    day = recon.build_daily_timeline(
        ledger_rows=[], predictions=predictions, fixtures=fixtures,
        first_day="2026-09-20", last_day="2026-09-20", today=date(2026, 9, 20),
    )["days"][0]
    assert day["ready_fixtures"] == 1
    assert day["ready_prediction_versions"] == 2
    assert day["blocked_fixtures"] == 0  # fixture resolved READY, not blocked
    assert day["blocked_prediction_versions"] == 1


def test_no_market_evidence_is_distinct_from_no_bet():
    assert recon.NO_MARKET_EVIDENCE == "NO_MARKET_EVIDENCE"
    assert recon.NO_MARKET_EVIDENCE != "NO_BET"
    timeline = recon.build_daily_timeline(ledger_rows=[], predictions=[], fixtures=[], first_day="2026-09-20", last_day="2026-09-20")
    assert timeline["days"][0]["market_evidence"] == "NO_MARKET_EVIDENCE"


def test_prospective_boundary_and_shadow_unchanged():
    from sports_ai.storage import football_prospective_cohort as cohort

    assert cohort.DEFAULT_EFFECTIVE_FROM == "2026-09-26T10:16:20.567997950+00:00"
    from sports_ai.market_data import five_dollar_shadow as shadow

    assert shadow.SHADOW_COMPETITIONS == frozenset({"FRA1"})
