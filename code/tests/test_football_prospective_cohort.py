from __future__ import annotations

import sqlite3

import pytest
from sports_ai.storage import football_canonical_fixture
from sports_ai.storage.football_paper_trading import (
    PaperTradeEntry,
    StoredMarketSnapshot,
    attach_closing_snapshot,
    fetch_paper_trade_rows,
    initialize_database,
    record_market_snapshot,
    record_paper_trade_entry,
)
from sports_ai.storage.football_paper_trading import (
    connect as paper_connect,
)
from sports_ai.storage.football_prospective_cohort import (
    FOCUS_COHORT_ID,
    ProspectiveCohort,
    closing_line_value,
    closing_quote_matches_position,
    clv_summary_for_rows,
    connect,
    ensure_schema,
    fetch_cohort,
    is_prospective_focus_row,
    prospective_focus_performance,
    record_cohort,
    resolve_closing_quote,
    resolve_cohort,
)

EFFECTIVE = "2026-09-26T10:16:20.567997950+00:00"


def _cohort(effective_from: str = EFFECTIVE) -> ProspectiveCohort:
    return ProspectiveCohort(
        cohort_id=FOCUS_COHORT_ID,
        effective_from=effective_from,
        focus_markets=("asian_handicap", "corner_total"),
    )


def _row(
    *,
    paper_trade_id: str,
    market: str,
    selection: str,
    decision_timestamp: str,
    line: float | None = None,
    trade_state: str = "PENDING",
    net_pnl: float | None = None,
    clv_pct: float | None = None,
    entry_odds: float | None = None,
    closing_odds: float | None = None,
    closing_margin: float | None = None,
    closing_snapshot_id: int | None = None,
    stake: float | None = None,
    fixture_id: str = "fixture-1",
    created_at: str | None = None,
) -> dict:
    return {
        "paper_trade_id": paper_trade_id,
        "fixture_id": fixture_id,
        "market": market,
        "selection": selection,
        "line": line,
        "decision_timestamp": decision_timestamp,
        "created_at": created_at if created_at is not None else decision_timestamp,
        "trade_state": trade_state,
        "net_pnl": net_pnl,
        "clv_pct": clv_pct,
        "entry_odds": entry_odds,
        "closing_odds": closing_odds,
        "closing_margin": closing_margin,
        "closing_snapshot_id": closing_snapshot_id,
        "latest_update_payload": {"stake": stake} if stake is not None else {},
    }


# --------------------------------------------------------------- cohort boundary
def test_prospective_cohort_boundary_includes_focused_after_effective():
    cohort = _cohort()
    ah = _row(paper_trade_id="p1", market="asian_handicap", selection="HOME", line=-0.5, decision_timestamp="2026-09-28T10:00:00+00:00")
    corners = _row(paper_trade_id="p2", market="corner_total", selection="OVER", line=9.5, decision_timestamp="2026-09-28T11:00:00+00:00")
    assert is_prospective_focus_row(ah, cohort) is True
    assert is_prospective_focus_row(corners, cohort) is True


def test_historical_row_excluded_from_prospective():
    cohort = _cohort()
    # Non-focused market is never prospective.
    one_x_two = _row(paper_trade_id="p1", market="1x2", selection="HOME", decision_timestamp="2026-09-28T10:00:00+00:00")
    assert is_prospective_focus_row(one_x_two, cohort) is False
    # Focused market decided before the boundary is historical.
    old_ah = _row(paper_trade_id="p2", market="asian_handicap", selection="HOME", line=-0.5, decision_timestamp="2026-09-26T10:00:00+00:00")
    assert is_prospective_focus_row(old_ah, cohort) is False


def test_valid_new_row_included():
    cohort = _cohort()
    row = _row(paper_trade_id="p1", market="asian_handicap", selection="AWAY", line=-0.25, decision_timestamp="2026-09-27T00:00:00+00:00")
    assert is_prospective_focus_row(row, cohort) is True


def test_boundary_one_instant_before_is_historical():
    cohort = _cohort()
    before = _row(
        paper_trade_id="p1",
        market="asian_handicap",
        selection="HOME",
        line=-0.5,
        decision_timestamp="2026-09-26T10:16:20.567997949+00:00",
    )
    assert is_prospective_focus_row(before, cohort) is False


