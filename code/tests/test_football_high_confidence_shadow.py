"""High-confidence AH shadow lane: forward-only, isolated, immutable, honest abstentions."""

from __future__ import annotations

import inspect
import json
import sqlite3
from pathlib import Path

import sports_ai.dashboard.services  # noqa: F401  (pre-import breaks the market_data/dashboard cycle)
import pytest
from sports_ai.dashboard import high_confidence_shadow as hc

KICKOFF = "2026-10-17T14:00:00+00:00"
BEFORE = "2026-10-16T09:00:00+00:00"
AFTER = "2026-10-17T16:00:00+00:00"
AF_V2 = "api_football_current_market_provider_v2"
AF_V1 = "api_football_current_market_provider_v1"


def _ah_rows(q_by_line):
    rows = []
    for line, q_home in q_by_line.items():
        for sel, q in (("HOME", q_home), ("AWAY", 1 - q_home)):
            rows.append({"market_family": "asian_handicap", "line": line, "selection": sel,
                         "fair_decimal_price": 1 / q, "effective_win_stake_probability": q * 0.95})
    rows.append({"market_family": "corner_total", "line": 9.5, "selection": "OVER", "fair_decimal_price": 1.2,
                 "effective_win_stake_probability": 0.83})
    return rows


DEFAULT_LINES = {-1.0: 0.40, -0.5: 0.52, 0.0: 0.62, 0.5: 0.73, 1.0: 0.81}


def _payload(lines=DEFAULT_LINES, home_n=10, away_n=10, blockers=(), resolved=True):
    def hist(n, venue):
        return {"available": n > 0, "venue": venue, "matches_observed_by_statistic": {"shots": n, "shots_on_target": n, "corners": n}}

    return {
        "blockers": list(blockers),
        "team_resolution": {side: ({"canonical_team_id": f"eng1_{side}"} if resolved else {}) for side in ("home", "away")},
        "model_history": {"home": hist(home_n, "home"), "away": hist(away_n, "away")},
        "model_versions": {"current_snapshot_model_version": "football_dashboard_current_snapshot_v1"},
        "market_rows": _ah_rows(lines),
    }


@pytest.fixture
def dbs(tmp_path):
    dash = sqlite3.connect(tmp_path / "dash.sqlite")
    dash.row_factory = sqlite3.Row
    dash.execute("CREATE TABLE dashboard_fixtures (fixture_id TEXT PRIMARY KEY, competition_id TEXT, home_team_name TEXT, "
                 "away_team_name TEXT, kickoff_utc TEXT, status TEXT, home_score INTEGER, away_score INTEGER)")
    dash.execute("CREATE TABLE dashboard_predictions (prediction_id TEXT PRIMARY KEY, fixture_id TEXT, status TEXT, "
                 "generated_at TEXT, model_as_of TEXT, model_snapshot_id TEXT, payload_json TEXT)")
    paper = sqlite3.connect(tmp_path / "paper.sqlite")
    paper.row_factory = sqlite3.Row
    paper.execute("CREATE TABLE market_snapshots (fixture_id TEXT, provider TEXT, bookmaker TEXT, market TEXT, selection TEXT, "
                  "line REAL, odds REAL, captured_at TEXT, provider_timestamp TEXT, is_pre_match INTEGER, ingestion_version TEXT)")
    shadow = hc.connect(tmp_path / "shadow.sqlite")
    yield dash, paper, shadow, tmp_path
    for c in (dash, paper, shadow):
        c.close()


def _fixture(dash, fid="api-football:1", kickoff=KICKOFF, home="Leeds", away="Fulham", status="PRE_MATCH", score=(None, None)):
    dash.execute("INSERT INTO dashboard_fixtures VALUES (?,?,?,?,?,?,?,?)", (fid, "ENG1", home, away, kickoff, status, *score))


def _prediction(dash, pid, fid="api-football:1", generated="2026-10-15T08:00:00+00:00", status="READY",
                model_as_of="2026-10-12T23:59:59+00:00", payload=None):
    dash.execute("INSERT INTO dashboard_predictions VALUES (?,?,?,?,?,?,?)",
                 (pid, fid, status, generated, model_as_of, "snap1", json.dumps(payload or _payload())))
    dash.commit()


def _entries(shadow):
    return [dict(r) for r in shadow.execute("SELECT * FROM hc_shadow_entries")]


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv(hc.ENABLE_ENV_VAR, raising=False)
    assert hc.enabled() is False


def test_issues_longest_fair_line_at_threshold_with_provenance(dbs):
    dash, paper, shadow, _ = dbs
    _fixture(dash)
    _prediction(dash, "p1")
    out = hc.issue_high_confidence_shadow(dash, shadow, paper, now=BEFORE)
    assert out["issued"] == 1
    (e,) = _entries(shadow)
    assert (e["line"], e["selection"]) == (0.5, "HOME")  # q 0.73 >= 0.70 with the longest fair price
    assert e["win_probability_ex_push"] == pytest.approx(0.73)
    assert e["model_fair_price"] == pytest.approx(1 / 0.73)
    assert e["prediction_id"] == "p1" and e["model_as_of"].startswith("2026-10-12")
    assert e["issued_at"] < e["kickoff_utc"]
    assert e["calibration_id"] == hc.CALIBRATION_ID and e["threshold"] == 0.70
    assert e["home_venue_matches"] == 10 and e["offered_price"] is None


