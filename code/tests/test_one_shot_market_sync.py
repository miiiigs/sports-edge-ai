"""Tests for the persistent one-shot market-sync checkpoint mechanism."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sports_ai.dashboard import storage as dashboard_storage
from sports_ai.dashboard.day12_rolling_xg import SOURCE_IDENTIFIER, build_xg_inspection
from sports_ai.dashboard.models import FIXTURE_STATUS_PRE_MATCH, NormalizedFixture
from sports_ai.dashboard.services import DashboardConfig, DashboardServices, _hash_one_shot_token
from sports_ai.market_data.providers import MarketProviderError, NormalizedMarketSnapshot
from sports_ai.storage import football_paper_trading


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _dashboard_config(tmp_path: Path) -> DashboardConfig:
    repo_root = _repo_root()
    performance_dir = tmp_path / "performance"
    performance_dir.mkdir(parents=True, exist_ok=True)
    (performance_dir / "football_performance_summary.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-08-20T04:18:32+00:00",
                "summary": {
                    "MODEL_VALIDATION": "PROMISING",
                    "PROFITABILITY_PROVEN": "NO",
                    "CLV_PROVEN": "NO",
                    "LIVE_EDGE_PROVEN": "NO",
                    "evaluation_sample": 9664,
                    "one_x_two_accuracy": 0.470509,
                },
                "status_flags": {"HISTORICAL_BENCHMARK_COMPLETE": "YES"},
            }
        ),
        encoding="utf-8",
    )
    (performance_dir / "football_performance_by_market.csv").write_text(
        "market,line,selection,sample_size,hit_rate,mean_predicted_probability,actual_frequency,brier,log_loss,avg_odds,break_even_rate,roi,status,notes\n"
        "1X2,,ALL,9664,0.470509,,,0.625145,1.039517,,,,READY,Test row\n",
        encoding="utf-8",
    )
    (performance_dir / "football_performance_by_competition.csv").write_text(
        "competition_id,fixtures,1x2_accuracy,1x2_brier,1x2_log_loss,goals_mae,corners_mae,candidate_count,candidate_roi,status\n"
        "ENG1,1520,0.520395,0.594798,0.99749,1.329659,2.733234,1910,,PRICE_CONTEXT_CLOSING_ONLY\n",
        encoding="utf-8",
    )
    (performance_dir / "football_performance_by_season.csv").write_text(
        "season_code,fixtures,1x2_accuracy,1x2_brier,1x2_log_loss,goals_mae,corners_mae,status\n"
        "2526,2416,0.472682,0.625951,1.041255,1.225309,2.684581,READY\n",
        encoding="utf-8",
    )
    (performance_dir / "football_calibration.csv").write_text(
        "scope_type,scope_value,market,line,selection,subset,bucket_label,bucket_lo,bucket_hi,predicted_mean_probability,observed_hit_rate,sample_size,calibration_gap\n"
        "overall,ALL,1x2,,HOME,ALL_PREDICTIONS,40-50%,0.4,0.5,0.45,0.47,100,-0.02\n",
        encoding="utf-8",
    )
    return DashboardConfig(
        repo_root=repo_root,
        research_db_path=repo_root / "research" / "football_research" / "store" / "football_research.sqlite",
        dashboard_db_path=tmp_path / "football_dashboard.sqlite",
        paper_trading_db_path=tmp_path / "football_paper_trading.sqlite",
        performance_output_dir=performance_dir,
        operational_data_dir=tmp_path,
        storage_mode="test_tmpdir",
        persistent_data_dir_configured=False,
        display_timezone="Asia/Manila",
        upcoming_horizon_days=3,
        scheduler_interval_seconds=9999,
        enable_scheduler=False,
        current_odds_connected=False,
        current_xg_connected=False,
        allow_paid_provider_calls=True,
    )


class FakeFixtureProvider:
    def __init__(self, fixtures: list[NormalizedFixture]) -> None:
        self.fixtures = fixtures

    def provider_name(self) -> str:
        return "fake-provider"

    def configured(self) -> bool:
        return True

    def reset_sync_diagnostics(self) -> None:
        return None

    def sync_diagnostics(self) -> dict:
        return {"provider": "fake-provider", "query_count": 0, "has_errors": False, "queries": []}

    def get_live_fixtures(self) -> list[NormalizedFixture]:
        return []

    def get_fixtures_for_date(self, target_date) -> list[NormalizedFixture]:
        return [f for f in self.fixtures if f.kickoff_utc[:10] == target_date.isoformat()]

    def get_upcoming_fixtures(self, *, from_utc: datetime, to_utc: datetime) -> list[NormalizedFixture]:
        return [
            f
            for f in self.fixtures
            if from_utc <= datetime.fromisoformat(f.kickoff_utc) <= to_utc
        ]

    def get_fixture(self, fixture_id: str) -> NormalizedFixture | None:
        for fixture in self.fixtures:
            if fixture.fixture_id == fixture_id:
                return fixture
        return None


class CountingMarketProvider:
    def __init__(self, *, configured: bool = True) -> None:
        self.calls = 0
        self.request_count = 0
        self.snapshots_by_fixture: dict[str, list[NormalizedMarketSnapshot]] = {}
        self._configured = configured
        self.raise_on_snapshot = False
        self.raise_on_diagnostics = False
        self.diagnostics_state: str | None = None

    def provider_name(self) -> str:
        return "counting-market-provider"

    def configured(self) -> bool:
        return self._configured

    def _snapshots(self, fixture_id: str) -> list[NormalizedMarketSnapshot]:
        self.calls += 1
        self.request_count += 1
        if self.raise_on_snapshot:
            raise MarketProviderError("provider exploded")
        return list(self.snapshots_by_fixture.get(fixture_id, []))

    def get_market_snapshot(self, fixture_id: str, *, fixture_context: dict | None = None) -> list[NormalizedMarketSnapshot]:
        return self._snapshots(fixture_id)

    def get_fixture_markets(self, fixture_id: str, *, fixture_context: dict | None = None) -> list[NormalizedMarketSnapshot]:
        return self._snapshots(fixture_id)

    def get_best_prices(self, fixture_id: str, *, fixture_context: dict | None = None) -> list[NormalizedMarketSnapshot]:
        return self._snapshots(fixture_id)

    def diagnostics(self) -> dict:
        if self.raise_on_diagnostics:
            raise MarketProviderError("diagnostics exploded")
        state = self.diagnostics_state or ("CONNECTED" if self._configured else "NOT_CONFIGURED")
        return {
            "provider": self.provider_name(),
            "configured": self._configured,
            "state": state,
            "current_market_decisions": "READY" if self._configured else "BLOCKED",
            "reason": None,
            "request_count": self.request_count,
        }


def _fixture(*, fixture_id: str = "fake:arsenal_chelsea") -> NormalizedFixture:
    kickoff_utc = (
        datetime.now(timezone.utc).replace(hour=20, minute=0, second=0, microsecond=0) + timedelta(days=2)
    ).isoformat()
    last_provider_update = (datetime.now(timezone.utc) - timedelta(hours=4)).replace(microsecond=0).isoformat()
    return NormalizedFixture(
        fixture_id=fixture_id,
        provider_fixture_id="fixture-1",
        provider="fake-provider",
        competition_id="ENG1",
        competition_name="Premier League",
        division_code="E0",
        season="2026",
        home_team_id="100",
        home_team_name="Arsenal",
        away_team_id="200",
        away_team_name="Chelsea",
        kickoff_utc=kickoff_utc,
        status=FIXTURE_STATUS_PRE_MATCH,
        elapsed_minute=None,
        home_score=None,
        away_score=None,
        last_provider_update=last_provider_update,
        source_version="fake_fixture_provider_v1",
        provider_payload={"fixture_id": fixture_id},
    )


def _market_snapshot(*, fixture: NormalizedFixture, bookmaker: str, market: str, selection: str, decimal_odds: float, reference_designation: str) -> NormalizedMarketSnapshot:
    return NormalizedMarketSnapshot(
        provider="counting-market-provider",
        fixture_id=fixture.fixture_id,
        provider_fixture_id=fixture.provider_fixture_id,
        captured_at="2026-08-20T10:00:00+00:00",
        source_timestamp=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds"),
        market=market,
        selection=selection,
        line=None,
        decimal_odds=decimal_odds,
        bookmaker=bookmaker,
        ingestion_version="test_market_ingestion_v1",
        provider_market_id=f"{bookmaker}:{market}:{selection}",
        home_team_name=fixture.home_team_name,
        away_team_name=fixture.away_team_name,
        competition_id=fixture.competition_id,
        competition_name=fixture.competition_name,
        kickoff_utc=fixture.kickoff_utc,
        freshness_state="FRESH",
        reference_designation=reference_designation,
        reference_rank=1 if reference_designation == "reference" else None,
    )


# ---------------------------------------------------------------------------
# Storage-level claim semantics
# ---------------------------------------------------------------------------


def test_claim_atomic_single_winner(tmp_path: Path):
    conn = dashboard_storage.connect(tmp_path / "dashboard.sqlite")
    token_hash = "abc123"
    assert dashboard_storage.claim_one_shot_market_sync_token(
        conn, token_hash=token_hash, token_id="abc", claimed_at="t0"
    ) is True
    assert dashboard_storage.claim_one_shot_market_sync_token(
        conn, token_hash=token_hash, token_id="abc", claimed_at="t1"
    ) is False


def test_claim_persists_across_connections(tmp_path: Path):
    db_path = tmp_path / "dashboard.sqlite"
    conn1 = dashboard_storage.connect(db_path)
    assert dashboard_storage.claim_one_shot_market_sync_token(
        conn1, token_hash="hash-1", token_id="id-1", claimed_at="t0"
    ) is True
    conn1.close()

    # Simulate a process restart / redeploy: a fresh connection to the same file.
    conn2 = dashboard_storage.connect(db_path)
    assert dashboard_storage.claim_one_shot_market_sync_token(
        conn2, token_hash="hash-1", token_id="id-1", claimed_at="t1"
    ) is False


def test_claim_update_and_get_roundtrip(tmp_path: Path):
    conn = dashboard_storage.connect(tmp_path / "dashboard.sqlite")
    token_hash = "hash-2"
    assert dashboard_storage.claim_one_shot_market_sync_token(
        conn, token_hash=token_hash, token_id="id-2", claimed_at="t0"
    ) is True
    row = dashboard_storage.get_one_shot_market_sync_claim(conn, token_hash)
    assert row is not None
    assert row["state"] == "CLAIMED"
    assert row["token_id"] == "id-2"

    dashboard_storage.update_one_shot_market_sync_claim(
        conn,
        token_hash=token_hash,
        state="SUCCESS",
        completed_at="t1",
        provider_requests=1,
        snapshots=3,
        decisions=2,
        paper_entries=1,
        error=None,
    )
    row = dashboard_storage.get_one_shot_market_sync_claim(conn, token_hash)
    assert row["state"] == "SUCCESS"
    assert row["completed_at"] == "t1"
    assert row["provider_requests"] == 1
    assert row["snapshots"] == 3


def test_raw_token_not_persisted(tmp_path: Path):
    conn = dashboard_storage.connect(tmp_path / "dashboard.sqlite")
    raw_token = "super-secret-operator-token-value"
    token_hash = "hash-of-token"
    dashboard_storage.claim_one_shot_market_sync_token(
        conn, token_hash=token_hash, token_id="id-3", claimed_at="t0"
    )
    raw_dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM one_shot_market_sync_claims")])
    assert raw_token not in raw_dump


# ---------------------------------------------------------------------------
# One-shot orchestration
# ---------------------------------------------------------------------------


def _services(tmp_path: Path, *, token: str | None, provider: CountingMarketProvider, xg: bool = False):
    config = replace(_dashboard_config(tmp_path), odds_sync_once_token=token or "")
    if xg:
        config = replace(config, current_xg_connected=True)
    return DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([_fixture()]),
        market_provider=provider,
    )


def test_no_token_zero_provider_calls(tmp_path: Path):
    provider = CountingMarketProvider()
    services = _services(tmp_path, token=None, provider=provider)
    asyncio.run(services.initialize())
    assert provider.calls == 0
    asyncio.run(services.sync_once())
    assert provider.calls == 0
    asyncio.run(services.shutdown())


def test_new_token_exactly_one_provider_call(tmp_path: Path):
    provider = CountingMarketProvider()
    services = _services(tmp_path, token="checkpoint-1", provider=provider)
    asyncio.run(services.initialize())
    assert provider.calls == 1
    assert services.get_system_status()["one_shot_sync"]["state"] == "SUCCESS"

    # Same token on subsequent scheduler cycles -> no additional calls.
    asyncio.run(services.sync_once())
    asyncio.run(services.sync_once())
    assert provider.calls == 1
    assert services.get_system_status()["one_shot_sync"]["state"] == "ALREADY_CONSUMED"
    asyncio.run(services.shutdown())


def test_same_token_after_restart_zero_calls(tmp_path: Path):
    provider1 = CountingMarketProvider()
    services1 = _services(tmp_path, token="checkpoint-1", provider=provider1)
    asyncio.run(services1.initialize())
    assert provider1.calls == 1
    asyncio.run(services1.shutdown())

    # Simulate redeploy: a brand-new service instance on the same DB paths.
    provider2 = CountingMarketProvider()
    services2 = _services(tmp_path, token="checkpoint-1", provider=provider2)
    asyncio.run(services2.initialize())
    assert provider2.calls == 0
    status = services2.get_system_status()
    assert status["one_shot_sync"]["state"] == "ALREADY_CONSUMED"
    asyncio.run(services2.shutdown())


def test_new_second_token_one_additional_call(tmp_path: Path):
    provider = CountingMarketProvider()
    services = _services(tmp_path, token="checkpoint-1", provider=provider)
    asyncio.run(services.initialize())
    assert provider.calls == 1

    services.config = replace(services.config, odds_sync_once_token="checkpoint-2")
    asyncio.run(services.sync_once())
    assert provider.calls == 2
    asyncio.run(services.shutdown())


def test_empty_token_zero_calls(tmp_path: Path):
    provider = CountingMarketProvider()
    services = _services(tmp_path, token="   ", provider=provider)
    asyncio.run(services.initialize())
    assert provider.calls == 0
    asyncio.run(services.shutdown())


def test_oversized_token_blocked(tmp_path: Path):
    provider = CountingMarketProvider()
    services = _services(tmp_path, token="x" * 300, provider=provider)
    asyncio.run(services.initialize())
    assert provider.calls == 0
    assert services.get_system_status()["one_shot_sync"]["state"] == "BLOCKED"
    asyncio.run(services.shutdown())


def test_automatic_sync_enabled_blocks_one_shot(tmp_path: Path):
    provider = CountingMarketProvider()
    config = replace(_dashboard_config(tmp_path), enable_odds_sync=True, odds_sync_once_token="checkpoint-1")
    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([_fixture()]),
        market_provider=provider,
    )
    asyncio.run(services.initialize())
    # Automatic sync owns provider synchronization; the one-shot must be blocked.
    assert services.get_system_status()["one_shot_sync"]["state"] == "BLOCKED"
    asyncio.run(services.shutdown())


def test_provider_failure_consumes_token_without_retry(tmp_path: Path):
    provider = CountingMarketProvider()
    provider.raise_on_snapshot = True
    services = _services(tmp_path, token="checkpoint-1", provider=provider)
    asyncio.run(services.initialize())
    # The provider raised, but the token was claimed before execution.
    assert provider.calls == 1

    # Same token must never retry.
    asyncio.run(services.sync_once())
    assert provider.calls == 1
    asyncio.run(services.shutdown())


def test_diagnostics_failure_records_failed_state(tmp_path: Path):
    provider = CountingMarketProvider()
    provider.raise_on_diagnostics = True
    services = _services(tmp_path, token="checkpoint-1", provider=provider)
    asyncio.run(services.initialize())

    # The token was claimed and the sync failed; the failure is recorded.
    assert services._one_shot_sync_state["state"] == "FAILED"

    # The token is consumed; a second sync must never retry it.
    provider.raise_on_diagnostics = False
    asyncio.run(services.sync_once())
    assert provider.calls == 0
    asyncio.run(services.shutdown())


def test_status_exposes_token_id_not_raw_token(tmp_path: Path):
    raw_token = "operator-secret-checkpoint-token"
    provider = CountingMarketProvider()
    services = _services(tmp_path, token=raw_token, provider=provider)
    asyncio.run(services.initialize())
    status_payload = json.dumps(services.get_system_status())
    assert raw_token not in status_payload
    assert services.get_system_status()["one_shot_sync"]["token_id"] is not None
    asyncio.run(services.shutdown())


def test_one_shot_runs_normal_pipeline_and_creates_paper_entry(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    fixture = _fixture()
    provider = CountingMarketProvider()

    def xg_factory(row, prediction, snapshots):
        expected_home = float(prediction["expected_home_goals"])
        expected_away = float(prediction["expected_away_goals"])

        def _row(role, recent):
            return {
                "fixture_id": fixture.fixture_id,
                "team_role": role,
                "recent_xg_for_per_match": recent,
                "matches_in_window": 5,
                "as_of": "2026-05-24T20:00:00+00:00",
                "window_start_at": "2026-04-01T20:00:00+00:00",
                "window_end_at": "2026-05-24T20:00:00+00:00",
                "source": SOURCE_IDENTIFIER,
                "competition_id": "ENG1",
            }

        rows = {
            (fixture.fixture_id, "HOME"): _row("HOME", expected_home),
            (fixture.fixture_id, "AWAY"): _row("AWAY", expected_away),
        }
        inspection = build_xg_inspection(rows)
        return lambda: (rows, inspection)

    config = replace(_dashboard_config(tmp_path), current_xg_connected=True, odds_sync_once_token="checkpoint-1")
    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=provider,
        xg_profile_factory=xg_factory,
    )

    provider.snapshots_by_fixture[fixture.fixture_id] = [
        _market_snapshot(fixture=fixture, bookmaker="Pinnacle", market="1x2", selection="HOME", decimal_odds=2.05, reference_designation="reference"),
        _market_snapshot(fixture=fixture, bookmaker="Pinnacle", market="1x2", selection="DRAW", decimal_odds=3.35, reference_designation="reference"),
        _market_snapshot(fixture=fixture, bookmaker="Pinnacle", market="1x2", selection="AWAY", decimal_odds=3.95, reference_designation="reference"),
        _market_snapshot(fixture=fixture, bookmaker="bet365", market="1x2", selection="HOME", decimal_odds=10.0, reference_designation="available"),
    ]

    asyncio.run(services.initialize())
    assert provider.calls == 1

    with football_paper_trading.connect(config.paper_trading_db_path) as conn:
        paper_rows = football_paper_trading.fetch_paper_trade_rows(conn)
    assert len(paper_rows) == 1

    status = services.get_system_status()
    assert status["one_shot_sync"]["state"] == "SUCCESS"
    assert status["one_shot_sync"]["provider_requests"] == 1
    asyncio.run(services.shutdown())


# ---------------------------------------------------------------------------
# Provider outcome classification (MEDIUM 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider_state", "expected_state", "expected_error"),
    [
        ("AUTH_ERROR", "FAILED", "AUTH_ERROR"),
        ("RATE_LIMITED", "FAILED", "RATE_LIMITED"),
        ("DEGRADED", "FAILED", "PROVIDER_ERROR"),
        ("QUOTA_GUARD", "BLOCKED", "QUOTA_GUARD"),
        ("QUOTA_EXHAUSTED", "BLOCKED", "QUOTA_EXHAUSTED"),
    ],
)
def test_provider_failure_state_not_success(tmp_path: Path, provider_state, expected_state, expected_error):
    provider = CountingMarketProvider()
    provider.diagnostics_state = provider_state
    services = _services(tmp_path, token="checkpoint-1", provider=provider)
    asyncio.run(services.initialize())
    status = services.get_system_status()
    assert status["one_shot_sync"]["state"] == expected_state
    assert status["one_shot_sync"]["error"] == expected_error
    asyncio.run(services.shutdown())


def test_zero_snapshots_with_valid_provider_response_is_success(tmp_path: Path):
    # A legitimate provider response that returns no eligible market rows must
    # remain SUCCESS, not be misreported as a provider failure.
    provider = CountingMarketProvider()
    provider.diagnostics_state = "CONNECTED"
    provider.snapshots_by_fixture = {}
    services = _services(tmp_path, token="checkpoint-1", provider=provider)
    asyncio.run(services.initialize())
    status = services.get_system_status()
    assert status["one_shot_sync"]["state"] == "SUCCESS"
    assert status["one_shot_sync"]["error"] is None
    assert status["one_shot_sync"]["snapshots"] == 0
    asyncio.run(services.shutdown())


def test_no_current_events_is_success_not_failure(tmp_path: Path):
    provider = CountingMarketProvider()
    provider.diagnostics_state = "NO_CURRENT_EVENTS"
    services = _services(tmp_path, token="checkpoint-1", provider=provider)
    asyncio.run(services.initialize())
    status = services.get_system_status()
    assert status["one_shot_sync"]["state"] == "SUCCESS"
    asyncio.run(services.shutdown())


# ---------------------------------------------------------------------------
# Token removal status reset (MEDIUM 2)
# ---------------------------------------------------------------------------


def test_token_removed_resets_status_and_preserves_history(tmp_path: Path):
    provider = CountingMarketProvider()
    services = _services(tmp_path, token="checkpoint-1", provider=provider)
    asyncio.run(services.initialize())
    assert services.get_system_status()["one_shot_sync"]["state"] == "SUCCESS"

    # Operator removes the token (config change) and the service runs again.
    services.config = replace(services.config, odds_sync_once_token="")
    asyncio.run(services.sync_once())
    status = services.get_system_status()
    assert status["one_shot_sync"]["configured"] is False
    assert status["one_shot_sync"]["state"] == "NOT_CONFIGURED"

    # Historical claim row must remain persisted.
    token_hash = _hash_one_shot_token("checkpoint-1")
    with dashboard_storage.connect(services.config.dashboard_db_path) as conn:
        claim = dashboard_storage.get_one_shot_market_sync_claim(conn, token_hash)
    assert claim is not None
    assert claim["state"] == "SUCCESS"
    asyncio.run(services.shutdown())


# ---------------------------------------------------------------------------
# Real SQLite concurrency (FOLLOW-UP 3)
# ---------------------------------------------------------------------------


def test_concurrent_claim_single_winner(tmp_path: Path):
    from concurrent.futures import ThreadPoolExecutor

    db_path = tmp_path / "dashboard.sqlite"
    token_hash = "concurrent-token-hash"
    results: list[bool] = []

    def _claim(i: int) -> bool:
        conn = dashboard_storage.connect(db_path)
        try:
            return dashboard_storage.claim_one_shot_market_sync_token(
                conn, token_hash=token_hash, token_id="id-c", claimed_at=f"t{i}"
            )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(_claim, range(8)))

    assert sum(results) == 1
    assert results.count(False) == 7
