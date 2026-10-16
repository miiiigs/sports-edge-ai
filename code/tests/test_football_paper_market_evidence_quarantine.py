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
import pytest

from sports_ai.dashboard import paper_market_evidence as pme
from sports_ai.dashboard.paper_market_evidence import (
    DISPUTED_FINAL_SCORE_FIXTURES,
    RESOLVED_FINAL_SCORE_DISPUTES,
    QUARANTINE_REASON_DISPUTED_RESULT,
    QUARANTINE_REASON_LEGACY_AH,
    partition_counted_paper_rows,
    quarantine_summary,
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
        "disputed_result_quarantined_positions": 0,
        "disputed_result_quarantined_by_market": {},
        "disputed_result_fixture_ids": [],
        "disputed_result_quarantine_reason": None,
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


def test_portfolio_headline_cannot_include_quarantined_positions(tmp_path, monkeypatch):
    ids = _seed_mixed_ledger(tmp_path / "paper.sqlite")
    services = _services(tmp_path)
    # Even if every position (including legacy AH) were portfolio-selected, the
    # portfolio KPIs are built from counted rows only.
    monkeypatch.setattr(
        services,
        "_portfolio_selection_records",
        lambda: {
            "policy": None,
            "rows": [],
            "evaluation_rows": [],
            "by_trade_id": {},
            "selected_trade_ids": set(ids.values()),
            "publish_state": "ACTIVE",
            "publish_note": "",
            "mode": "test",
        },
    )
    payload = services.get_performance_payload()
    portfolio = payload["current_portfolio_summary"]
    assert portfolio["positions"] == 3
    assert portfolio["by_market"]["asian_handicap"]["positions"] == 2
    quarantined = {ids["ah_v1"], ids["ah_unlinked"]}
    assert not quarantined & {str(row.get("paper_trade_id")) for row in payload.get("portfolio_positions") or []}


def test_quarantined_count_excludes_reconciled_duplicates(tmp_path):
    ids = _seed_mixed_ledger(tmp_path / "paper.sqlite")
    with pt.connect(tmp_path / "paper.sqlite") as conn:
        rows = pt.fetch_paper_trade_rows(conn)
    # A legacy AH row that is also a reconciled duplicate is counted once, as a
    # reconciliation exclusion, never as a quarantined position.
    counted, quarantined = partition_counted_paper_rows(rows, excluded_trade_ids={ids["ah_v1"]})
    assert {row["paper_trade_id"] for row in quarantined} == {ids["ah_unlinked"]}
    assert len(counted) + len(quarantined) == len(rows) - 1


DISPUTED_AF = "api-football:9990001"
DISPUTED_FDO = "football-data.org:9990001"
OSASUNA_AF = "api-football:1570358"
OSASUNA_FDO = "football-data.org:564657"


@pytest.fixture
def disputed_registry(monkeypatch):
    monkeypatch.setattr(
        pme, "DISPUTED_FINAL_SCORE_FIXTURES", {DISPUTED_AF: "test dispute", DISPUTED_FDO: "test dispute"}
    )


def test_disputed_fixture_quarantines_score_markets_but_keeps_corners(tmp_path, disputed_registry):
    with pt.connect(tmp_path / "paper.sqlite") as conn:
        cf.fetch_excluded_paper_trade_ids(conn, initialize_schema=True)
        ids = {
            "x12": _seed(conn, fixture_id=DISPUTED_AF, market="1x2", selection="HOME", line=None),
            "goals": _seed(conn, fixture_id=DISPUTED_AF, market="goals_total", selection="OVER", line=1.5, trade_state="LOST"),
            "btts": _seed(conn, fixture_id=DISPUTED_AF, market="btts", selection="YES", line=None, trade_state="LOST"),
            "corner": _seed(conn, fixture_id=DISPUTED_AF, market="corner_total", selection="UNDER", line=8.5),
            "ah_v2": _seed(conn, fixture_id=DISPUTED_AF, ingestion_version=AF_V2),
            "ah_v1": _seed(conn, fixture_id=DISPUTED_AF, line=-0.5),
            "other": _seed(conn, fixture_id="fx-undisputed", market="goals_total", selection="OVER", line=2.5),
        }
        rows = pt.fetch_paper_trade_rows(conn)
    counted, quarantined = partition_counted_paper_rows(rows, set())
    by_id = {r["paper_trade_id"]: r for r in quarantined}
    assert {r["paper_trade_id"] for r in counted} == {ids["corner"], ids["other"]}
    for key in ("x12", "goals", "btts", "ah_v2"):
        assert by_id[ids[key]]["market_evidence_quarantine_reason"] == QUARANTINE_REASON_DISPUTED_RESULT
    # Legacy AH keeps its own reason and is never double-counted as disputed.
    assert by_id[ids["ah_v1"]]["market_evidence_quarantine_reason"] == QUARANTINE_REASON_LEGACY_AH

    summary = quarantine_summary(quarantined)
    assert summary["market_evidence_quarantined_positions"] == 1
    assert summary["disputed_result_quarantined_positions"] == 4
    assert summary["disputed_result_quarantined_by_market"] == {"1x2": 1, "asian_handicap": 1, "btts": 1, "goals_total": 1}
    assert summary["disputed_result_fixture_ids"] == [DISPUTED_AF]


def test_disputed_fixture_quarantine_is_read_only_and_reported(tmp_path, disputed_registry):
    with pt.connect(tmp_path / "paper.sqlite") as conn:
        cf.fetch_excluded_paper_trade_ids(conn, initialize_schema=True)
        _seed(conn, fixture_id=DISPUTED_FDO, market="1x2", selection="HOME", line=None)
        _seed(conn, fixture_id="fx-ok", market="1x2", selection="HOME", line=None, trade_state="LOST")
        before = [tuple(row) for row in conn.execute("SELECT * FROM paper_trade_updates ORDER BY rowid")]
    services = _services(tmp_path)
    rows, status = services._paper_positions()
    assert [r["fixture_id"] for r in rows] == ["fx-ok"]
    assert status["disputed_result_quarantined_positions"] == 1
    assert status["disputed_result_fixture_ids"] == [DISPUTED_FDO]
    with pt.connect(tmp_path / "paper.sqlite") as conn:
        after = [tuple(row) for row in conn.execute("SELECT * FROM paper_trade_updates ORDER BY rowid")]
    assert before == after


def test_resolved_osasuna_getafe_positions_are_counted_and_untouched(tmp_path):
    """Official LaLiga + CA Osasuna: 1-0. API-Football graded positions are counted again."""
    assert DISPUTED_FINAL_SCORE_FIXTURES == {}
    assert set(RESOLVED_FINAL_SCORE_DISPUTES) == {OSASUNA_AF, OSASUNA_FDO}
    assert all(e["authoritative_result"] == "1-0" for e in RESOLVED_FINAL_SCORE_DISPUTES.values())
    with pt.connect(tmp_path / "paper.sqlite") as conn:
        cf.fetch_excluded_paper_trade_ids(conn, initialize_schema=True)
        ids = {
            "home": _seed(conn, fixture_id=OSASUNA_AF, market="1x2", selection="HOME", line=None, trade_state="WON"),
            "btts": _seed(conn, fixture_id=OSASUNA_AF, market="btts", selection="YES", line=None, trade_state="LOST"),
            "over": _seed(conn, fixture_id=OSASUNA_AF, market="goals_total", selection="OVER", line=1.5, trade_state="LOST"),
            "ah_v1": _seed(conn, fixture_id=OSASUNA_AF),
        }
        before = [tuple(row) for row in conn.execute("SELECT * FROM paper_trade_updates ORDER BY rowid")]
        rows = pt.fetch_paper_trade_rows(conn)
    counted, quarantined = partition_counted_paper_rows(rows, set())
    assert {r["paper_trade_id"] for r in counted} == {ids["home"], ids["btts"], ids["over"]}
    assert [r["market_evidence_quarantine_reason"] for r in quarantined] == [QUARANTINE_REASON_LEGACY_AH]
    summary = quarantine_summary(quarantined)
    assert summary["disputed_result_quarantined_positions"] == 0
    assert summary["market_evidence_quarantined_positions"] == 1
    with pt.connect(tmp_path / "paper.sqlite") as conn:
        after = [tuple(row) for row in conn.execute("SELECT * FROM paper_trade_updates ORDER BY rowid")]
    assert before == after  # nothing regraded


# Remaining surfaces (2026-10-15 audit): the reconciliation report, the offline
# reconciliation/audit CLIs and the portfolio candidate rows must never count
# legacy AH positions as trusted P/L either.


def _trusted_ids(ids: dict[str, str]) -> set[str]:
    return {ids["ah_v2"], ids["ah_odds_api"], ids["corner_v1"]}


def test_reconciliation_report_excludes_and_discloses_quarantined_positions(tmp_path):
    _seed_mixed_ledger(tmp_path / "paper.sqlite")
    services = _services(tmp_path)

    report = services.get_reconciliation_report()

    # The plan still sees every ledger row; P/L sees trusted rows only.
    assert report["total_rows"] == 5
    for key in ("before", "after_projected"):
        summary = report[key]
        assert summary["total"] == 3
        assert summary["won"] == 1 and summary["lost"] == 2
        assert summary["pl"] == pytest.approx(1.6 - 2.0)
    for key in ("before_quarantine", "after_projected_quarantine"):
        assert report[key]["market_evidence_quarantined_positions"] == 2
        assert report[key]["market_evidence_quarantined_by_market"] == {"asian_handicap": 2}


def test_reconcile_cli_report_excludes_quarantined_positions(tmp_path, capsys):
    from sports_ai import reconcile_football_paper

    _seed_mixed_ledger(tmp_path / "paper.sqlite")
    with pt.connect(tmp_path / "paper.sqlite") as conn:
        rows = pt.fetch_paper_trade_rows(conn)

    reconcile_football_paper._print_report(rows, {}, 0)
    out = capsys.readouterr().out

    assert "total paper rows:            5" in out
    assert "BEFORE  total=3 " in out and "AFTER   total=3 " in out
    assert "W=1 L=2" in out
    assert "P/L=-0.4 " in out
    assert "quarantined (excluded from P/L): before=2 after=2" in out


def test_market_performance_audit_excludes_quarantined_positions(tmp_path):
    from sports_ai.experiments import football_market_performance_audit as audit

    ids = _seed_mixed_ledger(tmp_path / "paper.sqlite")

    counted, conn = audit._load_counted_rows(tmp_path / "paper.sqlite")
    conn.close()
    result = audit.audit_market_performance(tmp_path / "paper.sqlite")

    assert {row["paper_trade_id"] for row in counted} == _trusted_ids(ids)
    assert result["raw_counted_rows"] == 3
    ah = result["raw"]["by_market"]["asian_handicap"]
    assert ah["positions"] == 2
    assert ah["wins"] == 0 and ah["losses"] == 2


def test_portfolio_candidate_rows_exclude_quarantined_positions(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from sports_ai.dashboard import services as services_module

    ids = _seed_mixed_ledger(tmp_path / "paper.sqlite")
    services = _services(tmp_path)
    monkeypatch.setattr(
        services_module.football_canonical_fixture,
        "fetch_canonical_fixture_ids_for_fixture_ids",
        lambda conn, fixture_ids, **_: {str(f): f"canon:{f}" for f in fixture_ids},
    )
    policy = SimpleNamespace(effective_from="2026-01-01T00:00:00+00:00")

    with pt.connect(tmp_path / "paper.sqlite") as conn:
        rows_by_trade_id, canonical_by_trade_id = services._counted_portfolio_paper_rows(conn, policy=policy)

    # Trusted rows are unchanged; legacy AH rows never reach portfolio selection.
    assert set(rows_by_trade_id) == _trusted_ids(ids)
    assert set(canonical_by_trade_id) == _trusted_ids(ids)