@pytest.mark.parametrize("now", [KICKOFF, AFTER])
def test_never_issues_at_or_after_kickoff(dbs, now):
    dash, paper, shadow, _ = dbs
    _fixture(dash)
    _prediction(dash, "p1")
    out = hc.issue_high_confidence_shadow(dash, shadow, paper, now=now)
    assert out["issued"] == 0 and _entries(shadow) == []


def test_rerun_and_restart_never_duplicate_and_later_versions_do_not_change_the_pick(dbs):
    dash, paper, shadow, tmp = dbs
    _fixture(dash)
    _prediction(dash, "p1")
    assert hc.issue_high_confidence_shadow(dash, shadow, paper, now=BEFORE)["issued"] == 1
    _prediction(dash, "p2", generated="2026-10-16T10:00:00+00:00", payload=_payload(lines={0.25: 0.71, -0.25: 0.5}))
    restarted = hc.connect(tmp / "shadow.sqlite")
    second = hc.issue_high_confidence_shadow(dash, restarted, paper, now="2026-10-16T11:00:00+00:00")
    assert second["issued"] == 0 and second["already_issued"] == 1
    (e,) = _entries(restarted)
    assert (e["prediction_id"], e["line"]) == ("p1", 0.5)
    restarted.close()


def test_frozen_source_is_first_pre_kickoff_ready_and_latest_must_be_ready(dbs):
    dash, paper, shadow, _ = dbs
    _fixture(dash)
    _prediction(dash, "p_ready", generated="2026-10-15T06:00:00+00:00")
    _prediction(dash, "p_blocked", generated="2026-10-15T08:00:00+00:00", status="BLOCKED")
    out = hc.issue_high_confidence_shadow(dash, shadow, paper, now=BEFORE)
    assert out["issued"] == 0 and out["abstained"] == {hc.ABSTAIN_LATEST_NOT_READY: 1}
    _prediction(dash, "p_ready_again", generated="2026-10-16T08:00:00+00:00")
    assert hc.issue_high_confidence_shadow(dash, shadow, paper, now=BEFORE)["issued"] == 1
    assert _entries(shadow)[0]["prediction_id"] == "p_ready"


@pytest.mark.parametrize(
    "payload,model_as_of,reason",
    [
        (_payload(home_n=1), "2026-10-12T23:59:59+00:00", hc.ABSTAIN_HISTORY),
        (_payload(away_n=0), "2026-10-12T23:59:59+00:00", hc.ABSTAIN_HISTORY),
        (_payload(resolved=False), "2026-10-12T23:59:59+00:00", hc.ABSTAIN_IDENTITY),
        (_payload(blockers=["MODEL_STATE_STALE"]), "2026-10-12T23:59:59+00:00", hc.ABSTAIN_BLOCKERS),
        (_payload(lines={0.0: 0.55, -0.5: 0.45}), "2026-10-12T23:59:59+00:00", hc.ABSTAIN_NO_LINE),
        (_payload(), "2026-10-17T23:59:59+00:00", hc.ABSTAIN_MODEL_STATE_NOT_BEFORE_KICKOFF),
    ],
)
def test_vetoes_produce_recorded_zero_pick_outcomes(dbs, payload, model_as_of, reason):
    dash, paper, shadow, _ = dbs
    _fixture(dash)
    _prediction(dash, "p1", payload=payload, model_as_of=model_as_of)
    out = hc.issue_high_confidence_shadow(dash, shadow, paper, now=BEFORE)
    assert out["issued"] == 0 and out["abstained"] == {reason: 1}
    assert _entries(shadow) == []
    perf = hc.performance(shadow)
    assert perf["evaluated_fixtures"] == 1 and perf["coverage"] == 0.0 and perf["abstentions"] == {reason: 1}


def test_two_provider_ids_for_one_match_give_one_pick(dbs):
    dash, paper, shadow, _ = dbs
    _fixture(dash, "api-football:1")
    _fixture(dash, "football-data.org:9", home="Leeds United", away="Fulham")  # same canonical identity
    _prediction(dash, "p_af", fid="api-football:1")
    _prediction(dash, "p_fdo", fid="football-data.org:9")
    out = hc.issue_high_confidence_shadow(dash, shadow, paper, now=BEFORE)
    assert out["issued"] == 1 and out["abstained"] == {hc.ABSTAIN_DUPLICATE: 1}
    assert [e["fixture_id"] for e in _entries(shadow)] == ["api-football:1"]


