from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sports_ai.dashboard import storage as dashboard_storage
from sports_ai.dashboard.app import create_app
from sports_ai.dashboard.football_daily_brief import (
    build_daily_brief_payload,
    football_day_context,
    football_day_for_timestamp,
)
from sports_ai.dashboard.models import FIXTURE_STATUS_PRE_MATCH, NormalizedFixture
from sports_ai.dashboard.portfolio_selection import (
    PORTFOLIO_POLICY_ID_V1,
    PORTFOLIO_POLICY_VERSION_V1,
    PORTFOLIO_STATE_SELECTED,
    PortfolioDisposition,
    PortfolioPolicy,
)
from sports_ai.storage import football_canonical_fixture, football_paper_trading, football_portfolio_selection


def _fixture(fixture_id: str, kickoff: str, *, status: str = FIXTURE_STATUS_PRE_MATCH) -> NormalizedFixture:
    return NormalizedFixture(
        fixture_id=fixture_id,
        provider_fixture_id=fixture_id.split(":", 1)[-1],
        provider="fake",
        competition_id="ENG1",
        competition_name="Premier League",
        division_code=None,
        season="2026",
        home_team_id=None,
        home_team_name=f"Home {fixture_id[-1]}",
        away_team_id=None,
        away_team_name=f"Away {fixture_id[-1]}",
        kickoff_utc=kickoff,
        status=status,
        elapsed_minute=None,
        home_score=None,
        away_score=None,
        last_provider_update=kickoff,
        source_version="test",
        provider_payload={},
    )


def _init_dbs(tmp_path: Path) -> tuple[Path, Path]:
    paper_db = tmp_path / "paper.sqlite"
    dashboard_db = tmp_path / "dashboard.sqlite"
    with football_paper_trading.connect(paper_db) as conn:
        football_paper_trading.initialize_database(conn)
        football_canonical_fixture.ensure_schema(conn)
        football_portfolio_selection.ensure_schema(conn)
    with dashboard_storage.connect(dashboard_db) as conn:
        dashboard_storage.initialize_database(conn)
    return paper_db, dashboard_db


def _record_fixture(dashboard_db: Path, fixture: NormalizedFixture) -> None:
    with dashboard_storage.connect(dashboard_db) as conn:
        dashboard_storage.upsert_fixtures(conn, [fixture], updated_at="2026-09-09T00:00:00+00:00")


def _record_trade(
    paper_db: Path,
    *,
    fixture_id: str,
    market: str = "1x2",
    selection: str = "HOME",
    kickoff: str,
    decision_timestamp: str,
    edge: float = 0.08,
    entry_odds: float = 2.1,
    closing_odds: float | None = 1.95,
    closing_snapshot_id: int | None = 99,
    state: str | None = None,
    net_pnl: float | None = None,
    stake: float = 1.0,
) -> str:
    with football_paper_trading.connect(paper_db) as conn:
        entry = football_paper_trading.PaperTradeEntry(
            prediction_id=f"pred_{fixture_id}_{market}_{selection}",
            fixture_id=fixture_id,
            model_snapshot_id="snapshot_v1",
            model_version="route-b-test",
            market=market,
            selection=selection,
            line=None,
            model_probability=0.58,
            model_fair_price=1.72,
            available_market_price=entry_odds,
            edge=edge,
            minimum_acceptable_price=1.9,
            decision="PRELIMINARY_CANDIDATE",
            decision_timestamp=decision_timestamp,
            kickoff=kickoff,
            bookmaker="Pinnacle",
            provider="fake",
            safeguard_state="PASS",
            entry_eligibility="ELIGIBLE",
            entry_odds=entry_odds,
            entry_captured_at=decision_timestamp,
            entry_snapshot_id=1,
            closing_odds=closing_odds,
            closing_captured_at=None if closing_odds is None else kickoff,
            closing_snapshot_id=closing_snapshot_id,
        )
        trade_id = football_paper_trading.record_paper_trade_entry(conn, entry)
        if state is not None:
            gross_return = stake + float(net_pnl or 0.0)
            update = football_paper_trading.PaperTradeUpdate(
                paper_trade_id=trade_id,
                trade_state=state,
                gross_return=gross_return,
                net_pnl=net_pnl,
                payload_json=f'{{"stake": {stake}}}',
                updated_at="2026-09-10T22:00:00+00:00",
            )
            football_paper_trading.append_trade_update(conn, update)
        return trade_id


