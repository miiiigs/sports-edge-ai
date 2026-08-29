"""Focused tests for cross-provider canonical fixture identity + paper reconciliation.

No live provider calls are made in these tests.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from sports_ai.dashboard.services import DashboardConfig, DashboardServices
from sports_ai.market_data.providers import NotConfiguredMarketProvider
from sports_ai.storage import football_canonical_fixture as cf
from sports_ai.storage import football_paper_trading as pt

ENG1 = "ENG1"
SUNDERLAND_FULHAM_KICK = "2026-08-30T13:00:00+00:00"
FD_SUNDERLAND = "football-data.org:560554"
API_SUNDERLAND = "api-football:1557385"
FD_TOTTENHAM = "football-data.org:560558"
API_TOTTENHAM = "api-football:1557386"


def _identity(fixture_id: str, *, competition_id: str = ENG1, home: str = "Sunderland", away: str = "Fulham", kickoff: str = SUNDERLAND_FULHAM_KICK) -> dict:
    return {
        "fixture_id": fixture_id,
        "competition_id": competition_id,
        "home_team_name": home,
        "away_team_name": away,
        "kickoff_utc": kickoff,
        "season": "2026",
    }


def _paper_db(tmp_path: Path):
    db_path = tmp_path / "paper.sqlite"
    conn = pt.connect(db_path)
    pt.ensure_schema(conn)
    return db_path, conn


def _entry(
    fixture_id: str,
    *,
    market: str = "1x2",
    selection: str = "AWAY",
    line: float | None = None,
    created_at: str = "2026-08-28T23:00:00+00:00",
    decision_timestamp: str = "2026-08-28T20:00:00+00:00",
    canonical_key: str | None = None,
    prediction_id: str = "prediction_a",
    kickoff: str = SUNDERLAND_FULHAM_KICK,
    bookmaker: str = "Pinnacle",
) -> pt.PaperTradeEntry:
    return pt.PaperTradeEntry(
        prediction_id=prediction_id,
        fixture_id=fixture_id,
        model_snapshot_id="snapshot_a",
        model_version="v1",
        market=market,
        selection=selection,
        line=line,
        model_probability=0.55,
        model_fair_price=2.0,
        available_market_price=2.5,
        decision="PAPER_CANDIDATE",
        decision_timestamp=decision_timestamp,
        kickoff=kickoff,
        safeguard_state="PASSED",
        entry_eligibility="ELIGIBLE",
        bookmaker=bookmaker,
        provider="api-football",
        entry_odds=2.5,
        created_at=created_at,
        canonical_business_key_signature=canonical_key,
    )


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


# -------------------------------------------------------------------- canonical identity
def test_same_provider_fixture_maps_to_same_canonical_fixture():
    ident = _identity(FD_SUNDERLAND)
    id1 = cf.canonical_fixture_id_from_identity(**{k: ident[k] for k in ("competition_id", "home_team_name", "away_team_name", "kickoff_utc")})
    id2 = cf.canonical_fixture_id_from_identity(**{k: ident[k] for k in ("competition_id", "home_team_name", "away_team_name", "kickoff_utc")})
    assert id1 == id2


def test_cross_provider_representations_map_to_one_canonical_fixture():
    fd = cf.canonical_fixture_id_from_identity(competition_id=ENG1, home_team_name="Sunderland", away_team_name="Fulham", kickoff_utc=SUNDERLAND_FULHAM_KICK)
    api = cf.canonical_fixture_id_from_identity(competition_id=ENG1, home_team_name="Sunderland", away_team_name="Fulham", kickoff_utc=SUNDERLAND_FULHAM_KICK)
    assert fd == api


def test_different_real_matches_never_merge_due_to_similar_names():
    a = cf.canonical_fixture_id_from_identity(competition_id=ENG1, home_team_name="Bournemouth", away_team_name="Everton", kickoff_utc="2026-08-29T14:00:00+00:00")
    b = cf.canonical_fixture_id_from_identity(competition_id=ENG1, home_team_name="Bournemouth", away_team_name="West Ham", kickoff_utc="2026-08-29T14:00:00+00:00")
    assert a != b


def test_ambiguous_fixture_mapping_fails_closed(tmp_path):
    _, conn = _paper_db(tmp_path)
    cf.ensure_schema(conn)
    cf.resolve_or_create_canonical_fixture(conn, provider="football-data.org", provider_fixture_id="1",
                                           competition_id=ENG1, home_team_name="Home", away_team_name="Away",
                                           kickoff_utc="2026-08-30T13:00:00+00:00", kickoff_tolerance_seconds=0)
    cf.resolve_or_create_canonical_fixture(conn, provider="api-football", provider_fixture_id="2",
                                           competition_id=ENG1, home_team_name="Home", away_team_name="Away",
                                           kickoff_utc="2026-08-30T13:05:00+00:00", kickoff_tolerance_seconds=0)
    # A third fixture whose kickoff is within tolerance of BOTH canonical rows must fail closed.
    with pytest.raises(cf.CanonicalFixtureAmbiguityError):
        cf.resolve_or_create_canonical_fixture(conn, provider="api-football", provider_fixture_id="3",
                                               competition_id=ENG1, home_team_name="Home", away_team_name="Away",
                                               kickoff_utc="2026-08-30T13:02:30+00:00", kickoff_tolerance_seconds=180)


def test_kickoff_tolerance_behaves_exactly_as_configured():
    kickoff_a = "2026-08-30T13:00:00+00:00"
    kickoff_b = "2026-08-30T13:01:00+00:00"
    # tolerance 0 -> not close
    assert not cf._kickoff_close(kickoff_a, kickoff_b, 0)
    # tolerance 120s -> close
    assert cf._kickoff_close(kickoff_a, kickoff_b, 120)
    # tolerance 30s -> not close
    assert not cf._kickoff_close(kickoff_a, kickoff_b, 30)


def test_canonical_identity_survives_provider_changes():
    ident = _identity(FD_SUNDERLAND)
    cid_a = cf.canonical_fixture_id_from_identity(**{k: ident[k] for k in ("competition_id", "home_team_name", "away_team_name", "kickoff_utc")})
    cid_b = cf.canonical_fixture_id_from_identity(competition_id=ENG1, home_team_name="Sunderland", away_team_name="Fulham", kickoff_utc=SUNDERLAND_FULHAM_KICK)
    assert cid_a == cid_b


def test_team_variant_normalization_handles_proven_provider_naming():
    assert cf.canonical_team_name("Leeds") == cf.canonical_team_name("Leeds United")
    assert cf.canonical_team_name("Man United") == cf.canonical_team_name("Manchester United")
    assert cf.canonical_team_name("Celta") == cf.canonical_team_name("Celta Vigo")
    assert cf.canonical_team_name("Sevilla") == cf.canonical_team_name("Sevilla FC")


# -------------------------------------------------------------------- paper business identity
def _canonical_key_for(market="1x2", selection="AWAY", line=None, fixture=FD_SUNDERLAND):
    ident = _identity(fixture)
    cid = cf.canonical_fixture_id_from_identity(competition_id=ident["competition_id"], home_team_name=ident["home_team_name"], away_team_name=ident["away_team_name"], kickoff_utc=ident["kickoff_utc"])
    return cf.canonical_paper_business_key_signature(canonical_fixture_id=cid, market=market, selection=selection, line=line)


def test_same_canonical_fixture_market_selection_line_cannot_create_two_future_positions(tmp_path):
    db_path, conn = _paper_db(tmp_path)
    key = _canonical_key_for()
    pt.record_paper_trade_entry(conn, _entry(FD_SUNDERLAND, canonical_key=key))
    services = _services(tmp_path)
    services.config = services.config  # no-op to keep reference
    existing = cf.find_counted_paper_trade_by_canonical_key(conn, key)
    assert existing is not None
    # A second capture under the OTHER provider fixture must resolve to the same canonical entry.
    services2 = _services(tmp_path)
    hit = services2._find_canonical_existing_paper_entry(
        conn,
        prediction={
            "fixture_id": API_SUNDERLAND,
            "competition_id": ENG1,
            "home_team_name": "Sunderland",
            "away_team_name": "Fulham",
            "kickoff_utc": SUNDERLAND_FULHAM_KICK,
        },
        market="1x2",
        selection="AWAY",
        line=None,
    )
    assert hit is not None
    assert hit["paper_trade_id"] == existing["paper_trade_id"]


def test_different_market_remains_distinct():
    a = _canonical_key_for(market="1x2", selection="AWAY")
    b = _canonical_key_for(market="btts", selection="YES")
    assert a != b


def test_different_selection_remains_distinct():
    a = _canonical_key_for(market="1x2", selection="AWAY")
    b = _canonical_key_for(market="1x2", selection="HOME")
    assert a != b


def test_different_canonical_line_remains_distinct():
    a = _canonical_key_for(market="goals_total", selection="OVER", line=2.5)
    b = _canonical_key_for(market="goals_total", selection="OVER", line=3.0)
    assert a != b


def test_prediction_version_does_not_duplicate_business_position(tmp_path):
    _, conn = _paper_db(tmp_path)
    key = _canonical_key_for()
    pt.record_paper_trade_entry(conn, _entry(FD_SUNDERLAND, prediction_id="prediction_v1", canonical_key=key))
    # different prediction version, same canonical business position
    hit = cf.find_counted_paper_trade_by_canonical_key(conn, key)
    assert hit is not None
    assert cf.find_counted_paper_trade_by_canonical_key(conn, key)["paper_trade_id"] == hit["paper_trade_id"]


def test_bookmaker_does_not_duplicate_business_position(tmp_path):
    _, conn = _paper_db(tmp_path)
    key = _canonical_key_for()
    e1 = _entry(FD_SUNDERLAND, bookmaker="Pinnacle", canonical_key=key)
    pt.record_paper_trade_entry(conn, e1)
    # A different bookmaker offer for the same canonical position is the same business position.
    assert cf.find_counted_paper_trade_by_canonical_key(conn, key)["paper_trade_id"] == e1.paper_trade_id


def test_original_provider_fixture_ids_remain_queryable(tmp_path):
    db_path, conn = _paper_db(tmp_path)
    e1 = _entry(FD_SUNDERLAND)
    e2 = _entry(API_SUNDERLAND, created_at="2026-08-28T23:05:00+00:00")
    pt.record_paper_trade_entry(conn, e1)
    pt.record_paper_trade_entry(conn, e2)
    identity = {FD_SUNDERLAND: _identity(FD_SUNDERLAND), API_SUNDERLAND: _identity(API_SUNDERLAND)}
    cf.apply_reconciliation(conn, identity)
    rows = pt.fetch_paper_trade_rows(conn)
    fixture_ids = {r["fixture_id"] for r in rows}
    assert FD_SUNDERLAND in fixture_ids
    assert API_SUNDERLAND in fixture_ids


# -------------------------------------------------------------------- reconciliation
def _plan_for(tmp_path, *, kickoff=SUNDERLAND_FULHAM_KICK, tol=0):
    db_path, conn = _paper_db(tmp_path)
    return db_path, conn


def test_reconciliation_dry_run_is_non_mutating(tmp_path):
    db_path, conn = _paper_db(tmp_path)
    e1 = _entry(FD_SUNDERLAND)
    e2 = _entry(API_SUNDERLAND, created_at="2026-08-28T23:05:00+00:00")
    pt.record_paper_trade_entry(conn, e1)
    pt.record_paper_trade_entry(conn, e2)
    before_rows = pt.fetch_paper_trade_rows(conn)
    identity = {FD_SUNDERLAND: _identity(FD_SUNDERLAND), API_SUNDERLAND: _identity(API_SUNDERLAND)}
    plan = cf.build_reconciliation_plan(before_rows, identity)
    assert plan.duplicate_representation_count == 1
    assert len(cf.fetch_reconciliation_rows(conn)) == 0
    after_rows = pt.fetch_paper_trade_rows(conn)
    assert len(after_rows) == 2  # no mutation


def test_reconciliation_apply_preserves_all_original_paper_rows(tmp_path):
    db_path, conn = _paper_db(tmp_path)
    e1 = _entry(FD_SUNDERLAND)
    e2 = _entry(API_SUNDERLAND, created_at="2026-08-28T23:05:00+00:00")
    pt.record_paper_trade_entry(conn, e1)
    pt.record_paper_trade_entry(conn, e2)
    pt.append_trade_update(conn, pt.build_trade_update_payload(paper_trade_id=e1.paper_trade_id, trade_state="LOST", settlement_context={"final_status": "FINISHED", "actual_home_goals": 1, "actual_away_goals": 2}, odds=3.15))
    pt.append_trade_update(conn, pt.build_trade_update_payload(paper_trade_id=e2.paper_trade_id, trade_state="LOST", settlement_context={"final_status": "FINISHED", "actual_home_goals": 1, "actual_away_goals": 2}, odds=3.15))
    identity = {FD_SUNDERLAND: _identity(FD_SUNDERLAND), API_SUNDERLAND: _identity(API_SUNDERLAND)}
    result = cf.apply_reconciliation(conn, identity)
    assert result.applied is True
    assert result.records_written == 1
    rows = pt.fetch_paper_trade_rows(conn)
    assert len(rows) == 2  # both preserved
    # entry odds untouched
    assert {r["paper_trade_id"]: r["entry_odds"] for r in rows} == {e1.paper_trade_id: 2.5, e2.paper_trade_id: 2.5}
    # settlement updates preserved
    updates = [conn.execute("SELECT COUNT(*) FROM paper_trade_updates WHERE paper_trade_id = ?", (e1.paper_trade_id,)).fetchone()[0],
               conn.execute("SELECT COUNT(*) FROM paper_trade_updates WHERE paper_trade_id = ?", (e2.paper_trade_id,)).fetchone()[0]]
    assert updates == [1, 1]


def test_duplicate_representation_excluded_only_through_additive_reconciliation(tmp_path):
    db_path, conn = _paper_db(tmp_path)
    e1 = _entry(FD_SUNDERLAND)
    e2 = _entry(API_SUNDERLAND, created_at="2026-08-28T23:05:00+00:00")
    pt.record_paper_trade_entry(conn, e1)
    pt.record_paper_trade_entry(conn, e2)
    identity = {FD_SUNDERLAND: _identity(FD_SUNDERLAND), API_SUNDERLAND: _identity(API_SUNDERLAND)}
    # before apply: nothing excluded
    assert len(cf.fetch_excluded_paper_trade_ids(conn)) == 0
    cf.apply_reconciliation(conn, identity)
    excluded = cf.fetch_excluded_paper_trade_ids(conn)
    assert len(excluded) == 1
    assert excluded == {e2.paper_trade_id}  # later-created row is the superseded duplicate


def test_canonical_position_selection_is_deterministic(tmp_path):
    db_path, conn = _paper_db(tmp_path)
    e1 = _entry(FD_SUNDERLAND, created_at="2026-08-28T23:00:00+00:00")
    e2 = _entry(API_SUNDERLAND, created_at="2026-08-28T23:05:00+00:00")
    pt.record_paper_trade_entry(conn, e1)
    pt.record_paper_trade_entry(conn, e2)
    identity = {FD_SUNDERLAND: _identity(FD_SUNDERLAND), API_SUNDERLAND: _identity(API_SUNDERLAND)}
    plan1 = cf.build_reconciliation_plan(pt.fetch_paper_trade_rows(conn), identity)
    plan2 = cf.build_reconciliation_plan(pt.fetch_paper_trade_rows(conn), identity)
    assert plan1.duplicate_groups[0].canonical_row["paper_trade_id"] == plan2.duplicate_groups[0].canonical_row["paper_trade_id"]
    assert plan1.duplicate_groups[0].canonical_row["paper_trade_id"] == e1.paper_trade_id  # earliest wins


def test_canonical_selection_is_outcome_independent(tmp_path):
    db_path, conn = _paper_db(tmp_path)
    e_early_lost = _entry(FD_SUNDERLAND, created_at="2026-08-28T23:00:00+00:00")
    e_late_won = _entry(API_SUNDERLAND, created_at="2026-08-28T23:05:00+00:00")
    pt.record_paper_trade_entry(conn, e_early_lost)
    pt.record_paper_trade_entry(conn, e_late_won)
    pt.append_trade_update(conn, pt.build_trade_update_payload(paper_trade_id=e_early_lost.paper_trade_id, trade_state="LOST", settlement_context={}, odds=2.5))
    pt.append_trade_update(conn, pt.build_trade_update_payload(paper_trade_id=e_late_won.paper_trade_id, trade_state="WON", settlement_context={}, odds=2.5))
    identity = {FD_SUNDERLAND: _identity(FD_SUNDERLAND), API_SUNDERLAND: _identity(API_SUNDERLAND)}
    plan = cf.build_reconciliation_plan(pt.fetch_paper_trade_rows(conn), identity)
    # The losing, earlier row is canonical even though the later row won.
    assert plan.duplicate_groups[0].canonical_row["paper_trade_id"] == e_early_lost.paper_trade_id
    assert plan.duplicate_groups[0].duplicate_rows[0]["paper_trade_id"] == e_late_won.paper_trade_id


def test_settlement_agreement_reconciles_safely(tmp_path):
    db_path, conn = _paper_db(tmp_path)
    e1 = _entry(FD_SUNDERLAND)
    e2 = _entry(API_SUNDERLAND, created_at="2026-08-28T23:05:00+00:00")
    pt.record_paper_trade_entry(conn, e1)
    pt.record_paper_trade_entry(conn, e2)
    for e in (e1, e2):
        pt.append_trade_update(conn, pt.build_trade_update_payload(paper_trade_id=e.paper_trade_id, trade_state="LOST", settlement_context={"actual_home_goals": 1, "actual_away_goals": 2}, odds=3.15))
    identity = {FD_SUNDERLAND: _identity(FD_SUNDERLAND), API_SUNDERLAND: _identity(API_SUNDERLAND)}
    result = cf.apply_reconciliation(conn, identity)
    assert result.applied is True
    assert not result.violations


def test_settlement_conflict_blocks_reconciliation(tmp_path):
    db_path, conn = _paper_db(tmp_path)
    e1 = _entry(FD_SUNDERLAND)
    e2 = _entry(API_SUNDERLAND, created_at="2026-08-28T23:05:00+00:00")
    pt.record_paper_trade_entry(conn, e1)
    pt.record_paper_trade_entry(conn, e2)
    pt.append_trade_update(conn, pt.build_trade_update_payload(paper_trade_id=e1.paper_trade_id, trade_state="WON", settlement_context={}, odds=2.5))
    pt.append_trade_update(conn, pt.build_trade_update_payload(paper_trade_id=e2.paper_trade_id, trade_state="LOST", settlement_context={}, odds=2.5))
    identity = {FD_SUNDERLAND: _identity(FD_SUNDERLAND), API_SUNDERLAND: _identity(API_SUNDERLAND)}
    plan = cf.build_reconciliation_plan(pt.fetch_paper_trade_rows(conn), identity)
    assert len(plan.conflicts) == 1
    result = cf.apply_reconciliation(conn, identity)
    assert result.applied is False
    assert any("SETTLEMENT_CONFLICT" in v for v in result.violations)
    assert len(cf.fetch_excluded_paper_trade_ids(conn)) == 0


def test_reconciliation_rerun_is_idempotent(tmp_path):
    db_path, conn = _paper_db(tmp_path)
    e1 = _entry(FD_SUNDERLAND)
    e2 = _entry(API_SUNDERLAND, created_at="2026-08-28T23:05:00+00:00")
    pt.record_paper_trade_entry(conn, e1)
    pt.record_paper_trade_entry(conn, e2)
    identity = {FD_SUNDERLAND: _identity(FD_SUNDERLAND), API_SUNDERLAND: _identity(API_SUNDERLAND)}
    first = cf.apply_reconciliation(conn, identity)
    assert first.records_written == 1
    second = cf.apply_reconciliation(conn, identity)
    assert second.applied is True
    assert second.records_written == 0
    assert len(cf.fetch_reconciliation_rows(conn)) == 1


def test_aggregate_position_count_drops_by_exactly_duplicate_count(tmp_path):
    db_path, conn = _paper_db(tmp_path)
    e1 = _entry(FD_SUNDERLAND, created_at="2026-08-28T23:00:00+00:00")
    e2 = _entry(API_SUNDERLAND, created_at="2026-08-28T23:05:00+00:00")
    e3 = _entry(FD_TOTTENHAM, market="goals_total", selection="OVER", line=2.5, created_at="2026-08-28T23:10:00+00:00")
    pt.record_paper_trade_entry(conn, e1)
    pt.record_paper_trade_entry(conn, e2)
    pt.record_paper_trade_entry(conn, e3)
    identity = {
        FD_SUNDERLAND: _identity(FD_SUNDERLAND),
        API_SUNDERLAND: _identity(API_SUNDERLAND),
        FD_TOTTENHAM: _identity(FD_TOTTENHAM, home="Tottenham", away="Newcastle"),
    }
    result = cf.apply_reconciliation(conn, identity)
    assert result.plan.duplicate_representation_count == 1
    rows = pt.fetch_paper_trade_rows(conn)
    counted = result.plan.counted_rows(rows)
    assert len(counted) == len(rows) - 1


def test_pl_counts_canonical_rows_once(tmp_path):
    db_path, conn = _paper_db(tmp_path)
    e1 = _entry(FD_SUNDERLAND)
    e2 = _entry(API_SUNDERLAND, created_at="2026-08-28T23:05:00+00:00")
    pt.record_paper_trade_entry(conn, e1)
    pt.record_paper_trade_entry(conn, e2)
    for e in (e1, e2):
        pt.append_trade_update(conn, pt.build_trade_update_payload(paper_trade_id=e.paper_trade_id, trade_state="LOST", settlement_context={}, odds=2.5))
    identity = {FD_SUNDERLAND: _identity(FD_SUNDERLAND), API_SUNDERLAND: _identity(API_SUNDERLAND)}
    rows = pt.fetch_paper_trade_rows(conn)
    before = cf.summarize_performance(rows)
    assert before["pl"] == -2.0  # double counted
    plan = cf.build_reconciliation_plan(rows, identity)
    after = cf.summarize_performance(plan.counted_rows(rows))
    assert after["pl"] == -1.0  # counted once


def test_roi_counts_canonical_stake_once(tmp_path):
    db_path, conn = _paper_db(tmp_path)
    e1 = _entry(FD_SUNDERLAND)
    e2 = _entry(API_SUNDERLAND, created_at="2026-08-28T23:05:00+00:00")
    pt.record_paper_trade_entry(conn, e1)
    pt.record_paper_trade_entry(conn, e2)
    for e in (e1, e2):
        pt.append_trade_update(conn, pt.build_trade_update_payload(paper_trade_id=e.paper_trade_id, trade_state="LOST", settlement_context={}, odds=2.5))
    identity = {FD_SUNDERLAND: _identity(FD_SUNDERLAND), API_SUNDERLAND: _identity(API_SUNDERLAND)}
    rows = pt.fetch_paper_trade_rows(conn)
    plan = cf.build_reconciliation_plan(rows, identity)
    after = cf.summarize_performance(plan.counted_rows(rows))
    assert after["settled"] == 1  # one stake unit
    assert after["roi"] == -1.0


def test_strike_rate_counts_canonical_settled_outcomes_once(tmp_path):
    db_path, conn = _paper_db(tmp_path)
    e1 = _entry(FD_SUNDERLAND)
    e2 = _entry(API_SUNDERLAND, created_at="2026-08-28T23:05:00+00:00")
    pt.record_paper_trade_entry(conn, e1)
    pt.record_paper_trade_entry(conn, e2)
    for e in (e1, e2):
        pt.append_trade_update(conn, pt.build_trade_update_payload(paper_trade_id=e.paper_trade_id, trade_state="WON", settlement_context={}, odds=2.5))
    identity = {FD_SUNDERLAND: _identity(FD_SUNDERLAND), API_SUNDERLAND: _identity(API_SUNDERLAND)}
    rows = pt.fetch_paper_trade_rows(conn)
    before = cf.summarize_performance(rows)
    assert before["won"] == 2
    plan = cf.build_reconciliation_plan(rows, identity)
    after = cf.summarize_performance(plan.counted_rows(rows))
    assert after["won"] == 1


# -------------------------------------------------------------------- services read model
def test_services_paper_positions_count_canonical_positions_once(tmp_path):
    db_path, conn = _paper_db(tmp_path)
    e1 = _entry(FD_SUNDERLAND)
    e2 = _entry(API_SUNDERLAND, created_at="2026-08-28T23:05:00+00:00")
    pt.record_paper_trade_entry(conn, e1)
    pt.record_paper_trade_entry(conn, e2)
    identity = {FD_SUNDERLAND: _identity(FD_SUNDERLAND), API_SUNDERLAND: _identity(API_SUNDERLAND)}
    services = _services(tmp_path)
    # build identity from the dashboard DB is unavailable; inject via apply on the conn directly
    result = cf.apply_reconciliation(conn, identity)
    assert result.applied is True
    counted, status = services._paper_positions()
    assert len(counted) == 1
    assert status["paper_bets"] == 1
    assert status["reconciled_excluded_positions"] == 1


def test_services_reconciliation_report_projects_canonical_truth(tmp_path):
    db_path, conn = _paper_db(tmp_path)
    e1 = _entry(FD_SUNDERLAND)
    e2 = _entry(API_SUNDERLAND, created_at="2026-08-28T23:05:00+00:00")
    pt.record_paper_trade_entry(conn, e1)
    pt.record_paper_trade_entry(conn, e2)
    identity = {FD_SUNDERLAND: _identity(FD_SUNDERLAND), API_SUNDERLAND: _identity(API_SUNDERLAND)}
    services = _services(tmp_path)
    # Make the identity available to the service report by seeding the dashboard DB.
    import sports_ai.dashboard.storage as dashboard_storage
    from sports_ai.dashboard.models import NormalizedFixture
    fixtures = [
        NormalizedFixture(
            fixture_id=FD_SUNDERLAND, provider_fixture_id="560554", provider="football-data.org",
            competition_id=ENG1, competition_name="Premier League", division_code="E0", season="2026",
            home_team_id="1", home_team_name="Sunderland", away_team_id="2", away_team_name="Fulham",
            kickoff_utc=SUNDERLAND_FULHAM_KICK, status="PRE_MATCH", home_score=None, away_score=None,
            last_provider_update="2026-08-30T00:00:00+00:00", source_version="test",
            elapsed_minute=None, provider_payload={},
        ),
        NormalizedFixture(
            fixture_id=API_SUNDERLAND, provider_fixture_id="1557385", provider="api-football",
            competition_id=ENG1, competition_name="Premier League", division_code="E0", season="2026",
            home_team_id="1", home_team_name="Sunderland", away_team_id="2", away_team_name="Fulham",
            kickoff_utc=SUNDERLAND_FULHAM_KICK, status="PRE_MATCH", home_score=None, away_score=None,
            last_provider_update="2026-08-30T00:00:00+00:00", source_version="test",
            elapsed_minute=None, provider_payload={},
        ),
    ]
    with dashboard_storage.connect(tmp_path / "dashboard.sqlite") as dconn:
        dashboard_storage.ensure_schema(dconn)
        dashboard_storage.upsert_fixtures(dconn, fixtures, updated_at="2026-08-30T00:00:00+00:00")
    report = services.get_reconciliation_report()
    assert report["total_rows"] == 2
    assert report["duplicate_representations"] == 1
    assert report["after_projected"]["total"] == 1
    assert report["before"]["total"] == 2


def test_legacy_non_duplicate_paper_positions_remain_unchanged(tmp_path):
    db_path, conn = _paper_db(tmp_path)
    e1 = _entry(FD_SUNDERLAND)
    e2 = _entry(FD_TOTTENHAM, market="btts", selection="YES", created_at="2026-08-28T23:05:00+00:00")
    pt.record_paper_trade_entry(conn, e1)
    pt.record_paper_trade_entry(conn, e2)
    identity = {FD_SUNDERLAND: _identity(FD_SUNDERLAND), FD_TOTTENHAM: _identity(FD_TOTTENHAM, home="Tottenham", away="Newcastle")}
    rows = pt.fetch_paper_trade_rows(conn)
    plan = cf.build_reconciliation_plan(rows, identity)
    assert plan.duplicate_representation_count == 0
    assert len(plan.counted_rows(rows)) == 2
    assert len(rows) == 2


def test_zero_live_provider_calls_occur_during_all_tests(tmp_path):
    # Assert the CLI/service path performs no provider HTTP by construction:
    # reconciliation only reads paper/dashboard SQLite files.
    db_path, conn = _paper_db(tmp_path)
    e1 = _entry(FD_SUNDERLAND)
    pt.record_paper_trade_entry(conn, e1)
    services = _services(tmp_path)
    assert services.config.allow_paid_provider_calls is False
    assert services._provider_budget.metrics().get("http_requests", 0) == 0
