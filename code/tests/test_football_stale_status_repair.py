"""Focused tests for the approved stale-status repair lane (audited, transactional)."""

from __future__ import annotations

import sqlite3

import pytest
import sports_ai.dashboard.services  # noqa: F401
from sports_ai.dashboard import football_stale_status_repair as repair
from sports_ai.storage import football_canonical_fixture as canonical


def _dash_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE dashboard_fixtures (fixture_id TEXT PRIMARY KEY, competition_id TEXT, competition_name TEXT, "
        "home_team_id TEXT, home_team_name TEXT, away_team_id TEXT, away_team_name TEXT, kickoff_utc TEXT, "
        "status TEXT, home_score INTEGER, away_score INTEGER)"
    )
    conn.execute(
        "INSERT INTO dashboard_fixtures VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("football-data.org:999", "ENG1", "Premier League", "349", "Ipswich Town", "71", "Sunderland",
         "2026-08-22T11:30:00+00:00", "LIVE", 2, 1),
    )
    conn.commit()
    return conn


def _ledger_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE football_model_pick_entries (model_pick_id TEXT PRIMARY KEY, fixture_id TEXT, market TEXT, "
        "line REAL, selection TEXT, kickoff_utc TEXT, trade_state TEXT)"
    )
    conn.execute(
        "INSERT INTO football_model_pick_entries VALUES (?,?,?,?,?,?,?)",
        ("mp1", "football-data.org:999", "asian_handicap", 0.0, "HOME", "2026-08-22T11:30:00+00:00", None),
    )
    conn.commit()
    return conn


def _research_event(**over):
    base = {
        "event_id": "evt_1", "competition_id": "ENG1", "match_date": "2026-08-22", "kickoff_time": "12:30",
        "home_team": "Ipswich", "away_team": "Sunderland", "home_goals": 2, "away_goals": 1, "result_code": "H",
    }
    base.update(over)
    return base


def _plan():
    dash, led = _dash_db(), _ledger_db()
    result = repair.build_repair_plan(dash, led, [_research_event()])
    dash.close()
    led.close()
    return result


def test_plan_selects_only_eligible_and_hash_is_deterministic():
    a = _plan()
    b = _plan()
    assert len(a["plan"]) == 1
    assert a["plan"][0]["new_status"] == "FINISHED"
    assert a["plan"][0]["old_status"] == "LIVE"
    assert a["manifest_hash"] == b["manifest_hash"]


def test_plan_excludes_dashboard_score_conflict():
    dash, led = _dash_db(), _ledger_db()
    result = repair.build_repair_plan(dash, led, [_research_event(home_goals=0, away_goals=0, result_code="D")])
    assert result["plan"] == []
    dash.close()
    led.close()


def test_apply_updates_status_only_and_writes_audit():
    dash = _dash_db()
    plan_result = _plan()
    result = repair.apply_repairs(dash, plan_result["plan"], expected_manifest_hash=plan_result["manifest_hash"])
    assert result["applied"] == 1
    row = dash.execute("SELECT status, home_score, away_score FROM dashboard_fixtures").fetchone()
    assert row["status"] == "FINISHED"
    assert (row["home_score"], row["away_score"]) == (2, 1)  # score never rewritten
    audit = dash.execute("SELECT * FROM football_fixture_result_reconciliations").fetchall()
    assert len(audit) == 1
    assert audit[0]["old_status"] == "LIVE" and audit[0]["new_status"] == "FINISHED"
    # idempotent: rebuilding the plan on the repaired DB yields nothing to do
    led = _ledger_db()
    rebuilt = repair.build_repair_plan(dash, led, [_research_event()])
    led.close()
    assert rebuilt["plan"] == []
    assert dash.execute("SELECT COUNT(*) FROM football_fixture_result_reconciliations").fetchone()[0] == 1
    dash.close()


def test_apply_hash_mismatch_aborts():
    dash = _dash_db()
    plan_result = _plan()
    with pytest.raises(RuntimeError, match="MANIFEST_HASH_MISMATCH"):
        repair.apply_repairs(dash, plan_result["plan"], expected_manifest_hash="deadbeef")
    assert dash.execute("SELECT status FROM dashboard_fixtures").fetchone()["status"] == "LIVE"
    dash.close()


def test_apply_precondition_drift_rolls_back():
    dash = _dash_db()
    plan_result = _plan()
    # status drifts between dry-run and apply
    dash.execute("UPDATE dashboard_fixtures SET status = 'FINISHED' WHERE fixture_id = 'football-data.org:999'")
    dash.commit()
    with pytest.raises(sqlite3.IntegrityError, match="REPAIR_PRECONDITION_FAILED"):
        repair.apply_repairs(dash, plan_result["plan"])
    assert dash.execute("SELECT COUNT(*) FROM football_fixture_result_reconciliations").fetchone()[0] == 0
    dash.close()


def test_apply_score_drift_rolls_back():
    dash = _dash_db()
    plan_result = _plan()
    dash.execute("UPDATE dashboard_fixtures SET home_score = 5 WHERE fixture_id = 'football-data.org:999'")
    dash.commit()
    with pytest.raises(sqlite3.IntegrityError, match="REPAIR_PRECONDITION_FAILED"):
        repair.apply_repairs(dash, plan_result["plan"])
    dash.close()


def test_persist_identity_evidence_idempotent_and_conflict_fails(tmp_path):
    paper = canonical.connect(tmp_path / "paper.sqlite")
    canonical.ensure_schema(paper)
    plan = _plan()["plan"]
    assert repair.persist_identity_evidence(paper, plan) == 1
    assert repair.persist_identity_evidence(paper, plan) == 1  # idempotent
    paper.commit()
    assert paper.execute("SELECT COUNT(*) FROM fixture_provider_aliases").fetchone()[0] >= 2
    # conflicting canonical expectation fails closed
    bad = [dict(plan[0], canonical_fixture_id="not-the-real-canonical")]
    with pytest.raises(sqlite3.IntegrityError, match="CANONICAL_IDENTITY_MISMATCH"):
        repair.persist_identity_evidence(paper, bad)
    paper.close()


def test_repair_module_has_no_provider_or_paper_writes():
    import pathlib

    src = pathlib.Path(repair.__file__).read_text(encoding="utf-8")
    for forbidden in ("import httpx", "import requests", "five_dollar", "the_odds_api", "paper_trade_entries"):
        assert forbidden not in src
