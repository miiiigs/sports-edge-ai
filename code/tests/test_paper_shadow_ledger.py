from sports_ai.evaluation.reporting import explain_assessment, summarize_outputs
from sports_ai.storage.paper_shadow_ledger import (
    LockRow,
    MarketSnapshot,
    OutcomeUpdate,
    PaperDecision,
    append_outcome_update,
    connect,
    fetch_lock_rows,
    fetch_ledger_rows,
    fetch_market_snapshots,
    record_lock_row,
    record_market_snapshot,
    record_decision,
)
from sports_ai.locking import make_lock_row


def test_paper_shadow_ledger_round_trip(tmp_path):
    db_path = tmp_path / "paper_shadow.sqlite"
    decision = PaperDecision(
        run_id="run_1",
        event_id="evt_football_20260814_team_a_team_b",
        sport="football",
        competition="ENG2",
        market="corners_over_under",
        selection="over_10_5",
        line="10.5",
        available_price=2.05,
        bookmaker="RetailBook",
        fair_probability=0.56,
        fair_odds=1.79,
        market_probability=0.50,
        edge=0.06,
        data_quality=84,
        uncertainty=0.12,
        decision="BET_CANDIDATE",
        decision_reason="retail price is above fair after margin check",
        model_version="model_1",
        strategy_version="strategy_1",
        policy_version="policy_1",
    )

    with connect(db_path) as conn:
        decision_id = record_decision(conn, decision)
        append_outcome_update(
            conn,
            OutcomeUpdate(
                decision_id=decision_id,
                result_status="PENDING",
                closing_price=1.91,
                notes="captured the close",
            ),
        )
        append_outcome_update(
            conn,
            OutcomeUpdate(
                decision_id=decision_id,
                result_status="SETTLED",
                settlement="WIN",
                notes="graded final result",
            ),
        )
        rows = fetch_ledger_rows(conn)

    assert len(rows) == 1
    row = rows[0]
    assert row["decision"] == "BET_CANDIDATE"
    assert row["closing_price"] == 1.91
    assert row["settlement"] == "WIN"
    assert row["result_status"] == "SETTLED"
    assert row["clv"] > 0


def test_paper_shadow_ledger_stores_locks_and_market_snapshots(tmp_path):
    db_path = tmp_path / "paper_shadow.sqlite"
    prediction_payload = [{"fair_probability": 0.54, "fair_odds": 1.85}]
    prediction_lock = make_lock_row("evt_lock_1", "run_lock_1", prediction_payload, prev_hash=None)
    market_payload = [{"bookmaker": "Bet365", "prices": {"home": 2.1}}]
    market_lock = make_lock_row("evt_lock_1", "run_lock_1", market_payload, prev_hash=prediction_lock["payload_hash"])

    with connect(db_path) as conn:
        record_lock_row(
            conn,
            LockRow(
                stage="prediction",
                event_id="evt_lock_1",
                run_id="run_lock_1",
                payload_json='[{"fair_odds":1.85,"fair_probability":0.54}]',
                payload_hash=prediction_lock["payload_hash"],
                prev_hash=prediction_lock["prev_hash"],
            ),
        )
        record_lock_row(
            conn,
            LockRow(
                stage="market",
                event_id="evt_lock_1",
                run_id="run_lock_1",
                payload_json='[{"bookmaker":"Bet365","prices":{"home":2.1}}]',
                payload_hash=market_lock["payload_hash"],
                prev_hash=market_lock["prev_hash"],
            ),
        )
        record_market_snapshot(
            conn,
            MarketSnapshot(
                run_id="run_lock_1",
                event_id="evt_lock_1",
                bookmaker="Bet365",
                market="1X2",
                selection="home",
                decimal_odds=2.1,
                source_kind="manual",
                source_ref="test",
            ),
        )
        lock_rows = fetch_lock_rows(conn, event_id="evt_lock_1")
        snapshots = fetch_market_snapshots(conn, event_id="evt_lock_1")

    assert len(lock_rows) == 2
    assert lock_rows[0]["stage"] == "prediction"
    assert lock_rows[1]["prev_hash"] == lock_rows[0]["payload_hash"]
    assert len(snapshots) == 1
    assert snapshots[0]["selection"] == "home"


def test_reporting_summarizes_and_explains_outputs():
    rows = [
        {
            "decision": "BET_CANDIDATE",
            "result_status": "SETTLED",
            "settlement": "WIN",
            "available_price": 2.00,
            "fair_probability": 0.57,
            "edge": 0.07,
            "data_quality": 82,
            "uncertainty": 0.15,
            "clv": 0.03,
        },
        {
            "decision": "BET_CANDIDATE",
            "result_status": "SETTLED",
            "settlement": "LOSS",
            "available_price": 2.20,
            "fair_probability": 0.49,
            "edge": 0.04,
            "data_quality": 79,
            "uncertainty": 0.21,
            "clv": 0.01,
        },
        {
            "decision": "WATCH",
            "result_status": "PENDING",
            "settlement": None,
            "available_price": 1.88,
            "fair_probability": 0.51,
            "edge": None,
            "data_quality": 76,
            "uncertainty": 0.27,
            "clv": None,
            "outcome_notes": "INVALID_LATE_MARKET_SNAPSHOT; captured_at=2026-08-15T12:34:34+00:00",
        },
        {
            "decision": "PASS",
            "result_status": "PENDING",
            "settlement": None,
            "available_price": 1.70,
            "fair_probability": 0.55,
            "edge": None,
            "data_quality": 60,
            "uncertainty": 0.35,
            "clv": None,
        },
    ]

    summary = summarize_outputs(rows)
    explanation = explain_assessment(summary)

    assert summary["funnel"]["PASS"] == 1
    assert summary["funnel"]["WATCH"] == 0
    assert summary["funnel"]["BET_CANDIDATE"] == 2
    assert summary["funnel"]["PAPER_SHADOW_CANDIDATE"] == 0
    assert summary["funnel"]["RESEARCH_SIGNAL"] == 0
    assert summary["audit"]["invalidated_rows"] == 1
    assert summary["clv"]["n"] == 2
    assert summary["returns"]["total_units"] == 0.0
    assert summary["prediction_metrics"]["n"] == 2
    assert any("Funnel:" in line for line in explanation)
    assert any("Audit:" in line for line in explanation)
    assert any("CLV:" in line for line in explanation)