def test_boundary_exact_instant_is_prospective():
    cohort = _cohort()
    exact = _row(
        paper_trade_id="p1",
        market="asian_handicap",
        selection="HOME",
        line=-0.5,
        decision_timestamp="2026-09-26T10:16:20.567997950+00:00",
    )
    assert is_prospective_focus_row(exact, cohort) is True


def test_boundary_one_instant_after_is_prospective():
    cohort = _cohort()
    after = _row(
        paper_trade_id="p1",
        market="asian_handicap",
        selection="HOME",
        line=-0.5,
        decision_timestamp="2026-09-26T10:16:20.567997951+00:00",
    )
    assert is_prospective_focus_row(after, cohort) is True


def test_canonical_duplicate_excluded_from_prospective_performance():
    cohort = _cohort()
    rows = [
        _row(
            paper_trade_id="p1",
            fixture_id="provider-a:100",
            market="asian_handicap",
            selection="HOME",
            line=-0.5,
            decision_timestamp="2026-09-28T10:00:00+00:00",
            trade_state="WON",
            net_pnl=0.5,
        ),
        _row(
            paper_trade_id="p2",
            fixture_id="provider-b:200",
            market="asian_handicap",
            selection="HOME",
            line=-0.5,
            decision_timestamp="2026-09-28T10:00:00+00:00",
            trade_state="WON",
            net_pnl=0.5,
        ),
    ]
    fixture_identity = {
        "provider-a:100": {
            "competition_id": "ENG1",
            "home_team_name": "Arsenal",
            "away_team_name": "Chelsea",
            "kickoff_utc": "2026-09-28T15:00:00+00:00",
        },
        "provider-b:200": {
            "competition_id": "ENG1",
            "home_team_name": "Arsenal",
            "away_team_name": "Chelsea",
            "kickoff_utc": "2026-09-28T15:00:00+00:00",
        },
    }
    plan = football_canonical_fixture.build_reconciliation_plan(rows, fixture_identity)
    counted = plan.counted_rows(rows)
    result = prospective_focus_performance(counted, cohort)
    assert result["prospective"]["positions"] == 1
    assert result["prospective"]["net_units"] == pytest.approx(0.5)


# ------------------------------------------------------------- CLV matching rules
def test_ah_same_line_closing_match():
    assert closing_quote_matches_position(
        quote_market="asian_handicap",
        quote_selection="HOME",
        quote_line=-0.5,
        position_market="asian_handicap",
        position_selection="HOME",
        position_line=-0.5,
    ) is True


def test_ah_different_line_closing_rejected():
    assert closing_quote_matches_position(
        quote_market="asian_handicap",
        quote_selection="HOME",
        quote_line=-0.25,
        position_market="asian_handicap",
        position_selection="HOME",
        position_line=-0.5,
    ) is False


def test_corners_same_line_match():
    assert closing_quote_matches_position(
        quote_market="corner_total",
        quote_selection="OVER",
        quote_line=9.5,
        position_market="corner_total",
        position_selection="OVER",
        position_line=9.5,
    ) is True


def test_corners_different_line_rejected():
    assert closing_quote_matches_position(
        quote_market="corner_total",
        quote_selection="OVER",
        quote_line=10.5,
        position_market="corner_total",
        position_selection="OVER",
        position_line=9.5,
    ) is False


def test_missing_closing_price_clv_unavailable():
    row = _row(paper_trade_id="p1", market="asian_handicap", selection="HOME", line=-0.5, decision_timestamp="2026-09-28T10:00:00+00:00", trade_state="WON", net_pnl=1.0)
    clv = clv_summary_for_rows([row])
    assert clv["sample"] == 0
    assert clv["average_clv"] is None
    assert clv["coverage"] == 0.0


def test_no_lookahead_closing_quote_rejected():
    snapshots = [
        {
            "fixture_id": "fixture-1",
            "market": "asian_handicap",
            "selection": "HOME",
            "line": -0.5,
            "captured_at": "2026-09-28T08:00:00+00:00",  # BEFORE entry
        },
    ]
    quote = resolve_closing_quote(
        snapshots,
        fixture_id="fixture-1",
        market="asian_handicap",
        selection="HOME",
        line=-0.5,
        entry_captured_at="2026-09-28T09:00:00+00:00",
        kickoff_utc="2026-09-28T15:00:00+00:00",
    )
    assert quote is None


