"""Focused tests for frozen Model Pick value evaluation (pure, DB-only contract)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sports_ai.dashboard.services as services_mod  # noqa: F401  (pre-import breaks the market_data/dashboard cycle)
from sports_ai.dashboard import frozen_value_evaluation as fv
from sports_ai.dashboard import storage
from sports_ai.dashboard.models import NormalizedFixture
from sports_ai.dashboard.services import (
    RUNTIME_ROLE_COMBINED,
    DashboardConfig,
    DashboardServices,
)
from sports_ai.market_data.providers import NotConfiguredMarketProvider
from sports_ai.storage import football_model_pick_ledger as ledger
from sports_ai.storage import football_paper_trading as pt

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
KICKOFF = "2026-10-08T15:00:00+00:00"


def _fixture(**overrides):
    base = {
        "fixture_id": "api-football:1",
        "provider": "api-football",
        "provider_fixture_id": "1",
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "division_code": "E0",
        "season": "2026",
        "home_team_name": "Home",
        "away_team_name": "Away",
        "kickoff_utc": KICKOFF,
    }
    base.update(overrides)
    return base


def _pick(market, selection, line, *, w=0.55, fair=1.82):
    return {
        "model_pick_id": "mp1",
        "fixture_id": "api-football:1",
        "market": market,
        "selection": selection,
        "line": line,
        "model_probability": w,
        "model_fair_price": fair,
        "model_as_of": "2026-10-08T10:00:00+00:00",
        "prediction_generated_at": "2026-10-08T10:00:00+00:00",
    }


def _row(market, selection, line, odds, bookmaker, *, captured="2026-10-08T11:50:00+00:00", source="2026-10-08T11:45:00+00:00", is_pre_match=True, snapshot_id=1, provider="api-football"):
    return {
        "fixture_id": "api-football:1", "market": market, "selection": selection, "line": line,
        "odds": odds, "bookmaker": bookmaker, "provider": provider, "captured_at": captured,
        "provider_timestamp": source, "is_pre_match": is_pre_match, "snapshot_id": snapshot_id,
        "freshness_state": "FRESH",
    }


def _complete_ah(*, ref_home=2.10, ref_away=1.80, other=None, line=-0.5):
    rows = [_row("asian_handicap", "HOME", line, ref_home, "Pinnacle"), _row("asian_handicap", "AWAY", line, ref_away, "Pinnacle")]
    if other is not None:
        rows.append(_row("asian_handicap", "HOME", line, other, "Betfair", snapshot_id=2))
    return rows


def _ev(pick, rows, *, fixture=None, now=NOW, safeguard=None, existing=False, lineage_ok=True):
    return fv.evaluate_frozen_pick(
        pick=pick, fixture=_fixture() if fixture is None else fixture, market_rows=rows,
        now_utc=now, safeguard_state=safeguard, existing_position=existing, existing_position_lineage_ok=lineage_ok,
    )


def test_value_bet_qualifies():
    result = _ev(_pick("asian_handicap", "HOME", -0.5), _complete_ah(other=3.00))
    assert result.state == fv.VALUE_BET
    assert result.selection == "HOME"  # frozen side
    assert result.best_price == 3.00
    assert result.reference_bookmaker == "Pinnacle"
    assert result.effective_win == 0.55
    assert result.effective_loss == pytest.approx(0.55 * 0.82)


def test_no_comparable_line_when_only_other_line():
    rows = [_row("asian_handicap", "HOME", -0.25, 3.0, "Pinnacle"), _row("asian_handicap", "AWAY", -0.25, 1.5, "Pinnacle")]
    result = _ev(_pick("asian_handicap", "HOME", -0.5), rows)
    assert result.state == fv.NO_COMPARABLE_MARKET_LINE


def test_better_price_on_other_line_cannot_redirect():
    # Frozen line -0.5 has no evidence; a huge price exists only at -0.25.
    rows = [_row("asian_handicap", "HOME", -0.25, 10.0, "Pinnacle"), _row("asian_handicap", "AWAY", -0.25, 1.05, "Pinnacle")]
    result = _ev(_pick("asian_handicap", "HOME", -0.5), rows)
    assert result.state == fv.NO_COMPARABLE_MARKET_LINE


def test_opposite_side_better_price_cannot_redirect():
    rows = _complete_ah()
    rows.append(_row("asian_handicap", "AWAY", -0.5, 5.0, "Betfair", snapshot_id=3))
    result = _ev(_pick("asian_handicap", "HOME", -0.5), rows)
    assert result.selection == "HOME"  # never switches to AWAY


def test_no_market_evidence():
    assert _ev(_pick("asian_handicap", "HOME", -0.5), []).state == fv.NO_MARKET_EVIDENCE


def test_stale_offer_is_market_stale():
    stale = _complete_ah(other=3.0)
    # make every row 300 minutes old
    for row in stale:
        row["captured_at"] = "2026-10-08T07:00:00+00:00"
        row["provider_timestamp"] = "2026-10-08T07:00:00+00:00"
    assert _ev(_pick("asian_handicap", "HOME", -0.5), stale).state == fv.MARKET_STALE


def test_stale_reference_book_is_market_stale():
    rows = [
        _row("asian_handicap", "HOME", -0.5, 2.10, "Pinnacle", captured="2026-10-08T07:00:00+00:00", source="2026-10-08T07:00:00+00:00"),
        _row("asian_handicap", "AWAY", -0.5, 1.80, "Pinnacle", captured="2026-10-08T07:00:00+00:00", source="2026-10-08T07:00:00+00:00"),
        _row("asian_handicap", "HOME", -0.5, 3.0, "Marathonbet", snapshot_id=2),
    ]
    assert _ev(_pick("asian_handicap", "HOME", -0.5), rows).state == fv.MARKET_STALE


def test_reference_absent_is_incomplete():
    rows = [_row("asian_handicap", "HOME", -0.5, 3.0, "Betfair", snapshot_id=2)]
    assert _ev(_pick("asian_handicap", "HOME", -0.5), rows).state == fv.REFERENCE_MARKET_INCOMPLETE


def test_fresh_offer_incomplete_reference():
    rows = [_row("asian_handicap", "HOME", -0.5, 2.10, "Pinnacle"), _row("asian_handicap", "HOME", -0.5, 3.0, "Betfair", snapshot_id=2)]
    assert _ev(_pick("asian_handicap", "HOME", -0.5), rows).state == fv.REFERENCE_MARKET_INCOMPLETE


def test_post_kickoff_blocked():
    result = _ev(_pick("asian_handicap", "HOME", -0.5), _complete_ah(other=3.0), now=datetime(2026, 10, 8, 16, 0, tzinfo=timezone.utc))
    assert result.state == fv.FIXTURE_ALREADY_STARTED


def test_in_play_rows_not_used():
    rows = _complete_ah(other=3.0)
    for row in rows:
        row["is_pre_match"] = False
    assert _ev(_pick("asian_handicap", "HOME", -0.5), rows).state == fv.MARKET_STALE


def test_source_timestamp_after_kickoff_blocked():
    rows = _complete_ah(other=3.0)
    for row in rows:
        row["provider_timestamp"] = "2026-10-08T15:30:00+00:00"
    assert _ev(_pick("asian_handicap", "HOME", -0.5), rows).state == fv.MARKET_STALE


def test_invalid_odds_blocked():
    rows = _complete_ah(other=1.0)
    assert _ev(_pick("asian_handicap", "HOME", -0.5), rows).state == fv.NO_MARKET_EVIDENCE


def test_identity_unresolved_missing_fixture():
    result = fv.evaluate_frozen_pick(pick=_pick("asian_handicap", "HOME", -0.5), fixture=None, market_rows=_complete_ah(), now_utc=NOW)
    assert result.state == fv.MARKET_IDENTITY_UNRESOLVED
    result2 = fv.evaluate_frozen_pick(pick=_pick("asian_handicap", "HOME", -0.5), fixture=_fixture(provider_fixture_id=""), market_rows=_complete_ah(), now_utc=NOW)
    assert result2.state == fv.MARKET_IDENTITY_UNRESOLVED


def test_240m_boundary_enforced():
    fresh = _complete_ah(other=3.0)
    # age 239 -> fresh
    for row in fresh:
        row["captured_at"] = "2026-10-08T08:01:00+00:00"
        row["provider_timestamp"] = "2026-10-08T08:01:00+00:00"
    assert _ev(_pick("asian_handicap", "HOME", -0.5), fresh).state == fv.VALUE_BET
    # age 241 -> stale
    for row in fresh:
        row["captured_at"] = "2026-10-08T07:59:00+00:00"
        row["provider_timestamp"] = "2026-10-08T07:59:00+00:00"
    assert _ev(_pick("asian_handicap", "HOME", -0.5), fresh).state == fv.MARKET_STALE


def test_no_bet_low_price():
    result = _ev(_pick("asian_handicap", "HOME", -0.5), _complete_ah(other=1.30))
    assert result.state == fv.NO_BET
    assert result.best_price == 1.30


def test_safeguard_blocked():
    result = _ev(_pick("asian_handicap", "HOME", -0.5), _complete_ah(other=3.0), safeguard="BLOCKED")
    assert result.state == fv.SAFEGUARD_BLOCKED


def test_existing_position():
    result = _ev(_pick("asian_handicap", "HOME", -0.5), _complete_ah(other=3.0), existing=True)
    assert result.state == fv.EXISTING_POSITION
    unresolved = _ev(_pick("asian_handicap", "HOME", -0.5), _complete_ah(other=3.0), existing=True, lineage_ok=False)
    assert unresolved.state == fv.EXISTING_POSITION_LINEAGE_UNRESOLVED


def test_missing_frozen_probability():
    pick = _pick("asian_handicap", "HOME", -0.5)
    pick["model_probability"] = None
    assert _ev(pick, _complete_ah(other=3.0)).state == fv.MODEL_PICK_UNAVAILABLE


def test_derived_loss_parity_helper():
    assert fv.derive_effective_loss_probability(model_probability=0.5, model_fair_price=3.0) == pytest.approx(1.0)
    assert fv.derive_effective_loss_probability(model_probability=0.55, model_fair_price=1.82) == pytest.approx(0.55 * 0.82)


def test_corners_value_bet():
    rows = [
        _row("corner_total", "OVER", 9.5, 1.95, "Pinnacle"),
        _row("corner_total", "UNDER", 9.5, 1.90, "Pinnacle"),
        _row("corner_total", "OVER", 9.5, 3.20, "Betfair", snapshot_id=2),
    ]
    result = _ev(_pick("corner_total", "OVER", 9.5), rows)
    assert result.state == fv.VALUE_BET
    assert result.selection == "OVER"


def test_action_hash_only_value_bets_and_deterministic():
    value = _ev(_pick("asian_handicap", "HOME", -0.5), _complete_ah(other=3.0))
    no_bet = _ev(_pick("asian_handicap", "HOME", -0.5), _complete_ah(other=1.30))
    h1 = fv.value_action_hash([value])
    h2 = fv.value_action_hash([value])
    assert h1 == h2
    assert fv.value_action_hash([no_bet]) != h1  # no-bet excluded -> empty hash


# --------------------------------------------------------------------------- services
class _NoProvider:
    def provider_name(self):
        return "api-football"

    def configured(self):
        return False

    def get_fixture(self, fixture_id):
        raise AssertionError("value evaluation must not call a provider")


def _config(tmp_path):
    return replace(
        DashboardConfig.default(),
        dashboard_db_path=tmp_path / "football_dashboard.sqlite",
        paper_trading_db_path=tmp_path / "football_paper_trading.sqlite",
        model_pick_db_path=tmp_path / "football_model_picks.sqlite",
        operational_data_dir=tmp_path,
        storage_mode="test_tmpdir",
        persistent_data_dir_configured=False,
        enable_scheduler=False,
        enable_value_bet_evaluation_scheduler=True,
        value_bet_evaluation_interval_seconds=600,
        runtime_role=RUNTIME_ROLE_COMBINED,
    )


def _seed_fixture(config, fixture_id, kickoff):
    provider, _, pfid = fixture_id.partition(":")
    normalized = NormalizedFixture(
        fixture_id=fixture_id, provider_fixture_id=pfid or fixture_id, provider=provider or "api-football",
        competition_id="ENG1", competition_name="Premier League", division_code="E0", season="2026",
        home_team_id="1", home_team_name="Home", away_team_id="2", away_team_name="Away",
        kickoff_utc=kickoff, status="PRE_MATCH", elapsed_minute=None, home_score=None, away_score=None,
        last_provider_update="2026-10-08T10:00:00+00:00", source_version="api_football_v1", provider_payload={},
    )
    with storage.connect(config.dashboard_db_path) as conn:
        storage.initialize_database(conn)
        storage.upsert_fixtures(conn, [normalized], updated_at="2026-10-08T10:00:00+00:00", initialize_schema=False)


def _seed_snapshot(config, fixture_id, market, selection, line, odds, bookmaker, source, captured):
    with pt.connect(config.paper_trading_db_path) as conn:
        pt.ensure_schema(conn)
        pt.record_market_snapshot(
            conn,
            pt.StoredMarketSnapshot(
                fixture_id=fixture_id, provider="api-football", bookmaker=bookmaker, market=market,
                selection=selection, odds=odds, ingestion_version="api_football_current_market_provider_v1",
                line=line, provider_timestamp=source, captured_at=captured, is_pre_match=True,
                provider_fixture_id=fixture_id.split(":")[-1],
            ),
            initialize_schema=False,
        )


def _seed_pick(config, fixture_id, market, selection, line, *, w, fair):
    conn = ledger.connect(config.model_pick_db_path)
    ledger.ensure_schema(conn)
    conn.execute(
        """
        INSERT INTO football_model_pick_entries (
            model_pick_id, policy_id, policy_version, fixture_id, market, selection, line,
            model_probability, model_fair_price, model_as_of, record_origin, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (f"mp_{fixture_id}_{market}", "football_model_pick_balanced_line_v1", "v1", fixture_id, market, selection, line,
         w, fair, "2026-10-08T10:00:00+00:00", "LIVE_ISSUED", "2026-10-08T10:00:00+00:00", "2026-10-08T10:00:00+00:00"),
    )
    conn.commit()
    conn.close()


