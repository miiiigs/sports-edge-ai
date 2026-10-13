"""End-to-end forward Model Pick lifecycle on the real dashboard schema.

fixture -> READY prediction -> pre-kickoff issuance -> market collection pool
-> verified result -> settlement -> forward-only performance. No provider calls,
no paper-ledger writes, no Value Bets.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

from sports_ai.dashboard import model_pick_issuance as issuance
from sports_ai.dashboard import storage
from sports_ai.dashboard.model_pick_history import build_model_pick_history_payload
from sports_ai.dashboard.services import RUNTIME_ROLE_SCHEDULER, DashboardConfig, DashboardServices
from sports_ai.dashboard.models import NormalizedFixture
from sports_ai.market_data.providers import NotConfiguredMarketProvider
from sports_ai.storage import football_model_pick_ledger as ledger

from test_football_model_pick_issuance import _payload

ISSUED_FIXTURE = "api-football:9001"
LATE_FIXTURE = "api-football:9002"
STALE_FIXTURE = "api-football:9003"
KICKOFF = "2026-09-20T15:00:00+00:00"
LATE_KICKOFF = "2026-09-19T10:00:00+00:00"
ISSUED_AT = "2026-09-19T12:00:00+00:00"


class _NoCallMarketProvider(NotConfiguredMarketProvider):
    def get_current_markets(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the lifecycle must not request bookmaker markets")


def _services(tmp_path: Path) -> DashboardServices:
    config = replace(
        DashboardConfig.default(),
        repo_root=tmp_path,
        dashboard_db_path=tmp_path / "dash.sqlite",
        paper_trading_db_path=tmp_path / "paper.sqlite",
        model_pick_db_path=tmp_path / "picks.sqlite",
        performance_output_dir=tmp_path / "performance",
        operational_data_dir=tmp_path,
        storage_mode="test_tmpdir",
        persistent_data_dir_configured=False,
        enable_scheduler=False,
        allow_paid_provider_calls=False,
        current_odds_connected=False,
        enable_model_pick_issuance_scheduler=True,
        enable_model_pick_settlement_scheduler=True,
        runtime_role=RUNTIME_ROLE_SCHEDULER,
    )
    return DashboardServices(config=config, fixture_provider=None, market_provider=_NoCallMarketProvider(provider_label="test"))


def _fixture(fixture_id, *, kickoff, status="SCHEDULED", home=None, away=None):
    return NormalizedFixture(
        fixture_id=fixture_id, provider_fixture_id=fixture_id.partition(":")[2], provider="api-football",
        competition_id="ENG3", competition_name="League One", division_code="E2", season="2026",
        home_team_id="1", home_team_name="Mansfield", away_team_id="2", away_team_name="Stockport",
        kickoff_utc=kickoff, status=status, elapsed_minute=None, home_score=home, away_score=away,
        last_provider_update=kickoff, source_version="api_football_v1", provider_payload={},
    )


def _prediction(conn, prediction_id, fixture_id, *, generated_at="2026-09-18T08:00:00+00:00", status="READY", version_key=None):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(dashboard_predictions)")}
    values = {
        "prediction_id": prediction_id, "fixture_id": fixture_id, "status": status, "generated_at": generated_at,
        "model_as_of": "2026-09-17T23:59:59+00:00", "model_snapshot_id": "snap1", "version_key": version_key or f"{prediction_id}:v1",
        "payload_json": json.dumps(_payload()), "updated_at": generated_at, "created_at": generated_at,
    }
    values = {k: v for k, v in values.items() if k in columns}
    conn.execute(
        f"INSERT INTO dashboard_predictions ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
        tuple(values.values()),
    )


def _seed(tmp_path: Path) -> None:
    with storage.connect(tmp_path / "dash.sqlite") as conn:
        storage.initialize_database(conn)
        storage.upsert_fixtures(
            conn,
            [
                _fixture(ISSUED_FIXTURE, kickoff=KICKOFF),
                _fixture(LATE_FIXTURE, kickoff=LATE_KICKOFF),
                _fixture(STALE_FIXTURE, kickoff=KICKOFF),
            ],
            updated_at="2026-09-18T07:00:00+00:00",
            initialize_schema=False,
        )
        _prediction(conn, "p_ok", ISSUED_FIXTURE)
        _prediction(conn, "p_late", LATE_FIXTURE)
        _prediction(conn, "p_stale_ready", STALE_FIXTURE)
        _prediction(conn, "p_stale_blocked", STALE_FIXTURE, generated_at="2026-09-18T09:00:00+00:00", status="BLOCKED")
        conn.commit()


def _issue(tmp_path: Path, now: str) -> dict:
    dash = sqlite3.connect(f"file:{(tmp_path / 'dash.sqlite').as_posix()}?mode=ro", uri=True)
    dash.row_factory = sqlite3.Row
    led = ledger.connect(tmp_path / "picks.sqlite")
    try:
        return issuance.issue_forward_model_picks(dash, led, now=now)
    finally:
        led.close()
        dash.close()


def _ledger_rows(tmp_path: Path) -> list[dict]:
    led = ledger.connect(tmp_path / "picks.sqlite")
    try:
        return ledger.fetch_model_pick_rows(led, initialize_schema=False)
    finally:
        led.close()


def test_forward_lifecycle_issue_collect_settle_report(tmp_path):
    _seed(tmp_path)
    services = _services(tmp_path)

    # 1. Pre-kickoff issuance: only the fixture whose latest prediction is READY and
    #    whose kickoff is still ahead of the issuance instant.
    first = _issue(tmp_path, ISSUED_AT)
    assert first["written"] == 2
    assert first["kickoff_passed_not_issued"] == 1  # LATE_FIXTURE
    assert first["latest_not_ready_not_issued"] == 1  # STALE_FIXTURE fails closed
    rows = _ledger_rows(tmp_path)
    assert {row["fixture_id"] for row in rows} == {ISSUED_FIXTURE}
    assert all(row["created_at"] == ISSUED_AT < row["kickoff_utc"] for row in rows)

    # 2. A restart / second pass never duplicates.
    assert _issue(tmp_path, "2026-09-19T13:00:00+00:00")["written"] == 0
    assert len(_ledger_rows(tmp_path)) == 2

    # 3. The issued fixture enters the AH/corners market collection pool.
    assert services._live_issued_collection_fixture_ids() == {ISSUED_FIXTURE}

    # 4. Before a verified final result, settlement leaves the picks open.
    assert services.run_model_pick_settlement_cycle()["applied"] == 0

    # 5. Verified truth arrives: FT 2-0, 11 corners.
    with storage.connect(tmp_path / "dash.sqlite") as conn:
        storage.upsert_fixtures(
            conn, [_fixture(ISSUED_FIXTURE, kickoff=KICKOFF, status="FINISHED", home=2, away=0)],
            updated_at="2026-09-20T17:10:00+00:00", initialize_schema=False,
        )
        storage.upsert_fixture_result(
            conn, fixture_id=ISSUED_FIXTURE, statistics_provider="api-football", statistics_provider_fixture_id="9001",
            home_corners=6, away_corners=5, total_corners=11, source_timestamp=None,
            captured_at="2026-09-20T17:12:00+00:00", initialize_schema=False,
        )
    settled = services.run_model_pick_settlement_cycle()
    assert settled["applied"] == 2
    assert services.run_model_pick_settlement_cycle()["applied"] == 0  # idempotent
    states = {row["market"]: row["trade_state"] for row in _ledger_rows(tmp_path)}
    assert set(states) == {"asian_handicap", "corner_total"}
    assert all(state for state in states.values())

    # 6. Forward-only performance reflects exactly these two picks.
    payload = build_model_pick_history_payload(model_pick_db_path=tmp_path / "picks.sqlite", timezone_name="UTC")
    assert payload["summary"]["issued"] == 2
    assert payload["summary"]["settled"] == 2
    assert payload["excluded_from_headline"] == 0
    assert payload["by_issuance_timing"]["FORWARD"]["settled"] == 2

    # No paper-ledger positions or Value Bets come out of the Model Pick lifecycle.
    assert not (tmp_path / "paper.sqlite").exists()