def test_resolve_closing_quote_selects_latest_strictly_matched():
    snapshots = [
        {
            "fixture_id": "fixture-1",
            "market": "asian_handicap",
            "selection": "HOME",
            "line": -0.5,
            "captured_at": "2026-09-28T14:00:00+00:00",
            "odds": 1.90,
        },
        {
            "fixture_id": "fixture-1",
            "market": "asian_handicap",
            "selection": "HOME",
            "line": -0.25,  # wrong line, must be ignored
            "captured_at": "2026-09-28T14:30:00+00:00",
            "odds": 1.60,
        },
    ]
    quote = resolve_closing_quote(
        snapshots,
        fixture_id="fixture-1",
        market="asian_handicap",
        selection="HOME",
        line=-0.5,
        entry_captured_at="2026-09-28T09:00:00+00:00",
        kickoff_utc="2026-09-28T15:00:00+00:00",
    )
    assert quote is not None
    assert quote["odds"] == 1.90


def test_closing_line_value_positive_means_beat_close():
    value = closing_line_value(entry_odds=2.20, closing_odds=2.00, closing_margin=0.05)
    assert value > 0.0


# ----------------------------------------------------------- settlement/performance
def _settled_rows(*states: str) -> list[dict]:
    rows = []
    for index, state in enumerate(states):
        net = 0.0
        if state == "WON":
            net = 1.0
        elif state == "HALF_WIN":
            net = 0.5
        elif state == "HALF_LOSS":
            net = -0.5
        elif state == "LOST":
            net = -1.0
        rows.append(
            _row(
                paper_trade_id=f"p{index}",
                market="asian_handicap",
                selection="HOME",
                line=-0.5,
                decision_timestamp="2026-09-28T10:00:00+00:00",
                trade_state=state,
                net_pnl=net,
            )
        )
    return rows


def test_half_win_settlement_counted():
    result = prospective_focus_performance(_settled_rows("HALF_WIN"), _cohort())
    assert result["prospective"]["half_wins"] == 1
    assert result["prospective"]["wins"] == 0


def test_half_loss_settlement_counted():
    result = prospective_focus_performance(_settled_rows("HALF_LOSS"), _cohort())
    assert result["prospective"]["half_losses"] == 1
    assert result["prospective"]["net_units"] == pytest.approx(-0.5)


def test_push_handling():
    result = prospective_focus_performance(_settled_rows("PUSH"), _cohort())
    assert result["prospective"]["pushes"] == 1
    assert result["prospective"]["net_units"] == pytest.approx(0.0)
    assert result["prospective"]["strike_rate"] is None


# ------------------------------------------------------------ market aggregation
def test_market_level_aggregation_separates_ah_and_corners():
    cohort = _cohort()
    rows = [
        _row(paper_trade_id="p1", market="asian_handicap", selection="HOME", line=-0.5, decision_timestamp="2026-09-28T10:00:00+00:00", trade_state="WON", net_pnl=1.0),
        _row(paper_trade_id="p2", market="corner_total", selection="OVER", line=9.5, decision_timestamp="2026-09-28T11:00:00+00:00", trade_state="LOST", net_pnl=-1.0),
    ]
    result = prospective_focus_performance(rows, cohort)
    assert set(result["by_market"].keys()) == {"asian_handicap", "corner_total"}
    assert result["by_market"]["asian_handicap"]["wins"] == 1
    assert result["by_market"]["corner_total"]["losses"] == 1


def test_prospective_vs_all_time_reporting():
    cohort = _cohort()
    rows = [
        _row(paper_trade_id="p1", market="1x2", selection="HOME", decision_timestamp="2026-09-20T10:00:00+00:00", trade_state="WON", net_pnl=1.0),
        _row(paper_trade_id="p2", market="asian_handicap", selection="HOME", line=-0.5, decision_timestamp="2026-09-28T10:00:00+00:00", trade_state="WON", net_pnl=1.0),
    ]
    result = prospective_focus_performance(rows, cohort)
    assert result["historical"]["positions"] == 2
    assert result["prospective"]["positions"] == 1


