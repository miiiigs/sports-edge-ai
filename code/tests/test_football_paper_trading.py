from __future__ import annotations

import sqlite3
import time as time_module

import pytest

from sports_ai.storage.football_paper_trading import (
    PaperTradeEntry,
    build_trade_update_payload,
    connect,
    ensure_schema,
    fetch_market_snapshots,
    find_existing_paper_trade_entry,
    fetch_paper_trade_rows,
    initialize_database,
    paper_trading_performance,
    paper_trading_status,
    record_market_snapshot,
    record_paper_trade_entry,
    settlement_state_for_market,
    StoredMarketSnapshot,
    append_trade_update,
)


def _normalize_sql(sql: str) -> str:
    return " ".join(str(sql).split()).lower()


def _recording_connect(connect_fn, statements: list[str]):
    class RecordingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            statements.append(_normalize_sql(str(sql)))
            return super().execute(sql, parameters)

    def _connect(*args, **kwargs):
        kwargs["factory"] = RecordingConnection
        return connect_fn(*args, **kwargs)

    return _connect


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
    assert rows[0]["staking_rule_version"] == "QUARTER_KELLY_CAP_2_V1"
    assert rows[0]["staking_probability"] == pytest.approx(0.74)
    assert rows[0]["staking_decimal_price"] == pytest.approx(1.9)
    assert rows[0]["suggested_stake_fraction"] == pytest.approx(0.02)
    assert paper_trading_status(rows)["ledger_state"] == "READY"


def test_paper_trading_store_keeps_rejected_entries_stake_free(tmp_path):
    db_path = tmp_path / "football_paper_trading.sqlite"
    with connect(db_path) as conn:
        paper_trade_id = record_paper_trade_entry(
            conn,
            PaperTradeEntry(
                prediction_id="pred_rejected",
                fixture_id="evt_fixture_rejected",
                model_snapshot_id="snapshot_rejected",
                model_version="football_day11_manager_core_v1",
                market="1x2",
                selection="HOME",
                model_probability=0.60,
                model_fair_price=1.67,
                available_market_price=2.20,
                decision="NO_BET",
                decision_timestamp="2026-08-20T09:00:00+00:00",
                kickoff="2026-08-20T12:00:00+00:00",
                safeguard_state="READY",
                entry_eligibility="INELIGIBLE",
            ),
        )
        rows = fetch_paper_trade_rows(conn)

    assert rows[0]["paper_trade_id"] == paper_trade_id
    assert rows[0]["suggested_stake_fraction"] is None


def test_paper_trading_store_rejects_positive_stake_on_non_candidate():
    with pytest.raises(ValueError, match="positive suggested stake"):
        PaperTradeEntry(
            prediction_id="pred_bad_stake",
            fixture_id="evt_fixture_bad_stake",
            model_snapshot_id="snapshot_bad_stake",
            model_version="football_day11_manager_core_v1",
            market="1x2",
            selection="HOME",
            model_probability=0.60,
            model_fair_price=1.67,
            available_market_price=2.20,
            decision="NO_BET",
            decision_timestamp="2026-08-20T09:00:00+00:00",
            kickoff="2026-08-20T12:00:00+00:00",
            safeguard_state="READY",
            entry_eligibility="INELIGIBLE",
            suggested_stake_fraction=0.02,
        )


