from __future__ import annotations

import sqlite3
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from sports_ai.dashboard import client_history
from sports_ai.dashboard.client_history import build_client_history_payload


def _row(
    trade_id: str,
    fixture_id: str,
    market: str,
    *,
    edge: float,
    state: str,
    pnl: float,
    line: float | None = None,
    selection: str = "HOME",
    odds: float = 2.0,
) -> dict:
    return {
        "paper_trade_id": trade_id,
        "fixture_id": fixture_id,
        "market": market,
        "selection": selection,
        "line": line,
        "edge": edge,
        "trade_state": state,
        "net_pnl": pnl,
        "entry_odds": odds,
        "kickoff": "2026-09-02T18:45:00+00:00",
        "decision_timestamp": "2026-09-02T12:00:00+00:00",
        "updated_at": "2026-09-02T21:00:00+00:00",
        "latest_update_payload": {"stake": 1.0},
        "implied_probability_clv": None,
    }


def test_client_history_collapses_alternative_lines_and_prefers_portfolio(monkeypatch, tmp_path: Path) -> None:
    paper_path = tmp_path / "paper.sqlite"
    sqlite3.connect(paper_path).close()

    rows = [
        _row("a-goals-portfolio", "fixture-a", "goals_total", edge=0.10, state="LOST", pnl=-1.0, line=2.0, selection="UNDER"),
        _row("a-goals-higher-edge", "fixture-a", "goals_total", edge=0.25, state="WON", pnl=2.0, line=2.5, selection="UNDER", odds=3.0),
        _row("a-1x2", "fixture-a", "1x2", edge=0.18, state="WON", pnl=1.5, selection="HOME", odds=2.5),
        _row("b-ah-low", "fixture-b", "asian_handicap", edge=0.08, state="LOST", pnl=-1.0, line=-0.5, selection="AWAY"),
        _row("b-ah-best", "fixture-b", "asian_handicap", edge=0.30, state="PUSH", pnl=0.0, line=-1.0, selection="AWAY", odds=4.0),
    ]

    monkeypatch.setattr(client_history.football_paper_trading, "fetch_paper_trade_rows", lambda _conn, initialize_schema=False: rows)
    monkeypatch.setattr(client_history.football_canonical_fixture, "fetch_excluded_paper_trade_ids", lambda _conn, initialize_schema=False: set())
    monkeypatch.setattr(
        client_history.football_canonical_fixture,
        "fetch_canonical_fixture_ids_for_fixture_ids",
        lambda _conn, _fixture_ids, initialize_schema=False: {"fixture-a": "canonical-a", "fixture-b": "canonical-b"},
    )
    monkeypatch.setattr(
        client_history.football_portfolio_selection,
        "fetch_current_portfolio_selections",
        lambda _conn, **_kwargs: [
            {
                "paper_trade_id": "a-goals-portfolio",
                "selection_state": "SELECTED",
                "selection_rank": 1,
            }
        ],
    )

    payload = build_client_history_payload(
        paper_db_path=paper_path,
        dashboard_db_path=tmp_path / "missing-dashboard.sqlite",
        timezone_name="Asia/Manila",
    )

    assert payload["available"] is True
    assert payload["raw_line_count"] == 5
    assert payload["normalized_count"] == 3
    assert payload["collapsed_line_count"] == 2
    assert payload["all_time"]["wins"] == 1
    assert payload["all_time"]["losses"] == 1
    assert payload["all_time"]["pushes"] == 1
    assert payload["all_time"]["net_units"] == 0.5
    assert payload["latest_completed_day"]["bets"] == 3
    assert payload["latest_completed_day"]["matches"] == 2

    selected_ids = {row["paper_trade_id"] for row in payload["recent_settled"]}
    assert "a-goals-portfolio" in selected_ids
    assert "a-goals-higher-edge" not in selected_ids
    assert "b-ah-best" in selected_ids
    assert "b-ah-low" not in selected_ids


def test_client_history_excludes_reconciled_duplicate_representation(monkeypatch, tmp_path: Path) -> None:
    paper_path = tmp_path / "paper.sqlite"
    sqlite3.connect(paper_path).close()
    rows = [
        _row("keep", "fixture-a", "1x2", edge=0.20, state="WON", pnl=1.0),
        _row("duplicate", "fixture-a-alias", "1x2", edge=0.90, state="LOST", pnl=-1.0),
    ]

    monkeypatch.setattr(client_history.football_paper_trading, "fetch_paper_trade_rows", lambda _conn, initialize_schema=False: rows)
    monkeypatch.setattr(client_history.football_canonical_fixture, "fetch_excluded_paper_trade_ids", lambda _conn, initialize_schema=False: {"duplicate"})
    monkeypatch.setattr(
        client_history.football_canonical_fixture,
        "fetch_canonical_fixture_ids_for_fixture_ids",
        lambda _conn, _fixture_ids, initialize_schema=False: {"fixture-a": "canonical-a"},
    )
    monkeypatch.setattr(client_history.football_portfolio_selection, "fetch_current_portfolio_selections", lambda _conn, **_kwargs: [])

    payload = build_client_history_payload(
        paper_db_path=paper_path,
        dashboard_db_path=tmp_path / "missing-dashboard.sqlite",
    )

    assert payload["raw_line_count"] == 1
    assert payload["normalized_count"] == 1
    assert payload["all_time"]["wins"] == 1
    assert payload["all_time"]["losses"] == 0


def test_client_history_datetime_label_is_platform_portable() -> None:
    assert client_history._local_datetime_label("2026-09-02T18:45:00+00:00", "Asia/Manila") == "Sep 3, 2:45 AM"


def test_clean_templates_compile() -> None:
    templates = Path(__file__).resolve().parents[1] / "src" / "sports_ai" / "dashboard" / "templates"
    env = Environment(loader=FileSystemLoader(str(templates)))
    for name in (
        "base.html",
        "dashboard.html",
        "dashboard_sections_clean.html",
        "matches.html",
        "matches_table_clean.html",
        "betting_desk.html",
        "betting_desk_content_clean.html",
        "performance.html",
        "performance_content_clean.html",
        "client_match_cards.html",
    ):
        assert env.get_template(name) is not None