# --------------------------------------------------------------- persistence
def test_restart_reload_preserves_cohort_identity(tmp_path):
    db_path = tmp_path / "cohort.sqlite"
    with connect(db_path) as conn:
        ensure_schema(conn)
        first = resolve_cohort(conn)
        assert first.cohort_id == FOCUS_COHORT_ID
        assert first.effective_from == EFFECTIVE
        assert first.focus_markets == ("asian_handicap", "corner_total")
    with connect(db_path) as conn:
        ensure_schema(conn)
        second = resolve_cohort(conn)
    assert second == first
    assert fetch_cohort(connect(db_path)) is not None


def test_cohort_redefinition_conflicts_fail_closed(tmp_path):
    db_path = tmp_path / "cohort.sqlite"
    with connect(db_path) as conn:
        ensure_schema(conn)
        record_cohort(conn, _cohort())
        with pytest.raises(sqlite3.IntegrityError):
            record_cohort(conn, _cohort(effective_from="2026-09-01T00:00:00+00:00"))


def test_startup_schema_init_and_resolve_idempotent(tmp_path):
    db_path = tmp_path / "cohort.sqlite"
    with connect(db_path) as conn:
        ensure_schema(conn)
        ensure_schema(conn)
        first = resolve_cohort(conn)
        second = resolve_cohort(conn)
    assert first == second
    assert fetch_cohort(connect(db_path)) == first


# ----------------------------------------------------- integration with CLV attach
def _ah_entry(fixture_id: str = "fixture-ah-1") -> PaperTradeEntry:
    return PaperTradeEntry(
        prediction_id=f"pred-{fixture_id}",
        fixture_id=fixture_id,
        model_snapshot_id="model-1",
        model_version="v1",
        market="asian_handicap",
        selection="HOME",
        line=-0.5,
        model_fair_price=1.90,
        available_market_price=2.10,
        decision="PAPER_CANDIDATE",
        decision_timestamp="2026-09-28T09:00:00+00:00",
        kickoff="2026-09-28T15:00:00+00:00",
        safeguard_state="PASS",
        entry_eligibility="ELIGIBLE",
        model_probability=0.55,
        bookmaker="pinnacle",
        provider="the-odds-api",
        entry_odds=2.10,
    )


def _ah_snapshot(*, selection: str, line: float, odds: float) -> StoredMarketSnapshot:
    return StoredMarketSnapshot(
        fixture_id="fixture-ah-1",
        provider="the-odds-api",
        bookmaker="pinnacle",
        market="asian_handicap",
        selection=selection,
        line=line,
        odds=odds,
        ingestion_version="test-v1",
        provider_timestamp="2026-09-28T14:59:00+00:00",
        captured_at="2026-09-28T14:59:00+00:00",
        is_pre_match=True,
    )


def test_ah_closing_attach_same_line_computes_clv(tmp_path):
    db_path = tmp_path / "paper.sqlite"
    with paper_connect(db_path) as conn:
        initialize_database(conn)
        home_id = record_market_snapshot(conn, _ah_snapshot(selection="HOME", line=-0.5, odds=1.90), initialize_schema=False)
        record_market_snapshot(conn, _ah_snapshot(selection="AWAY", line=-0.5, odds=1.95), initialize_schema=False)
        entry = _ah_entry()
        record_paper_trade_entry(conn, entry, initialize_schema=False)
        attach_closing_snapshot(
            conn,
            paper_trade_id=entry.paper_trade_id,
            closing_odds=1.90,
            closing_captured_at="2026-09-28T14:59:00+00:00",
            closing_snapshot_id=home_id,
            initialize_schema=False,
        )
        row = fetch_paper_trade_rows(conn, initialize_schema=False)[0]
    assert row["clv_pct"] is not None


def test_ah_closing_attach_different_line_fails_closed(tmp_path):
    db_path = tmp_path / "paper.sqlite"
    with paper_connect(db_path) as conn:
        initialize_database(conn)
        wrong_line_id = record_market_snapshot(conn, _ah_snapshot(selection="HOME", line=-0.25, odds=1.70), initialize_schema=False)
        entry = _ah_entry()
        record_paper_trade_entry(conn, entry, initialize_schema=False)
        attach_closing_snapshot(
            conn,
            paper_trade_id=entry.paper_trade_id,
            closing_odds=1.70,
            closing_captured_at="2026-09-28T14:59:00+00:00",
            closing_snapshot_id=wrong_line_id,
            initialize_schema=False,
        )
        row = fetch_paper_trade_rows(conn, initialize_schema=False)[0]
    assert row["clv_pct"] is None


