"""September/October forward-pick month auditor contract tests (no provider/network)."""
from __future__ import annotations

import json
import sqlite3

import pytest

from sports_ai.dashboard import football_period_comparison as audit


def pick(ident, kickoff, state="FULL_WIN", *, market="asian_handicap",
         issued="2026-09-20T00:00:00Z", origin="LIVE_ISSUED", canonical=None):
    return {
        "model_pick_id": ident, "canonical_fixture_id": canonical,
        "policy_id": audit.POLICY_ID, "record_origin": origin,
        "kickoff_utc": kickoff, "created_at": issued,
        "prediction_generated_at": issued, "model_as_of": "2026-09-19T00:00:00Z",
        "competition_id": "ENG1", "market": market, "trade_state": state,
    }


def test_september_vs_october_cohorts_do_not_mix_replay_or_retrospective():
    rows = [
        pick("s1", "2026-09-28T12:00:00Z"),
        pick("s2", "2026-09-28T12:00:00Z", "FULL_LOSS", market="corner_total"),
        pick("s3", "2026-09-29T12:00:00Z", "HALF_WIN"),
        pick("o1", "2026-10-06T13:00:00Z", issued="2026-10-05T09:00:00Z"),
        pick("w1", "2026-10-10T12:00:00Z", "HALF_LOSS", issued="2026-10-06T09:00:00Z"),
        pick("w2", "2026-10-10T12:00:00Z", "PUSH", market="corner_total", issued="2026-10-06T09:00:00Z"),
        pick("w3", "2026-10-11T12:00:00Z", None, market="corner_total", issued="2026-10-06T09:00:00Z"),
        pick("late", "2026-10-11T12:00:00Z", issued="2026-10-11T12:00:00Z"),
        pick("replay", "2026-10-11T12:00:00Z", origin="CURRENT_SYSTEM_REPLAY"),
    ]
    result = audit.audit(rows)
    assert result["status"] == "RECONCILED_OFFLINE_COPY"
    sep = result["periods"]["september_2026"]["summary"]
    assert sep["issued"] == 3
    assert sep["stake_weighted_win_share"] == round(1.5 / 2.5, 6)
    oct1 = result["periods"]["october_1_9"]["summary"]
    assert oct1["issued"] == 1
    weekend = result["periods"]["weekend_october_10_11"]["summary"]
    assert (weekend["issued"], weekend["settled"], weekend["pending"]) == (3, 2, 1)
    assert weekend["stake_weighted_win_share"] == 0
    assert result["periods"]["october_1_14_overlapping_total"]["summary"]["issued"] == 4
    assert result["excluded"] == {"not_live_issued": 1, "retrospective_created_at": 1}


def test_zero_sample_is_null_not_zero():
    r = audit.audit([pick("late", "2026-10-08T10:00:00Z", issued="2026-10-07T10:00:00Z")])
    assert r["periods"]["september_2026"]["summary"]["stake_weighted_win_share"] is None
    assert r["periods"]["september_2026"]["result_completeness"] == "NO_FORWARD_PICKS_IN_LEDGER_COPY"


def test_uk_kickoff_day_boundary_and_lead_time():
    r = audit.audit([pick("boundary", "2026-10-11T23:30:00Z", issued="2026-10-06T12:00:00Z")])
    assert r["periods"]["weekend_october_10_11"]["summary"]["issued"] == 0
    assert r["periods"]["october_12_14"]["summary"]["issued"] == 1
    assert r["periods"]["october_12_14"]["summary"]["issued_48h_or_more_before_kickoff"] == 1


def test_canonical_duplicate_and_future_model_state_fail_closed():
    a = pick("one", "2026-10-10T12:00:00Z", canonical="same", issued="2026-10-06T00:00:00Z")
    b = pick("two", "2026-10-10T12:00:00Z", canonical="same", issued="2026-10-06T00:00:00Z")
    c = pick("three", "2026-10-10T12:00:00Z", issued="2026-10-06T00:00:00Z")
    c["model_as_of"] = "2026-10-11T23:00:00Z"
    r = audit.audit([a, b, c])
    assert r["status"] == "INTEGRITY_BLOCKED"
    assert {v["reason"] for v in r["integrity_failures"]} == {
        "DUPLICATE_CANONICAL_MATCH_MARKET", "MODEL_STATE_NOT_PREKICKOFF"
    }


def test_voids_distinct_from_pending_and_bad_state_blocked():
    a = pick("a", "2026-10-10T12:00:00Z", "VOID", issued="2026-10-06T00:00:00Z")
    b = pick("b", "2026-10-10T12:00:00Z", None, market="corner_total", issued="2026-10-06T00:00:00Z")
    r = audit.audit([a, b])
    s = r["periods"]["weekend_october_10_11"]["summary"]
    assert s["issued"] == 2 and s["void"] == 1 and s["pending"] == 1
    assert s["stake_weighted_win_share"] is None
    bad = pick("c", "2026-10-10T12:00:00Z", "SETTLED_SOMEHOW", issued="2026-10-06T00:00:00Z")
    assert audit.audit([bad])["status"] == "INTEGRITY_BLOCKED"


def test_reads_offline_backup_without_writing_and_never_creates_missing_db(tmp_path):
    db = tmp_path / "football_model_picks.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE football_model_pick_entries "
        "(model_pick_id TEXT, canonical_fixture_id TEXT, policy_id TEXT, "
        "record_origin TEXT, kickoff_utc TEXT, created_at TEXT, "
        "prediction_generated_at TEXT, model_as_of TEXT, "
        "competition_id TEXT, market TEXT, trade_state TEXT)"
    )
    p = pick("one", "2026-10-10T12:00:00Z", issued="2026-10-06T00:00:00Z")
    con.execute("INSERT INTO football_model_pick_entries VALUES (?,?,?,?,?,?,?,?,?,?,?)", tuple(p[k] for k in (
        "model_pick_id", "canonical_fixture_id", "policy_id",
        "record_origin", "kickoff_utc", "created_at",
        "prediction_generated_at", "model_as_of",
        "competition_id", "market", "trade_state"
    )))
    con.commit()
    con.close()
    before = db.read_bytes()
    assert len(audit.read_offline_copy(db)) == 1
    assert db.read_bytes() == before
    with pytest.raises(FileNotFoundError):
        audit.read_offline_copy(tmp_path / "missing.sqlite")
    (tmp_path / "football_model_picks.sqlite-wal").write_bytes(b"")
    with pytest.raises(ValueError, match="NOT_STANDALONE_BACKUP"):
        audit.read_offline_copy(db)


def test_bad_snapshot_timestamp_fails_closed(tmp_path, capsys):
    rc = audit.main([
        "--ledger-copy", str(tmp_path / "missing.sqlite"),
        "--snapshot-as-of", "unverified",
    ])
    assert rc == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "INVALID_SNAPSHOT_TIMESTAMP"