def _record_selected(
    paper_db: Path,
    *,
    trade_id: str,
    canonical_fixture_id: str,
    evaluated_at: str,
    edge: float = 0.08,
) -> None:
    policy = PortfolioPolicy(
        policy_id=PORTFOLIO_POLICY_ID_V1,
        policy_version=PORTFOLIO_POLICY_VERSION_V1,
        effective_from="2026-09-01T00:00:00+00:00",
    )
    disposition = PortfolioDisposition(
        paper_trade_id=trade_id,
        canonical_fixture_id=canonical_fixture_id,
        market="1x2",
        selection="HOME",
        line=None,
        selection_state=PORTFOLIO_STATE_SELECTED,
        selection_rank=1,
        selection_reason="test selected",
        edge_at_selection=edge,
    )
    with football_paper_trading.connect(paper_db) as conn:
        signature = football_portfolio_selection.portfolio_evaluation_signature(
            policy=policy,
            canonical_fixture_id=canonical_fixture_id,
            candidates=[
                {
                    "paper_trade_id": trade_id,
                    "market": "1x2",
                    "selection": "HOME",
                    "line": None,
                    "edge_at_selection": edge,
                    "decision_timestamp": evaluated_at,
                    "created_at": evaluated_at,
                    "safeguard_state": "PASS",
                }
            ],
        )
        football_portfolio_selection.record_portfolio_evaluation(
            conn,
            policy=policy,
            canonical_fixture_id=canonical_fixture_id,
            evaluated_at=evaluated_at,
            candidate_snapshot_at=evaluated_at,
            evaluation_signature=f"{signature}_{evaluated_at}",
            dispositions=[disposition],
        )


def test_football_day_boundary_and_previous_context() -> None:
    assert football_day_for_timestamp("2026-09-10T02:59:59+00:00").isoformat() == "2026-09-09"
    assert football_day_for_timestamp("2026-09-10T03:00:00+00:00").isoformat() == "2026-09-10"

    context = football_day_context(datetime(2026, 9, 11, 4, 0, tzinfo=timezone.utc))

    assert context["current_football_day"] == "2026-09-11"
    assert context["previous_football_day"] == "2026-09-10"
    assert context["previous_day_start_utc"] == "2026-09-10T03:00:00+00:00"
    assert context["previous_day_end_utc"] == "2026-09-11T03:00:00+00:00"


def test_selected_recommendation_truth_dedupes_and_preserves_settlement(tmp_path: Path) -> None:
    paper_db, dashboard_db = _init_dbs(tmp_path)
    kickoff = "2026-09-10T19:00:00+00:00"
    fixture = _fixture("fake:1", kickoff)
    _record_fixture(dashboard_db, fixture)
    won = _record_trade(
        paper_db,
        fixture_id=fixture.fixture_id,
        kickoff=kickoff,
        decision_timestamp="2026-09-10T10:00:00+00:00",
        state="WON",
        net_pnl=1.37,
        stake=2.0,
    )
    lost = _record_trade(
        paper_db,
        fixture_id="fake:2",
        kickoff="2026-09-10T20:00:00+00:00",
        decision_timestamp="2026-09-10T10:05:00+00:00",
        state="LOST",
        net_pnl=-1.0,
    )
    pending = _record_trade(
        paper_db,
        fixture_id="fake:3",
        kickoff="2026-09-10T21:00:00+00:00",
        decision_timestamp="2026-09-10T10:10:00+00:00",
        closing_odds=None,
        closing_snapshot_id=None,
    )
    never_selected = _record_trade(
        paper_db,
        fixture_id="fake:4",
        kickoff="2026-09-10T22:00:00+00:00",
        decision_timestamp="2026-09-10T10:15:00+00:00",
        state="WON",
        net_pnl=99.0,
    )
    post_kickoff = _record_trade(
        paper_db,
        fixture_id="fake:5",
        kickoff="2026-09-10T18:00:00+00:00",
        decision_timestamp="2026-09-10T10:20:00+00:00",
        state="WON",
        net_pnl=1.0,
    )
    _record_selected(paper_db, trade_id=won, canonical_fixture_id="canon:1", evaluated_at="2026-09-10T11:00:00+00:00")
    _record_selected(paper_db, trade_id=won, canonical_fixture_id="canon:1", evaluated_at="2026-09-10T12:00:00+00:00")
    _record_selected(paper_db, trade_id=lost, canonical_fixture_id="canon:2", evaluated_at="2026-09-10T11:05:00+00:00")
    _record_selected(paper_db, trade_id=pending, canonical_fixture_id="canon:3", evaluated_at="2026-09-10T11:10:00+00:00")
    _record_selected(paper_db, trade_id=post_kickoff, canonical_fixture_id="canon:5", evaluated_at="2026-09-10T19:00:00+00:00")

    payload = build_daily_brief_payload(
        paper_db_path=paper_db,
        dashboard_db_path=dashboard_db,
        now_utc=datetime(2026, 9, 11, 4, 0, tzinfo=timezone.utc),
    )
    selected = payload["previous_day"]["selected_recommendation_performance"]

    assert selected["selected_bets"] == 3
    assert selected["settled_bets"] == 2
    assert selected["pending_bets"] == 1
    assert selected["wins"] == 1
    assert selected["losses"] == 1
    assert selected["settled_stake_units"] == pytest.approx(3.0)
    assert selected["net_units"] == pytest.approx(0.37)
    assert selected["roi"] == pytest.approx(0.37 / 3.0)
    assert selected["clv_sample"] == 2
    assert selected["clv_coverage"] == pytest.approx(1.0)
    assert never_selected not in {row["paper_trade_id"] for row in selected["rows"]}
    assert post_kickoff not in {row["paper_trade_id"] for row in selected["rows"]}
    assert [row["paper_trade_id"] for row in selected["rows"]].count(won) == 1
    assert next(row for row in selected["rows"] if row["paper_trade_id"] == pending)["closing_odds"] is None