def test_market_snapshot_queries_use_indexes_for_identity_lookup_and_fixture_history(tmp_path):
    db_path = tmp_path / "football_paper_trading.sqlite"
    snapshot = StoredMarketSnapshot(
        fixture_id="evt_fixture_1",
        provider="mock-provider",
        bookmaker="Book A",
        market="1x2",
        selection="HOME",
        line=2.5,
        odds=2.2,
        ingestion_version="v1",
        provider_market_id="mkt_1",
        provider_timestamp="2026-08-20T09:00:00+00:00",
        captured_at="2026-08-20T09:00:30+00:00",
        provider_fixture_id="provider_fixture_1",
    )
    with connect(db_path) as conn:
        ensure_schema(conn)
        record_market_snapshot(conn, snapshot)
        identity_plan = conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT snapshot_id
            FROM market_snapshots
            WHERE fixture_id = ?
              AND provider = ?
              AND bookmaker = ?
              AND market = ?
              AND selection = ?
              AND line IS ?
              AND odds = ?
              AND provider_timestamp IS ?
              AND provider_fixture_id IS ?
              AND is_pre_match = ?
            ORDER BY snapshot_id DESC
            LIMIT 1
            """,
            (
                snapshot.fixture_id,
                snapshot.provider,
                snapshot.bookmaker,
                snapshot.market,
                snapshot.selection,
                snapshot.line,
                snapshot.odds,
                snapshot.provider_timestamp,
                snapshot.provider_fixture_id,
                1,
            ),
        ).fetchall()
        fixture_plan = conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT snapshot_id, fixture_id, provider, bookmaker, market, selection, line, odds,
                   provider_market_id, provider_timestamp, captured_at, is_pre_match, ingestion_version,
                   provider_fixture_id, home_team_name, away_team_name, competition_id, competition_name,
                   kickoff_utc, freshness_state
            FROM market_snapshots
            WHERE fixture_id = ?
            ORDER BY captured_at, snapshot_id
            """,
            (snapshot.fixture_id,),
        ).fetchall()

    identity_details = " | ".join(str(row[3]) for row in identity_plan)
    fixture_details = " | ".join(str(row[3]) for row in fixture_plan)

    assert "idx_market_snapshots_identity_lookup" in identity_details
    assert "idx_market_snapshots_fixture_captured" in fixture_details
    assert "SCAN market_snapshots" not in identity_details
    assert "SCAN market_snapshots" not in fixture_details
    assert "USE TEMP B-TREE FOR ORDER BY" not in fixture_details


