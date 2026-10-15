"""Paper/candidate updates load only the snapshots they can use, with unchanged results."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

from sports_ai.dashboard import services as services_mod
from sports_ai.dashboard.models import FIXTURE_STATUS_FINISHED
from sports_ai.dashboard.production_services import ProductionDashboardServices
from sports_ai.dashboard.services import DashboardConfig, DashboardServices
from sports_ai.market_data.providers import NotConfiguredMarketProvider
from sports_ai.storage import football_candidate_evidence
from sports_ai.storage import football_paper_trading as pt

KICKOFF = "2026-10-10T14:00:00+00:00"
NEEDS_CLOSING = "api-football:1"
HAS_CLOSING = "api-football:2"
SNAPSHOTS_ONLY = "api-football:3"


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


def _snapshot(fixture_id: str, captured_at: str, odds: float) -> pt.StoredMarketSnapshot:
    return pt.StoredMarketSnapshot(
        fixture_id=fixture_id,
        provider="api-football",
        bookmaker="Pinnacle",
        market="corner_total",
        selection="OVER",
        line=9.5,
        odds=odds,
        ingestion_version="api_football_current_market_provider_v2",
        captured_at=captured_at,
        kickoff_utc=KICKOFF,
    )


def _entry(fixture_id: str) -> pt.PaperTradeEntry:
    return pt.PaperTradeEntry(
        prediction_id=f"prediction_{fixture_id}",
        fixture_id=fixture_id,
        model_snapshot_id="snapshot_a",
        model_version="v1",
        market="corner_total",
        selection="OVER",
        line=9.5,
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
    )


def _seed(db_path: Path) -> None:
    conn = pt.connect(db_path)
    pt.ensure_schema(conn)
    # Interleave captures across fixtures so per-fixture order is a real test.
    for minute, odds in ((50, 1.9), (40, 1.95), (30, 2.0), (20, 2.05)):
        for fixture_id in (SNAPSHOTS_ONLY, NEEDS_CLOSING, HAS_CLOSING):
            pt.record_market_snapshot(conn, _snapshot(fixture_id, f"2026-10-10T13:{60 - minute:02d}:00+00:00", odds))
    pt.record_paper_trade_entry(conn, _entry(NEEDS_CLOSING))
    has_closing = pt.record_paper_trade_entry(conn, _entry(HAS_CLOSING))
    stored_id = has_closing if isinstance(has_closing, str) else next(
        row["paper_trade_id"] for row in pt.fetch_paper_trade_rows(conn) if row["fixture_id"] == HAS_CLOSING
    )
    pt.attach_closing_snapshot(
        conn,
        paper_trade_id=str(stored_id),
        closing_odds=1.5,
        closing_captured_at="2026-10-10T13:00:00+00:00",
        closing_snapshot_id=1,
    )
    conn.close()


def _legacy_full_scan(conn, fixture_ids, *, initialize_schema=True):
    grouped: dict[str, list[dict]] = {}
    for row in pt.fetch_market_snapshots(conn, initialize_schema=initialize_schema):
        grouped.setdefault(str(row["fixture_id"]), []).append(row)
    return grouped


def _run(tmp_path: Path, monkeypatch, *, legacy: bool) -> list[tuple]:
    requested: list[set[str]] = []
    if legacy:
        monkeypatch.setattr(pt, "fetch_market_snapshots_by_fixture", _legacy_full_scan)
    else:
        real = pt.fetch_market_snapshots_by_fixture

        def spy(conn, fixture_ids, **kwargs):
            requested.append(set(fixture_ids))
            return real(conn, fixture_ids, **kwargs)

        monkeypatch.setattr(pt, "fetch_market_snapshots_by_fixture", spy)
    services = _services(tmp_path)
    fixtures = {
        fixture_id: {
            "fixture_id": fixture_id,
            "provider": "api-football",
            "status": FIXTURE_STATUS_FINISHED,
            "home_score": 1,
            "away_score": 0,
            "home_corners": 6,
            "away_corners": 5,
        }
        for fixture_id in (NEEDS_CLOSING, HAS_CLOSING)
    }
    monkeypatch.setattr(
        services,
        "_resolve_fixture_for_paper_settlement",
        lambda *, row, fixture_rows: fixtures.get(str(row["fixture_id"])),
    )
    services._process_paper_trading_updates([])
    monkeypatch.undo()
    if not legacy:
        assert requested == [{NEEDS_CLOSING}]
    conn = pt.connect(tmp_path / "paper.sqlite")
    try:
        return sorted(
            (
                row["fixture_id"],
                row["trade_state"],
                row.get("closing_odds"),
                row.get("closing_snapshot_id"),
                row.get("closing_captured_at"),
                row.get("clv_pct"),
            )
            for row in pt.fetch_paper_trade_rows(conn)
        )
    finally:
        conn.close()


def test_scoped_snapshot_load_gives_identical_paper_results(tmp_path: Path, monkeypatch) -> None:
    scoped_dir = tmp_path / "scoped"
    legacy_dir = tmp_path / "legacy"
    scoped_dir.mkdir()
    _seed(scoped_dir / "paper.sqlite")
    shutil.copytree(scoped_dir, legacy_dir, dirs_exist_ok=True)

    scoped = _run(scoped_dir, monkeypatch, legacy=False)
    legacy = _run(legacy_dir, monkeypatch, legacy=True)

    assert scoped == legacy
    by_fixture = {row[0]: row for row in scoped}
    # The missing closing price was attached from the latest in-window snapshot ...
    assert by_fixture[NEEDS_CLOSING][2] == 2.05
    # ... the stored one was never rewritten, and both positions settled.
    assert by_fixture[HAS_CLOSING][2] == 1.5
    assert {row[1] for row in scoped} == {"WON"}


def test_fixture_scoped_fetch_matches_full_scan_per_fixture(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "paper.sqlite"
    _seed(db_path)
    monkeypatch.setattr(pt, "_FIXTURE_ID_CHUNK_SIZE", 1)  # exercise chunking
    conn = pt.connect(db_path)
    try:
        full = _legacy_full_scan(conn, ())
        wanted = [NEEDS_CLOSING, SNAPSHOTS_ONLY, "api-football:missing"]
        scoped = pt.fetch_market_snapshots_by_fixture(conn, wanted)
        assert pt.fetch_market_snapshots_by_fixture(conn, []) == {}
    finally:
        conn.close()
    assert scoped == {fixture_id: full[fixture_id] for fixture_id in (NEEDS_CLOSING, SNAPSHOTS_ONLY)}


def test_candidate_evidence_loads_snapshots_only_for_open_closing(tmp_path: Path, monkeypatch) -> None:
    services = ProductionDashboardServices.__new__(ProductionDashboardServices)
    services.config = SimpleNamespace(paper_trading_db_path=tmp_path / "paper.sqlite")
    monkeypatch.setattr(ProductionDashboardServices, "_read_requires_schema_init", lambda self: True)
    observations = [
        {"observation_id": "o1", "fixture_id": NEEDS_CLOSING, "closing_odds": None, "kickoff": KICKOFF},
        {"observation_id": "o2", "fixture_id": HAS_CLOSING, "closing_odds": 1.8, "kickoff": KICKOFF},
    ]
    monkeypatch.setattr(football_candidate_evidence, "fetch_observations", lambda conn, active_only: observations)
    requested: list[set[str]] = []
    monkeypatch.setattr(
        pt,
        "fetch_market_snapshots_by_fixture",
        lambda conn, fixture_ids, **kwargs: requested.append(set(fixture_ids)) or {},
    )
    monkeypatch.setattr(
        ProductionDashboardServices,
        "_resolve_fixture_for_paper_settlement",
        lambda self, *, row, fixture_rows: None,
    )

    services._process_candidate_evidence_updates([])

    assert requested == [{NEEDS_CLOSING}]


def test_closing_lookup_ids_only_rows_without_closing() -> None:
    rows = [
        {"fixture_id": "a", "closing_odds": None},
        {"fixture_id": "b", "closing_odds": 1.9},
        {"fixture_id": "c"},
    ]
    assert services_mod._closing_lookup_fixture_ids(rows) == {"a", "c"}