def test_clv_denominator_semantics_worked_example(tmp_path):
    """Prove closing_margin is the overround expected by the CLV formula.

    Entry: AH HOME -0.5 @ 2.10.
    Closing book: HOME -0.5 @ 1.90, AWAY +0.5 @ 1.95.
    overround = 1/1.90 + 1/1.95 - 1.
    clv = entry / closing / (1 + overround) - 1.
    """
    db_path = tmp_path / "paper.sqlite"
    with paper_connect(db_path) as conn:
        initialize_database(conn)
        home_id = record_market_snapshot(conn, _ah_snapshot(selection="HOME", line=-0.5, odds=1.90), initialize_schema=False)
        record_market_snapshot(conn, _ah_snapshot(selection="AWAY", line=-0.5, odds=1.95), initialize_schema=False)
        entry = _ah_entry()
        record_paper_trade_entry(conn, entry, initialize_schema=False)
        attach_closing_snapshot(
            conn,
            paper_trade_id=entry.paper_trade_id,
            closing_odds=1.90,
            closing_captured_at="2026-09-28T14:59:00+00:00",
            closing_snapshot_id=home_id,
            initialize_schema=False,
        )
        row = fetch_paper_trade_rows(conn, initialize_schema=False)[0]

    expected_margin = (1 / 1.90) + (1 / 1.95) - 1.0
    expected_clv = 2.10 / 1.90 / (1.0 + expected_margin) - 1.0
    assert row["closing_margin"] == pytest.approx(expected_margin)
    assert row["clv_pct"] == pytest.approx(expected_clv)
    assert expected_clv > 0.0


def _corner_entry() -> PaperTradeEntry:
    return PaperTradeEntry(
        prediction_id="pred-corners",
        fixture_id="fixture-corners-1",
        model_snapshot_id="model-1",
        model_version="v1",
        market="corner_total",
        selection="OVER",
        line=9.5,
        model_fair_price=1.90,
        available_market_price=2.00,
        decision="PAPER_CANDIDATE",
        decision_timestamp="2026-09-28T09:00:00+00:00",
        kickoff="2026-09-28T15:00:00+00:00",
        safeguard_state="PASS",
        entry_eligibility="ELIGIBLE",
        model_probability=0.52,
        bookmaker="pinnacle",
        provider="the-odds-api",
        entry_odds=2.00,
    )


def _corner_snapshot(*, selection: str, line: float, odds: float) -> StoredMarketSnapshot:
    return StoredMarketSnapshot(
        fixture_id="fixture-corners-1",
        provider="the-odds-api",
        bookmaker="pinnacle",
        market="corner_total",
        selection=selection,
        line=line,
        odds=odds,
        ingestion_version="test-v1",
        provider_timestamp="2026-09-28T14:59:00+00:00",
        captured_at="2026-09-28T14:59:00+00:00",
        is_pre_match=True,
    )


def test_corners_closing_attach_different_line_fails_closed(tmp_path):
    """Corners Over 9.5 must not adopt a closing quote for Over 10.5."""
    db_path = tmp_path / "paper.sqlite"
    with paper_connect(db_path) as conn:
        initialize_database(conn)
        wrong_line_id = record_market_snapshot(conn, _corner_snapshot(selection="OVER", line=10.5, odds=1.80), initialize_schema=False)
        entry = _corner_entry()
        record_paper_trade_entry(conn, entry, initialize_schema=False)
        attach_closing_snapshot(
            conn,
            paper_trade_id=entry.paper_trade_id,
            closing_odds=1.80,
            closing_captured_at="2026-09-28T14:59:00+00:00",
            closing_snapshot_id=wrong_line_id,
            initialize_schema=False,
        )
        row = fetch_paper_trade_rows(conn, initialize_schema=False)[0]
    assert row["clv_pct"] is None
