"""Legacy API-Football Asian Handicap paper positions are quarantined on reporting surfaces.

PR #106 proved v1 API-Football AH snapshots used an unsafe NAMED_TEAM sign-flip and
excluded them from Value eligibility. These tests pin the same rule for paper
positions: legacy AH rows are marked, excluded from counted KPIs and disclosed as
a count, while the ledger itself is never rewritten. Corners, corrected (v2) AH
evidence and other providers are counted as before.
"""

from __future__ import annotations

from pathlib import Path

from sports_ai.dashboard.client_history import build_client_history_payload
from sports_ai.dashboard.paper_market_evidence import (
    QUARANTINE_REASON_LEGACY_AH,
    partition_counted_paper_rows,
)
from sports_ai.dashboard.services import DashboardConfig, DashboardServices
from sports_ai.market_data.providers import NotConfiguredMarketProvider
from sports_ai.storage import football_canonical_fixture as cf
from sports_ai.storage import football_paper_trading as pt

AF_V1 = "api_football_current_market_provider_v1"
AF_V2 = "api_football_current_market_provider_v2"
ODDS_API_V1 = "the_odds_api_current_market_provider_v1"


def _seed(
    conn,
    *,
    fixture_id: str,
    market: str = "asian_handicap",
    selection: str = "AWAY",
    line: float = -0.25,
    provider: str = "api-football",
    ingestion_version: str | None = AF_V1,
    trade_state: str = "WON",
) -> str:
    snapshot_id = None
    if ingestion_version is not None:
        snapshot_id = pt.record_market_snapshot(
            conn,
            pt.StoredMarketSnapshot(
                fixture_id=fixture_id,
                provider=provider,
                bookmaker="Pinnacle",
                market=market,
                selection=selection,
                line=line,
                odds=2.6,
                ingestion_version=ingestion_version,
                provider_market_id=f"{fixture_id}:{market}:{line}",
                provider_timestamp="2026-09-01T09:00:00+00:00",
            ),
        )
    trade_id = pt.record_paper_trade_entry(
        conn,
        pt.PaperTradeEntry(
            prediction_id=f"pred-{fixture_id}-{market}",
            fixture_id=fixture_id,
            model_snapshot_id="snap",
            model_version="football_day11_manager_core_v1",
            market=market,
            selection=selection,
            line=line,
            model_probability=0.6,
            model_fair_price=1.67,
            available_market_price=2.6,
            edge=0.2,
            minimum_acceptable_price=1.8,
            decision="PAPER_CANDIDATE",
            decision_timestamp="2026-09-01T09:00:00+00:00",
            kickoff="2026-09-01T14:00:00+00:00",
            safeguard_state="PASSED",
            entry_eligibility="ELIGIBLE",
            bookmaker="Pinnacle",
            provider=provider,
            entry_odds=2.6,
            entry_captured_at="2026-09-01T09:00:00+00:00",
            entry_snapshot_id=snapshot_id,
        ),
    )
    pt.append_trade_update(
        conn,
        pt.build_trade_update_payload(
            paper_trade_id=trade_id,
            trade_state=trade_state,
            settlement_context={"actual_home_goals": 0, "actual_away_goals": 1},
            odds=2.6,
        ),
    )
    return trade_id


def _seed_mixed_ledger(paper_db: Path) -> dict[str, str]:
    with pt.connect(paper_db) as conn:
        # Production ledgers always carry the reconciliation schema.
        cf.fetch_excluded_paper_trade_ids(conn, initialize_schema=True)
        return {
            "ah_v1": _seed(conn, fixture_id="fx-ah-v1"),
            "ah_unlinked": _seed(conn, fixture_id="fx-ah-unlinked", ingestion_version=None),
            "ah_v2": _seed(conn, fixture_id="fx-ah-v2", ingestion_version=AF_V2, trade_state="LOST"),
            "ah_odds_api": _seed(
                conn,
                fixture_id="fx-ah-odds-api",
                provider="the-odds-api",
                ingestion_version=ODDS_API_V1,
                trade_state="LOST",
            ),
            "corner_v1": _seed(
                conn,
                fixture_id="fx-corner-v1",
                market="corner_total",
                selection="OVER",
                line=9.5,
                trade_state="WON",
            ),
        }


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


