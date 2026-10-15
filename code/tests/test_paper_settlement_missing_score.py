"""A FINISHED fixture without a verified score must not settle paper positions as 0-0."""

from __future__ import annotations

from pathlib import Path

import pytest

from sports_ai.dashboard.models import FIXTURE_STATUS_FINISHED
from sports_ai.dashboard.services import DashboardConfig, DashboardServices
from sports_ai.market_data.providers import NotConfiguredMarketProvider
from sports_ai.storage import football_paper_trading as pt

FIXTURE_ID = "api-football:9000001"
KICKOFF = "2026-10-10T14:00:00+00:00"


def _services(tmp_path: Path) -> DashboardServices:
    config = DashboardConfig(
        repo_root=tmp_path,
        research_db_path=tmp_path / "research.sqlite",
        dashboard_db_path=tmp_path / "dashboard.sqlite",
        paper_trading_db_path=tmp_path / "paper.sqlite",
        performance_output_dir=tmp_path / "performance",
        operational_data_dir=tmp_path,
        storage_mode="test_tmpdir",
        persistent_data_dir_configured=False,
        display_timezone="Asia/Manila",
        enable_scheduler=False,
        allow_paid_provider_calls=False,
        current_odds_connected=False,
        current_xg_connected=False,
    )
    return DashboardServices(
        config=config,
        fixture_provider=None,
        market_provider=NotConfiguredMarketProvider(provider_label="test"),
    )


def _record_entry(db_path: Path, *, market: str, selection: str, line: float) -> None:
    conn = pt.connect(db_path)
    pt.ensure_schema(conn)
    pt.record_paper_trade_entry(
        conn,
        pt.PaperTradeEntry(
            prediction_id=f"prediction_{market}",
            fixture_id=FIXTURE_ID,
            model_snapshot_id="snapshot_a",
            model_version="v1",
            market=market,
            selection=selection,
            line=line,
            model_probability=0.55,
            model_fair_price=2.0,
            available_market_price=2.1,
            decision="PAPER_CANDIDATE",
            decision_timestamp="2026-10-09T10:00:00+00:00",
            kickoff=KICKOFF,
            safeguard_state="PASSED",
            entry_eligibility="ELIGIBLE",
            bookmaker="Pinnacle",
            provider="api-football",
            entry_odds=2.1,
            created_at="2026-10-09T10:00:00+00:00",
        ),
    )
    conn.close()


def _settle(tmp_path: Path, monkeypatch, *, home_score, away_score) -> dict[str, str]:
    services = _services(tmp_path)
    fixture = {
        "fixture_id": FIXTURE_ID,
        "provider": "api-football",
        "status": FIXTURE_STATUS_FINISHED,
        "home_score": home_score,
        "away_score": away_score,
        "home_corners": 6,
        "away_corners": 5,
    }
    monkeypatch.setattr(services, "_resolve_fixture_for_paper_settlement", lambda **_: fixture)
    services._process_paper_trading_updates([])
    conn = pt.connect(tmp_path / "paper.sqlite")
    try:
        return {row["market"]: str(row["trade_state"]) for row in pt.fetch_paper_trade_rows(conn)}
    finally:
        conn.close()


@pytest.fixture
def entries(tmp_path: Path) -> Path:
    _record_entry(tmp_path / "paper.sqlite", market="asian_handicap", selection="AWAY", line=0.5)
    _record_entry(tmp_path / "paper.sqlite", market="corner_total", selection="OVER", line=9.5)
    return tmp_path


def test_missing_score_leaves_goal_markets_unsettled(entries: Path, monkeypatch) -> None:
    states = _settle(entries, monkeypatch, home_score=None, away_score=None)
    # Before the fix the AH position was graded permanently as if the score were 0-0.
    assert states["asian_handicap"] == "UNSETTLED"
    # Corner settlement does not depend on goals and still uses verified corners.
    assert states["corner_total"] == "WON"


def test_verified_score_settles_goal_markets(entries: Path, monkeypatch) -> None:
    states = _settle(entries, monkeypatch, home_score=2, away_score=0)
    assert states["asian_handicap"] == "LOST"
    assert states["corner_total"] == "WON"
