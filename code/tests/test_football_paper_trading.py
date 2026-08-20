from __future__ import annotations

import sqlite3

import pytest

from sports_ai.storage.football_paper_trading import (
    PaperTradeEntry,
    build_trade_update_payload,
    connect,
    fetch_market_snapshots,
    fetch_paper_trade_rows,
    paper_trading_status,
    record_market_snapshot,
    record_paper_trade_entry,
    settlement_state_for_market,
    StoredMarketSnapshot,
    append_trade_update,
)


def test_paper_trading_store_appends_snapshots_and_settles_entries(tmp_path):
    db_path = tmp_path / "football_paper_trading.sqlite"
    with connect(db_path) as conn:
        first_snapshot_id = record_market_snapshot(
            conn,
            StoredMarketSnapshot(
                fixture_id="evt_fixture_1",
                provider="mock-provider",
                bookmaker="Book A",
                market="1x2",
                selection="HOME",
                odds=2.2,
                ingestion_version="v1",
                provider_market_id="mkt_1",
                provider_timestamp="2026-08-20T09:00:00+00:00",
            ),
        )
        record_market_snapshot(
            conn,
            StoredMarketSnapshot(
                fixture_id="evt_fixture_1",
                provider="mock-provider",
                bookmaker="Book A",
                market="1x2",
                selection="HOME",
                odds=2.1,
                ingestion_version="v1",
                provider_market_id="mkt_1",
                provider_timestamp="2026-08-20T09:10:00+00:00",
            ),
        )
        paper_trade_id = record_paper_trade_entry(
            conn,
            PaperTradeEntry(
                prediction_id="pred_1",
                fixture_id="evt_fixture_1",
                model_snapshot_id="snapshot_1",
                model_version="football_day11_manager_core_v1",
                market="goals_total",
                selection="OVER",
                line=1.5,
                model_probability=0.74,
                model_fair_price=1.36,
                available_market_price=1.9,
                edge=0.08,
                minimum_acceptable_price=1.5,
                decision="PAPER_CANDIDATE",
                decision_timestamp="2026-08-20T09:00:00+00:00",
                kickoff="2026-08-20T12:00:00+00:00",
                safeguard_state="READY",
                entry_eligibility="ELIGIBLE",
                bookmaker="Book A",
                provider="mock-provider",
                entry_odds=1.9,
                entry_captured_at="2026-08-20T09:00:00+00:00",
                entry_snapshot_id=first_snapshot_id,
            ),
        )
        state = settlement_state_for_market(
            market="goals_total",
            selection="OVER",
            line=1.5,
            actual_home_goals=2,
            actual_away_goals=1,
        )
        append_trade_update(
            conn,
            build_trade_update_payload(
                paper_trade_id=paper_trade_id,
                trade_state=state,
                settlement_context={"actual_home_goals": 2, "actual_away_goals": 1},
                odds=1.9,
            ),
        )
        snapshots = fetch_market_snapshots(conn, fixture_id="evt_fixture_1")
        rows = fetch_paper_trade_rows(conn)

    assert len(snapshots) == 2
    assert len(rows) == 1
    assert rows[0]["trade_state"] == "WON"
    assert rows[0]["net_pnl"] == pytest.approx(0.9)
    assert paper_trading_status(rows)["ledger_state"] == "READY"


def test_paper_trading_store_prevents_duplicate_entry_signatures(tmp_path):
    db_path = tmp_path / "football_paper_trading.sqlite"
    entry = PaperTradeEntry(
        prediction_id="pred_dup",
        fixture_id="evt_fixture_dup",
        model_snapshot_id="snapshot_dup",
        model_version="football_day11_manager_core_v1",
        market="1x2",
        selection="HOME",
        model_probability=0.55,
        model_fair_price=1.82,
        available_market_price=2.1,
        decision="PAPER_CANDIDATE",
        decision_timestamp="2026-08-20T10:00:00+00:00",
        kickoff="2026-08-20T12:00:00+00:00",
        safeguard_state="READY",
        entry_eligibility="ELIGIBLE",
    )
    with connect(db_path) as conn:
        record_paper_trade_entry(conn, entry)
        with pytest.raises(sqlite3.IntegrityError):
            record_paper_trade_entry(conn, entry)
