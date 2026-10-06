"""Rendered-UI tests for the MODEL PICKS vs VALUE BETS split (Jinja render)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import sports_ai.dashboard.services  # noqa: F401
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sports_ai.dashboard import model_pick_history as mph

_TEMPLATES = Path(mph.__file__).parent / "templates"
_ENV = Environment(loader=FileSystemLoader(str(_TEMPLATES)), autoescape=select_autoescape(["html"]))

_COLUMNS = (
    "model_pick_id, fixture_id, canonical_fixture_id, competition_id, competition_name, home_team_name, "
    "away_team_name, kickoff_utc, market, selection, line, model_probability, model_fair_price, record_origin, "
    "prediction_generated_at, trade_state"
)


def _row(**over):
    base = {
        "model_pick_id": "mp", "fixture_id": "fx", "canonical_fixture_id": "c", "competition_id": "ENG1",
        "competition_name": "Premier League", "home_team_name": "Liverpool", "away_team_name": "Chelsea",
        "kickoff_utc": "2026-09-20T15:00:00+00:00", "market": "asian_handicap", "selection": "HOME", "line": -0.25,
        "model_probability": 0.564, "model_fair_price": 1.77, "record_origin": "LIVE_ISSUED",
        "prediction_generated_at": "2026-09-19T10:00:00+00:00", "trade_state": None,
    }
    base.update(over)
    return base


def _payload(tmp_path):
    path = tmp_path / "mpl.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE football_model_pick_entries ({_COLUMNS})")
    for row in (
        _row(trade_state="FULL_WIN"),
        _row(model_pick_id="mp_pending", fixture_id="fx2", kickoff_utc="2026-08-22T11:30:00+00:00"),
        _row(model_pick_id="mp_future", fixture_id="fx3", kickoff_utc="2026-10-21T15:00:00+00:00"),
    ):
        conn.execute(
            f"INSERT INTO football_model_pick_entries ({_COLUMNS}) VALUES "
            "(:model_pick_id,:fixture_id,:canonical_fixture_id,:competition_id,:competition_name,:home_team_name,"
            ":away_team_name,:kickoff_utc,:market,:selection,:line,:model_probability,:model_fair_price,"
            ":record_origin,:prediction_generated_at,:trade_state)",
            row,
        )
    conn.commit()
    conn.close()
    return mph.build_model_pick_history_payload(model_pick_db_path=path, timezone_name="UTC", now=datetime(2026, 10, 6, tzinfo=timezone.utc))


class _Any:
    """Permissive stub for unrelated payload keys (renders empty, iterates empty)."""

    def __getattr__(self, _name):
        return self

    def __getitem__(self, _key):
        return self

    def __len__(self):
        return 0

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return False

    def __str__(self):
        return ""

    def __html__(self):
        return ""

    def __format__(self, _spec):
        return ""

    def __float__(self):
        return 0.0


class _Payload(dict):
    def __missing__(self, _key):
        return _Any()


def _render_performance(model_pick_payload):
    context = _Payload()
    context["model_pick_history"] = model_pick_payload
    context["client_history"] = {"available": False}
    return _ENV.get_template("performance_content_clean.html").render(payload=context)


def test_performance_renders_model_picks_before_value_bets(tmp_path):
    html = _render_performance(_payload(tmp_path))
    assert "MODEL PICKS" in html.upper()
    assert "VALUE BETS" in html.upper()
    assert html.upper().index("MODEL PICKS") < html.upper().index("VALUE BETS")


def test_performance_renders_metrics_and_labels(tmp_path):
    html = _render_performance(_payload(tmp_path))
    assert "Weighted Success" in html
    assert "Asian Handicap" in html
    assert "Corners" in html
    assert "RESULT PENDING" in html
    assert "UPCOMING" in html
    assert "model reference line" in html.lower()


def test_model_picks_section_has_no_roi(tmp_path):
    payload = _payload(tmp_path)
    section = _ENV.get_template("_model_picks_section.html").render(payload={"model_pick_history": payload})
    assert "ROI" not in section.upper()
    assert "P/L" not in section.upper()
    assert "CLV" not in section.upper()


def test_future_pick_excluded_from_daily_history_but_in_upcoming(tmp_path):
    payload = _payload(tmp_path)
    html = _ENV.get_template("_model_picks_section.html").render(payload={"model_pick_history": payload})
    # 2026-10-21 is the only future pick; it must appear (upcoming) but not as a daily-history date row
    assert "2026-10-21" in html
    daily = [d["date"] for d in payload["recent_days"]]
    assert "2026-10-21" not in daily


def test_open_result_never_rendered_as_loss(tmp_path):
    payload = _payload(tmp_path)
    section = _ENV.get_template("_model_picks_section.html").render(payload={"model_pick_history": payload})
    assert "FULL LOSS" not in section  # no loss rows in the seed
    assert "RESULT PENDING" in section


def test_home_card_and_api_contract(tmp_path):
    home_src = (_TEMPLATES / "dashboard_sections_clean.html").read_text(encoding="utf-8")
    assert 'id="model-picks-card"' in home_src
    assert "ACTIVE" in home_src
    assert "/model-performance#model-picks" in home_src
    payload = _payload(tmp_path)
    for key in ("generated_at", "policy", "summary", "by_market", "labels", "recent_days", "recent_settled", "result_pending", "upcoming"):
        assert key in payload
