from __future__ import annotations

import pytest

from sports_ai.storage.football_paper_trading import (
    PaperTradeEntry,
    StoredMarketSnapshot,
    attach_closing_snapshot,
    closing_market_margin_for_snapshot,
    connect,
    fetch_paper_trade_rows,
    initialize_database,
    margin_adjusted_clv,
    record_market_snapshot,
    record_paper_trade_entry,
)


def _snapshot(*, selection: str, odds: float, captured_at: str = "2026-09-15T10:00:00+00:00") -> StoredMarketSnapshot:
    return StoredMarketSnapshot(
        fixture_id="fixture-clv-1",
        provider="the-odds-api",
        bookmaker="pinnacle",
        market="1x2",
        selection=selection,
        odds=odds,
        line=None,
        ingestion_version="test-v1",
        provider_timestamp="2026-09-15T09:59:30+00:00",
        captured_at=captured_at,
        is_pre_match=True,
    )


def _entry() -> PaperTradeEntry:
    return PaperTradeEntry(
        prediction_id="prediction-clv-1",
        fixture_id="fixture-clv-1",
        model_snapshot_id="model-1",
        model_version="v1",
        market="1x2",
        selection="HOME",
        model_fair_price=2.00,
        available_market_price=2.20,
        decision="PAPER_CANDIDATE",
        decision_timestamp="2026-09-15T09:00:00+00:00",
        kickoff="2026-09-15T10:01:00+00:00",
        safeguard_state="PASS",
        entry_eligibility="ELIGIBLE",
        model_probability=0.50,
        bookmaker="pinnacle",
        provider="the-odds-api",
        entry_odds=2.20,
    )


def test_closing_snapshot_persists_complete_book_margin_and_spec_clv(tmp_path):
    db_path = tmp_path / "paper.sqlite"
    with connect(db_path) as conn:
        initialize_database(conn)
        home_id = record_market_snapshot(conn, _snapshot(selection="HOME", odds=2.00), initialize_schema=False)
        record_market_snapshot(conn, _snapshot(selection="DRAW", odds=3.50), initialize_schema=False)
        record_market_snapshot(conn, _snapshot(selection="AWAY", odds=4.00), initialize_schema=False)
        entry = _entry()
        record_paper_trade_entry(conn, entry, initialize_schema=False)

        expected_margin = (1 / 2.00) + (1 / 3.50) + (1 / 4.00) - 1.0
        assert closing_market_margin_for_snapshot(conn, home_id, initialize_schema=False) == pytest.approx(expected_margin)

        attach_closing_snapshot(
            conn,
            paper_trade_id=entry.paper_trade_id,
            closing_odds=2.00,
            closing_captured_at="2026-09-15T10:00:00+00:00",
            closing_snapshot_id=home_id,
            initialize_schema=False,
        )
        row = fetch_paper_trade_rows(conn, initialize_schema=False)[0]

    expected_clv = 2.20 / 2.00 / (1.0 + expected_margin) - 1.0
    assert row["closing_margin"] == pytest.approx(expected_margin)
    assert row["clv_pct"] == pytest.approx(expected_clv)
    assert margin_adjusted_clv(entry_odds=2.20, closing_odds=2.00, closing_margin=expected_margin) == pytest.approx(expected_clv)


def test_incomplete_closing_book_fails_closed_without_clv(tmp_path):
    db_path = tmp_path / "paper.sqlite"
    with connect(db_path) as conn:
        initialize_database(conn)
        home_id = record_market_snapshot(conn, _snapshot(selection="HOME", odds=2.00), initialize_schema=False)
        entry = _entry()
        record_paper_trade_entry(conn, entry, initialize_schema=False)

        assert closing_market_margin_for_snapshot(conn, home_id, initialize_schema=False) is None
        attach_closing_snapshot(
            conn,
            paper_trade_id=entry.paper_trade_id,
            closing_odds=2.00,
            closing_captured_at="2026-09-15T10:00:00+00:00",
            closing_snapshot_id=home_id,
            initialize_schema=False,
        )
        row = fetch_paper_trade_rows(conn, initialize_schema=False)[0]

    assert row["closing_odds"] == pytest.approx(2.00)
    assert row["closing_margin"] is None
    assert row["clv_pct"] is None