def test_value_cycle_reads_snapshots_from_paper_db(tmp_path):
    config = _config(tmp_path)
    kickoff = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(microsecond=0).isoformat()
    fid = "api-football:1"
    source = (datetime.now(timezone.utc) - timedelta(minutes=30)).replace(microsecond=0).isoformat()
    captured = (datetime.now(timezone.utc) - timedelta(minutes=28)).replace(microsecond=0).isoformat()
    _seed_fixture(config, fid, kickoff)
    _seed_snapshot(config, fid, "asian_handicap", "HOME", -0.5, 2.10, "Pinnacle", source, captured)
    _seed_snapshot(config, fid, "asian_handicap", "AWAY", -0.5, 1.80, "Pinnacle", source, captured)
    _seed_snapshot(config, fid, "asian_handicap", "HOME", -0.5, 3.00, "Betfair", source, captured)
    _seed_pick(config, fid, "asian_handicap", "HOME", -0.5, w=0.55, fair=1.82)
    services = DashboardServices(config, fixture_provider=_NoProvider(), market_provider=NotConfiguredMarketProvider())
    result = services.run_value_bet_evaluation_dry_run()
    assert result["counts"].get(fv.VALUE_BET) == 1
    assert result["eligible"][0]["selection"] == "HOME"
    assert services._value_evaluation_runtime_state  # sanity
    with pt.connect(config.paper_trading_db_path) as conn:
        assert conn.execute("select count(*) from paper_trade_entries").fetchone()[0] == 0
        assert conn.execute("select count(*) from paper_trade_updates").fetchone()[0] == 0