def test_offered_price_uses_only_fresh_eligible_pre_kickoff_rows_for_the_exact_line(dbs):
    dash, paper, shadow, _ = dbs
    _fixture(dash)
    _prediction(dash, "p1")
    rows = [
        ("Pinnacle", 1.36, "2026-10-16T08:30:00+00:00", 1, AF_V2, 0.5, "HOME"),   # eligible
        ("Bet365", 1.33, "2026-10-16T08:40:00+00:00", 1, AF_V2, 0.5, "HOME"),     # eligible
        ("Legacy", 1.90, "2026-10-16T08:45:00+00:00", 1, AF_V1, 0.5, "HOME"),     # legacy v1 AH: excluded
        ("Stale", 1.80, "2026-10-15T08:00:00+00:00", 1, AF_V2, 0.5, "HOME"),      # older than freshness window
        ("InPlay", 1.70, "2026-10-16T08:50:00+00:00", 0, AF_V2, 0.5, "HOME"),     # not pre-match
        ("OtherLine", 1.50, "2026-10-16T08:50:00+00:00", 1, AF_V2, 0.25, "HOME"), # different line
    ]
    for book, odds, cap, pre, ver, line, sel in rows:
        paper.execute("INSERT INTO market_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      ("api-football:1", "api-football", book, "asian_handicap", sel, line, odds, cap, cap, pre, ver))
    paper.commit()
    hc.issue_high_confidence_shadow(dash, shadow, paper, now=BEFORE)
    (e,) = _entries(shadow)
    assert (e["offered_price"], e["offered_bookmaker"], e["offered_book_count"]) == (1.36, "Pinnacle", 2)
    assert e["offered_ingestion_version"] == AF_V2
    assert e["model_fair_price"] == pytest.approx(1 / 0.73)  # model fair is never replaced by the offered price


def test_entries_are_immutable_and_settle_once_from_verified_truth(dbs):
    dash, paper, shadow, _ = dbs
    _fixture(dash)
    _prediction(dash, "p1")
    hc.issue_high_confidence_shadow(dash, shadow, paper, now=BEFORE)
    with pytest.raises(sqlite3.DatabaseError, match="HC_SHADOW_IMMUTABLE"):
        shadow.execute("UPDATE hc_shadow_entries SET line = 1.0")
    with pytest.raises(sqlite3.DatabaseError, match="HC_SHADOW_IMMUTABLE"):
        shadow.execute("DELETE FROM hc_shadow_entries")
    shadow.rollback()

    assert hc.settle_high_confidence_shadow(dash, shadow, now=AFTER)["applied"] == 0  # not final yet
    dash.execute("UPDATE dashboard_fixtures SET status = 'FINISHED', home_score = 0, away_score = 1")
    dash.commit()
    assert hc.settle_high_confidence_shadow(dash, shadow, now=AFTER)["applied"] == 1
    assert _entries(shadow)[0]["trade_state"] == "FULL_LOSS"  # HOME +0.5 at 0-1: margin -0.5
    assert hc.settle_high_confidence_shadow(dash, shadow, now=AFTER)["applied"] == 0  # idempotent
    with pytest.raises(sqlite3.DatabaseError, match="HC_SHADOW_IMMUTABLE"):
        shadow.execute("UPDATE hc_shadow_entries SET trade_state = 'FULL_WIN'")
    shadow.rollback()
    perf = hc.performance(shadow)
    assert perf["settled"] == 1 and perf["win_share_stake_weighted"] == 0.0
    assert perf["mean_q_settled"] == pytest.approx(0.73) and perf["insufficient_sample"] is True
    assert perf["value_claim"].startswith("NONE")


def test_lane_is_isolated_from_model_picks_and_value_bets(dbs):
    dash, paper, shadow, tmp = dbs
    _fixture(dash)
    _prediction(dash, "p1")
    dash_before, paper_before = (tmp / "dash.sqlite").read_bytes(), (tmp / "paper.sqlite").read_bytes()
    hc.issue_high_confidence_shadow(dash, shadow, paper, now=BEFORE)
    assert (tmp / "dash.sqlite").read_bytes() == dash_before
    assert (tmp / "paper.sqlite").read_bytes() == paper_before
    assert not (tmp / "football_model_picks.sqlite").exists()
    source = inspect.getsource(hc)
    for forbidden in ("model_pick_policy", "football_model_pick_ledger", "record_paper_trade", "VALUE_BET", "record_model_pick"):
        assert forbidden not in source
    assert hc.default_db_path(tmp / "football_model_picks.sqlite").name == hc.DEFAULT_DB_FILENAME


def test_select_line_requires_threshold_and_prefers_longest_fair():
    rows = _ah_rows({0.5: 0.73, 1.0: 0.81, -0.5: 0.69})
    pick = hc.select_high_confidence_line(rows)
    assert (pick["line"], pick["selection"]) == (0.5, "HOME")
    assert hc.select_high_confidence_line(_ah_rows({0.0: 0.6, 0.25: 0.68})) is None
    assert hc.select_high_confidence_line([r for r in rows if r["market_family"] != "asian_handicap"]) is None