def test_paper_rows_expose_entry_ingestion_version(tmp_path):
    ids = _seed_mixed_ledger(tmp_path / "paper.sqlite")
    with pt.connect(tmp_path / "paper.sqlite") as conn:
        rows = {row["paper_trade_id"]: row for row in pt.fetch_paper_trade_rows(conn)}
    assert rows[ids["ah_v1"]]["entry_ingestion_version"] == AF_V1
    assert rows[ids["ah_v2"]]["entry_ingestion_version"] == AF_V2
    assert rows[ids["ah_unlinked"]]["entry_ingestion_version"] is None
    assert rows[ids["corner_v1"]]["entry_ingestion_version"] == AF_V1


def test_partition_quarantines_only_legacy_api_football_ah(tmp_path):
    ids = _seed_mixed_ledger(tmp_path / "paper.sqlite")
    with pt.connect(tmp_path / "paper.sqlite") as conn:
        rows = pt.fetch_paper_trade_rows(conn)
    counted, quarantined = partition_counted_paper_rows(rows, excluded_trade_ids={ids["ah_odds_api"]})

    assert {row["paper_trade_id"] for row in quarantined} == {ids["ah_v1"], ids["ah_unlinked"]}
    assert {row["paper_trade_id"] for row in counted} == {ids["ah_v2"], ids["corner_v1"]}
    assert all(row["market_evidence_quarantined"] is True for row in quarantined)
    assert all(row["market_evidence_quarantine_reason"] == QUARANTINE_REASON_LEGACY_AH for row in quarantined)
    assert all(row["market_evidence_quarantined"] is False for row in counted)


def test_partition_counts_other_provider_ah(tmp_path):
    ids = _seed_mixed_ledger(tmp_path / "paper.sqlite")
    with pt.connect(tmp_path / "paper.sqlite") as conn:
        rows = pt.fetch_paper_trade_rows(conn)
    counted, _ = partition_counted_paper_rows(rows, excluded_trade_ids=set())
    assert ids["ah_odds_api"] in {row["paper_trade_id"] for row in counted}


def test_services_headline_excludes_and_discloses_quarantined_positions(tmp_path):
    ids = _seed_mixed_ledger(tmp_path / "paper.sqlite")
    services = _services(tmp_path)

    counted, status = services._paper_positions()

    assert {row["paper_trade_id"] for row in counted} == {ids["ah_v2"], ids["ah_odds_api"], ids["corner_v1"]}
    assert status["raw_paper_rows"] == 5
    assert status["paper_bets"] == 3
    assert status["market_evidence_quarantined_positions"] == 2
    assert status["market_evidence_quarantined_by_market"] == {"asian_handicap": 2}
    assert status["market_evidence_quarantine_applied"] is True
    ah = status["performance"]["by_market"]["asian_handicap"]
    assert ah["positions"] == 2
    assert ah["wins"] == 0 and ah["losses"] == 2


def test_performance_payload_reports_quarantine_block(tmp_path):
    _seed_mixed_ledger(tmp_path / "paper.sqlite")
    payload = _services(tmp_path).get_performance_payload()
    assert payload["paper_market_evidence_quarantine"] == {
        "market_evidence_quarantined_positions": 2,
        "market_evidence_quarantined_by_market": {"asian_handicap": 2},
        "market_evidence_quarantine_applied": True,
        "market_evidence_quarantine_reason": QUARANTINE_REASON_LEGACY_AH,
    }


def test_quarantine_never_rewrites_the_ledger(tmp_path):
    ids = _seed_mixed_ledger(tmp_path / "paper.sqlite")
    with pt.connect(tmp_path / "paper.sqlite") as conn:
        before = [tuple(row) for row in conn.execute("SELECT * FROM paper_trade_updates ORDER BY rowid")]
    _services(tmp_path).get_performance_payload()
    with pt.connect(tmp_path / "paper.sqlite") as conn:
        after = [tuple(row) for row in conn.execute("SELECT * FROM paper_trade_updates ORDER BY rowid")]
        states = dict(conn.execute("SELECT paper_trade_id, trade_state FROM paper_trade_updates").fetchall())
    assert before == after
    assert states[ids["ah_v1"]] == "WON"


def test_client_history_excludes_quarantined_positions(tmp_path):
    _seed_mixed_ledger(tmp_path / "paper.sqlite")
    payload = build_client_history_payload(
        paper_db_path=tmp_path / "paper.sqlite",
        dashboard_db_path=tmp_path / "dashboard.sqlite",
        timezone_name="Asia/Manila",
    )
    assert payload["available"] is True, payload.get("reason")
    fixture_ids = {str(row.get("fixture_id")) for row in payload.get("recent_settled") or []}
    assert {"fx-ah-v2", "fx-corner-v1"} <= fixture_ids
    assert "fx-ah-v1" not in fixture_ids
    assert "fx-ah-unlinked" not in fixture_ids