def test_value_cycle_persists_state_without_writes(tmp_path):
    config = _config(tmp_path)
    kickoff = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(microsecond=0).isoformat()
    fid = "api-football:2"
    source = (datetime.now(timezone.utc) - timedelta(minutes=20)).replace(microsecond=0).isoformat()
    _seed_fixture(config, fid, kickoff)
    _seed_snapshot(config, fid, "corner_total", "OVER", 9.5, 1.95, "Pinnacle", source, source)
    _seed_snapshot(config, fid, "corner_total", "UNDER", 9.5, 1.90, "Pinnacle", source, source)
    _seed_snapshot(config, fid, "corner_total", "OVER", 9.5, 3.20, "Betfair", source, source)
    _seed_pick(config, fid, "corner_total", "OVER", 9.5, w=0.55, fair=1.82)
    services = DashboardServices(config, fixture_provider=_NoProvider(), market_provider=NotConfiguredMarketProvider())
    result = services.run_value_bet_evaluation_cycle()
    assert result["counts"].get(fv.VALUE_BET) == 1
    assert services._value_evaluation_runtime_state["value_evaluation_value_bets_last_cycle"] == 1
    with storage.connect(config.dashboard_db_path) as conn:
        state = storage.get_runtime_state(conn, "value_evaluation_state", initialize_schema=False)
    assert state is not None and state["value_evaluation_cycles_completed"] == 1
    assert not Path(config.paper_trading_db_path).with_name("paper_trade_entries").exists()