def test_paper_connect_skips_wal_reinitialization_while_writer_holds_transaction(tmp_path, monkeypatch):
    db_path = tmp_path / "football_paper_trading.sqlite"
    with connect(db_path) as conn:
        initialize_database(conn)

    with sqlite3.connect(db_path) as conn:
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"

    writer = sqlite3.connect(db_path, timeout=5)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        """
        INSERT INTO market_snapshots (
            fixture_id, provider, bookmaker, market, selection, odds,
            captured_at, is_pre_match, ingestion_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "writer-fixture",
            "writer-provider",
            "Writer Book",
            "1x2",
            "HOME",
            2.0,
            "2026-09-01T00:00:00+00:00",
            1,
            "writer-ingestion",
        ),
    )

    statements: list[str] = []
    original_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", _recording_connect(original_connect, statements))

    started = time_module.perf_counter()
    conn = connect(db_path)
    elapsed = time_module.perf_counter() - started
    conn.close()

    writer.rollback()
    writer.close()

    assert elapsed < 1.0
    assert sum("pragma journal_mode = wal" in statement for statement in statements) == 0
    assert sum("pragma busy_timeout = 5000" in statement for statement in statements) == 1


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


def test_paper_trading_store_blocks_duplicate_business_key_recaptures(tmp_path):
    db_path = tmp_path / "football_paper_trading.sqlite"
    original_entry = PaperTradeEntry(
        prediction_id="pred_original",
        fixture_id="evt_fixture_dup",
        model_snapshot_id="snapshot_original",
        model_version="football_day11_manager_core_v1",
        market="corner_total",
        selection="OVER",
        line=10.5,
        model_probability=0.58,
        model_fair_price=1.72,
        available_market_price=2.1,
        decision="PAPER_CANDIDATE",
        decision_timestamp="2026-08-20T10:00:00+00:00",
        kickoff="2026-08-20T12:00:00+00:00",
        safeguard_state="READY",
        entry_eligibility="ELIGIBLE",
        bookmaker="Book A",
        provider="mock-provider",
        entry_odds=2.1,
        entry_captured_at="2026-08-20T10:00:00+00:00",
    )
    recapture_entry = PaperTradeEntry(
        prediction_id="pred_recapture",
        fixture_id="evt_fixture_dup",
        model_snapshot_id="snapshot_recapture",
        model_version="football_day11_manager_core_v2",
        market="corner_total",
        selection="OVER",
        line=10.5,
        model_probability=0.61,
        model_fair_price=1.65,
        available_market_price=2.25,
        decision="PAPER_CANDIDATE",
        decision_timestamp="2026-08-20T11:00:00+00:00",
        kickoff="2026-08-20T12:00:00+00:00",
        safeguard_state="READY",
        entry_eligibility="ELIGIBLE",
        bookmaker="Book B",
        provider="mock-provider",
        entry_odds=2.25,
        entry_captured_at="2026-08-20T11:00:00+00:00",
    )

    with connect(db_path) as conn:
        record_paper_trade_entry(conn, original_entry)

        with pytest.raises(sqlite3.IntegrityError):
            record_paper_trade_entry(conn, recapture_entry)

        found = find_existing_paper_trade_entry(
            conn,
            prediction_id="pred_any_future_version",
            fixture_id="evt_fixture_dup",
            market="corner_total",
            selection="OVER",
            line=10.5,
        )
        rows = fetch_paper_trade_rows(conn)

    assert found is not None
    assert found["paper_trade_id"] == original_entry.paper_trade_id
    assert found["prediction_id"] == "pred_original"
    assert found["decision_timestamp"] == "2026-08-20T10:00:00+00:00"
    assert len(rows) == 1
    assert rows[0]["paper_trade_id"] == original_entry.paper_trade_id
    assert rows[0]["entry_odds"] == pytest.approx(2.1)
    assert rows[0]["bookmaker"] == "Book A"


def test_ensure_schema_requires_reconciliation_when_legacy_business_key_duplicates_exist(tmp_path):
    db_path = tmp_path / "football_paper_trading.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE paper_trade_entries (
                paper_trade_id TEXT PRIMARY KEY,
                prediction_id TEXT NOT NULL,
                fixture_id TEXT NOT NULL,
                model_snapshot_id TEXT NOT NULL,
                model_version TEXT NOT NULL,
                market TEXT NOT NULL,
                selection TEXT NOT NULL,
                line REAL,
                model_probability REAL,
                model_fair_price REAL NOT NULL,
                available_market_price REAL NOT NULL,
                edge REAL,
                minimum_acceptable_price REAL,
                decision TEXT NOT NULL,
                decision_timestamp TEXT NOT NULL,
                kickoff TEXT NOT NULL,
                bookmaker TEXT,
                provider TEXT,
                safeguard_state TEXT NOT NULL,
                entry_eligibility TEXT NOT NULL,
                entry_odds REAL,
                entry_captured_at TEXT,
                entry_snapshot_id INTEGER,
                closing_odds REAL,
                closing_captured_at TEXT,
                closing_snapshot_id INTEGER,
                entry_signature TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE paper_trade_updates (
                paper_trade_update_id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_trade_id TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                trade_state TEXT NOT NULL,
                gross_return REAL,
                net_pnl REAL,
                notes TEXT,
                payload_json TEXT NOT NULL,
                FOREIGN KEY(paper_trade_id) REFERENCES paper_trade_entries(paper_trade_id)
            )
            """
        )
        row_values = (
            "evt_fixture_dup",
            "snapshot_1",
            "football_day11_manager_core_v1",
            "1x2",
            "HOME",
            None,
            0.55,
            1.82,
            2.1,
            None,
            None,
            "PAPER_CANDIDATE",
            "2026-08-20T10:00:00+00:00",
            "2026-08-20T12:00:00+00:00",
            "Book A",
            "mock-provider",
            "READY",
            "ELIGIBLE",
            2.1,
            "2026-08-20T10:00:00+00:00",
            None,
            None,
            None,
            None,
        )
        conn.execute(
            """
            INSERT INTO paper_trade_entries (
                paper_trade_id, prediction_id, fixture_id, model_snapshot_id, model_version, market,
                selection, line, model_probability, model_fair_price, available_market_price, edge,
                minimum_acceptable_price, decision, decision_timestamp, kickoff, bookmaker, provider,
                safeguard_state, entry_eligibility, entry_odds, entry_captured_at, entry_snapshot_id,
                closing_odds, closing_captured_at, closing_snapshot_id, entry_signature, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "paper_legacy_1",
                "pred_legacy_1",
                *row_values,
                "legacy_sig_1",
                "2026-08-20T10:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO paper_trade_entries (
                paper_trade_id, prediction_id, fixture_id, model_snapshot_id, model_version, market,
                selection, line, model_probability, model_fair_price, available_market_price, edge,
                minimum_acceptable_price, decision, decision_timestamp, kickoff, bookmaker, provider,
                safeguard_state, entry_eligibility, entry_odds, entry_captured_at, entry_snapshot_id,
                closing_odds, closing_captured_at, closing_snapshot_id, entry_signature, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "paper_legacy_2",
                "pred_legacy_2",
                *row_values,
                "legacy_sig_2",
                "2026-08-20T11:00:00+00:00",
            ),
        )

    with connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="PAPER_TRADE_BUSINESS_KEY_DUPLICATES_PRESENT"):
            ensure_schema(conn)


def test_fetch_paper_trade_rows_batches_update_queries_and_preserves_latest_update_semantics(tmp_path):
    db_path = tmp_path / "football_paper_trading.sqlite"
    expected_latest_states: dict[str, str] = {}
    sample_paper_trade_id: str | None = None

    with connect(db_path) as conn:
        ensure_schema(conn)
        for index in range(25):
            paper_trade_id = record_paper_trade_entry(
                conn,
                PaperTradeEntry(
                    prediction_id=f"pred_batch_{index}",
                    fixture_id=f"evt_batch_{index}",
                    model_snapshot_id=f"snapshot_batch_{index}",
                    model_version="football_day11_manager_core_v1",
                    market="1x2" if index % 2 == 0 else "goals_total",
                    selection="HOME" if index % 2 == 0 else "OVER",
                    line=None if index % 2 == 0 else 2.5,
                    model_probability=0.57,
                    model_fair_price=1.78,
                    available_market_price=2.12,
                    edge=0.09,
                    decision="PAPER_CANDIDATE",
                    decision_timestamp=f"2026-08-20T10:{index:02d}:00+00:00",
                    kickoff=f"2026-08-20T12:{index:02d}:00+00:00",
                    safeguard_state="READY",
                    entry_eligibility="ELIGIBLE",
                    bookmaker="Book A",
                    provider="mock-provider",
                    entry_odds=2.12,
                    entry_captured_at=f"2026-08-20T10:{index:02d}:00+00:00",
                ),
            )
            append_trade_update(
                conn,
                build_trade_update_payload(
                    paper_trade_id=paper_trade_id,
                    trade_state="OPEN",
                    settlement_context={"phase": "open", "index": index},
                    odds=2.12,
                    notes=f"open-{index}",
                ),
            )
            final_state = "WON" if index % 3 == 0 else "LOST" if index % 3 == 1 else "PUSH"
            append_trade_update(
                conn,
                build_trade_update_payload(
                    paper_trade_id=paper_trade_id,
                    trade_state=final_state,
                    settlement_context={"phase": "final", "index": index},
                    odds=2.12,
                    notes=f"final-{index}",
                ),
            )
            expected_latest_states[paper_trade_id] = final_state
            if index == 1:
                sample_paper_trade_id = paper_trade_id

        traced_sql: list[str] = []
        conn.set_trace_callback(traced_sql.append)
        rows = fetch_paper_trade_rows(conn, initialize_schema=False)
        conn.set_trace_callback(None)

    updates_selects = [
        sql for sql in traced_sql if "from paper_trade_updates" in " ".join(sql.strip().split()).lower()
    ]
    row_by_id = {row["paper_trade_id"]: row for row in rows}
    status = paper_trading_status(rows)

    assert len(rows) == 25
    assert len(updates_selects) == 1
    assert sample_paper_trade_id is not None
    assert row_by_id[sample_paper_trade_id]["trade_state"] == expected_latest_states[sample_paper_trade_id]
    assert row_by_id[sample_paper_trade_id]["update_notes"] == "final-1"
    assert row_by_id[sample_paper_trade_id]["latest_update_payload"]["settlement_context"] == {"phase": "final", "index": 1}
    assert status["paper_bets"] == 25
    assert status["settled_positions"] == 25
    assert status["open_positions"] == 0
    assert status["performance"]["wins"] == sum(1 for state in expected_latest_states.values() if state == "WON")
    assert status["performance"]["losses"] == sum(1 for state in expected_latest_states.values() if state == "LOST")
    assert status["performance"]["pushes"] == sum(1 for state in expected_latest_states.values() if state == "PUSH")

@pytest.mark.parametrize(
    ("market", "selection", "line", "home_goals", "away_goals", "actual_total_corners", "expected_state"),
    [
        ("1x2", "HOME", None, 2, 1, None, "WON"),
        ("1x2", "HOME", None, 0, 1, None, "LOST"),
        ("btts", "YES", None, 1, 1, None, "WON"),
        ("btts", "YES", None, 2, 0, None, "LOST"),
        ("btts", "NO", None, 2, 0, None, "WON"),
        ("btts", "NO", None, 1, 1, None, "LOST"),
        ("goals_total", "OVER", 2.5, 2, 1, None, "WON"),
        ("goals_total", "UNDER", 2.5, 1, 1, None, "WON"),
        ("goals_total", "OVER", 3.0, 2, 1, None, "PUSH"),
        ("corner_total", "OVER", 9.5, 0, 0, 11, "WON"),
        ("corner_total", "UNDER", 9.5, 0, 0, 8, "WON"),
        ("corner_total", "OVER", 10.0, 0, 0, 10, "PUSH"),
        ("asian_handicap", "HOME", -0.5, 1, 0, None, "WON"),
        ("asian_handicap", "HOME", 0.5, 0, 0, None, "WON"),
        ("asian_handicap", "HOME", -1.0, 1, 0, None, "PUSH"),
        ("asian_handicap", "AWAY", -1.0, 0, 0, None, "WON"),
        ("asian_handicap", "HOME", -0.25, 1, 1, None, "HALF_LOSS"),
        ("asian_handicap", "HOME", 0.25, 1, 1, None, "HALF_WIN"),
        ("asian_handicap", "AWAY", 0.75, 0, 1, None, "HALF_WIN"),
    ],
)
def test_settlement_state_for_supported_live_paper_markets(
    market,
    selection,
    line,
    home_goals,
    away_goals,
    actual_total_corners,
    expected_state,
):
    assert (
        settlement_state_for_market(
            market=market,
            selection=selection,
            line=line,
            actual_home_goals=home_goals,
            actual_away_goals=away_goals,
            actual_total_corners=actual_total_corners,
        )
        == expected_state
    )


def test_paper_trading_performance_tracks_asian_handicap_half_states() -> None:
    rows = [
        {
            "market": "asian_handicap",
            "trade_state": "WON",
            "entry_odds": 2.1,
            "net_pnl": 1.1,
            "latest_update_payload": {"stake": 1.0},
            "implied_probability_clv": None,
        },
        {
            "market": "asian_handicap",
            "trade_state": "HALF_WIN",
            "entry_odds": 1.9,
            "net_pnl": 0.45,
            "latest_update_payload": {"stake": 1.0},
            "implied_probability_clv": None,
        },
        {
            "market": "asian_handicap",
            "trade_state": "PUSH",
            "entry_odds": 1.95,
            "net_pnl": 0.0,
            "latest_update_payload": {"stake": 1.0},
            "implied_probability_clv": None,
        },
    ]

    performance = paper_trading_performance(rows)

    assert performance["by_market"]["asian_handicap"]["positions"] == 3
    assert performance["by_market"]["asian_handicap"]["settled_positions"] == 3
    assert performance["by_market"]["asian_handicap"]["wins"] == 1
    assert performance["by_market"]["asian_handicap"]["half_wins"] == 1
    assert performance["by_market"]["asian_handicap"]["pushes"] == 1
    assert performance["by_market"]["asian_handicap"]["net_units"] == pytest.approx(1.55)
    assert performance["by_market"]["asian_handicap"]["strike_rate"] == pytest.approx(0.75)


def test_market_snapshot_dedup_identical(tmp_path):
    db_path = tmp_path / "football_paper_trading.sqlite"
    snapshot = StoredMarketSnapshot(
        fixture_id="evt_fixture_1",
        provider="the-odds-api",
        bookmaker="pinnacle",
        market="1x2",
        selection="HOME",
        odds=2.2,
        ingestion_version="v1",
        provider_market_id="mkt_1",
        provider_timestamp="2026-08-20T09:00:00+00:00",
        provider_fixture_id="epl_event_123",
    )
    with connect(db_path) as conn:
        first_id = record_market_snapshot(conn, snapshot)
        second_id = record_market_snapshot(conn, snapshot)
        snapshots = fetch_market_snapshots(conn, fixture_id="evt_fixture_1")

    assert first_id == second_id
    assert len(snapshots) == 1


def test_market_snapshot_preserves_price_movement(tmp_path):
    db_path = tmp_path / "football_paper_trading.sqlite"
    base = dict(
        fixture_id="evt_fixture_1",
        provider="the-odds-api",
        bookmaker="pinnacle",
        market="1x2",
        selection="HOME",
        ingestion_version="v1",
        provider_market_id="mkt_1",
        provider_fixture_id="epl_event_123",
    )
    first = StoredMarketSnapshot(odds=2.2, provider_timestamp="2026-08-20T09:00:00+00:00", **base)
    second = StoredMarketSnapshot(odds=2.1, provider_timestamp="2026-08-20T09:10:00+00:00", **base)
    with connect(db_path) as conn:
        record_market_snapshot(conn, first)
        record_market_snapshot(conn, second)
        snapshots = fetch_market_snapshots(conn, fixture_id="evt_fixture_1")

    assert len(snapshots) == 2
    assert [snapshot["odds"] for snapshot in snapshots] == [2.2, 2.1]


def test_market_snapshot_preserves_update_change(tmp_path):
    db_path = tmp_path / "football_paper_trading.sqlite"
    base = dict(
        fixture_id="evt_fixture_1",
        provider="the-odds-api",
        bookmaker="pinnacle",
        market="1x2",
        selection="HOME",
        odds=2.2,
        ingestion_version="v1",
        provider_market_id="mkt_1",
        provider_fixture_id="epl_event_123",
    )
    first = StoredMarketSnapshot(provider_timestamp="2026-08-20T09:00:00+00:00", **base)
    second = StoredMarketSnapshot(provider_timestamp="2026-08-20T09:10:00+00:00", **base)
    with connect(db_path) as conn:
        record_market_snapshot(conn, first)
        record_market_snapshot(conn, second)
        snapshots = fetch_market_snapshots(conn, fixture_id="evt_fixture_1")

    assert len(snapshots) == 2


def test_explicit_schema_boundary_allows_hot_path_operations_without_reinitializing(tmp_path):
    db_path = tmp_path / "football_paper_trading.sqlite"
    with connect(db_path) as conn:
        ensure_schema(conn)
        snapshot_id = record_market_snapshot(
            conn,
            StoredMarketSnapshot(
                fixture_id="evt_fixture_boundary",
                provider="mock-provider",
                bookmaker="Book A",
                market="1x2",
                selection="HOME",
                odds=2.05,
                ingestion_version="v1",
                provider_market_id="mkt_boundary",
                provider_timestamp="2026-08-20T09:00:00+00:00",
            ),
            initialize_schema=False,
        )
        paper_trade_id = record_paper_trade_entry(
            conn,
            PaperTradeEntry(
                prediction_id="pred_boundary",
                fixture_id="evt_fixture_boundary",
                model_snapshot_id="snapshot_boundary",
                model_version="football_day11_manager_core_v1",
                market="1x2",
                selection="HOME",
                line=None,
                model_probability=0.55,
                model_fair_price=1.82,
                available_market_price=2.05,
                edge=0.06,
                minimum_acceptable_price=1.95,
                decision="PAPER_CANDIDATE",
                decision_timestamp="2026-08-20T09:00:00+00:00",
                kickoff="2026-08-20T12:00:00+00:00",
                safeguard_state="PASSED",
                entry_eligibility="ELIGIBLE",
                bookmaker="Book A",
                provider="mock-provider",
                entry_odds=2.05,
                entry_captured_at="2026-08-20T09:00:00+00:00",
                entry_snapshot_id=snapshot_id,
            ),
            initialize_schema=False,
        )
        append_trade_update(
            conn,
            build_trade_update_payload(
                paper_trade_id=paper_trade_id,
                trade_state="OPEN",
                settlement_context={"phase": "open"},
                odds=2.05,
                notes="opened",
            ),
            initialize_schema=False,
        )
        snapshots = fetch_market_snapshots(conn, fixture_id="evt_fixture_boundary", initialize_schema=False)
        rows = fetch_paper_trade_rows(conn, initialize_schema=False)

    assert len(snapshots) == 1
    assert len(rows) == 1
    assert rows[0]["paper_trade_id"] == paper_trade_id
    assert rows[0]["trade_state"] == "OPEN"


def test_uninitialized_hot_path_operations_fail_clearly_without_boundary_init(tmp_path):
    db_path = tmp_path / "football_paper_trading.sqlite"
    with connect(db_path) as conn:
        with pytest.raises(sqlite3.OperationalError):
            fetch_paper_trade_rows(conn, initialize_schema=False)
        with pytest.raises(sqlite3.OperationalError):
            fetch_market_snapshots(conn, initialize_schema=False)


def test_paper_trading_performance_excludes_pending_from_roi_and_push_from_strike_rate():
    rows = [
        {
            "market": "1x2",
            "trade_state": "WON",
            "entry_odds": 2.0,
            "net_pnl": 1.0,
            "latest_update_payload": {"stake": 1.0},
            "implied_probability_clv": None,
        },
        {
            "market": "1x2",
            "trade_state": "LOST",
            "entry_odds": 2.2,
            "net_pnl": -2.0,
            "latest_update_payload": {"stake": 2.0},
            "implied_probability_clv": None,
        },
        {
            "market": "btts",
            "trade_state": "PUSH",
            "entry_odds": 1.95,
            "net_pnl": 0.0,
            "latest_update_payload": {"stake": 1.0},
            "implied_probability_clv": None,
        },
        {
            "market": "goals_total",
            "trade_state": "PENDING",
            "entry_odds": 1.9,
            "net_pnl": None,
            "latest_update_payload": None,
            "implied_probability_clv": None,
        },
    ]

    performance = paper_trading_performance(rows)

    assert performance["positions"] == 4
    assert performance["settled_positions"] == 3
    assert performance["pending_positions"] == 1
    assert performance["wins"] == 1
    assert performance["losses"] == 1
    assert performance["pushes"] == 1
    assert performance["net_units"] == pytest.approx(-1.0)
    assert performance["settled_stake_units"] == pytest.approx(4.0)
    assert performance["roi"] == pytest.approx(-0.25)
    assert performance["strike_rate"] == pytest.approx(0.5)
    assert performance["by_market"]["1x2"]["positions"] == 2
    assert performance["by_market"]["1x2"]["settled_positions"] == 2
    assert performance["by_market"]["1x2"]["net_units"] == pytest.approx(-1.0)
    assert performance["by_market"]["1x2"]["roi"] == pytest.approx(-1.0 / 3.0)
    assert performance["by_market"]["1x2"]["strike_rate"] == pytest.approx(0.5)
    assert performance["by_market"]["btts"]["pushes"] == 1
    assert performance["by_market"]["btts"]["roi"] == pytest.approx(0.0)
    assert performance["by_market"]["goals_total"]["roi"] is None