def test_selected_summary_handles_push_half_and_void_states(tmp_path: Path) -> None:
    paper_db, dashboard_db = _init_dbs(tmp_path)
    states = [
        ("fake:push", "PUSH", 0.0),
        ("fake:halfwin", "HALF_WIN", 0.45),
        ("fake:halfloss", "HALF_LOSS", -0.5),
        ("fake:void", "VOID", 0.0),
    ]
    for index, (fixture_id, state, pnl) in enumerate(states, start=1):
        kickoff = f"2026-09-10T1{index}:00:00+00:00"
        trade_id = _record_trade(
            paper_db,
            fixture_id=fixture_id,
            kickoff=kickoff,
            decision_timestamp="2026-09-10T08:00:00+00:00",
            state=state,
            net_pnl=pnl,
        )
        _record_selected(paper_db, trade_id=trade_id, canonical_fixture_id=f"canon:{fixture_id}", evaluated_at="2026-09-10T09:00:00+00:00")

    payload = build_daily_brief_payload(
        paper_db_path=paper_db,
        dashboard_db_path=dashboard_db,
        now_utc=datetime(2026, 9, 11, 4, 0, tzinfo=timezone.utc),
    )
    selected = payload["previous_day"]["selected_recommendation_performance"]

    assert selected["pushes"] == 1
    assert selected["half_wins"] == 1
    assert selected["half_losses"] == 1
    assert selected["voids"] == 1
    assert selected["settled_bets"] == 4


