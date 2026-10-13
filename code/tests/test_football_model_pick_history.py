"""Focused tests for the model-pick read-only presentation payload."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import sports_ai.dashboard.services  # noqa: F401
from sports_ai.dashboard import model_pick_history as mph

_COLUMNS = (
    "model_pick_id, fixture_id, canonical_fixture_id, competition_id, competition_name, home_team_name, "
    "away_team_name, kickoff_utc, market, selection, line, model_probability, model_fair_price, record_origin, "
    "prediction_generated_at, trade_state, created_at"
)


def _db(tmp_path):
    path = tmp_path / "mpl.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE football_model_pick_entries ({_COLUMNS})")
    return path, conn


def _row(**over):
    base = {
        "model_pick_id": "mp", "fixture_id": "fx", "canonical_fixture_id": "c", "competition_id": "ENG1",
        "competition_name": "Premier League", "home_team_name": "A", "away_team_name": "B",
        "kickoff_utc": "2026-09-20T15:00:00+00:00", "market": "asian_handicap", "selection": "HOME", "line": -0.25,
        "model_probability": 0.568, "model_fair_price": 1.76, "record_origin": "LIVE_ISSUED",
        "prediction_generated_at": "2026-09-19T10:00:00+00:00", "trade_state": None,
        "created_at": "2026-08-01T00:00:00+00:00",
    }
    base.update(over)
    return base


def _seed(tmp_path):
    path, conn = _db(tmp_path)
    rows = [
        _row(model_pick_id="ah_win", kickoff_utc="2026-09-20T15:00:00+00:00", trade_state="FULL_WIN"),
        _row(model_pick_id="ah_pending", fixture_id="fx2", kickoff_utc="2026-08-22T11:30:00+00:00", trade_state=None),
        _row(model_pick_id="ah_future", fixture_id="fx3", kickoff_utc="2026-10-21T15:00:00+00:00", trade_state=None),
        _row(model_pick_id="cn_win", fixture_id="fx4", market="corner_total", selection="OVER", line=9.5,
             kickoff_utc="2026-09-19T14:00:00+00:00", trade_state="HALF_WIN"),
        _row(model_pick_id="cn_future", fixture_id="fx5", market="corner_total", selection="UNDER", line=10.0,
             kickoff_utc="2026-10-21T15:00:00+00:00", trade_state=None),
    ]
    for row in rows:
        conn.execute(
            f"INSERT INTO football_model_pick_entries ({_COLUMNS}) VALUES "
            "(:model_pick_id,:fixture_id,:canonical_fixture_id,:competition_id,:competition_name,:home_team_name,"
            ":away_team_name,:kickoff_utc,:market,:selection,:line,:model_probability,:model_fair_price,"
            ":record_origin,:prediction_generated_at,:trade_state,:created_at)",
            row,
        )
    conn.commit()
    conn.close()
    return path


_NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


def _payload(tmp_path):
    return mph.build_model_pick_history_payload(model_pick_db_path=_seed(tmp_path), timezone_name="UTC", now=_NOW)


def test_unavailable_db_degrades_safely(tmp_path):
    payload = mph.build_model_pick_history_payload(model_pick_db_path=tmp_path / "nope.sqlite", now=_NOW)
    assert payload["available"] is False
    assert payload["summary"]["issued"] == 0
    assert payload["recent_days"] == []


def test_summary_counts_and_market_separation(tmp_path):
    payload = _payload(tmp_path)
    assert payload["available"] is True
    s = payload["summary"]
    assert (s["issued"], s["settled"], s["open"]) == (5, 2, 3)
    assert (s["full_wins"], s["half_wins"]) == (1, 1)
    ah = payload["by_market"]["asian_handicap"]
    cn = payload["by_market"]["corner_total"]
    assert (ah["issued"], ah["settled"], ah["open"]) == (3, 1, 2)
    assert (cn["issued"], cn["settled"], cn["open"]) == (2, 1, 1)
    assert ah["weighted_success_rate"] == 1.0


def test_open_future_is_upcoming_and_open_past_is_result_pending(tmp_path):
    payload = _payload(tmp_path)
    upcoming_ids = {r["model_pick_id"] for r in payload["upcoming"]}
    pending_ids = {r["model_pick_id"] for r in payload["result_pending"]}
    assert upcoming_ids == {"ah_future", "cn_future"}
    assert pending_ids == {"ah_pending"}
    assert all(r["result_state"] == "UPCOMING" for r in payload["upcoming"])
    assert all(r["result_state"] == "RESULT_PENDING" for r in payload["result_pending"])


def test_open_never_counted_as_loss_and_future_excluded_from_history(tmp_path):
    payload = _payload(tmp_path)
    for day in payload["recent_days"]:
        assert day["date"] <= "2026-10-06"
    assert all(d["date"] != "2026-10-21" for d in payload["recent_days"])
    # open rows contribute to open, never to full_losses
    assert payload["summary"]["full_losses"] == 0
    assert payload["summary"]["open"] == 3


def test_no_roi_or_bookmaker_fields(tmp_path):
    payload = _payload(tmp_path)
    blob = str(payload).lower()
    for forbidden in ("roi", "clv", "units", "edge", "bookmaker", "entry_odds"):
        assert forbidden not in blob
    assert payload["upcoming"][0]["line_source"] == "model_reference_line"
    assert payload["weighted_success_label"] == "Weighted Success"


def test_labels_live_issued_and_no_replay(tmp_path):
    payload = _payload(tmp_path)
    labels = payload["labels"]
    assert labels["latest_model_pick_issued_day"] == "2026-09-19"
    assert labels["latest_model_pick_scheduled_kickoff_day"] == "2026-10-21"
    assert labels["latest_settled_ah_fixture_day"] == "2026-09-20"
    assert labels["latest_settled_corners_fixture_day"] == "2026-09-19"
    assert all(r["record_origin"] == "LIVE_ISSUED" for r in payload["recent_settled"])


def _seed_cohorts(tmp_path):
    path, conn = _db(tmp_path)
    rows = [
        _row(model_pick_id="fwd_win", trade_state="FULL_WIN"),
        _row(model_pick_id="fwd_loss", fixture_id="fx2", trade_state="FULL_LOSS"),
        # written after kickoff by the old manual backfill; record_origin stays LIVE_ISSUED
        _row(model_pick_id="retro_win1", fixture_id="fx3", trade_state="FULL_WIN", created_at="2026-10-06T08:00:00+00:00"),
        _row(model_pick_id="retro_win2", fixture_id="fx4", trade_state="FULL_WIN", created_at="2026-10-06T08:00:00+00:00"),
        _row(model_pick_id="retro_open", fixture_id="fx5", trade_state=None, created_at="2026-10-06T08:00:00+00:00"),
        _row(model_pick_id="unknown_win", fixture_id="fx6", trade_state="FULL_WIN", created_at=None),
    ]
    for row in rows:
        conn.execute(
            f"INSERT INTO football_model_pick_entries ({_COLUMNS}) VALUES "
            "(:model_pick_id,:fixture_id,:canonical_fixture_id,:competition_id,:competition_name,:home_team_name,"
            ":away_team_name,:kickoff_utc,:market,:selection,:line,:model_probability,:model_fair_price,"
            ":record_origin,:prediction_generated_at,:trade_state,:created_at)",
            row,
        )
    conn.commit()
    conn.close()
    return mph.build_model_pick_history_payload(model_pick_db_path=path, timezone_name="UTC", now=_NOW)


def test_headline_counts_only_forward_picks(tmp_path):
    payload = _seed_cohorts(tmp_path)
    assert payload["headline_cohort"] == "FORWARD"
    s = payload["summary"]
    assert (s["issued"], s["settled"], s["full_wins"], s["full_losses"]) == (2, 2, 1, 1)
    assert s["weighted_success_rate"] == 0.5  # retrospective wins never lift the headline
    assert payload["by_market"]["asian_handicap"]["weighted_success_rate"] == 0.5
    assert payload["excluded_from_headline"] == 4
    assert [d["settled"] for d in payload["recent_days"]] == [2]


def test_retrospective_and_unknown_cohorts_retained_without_rate(tmp_path):
    payload = _seed_cohorts(tmp_path)
    retro = payload["by_issuance_timing"]["RETROSPECTIVE_BACKFILL"]
    unknown = payload["by_issuance_timing"]["UNKNOWN"]
    assert (retro["issued"], retro["settled"], retro["open"], retro["full_wins"]) == (3, 2, 1, 2)
    assert retro["weighted_success_rate"] is None
    assert (unknown["issued"], unknown["full_wins"]) == (1, 1)
    assert unknown["weighted_success_rate"] is None
    assert payload["by_issuance_timing"]["FORWARD"]["weighted_success_rate"] == 0.5
    listed = {r["model_pick_id"]: r for r in payload["recent_settled"]}
    assert listed["retro_win1"]["issuance_timing"] == "RETROSPECTIVE_BACKFILL"
    assert listed["retro_win1"]["forward_issued"] is False
    assert listed["retro_win1"]["record_origin"] == "LIVE_ISSUED"  # immutable audit field untouched
    assert listed["fwd_win"]["forward_issued"] is True


def test_section_discloses_retrospective_exclusion(tmp_path):
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader, select_autoescape

    templates = Path(mph.__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(templates)), autoescape=select_autoescape(["html"]))
    html = env.get_template("_model_picks_section.html").render(payload={"model_pick_history": _seed_cohorts(tmp_path)})
    assert "Forward Model Picks" in html
    assert "4 retrospective backfill picks" in html
    assert "Retrospective backfill" in html
    assert "Timing unknown" in html


def test_win_probability_ex_push_uses_fair_price_not_unconditional_probability(tmp_path):
    path, conn = _db(tmp_path)
    # AH 0.0 with 26% push mass: unconditional stake probability 0.397, fair 1.852 -> 54.0% excl. push.
    conn.execute(
        f"INSERT INTO football_model_pick_entries ({_COLUMNS}) VALUES "
        "(:model_pick_id,:fixture_id,:canonical_fixture_id,:competition_id,:competition_name,:home_team_name,"
        ":away_team_name,:kickoff_utc,:market,:selection,:line,:model_probability,:model_fair_price,"
        ":record_origin,:prediction_generated_at,:trade_state,:created_at)",
        _row(model_pick_id="push_line", line=0.0, model_probability=0.3973, model_fair_price=1.8523, trade_state="FULL_WIN"),
    )
    conn.commit()
    conn.close()
    payload = mph.build_model_pick_history_payload(model_pick_db_path=path, timezone_name="UTC", now=_NOW)
    row = payload["recent_settled"][0]
    assert row["model_probability"] == 0.3973  # stored value is untouched
    assert round(row["win_probability_ex_push"], 3) == 0.540
    assert mph.win_probability_ex_push(None) is None
    assert mph.win_probability_ex_push("inf") is None
    assert mph.win_probability_ex_push(1.0) is None


def test_stake_weighted_win_share_is_reported_beside_weighted_success(tmp_path):
    path, conn = _db(tmp_path)
    states = ["FULL_WIN", "HALF_WIN", "HALF_WIN", "HALF_LOSS", "FULL_LOSS"]
    for i, state in enumerate(states):
        conn.execute(
            f"INSERT INTO football_model_pick_entries ({_COLUMNS}) VALUES "
            "(:model_pick_id,:fixture_id,:canonical_fixture_id,:competition_id,:competition_name,:home_team_name,"
            ":away_team_name,:kickoff_utc,:market,:selection,:line,:model_probability,:model_fair_price,"
            ":record_origin,:prediction_generated_at,:trade_state,:created_at)",
            _row(model_pick_id=f"p{i}", fixture_id=f"f{i}", trade_state=state),
        )
    conn.commit()
    conn.close()
    payload = mph.build_model_pick_history_payload(model_pick_db_path=path, timezone_name="UTC", now=_NOW)
    s = payload["summary"]
    assert s["weighted_success_rate"] == round((1 + 0.5 * 2) / 5, 4)  # unchanged definition: 0.4
    assert s["stake_weighted_win_share"] == round(2.0 / (2.0 + 1.5), 4)  # 0.5714
    retro = payload["by_issuance_timing"]["RETROSPECTIVE_BACKFILL"]
    assert retro["stake_weighted_win_share"] is None  # never a rate for non-forward cohorts
