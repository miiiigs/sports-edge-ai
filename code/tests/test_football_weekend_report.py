from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from sports_ai.dashboard.football_weekend_report import (
    build_weekend_model_pick_report,
    main,
)


def _database(tmp_path, rows):
    path = tmp_path / "football-model-picks.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE football_model_pick_entries ("
        "model_pick_id TEXT, fixture_id TEXT, market TEXT, record_origin TEXT,"
        "kickoff_utc TEXT, trade_state TEXT, updated_at TEXT, created_at TEXT)"
    )
    conn.executemany(
        "INSERT INTO football_model_pick_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(ident, ident, market, origin, kickoff, state, "2026-10-12T01:00:00+00:00",
          created_at)
         for ident, market, origin, kickoff, state, *rest in rows
         for created_at in [rest[0] if rest else "2026-10-09T00:00:00+00:00"]],
    )
    conn.commit()
    conn.close()
    return path


def test_weekend_report_includes_only_live_issued_and_uk_local_kickoffs(tmp_path):
    path = _database(tmp_path, [
        ("a", "asian_handicap", "LIVE_ISSUED", "2026-10-09T23:30:00+00:00", "FULL_WIN"),
        ("b", "asian_handicap", "LIVE_ISSUED", "2026-10-10T15:00:00+00:00", "HALF_LOSS"),
        ("c", "corner_total", "LIVE_ISSUED", "2026-10-11T12:30:00+00:00", "HALF_WIN"),
        ("d", "corner_total", "LIVE_ISSUED", "2026-10-11T16:00:00+00:00", "PUSH"),
        ("e", "corner_total", "LIVE_ISSUED", "2026-10-11T17:00:00+00:00", None),
        ("replay", "asian_handicap", "CURRENT_SYSTEM_REPLAY", "2026-10-10T12:00:00+00:00", "FULL_WIN"),
        ("outside", "asian_handicap", "LIVE_ISSUED", "2026-10-11T23:30:00+00:00", "FULL_LOSS"),
    ])
    report = build_weekend_model_pick_report(
        db_path=path, start_date=date(2026, 10, 10),
        end_date=date(2026, 10, 11), timezone_name="Europe/London",
    )
    assert report["record_origin"] == "LIVE_ISSUED"
    assert report["basis"] == "kickoff_local_day"
    assert report["summary"]["issued"] == 5
    assert report["summary"]["settled"] == 4
    assert report["summary"]["open"] == 1
    assert report["summary"]["weighted_success_rate"] == 0.5
    assert report["decisive_count"] == 3
    assert report["by_market"]["asian_handicap"]["issued"] == 2
    assert report["by_market"]["corner_total"]["issued"] == 3


def test_weekend_report_blocks_duplicate_identity_and_missing_db(tmp_path):
    path = _database(tmp_path, [
        ("duplicated", "corner_total", "LIVE_ISSUED", "2026-10-10T12:00:00+00:00", "FULL_WIN"),
        ("duplicated", "corner_total", "LIVE_ISSUED", "2026-10-11T12:00:00+00:00", "FULL_LOSS"),
    ])
    with pytest.raises(ValueError, match="duplicate"):
        build_weekend_model_pick_report(
            db_path=path, start_date=date(2026, 10, 10),
            end_date=date(2026, 10, 11),
        )
    assert main([
        "--db-path", str(tmp_path / "missing.sqlite"), "--start-date", "2026-10-10",
        "--end-date", "2026-10-11",
    ]) == 2


def test_weekend_report_never_writes_and_fails_closed_on_bad_kickoff(tmp_path):
    path = _database(tmp_path, [
        ("bad", "asian_handicap", "LIVE_ISSUED", None, "FULL_WIN"),
    ])
    before = path.read_bytes()
    report = build_weekend_model_pick_report(
        db_path=path, start_date=date(2026, 10, 10), end_date=date(2026, 10, 11),
    )
    assert report["summary"]["issued"] == 0
    assert report["summary"]["weighted_success_rate"] is None
    assert report["unparseable_live_issued_kickoffs"] == 1
    assert report["complete_kickoff_population"] is False
    assert path.read_bytes() == before


def test_weekend_report_excludes_live_issued_rows_created_at_or_after_kickoff(tmp_path):
    path = _database(tmp_path, [
        ("forward", "asian_handicap", "LIVE_ISSUED", "2026-10-10T15:00:00+00:00", "FULL_WIN"),
        ("late", "asian_handicap", "LIVE_ISSUED", "2026-10-10T15:00:00+00:00", "FULL_WIN",
         "2026-10-10T16:00:00+00:00"),
        ("at_kickoff", "corner_total", "LIVE_ISSUED", "2026-10-11T12:30:00+00:00", "FULL_LOSS",
         "2026-10-11T12:30:00+00:00"),
        ("unknown", "corner_total", "LIVE_ISSUED", "2026-10-11T12:30:00+00:00", "FULL_LOSS", None),
    ])
    report = build_weekend_model_pick_report(
        db_path=path, start_date=date(2026, 10, 10), end_date=date(2026, 10, 11),
    )
    assert report["issuance_timing"] == "FORWARD"
    assert report["summary"]["issued"] == 1
    assert report["summary"]["weighted_success_rate"] == 1.0
    assert report["excluded_not_forward"] == {"RETROSPECTIVE_BACKFILL": 2, "UNKNOWN": 1}


def test_weekend_report_reports_zero_not_a_rate_when_no_forward_picks(tmp_path):
    path = _database(tmp_path, [
        ("late", "asian_handicap", "LIVE_ISSUED", "2026-10-10T15:00:00+00:00", "FULL_WIN",
         "2026-10-10T18:00:00+00:00"),
    ])
    report = build_weekend_model_pick_report(
        db_path=path, start_date=date(2026, 10, 10), end_date=date(2026, 10, 11),
    )
    assert report["summary"]["issued"] == 0
    assert report["decisive_count"] == 0
    assert report["summary"]["weighted_success_rate"] is None
    assert report["summary"]["stake_weighted_win_share"] is None