def test_current_slate_and_match_outlook_keep_portfolio_truth(tmp_path: Path) -> None:
    paper_db, dashboard_db = _init_dbs(tmp_path)
    dashboard_payload = {
        "upcoming_count": 2,
        "upcoming": [
            {"fixture_id": "future:selected", "prediction": {"prediction_status": "READY"}},
            {"fixture_id": "future:no-bet", "prediction": {"prediction_status": "READY"}},
        ],
        "current_slate_rows": [
            {
                "fixture_id": "future:selected",
                "kickoff_utc": "2026-09-11T12:00:00+00:00",
                "competition_name": "Premier League",
                "home_team_name": "Home",
                "away_team_name": "Away",
                "market_family": "1x2",
                "market_label": "1X2",
                "selection_label": "Home to Win",
                "model_probability": 0.61,
                "fair_odds": 1.64,
                "market_odds": 1.9,
                "bookmaker": "Pinnacle",
                "edge": 0.08,
                "minimum_acceptable_odds": 1.75,
                "freshness": "FRESH",
                "safeguard": "PASS",
                "portfolio_selected": True,
                "portfolio_selection_state": "SELECTED",
                "current_decision_state": "BET",
                "paper_trade_id": "paper-selected",
            },
            {
                "fixture_id": "future:no-bet",
                "kickoff_utc": "2026-09-11T13:00:00+00:00",
                "competition_name": "Premier League",
                "home_team_name": "No",
                "away_team_name": "Bet",
                "market_family": "btts",
                "market_label": "BTTS",
                "selection_label": "Yes",
                "model_probability": 0.59,
                "fair_odds": 1.69,
                "market_odds": 1.7,
                "freshness": "FRESH",
                "portfolio_selected": False,
                "current_decision_state": "NO_BET_PORTFOLIO_NOT_SELECTED",
                "paper_trade_id": "paper-unselected",
            },
            {
                "fixture_id": "past:selected",
                "kickoff_utc": "2026-09-10T12:00:00+00:00",
                "portfolio_selected": True,
                "current_decision_state": "BET",
                "freshness": "FRESH",
            },
        ],
        "client_match_cards": [
            {
                "fixture_id": "future:selected",
                "kickoff_utc": "2026-09-11T12:00:00+00:00",
                "competition_name": "Premier League",
                "home_team_name": "Home",
                "away_team_name": "Away",
                "market_rows": [
                    {"market_family": "1x2", "market_label": "1X2", "selection_label": "Home", "is_bet": True, "portfolio_selected": True, "odds": 1.9},
                    {"market_family": "btts", "market_label": "BTTS", "selection_label": "Yes", "is_bet": False, "portfolio_selected": False, "decision_state": "NO_BET_PORTFOLIO_NOT_SELECTED"},
                ],
            }
        ],
    }

    payload = build_daily_brief_payload(
        paper_db_path=paper_db,
        dashboard_db_path=dashboard_db,
        dashboard_payload=dashboard_payload,
        now_utc=datetime(2026, 9, 11, 4, 0, tzinfo=timezone.utc),
    )
    slate = payload["current_slate"]

    assert slate["selected_bets"] == 1
    assert slate["past_selections_excluded"] == 1
    assert slate["rows"][0]["paper_trade_id"] == "paper-selected"
    outlook_markets = {row["market"]: row for row in slate["match_outlook"][0]["markets"]}
    assert outlook_markets["1x2"]["decision"] == "BET"
    assert outlook_markets["btts"]["decision"] == "NO_BET"
    assert outlook_markets["btts"]["reason"] == "NOT_SELECTED_PORTFOLIO"


@dataclass
class _Snapshot:
    snapshot_id: str = "snapshot_v1"
    model_version: str = "route-b-test"
    model_as_of: str = "2026-05-01T00:00:00+00:00"


class _StubServices:
    def __init__(self, config: object, dashboard_payload: dict[str, object]) -> None:
        self.config = config
        self.snapshot = _Snapshot()
        self.dashboard_payload = dashboard_payload

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    def get_dashboard_payload(self, **_: object) -> dict[str, object]:
        return self.dashboard_payload

    def get_system_status(self) -> dict[str, object]:
        return {
            "fixture_feed": {"state": "CONNECTED"},
            "odds_feed": {"state": "CONNECTED"},
            "current_model_snapshot": {"snapshot_id": "snapshot_v1"},
        }


def test_json_daily_brief_route_is_read_only(tmp_path: Path) -> None:
    paper_db, dashboard_db = _init_dbs(tmp_path)

    @dataclass(frozen=True)
    class Config:
        paper_trading_db_path: Path
        dashboard_db_path: Path
        display_timezone: str = "Asia/Manila"

    services = _StubServices(
        Config(paper_trading_db_path=paper_db, dashboard_db_path=dashboard_db),
        {"upcoming": [], "current_slate_rows": [], "client_match_cards": []},
    )
    with sqlite3.connect(paper_db) as conn:
        before_tables = conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()

    app = create_app(services=services)
    with TestClient(app) as client:
        response = client.get("/api/analysis/daily-brief")

    assert response.status_code == 200
    assert response.json()["football_day_cutoff"] == "03:00 UTC"
    assert response.json()["model_snapshot"]["snapshot_id"] == "snapshot_v1"
    with sqlite3.connect(paper_db) as conn:
        after_tables = conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    assert after_tables == before_tables


def test_analysis_page_renders_daily_brief_when_intelligence_unavailable(tmp_path: Path) -> None:
    paper_db = tmp_path / "missing-paper.sqlite"
    dashboard_db = tmp_path / "missing-dashboard.sqlite"

    @dataclass(frozen=True)
    class Config:
        paper_trading_db_path: Path
        dashboard_db_path: Path
        display_timezone: str = "Asia/Manila"

    services = _StubServices(
        Config(paper_trading_db_path=paper_db, dashboard_db_path=dashboard_db),
        {"upcoming": [], "current_slate_rows": [], "client_match_cards": []},
    )

    app = create_app(services=services)
    with TestClient(app) as client:
        response = client.get("/analysis")

    assert response.status_code == 200
    assert "Daily Brief" in response.text
    assert "No current qualified bets. Sports Edge is waiting for sufficient verified value." in response.text
