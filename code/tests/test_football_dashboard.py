from __future__ import annotations

import asyncio
import json
import sqlite3
import math
import threading
import time as time_module
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from sports_ai.dashboard.app import create_app
from sports_ai.dashboard import storage as dashboard_storage
from sports_ai.dashboard.day12_rolling_xg import SOURCE_IDENTIFIER, build_xg_inspection
from sports_ai.dashboard.fixture_provider import ApiFootballFixtureProvider, FixtureProviderError, FootballDataOrgFixtureProvider
from sports_ai.dashboard.fixture_provider import API_FOOTBALL_PROVIDER, AuthoritativeFixtureStatistics
from sports_ai.dashboard.portfolio_selection import (
    PORTFOLIO_POLICY_ID_V1,
    PORTFOLIO_POLICY_VERSION_V1,
    PORTFOLIO_STATE_NOT_SELECTED_FIXTURE_LIMIT,
    PORTFOLIO_STATE_PENDING,
    PORTFOLIO_STATE_SELECTED,
    PortfolioDisposition,
)
from sports_ai.dashboard.models import (
    FIXTURE_STATUS_FINISHED,
    FIXTURE_STATUS_LIVE,
    FIXTURE_STATUS_PRE_MATCH,
    NormalizedFixture,
    normalize_text_key,
)
from sports_ai.dashboard.services import DashboardConfig, DashboardServices, _default_market_provider, _humanize_market_selection
from sports_ai.market_data.providers import NormalizedMarketSnapshot
from sports_ai.storage import football_canonical_fixture, football_edge_desk, football_paper_trading, football_portfolio_selection


class FakeFixtureProvider:
    def __init__(
        self,
        fixtures: list[NormalizedFixture] | None = None,
        *,
        configured: bool = True,
        diagnostics: dict | None = None,
    ):
        self.fixtures = fixtures or []
        self._configured = configured
        self._diagnostics = diagnostics or {
            "provider": "fake-provider",
            "query_count": 0,
            "league_ids_queried": [],
            "provider_seasons_queried": [],
            "has_errors": False,
            "last_error": None,
            "queries": [],
        }

    def provider_name(self) -> str:
        return "fake-provider"

    def configured(self) -> bool:
        return self._configured

    def reset_sync_diagnostics(self) -> None:
        return None

    def sync_diagnostics(self) -> dict:
        return dict(self._diagnostics)

    def get_live_fixtures(self) -> list[NormalizedFixture]:
        return [fixture for fixture in self.fixtures if fixture.status == FIXTURE_STATUS_LIVE]

    def get_fixtures_for_date(self, target_date: date) -> list[NormalizedFixture]:
        timezone_name = ZoneInfo("Asia/Manila")
        return [
            fixture
            for fixture in self.fixtures
            if datetime.fromisoformat(fixture.kickoff_utc).astimezone(timezone_name).date() == target_date
        ]

    def get_upcoming_fixtures(
        self,
        *,
        from_utc: datetime,
        to_utc: datetime,
    ) -> list[NormalizedFixture]:
        return [
            fixture
            for fixture in self.fixtures
            if from_utc <= datetime.fromisoformat(fixture.kickoff_utc) <= to_utc
        ]

    def get_fixture(self, fixture_id: str) -> NormalizedFixture | None:
        for fixture in self.fixtures:
            if fixture.fixture_id == fixture_id:
                return fixture
        return None


class FakeMarketProvider:
    def __init__(
        self,
        snapshots_by_fixture: dict[str, list[NormalizedMarketSnapshot]] | None = None,
        *,
        configured: bool = True,
    ) -> None:
        self.snapshots_by_fixture = snapshots_by_fixture or {}
        self._configured = configured

    def provider_name(self) -> str:
        return "fake-market-provider"

    def configured(self) -> bool:
        return self._configured

    def get_fixture_markets(
        self,
        fixture_id: str,
        *,
        fixture_context: dict | None = None,
    ) -> list[NormalizedMarketSnapshot]:
        return list(self.snapshots_by_fixture.get(fixture_id, []))

    def get_best_prices(
        self,
        fixture_id: str,
        *,
        fixture_context: dict | None = None,
    ) -> list[NormalizedMarketSnapshot]:
        return list(self.snapshots_by_fixture.get(fixture_id, []))

    def get_market_snapshot(
        self,
        fixture_id: str,
        *,
        fixture_context: dict | None = None,
    ) -> list[NormalizedMarketSnapshot]:
        return list(self.snapshots_by_fixture.get(fixture_id, []))

    def diagnostics(self) -> dict:
        return {
            "provider": self.provider_name(),
            "configured": self._configured,
            "state": "CONNECTED" if self._configured else "NOT_CONFIGURED",
            "current_market_decisions": "READY" if self._configured else "BLOCKED",
            "captured_at": "2026-08-20T00:00:00+00:00",
            "reason": None if self._configured else "Fake market provider disabled for this test.",
        }


class FakeStatisticsProvider:
    def __init__(
        self,
        results_by_fixture: dict[str, AuthoritativeFixtureStatistics] | None = None,
        *,
        configured: bool = True,
        errors_by_fixture: dict[str, Exception] | None = None,
    ) -> None:
        self.results_by_fixture = results_by_fixture or {}
        self.errors_by_fixture = errors_by_fixture or {}
        self._configured = configured
        self.calls: list[str] = []

    def provider_name(self) -> str:
        return API_FOOTBALL_PROVIDER

    def configured(self) -> bool:
        return self._configured

    def get_authoritative_corner_statistics(self, fixture_context: dict[str, object]) -> AuthoritativeFixtureStatistics:
        fixture_id = str(fixture_context["fixture_id"])
        self.calls.append(fixture_id)
        if fixture_id in self.errors_by_fixture:
            raise self.errors_by_fixture[fixture_id]
        return self.results_by_fixture[fixture_id]

    def sync_diagnostics(self) -> dict[str, object]:
        return {
            "provider": self.provider_name(),
            "query_count": len(self.calls),
            "has_errors": bool(self.errors_by_fixture),
            "last_error": None if not self.errors_by_fixture else str(next(iter(self.errors_by_fixture.values()))),
            "queries": [{"fixture_id": fixture_id} for fixture_id in self.calls],
        }


class FlakyFixtureProvider(FakeFixtureProvider):
    def __init__(self, fixtures: list[NormalizedFixture]) -> None:
        super().__init__(fixtures)
        self.fail_next = True

    def get_live_fixtures(self) -> list[NormalizedFixture]:
        if self.fail_next:
            self.fail_next = False
            raise FixtureProviderError("transient provider failure")
        return super().get_live_fixtures()


class PostEvaluationFailureDashboardServices(DashboardServices):
    def _process_paper_trading_updates(self, rows_with_predictions: list[dict[str, object]]) -> None:
        raise RuntimeError("synthetic settlement crash")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


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


def _count_sql_with_prefixes(statements: list[str], *prefixes: str) -> int:
    return sum(any(statement.startswith(prefix) for prefix in prefixes) for statement in statements)


def _count_sql_containing(statements: list[str], fragment: str) -> int:
    return sum(fragment in statement for statement in statements)


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
                    "one_x_two_brier": 0.625145,
                    "one_x_two_log_loss": 1.039517,
                },
                "status_flags": {
                    "HISTORICAL_BENCHMARK_COMPLETE": "YES",
                },
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
        startup_sync_timeout_seconds=15,
    )


def _fixture(
    *,
    fixture_id: str = "fake:arsenal_chelsea",
    home_team_name: str = "Arsenal",
    away_team_name: str = "Chelsea",
    status: str = FIXTURE_STATUS_PRE_MATCH,
    home_score: int | None = None,
    away_score: int | None = None,
) -> NormalizedFixture:
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
        home_team_name=home_team_name,
        away_team_id="200",
        away_team_name=away_team_name,
        kickoff_utc=kickoff_utc,
        status=status,
        elapsed_minute=63 if status == FIXTURE_STATUS_LIVE else None,
        home_score=home_score,
        away_score=away_score,
        last_provider_update=last_provider_update,
        source_version="fake_fixture_provider_v1",
        provider_payload={"fixture_id": fixture_id},
    )


def _kickoff_utc_for_local_date(local_date: date, *, hour: int, minute: int = 0, timezone_name: str = "Asia/Manila") -> str:
    local_dt = datetime.combine(local_date, time(hour=hour, minute=minute), tzinfo=ZoneInfo(timezone_name))
    return local_dt.astimezone(timezone.utc).isoformat()


def _market_snapshot(
    *,
    fixture: NormalizedFixture,
    bookmaker: str,
    market: str,
    selection: str,
    decimal_odds: float,
    reference_designation: str,
    line: float | None = None,
    provider_market_id: str | None = None,
) -> NormalizedMarketSnapshot:
    return NormalizedMarketSnapshot(
        provider="fake-market-provider",
        fixture_id=fixture.fixture_id,
        provider_fixture_id=fixture.provider_fixture_id,
        captured_at="2026-08-20T10:00:00+00:00",
        source_timestamp=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds"),
        market=market,
        selection=selection,
        line=line,
        decimal_odds=decimal_odds,
        bookmaker=bookmaker,
        ingestion_version="test_market_ingestion_v1",
        provider_market_id=provider_market_id,
        home_team_name=fixture.home_team_name,
        away_team_name=fixture.away_team_name,
        competition_id=fixture.competition_id,
        competition_name=fixture.competition_name,
        kickoff_utc=fixture.kickoff_utc,
        freshness_state="FRESH",
        reference_designation=reference_designation,
        reference_rank=1 if reference_designation == "reference" else None,
    )


def _xg_factory_for_fixtures(*fixtures: NormalizedFixture):
    fixture_map = {fixture.fixture_id: fixture for fixture in fixtures}

    def factory(row, prediction, snapshots):
        fixture = fixture_map[str(row["fixture_id"])]
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
                "competition_id": str(row["competition_id"]),
            }

        rows = {
            (fixture.fixture_id, "HOME"): _row("HOME", expected_home),
            (fixture.fixture_id, "AWAY"): _row("AWAY", expected_away),
        }
        inspection = build_xg_inspection(rows)
        return lambda: (rows, inspection)

    return factory


def _build_paginated_dashboard_services(
    tmp_path: Path,
    monkeypatch,
    *,
    fixture_count: int,
    target_date: date = date(2026, 8, 30),
) -> tuple[DashboardServices, list[dict[str, object]], dict[str, dict[str, object]]]:
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([]),
        market_provider=FakeMarketProvider(configured=False),
    )
    today_fixture_rows: list[dict[str, object]] = []
    all_fixture_rows: list[dict[str, object]] = []
    market_evaluations: dict[str, dict[str, object]] = {}
    for index in range(fixture_count):
        fixture_number = index + 1
        fixture = replace(
            _fixture(
                fixture_id=f"fake:today_{fixture_number:02d}",
                home_team_name=f"Today {fixture_number:02d}",
                away_team_name=f"Opponent {fixture_number:02d}",
            ),
            kickoff_utc=_kickoff_utc_for_local_date(target_date, hour=10 + (index % 11), minute=index % 2),
        )
        fixture_row = fixture.to_dict(timezone_name="UTC")
        today_fixture_rows.append(fixture_row)
        all_fixture_rows.append(fixture_row)
        market_evaluations[fixture.fixture_id] = {
            "state": "READY",
            "reason": None,
            "decision_rows": [
                {
                    "fixture_id": fixture.fixture_id,
                    "competition_id": "ENG1",
                    "competition_name": "Premier League",
                    "decision": "PRELIMINARY_CANDIDATE",
                    "current_decision_state": "BET",
                    "market_family": "1x2",
                    "selection": "HOME",
                    "line": None,
                    "model_probability": 0.60 - (index * 0.005),
                    "model_fair_decimal_price": 1.60 + (index * 0.01),
                    "best_available_price": 2.20 + (index * 0.01),
                    "best_available_bookmaker": "Pinnacle",
                    "best_available_source_timestamp": "2026-08-30T09:00:00Z",
                    "settlement_aware_edge": 0.40 - (index * 0.01),
                    "paper_entry_eligible": True,
                    "paper_trade_id": None,
                    "market_freshness": "FRESH",
                    "day12_state": "PASSED",
                    "reason_codes": [],
                },
                {
                    "fixture_id": fixture.fixture_id,
                    "competition_id": "ENG1",
                    "competition_name": "Premier League",
                    "decision": "NO_BET",
                    "current_decision_state": "NO_BET_VALUE",
                    "market_family": "btts",
                    "selection": "YES",
                    "line": None,
                    "model_probability": 0.52,
                    "model_fair_decimal_price": 1.90,
                    "best_available_price": 1.70,
                    "best_available_bookmaker": "bet365",
                    "best_available_source_timestamp": "2026-08-30T09:00:00Z",
                    "settlement_aware_edge": -0.07,
                    "paper_entry_eligible": False,
                    "paper_trade_id": None,
                    "market_freshness": "FRESH",
                    "day12_state": "PASSED",
                    "reason_codes": ["PRICE_NO_VALUE"],
                },
                {
                    "fixture_id": fixture.fixture_id,
                    "competition_id": "ENG1",
                    "competition_name": "Premier League",
                    "decision": "STALE_MARKET",
                    "current_decision_state": "NO_BET_STALE_MARKET",
                    "market_family": "goals_total",
                    "selection": "OVER",
                    "line": 2.5,
                    "model_probability": 0.49,
                    "model_fair_decimal_price": 2.02,
                    "best_available_price": 1.88,
                    "best_available_bookmaker": "matchbook",
                    "best_available_source_timestamp": "2026-08-30T06:00:00Z",
                    "settlement_aware_edge": -0.03,
                    "paper_entry_eligible": False,
                    "paper_trade_id": None,
                    "market_freshness": "STALE",
                    "day12_state": "PASSED",
                    "reason_codes": ["MARKET_STALE"],
                },
            ],
            "display_rows": [],
        }
        market_evaluations[fixture.fixture_id]["display_rows"] = list(market_evaluations[fixture.fixture_id]["decision_rows"])

    tomorrow_fixture = replace(
        _fixture(
            fixture_id="fake:tomorrow_fixture",
            home_team_name="Tomorrow FC",
            away_team_name="Next Opponent",
        ),
        kickoff_utc=_kickoff_utc_for_local_date(target_date + timedelta(days=1), hour=15),
    )
    tomorrow_row = tomorrow_fixture.to_dict(timezone_name="UTC")
    all_fixture_rows.append(tomorrow_row)

    monkeypatch.setattr(services, "_current_local_date", lambda: target_date)
    monkeypatch.setattr(services, "_all_fixtures_with_predictions", lambda: all_fixture_rows)
    monkeypatch.setattr(
        services,
        "_future_fixture_rows",
        lambda rows: sorted(today_fixture_rows + [tomorrow_row], key=lambda row: (str(row["kickoff_utc"]), str(row["fixture_id"]))),
    )
    monkeypatch.setattr(
        services,
        "_filter_fixture_rows",
        lambda *, bucket, fixture_rows: (
            today_fixture_rows
            if bucket == "today"
            else [tomorrow_row]
            if bucket == "upcoming"
            else []
        ),
    )
    monkeypatch.setattr(services, "_market_evaluation_map", lambda: market_evaluations)
    monkeypatch.setattr(
        services,
        "_paper_positions",
        lambda: ([], football_paper_trading.paper_trading_status([])),
    )
    return services, today_fixture_rows, market_evaluations


def _record_test_paper_trade(
    conn,
    *,
    fixture: NormalizedFixture,
    prediction_id: str,
    market: str,
    selection: str,
    entry_odds: float,
    model_fair_price: float,
    line: float | None = None,
) -> str:
    return football_paper_trading.record_paper_trade_entry(
        conn,
        football_paper_trading.PaperTradeEntry(
            prediction_id=prediction_id,
            fixture_id=fixture.fixture_id,
            model_snapshot_id=f"snapshot_{prediction_id}",
            model_version="football_day11_manager_core_v1",
            market=market,
            selection=selection,
            line=line,
            model_probability=0.55,
            model_fair_price=model_fair_price,
            available_market_price=entry_odds,
            edge=0.06,
            minimum_acceptable_price=1.8,
            decision="PAPER_CANDIDATE",
            decision_timestamp=(datetime.now(timezone.utc) - timedelta(hours=4)).replace(microsecond=0).isoformat(),
            kickoff=fixture.kickoff_utc,
            safeguard_state="READY",
            entry_eligibility="ELIGIBLE",
            bookmaker="Pinnacle",
            provider="fake-market-provider",
            entry_odds=entry_odds,
            entry_captured_at=(datetime.now(timezone.utc) - timedelta(hours=4)).replace(microsecond=0).isoformat(),
        ),
    )


def _build_today_read_path_services(tmp_path: Path) -> tuple[DashboardServices, list[NormalizedFixture]]:
    target_date = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Manila")).date()
    today_primary = replace(
        _fixture(
            fixture_id="fake:today_primary",
            home_team_name="Primary FC",
            away_team_name="Visitors FC",
        ),
        kickoff_utc=_kickoff_utc_for_local_date(target_date, hour=20),
    )
    today_secondary = replace(
        _fixture(
            fixture_id="fake:today_secondary",
            home_team_name="Secondary FC",
            away_team_name="Rivals FC",
        ),
        kickoff_utc=_kickoff_utc_for_local_date(target_date, hour=22),
    )
    tomorrow_fixture = replace(
        _fixture(
            fixture_id="fake:tomorrow_future",
            home_team_name="Tomorrow FC",
            away_team_name="Future Opponent",
        ),
        kickoff_utc=_kickoff_utc_for_local_date(target_date + timedelta(days=1), hour=18),
    )
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([today_primary, today_secondary, tomorrow_fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )
    services._market_evaluation_map = lambda: {
        today_primary.fixture_id: {
            "state": "READY",
            "reason": None,
            "decision_rows": [
                {
                    "fixture_id": today_primary.fixture_id,
                    "competition_id": "ENG1",
                    "competition_name": "Premier League",
                    "decision": "PRELIMINARY_CANDIDATE",
                    "current_decision_state": "BET",
                    "market_family": "1x2",
                    "selection": "HOME",
                    "line": None,
                    "model_probability": 0.61,
                    "model_fair_decimal_price": 1.64,
                    "best_available_price": 2.18,
                    "best_available_bookmaker": "Pinnacle",
                    "best_available_source_timestamp": "2026-08-30T09:00:00Z",
                    "settlement_aware_edge": 0.33,
                    "paper_entry_eligible": True,
                    "paper_trade_id": "paper_today_primary",
                    "market_freshness": "FRESH",
                    "day12_state": "PASSED",
                    "reason_codes": [],
                },
                {
                    "fixture_id": today_primary.fixture_id,
                    "competition_id": "ENG1",
                    "competition_name": "Premier League",
                    "decision": "NO_BET",
                    "current_decision_state": "NO_BET_VALUE",
                    "market_family": "btts",
                    "selection": "YES",
                    "line": None,
                    "model_probability": 0.52,
                    "model_fair_decimal_price": 1.90,
                    "best_available_price": 1.70,
                    "best_available_bookmaker": "bet365",
                    "best_available_source_timestamp": "2026-08-30T09:00:00Z",
                    "settlement_aware_edge": -0.07,
                    "paper_entry_eligible": False,
                    "paper_trade_id": None,
                    "market_freshness": "FRESH",
                    "day12_state": "PASSED",
                    "reason_codes": ["PRICE_NO_VALUE"],
                },
            ],
            "display_rows": [],
        },
        today_secondary.fixture_id: {
            "state": "READY",
            "reason": None,
            "decision_rows": [
                {
                    "fixture_id": today_secondary.fixture_id,
                    "competition_id": "ENG1",
                    "competition_name": "Premier League",
                    "decision": "PRELIMINARY_CANDIDATE",
                    "current_decision_state": "BET",
                    "market_family": "goals_total",
                    "selection": "OVER",
                    "line": 2.5,
                    "model_probability": 0.58,
                    "model_fair_decimal_price": 1.72,
                    "best_available_price": 2.04,
                    "best_available_bookmaker": "Pinnacle",
                    "best_available_source_timestamp": "2026-08-30T09:00:00Z",
                    "settlement_aware_edge": 0.19,
                    "paper_entry_eligible": True,
                    "paper_trade_id": "paper_today_secondary",
                    "market_freshness": "FRESH",
                    "day12_state": "PASSED",
                    "reason_codes": [],
                }
            ],
            "display_rows": [],
        },
    }
    return services, [today_primary, today_secondary, tomorrow_fixture]


def _seed_today_read_path_paper_rows(config: DashboardConfig, fixtures: list[NormalizedFixture]) -> None:
    with football_paper_trading.connect(config.paper_trading_db_path) as conn:
        primary_trade_id = _record_test_paper_trade(
            conn,
            fixture=fixtures[0],
            prediction_id="today_primary",
            market="1x2",
            selection="HOME",
            entry_odds=2.18,
            model_fair_price=1.64,
        )
        football_paper_trading.append_trade_update(
            conn,
            football_paper_trading.build_trade_update_payload(
                paper_trade_id=primary_trade_id,
                trade_state="WON",
                settlement_context={"actual_home_goals": 2, "actual_away_goals": 0},
                odds=2.18,
                notes="settled-primary",
            ),
        )
        _record_test_paper_trade(
            conn,
            fixture=fixtures[1],
            prediction_id="today_secondary",
            market="goals_total",
            selection="OVER",
            line=2.5,
            entry_odds=2.04,
            model_fair_price=1.72,
        )


def _portfolio_enabled_config(tmp_path: Path) -> DashboardConfig:
    return replace(
        _dashboard_config(tmp_path),
        portfolio_policy_effective_from="2026-08-01T00:00:00+00:00",
    )


def _normalize_traced_sql(sql: str) -> str:
    return " ".join(sql.strip().split()).lower()


def _sql_trace_counts(statements: list[str]) -> dict[str, int]:
    counts = {
        "select": 0,
        "insert": 0,
        "update": 0,
        "delete": 0,
        "ddl": 0,
        "pragma_table_info": 0,
        "sqlite_master": 0,
        "schema_introspection": 0,
        "writes": 0,
    }
    for statement in statements:
        normalized = _normalize_traced_sql(statement)
        if not normalized:
            continue
        if normalized.startswith("select"):
            counts["select"] += 1
        elif normalized.startswith("insert"):
            counts["insert"] += 1
            counts["writes"] += 1
        elif normalized.startswith("update"):
            counts["update"] += 1
            counts["writes"] += 1
        elif normalized.startswith("delete"):
            counts["delete"] += 1
            counts["writes"] += 1
        if normalized.startswith(("create table", "create index", "alter table", "drop table", "drop index")):
            counts["ddl"] += 1
        if normalized.startswith("pragma table_info"):
            counts["pragma_table_info"] += 1
            counts["schema_introspection"] += 1
        if "sqlite_master" in normalized:
            counts["sqlite_master"] += 1
            counts["schema_introspection"] += 1
    return counts


def _install_dashboard_sql_trace(monkeypatch, traced_sql: list[str]) -> None:
    original_dashboard_connect = dashboard_storage.connect
    original_paper_connect = football_paper_trading.connect

    def traced_dashboard_connect(*args, **kwargs):
        conn = original_dashboard_connect(*args, **kwargs)
        conn.set_trace_callback(traced_sql.append)
        return conn

    def traced_paper_connect(*args, **kwargs):
        conn = original_paper_connect(*args, **kwargs)
        conn.set_trace_callback(traced_sql.append)
        return conn

    monkeypatch.setattr(dashboard_storage, "connect", traced_dashboard_connect)
    monkeypatch.setattr(football_paper_trading, "connect", traced_paper_connect)


def _portfolio_prediction(fixture: NormalizedFixture) -> dict[str, object]:
    return {
        "prediction_id": f"portfolio_prediction_{fixture.fixture_id}",
        "fixture_id": fixture.fixture_id,
        "competition_id": fixture.competition_id,
        "competition_name": fixture.competition_name,
        "home_team_name": fixture.home_team_name,
        "away_team_name": fixture.away_team_name,
        "kickoff_utc": fixture.kickoff_utc,
        "season": fixture.season,
        "model_snapshot_id": f"snapshot_{fixture.fixture_id}",
        "model_versions": {"current_snapshot_model_version": "football_day11_manager_core_v1"},
        "prediction_version": "football_prediction_v1",
    }


def _portfolio_decision_row(
    *,
    fixture: NormalizedFixture,
    market_family: str,
    selection: str,
    line: float | None,
    paper_trade_id: str | None,
    edge: float,
    model_probability: float,
    fair_price: float,
    best_price: float,
    source_timestamp: str = "2026-08-20T10:00:00+00:00",
    current_decision_state: str = "BET",
    decision: str = "PRELIMINARY_CANDIDATE",
    paper_entry_eligible: bool = True,
    market_freshness: str = "FRESH",
    day12_state: str = "PASSED",
    reason_codes: list[str] | None = None,
) -> dict[str, object]:
    return {
        "fixture_id": fixture.fixture_id,
        "competition_id": fixture.competition_id,
        "competition_name": fixture.competition_name,
        "decision": decision,
        "current_decision_state": current_decision_state,
        "market_family": market_family,
        "selection": selection,
        "line": line,
        "model_probability": model_probability,
        "model_fair_decimal_price": fair_price,
        "best_available_price": best_price,
        "best_available_bookmaker": "Pinnacle",
        "best_available_source_timestamp": source_timestamp,
        "market_as_of": source_timestamp,
        "settlement_aware_edge": edge,
        "paper_entry_eligible": paper_entry_eligible,
        "paper_trade_id": paper_trade_id,
        "market_freshness": market_freshness,
        "day12_state": day12_state,
        "reason_codes": list(reason_codes or []),
    }


def _record_portfolio_test_trade(
    conn,
    *,
    fixture: NormalizedFixture,
    prediction_id: str,
    market: str,
    selection: str,
    line: float | None,
    edge: float,
    model_probability: float = 0.55,
    fair_price: float = 1.8,
    entry_odds: float = 2.0,
    decision_timestamp: str = "2026-08-20T10:00:00+00:00",
) -> str:
    return football_paper_trading.record_paper_trade_entry(
        conn,
        football_paper_trading.PaperTradeEntry(
            prediction_id=prediction_id,
            fixture_id=fixture.fixture_id,
            model_snapshot_id=f"snapshot_{prediction_id}",
            model_version="football_day11_manager_core_v1",
            market=market,
            selection=selection,
            line=line,
            model_probability=model_probability,
            model_fair_price=fair_price,
            available_market_price=entry_odds,
            edge=edge,
            minimum_acceptable_price=1.8,
            decision="PAPER_CANDIDATE",
            decision_timestamp=decision_timestamp,
            kickoff=fixture.kickoff_utc,
            safeguard_state="PASSED",
            entry_eligibility="ELIGIBLE",
            bookmaker="Pinnacle",
            provider="fake-market-provider",
            entry_odds=entry_odds,
            entry_captured_at=decision_timestamp,
        ),
    )


def test_api_football_normalization_maps_status_and_score():
    provider = ApiFootballFixtureProvider()
    payload = {
        "fixture": {
            "id": 12345,
            "date": "2026-08-19T19:00:00+00:00",
            "status": {"short": "1H", "elapsed": 27},
        },
        "league": {
            "id": 39,
            "name": "Premier League",
            "season": 2026,
        },
        "teams": {
            "home": {"id": 1, "name": "Arsenal"},
            "away": {"id": 2, "name": "Chelsea"},
        },
        "goals": {"home": 1, "away": 0},
    }

    fixture = provider._normalize_fixture(payload)

    assert fixture is not None
    assert fixture.fixture_id == "api-football:12345"
    assert fixture.status == "LIVE"
    assert fixture.elapsed_minute == 27
    assert fixture.home_score == 1
    assert fixture.away_score == 0
    assert fixture.competition_id == "ENG1"


def test_football_data_org_normalization_maps_status_and_score():
    provider = FootballDataOrgFixtureProvider()
    payload = {
        "id": 321,
        "utcDate": "2026-08-19T19:00:00Z",
        "status": "IN_PLAY",
        "lastUpdated": "2026-08-19T19:27:00Z",
        "competition": {
            "id": 2021,
            "code": "PL",
            "name": "Premier League",
        },
        "season": {
            "id": 2245,
            "startDate": "2026-08-14",
        },
        "homeTeam": {"id": 57, "shortName": "Arsenal"},
        "awayTeam": {"id": 61, "shortName": "Chelsea"},
        "score": {
            "fullTime": {"home": 1, "away": 0},
        },
    }

    fixture = provider._normalize_fixture(payload)

    assert fixture is not None
    assert fixture.fixture_id == "football-data.org:321"
    assert fixture.status == "LIVE"
    assert fixture.home_score == 1
    assert fixture.away_score == 0
    assert fixture.competition_id == "ENG1"
    assert fixture.season == "2026"


def test_dashboard_services_generate_prediction_and_ignore_live_score(tmp_path: Path):
    fixture = _fixture()
    provider = FakeFixtureProvider([fixture])
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=provider,
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())
    initial_payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert initial_payload is not None
    assert initial_payload["prediction"]["prediction_status"] == "READY"
    initial_prediction = initial_payload["prediction"]

    provider.fixtures = [
        replace(
            fixture,
            status=FIXTURE_STATUS_LIVE,
            home_score=3,
            away_score=2,
            elapsed_minute=74,
        )
    ]
    asyncio.run(services.sync_once())

    updated_payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert updated_payload is not None
    updated_prediction = updated_payload["prediction"]
    assert updated_prediction["version_key"] == initial_prediction["version_key"]
    assert updated_prediction["expected_home_goals"] == initial_prediction["expected_home_goals"]
    assert updated_prediction["expected_away_goals"] == initial_prediction["expected_away_goals"]
    assert updated_prediction["top_scorelines"] == initial_prediction["top_scorelines"]
    asyncio.run(services.shutdown())


def test_dashboard_services_shadow_edge_desk_records_route_a_and_route_b_without_changing_current_prediction(tmp_path: Path):
    fixture = _fixture()
    config = _dashboard_config(tmp_path)
    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())
    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["prediction"]["prediction_status"] == "READY"
    assert payload["prediction"]["fixture_id"] == fixture.fixture_id
    assert payload["prediction"]["competition_id"] == "ENG1"

    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        active_agents = football_edge_desk.list_active_agents(conn)
        edge_predictions = football_edge_desk.fetch_agent_predictions(conn, fixture_id=fixture.fixture_id)
        edge_consensus = football_edge_desk.fetch_consensus_evaluations(conn, fixture_id=fixture.fixture_id)
        edge_decisions = football_edge_desk.fetch_decision_history(conn, fixture_id=fixture.fixture_id)

    assert edge_predictions
    assert {row["agent_id"] for row in active_agents} == {football_edge_desk.ROUTE_B_COMPOSITE_AGENT_ID}
    assert any(row["agent_id"] == football_edge_desk.ROUTE_B_COMPOSITE_AGENT_ID for row in edge_predictions)
    assert any(row["agent_id"] == "ROUTE_A_DIRECT_CLASSIFIER" for row in edge_predictions)
    assert all(
        row["prediction_status"] == "PREDICTED"
        for row in edge_predictions
        if row["agent_id"] == football_edge_desk.ROUTE_B_COMPOSITE_AGENT_ID
    )
    assert {
        row["market_family"]
        for row in edge_predictions
        if row["agent_id"] == "ROUTE_A_DIRECT_CLASSIFIER"
    } == {"btts", "goals_total"}
    assert all(
        row["prediction_status"] == "BLOCKED"
        and row["block_reason"] == "MARKET_REJECTED"
        for row in edge_predictions
        if row["agent_id"] == "ROUTE_A_DIRECT_CLASSIFIER"
    )
    assert edge_consensus
    assert any(row["market_family"] == "btts" and row["status"] == "INSUFFICIENT_AGENTS" for row in edge_consensus)
    assert any(
        row["market_family"] == "goals_total" and row["line"] == 2.5 and row["status"] == "INSUFFICIENT_AGENTS"
        for row in edge_consensus
    )
    assert all(
        row["status"] == "INSUFFICIENT_AGENTS"
        for row in edge_consensus
    )
    assert edge_decisions
    assert all(row["decision_status"] == "NO_BET" for row in edge_decisions)
    status = services.get_system_status()
    assert status["storage"]["edge_desk_shadow"]["available"] is True
    assert status["storage"]["edge_desk_shadow"]["state"] == "READY"
    assert status["storage"]["edge_desk_shadow"]["last_error"] is None
    asyncio.run(services.shutdown())


def test_dashboard_services_fail_open_when_edge_desk_init_fails(monkeypatch, tmp_path: Path):
    fixture = _fixture()
    config = _dashboard_config(tmp_path)
    shadow_calls = {"count": 0}

    def broken_edge_desk_schema(conn):
        raise RuntimeError("edge desk init failed")

    def record_shadow_call(*args, **kwargs):
        shadow_calls["count"] += 1

    monkeypatch.setattr(football_edge_desk, "ensure_schema", broken_edge_desk_schema)
    monkeypatch.setattr(football_edge_desk, "record_shadow_prediction_bundle", record_shadow_call)

    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())

    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["prediction"]["prediction_status"] == "READY"

    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        prediction = dashboard_storage.fetch_latest_prediction_for_fixture(conn, fixture.fixture_id)
    assert prediction is not None
    assert prediction["prediction_status"] == "READY"

    status = services.get_system_status()
    assert status["storage"]["edge_desk_shadow"]["available"] is False
    assert status["storage"]["edge_desk_shadow"]["state"] == "DEGRADED"
    assert "initialization failure" in (status["storage"]["edge_desk_shadow"]["last_error"] or "")
    assert "edge desk init failed" in (status["storage"]["edge_desk_shadow"]["last_error"] or "")
    assert shadow_calls["count"] == 0

    asyncio.run(services.shutdown())


def test_dashboard_services_runtime_edge_desk_failure_degrades_status_and_skips_future_writes(
    monkeypatch,
    tmp_path: Path,
):
    fixture = _fixture()
    second_fixture = _fixture(
        fixture_id="fake:liverpool_everton",
        home_team_name="Liverpool",
        away_team_name="Everton",
    )
    config = _dashboard_config(tmp_path)
    shadow_calls = {"count": 0}
    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([fixture, second_fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())

    healthy_status = services.get_system_status()
    assert healthy_status["storage"]["edge_desk_shadow"]["available"] is True
    assert healthy_status["storage"]["edge_desk_shadow"]["state"] == "READY"
    assert healthy_status["storage"]["edge_desk_shadow"]["last_error"] is None

    def broken_edge_desk_write(*args, **kwargs):
        shadow_calls["count"] += 1
        raise RuntimeError("edge desk runtime write failed")

    monkeypatch.setattr(football_edge_desk, "record_shadow_prediction_bundle", broken_edge_desk_write)

    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        edge_prediction_count_before = len(football_edge_desk.fetch_agent_predictions(conn, fixture_id=fixture.fixture_id))
        second_edge_prediction_count_before = len(
            football_edge_desk.fetch_agent_predictions(conn, fixture_id=second_fixture.fixture_id)
        )

    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        first_payload = services.ensure_prediction(conn, fixture)
    assert first_payload["prediction_status"] == "READY"

    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        first_prediction = dashboard_storage.fetch_latest_prediction_for_fixture(conn, fixture.fixture_id)
        persisted_runtime_state = dashboard_storage.get_runtime_state(conn, "storage_runtime_state")
        edge_predictions = football_edge_desk.fetch_agent_predictions(conn, fixture_id=fixture.fixture_id)

    assert first_prediction is not None
    assert first_prediction["prediction_status"] == "READY"
    assert len(edge_predictions) == edge_prediction_count_before
    assert persisted_runtime_state is not None
    assert persisted_runtime_state["edge_desk_shadow"]["available"] is False
    assert persisted_runtime_state["edge_desk_shadow"]["state"] == "DEGRADED"
    assert "runtime shadow-write failure" in (persisted_runtime_state["edge_desk_shadow"]["last_error"] or "")
    assert "edge desk runtime write failed" in (persisted_runtime_state["edge_desk_shadow"]["last_error"] or "")

    degraded_status = services.get_system_status()
    assert degraded_status["storage"]["edge_desk_shadow"]["available"] is False
    assert degraded_status["storage"]["edge_desk_shadow"]["state"] == "DEGRADED"
    assert "runtime shadow-write failure" in (degraded_status["storage"]["edge_desk_shadow"]["last_error"] or "")
    assert "edge desk runtime write failed" in (degraded_status["storage"]["edge_desk_shadow"]["last_error"] or "")

    reload_payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert reload_payload is not None
    assert reload_payload["prediction"]["prediction_status"] == "READY"
    assert reload_payload["prediction"]["fixture_id"] == fixture.fixture_id
    assert "consensus_rows" not in reload_payload["prediction"]

    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        second_payload = services.ensure_prediction(conn, second_fixture)
    assert second_payload["prediction_status"] == "READY"

    second_detail_payload = services.get_fixture_detail_payload(second_fixture.fixture_id)
    assert second_detail_payload is not None
    assert second_detail_payload["prediction"]["prediction_status"] == "READY"
    assert second_detail_payload["prediction"]["fixture_id"] == second_fixture.fixture_id

    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        second_prediction = dashboard_storage.fetch_latest_prediction_for_fixture(conn, second_fixture.fixture_id)
        second_runtime_state = dashboard_storage.get_runtime_state(conn, "storage_runtime_state")
        second_edge_predictions = football_edge_desk.fetch_agent_predictions(conn, fixture_id=second_fixture.fixture_id)

    assert second_prediction is not None
    assert second_prediction["prediction_status"] == "READY"
    assert second_runtime_state is not None
    assert second_runtime_state["edge_desk_shadow"]["available"] is False
    assert second_runtime_state["edge_desk_shadow"]["state"] == "DEGRADED"
    assert len(second_edge_predictions) == second_edge_prediction_count_before
    assert shadow_calls["count"] == 1

    asyncio.run(services.shutdown())


def test_dashboard_config_default_uses_configured_data_dir(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("SPORTS_AI_DATA_DIR", str(tmp_path))
    config = DashboardConfig.default()

    assert config.operational_data_dir == tmp_path
    assert config.dashboard_db_path == tmp_path / "football_dashboard.sqlite"
    assert config.paper_trading_db_path == tmp_path / "football_paper_trading.sqlite"
    assert config.storage_mode == "persistent"
    assert config.persistent_data_dir_configured is True


def test_dashboard_services_odds_sync_disabled_makes_no_provider_calls(tmp_path: Path):
    fixture = _fixture()
    calls = {"count": 0}

    class RecordingMarketProvider(FakeMarketProvider):
        def get_market_snapshot(
            self,
            fixture_id: str,
            *,
            fixture_context: dict | None = None,
        ) -> list[NormalizedMarketSnapshot]:
            calls["count"] += 1
            return []

    config = replace(_dashboard_config(tmp_path), current_odds_connected=True, enable_odds_sync=False)
    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=RecordingMarketProvider(),
    )

    asyncio.run(services.initialize())
    asyncio.run(services.sync_once())
    assert calls["count"] == 0
    asyncio.run(services.shutdown())


def test_dashboard_services_block_unresolved_team_identity(tmp_path: Path):
    fixture = _fixture(home_team_name="Unknown Town")
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())
    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["prediction"]["prediction_status"] == "BLOCKED"
    assert payload["prediction"]["blockers"] == ["HOME_TEAM_IDENTITY_UNRESOLVED"]
    asyncio.run(services.shutdown())


def test_dashboard_services_do_not_generate_first_prediction_after_kickoff(tmp_path: Path):
    fixture = replace(
        _fixture(),
        fixture_id="fake:past_fixture",
        kickoff_utc=(
            datetime.now(timezone.utc).replace(hour=20, minute=0, second=0, microsecond=0) - timedelta(days=2)
        ).isoformat(),
        status=FIXTURE_STATUS_LIVE,
        elapsed_minute=52,
        home_score=1,
        away_score=0,
    )
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())
    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["prediction"]["prediction_status"] == "BLOCKED"
    assert payload["prediction"]["blockers"] == ["PREDICTION_AFTER_KICKOFF_BLOCKED"]
    assert payload["prediction"]["lock_state"] == "LOCKED_PRE_MATCH_PREDICTION"
    asyncio.run(services.shutdown())


def test_normalize_text_key_preserves_safe_tokens():
    assert normalize_text_key("Derby County FC") == "derby county"
    assert normalize_text_key("Real Racing Club de Santander") == "real racing de santander"


def test_dashboard_services_resolve_alias_and_generate_prediction(tmp_path: Path):
    fixture = _fixture(home_team_name="Brighton Hove", away_team_name="Chelsea")
    fixture = replace(fixture, provider="football-data.org", home_team_id="397")
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())
    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["prediction"]["prediction_status"] == "READY"
    assert payload["prediction"]["team_resolution"]["home"]["canonical_team_name"] == "Brighton"
    assert payload["prediction"]["team_resolution"]["home"]["model_history_available"] is True
    asyncio.run(services.shutdown())


def test_dashboard_services_block_missing_model_history_after_identity_resolution(tmp_path: Path):
    fixture = replace(
        _fixture(home_team_name="West Ham", away_team_name="Leeds"),
        provider="football-data.org",
        competition_id="ENG2",
        competition_name="Championship",
        division_code="E1",
        home_team_id="563",
        away_team_id="341",
    )
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())
    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["prediction"]["prediction_status"] == "BLOCKED"
    assert payload["prediction"]["blockers"] == ["HOME_MODEL_HISTORY_MISSING"]
    assert payload["prediction"]["team_resolution"]["home"]["canonical_team_name"] == "West Ham"
    assert payload["prediction"]["model_history"]["home"]["available"] is False
    status = services.get_system_status()
    assert status["team_resolution"]["model_history_missing"] >= 1
    asyncio.run(services.shutdown())


def test_dashboard_services_resolve_malaga_alias_but_keep_history_block(tmp_path: Path):
    fixture = replace(
        _fixture(home_team_name="Atleti", away_team_name="M\ufffdlaga"),
        provider="football-data.org",
        competition_id="ESP1",
        competition_name="Primera Division",
        division_code="SP1",
        home_team_id="78",
        away_team_id="84",
    )
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())
    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["prediction"]["prediction_status"] == "BLOCKED"
    assert payload["prediction"]["blockers"] == ["AWAY_MODEL_HISTORY_MISSING"]
    assert payload["prediction"]["team_resolution"]["home"]["canonical_team_name"] == "Ath Madrid"
    assert payload["prediction"]["team_resolution"]["away"]["canonical_team_name"] == "Malaga"
    assert payload["prediction"]["model_history"]["away"]["available"] is False
    asyncio.run(services.shutdown())


def test_dashboard_status_recomputes_team_resolution_from_current_fixture_rows(tmp_path: Path):
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([_fixture()]),
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())
    kickoff_utc = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0).isoformat()
    counts = services._team_resolution_counts(
        [
            {
                "fixture_id": "football-data.org:564628",
                "provider": "football-data.org",
                "competition_id": "ESP1",
                "competition_name": "Primera Division",
                "status": FIXTURE_STATUS_PRE_MATCH,
                "home_team_id": "78",
                "home_team_name": "Atleti",
                "away_team_id": "84",
                "away_team_name": "M\ufffdlaga",
                "kickoff_utc": kickoff_utc,
                "prediction": {
                    "prediction_id": "prediction_legacy_malaga",
                    "fixture_id": "football-data.org:564628",
                    "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    "prediction_status": "BLOCKED",
                    "blockers": ["PREDICTION_BLOCKED_TEAM_IDENTITY"],
                    "team_resolution": {
                        "home": {
                            "provider": "football-data.org",
                            "competition_id": "ESP1",
                            "provider_team_id": "78",
                            "provider_team_name": "Atleti",
                            "normalized_team_name": "atleti",
                            "canonical_team_id": None,
                            "canonical_team_name": None,
                            "resolution_kind": "unresolved",
                            "confidence": 0.0,
                            "reason": "no deterministic canonical team mapping found",
                        },
                        "away": {
                            "provider": "football-data.org",
                            "competition_id": "ESP1",
                            "provider_team_id": "84",
                            "provider_team_name": "M\ufffdlaga",
                            "normalized_team_name": "mlaga",
                            "canonical_team_id": None,
                            "canonical_team_name": None,
                            "resolution_kind": "unresolved",
                            "confidence": 0.0,
                            "reason": "no deterministic canonical team mapping found",
                        },
                    },
                    "model_history": None,
                },
            }
        ]
    )
    assert counts["unresolved"] == 0
    assert counts["model_history_missing"] >= 1
    assert any(
        example["provider_team_id"] == "84"
        for example in counts["model_history_missing_examples"]
    )
    asyncio.run(services.shutdown())


def test_dashboard_services_leave_bare_deportivo_unresolved_without_context(tmp_path: Path):
    fixture = replace(
        _fixture(home_team_name="Deportivo", away_team_name="Valencia"),
        provider="football-data.org",
        competition_id="ESP1",
        competition_name="Primera Division",
        division_code="SP1",
        home_team_id=None,
        away_team_id="95",
    )
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())
    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["prediction"]["prediction_status"] == "BLOCKED"
    assert payload["prediction"]["blockers"] == ["HOME_TEAM_IDENTITY_UNRESOLVED"]
    assert payload["prediction"]["team_resolution"]["home"]["canonical_team_name"] is None
    asyncio.run(services.shutdown())


def test_dashboard_routes_render_and_apis_return_current_prediction(tmp_path: Path):
    fixture = _fixture()
    app = create_app(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    with TestClient(app) as client:
        dashboard_response = client.get("/")
        assert dashboard_response.status_code == 200
        assert "Sports Edge AI" in dashboard_response.text
        assert "EDGE DESK" in dashboard_response.text
        assert 'href="/static/dashboard.css"' in dashboard_response.text
        assert 'src="/static/dashboard.js"' in dashboard_response.text
        assert "/matches/static/dashboard.css" not in dashboard_response.text
        assert 'href="/status"' in dashboard_response.text

        matches_response = client.get("/matches")
        assert matches_response.status_code == 200
        assert "Matches &amp; Live Monitor" in matches_response.text
        assert 'href="/static/dashboard.css"' in matches_response.text
        assert 'src="/static/dashboard.js"' in matches_response.text
        assert "/matches/static/dashboard.css" not in matches_response.text
        assert 'data-auto-submit-filters="true"' in matches_response.text
        assert "Apply Filters" not in matches_response.text

        match_detail_response = client.get(f"/matches/{fixture.fixture_id}")
        assert match_detail_response.status_code == 200
        assert fixture.home_team_name in match_detail_response.text
        assert fixture.away_team_name in match_detail_response.text
        assert "Fixture And Prediction State" in match_detail_response.text

        predictions_page = client.get("/predictions")
        assert predictions_page.status_code == 200
        assert "Current Football Predictions" in predictions_page.text
        assert 'href="/static/dashboard.css"' in predictions_page.text
        assert 'src="/static/dashboard.js"' in predictions_page.text

        candidates_page = client.get("/candidates")
        assert candidates_page.status_code == 200
        assert "Betting Desk" in candidates_page.text
        assert 'href="/static/dashboard.css"' in candidates_page.text
        assert 'src="/static/dashboard.js"' in candidates_page.text

        performance_page = client.get("/performance")
        assert performance_page.status_code == 200
        assert "Model Performance" in performance_page.text
        assert 'href="/static/dashboard.css"' in performance_page.text
        assert 'src="/static/dashboard.js"' in performance_page.text

        status_page = client.get("/status")
        assert status_page.status_code == 200
        assert "System health, feeds and model readiness." in status_page.text
        assert "Route B Composite" in status_page.text
        assert "Route A Direct Classifier" in status_page.text
        assert "NOT IMPLEMENTED" in status_page.text
        assert "/api/system/status" in status_page.text
        assert 'href="/static/dashboard.css"' in status_page.text
        assert 'src="/static/dashboard.js"' in status_page.text

        stylesheet_response = client.get("/static/dashboard.css")
        assert stylesheet_response.status_code == 200
        assert "text/css" in stylesheet_response.headers["content-type"]
        assert ":root" in stylesheet_response.text
        assert ".app-shell" in stylesheet_response.text

        status_response = client.get("/api/system/status")
        assert status_response.status_code == 200
        assert status_response.json()["fixture_feed"]["state"] == "CONNECTED"

        upcoming_response = client.get("/api/fixtures/upcoming")
        assert upcoming_response.status_code == 200
        assert upcoming_response.json()["count"] == 1

        next_response = client.get("/api/fixtures/next")
        assert next_response.status_code == 200
        assert next_response.json()["count"] == 1

        predictions_response = client.get("/api/predictions")
        assert predictions_response.status_code == 200
        assert predictions_response.json()["rows"][0]["prediction"]["prediction_status"] == "READY"

        performance_response = client.get("/api/performance")
        assert performance_response.status_code == 200
        assert performance_response.json()["summary"]["MODEL_VALIDATION"] == "PROMISING"

        health_response = client.get("/health")
        assert health_response.status_code == 200
        assert health_response.json()["status"] == "ok"
        assert health_response.json()["web_process_status"] == "ok"
        assert health_response.json()["startup_sync_state"] == "COMPLETED"
        assert health_response.json()["scheduler_running"] is False
        assert health_response.json()["market_sync_state"] == "NOT_CONFIGURED"

        prediction_response = client.get(f"/api/predictions/{fixture.fixture_id}")
        assert prediction_response.status_code == 200
        assert prediction_response.json()["prediction_status"] == "READY"

        refresh_response = client.post("/api/fixtures/refresh")
        assert refresh_response.status_code == 200
        assert refresh_response.json()["refresh_state"] in {"SYNCED", "COOLDOWN"}


def test_dashboard_routes_surface_provider_not_configured(tmp_path: Path):
    app = create_app(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider(configured=False),
        market_provider=FakeMarketProvider(configured=False),
    )

    with TestClient(app) as client:
        status_response = client.get("/api/system/status")
        assert status_response.status_code == 200
        payload = status_response.json()
        assert payload["fixture_feed"]["state"] == "NOT_CONFIGURED"
        assert payload["current_fixture_feed_ready"] == "NO"
        assert payload["storage"]["persistent_path_configured"] == "NO"


def test_dashboard_routes_surface_provider_diagnostics_errors(tmp_path: Path):
    fixture = _fixture()
    diagnostics = {
        "provider": "fake-provider",
        "query_count": 1,
        "league_ids_queried": [39],
        "provider_seasons_queried": ["2026"],
        "has_errors": True,
        "last_error": {
            "provider_season": "2026",
            "provider_league_id": 39,
            "errors": {"plan": "current season unavailable on this plan"},
        },
        "queries": [],
    }
    app = create_app(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture], diagnostics=diagnostics),
        market_provider=FakeMarketProvider(configured=False),
    )

    with TestClient(app) as client:
        payload = client.get("/api/system/status").json()
        assert payload["fixture_feed"]["state"] == "DEGRADED"
        assert "current season unavailable on this plan" in payload["fixture_feed"]["last_error"]


def test_dashboard_services_restart_preserves_prediction_without_duplicates(tmp_path: Path):
    fixture = _fixture()
    config = _dashboard_config(tmp_path)

    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )
    asyncio.run(services.initialize())
    asyncio.run(services.shutdown())

    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )
    asyncio.run(services.initialize())

    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        predictions = dashboard_storage.fetch_all_predictions(conn)
        assert len(predictions) == 1
        assert predictions[0]["prediction_status"] == "READY"
    status = services.get_system_status()
    assert status["storage"]["dashboard_db_reachable"] is True
    assert status["prediction_engine"]["last_successful_prediction_at"] is not None
    asyncio.run(services.shutdown())


def test_dashboard_initialize_can_continue_while_startup_sync_runs_in_background(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = _fixture()
    services = DashboardServices(
        config=replace(_dashboard_config(tmp_path), startup_sync_timeout_seconds=0),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_sync_once(*, sync_kind: str = "manual", include_schedule: bool = True) -> dict[str, str]:
        started.set()
        await release.wait()
        return {"state": "ok", "kind": sync_kind, "include_schedule": str(include_schedule)}

    monkeypatch.setattr(services, "sync_once", fake_sync_once)

    async def scenario() -> None:
        await services.initialize()
        await asyncio.wait_for(started.wait(), timeout=1)
        running_health = services.get_health_payload()
        assert running_health["startup_sync_state"] == "RUNNING"
        assert running_health["startup_sync_task_running"] is True
        assert running_health["scheduler_running"] is False

        release.set()
        assert services._startup_sync_task is not None
        await asyncio.wait_for(services._startup_sync_task, timeout=1)

        completed_health = services.get_health_payload()
        assert completed_health["startup_sync_state"] == "COMPLETED"
        assert completed_health["startup_sync_task_running"] is False
        await services.shutdown()

    asyncio.run(scenario())


def test_dashboard_services_recovers_after_provider_failure(tmp_path: Path):
    fixture = _fixture()
    provider = FlakyFixtureProvider([fixture])
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=provider,
        market_provider=FakeMarketProvider(configured=False),
    )

    asyncio.run(services.initialize())
    first_status = services.get_system_status()
    assert first_status["fixture_feed"]["state"] == "DEGRADED"
    assert "transient provider failure" in (first_status["fixture_feed"]["last_error"] or "")

    asyncio.run(services.sync_once())
    second_status = services.get_system_status()
    assert second_status["fixture_feed"]["state"] == "CONNECTED"
    assert second_status["current_fixture_state"]["fixtures_discovered"] == 1
    asyncio.run(services.shutdown())


def test_dashboard_services_current_market_candidate_creates_idempotent_paper_entry(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    fixture = _fixture()
    config = replace(
        _dashboard_config(tmp_path),
        current_odds_connected=True,
        current_xg_connected=True,
        enable_odds_sync=True,
    )
    market_provider = FakeMarketProvider()

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

    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=market_provider,
        xg_profile_factory=xg_factory,
    )

    asyncio.run(services.initialize())
    market_provider.snapshots_by_fixture[fixture.fixture_id] = [
        _market_snapshot(
            fixture=fixture,
            bookmaker="Pinnacle",
            market="1x2",
            selection="HOME",
            decimal_odds=2.05,
            reference_designation="reference",
            provider_market_id="pin-home",
        ),
        _market_snapshot(
            fixture=fixture,
            bookmaker="Pinnacle",
            market="1x2",
            selection="DRAW",
            decimal_odds=3.35,
            reference_designation="reference",
            provider_market_id="pin-draw",
        ),
        _market_snapshot(
            fixture=fixture,
            bookmaker="Pinnacle",
            market="1x2",
            selection="AWAY",
            decimal_odds=3.95,
            reference_designation="reference",
            provider_market_id="pin-away",
        ),
        _market_snapshot(
            fixture=fixture,
            bookmaker="bet365",
            market="1x2",
            selection="HOME",
            decimal_odds=10.0,
            reference_designation="available",
            provider_market_id="b365-home",
        ),
    ]

    asyncio.run(services.sync_once())
    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["market_evaluation"]["state"] == "READY"
    assert payload["market_evaluation"]["counts"]["preliminary_candidates"] >= 1
    assert payload["fixture"]["market_signal"]["decision"] == "PRELIMINARY_CANDIDATE"

    candidates_payload = services.get_candidates_payload()
    assert candidates_payload["connected"] is True
    assert candidates_payload["summary"]["rows"] >= 1

    performance_payload = services.get_performance_payload()
    assert performance_payload["current_paper_summary"]["positions"] == 1
    assert performance_payload["current_paper_summary"]["open_positions"] == 1
    assert performance_payload["recent_paper_bets"] == []
    assert len(performance_payload["open_paper_positions"]) == 1
    assert performance_payload["live_paper_kpis"][0] == ("Tracked", "1")

    with football_paper_trading.connect(config.paper_trading_db_path) as conn:
        paper_rows = football_paper_trading.fetch_paper_trade_rows(conn)
        assert len(paper_rows) == 1
        assert paper_rows[0]["entry_snapshot_id"] is not None
        first_trade_id = str(paper_rows[0]["paper_trade_id"])

    asyncio.run(services.sync_once())

    with football_paper_trading.connect(config.paper_trading_db_path) as conn:
        paper_rows = football_paper_trading.fetch_paper_trade_rows(conn)
        assert len(paper_rows) == 1
        assert str(paper_rows[0]["paper_trade_id"]) == first_trade_id

    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        edge_decisions = football_edge_desk.fetch_decision_history(conn, fixture_id=fixture.fixture_id)
        edge_consensus = football_edge_desk.fetch_consensus_evaluations(conn, fixture_id=fixture.fixture_id)

    assert edge_consensus
    assert any(
        row["market_family"] == "1x2" and row["status"] == "INSUFFICIENT_AGENTS"
        for row in edge_consensus
    )
    assert any(
        row["market_family"] == "btts" and row["status"] == "INSUFFICIENT_AGENTS"
        for row in edge_consensus
    )
    assert edge_decisions
    assert any(
        row["market_family"] == "1x2" and row["decision_status"] == "NO_BET"
        for row in edge_decisions
    )
    assert any(
        row["market_family"] in {"btts", "goals_total"} and row["decision_status"] == "NO_BET"
        for row in edge_decisions
    )

    asyncio.run(services.shutdown())


def test_dashboard_services_reuses_original_paper_entry_across_prediction_versions(tmp_path: Path) -> None:
    fixture = _fixture()
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=True),
    )
    initial_row = {
        "market_family": "1x2",
        "selection": "HOME",
        "line": None,
        "model_probability": 0.63,
        "model_fair_decimal_price": 1.59,
        "best_available_price": 2.05,
        "best_available_bookmaker": "Pinnacle",
        "best_available_source_timestamp": "2026-08-28T08:28:07+00:00",
        "settlement_aware_edge": 0.2915,
        "minimum_acceptable_price": 1.90,
        "paper_entry_eligible": True,
        "market_freshness": "FRESH",
        "day12_state": "PASSED",
        "market_as_of": "2026-08-28T08:30:00+00:00",
        "entry_snapshot_id": 101,
    }
    recapture_row = {
        **initial_row,
        "model_probability": 0.67,
        "model_fair_decimal_price": 1.49,
        "best_available_price": 2.25,
        "best_available_bookmaker": "Betfair",
        "best_available_source_timestamp": "2026-08-28T09:10:00+00:00",
        "settlement_aware_edge": 0.3389,
        "minimum_acceptable_price": 2.00,
        "market_as_of": "2026-08-28T09:15:00+00:00",
        "entry_snapshot_id": 202,
    }
    initial_prediction = {
        "prediction_id": "pred_initial",
        "fixture_id": fixture.fixture_id,
        "model_snapshot_id": "snapshot_initial",
        "model_versions": {"current_snapshot_model_version": "football_day11_manager_core_v1"},
        "prediction_version": "football_prediction_v1",
        "kickoff_utc": fixture.kickoff_utc,
    }
    recapture_prediction = {
        "prediction_id": "pred_recapture",
        "fixture_id": fixture.fixture_id,
        "model_snapshot_id": "snapshot_recapture",
        "model_versions": {"current_snapshot_model_version": "football_day11_manager_core_v2"},
        "prediction_version": "football_prediction_v2",
        "kickoff_utc": fixture.kickoff_utc,
    }

    with football_paper_trading.connect(services.config.paper_trading_db_path) as conn:
        created = services._maybe_create_paper_entries(
            conn,
            prediction=initial_prediction,
            evaluation={"decision_rows": [initial_row]},
        )
        paper_rows = football_paper_trading.fetch_paper_trade_rows(conn)
        assert created == 1
        assert len(paper_rows) == 1
        original_trade_id = str(paper_rows[0]["paper_trade_id"])

        created = services._maybe_create_paper_entries(
            conn,
            prediction=recapture_prediction,
            evaluation={"decision_rows": [recapture_row]},
        )
        paper_rows = football_paper_trading.fetch_paper_trade_rows(conn)

    assert created == 0
    assert initial_row["paper_trade_id"] == original_trade_id
    assert recapture_row["paper_trade_id"] == original_trade_id
    assert len(paper_rows) == 1
    assert paper_rows[0]["paper_trade_id"] == original_trade_id
    assert paper_rows[0]["prediction_id"] == "pred_initial"
    assert paper_rows[0]["model_snapshot_id"] == "snapshot_initial"
    assert paper_rows[0]["model_version"] == "football_day11_manager_core_v1"
    assert paper_rows[0]["decision_timestamp"] == "2026-08-28T08:30:00+00:00"
    assert paper_rows[0]["entry_odds"] == pytest.approx(2.05)
    assert paper_rows[0]["bookmaker"] == "Pinnacle"
    assert paper_rows[0]["entry_snapshot_id"] == 101


def test_dashboard_services_settles_finished_paper_trade_idempotently_and_reports_live_results(tmp_path: Path):
    kickoff_utc = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(microsecond=0).isoformat()
    pending_fixture = replace(
        _fixture(),
        kickoff_utc=kickoff_utc,
        status=FIXTURE_STATUS_PRE_MATCH,
    )
    config = replace(
        _dashboard_config(tmp_path),
        current_odds_connected=True,
        current_xg_connected=True,
        enable_odds_sync=True,
    )
    provider = FakeFixtureProvider([pending_fixture])
    services = DashboardServices(
        config=config,
        fixture_provider=provider,
        market_provider=FakeMarketProvider(configured=True),
    )

    asyncio.run(services.initialize())
    with football_paper_trading.connect(config.paper_trading_db_path) as conn:
        football_paper_trading.record_paper_trade_entry(
            conn,
            football_paper_trading.PaperTradeEntry(
                prediction_id="pred_settle_1",
                fixture_id=pending_fixture.fixture_id,
                model_snapshot_id="snapshot_settle_1",
                model_version="football_day11_manager_core_v1",
                market="1x2",
                selection="HOME",
                model_probability=0.52,
                model_fair_price=1.92,
                available_market_price=2.2,
                edge=0.06,
                minimum_acceptable_price=2.0,
                decision="PAPER_CANDIDATE",
                decision_timestamp=(datetime.now(timezone.utc) - timedelta(hours=4)).replace(microsecond=0).isoformat(),
                kickoff=kickoff_utc,
                safeguard_state="READY",
                entry_eligibility="ELIGIBLE",
                bookmaker="Pinnacle",
                provider="fake-market-provider",
                entry_odds=2.2,
                entry_captured_at=(datetime.now(timezone.utc) - timedelta(hours=4)).replace(microsecond=0).isoformat(),
            ),
        )

    asyncio.run(services.sync_once())
    initial_performance = services.get_performance_payload()
    assert initial_performance["current_paper_summary"]["settled_positions"] == 0
    assert initial_performance["current_paper_summary"]["pending_positions"] == 1
    assert initial_performance["recent_paper_bets"] == []
    assert len(initial_performance["open_paper_positions"]) == 1

    provider.fixtures = [
        replace(
            pending_fixture,
            status=FIXTURE_STATUS_FINISHED,
            home_score=2,
            away_score=1,
            last_provider_update=(datetime.now(timezone.utc) - timedelta(minutes=2)).replace(microsecond=0).isoformat(),
        )
    ]

    asyncio.run(services.sync_once())
    asyncio.run(services.sync_once())

    with football_paper_trading.connect(config.paper_trading_db_path) as conn:
        paper_rows = football_paper_trading.fetch_paper_trade_rows(conn)
        updates = conn.execute(
            "SELECT trade_state, payload_json FROM paper_trade_updates ORDER BY paper_trade_update_id"
        ).fetchall()

    assert len(paper_rows) == 1
    assert paper_rows[0]["trade_state"] == "WON"
    assert paper_rows[0]["entry_odds"] == pytest.approx(2.2)
    assert len(updates) == 1
    assert updates[0]["trade_state"] == "WON"
    settlement_context = json.loads(str(updates[0]["payload_json"]))["settlement_context"]
    assert settlement_context["provider"] == "fake-provider"
    assert settlement_context["provider_fixture_id"] == "fixture-1"
    assert settlement_context["final_status"] == "FINISHED"
    assert settlement_context["actual_home_goals"] == 2
    assert settlement_context["actual_away_goals"] == 1

    performance_payload = services.get_performance_payload()
    assert performance_payload["current_paper_summary"]["settled_positions"] == 1
    assert performance_payload["current_paper_summary"]["wins"] == 1
    assert performance_payload["current_paper_summary"]["pending_positions"] == 0
    assert performance_payload["current_paper_summary"]["strike_rate"] == pytest.approx(1.0)
    assert performance_payload["current_paper_summary"]["net_units"] == pytest.approx(1.2)
    assert performance_payload["current_paper_summary"]["roi"] == pytest.approx(1.2)
    assert performance_payload["live_paper_market_rows"][0]["market"] == "1x2"
    assert performance_payload["live_paper_market_rows"][0]["wins"] == 1
    assert performance_payload["recent_paper_bets"][0]["final_score"] == "2-1"
    assert performance_payload["recent_paper_bets"][0]["fixture_label"] == "Arsenal vs Chelsea"
    assert performance_payload["recent_paper_bets"][0]["result_provider"] == "fake-provider"
    assert performance_payload["open_paper_positions"] == []

    app = create_app(
        config=config,
        fixture_provider=provider,
        market_provider=FakeMarketProvider(configured=True),
    )
    with TestClient(app) as client:
        response = client.get("/performance")
        api_response = client.get("/api/performance")

    assert response.status_code == 200
    assert "Model Performance" in response.text
    assert api_response.status_code == 200
    assert api_response.json()["summary"]["MODEL_VALIDATION"] == "PROMISING"
    assert api_response.json()["current_paper_summary"]["roi"] == pytest.approx(1.2)
    assert api_response.json()["recent_paper_bets"][0]["fixture_label"] == "Arsenal vs Chelsea"
    assert api_response.json()["recent_paper_bets"][0]["final_score"] == "2-1"
    assert api_response.json()["recent_paper_bets"][0]["trade_state"] == "WON"

    asyncio.run(services.shutdown())


def test_market_runtime_state_checkpoints_decisions_before_late_sync_failure(tmp_path: Path) -> None:
    fixture = _fixture()
    config = replace(
        _dashboard_config(tmp_path),
        current_odds_connected=True,
        current_xg_connected=True,
        enable_odds_sync=True,
    )
    def xg_factory(row, prediction, snapshots):
        expected_home = float(prediction["expected_home_goals"])
        expected_away = float(prediction["expected_away_goals"])

        def _row(role, recent):
            return {
                "fixture_id": fixture.fixture_id,
                "team_role": role,
                "recent_xg_for_per_match": recent,
                "matches_in_window": 5,
                "as_of": "2026-08-28T20:00:00+00:00",
                "window_start_at": "2026-08-01T20:00:00+00:00",
                "window_end_at": "2026-08-28T20:00:00+00:00",
                "source": SOURCE_IDENTIFIER,
                "competition_id": "ENG1",
            }

        rows = {
            (fixture.fixture_id, "HOME"): _row("HOME", expected_home),
            (fixture.fixture_id, "AWAY"): _row("AWAY", expected_away),
        }
        inspection = build_xg_inspection(rows)
        return lambda: (rows, inspection)

    services = PostEvaluationFailureDashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(
            snapshots_by_fixture={
                fixture.fixture_id: [
                    _market_snapshot(
                        fixture=fixture,
                        bookmaker="Pinnacle",
                        market="1x2",
                        selection="HOME",
                        decimal_odds=2.05,
                        reference_designation="reference",
                        provider_market_id="pin-home",
                    ),
                    _market_snapshot(
                        fixture=fixture,
                        bookmaker="Pinnacle",
                        market="1x2",
                        selection="DRAW",
                        decimal_odds=3.35,
                        reference_designation="reference",
                        provider_market_id="pin-draw",
                    ),
                    _market_snapshot(
                        fixture=fixture,
                        bookmaker="Pinnacle",
                        market="1x2",
                        selection="AWAY",
                        decimal_odds=3.95,
                        reference_designation="reference",
                        provider_market_id="pin-away",
                    ),
                    _market_snapshot(
                        fixture=fixture,
                        bookmaker="bet365",
                        market="1x2",
                        selection="HOME",
                        decimal_odds=10.0,
                        reference_designation="available",
                        provider_market_id="b365-home",
                    ),
                ]
            },
            configured=True,
        ),
        xg_profile_factory=xg_factory,
    )
    services._build_snapshot()

    with dashboard_storage.connect(config.dashboard_db_path) as dashboard_conn:
        dashboard_storage.ensure_schema(dashboard_conn)
        football_edge_desk.ensure_schema(dashboard_conn)
        dashboard_storage.upsert_fixtures(
            dashboard_conn,
            [fixture],
            updated_at="2026-08-29T00:00:00+00:00",
        )
        with football_paper_trading.connect(config.paper_trading_db_path) as paper_conn:
            football_paper_trading.ensure_schema(paper_conn)
        prediction = services.ensure_prediction(dashboard_conn, fixture)
        assert services._prediction_eligibility_status(prediction) == "PREDICTION_READY"

        fixture_row = fixture.to_dict(timezone_name="UTC")
        fixture_row["prediction"] = prediction

        with pytest.raises(RuntimeError, match="synthetic settlement crash"):
            services._sync_current_market_state([fixture_row], dashboard_conn=dashboard_conn)

        runtime_state = dashboard_storage.get_runtime_state(dashboard_conn, "market_runtime_state")

    assert runtime_state is not None
    assert runtime_state["state"] == "READY"
    assert runtime_state["fixtures_matched"] == 1
    assert runtime_state["market_snapshots"] == 4
    assert runtime_state["decision_count"] >= 1
    assert fixture.fixture_id in runtime_state["evaluations"]


def test_sync_current_market_state_does_not_run_schema_initialization_after_boundary_init(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = _fixture()
    config = replace(
        _dashboard_config(tmp_path),
        current_odds_connected=True,
        current_xg_connected=True,
        enable_odds_sync=True,
    )

    def xg_factory(row, prediction, snapshots):
        expected_home = float(prediction["expected_home_goals"])
        expected_away = float(prediction["expected_away_goals"])

        def _row(role, recent):
            return {
                "fixture_id": fixture.fixture_id,
                "team_role": role,
                "recent_xg_for_per_match": recent,
                "matches_in_window": 5,
                "as_of": "2026-08-28T20:00:00+00:00",
                "window_start_at": "2026-08-01T20:00:00+00:00",
                "window_end_at": "2026-08-28T20:00:00+00:00",
                "source": SOURCE_IDENTIFIER,
                "competition_id": "ENG1",
            }

        rows = {
            (fixture.fixture_id, "HOME"): _row("HOME", expected_home),
            (fixture.fixture_id, "AWAY"): _row("AWAY", expected_away),
        }
        inspection = build_xg_inspection(rows)
        return lambda: (rows, inspection)

    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(
            snapshots_by_fixture={
                fixture.fixture_id: [
                    _market_snapshot(
                        fixture=fixture,
                        bookmaker="Pinnacle",
                        market="1x2",
                        selection="HOME",
                        decimal_odds=2.05,
                        reference_designation="reference",
                        provider_market_id="pin-home",
                    ),
                    _market_snapshot(
                        fixture=fixture,
                        bookmaker="Pinnacle",
                        market="1x2",
                        selection="DRAW",
                        decimal_odds=3.35,
                        reference_designation="reference",
                        provider_market_id="pin-draw",
                    ),
                    _market_snapshot(
                        fixture=fixture,
                        bookmaker="Pinnacle",
                        market="1x2",
                        selection="AWAY",
                        decimal_odds=3.95,
                        reference_designation="reference",
                        provider_market_id="pin-away",
                    ),
                    _market_snapshot(
                        fixture=fixture,
                        bookmaker="bet365",
                        market="1x2",
                        selection="HOME",
                        decimal_odds=10.0,
                        reference_designation="available",
                        provider_market_id="b365-home",
                    ),
                ]
            },
            configured=True,
        ),
        xg_profile_factory=xg_factory,
    )
    services._build_snapshot()

    schema_calls = {"dashboard": 0, "paper": 0, "canonical": 0, "portfolio": 0, "edge_desk": 0}
    original_dashboard_ensure = dashboard_storage.ensure_schema
    original_edge_desk_ensure = football_edge_desk.ensure_schema
    original_paper_ensure = football_paper_trading.ensure_schema
    original_canonical_ensure = football_canonical_fixture.ensure_schema
    original_portfolio_ensure = football_portfolio_selection.ensure_schema

    def counted_dashboard_ensure(conn):
        schema_calls["dashboard"] += 1
        return original_dashboard_ensure(conn)

    def counted_edge_desk_ensure(conn):
        schema_calls["edge_desk"] += 1
        return original_edge_desk_ensure(conn)

    def counted_paper_ensure(conn):
        schema_calls["paper"] += 1
        return original_paper_ensure(conn)

    def counted_canonical_ensure(conn):
        schema_calls["canonical"] += 1
        return original_canonical_ensure(conn)

    def counted_portfolio_ensure(conn):
        schema_calls["portfolio"] += 1
        return original_portfolio_ensure(conn)

    with dashboard_storage.connect(config.dashboard_db_path) as dashboard_conn:
        dashboard_storage.ensure_schema(dashboard_conn)
        football_edge_desk.ensure_schema(dashboard_conn)
        dashboard_storage.upsert_fixtures(
            dashboard_conn,
            [fixture],
            updated_at="2026-08-29T00:00:00+00:00",
        )
        with football_paper_trading.connect(config.paper_trading_db_path) as paper_conn:
            football_paper_trading.ensure_schema(paper_conn)
            football_canonical_fixture.ensure_schema(paper_conn)
            football_portfolio_selection.ensure_schema(paper_conn)
        services._schema_initialized = True
        prediction = services.ensure_prediction(dashboard_conn, fixture)
        fixture_row = fixture.to_dict(timezone_name="UTC")
        fixture_row["prediction"] = prediction

        monkeypatch.setattr(dashboard_storage, "ensure_schema", counted_dashboard_ensure)
        monkeypatch.setattr(football_edge_desk, "ensure_schema", counted_edge_desk_ensure)
        monkeypatch.setattr(football_paper_trading, "ensure_schema", counted_paper_ensure)
        monkeypatch.setattr(football_canonical_fixture, "ensure_schema", counted_canonical_ensure)
        monkeypatch.setattr(football_portfolio_selection, "ensure_schema", counted_portfolio_ensure)
        services._sync_current_market_state([fixture_row], dashboard_conn=dashboard_conn)

    assert schema_calls == {"dashboard": 0, "paper": 0, "canonical": 0, "portfolio": 0, "edge_desk": 0}


def test_existing_prediction_shadow_write_does_not_reinitialize_edge_desk_after_boundary_init(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = _fixture()
    config = replace(_dashboard_config(tmp_path), current_xg_connected=True)
    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
        xg_profile_factory=_xg_factory_for_fixtures(fixture),
    )
    services._build_snapshot()

    schema_calls = {"edge_desk": 0}
    original_edge_desk_ensure = football_edge_desk.ensure_schema

    def counted_edge_desk_ensure(conn):
        schema_calls["edge_desk"] += 1
        return original_edge_desk_ensure(conn)

    with dashboard_storage.connect(config.dashboard_db_path) as dashboard_conn:
        dashboard_storage.ensure_schema(dashboard_conn)
        football_edge_desk.ensure_schema(dashboard_conn)
        dashboard_storage.upsert_fixtures(
            dashboard_conn,
            [fixture],
            updated_at="2026-08-29T00:00:00+00:00",
        )
        with football_paper_trading.connect(config.paper_trading_db_path) as paper_conn:
            football_paper_trading.ensure_schema(paper_conn)
            football_canonical_fixture.ensure_schema(paper_conn)
            football_portfolio_selection.ensure_schema(paper_conn)

        services._schema_initialized = True
        first_prediction = services.ensure_prediction(dashboard_conn, fixture)
        shadow_rows_before = dashboard_conn.execute(
            "SELECT COUNT(*) FROM edge_desk_agent_predictions WHERE fixture_id = ?",
            (fixture.fixture_id,),
        ).fetchone()[0]

        monkeypatch.setattr(football_edge_desk, "ensure_schema", counted_edge_desk_ensure)
        reused_prediction = services.ensure_prediction(dashboard_conn, fixture)
        shadow_rows_after = dashboard_conn.execute(
            "SELECT COUNT(*) FROM edge_desk_agent_predictions WHERE fixture_id = ?",
            (fixture.fixture_id,),
        ).fetchone()[0]

    assert reused_prediction["prediction_id"] == first_prediction["prediction_id"]
    assert shadow_rows_before > 0
    assert shadow_rows_after == shadow_rows_before
    assert schema_calls["edge_desk"] == 0


def test_dashboard_services_settles_finished_asian_handicap_trade_idempotently_and_reports_live_results(
    tmp_path: Path,
) -> None:
    kickoff_utc = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(microsecond=0).isoformat()
    pending_fixture = replace(
        _fixture(),
        kickoff_utc=kickoff_utc,
        status=FIXTURE_STATUS_PRE_MATCH,
    )
    config = replace(
        _dashboard_config(tmp_path),
        current_odds_connected=True,
        enable_odds_sync=True,
    )
    provider = FakeFixtureProvider([pending_fixture])
    services = DashboardServices(
        config=config,
        fixture_provider=provider,
        market_provider=FakeMarketProvider(configured=True),
    )

    asyncio.run(services.initialize())
    with football_paper_trading.connect(config.paper_trading_db_path) as conn:
        _record_test_paper_trade(
            conn,
            fixture=pending_fixture,
            prediction_id="pred_settle_ah_1",
            market="asian_handicap",
            selection="HOME",
            line=-0.5,
            entry_odds=2.2,
            model_fair_price=1.92,
        )

    asyncio.run(services.sync_once())

    provider.fixtures = [
        replace(
            pending_fixture,
            status=FIXTURE_STATUS_FINISHED,
            home_score=2,
            away_score=1,
            last_provider_update=(datetime.now(timezone.utc) - timedelta(minutes=2)).replace(microsecond=0).isoformat(),
        )
    ]

    asyncio.run(services.sync_once())
    asyncio.run(services.sync_once())

    with football_paper_trading.connect(config.paper_trading_db_path) as conn:
        paper_rows = football_paper_trading.fetch_paper_trade_rows(conn)
        updates = conn.execute(
            "SELECT trade_state, payload_json FROM paper_trade_updates ORDER BY paper_trade_update_id"
        ).fetchall()

    assert len(paper_rows) == 1
    assert paper_rows[0]["trade_state"] == "WON"
    assert paper_rows[0]["market"] == "asian_handicap"
    assert paper_rows[0]["line"] == pytest.approx(-0.5)
    assert paper_rows[0]["entry_odds"] == pytest.approx(2.2)
    assert len(updates) == 1
    assert updates[0]["trade_state"] == "WON"
    settlement_context = json.loads(str(updates[0]["payload_json"]))["settlement_context"]
    assert settlement_context["provider"] == "fake-provider"
    assert settlement_context["provider_fixture_id"] == "fixture-1"
    assert settlement_context["final_status"] == "FINISHED"
    assert settlement_context["actual_home_goals"] == 2
    assert settlement_context["actual_away_goals"] == 1

    performance_payload = services.get_performance_payload()
    assert performance_payload["current_paper_summary"]["settled_positions"] == 1
    assert performance_payload["current_paper_summary"]["wins"] == 1
    assert performance_payload["current_paper_summary"]["net_units"] == pytest.approx(1.2)
    assert performance_payload["current_paper_summary"]["roi"] == pytest.approx(1.2)
    assert performance_payload["current_paper_by_market"]["asian_handicap"]["wins"] == 1
    assert performance_payload["live_paper_market_rows"][0]["market"] == "asian_handicap"
    assert performance_payload["recent_paper_bets"][0]["bet_label"] == "Arsenal -0.5"
    assert performance_payload["recent_paper_bets"][0]["final_score"] == "2-1"
    assert performance_payload["recent_paper_bets"][0]["result_provider"] == "fake-provider"

    app = create_app(
        config=config,
        fixture_provider=provider,
        market_provider=FakeMarketProvider(configured=True),
    )
    with TestClient(app) as client:
        response = client.get("/performance")

    assert response.status_code == 200
    assert "Asian Handicap" in response.text
    assert "Arsenal -0.5" in response.text
    assert "WON" in response.text

    asyncio.run(services.shutdown())


def test_dashboard_services_settles_finished_corner_trade_with_authoritative_stats_idempotently(tmp_path: Path):
    kickoff_utc = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(microsecond=0).isoformat()
    finished_fixture = replace(
        _fixture(),
        kickoff_utc=kickoff_utc,
        status=FIXTURE_STATUS_FINISHED,
        home_score=2,
        away_score=1,
        last_provider_update=(datetime.now(timezone.utc) - timedelta(minutes=2)).replace(microsecond=0).isoformat(),
    )
    config = replace(
        _dashboard_config(tmp_path),
        current_odds_connected=True,
        enable_odds_sync=True,
    )
    statistics_provider = FakeStatisticsProvider(
        results_by_fixture={
            finished_fixture.fixture_id: AuthoritativeFixtureStatistics(
                fixture_id=finished_fixture.fixture_id,
                provider=API_FOOTBALL_PROVIDER,
                provider_fixture_id="9001",
                home_corners=6,
                away_corners=5,
                total_corners=11,
                source_timestamp="2026-08-28T11:58:00+00:00",
                captured_at="2026-08-28T12:00:00+00:00",
            )
        }
    )
    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([finished_fixture]),
        market_provider=FakeMarketProvider(configured=True),
        statistics_provider=statistics_provider,
    )

    asyncio.run(services.initialize())
    with football_paper_trading.connect(config.paper_trading_db_path) as conn:
        _record_test_paper_trade(
            conn,
            fixture=finished_fixture,
            prediction_id="pred_corner_1",
            market="corner_total",
            selection="OVER",
            line=9.5,
            entry_odds=1.95,
            model_fair_price=1.82,
        )

    asyncio.run(services.sync_once())
    asyncio.run(services.sync_once())

    with football_paper_trading.connect(config.paper_trading_db_path) as conn:
        paper_rows = football_paper_trading.fetch_paper_trade_rows(conn)
        updates = conn.execute(
            "SELECT trade_state, payload_json FROM paper_trade_updates ORDER BY paper_trade_update_id"
        ).fetchall()
    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        fixture_result = dashboard_storage.fetch_fixture_result(conn, finished_fixture.fixture_id)

    assert statistics_provider.calls == [finished_fixture.fixture_id]
    assert fixture_result is not None
    assert fixture_result["statistics_provider"] == API_FOOTBALL_PROVIDER
    assert fixture_result["statistics_provider_fixture_id"] == "9001"
    assert fixture_result["home_corners"] == 6
    assert fixture_result["away_corners"] == 5
    assert fixture_result["total_corners"] == 11
    assert len(paper_rows) == 1
    assert paper_rows[0]["trade_state"] == "WON"
    assert paper_rows[0]["entry_odds"] == pytest.approx(1.95)
    assert len(updates) == 1
    settlement_context = json.loads(str(updates[0]["payload_json"]))["settlement_context"]
    assert settlement_context["statistics_provider"] == API_FOOTBALL_PROVIDER
    assert settlement_context["statistics_provider_fixture_id"] == "9001"
    assert settlement_context["actual_home_corners"] == 6
    assert settlement_context["actual_away_corners"] == 5
    assert settlement_context["actual_total_corners"] == 11

    performance_payload = services.get_performance_payload()
    assert performance_payload["current_paper_summary"]["settled_positions"] == 1
    assert performance_payload["current_paper_summary"]["wins"] == 1
    assert performance_payload["current_paper_summary"]["net_units"] == pytest.approx(0.95)
    assert performance_payload["current_paper_summary"]["roi"] == pytest.approx(0.95)
    assert performance_payload["current_paper_by_market"]["corner_total"]["wins"] == 1
    assert performance_payload["live_paper_market_rows"][0]["market"] == "corner_total"
    assert performance_payload["recent_paper_bets"][0]["result_summary"] == "6-5 (Total 11)"
    assert performance_payload["recent_paper_bets"][0]["result_provider"] == API_FOOTBALL_PROVIDER

    asyncio.run(services.shutdown())


def test_dashboard_services_missing_corner_stats_remains_unsettled_and_score_markets_still_settle(tmp_path: Path):
    kickoff_utc = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(microsecond=0).isoformat()
    finished_fixture = replace(
        _fixture(),
        kickoff_utc=kickoff_utc,
        status=FIXTURE_STATUS_FINISHED,
        home_score=2,
        away_score=1,
        last_provider_update=(datetime.now(timezone.utc) - timedelta(minutes=2)).replace(microsecond=0).isoformat(),
    )
    config = replace(
        _dashboard_config(tmp_path),
        current_odds_connected=True,
        enable_odds_sync=True,
    )
    statistics_provider = FakeStatisticsProvider(
        results_by_fixture={
            finished_fixture.fixture_id: AuthoritativeFixtureStatistics(
                fixture_id=finished_fixture.fixture_id,
                provider=API_FOOTBALL_PROVIDER,
                provider_fixture_id="9001",
                home_corners=None,
                away_corners=5,
                total_corners=None,
                source_timestamp="2026-08-28T11:58:00+00:00",
                captured_at="2026-08-28T12:00:00+00:00",
            )
        }
    )
    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([finished_fixture]),
        market_provider=FakeMarketProvider(configured=True),
        statistics_provider=statistics_provider,
    )

    asyncio.run(services.initialize())
    with football_paper_trading.connect(config.paper_trading_db_path) as conn:
        _record_test_paper_trade(
            conn,
            fixture=finished_fixture,
            prediction_id="pred_corner_missing",
            market="corner_total",
            selection="OVER",
            line=9.5,
            entry_odds=1.95,
            model_fair_price=1.82,
        )
        _record_test_paper_trade(
            conn,
            fixture=finished_fixture,
            prediction_id="pred_score_1",
            market="1x2",
            selection="HOME",
            entry_odds=2.20,
            model_fair_price=1.92,
        )

    asyncio.run(services.sync_once())
    asyncio.run(services.sync_once())

    with football_paper_trading.connect(config.paper_trading_db_path) as conn:
        paper_rows = football_paper_trading.fetch_paper_trade_rows(conn)
        updates = conn.execute(
            "SELECT trade_state, payload_json FROM paper_trade_updates ORDER BY paper_trade_update_id"
        ).fetchall()

    rows_by_market = {str(row["market"]): row for row in paper_rows}
    assert rows_by_market["corner_total"]["trade_state"] == "UNSETTLED"
    assert rows_by_market["corner_total"]["entry_odds"] == pytest.approx(1.95)
    assert rows_by_market["1x2"]["trade_state"] == "WON"
    assert rows_by_market["1x2"]["entry_odds"] == pytest.approx(2.20)
    assert [row["trade_state"] for row in updates] == ["UNSETTLED", "WON"]

    performance_payload = services.get_performance_payload()
    assert performance_payload["current_paper_summary"]["settled_positions"] == 1
    assert performance_payload["current_paper_summary"]["pending_positions"] == 1
    assert performance_payload["current_paper_by_market"]["corner_total"]["pending_positions"] == 1
    assert performance_payload["current_paper_by_market"]["1x2"]["wins"] == 1

    asyncio.run(services.shutdown())


def test_dashboard_services_unfinished_corner_fixture_does_not_request_stats(tmp_path: Path):
    kickoff_utc = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat()
    pending_fixture = replace(
        _fixture(),
        kickoff_utc=kickoff_utc,
        status=FIXTURE_STATUS_PRE_MATCH,
    )
    config = replace(
        _dashboard_config(tmp_path),
        current_odds_connected=True,
        enable_odds_sync=True,
    )
    statistics_provider = FakeStatisticsProvider(results_by_fixture={})
    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([pending_fixture]),
        market_provider=FakeMarketProvider(configured=True),
        statistics_provider=statistics_provider,
    )

    asyncio.run(services.initialize())
    with football_paper_trading.connect(config.paper_trading_db_path) as conn:
        _record_test_paper_trade(
            conn,
            fixture=pending_fixture,
            prediction_id="pred_pending_corner",
            market="corner_total",
            selection="OVER",
            line=9.5,
            entry_odds=1.95,
            model_fair_price=1.82,
        )

    asyncio.run(services.sync_once())

    with football_paper_trading.connect(config.paper_trading_db_path) as conn:
        paper_rows = football_paper_trading.fetch_paper_trade_rows(conn)

    assert statistics_provider.calls == []
    assert paper_rows[0]["trade_state"] == "PENDING"

    asyncio.run(services.shutdown())


def test_dashboard_services_corner_stats_provider_failure_does_not_break_runtime(tmp_path: Path):
    kickoff_utc = (datetime.now(timezone.utc) - timedelta(hours=3)).replace(microsecond=0).isoformat()
    finished_fixture = replace(
        _fixture(),
        kickoff_utc=kickoff_utc,
        status=FIXTURE_STATUS_FINISHED,
        home_score=2,
        away_score=1,
        last_provider_update=(datetime.now(timezone.utc) - timedelta(minutes=2)).replace(microsecond=0).isoformat(),
    )
    config = replace(
        _dashboard_config(tmp_path),
        current_odds_connected=True,
        enable_odds_sync=True,
    )
    statistics_provider = FakeStatisticsProvider(
        results_by_fixture={},
        errors_by_fixture={finished_fixture.fixture_id: FixtureProviderError("rate limited")},
    )
    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([finished_fixture]),
        market_provider=FakeMarketProvider(configured=True),
        statistics_provider=statistics_provider,
    )

    asyncio.run(services.initialize())
    with football_paper_trading.connect(config.paper_trading_db_path) as conn:
        _record_test_paper_trade(
            conn,
            fixture=finished_fixture,
            prediction_id="pred_corner_error",
            market="corner_total",
            selection="OVER",
            line=9.5,
            entry_odds=1.95,
            model_fair_price=1.82,
        )
        _record_test_paper_trade(
            conn,
            fixture=finished_fixture,
            prediction_id="pred_score_error",
            market="1x2",
            selection="HOME",
            entry_odds=2.20,
            model_fair_price=1.92,
        )

    asyncio.run(services.sync_once())

    performance_payload = services.get_performance_payload()
    system_status = services.get_system_status()
    rows_by_market = {str(row["market"]): row for row in performance_payload["paper_positions"]}

    assert rows_by_market["corner_total"]["trade_state"] == "UNSETTLED"
    assert rows_by_market["1x2"]["trade_state"] == "WON"
    assert system_status["storage"]["corner_result_sync"]["state"] == "DEGRADED"
    assert system_status["storage"]["corner_result_sync"]["query_count"] == 1

    asyncio.run(services.shutdown())


def test_best_pick_market_labels_are_human_readable() -> None:
    assert _humanize_market_selection(
        market_family="1x2",
        selection="HOME",
        line=None,
        home_team_name="Arsenal",
        away_team_name="Chelsea",
    )["bet_label"] == "Arsenal to Win"
    assert _humanize_market_selection(
        market_family="btts",
        selection="YES",
        line=None,
        home_team_name="Arsenal",
        away_team_name="Chelsea",
    )["bet_label"] == "Both Teams to Score - Yes"
    assert _humanize_market_selection(
        market_family="goals_total",
        selection="OVER",
        line=2.5,
        home_team_name="Arsenal",
        away_team_name="Chelsea",
    )["bet_label"] == "Over 2.5 Goals"
    assert _humanize_market_selection(
        market_family="corner_total",
        selection="OVER",
        line=9.5,
        home_team_name="Arsenal",
        away_team_name="Chelsea",
    )["bet_label"] == "Over 9.5 Corners"
    assert _humanize_market_selection(
        market_family="asian_handicap",
        selection="HOME",
        line=-0.5,
        home_team_name="Arsenal",
        away_team_name="Chelsea",
    )["bet_label"] == "Arsenal -0.5"
    assert _humanize_market_selection(
        market_family="asian_handicap",
        selection="AWAY",
        line=-0.5,
        home_team_name="Arsenal",
        away_team_name="Chelsea",
    )["bet_label"] == "Chelsea +0.5"
    assert _humanize_market_selection(
        market_family="asian_handicap",
        selection="AWAY",
        line=0.5,
        home_team_name="Arsenal",
        away_team_name="Chelsea",
    )["bet_label"] == "Chelsea -0.5"


def test_odds_runtime_requirements_include_corner_total_and_asian_handicap(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SPORTS_AI_THE_ODDS_API_KEY", "test-key")
    config = replace(_dashboard_config(tmp_path), current_odds_connected=True)
    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([]),
        market_provider=_default_market_provider(config),
    )

    requirements = services._odds_feed_runtime_requirements()
    assert "corner_total" in requirements["supported_markets"]
    assert "asian_handicap" in requirements["supported_markets"]


def test_dashboard_services_best_picks_use_qualified_current_truth(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    qualified_fixture = _fixture(fixture_id="fake:arsenal_chelsea")
    config = replace(
        _dashboard_config(tmp_path),
        current_odds_connected=True,
        current_xg_connected=True,
        enable_odds_sync=True,
    )
    market_provider = FakeMarketProvider()
    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([qualified_fixture]),
        market_provider=market_provider,
        xg_profile_factory=_xg_factory_for_fixtures(qualified_fixture),
    )

    asyncio.run(services.initialize())
    market_provider.snapshots_by_fixture[qualified_fixture.fixture_id] = [
        _market_snapshot(
            fixture=qualified_fixture,
            bookmaker="Pinnacle",
            market="1x2",
            selection="HOME",
            decimal_odds=2.05,
            reference_designation="reference",
            provider_market_id="pin-home-1",
        ),
        _market_snapshot(
            fixture=qualified_fixture,
            bookmaker="Pinnacle",
            market="1x2",
            selection="DRAW",
            decimal_odds=3.35,
            reference_designation="reference",
            provider_market_id="pin-draw-1",
        ),
        _market_snapshot(
            fixture=qualified_fixture,
            bookmaker="Pinnacle",
            market="1x2",
            selection="AWAY",
            decimal_odds=3.95,
            reference_designation="reference",
            provider_market_id="pin-away-1",
        ),
        _market_snapshot(
            fixture=qualified_fixture,
            bookmaker="bet365",
            market="1x2",
            selection="HOME",
            decimal_odds=10.0,
            reference_designation="available",
            provider_market_id="b365-home-1",
        ),
    ]
    asyncio.run(services.sync_once())

    dashboard_payload = services.get_dashboard_payload()
    candidates_payload = services.get_candidates_payload()

    assert dashboard_payload["best_picks_summary"]["count"] == 1
    assert {row["fixture_id"] for row in dashboard_payload["best_picks"]} == {qualified_fixture.fixture_id}
    assert candidates_payload["preliminary_candidates"]

    candidate = candidates_payload["preliminary_candidates"][0]
    best_pick = dashboard_payload["best_picks"][0]
    assert best_pick["bet_label"] == "Arsenal to Win"
    assert best_pick["model_probability"] == candidate["model_probability"]
    assert best_pick["market_odds"] == candidate["best_available_price"]
    assert best_pick["fair_odds"] == candidate["model_fair_decimal_price"]
    assert best_pick["edge"] == candidate["settlement_aware_edge"]
    assert best_pick["paper_qualification_state"] == "QUALIFIED"

    asyncio.run(services.shutdown())


def test_dashboard_services_current_asian_handicap_candidate_creates_idempotent_paper_entry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "60")
    fixture = _fixture()
    config = replace(
        _dashboard_config(tmp_path),
        current_odds_connected=True,
        current_xg_connected=True,
        enable_odds_sync=True,
    )
    market_provider = FakeMarketProvider()
    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=market_provider,
        xg_profile_factory=_xg_factory_for_fixtures(fixture),
    )

    asyncio.run(services.initialize())
    market_provider.snapshots_by_fixture[fixture.fixture_id] = [
        _market_snapshot(
            fixture=fixture,
            bookmaker="Pinnacle",
            market="asian_handicap",
            selection="HOME",
            line=-0.5,
            decimal_odds=1.95,
            reference_designation="reference",
            provider_market_id="pin-home-ah",
        ),
        _market_snapshot(
            fixture=fixture,
            bookmaker="Pinnacle",
            market="asian_handicap",
            selection="AWAY",
            line=-0.5,
            decimal_odds=1.91,
            reference_designation="reference",
            provider_market_id="pin-away-ah",
        ),
        _market_snapshot(
            fixture=fixture,
            bookmaker="bet365",
            market="asian_handicap",
            selection="HOME",
            line=-0.5,
            decimal_odds=10.0,
            reference_designation="available",
            provider_market_id="b365-home-ah",
        ),
        _market_snapshot(
            fixture=fixture,
            bookmaker="bet365",
            market="asian_handicap",
            selection="AWAY",
            line=-0.5,
            decimal_odds=1.80,
            reference_designation="available",
            provider_market_id="b365-away-ah",
        ),
    ]

    asyncio.run(services.sync_once())

    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["market_evaluation"]["state"] == "READY"
    assert payload["market_evaluation"]["covered_markets"] == ["asian_handicap"]

    candidates_payload = services.get_candidates_payload()
    ah_candidates = [
        row
        for row in candidates_payload["preliminary_candidates"]
        if row["market_family"] == "asian_handicap"
    ]
    assert len(ah_candidates) == 1
    assert ah_candidates[0]["selection"] == "HOME"
    assert ah_candidates[0]["line"] == pytest.approx(-0.5)
    assert ah_candidates[0]["paper_entry_eligible"] is True

    performance_payload = services.get_performance_payload()
    assert performance_payload["current_paper_summary"]["positions"] == 1
    assert performance_payload["current_paper_summary"]["open_positions"] == 1
    assert performance_payload["open_paper_positions"][0]["market"] == "asian_handicap"
    assert performance_payload["open_paper_positions"][0]["bet_label"] == "Arsenal -0.5"

    with football_paper_trading.connect(config.paper_trading_db_path) as conn:
        paper_rows = football_paper_trading.fetch_paper_trade_rows(conn)
        assert len(paper_rows) == 1
        assert paper_rows[0]["market"] == "asian_handicap"
        assert paper_rows[0]["selection"] == "HOME"
        assert paper_rows[0]["line"] == pytest.approx(-0.5)
        assert paper_rows[0]["entry_snapshot_id"] is not None
        first_trade_id = str(paper_rows[0]["paper_trade_id"])

    asyncio.run(services.sync_once())

    with football_paper_trading.connect(config.paper_trading_db_path) as conn:
        paper_rows = football_paper_trading.fetch_paper_trade_rows(conn)
        assert len(paper_rows) == 1
        assert str(paper_rows[0]["paper_trade_id"]) == first_trade_id

    asyncio.run(services.shutdown())


def test_dashboard_services_today_working_rows_keep_bet_and_no_bet_truth(
    tmp_path: Path,
    monkeypatch,
) -> None:
    kickoff_one = (datetime.now(timezone.utc) + timedelta(hours=2)).replace(minute=0, second=0, microsecond=0).isoformat()
    kickoff_two = (datetime.now(timezone.utc) + timedelta(hours=5)).replace(minute=0, second=0, microsecond=0).isoformat()
    bet_fixture = replace(_fixture(fixture_id="fake:bet_row"), kickoff_utc=kickoff_one)
    no_bet_fixture = replace(
        _fixture(
            fixture_id="fake:no_bet_row",
            home_team_name="Sevilla",
            away_team_name="Betis",
        ),
        kickoff_utc=kickoff_two,
    )
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([bet_fixture, no_bet_fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )
    fixture_rows = [
        bet_fixture.to_dict(timezone_name="UTC"),
        no_bet_fixture.to_dict(timezone_name="UTC"),
    ]

    monkeypatch.setattr(
        services,
        "_filter_fixture_rows",
        lambda *, bucket, fixture_rows: fixture_rows if bucket == "today" else [],
    )
    monkeypatch.setattr(
        services,
        "_market_evaluation_map",
        lambda: {
            "bet": {
                "decision_rows": [
                    {
                        "fixture_id": bet_fixture.fixture_id,
                        "decision": "PRELIMINARY_CANDIDATE",
                        "market_family": "1x2",
                        "selection": "HOME",
                        "line": None,
                        "model_probability": 0.63,
                        "best_available_price": 2.05,
                        "settlement_aware_edge": 0.042,
                        "paper_entry_eligible": True,
                        "market_freshness": "FRESH",
                        "day12_state": "PASS",
                        "reason_codes": [],
                    },
                    {
                        "fixture_id": no_bet_fixture.fixture_id,
                        "market_family": "1x2",
                        "selection": "HOME",
                        "line": None,
                        "model_probability": 0.71,
                        "best_available_price": 1.30,
                        "settlement_aware_edge": -0.059,
                        "paper_entry_eligible": False,
                        "market_freshness": "FRESH",
                        "day12_state": "PASS",
                        "decision": "NO_BET",
                        "reason_codes": ["PRICE_NO_VALUE"],
                    },
                ],
            }
        },
    )

    rows = services._build_today_working_rows(fixture_rows)

    assert [row["desk_label"] for row in rows] == ["BET", "NO BET"]
    assert rows[0]["is_actionable"] is True
    assert rows[0]["is_featured"] is True
    assert rows[0]["bet_label"] == "Arsenal to Win"
    assert rows[0]["market_odds"] == 2.05
    assert math.isclose(rows[0]["implied_probability"], 1 / 2.05, rel_tol=0.0, abs_tol=1e-9)
    assert rows[1]["is_actionable"] is False
    assert rows[1]["desk_note"] == "Price No Value"
    assert rows[1]["bet_label"] == "Sevilla to Win"

def test_dashboard_payload_current_slate_counts_use_persisted_paper_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    kickoff_one = "2026-08-29T10:00:00+00:00"
    kickoff_two = "2026-08-30T10:00:00+00:00"
    first_fixture = replace(_fixture(fixture_id="fake:slate_one"), kickoff_utc=kickoff_one)
    second_fixture = replace(
        _fixture(
            fixture_id="fake:slate_two",
            home_team_name="Sevilla",
            away_team_name="Betis",
        ),
        kickoff_utc=kickoff_two,
    )
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([first_fixture, second_fixture]),
        market_provider=FakeMarketProvider(configured=True),
    )
    fixture_rows = [
        first_fixture.to_dict(timezone_name="UTC"),
        second_fixture.to_dict(timezone_name="UTC"),
    ]
    first_bet_row = {
        "fixture_id": first_fixture.fixture_id,
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "decision": "PRELIMINARY_CANDIDATE",
        "current_decision_state": "BET",
        "market_family": "1x2",
        "selection": "HOME",
        "line": None,
        "model_probability": 0.63,
        "model_fair_decimal_price": 1.59,
        "best_available_price": 2.05,
        "best_available_bookmaker": "bet365",
        "best_available_source_timestamp": "2026-08-28T08:28:07Z",
        "settlement_aware_edge": 0.2915,
        "paper_entry_eligible": True,
        "paper_trade_id": "paper_current_1",
        "market_freshness": "FRESH",
        "day12_state": "PASSED",
        "reason_codes": [],
    }
    second_bet_row = {
        "fixture_id": second_fixture.fixture_id,
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "decision": "PRELIMINARY_CANDIDATE",
        "current_decision_state": "BET",
        "market_family": "goals_total",
        "selection": "OVER",
        "line": 2.5,
        "model_probability": 0.58,
        "model_fair_decimal_price": 1.72,
        "best_available_price": 1.95,
        "best_available_bookmaker": "matchbook",
        "best_available_source_timestamp": "2026-08-28T08:28:49Z",
        "settlement_aware_edge": 0.1311,
        "paper_entry_eligible": True,
        "paper_trade_id": "paper_current_2",
        "market_freshness": "FRESH",
        "day12_state": "PASSED",
        "reason_codes": [],
    }
    second_no_bet_row = {
        "fixture_id": second_fixture.fixture_id,
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "decision": "NO_BET",
        "current_decision_state": "NO_BET_VALUE",
        "market_family": "btts",
        "selection": "YES",
        "line": None,
        "model_probability": 0.54,
        "model_fair_decimal_price": 1.85,
        "best_available_price": 1.72,
        "best_available_bookmaker": "bet365",
        "best_available_source_timestamp": "2026-08-28T08:28:07Z",
        "settlement_aware_edge": -0.0712,
        "paper_entry_eligible": False,
        "market_freshness": "FRESH",
        "day12_state": "PASSED",
        "reason_codes": ["PRICE_NO_VALUE"],
    }
    paper_rows = [
        {
            "paper_trade_id": "paper_current_1",
            "fixture_id": first_fixture.fixture_id,
            "market": "1x2",
            "selection": "HOME",
            "kickoff": kickoff_one,
            "decision_timestamp": "2026-08-29T08:45:00+00:00",
            "trade_state": "PENDING",
            "entry_odds": 2.05,
        },
        {
            "paper_trade_id": "paper_current_2",
            "fixture_id": second_fixture.fixture_id,
            "market": "goals_total",
            "selection": "OVER",
            "line": 2.5,
            "kickoff": kickoff_two,
            "decision_timestamp": "2026-08-30T08:45:00+00:00",
            "trade_state": "OPEN",
            "entry_odds": 1.95,
        },
        {
            "paper_trade_id": "paper_old",
            "market": "1x2",
            "decision_timestamp": "2026-08-27T08:45:00+00:00",
            "trade_state": "OPEN",
            "entry_odds": 2.20,
        },
        ]

    monkeypatch.setattr(services, "_current_local_date", lambda: date(2026, 8, 29))
    monkeypatch.setattr(services, "_all_fixtures_with_predictions", lambda: fixture_rows)
    monkeypatch.setattr(services, "_future_fixture_rows", lambda rows: fixture_rows)
    monkeypatch.setattr(
        services,
        "_filter_fixture_rows",
        lambda *, bucket, fixture_rows: [] if bucket in {"today", "live", "upcoming"} else [],
    )
    monkeypatch.setattr(
        services,
        "_market_evaluation_map",
        lambda: {
            first_fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [first_bet_row],
                "display_rows": [first_bet_row],
            },
            second_fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [second_bet_row, second_no_bet_row],
                "display_rows": [second_bet_row, second_no_bet_row],
            },
        },
    )
    monkeypatch.setattr(
        services,
        "_paper_positions",
        lambda: (paper_rows, football_paper_trading.paper_trading_status(paper_rows)),
    )

    payload = services.get_dashboard_payload()
    metrics = {item["label"]: item for item in payload["glance_metrics"]}
    funnel = {item["label"]: item for item in payload["desk_funnel"]}

    assert payload["best_picks_summary"]["count"] == 2
    assert len(payload["today_published_rows"]) == 1
    assert payload["current_slate_summary"]["fixture_count"] == 2
    assert payload["current_slate_summary"]["count"] == 3
    assert payload["current_slate_summary"]["bet_count"] == 2
    assert payload["current_slate_summary"]["no_bet_count"] == 1
    assert payload["current_slate_summary"]["paper_entry_count"] == 2
    assert payload["current_slate_summary"]["pending_count"] == 2
    assert payload["today_summary"]["suggested_bets"] == 1
    assert payload["system_status"]["current_fixture_state"]["today_count"] == 0
    assert metrics["Analysed"] == {"label": "Analysed", "value": "0", "note": "current desk · unique today fixtures"}
    assert metrics["Predictions"] == {"label": "Predictions", "value": "0", "note": "current desk rows for today"}
    assert metrics["BET"] == {"label": "BET", "value": "0", "note": "current desk · passed decision gates"}
    assert metrics["NO BET"] == {"label": "NO BET", "value": "0", "note": "current desk · retained rejections"}
    assert metrics["Tracked"] == {"label": "Tracked", "value": "1", "note": "canonical paper positions"}
    assert metrics["Open"] == {"label": "Open", "value": "1", "note": "unsettled paper positions"}
    assert metrics["Settled"] == {"label": "Settled", "value": "0", "note": "terminal paper positions"}
    assert metrics["Won"] == {"label": "Won", "value": "0", "note": "full wins"}
    assert metrics["Lost"] == {"label": "Lost", "value": "0", "note": "full losses"}
    assert "Push" not in metrics
    assert metrics["P/L"] == {"label": "P/L", "value": "+0.00", "note": "net paper units"}
    assert metrics["ROI"] == {"label": "ROI", "value": "NOT AVAILABLE", "note": "settled stake only"}
    assert metrics["Strike Rate"] == {"label": "Strike Rate", "value": "NOT AVAILABLE", "note": "pushes excluded"}
    assert funnel["Fixtures Analysed"]["value"] == "0"
    assert funnel["Decision Rows"]["value"] == "0"
    assert funnel["BET"]["value"] == "0"
    assert funnel["NO BET"]["value"] == "0"
    assert funnel["NO BET"]["note"] == "stale 0 · missing reference 0 · no value 0 · safeguard 0"
    assert payload["blocking_reasons"] == {
        "Stale market": 0,
        "Missing reference": 0,
        "No value": 0,
        "Safeguard": 0,
    }


def test_dashboard_home_published_pick_uses_frozen_entry_truth_and_no_bet_has_no_paper_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = _fixture(home_team_name="Aston Villa", away_team_name="Arsenal")
    no_bet_fixture = replace(_fixture(fixture_id="fake:no_bet", home_team_name="Sevilla", away_team_name="Betis"))
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture, no_bet_fixture]),
        market_provider=FakeMarketProvider(configured=True),
    )
    fixture_rows = [
        fixture.to_dict(timezone_name="UTC"),
        no_bet_fixture.to_dict(timezone_name="UTC"),
    ]
    published_bet_row = {
        "fixture_id": fixture.fixture_id,
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "decision": "PRELIMINARY_CANDIDATE",
        "current_decision_state": "BET",
        "market_family": "1x2",
        "selection": "HOME",
        "line": None,
        "model_probability": 0.4012,
        "model_fair_decimal_price": 2.49,
        "best_available_price": 6.8,
        "best_available_bookmaker": "bet365",
        "best_available_source_timestamp": "2026-08-28T09:10:00Z",
        "settlement_aware_edge": 1.1466,
        "paper_entry_eligible": True,
        "paper_trade_id": "paper_published_1",
        "market_freshness": "FRESH",
        "day12_state": "PASSED",
        "reason_codes": [],
    }
    no_bet_row = {
        "fixture_id": no_bet_fixture.fixture_id,
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "decision": "NO_BET",
        "current_decision_state": "NO_BET_VALUE",
        "market_family": "1x2",
        "selection": "HOME",
        "line": None,
        "model_probability": 0.55,
        "model_fair_decimal_price": 1.82,
        "best_available_price": 1.61,
        "best_available_bookmaker": "Pinnacle",
        "best_available_source_timestamp": "2026-08-28T09:10:00Z",
        "settlement_aware_edge": -0.114,
        "paper_entry_eligible": False,
        "market_freshness": "FRESH",
        "day12_state": "PASSED",
        "reason_codes": ["PRICE_NO_VALUE"],
    }
    paper_rows = [
        {
            "paper_trade_id": "paper_published_1",
            "fixture_id": fixture.fixture_id,
            "market": "1x2",
            "selection": "HOME",
            "line": None,
            "model_probability": 0.315679,
            "model_fair_price": 3.1678,
            "entry_odds": 7.0,
            "edge": 1.2098,
            "bookmaker": "Pinnacle",
            "entry_captured_at": "2026-08-28T08:45:00Z",
            "decision_timestamp": "2026-08-28T08:45:00Z",
            "trade_state": "PENDING",
            "latest_update_payload": None,
        }
    ]

    monkeypatch.setattr(services, "_current_local_date", lambda: date(2026, 8, 28))
    monkeypatch.setattr(services, "_all_fixtures_with_predictions", lambda: fixture_rows)
    monkeypatch.setattr(services, "_future_fixture_rows", lambda rows: fixture_rows)
    monkeypatch.setattr(
        services,
        "_filter_fixture_rows",
        lambda *, bucket, fixture_rows: fixture_rows if bucket == "today" else [],
    )
    monkeypatch.setattr(
        services,
        "_market_evaluation_map",
        lambda: {
            fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [published_bet_row],
                "display_rows": [published_bet_row],
            },
            no_bet_fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [no_bet_row],
                "display_rows": [no_bet_row],
            },
        },
    )
    monkeypatch.setattr(
        services,
        "_paper_positions",
        lambda: (paper_rows, football_paper_trading.paper_trading_status(paper_rows)),
    )

    payload = services.get_dashboard_payload()
    published_pick = payload["best_picks"][0]

    assert published_pick["entry_odds"] == pytest.approx(7.0)
    assert published_pick["market_odds"] == pytest.approx(7.0)
    assert published_pick["current_market_odds"] == pytest.approx(6.8)
    assert published_pick["entry_edge"] == pytest.approx(1.2098)
    assert published_pick["edge"] == pytest.approx(1.2098)
    assert published_pick["current_market_edge"] == pytest.approx(1.1466)
    assert published_pick["entry_model_probability"] == pytest.approx(0.315679)
    assert published_pick["model_probability"] == pytest.approx(0.315679)
    assert published_pick["entry_fair_odds"] == pytest.approx(3.1678)
    assert published_pick["fair_odds"] == pytest.approx(3.1678)
    assert published_pick["paper_trade_state"] == "PENDING"
    assert payload["other_decision_rows"][0]["desk_label"] == "NO BET"
    no_bet_today_row = next(row for row in payload["today_desk_rows"] if row["desk_label"] == "NO BET")
    assert no_bet_today_row["status_label"] != "PENDING"

    app = create_app(services=services)
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Best Valid Bets" in response.text
    assert "7.00" in response.text
    assert "NO BET" in response.text


def test_dashboard_payload_surfaces_mixed_freshness_and_low_sample_warning(
    tmp_path: Path,
    monkeypatch,
) -> None:
    kickoff = "2026-08-29T10:00:00+00:00"
    fixture = replace(_fixture(home_team_name="Aston Villa", away_team_name="Arsenal"), kickoff_utc=kickoff)
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=True),
    )
    fixture_row = fixture.to_dict(timezone_name="UTC")
    bet_row = {
        "fixture_id": fixture.fixture_id,
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "decision": "PRELIMINARY_CANDIDATE",
        "current_decision_state": "BET",
        "market_family": "1x2",
        "selection": "HOME",
        "line": None,
        "model_probability": 0.63,
        "model_fair_decimal_price": 1.59,
        "best_available_price": 2.05,
        "best_available_bookmaker": "bet365",
        "best_available_source_timestamp": "2026-08-29T08:28:07Z",
        "settlement_aware_edge": 0.2915,
        "paper_entry_eligible": True,
        "paper_trade_id": "paper_settled_1",
        "market_freshness": "FRESH",
        "day12_state": "PASSED",
        "reason_codes": [],
    }
    stale_row = {
        "fixture_id": fixture.fixture_id,
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "decision": "STALE_MARKET",
        "current_decision_state": "NO_BET_STALE_MARKET",
        "market_family": "btts",
        "selection": "YES",
        "line": None,
        "model_probability": 0.54,
        "model_fair_decimal_price": 1.85,
        "best_available_price": 1.72,
        "best_available_bookmaker": "bet365",
        "best_available_source_timestamp": "2026-08-29T05:10:00Z",
        "settlement_aware_edge": -0.0712,
        "paper_entry_eligible": False,
        "market_freshness": "STALE",
        "day12_state": "PASSED",
        "reason_codes": [],
    }
    paper_rows = [
        {
            "paper_trade_id": "paper_settled_1",
            "fixture_id": fixture.fixture_id,
            "market": "1x2",
            "selection": "HOME",
            "line": None,
            "model_probability": 0.315679,
            "model_fair_price": 3.1678,
            "entry_odds": 2.05,
            "edge": 0.2915,
            "bookmaker": "bet365",
            "kickoff": kickoff,
            "entry_captured_at": "2026-08-29T08:00:00Z",
            "decision_timestamp": "2026-08-29T08:00:00Z",
            "trade_state": "LOST",
            "net_pnl": -1.0,
            "latest_update_payload": None,
        }
    ]

    monkeypatch.setattr(services, "_current_local_date", lambda: date(2026, 8, 29))
    monkeypatch.setattr(services, "_all_fixtures_with_predictions", lambda: [fixture_row])
    monkeypatch.setattr(services, "_future_fixture_rows", lambda rows: [fixture_row])
    monkeypatch.setattr(
        services,
        "_filter_fixture_rows",
        lambda *, bucket, fixture_rows: [fixture_row] if bucket == "today" else [],
    )
    monkeypatch.setattr(
        services,
        "_market_evaluation_map",
        lambda: {
            fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [bet_row, stale_row],
                "display_rows": [bet_row, stale_row],
            }
        },
    )
    monkeypatch.setattr(
        services,
        "_paper_positions",
        lambda: (paper_rows, football_paper_trading.paper_trading_status(paper_rows)),
    )
    services._market_runtime_state.update(
        {
            "state": "READY",
            "last_sync_at": "2026-08-29T10:15:00Z",
            "evaluations": {
                fixture.fixture_id: {
                    "decision_rows": [bet_row, stale_row],
                }
            },
        }
    )

    payload = services.get_dashboard_payload()
    health = {item["label"]: item for item in payload["system_health"]}

    assert payload["live_paper_sample"] == {
        "headline": "LOW SAMPLE",
        "sample_label": "1 SETTLED BET",
        "detail": "ROI and strike rate are directional only until at least 5 settled live paper bets exist.",
    }
    assert health["Odds Data Age"]["value"] == "MIXED FRESHNESS"
    assert "1 fresh, 1 stale" in health["Odds Data Age"]["note"]
    assert health["Last Odds Refresh"]["value"] == "2026-08-29T10:15:00Z"

    app = create_app(services=services)
    with TestClient(app) as client:
        dashboard_response = client.get("/")
        performance_response = client.get("/performance")

    assert dashboard_response.status_code == 200
    assert "LOW SAMPLE" in dashboard_response.text
    assert "1 SETTLED BET" in dashboard_response.text
    assert "MIXED FRESHNESS" in dashboard_response.text
    assert performance_response.status_code == 200
    assert "LOW SAMPLE" in performance_response.text
    assert "1 SETTLED BET" in performance_response.text


def test_dashboard_home_best_picks_empty_state_and_agent_truth(tmp_path: Path) -> None:
    fixture = _fixture()
    app = create_app(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Today" in response.text
    assert "Best Picks" in response.text
    assert "Production Funnel" in response.text
    assert "No genuine published paper picks are currently attached to today&#39;s board." in response.text
    assert "No current Route B desk rows are available for today&#39;s in-scope fixtures." in response.text
    assert "Route B Composite" in response.text
    assert "Route A Direct Classifier" in response.text
    assert "REJECTED" in response.text
    assert "Agent 3" in response.text
    assert "NOT IMPLEMENTED" in response.text
    assert "Agent 1" not in response.text
    assert "Agent 2" not in response.text


def test_dashboard_home_avoids_disconnected_market_copy_when_provider_is_connected(tmp_path: Path) -> None:
    fixture = _fixture()
    app = create_app(
        config=replace(
            _dashboard_config(tmp_path),
            current_odds_connected=True,
            enable_odds_sync=True,
        ),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=True),
    )

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "No current Route B desk rows are available for today&#39;s in-scope fixtures." in response.text
    assert "MARKET_DATA_NOT_CONNECTED" not in response.text


def test_dashboard_performance_empty_state_uses_neutral_paper_copy(tmp_path: Path) -> None:
    fixture = _fixture()
    app = create_app(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=True),
    )

    with TestClient(app) as client:
        response = client.get("/performance")

    assert response.status_code == 200
    assert "No settled paper bets yet." in response.text
    assert "No open paper positions right now." in response.text
    assert "current odds provider is still not configured" not in response.text


def test_match_detail_separates_current_market_from_published_entry_and_uses_current_gate_truth(
    tmp_path: Path,
    monkeypatch,
) -> None:
    fixture = _fixture(home_team_name="Aston Villa", away_team_name="Arsenal")
    config = replace(
        _dashboard_config(tmp_path),
        current_odds_connected=True,
        current_xg_connected=True,
        enable_odds_sync=True,
    )
    services = DashboardServices(
        config=config,
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=True),
    )

    asyncio.run(services.initialize())
    with dashboard_storage.connect(config.dashboard_db_path) as conn:
        prediction = dashboard_storage.fetch_latest_prediction_for_fixture(conn, fixture.fixture_id)
        assert prediction is not None
        prediction["safeguard_status"] = "BLOCKED"
        prediction["safeguard_reason"] = "POINT-IN-TIME XG NOT AVAILABLE"
        dashboard_storage.save_prediction(conn, prediction, updated_at="2026-08-28T09:12:00+00:00")

    bet_row = {
        "fixture_id": fixture.fixture_id,
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "decision": "PRELIMINARY_CANDIDATE",
        "current_decision_state": "BET",
        "market_family": "1x2",
        "selection": "HOME",
        "line": None,
        "model_probability": 0.4012,
        "model_fair_decimal_price": 2.49,
        "best_available_price": 6.8,
        "best_available_bookmaker": "bet365",
        "best_available_source_timestamp": "2026-08-28T09:10:00Z",
        "settlement_aware_edge": 1.1466,
        "minimum_acceptable_price": 2.41,
        "paper_entry_eligible": True,
        "paper_trade_id": "paper_detail_1",
        "market_freshness": "FRESH",
        "day12_state": "PASSED",
        "xg_status": "PASS",
        "reason_codes": [],
    }
    paper_rows = [
        {
            "paper_trade_id": "paper_detail_1",
            "fixture_id": fixture.fixture_id,
            "market": "1x2",
            "selection": "HOME",
            "line": None,
            "model_probability": 0.315679,
            "model_fair_price": 3.1678,
            "entry_odds": 7.0,
            "edge": 1.2098,
            "bookmaker": "Pinnacle",
            "entry_captured_at": "2026-08-28T08:45:00Z",
            "decision_timestamp": "2026-08-28T08:45:00Z",
            "trade_state": "PENDING",
            "latest_update_payload": None,
        }
    ]
    monkeypatch.setattr(
        services,
        "_market_evaluation_map",
        lambda: {
            fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [bet_row],
                "display_rows": [bet_row],
                "counts": {"preliminary_candidates": 1, "passed_safeguards": 1},
                "market_snapshot_count": 1,
                "freshness": "FRESH",
            }
        },
    )
    monkeypatch.setattr(
        services,
        "_paper_positions",
        lambda: (paper_rows, football_paper_trading.paper_trading_status(paper_rows)),
    )

    payload = services.get_fixture_detail_payload(fixture.fixture_id)
    assert payload is not None
    assert payload["decision_gates"]["current_market"] == "BET"
    assert payload["decision_gates"]["safeguard"] == "PASS"
    assert payload["decision_gates"]["safeguard_reason"] == "PASSED"
    assert payload["decision_gates"]["point_in_time_xg"] == "PASS"
    assert payload["paper_entries"][0]["entry_odds"] == pytest.approx(7.0)
    assert payload["paper_entries"][0]["current_market_odds"] == pytest.approx(6.8)
    assert payload["paper_entries"][0]["edge"] == pytest.approx(1.2098)
    assert payload["paper_entries"][0]["current_market_edge"] == pytest.approx(1.1466)

    app = create_app(services=services)
    with TestClient(app) as client:
        response = client.get(f"/matches/{fixture.fixture_id}")

    assert response.status_code == 200
    assert "Paper Entry" in response.text
    assert "Current-Market Evaluation" in response.text
    assert "Current odds 6.80" in response.text
    assert "Current EV +114.66%" in response.text
    assert "7.00" in response.text
    assert "+120.98%" in response.text
    assert "<span>Safeguard</span><strong>PASS</strong>" in response.text
    assert "<span>Point-In-Time xG</span><strong>PASS</strong>" in response.text
    assert "<span>Safeguard Reason</span><strong>PASSED</strong>" in response.text

    asyncio.run(services.shutdown())


def test_dashboard_home_today_matches_stay_on_strict_local_day_and_upcoming_is_link_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target_date = date(2026, 8, 30)
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([]),
        market_provider=FakeMarketProvider(configured=False),
    )
    today_live = replace(
        _fixture(
            fixture_id="fake:today_live",
            home_team_name="Today Live",
            away_team_name="Current Club",
            status=FIXTURE_STATUS_LIVE,
            home_score=1,
            away_score=0,
        ),
        kickoff_utc=_kickoff_utc_for_local_date(target_date, hour=13),
    )
    today_scheduled = replace(
        _fixture(
            fixture_id="fake:today_scheduled",
            home_team_name="Today Scheduled",
            away_team_name="Current Opponent",
        ),
        kickoff_utc=_kickoff_utc_for_local_date(target_date, hour=20),
    )
    previous_day_live = replace(
        _fixture(
            fixture_id="fake:previous_day_live",
            home_team_name="Previous Live",
            away_team_name="Old Club",
            status=FIXTURE_STATUS_LIVE,
            home_score=2,
            away_score=2,
        ),
        kickoff_utc=_kickoff_utc_for_local_date(target_date - timedelta(days=1), hour=23),
    )
    tomorrow_fixture = replace(
        _fixture(
            fixture_id="fake:tomorrow",
            home_team_name="Tomorrow Match",
            away_team_name="Future Club",
        ),
        kickoff_utc=_kickoff_utc_for_local_date(target_date + timedelta(days=1), hour=19),
    )
    today_live_row = today_live.to_dict(timezone_name="UTC")
    today_scheduled_row = today_scheduled.to_dict(timezone_name="UTC")
    previous_day_live_row = previous_day_live.to_dict(timezone_name="UTC")
    tomorrow_row = tomorrow_fixture.to_dict(timezone_name="UTC")

    monkeypatch.setattr(services, "_current_local_date", lambda: target_date)
    monkeypatch.setattr(
        services,
        "_all_fixtures_with_predictions",
        lambda: [today_live_row, today_scheduled_row, previous_day_live_row, tomorrow_row],
    )
    monkeypatch.setattr(services, "_future_fixture_rows", lambda rows: [tomorrow_row])
    monkeypatch.setattr(
        services,
        "_filter_fixture_rows",
        lambda *, bucket, fixture_rows: (
            [today_live_row, previous_day_live_row]
            if bucket == "live"
            else [today_scheduled_row]
            if bucket == "today"
            else [tomorrow_row]
            if bucket == "upcoming"
            else []
        ),
    )
    monkeypatch.setattr(services, "_market_evaluation_map", lambda: {})
    monkeypatch.setattr(
        services,
        "_paper_positions",
        lambda: ([], football_paper_trading.paper_trading_status([])),
    )

    app = create_app(services=services)
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Today Live vs Current Club" in response.text
    assert "Today Scheduled vs Current Opponent" in response.text
    assert "Previous Live vs Old Club" not in response.text
    assert "Tomorrow Match vs Future Club" not in response.text
    assert 'href="/matches?status_bucket=upcoming"' in response.text
    assert "View Upcoming Matches (1)" in response.text


def test_dashboard_home_places_best_picks_before_today_matches_and_current_desk(
    tmp_path: Path,
    monkeypatch,
) -> None:
    services, _, _ = _build_paginated_dashboard_services(tmp_path, monkeypatch, fixture_count=11)
    app = create_app(services=services)

    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.text.index('id="best-picks"') < response.text.index('id="today-matches"')
    assert response.text.index('id="today-matches"') < response.text.index('id="current-desk"')


def test_dashboard_payload_paginates_today_sections_and_clamps_out_of_range_pages(
    tmp_path: Path,
    monkeypatch,
) -> None:
    services, today_fixture_rows, _ = _build_paginated_dashboard_services(tmp_path, monkeypatch, fixture_count=11)

    payload = services.get_dashboard_payload(picks_page=2, matches_page=2, desk_page=4)
    visible_best_pick_ids = [row["fixture_id"] for row in payload["visible_best_picks"]]
    full_best_pick_ids = [row["fixture_id"] for row in payload["best_picks"][8:]]
    visible_match_ids = [row["fixture_id"] for row in payload["visible_today_matches"]]
    expected_match_ids = [str(row["fixture_id"]) for row in sorted(today_fixture_rows, key=lambda row: (str(row["kickoff_utc"]), str(row["fixture_id"])))[8:]]
    visible_desk_keys = [(row["fixture_id"], row["bet_label"]) for row in payload["visible_today_desk_rows"]]
    expected_desk_keys = [(row["fixture_id"], row["bet_label"]) for row in payload["today_desk_rows"][30:]]

    assert payload["best_picks_pagination"]["page"] == 2
    assert payload["best_picks_pagination"]["total_pages"] == 2
    assert visible_best_pick_ids == full_best_pick_ids
    assert payload["today_matches_pagination"]["page"] == 2
    assert payload["today_matches_pagination"]["total_pages"] == 2
    assert visible_match_ids == expected_match_ids
    assert payload["today_desk_pagination"]["page"] == 4
    assert payload["today_desk_pagination"]["total_pages"] == 4
    assert visible_desk_keys == expected_desk_keys

    clamped_payload = services.get_dashboard_payload(picks_page=99, matches_page=99, desk_page=99)
    assert clamped_payload["best_picks_pagination"]["page"] == clamped_payload["best_picks_pagination"]["total_pages"]
    assert clamped_payload["today_matches_pagination"]["page"] == clamped_payload["today_matches_pagination"]["total_pages"]
    assert clamped_payload["today_desk_pagination"]["page"] == clamped_payload["today_desk_pagination"]["total_pages"]


def test_matches_page_pagination_preserves_filters_and_resets_form_page(
    tmp_path: Path,
    monkeypatch,
) -> None:
    services, _, _ = _build_paginated_dashboard_services(tmp_path, monkeypatch, fixture_count=21)
    page_one_payload = services.get_matches_payload(competition="ENG1", status_bucket="today", page=1)
    page_two_payload = services.get_matches_payload(competition="ENG1", status_bucket="today", page=2)
    app = create_app(services=services)

    with TestClient(app) as client:
        response = client.get("/matches?competition=ENG1&status_bucket=today&page=2")

    assert response.status_code == 200
    assert "Page 2 of 2" in response.text
    assert 'href="/matches?competition=ENG1&amp;status_bucket=today"' in response.text
    assert 'name="page" value="1"' in response.text
    assert f"{page_two_payload['visible_client_match_cards'][0]['home_team_name']}" in response.text
    assert f"{page_two_payload['visible_client_match_cards'][0]['away_team_name']}" in response.text
    assert f"{page_one_payload['visible_client_match_cards'][0]['home_team_name']}" not in response.text


def test_betting_desk_and_no_bet_archive_paginate_without_provider_http_work(
    tmp_path: Path,
    monkeypatch,
) -> None:
    services, _, _ = _build_paginated_dashboard_services(tmp_path, monkeypatch, fixture_count=21)

    before = services.get_system_status()["provider_budget"]["metrics"]["provider_http_requests"]
    betting_payload = services.get_betting_desk_payload(page=2)
    archive_payload = services.get_no_bet_archive_payload(page=2)
    after = services.get_system_status()["provider_budget"]["metrics"]["provider_http_requests"]

    visible_betting_keys = [(row["fixture_id"], row["bet_label"]) for row in betting_payload["visible_rows"]]
    expected_betting_keys = [(row["fixture_id"], row["bet_label"]) for row in betting_payload["rows"][20:40]]
    visible_archive_keys = [(row["fixture_id"], row["bet_label"]) for row in archive_payload["visible_rows"]]
    expected_archive_keys = [(row["fixture_id"], row["bet_label"]) for row in archive_payload["rows"][20:40]]

    assert before == 0
    assert after == 0
    assert betting_payload["pagination"]["page"] == 2
    assert betting_payload["pagination"]["total_pages"] == 4
    assert visible_betting_keys == expected_betting_keys
    assert archive_payload["pagination"]["page"] == 2
    assert archive_payload["pagination"]["total_pages"] == 3
    assert visible_archive_keys == expected_archive_keys


def test_route_reads_do_not_create_provider_requests(
    tmp_path: Path,
    monkeypatch,
) -> None:
    services, _, _ = _build_paginated_dashboard_services(tmp_path, monkeypatch, fixture_count=21)
    app = create_app(services=services)

    before = services.get_system_status()["provider_budget"]["metrics"]["provider_http_requests"]
    with TestClient(app) as client:
        today_response = client.get("/today?matches_page=2")
        matches_response = client.get("/matches?page=2")
        betting_response = client.get("/betting-desk?page=2")
        performance_response = client.get("/model-performance")
    after = services.get_system_status()["provider_budget"]["metrics"]["provider_http_requests"]

    assert today_response.status_code == 200
    assert matches_response.status_code == 200
    assert betting_response.status_code == 200
    assert performance_response.status_code == 200
    assert before == 0
    assert after == 0


def test_today_route_reuses_single_paper_positions_read(tmp_path: Path, monkeypatch) -> None:
    services, fixtures = _build_today_read_path_services(tmp_path)
    _seed_today_read_path_paper_rows(services.config, fixtures)
    app = create_app(services=services)
    paper_position_calls = {"count": 0}
    original_paper_positions = services._paper_positions

    def counted_paper_positions():
        paper_position_calls["count"] += 1
        return original_paper_positions()

    monkeypatch.setattr(services, "_paper_positions", counted_paper_positions)

    with TestClient(app) as client:
        paper_position_calls["count"] = 0
        response = client.get("/today")

    assert response.status_code == 200
    assert paper_position_calls["count"] == 1


def test_dashboard_payload_system_status_matches_standalone_status(tmp_path: Path) -> None:
    services, fixtures = _build_today_read_path_services(tmp_path)
    _seed_today_read_path_paper_rows(services.config, fixtures)

    asyncio.run(services.initialize())
    payload = services.get_dashboard_payload()
    standalone_status = services.get_system_status()
    asyncio.run(services.shutdown())

    payload_status = dict(payload["system_status"])
    payload_status["current_time_utc"] = None
    payload_status["current_time_local"] = None
    standalone_status["current_time_utc"] = None
    standalone_status["current_time_local"] = None

    assert payload_status == standalone_status


def test_dashboard_get_routes_do_not_run_schema_initialization_after_startup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    services, fixtures = _build_today_read_path_services(tmp_path)
    _seed_today_read_path_paper_rows(services.config, fixtures)
    schema_calls = {"dashboard": 0, "paper": 0, "canonical": 0}
    original_dashboard_ensure = dashboard_storage.ensure_schema
    original_paper_ensure = football_paper_trading.ensure_schema
    original_canonical_ensure = football_canonical_fixture.ensure_schema

    def counted_dashboard_ensure(conn):
        schema_calls["dashboard"] += 1
        return original_dashboard_ensure(conn)

    def counted_paper_ensure(conn):
        schema_calls["paper"] += 1
        return original_paper_ensure(conn)

    def counted_canonical_ensure(conn):
        schema_calls["canonical"] += 1
        return original_canonical_ensure(conn)

    monkeypatch.setattr(dashboard_storage, "ensure_schema", counted_dashboard_ensure)
    monkeypatch.setattr(football_paper_trading, "ensure_schema", counted_paper_ensure)
    monkeypatch.setattr(football_canonical_fixture, "ensure_schema", counted_canonical_ensure)

    app = create_app(services=services)
    with TestClient(app) as client:
        schema_calls.update({"dashboard": 0, "paper": 0, "canonical": 0})
        responses = [
            client.get("/today"),
            client.get("/status"),
            client.get("/matches"),
            client.get("/betting-desk"),
            client.get("/model-performance"),
        ]

    assert all(response.status_code == 200 for response in responses)
    assert schema_calls == {"dashboard": 0, "paper": 0, "canonical": 0}


@pytest.mark.parametrize("path", ["/today", "/today?partial=1"])
def test_today_routes_do_not_issue_schema_ddl_or_writes_after_startup(
    tmp_path: Path,
    monkeypatch,
    path: str,
) -> None:
    services, fixtures = _build_today_read_path_services(tmp_path)
    _seed_today_read_path_paper_rows(services.config, fixtures)
    traced_sql: list[str] = []
    _install_dashboard_sql_trace(monkeypatch, traced_sql)

    app = create_app(services=services)
    with TestClient(app) as client:
        traced_sql.clear()
        response = client.get(path)

    counts = _sql_trace_counts(traced_sql)

    assert response.status_code == 200
    assert counts["select"] > 0
    assert counts["ddl"] == 0
    assert counts["pragma_table_info"] == 0
    assert counts["sqlite_master"] == 0
    assert counts["schema_introspection"] == 0
    assert counts["writes"] == 0


def test_repeated_today_payload_builds_do_not_reintroduce_schema_work(
    tmp_path: Path,
    monkeypatch,
) -> None:
    services, fixtures = _build_today_read_path_services(tmp_path)
    _seed_today_read_path_paper_rows(services.config, fixtures)

    asyncio.run(services.initialize())
    traced_sql: list[str] = []
    try:
        _install_dashboard_sql_trace(monkeypatch, traced_sql)
        for _ in range(10):
            payload = services.get_dashboard_payload()
            assert "today" in payload
    finally:
        asyncio.run(services.shutdown())

    counts = _sql_trace_counts(traced_sql)

    assert counts["select"] > 0
    assert counts["ddl"] == 0
    assert counts["pragma_table_info"] == 0
    assert counts["sqlite_master"] == 0
    assert counts["schema_introspection"] == 0
    assert counts["writes"] == 0


def test_dashboard_portfolio_policy_separates_selected_subset_from_opportunity_truth(tmp_path: Path) -> None:
    services, fixtures = _build_today_read_path_services(tmp_path)
    services.config = replace(
        services.config,
        portfolio_policy_effective_from="2026-08-01T00:00:00+00:00",
    )
    primary_fixture = fixtures[0]
    decision_timestamp = "2026-08-20T10:00:00+00:00"

    def record_trade(
        conn,
        *,
        prediction_id: str,
        market: str,
        selection: str,
        line: float | None,
        edge: float,
        fair_price: float,
        entry_odds: float,
    ) -> str:
        return football_paper_trading.record_paper_trade_entry(
            conn,
            football_paper_trading.PaperTradeEntry(
                prediction_id=prediction_id,
                fixture_id=primary_fixture.fixture_id,
                model_snapshot_id=f"snapshot_{prediction_id}",
                model_version="football_day11_manager_core_v1",
                market=market,
                selection=selection,
                line=line,
                model_probability=0.55,
                model_fair_price=fair_price,
                available_market_price=entry_odds,
                edge=edge,
                minimum_acceptable_price=1.8,
                decision="PAPER_CANDIDATE",
                decision_timestamp=decision_timestamp,
                kickoff=primary_fixture.kickoff_utc,
                safeguard_state="READY",
                entry_eligibility="ELIGIBLE",
                bookmaker="Pinnacle",
                provider="fake-market-provider",
                entry_odds=entry_odds,
                entry_captured_at=decision_timestamp,
            ),
        )

    asyncio.run(services.initialize())
    with football_paper_trading.connect(services.config.paper_trading_db_path) as conn:
        goals_trade_id = record_trade(
            conn,
            prediction_id="portfolio_goals",
            market="goals_total",
            selection="OVER",
            line=2.5,
            edge=0.09,
            fair_price=1.72,
            entry_odds=2.04,
        )
        one_x_two_trade_id = record_trade(
            conn,
            prediction_id="portfolio_1x2",
            market="1x2",
            selection="HOME",
            line=None,
            edge=0.07,
            fair_price=1.64,
            entry_odds=2.18,
        )
        btts_trade_id = record_trade(
            conn,
            prediction_id="portfolio_btts",
            market="btts",
            selection="YES",
            line=None,
            edge=0.06,
            fair_price=1.90,
            entry_odds=2.12,
        )
        corners_trade_id = record_trade(
            conn,
            prediction_id="portfolio_corners",
            market="corner_total",
            selection="OVER",
            line=9.5,
            edge=0.04,
            fair_price=1.95,
            entry_odds=2.08,
        )

    primary_decision_rows = [
        {
            "fixture_id": primary_fixture.fixture_id,
            "competition_id": "ENG1",
            "competition_name": "Premier League",
            "decision": "PRELIMINARY_CANDIDATE",
            "current_decision_state": "BET",
            "market_family": "goals_total",
            "selection": "OVER",
            "line": 2.5,
            "model_probability": 0.58,
            "model_fair_decimal_price": 1.72,
            "best_available_price": 2.04,
            "best_available_bookmaker": "Pinnacle",
            "best_available_source_timestamp": decision_timestamp,
            "settlement_aware_edge": 0.09,
            "paper_entry_eligible": True,
            "paper_trade_id": goals_trade_id,
            "market_freshness": "FRESH",
            "day12_state": "PASSED",
            "reason_codes": [],
        },
        {
            "fixture_id": primary_fixture.fixture_id,
            "competition_id": "ENG1",
            "competition_name": "Premier League",
            "decision": "PRELIMINARY_CANDIDATE",
            "current_decision_state": "BET",
            "market_family": "1x2",
            "selection": "HOME",
            "line": None,
            "model_probability": 0.61,
            "model_fair_decimal_price": 1.64,
            "best_available_price": 2.18,
            "best_available_bookmaker": "Pinnacle",
            "best_available_source_timestamp": decision_timestamp,
            "settlement_aware_edge": 0.07,
            "paper_entry_eligible": True,
            "paper_trade_id": one_x_two_trade_id,
            "market_freshness": "FRESH",
            "day12_state": "PASSED",
            "reason_codes": [],
        },
        {
            "fixture_id": primary_fixture.fixture_id,
            "competition_id": "ENG1",
            "competition_name": "Premier League",
            "decision": "PRELIMINARY_CANDIDATE",
            "current_decision_state": "BET",
            "market_family": "btts",
            "selection": "YES",
            "line": None,
            "model_probability": 0.52,
            "model_fair_decimal_price": 1.90,
            "best_available_price": 2.12,
            "best_available_bookmaker": "Pinnacle",
            "best_available_source_timestamp": decision_timestamp,
            "settlement_aware_edge": 0.06,
            "paper_entry_eligible": True,
            "paper_trade_id": btts_trade_id,
            "market_freshness": "FRESH",
            "day12_state": "PASSED",
            "reason_codes": [],
        },
        {
            "fixture_id": primary_fixture.fixture_id,
            "competition_id": "ENG1",
            "competition_name": "Premier League",
            "decision": "PRELIMINARY_CANDIDATE",
            "current_decision_state": "BET",
            "market_family": "corner_total",
            "selection": "OVER",
            "line": 9.5,
            "model_probability": 0.50,
            "model_fair_decimal_price": 1.95,
            "best_available_price": 2.08,
            "best_available_bookmaker": "Pinnacle",
            "best_available_source_timestamp": decision_timestamp,
            "settlement_aware_edge": 0.04,
            "paper_entry_eligible": True,
            "paper_trade_id": corners_trade_id,
            "market_freshness": "FRESH",
            "day12_state": "PASSED",
            "reason_codes": [],
        },
    ]
    services._market_evaluation_map = lambda: {
        primary_fixture.fixture_id: {
            "state": "READY",
            "reason": None,
            "decision_rows": primary_decision_rows,
            "display_rows": list(primary_decision_rows),
        }
    }

    prediction = {
        "prediction_id": "portfolio_prediction_primary",
        "fixture_id": primary_fixture.fixture_id,
        "competition_id": primary_fixture.competition_id,
        "home_team_name": primary_fixture.home_team_name,
        "away_team_name": primary_fixture.away_team_name,
        "kickoff_utc": primary_fixture.kickoff_utc,
        "season": primary_fixture.season,
    }

    with football_paper_trading.connect(services.config.paper_trading_db_path) as conn:
        assert services._canonical_fixture_for_prediction(conn, prediction) is not None
        services._sync_portfolio_selection_for_prediction(conn, prediction=prediction)
        services._sync_portfolio_selection_for_prediction(conn, prediction=prediction)
        selection_rows = football_portfolio_selection.fetch_current_portfolio_selections(
            conn,
            policy_id=PORTFOLIO_POLICY_ID_V1,
            policy_version=PORTFOLIO_POLICY_VERSION_V1,
        )

    selection_by_trade_id = {
        str(row["paper_trade_id"]): str(row["selection_state"])
        for row in selection_rows
    }
    assert selection_by_trade_id[goals_trade_id] == PORTFOLIO_STATE_SELECTED
    assert selection_by_trade_id[one_x_two_trade_id] == PORTFOLIO_STATE_SELECTED
    assert selection_by_trade_id[btts_trade_id] == PORTFOLIO_STATE_SELECTED
    assert selection_by_trade_id[corners_trade_id] == PORTFOLIO_STATE_NOT_SELECTED_FIXTURE_LIMIT
    assert len(selection_rows) == 4

    dashboard_payload = services.get_dashboard_payload()
    member_card_payload = services.get_member_card_payload()
    performance_payload = services.get_performance_payload()
    reports_payload = services.get_reports_payload()

    assert dashboard_payload["today_summary"]["suggested_bets"] == 3
    assert dashboard_payload["today_summary"]["matches"] == 1
    assert dashboard_payload["today_summary"]["open_portfolio_exposure"] == pytest.approx(3.0)
    assert dashboard_payload["best_picks_summary"]["count"] == 3
    assert (
        dashboard_payload["best_picks_summary"]["source"]
        == "Current deterministic portfolio selection. A separate frozen publication lifecycle is not active yet."
    )
    assert {
        str(row["paper_trade_id"])
        for row in dashboard_payload["published_paper_rows"]
    } == {goals_trade_id, one_x_two_trade_id, btts_trade_id}
    desk_by_trade_id = {
        str(row["paper_trade_id"]): row
        for row in dashboard_payload["today_desk_rows"]
        if row.get("paper_trade_id") is not None
    }
    assert dashboard_payload["today_summary"]["approved"] == 3
    assert desk_by_trade_id[corners_trade_id]["desk_decision"] == "NO_BET_PORTFOLIO_NOT_SELECTED"
    assert desk_by_trade_id[corners_trade_id]["portfolio_selection_state"] == PORTFOLIO_STATE_NOT_SELECTED_FIXTURE_LIMIT
    assert desk_by_trade_id[corners_trade_id]["paper_qualification_state"] == "NOT_PUBLISHED"

    assert member_card_payload["publish_state"] == "CURRENT PORTFOLIO"
    assert member_card_payload["portfolio_policy"] == {
        "policy_id": PORTFOLIO_POLICY_ID_V1,
        "policy_version": PORTFOLIO_POLICY_VERSION_V1,
        "effective_from": "2026-08-01T00:00:00+00:00",
    }
    assert len(member_card_payload["rows"]) == 3
    assert {row["portfolio_state"] for row in member_card_payload["rows"]} == {"SELECTED"}

    assert performance_payload["current_paper_summary"]["positions"] == 4
    assert performance_payload["current_paper_summary"]["open_positions"] == 4
    assert performance_payload["current_portfolio_summary"]["positions"] == 3
    assert performance_payload["current_portfolio_summary"]["open_positions"] == 3
    assert len(performance_payload["portfolio_positions"]) == 3
    assert len(performance_payload["open_portfolio_positions"]) == 3
    assert performance_payload["portfolio_publish_state"] == "CURRENT PORTFOLIO"

    assert reports_payload["portfolio_publish_state"] == "CURRENT PORTFOLIO"
    assert reports_payload["portfolio_policy"] == {
        "policy_id": PORTFOLIO_POLICY_ID_V1,
        "policy_version": PORTFOLIO_POLICY_VERSION_V1,
        "effective_from": "2026-08-01T00:00:00+00:00",
    }

    asyncio.run(services.shutdown())


def test_current_portfolio_removes_position_when_current_bet_becomes_no_bet(tmp_path: Path) -> None:
    fixture = _fixture()
    services = DashboardServices(
        config=_portfolio_enabled_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )
    prediction = _portfolio_prediction(fixture)

    asyncio.run(services.initialize())
    with football_paper_trading.connect(services.config.paper_trading_db_path) as conn:
        assert services._canonical_fixture_for_prediction(conn, prediction) is not None
        paper_trade_id = _record_portfolio_test_trade(
            conn,
            fixture=fixture,
            prediction_id="portfolio_no_bet_history",
            market="goals_total",
            selection="OVER",
            line=2.5,
            edge=0.09,
            fair_price=1.72,
            entry_odds=2.04,
        )

        services._market_evaluation_map = lambda: {
            fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [
                    _portfolio_decision_row(
                        fixture=fixture,
                        market_family="goals_total",
                        selection="OVER",
                        line=2.5,
                        paper_trade_id=paper_trade_id,
                        edge=0.09,
                        model_probability=0.58,
                        fair_price=1.72,
                        best_price=2.04,
                    )
                ],
                "display_rows": [],
            }
        }
        services._sync_portfolio_selection_for_prediction(conn, prediction=prediction)
        selected_rows = football_portfolio_selection.fetch_current_portfolio_selections(
            conn,
            policy_id=PORTFOLIO_POLICY_ID_V1,
            policy_version=PORTFOLIO_POLICY_VERSION_V1,
        )

        services._market_evaluation_map = lambda: {
            fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [
                    _portfolio_decision_row(
                        fixture=fixture,
                        market_family="goals_total",
                        selection="OVER",
                        line=2.5,
                        paper_trade_id=paper_trade_id,
                        edge=-0.02,
                        model_probability=0.52,
                        fair_price=1.92,
                        best_price=1.70,
                        source_timestamp="2026-08-20T10:30:00+00:00",
                        current_decision_state="NO_BET_VALUE",
                        decision="NO_BET",
                        paper_entry_eligible=False,
                        reason_codes=["PRICE_NO_VALUE"],
                    )
                ],
                "display_rows": [],
            }
        }
        services._sync_portfolio_selection_for_prediction(conn, prediction=prediction)
        current_rows = football_portfolio_selection.fetch_current_portfolio_selections(
            conn,
            policy_id=PORTFOLIO_POLICY_ID_V1,
            policy_version=PORTFOLIO_POLICY_VERSION_V1,
        )
        evaluation_count = conn.execute("SELECT COUNT(*) FROM football_portfolio_evaluations").fetchone()[0]
        paper_rows = football_paper_trading.fetch_paper_trade_rows(conn)

    assert len(selected_rows) == 1
    assert selected_rows[0]["selection_state"] == PORTFOLIO_STATE_SELECTED
    assert current_rows == []
    assert evaluation_count == 2
    assert len(paper_rows) == 1
    assert paper_rows[0]["paper_trade_id"] == paper_trade_id

    asyncio.run(services.shutdown())


def test_current_portfolio_removes_position_when_current_bet_becomes_stale(tmp_path: Path) -> None:
    fixture = _fixture()
    services = DashboardServices(
        config=_portfolio_enabled_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )
    prediction = _portfolio_prediction(fixture)

    asyncio.run(services.initialize())
    with football_paper_trading.connect(services.config.paper_trading_db_path) as conn:
        assert services._canonical_fixture_for_prediction(conn, prediction) is not None
        paper_trade_id = _record_portfolio_test_trade(
            conn,
            fixture=fixture,
            prediction_id="portfolio_stale_history",
            market="goals_total",
            selection="OVER",
            line=2.5,
            edge=0.08,
            fair_price=1.76,
            entry_odds=2.01,
        )

        services._market_evaluation_map = lambda: {
            fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [
                    _portfolio_decision_row(
                        fixture=fixture,
                        market_family="goals_total",
                        selection="OVER",
                        line=2.5,
                        paper_trade_id=paper_trade_id,
                        edge=0.08,
                        model_probability=0.57,
                        fair_price=1.76,
                        best_price=2.01,
                    )
                ],
                "display_rows": [],
            }
        }
        services._sync_portfolio_selection_for_prediction(conn, prediction=prediction)

        services._market_evaluation_map = lambda: {
            fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [
                    _portfolio_decision_row(
                        fixture=fixture,
                        market_family="goals_total",
                        selection="OVER",
                        line=2.5,
                        paper_trade_id=paper_trade_id,
                        edge=0.02,
                        model_probability=0.55,
                        fair_price=1.82,
                        best_price=1.88,
                        source_timestamp="2026-08-20T10:30:00+00:00",
                        current_decision_state="NO_BET_STALE_MARKET",
                        decision="STALE_MARKET",
                        paper_entry_eligible=False,
                        market_freshness="STALE",
                    )
                ],
                "display_rows": [],
            }
        }
        services._sync_portfolio_selection_for_prediction(conn, prediction=prediction)
        current_rows = football_portfolio_selection.fetch_current_portfolio_selections(
            conn,
            policy_id=PORTFOLIO_POLICY_ID_V1,
            policy_version=PORTFOLIO_POLICY_VERSION_V1,
        )
        evaluation_count = conn.execute("SELECT COUNT(*) FROM football_portfolio_evaluations").fetchone()[0]

    assert current_rows == []
    assert evaluation_count == 2

    asyncio.run(services.shutdown())


def test_current_portfolio_same_source_timestamp_fresh_to_stale_uses_newer_evaluation(tmp_path: Path, monkeypatch) -> None:
    fixture = _fixture()
    services = DashboardServices(
        config=_portfolio_enabled_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )
    prediction = _portfolio_prediction(fixture)
    evaluation_times = iter(
        [
            "2026-08-31T10:00:00+00:00",
            "2026-08-31T10:00:00+00:00",
            "2026-08-31T12:30:00+00:00",
            "2026-08-31T12:30:00+00:00",
        ]
    )

    asyncio.run(services.initialize())
    monkeypatch.setattr("sports_ai.dashboard.services.utc_now_iso", lambda: next(evaluation_times))
    with football_paper_trading.connect(services.config.paper_trading_db_path) as conn:
        assert services._canonical_fixture_for_prediction(conn, prediction) is not None
        paper_trade_id = _record_portfolio_test_trade(
            conn,
            fixture=fixture,
            prediction_id="portfolio_same_source_timestamp_stale",
            market="goals_total",
            selection="OVER",
            line=2.5,
            edge=0.08,
            fair_price=1.76,
            entry_odds=2.01,
            decision_timestamp="2026-08-31T09:55:00+00:00",
        )

        services._market_evaluation_map = lambda: {
            fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [
                    _portfolio_decision_row(
                        fixture=fixture,
                        market_family="goals_total",
                        selection="OVER",
                        line=2.5,
                        paper_trade_id=paper_trade_id,
                        edge=0.08,
                        model_probability=0.57,
                        fair_price=1.76,
                        best_price=2.01,
                        source_timestamp="2026-08-31T09:55:00+00:00",
                    )
                ],
                "display_rows": [],
            }
        }
        services._sync_portfolio_selection_for_prediction(conn, prediction=prediction)

        services._market_evaluation_map = lambda: {
            fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [
                    _portfolio_decision_row(
                        fixture=fixture,
                        market_family="goals_total",
                        selection="OVER",
                        line=2.5,
                        paper_trade_id=paper_trade_id,
                        edge=0.02,
                        model_probability=0.55,
                        fair_price=1.82,
                        best_price=1.88,
                        source_timestamp="2026-08-31T09:55:00+00:00",
                        current_decision_state="NO_BET_STALE_MARKET",
                        decision="STALE_MARKET",
                        paper_entry_eligible=False,
                        market_freshness="STALE",
                    )
                ],
                "display_rows": [],
            }
        }
        services._sync_portfolio_selection_for_prediction(conn, prediction=prediction)
        current_evaluations = football_portfolio_selection.fetch_current_portfolio_evaluations(
            conn,
            policy_id=PORTFOLIO_POLICY_ID_V1,
            policy_version=PORTFOLIO_POLICY_VERSION_V1,
            initialize_schema=False,
        )
        current_rows = football_portfolio_selection.fetch_current_portfolio_selections(
            conn,
            policy_id=PORTFOLIO_POLICY_ID_V1,
            policy_version=PORTFOLIO_POLICY_VERSION_V1,
        )

    assert current_evaluations[0]["evaluated_at"] == "2026-08-31T12:30:00+00:00"
    assert current_evaluations[0]["candidate_snapshot_at"] == "2026-08-31T09:55:00+00:00"
    assert current_rows == []

    asyncio.run(services.shutdown())


def test_current_portfolio_same_source_timestamp_fresh_to_stale_to_fresh_appends_recurrence(tmp_path: Path, monkeypatch) -> None:
    fixture = _fixture()
    services = DashboardServices(
        config=_portfolio_enabled_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )
    prediction = _portfolio_prediction(fixture)
    evaluation_times = iter(
        [
            "2026-08-31T10:00:00+00:00",
            "2026-08-31T10:00:00+00:00",
            "2026-08-31T12:30:00+00:00",
            "2026-08-31T12:30:00+00:00",
            "2026-08-31T13:00:00+00:00",
            "2026-08-31T13:00:00+00:00",
        ]
    )

    asyncio.run(services.initialize())
    monkeypatch.setattr("sports_ai.dashboard.services.utc_now_iso", lambda: next(evaluation_times))
    with football_paper_trading.connect(services.config.paper_trading_db_path) as conn:
        assert services._canonical_fixture_for_prediction(conn, prediction) is not None
        paper_trade_id = _record_portfolio_test_trade(
            conn,
            fixture=fixture,
            prediction_id="portfolio_same_source_timestamp_recurrence",
            market="goals_total",
            selection="OVER",
            line=2.5,
            edge=0.08,
            fair_price=1.76,
            entry_odds=2.01,
            decision_timestamp="2026-08-31T09:55:00+00:00",
        )

        services._market_evaluation_map = lambda: {
            fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [
                    _portfolio_decision_row(
                        fixture=fixture,
                        market_family="goals_total",
                        selection="OVER",
                        line=2.5,
                        paper_trade_id=paper_trade_id,
                        edge=0.08,
                        model_probability=0.57,
                        fair_price=1.76,
                        best_price=2.01,
                        source_timestamp="2026-08-31T09:55:00+00:00",
                    )
                ],
                "display_rows": [],
            }
        }
        services._sync_portfolio_selection_for_prediction(conn, prediction=prediction)

        services._market_evaluation_map = lambda: {
            fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [
                    _portfolio_decision_row(
                        fixture=fixture,
                        market_family="goals_total",
                        selection="OVER",
                        line=2.5,
                        paper_trade_id=paper_trade_id,
                        edge=0.02,
                        model_probability=0.55,
                        fair_price=1.82,
                        best_price=1.88,
                        source_timestamp="2026-08-31T09:55:00+00:00",
                        current_decision_state="NO_BET_STALE_MARKET",
                        decision="STALE_MARKET",
                        paper_entry_eligible=False,
                        market_freshness="STALE",
                    )
                ],
                "display_rows": [],
            }
        }
        services._sync_portfolio_selection_for_prediction(conn, prediction=prediction)

        services._market_evaluation_map = lambda: {
            fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [
                    _portfolio_decision_row(
                        fixture=fixture,
                        market_family="goals_total",
                        selection="OVER",
                        line=2.5,
                        paper_trade_id=paper_trade_id,
                        edge=0.08,
                        model_probability=0.57,
                        fair_price=1.76,
                        best_price=2.01,
                        source_timestamp="2026-08-31T09:55:00+00:00",
                    )
                ],
                "display_rows": [],
            }
        }
        services._sync_portfolio_selection_for_prediction(conn, prediction=prediction)

        evaluation_history = conn.execute(
            """
            SELECT evaluation_id, predecessor_evaluation_id, evaluated_at, evaluation_signature, candidate_count
            FROM football_portfolio_evaluations
            ORDER BY evaluated_at, created_at, evaluation_id
            """
        ).fetchall()
        current_evaluations = football_portfolio_selection.fetch_current_portfolio_evaluations(
            conn,
            policy_id=PORTFOLIO_POLICY_ID_V1,
            policy_version=PORTFOLIO_POLICY_VERSION_V1,
            initialize_schema=False,
        )
        current_rows = football_portfolio_selection.fetch_current_portfolio_selections(
            conn,
            policy_id=PORTFOLIO_POLICY_ID_V1,
            policy_version=PORTFOLIO_POLICY_VERSION_V1,
        )

    history = [dict(row) for row in evaluation_history]
    assert len(history) == 3
    assert history[0]["evaluation_signature"] == history[2]["evaluation_signature"]
    assert history[0]["evaluation_id"] != history[2]["evaluation_id"]
    assert history[2]["predecessor_evaluation_id"] == history[1]["evaluation_id"]
    assert current_evaluations[0]["evaluation_id"] == history[2]["evaluation_id"]
    assert current_evaluations[0]["evaluated_at"] == "2026-08-31T13:00:00+00:00"
    assert current_rows[0]["paper_trade_id"] == paper_trade_id
    assert current_rows[0]["selection_state"] == PORTFOLIO_STATE_SELECTED

    asyncio.run(services.shutdown())


def test_dashboard_policy_active_without_selection_keeps_best_picks_empty(tmp_path: Path, monkeypatch) -> None:
    fixture = _fixture()
    services = DashboardServices(
        config=_portfolio_enabled_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )
    fixture_row = fixture.to_dict(timezone_name="UTC")
    paper_rows = [
        {
            "paper_trade_id": f"paper_pending_{index}",
            "fixture_id": fixture.fixture_id,
            "market": market,
            "selection": selection,
            "line": line,
            "model_probability": 0.55,
            "model_fair_price": fair_price,
            "entry_odds": entry_odds,
            "edge": edge,
            "bookmaker": "Pinnacle",
            "kickoff": fixture.kickoff_utc,
            "competition_id": fixture.competition_id,
            "competition_name": fixture.competition_name,
            "home_team_name": fixture.home_team_name,
            "away_team_name": fixture.away_team_name,
            "decision_timestamp": "2026-08-20T10:00:00+00:00",
            "trade_state": "PENDING",
            "latest_update_payload": None,
        }
        for index, (market, selection, line, edge, fair_price, entry_odds) in enumerate(
            [
                ("goals_total", "OVER", 2.5, 0.09, 1.72, 2.04),
                ("1x2", "HOME", None, 0.07, 1.64, 2.18),
                ("btts", "YES", None, 0.06, 1.90, 2.12),
                ("corner_total", "OVER", 9.5, 0.04, 1.95, 2.08),
            ],
            start=1,
        )
    ]
    decision_rows = [
        _portfolio_decision_row(
            fixture=fixture,
            market_family=str(row["market"]),
            selection=str(row["selection"]),
            line=None if row.get("line") is None else float(row["line"]),
            paper_trade_id=str(row["paper_trade_id"]),
            edge=float(row["edge"]),
            model_probability=0.55,
            fair_price=float(row["model_fair_price"]),
            best_price=float(row["entry_odds"]),
        )
        for row in paper_rows
    ]

    local_date = datetime.fromisoformat(fixture.kickoff_utc).astimezone(ZoneInfo("Asia/Manila")).date()
    monkeypatch.setattr(services, "_current_local_date", lambda: local_date)
    monkeypatch.setattr(services, "_all_fixtures_with_predictions", lambda: [fixture_row])
    monkeypatch.setattr(services, "_future_fixture_rows", lambda rows: [fixture_row])
    monkeypatch.setattr(services, "_filter_fixture_rows", lambda *, bucket, fixture_rows: [fixture_row] if bucket == "today" else [])
    monkeypatch.setattr(
        services,
        "_market_evaluation_map",
        lambda: {
            fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": decision_rows,
                "display_rows": list(decision_rows),
            }
        },
    )
    monkeypatch.setattr(services, "_paper_positions", lambda: (paper_rows, football_paper_trading.paper_trading_status(paper_rows)))

    payload = services.get_dashboard_payload()

    assert payload["best_picks"] == []
    assert payload["best_picks_summary"]["count"] == 0
    assert payload["best_picks_summary"]["empty_message"] == "Portfolio selection pending for current qualified opportunities."
    assert len([row for row in payload["today_desk_rows"] if row["desk_label"] == "BET"]) == 4


def test_dashboard_policy_inactive_keeps_legacy_best_picks_behavior(tmp_path: Path, monkeypatch) -> None:
    fixture = _fixture()
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )
    fixture_row = fixture.to_dict(timezone_name="UTC")
    paper_rows = [
        {
            "paper_trade_id": "paper_legacy_best_pick",
            "fixture_id": fixture.fixture_id,
            "market": "1x2",
            "selection": "HOME",
            "line": None,
            "model_probability": 0.55,
            "model_fair_price": 1.64,
            "entry_odds": 2.18,
            "edge": 0.07,
            "bookmaker": "Pinnacle",
            "kickoff": fixture.kickoff_utc,
            "competition_id": fixture.competition_id,
            "competition_name": fixture.competition_name,
            "home_team_name": fixture.home_team_name,
            "away_team_name": fixture.away_team_name,
            "decision_timestamp": "2026-08-20T10:00:00+00:00",
            "trade_state": "PENDING",
            "latest_update_payload": None,
        }
    ]
    decision_row = _portfolio_decision_row(
        fixture=fixture,
        market_family="1x2",
        selection="HOME",
        line=None,
        paper_trade_id="paper_legacy_best_pick",
        edge=0.07,
        model_probability=0.61,
        fair_price=1.64,
        best_price=2.18,
    )

    monkeypatch.setattr(services, "_current_local_date", lambda: datetime.fromisoformat(fixture.kickoff_utc).astimezone(ZoneInfo("Asia/Manila")).date())
    monkeypatch.setattr(services, "_all_fixtures_with_predictions", lambda: [fixture_row])
    monkeypatch.setattr(services, "_future_fixture_rows", lambda rows: [fixture_row])
    monkeypatch.setattr(services, "_filter_fixture_rows", lambda *, bucket, fixture_rows: [fixture_row] if bucket == "today" else [])
    monkeypatch.setattr(
        services,
        "_market_evaluation_map",
        lambda: {
            fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [decision_row],
                "display_rows": [decision_row],
            }
        },
    )
    monkeypatch.setattr(services, "_paper_positions", lambda: (paper_rows, football_paper_trading.paper_trading_status(paper_rows)))

    payload = services.get_dashboard_payload()

    assert payload["best_picks_summary"]["count"] == 1
    assert payload["best_picks"][0]["paper_trade_id"] == "paper_legacy_best_pick"


def test_dashboard_portfolio_match_counts_use_canonical_fixture_identity(tmp_path: Path, monkeypatch) -> None:
    kickoff_utc = "2026-08-31T12:00:00+00:00"
    first_fixture = replace(
        _fixture(
            fixture_id="api-football:123",
            home_team_name="Arsenal",
            away_team_name="Chelsea",
        ),
        provider="api-football",
        provider_fixture_id="123",
        kickoff_utc=kickoff_utc,
    )
    second_fixture = replace(
        _fixture(
            fixture_id="football-data.org:456",
            home_team_name="Arsenal",
            away_team_name="Chelsea",
        ),
        provider="football-data.org",
        provider_fixture_id="456",
        kickoff_utc=kickoff_utc,
    )
    services = DashboardServices(
        config=_portfolio_enabled_config(tmp_path),
        fixture_provider=FakeFixtureProvider([first_fixture, second_fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )

    with dashboard_storage.connect(services.config.dashboard_db_path) as conn:
        dashboard_storage.ensure_schema(conn)
        dashboard_storage.upsert_fixtures(conn, [first_fixture, second_fixture], updated_at="2026-08-31T00:00:00+00:00")

    with football_paper_trading.connect(services.config.paper_trading_db_path) as conn:
        first_trade_id = _record_portfolio_test_trade(
            conn,
            fixture=first_fixture,
            prediction_id="api_position",
            market="1x2",
            selection="HOME",
            line=None,
            edge=0.07,
            fair_price=1.64,
            entry_odds=2.18,
            decision_timestamp="2026-08-31T09:00:00+00:00",
        )
        second_trade_id = _record_portfolio_test_trade(
            conn,
            fixture=second_fixture,
            prediction_id="fd_position",
            market="btts",
            selection="YES",
            line=None,
            edge=0.06,
            fair_price=1.90,
            entry_odds=2.12,
            decision_timestamp="2026-08-31T09:05:00+00:00",
        )
        canonical_fixture_id_1 = services._canonical_fixture_for_prediction(conn, _portfolio_prediction(first_fixture))
        canonical_fixture_id_2 = services._canonical_fixture_for_prediction(conn, _portfolio_prediction(second_fixture))
        assert canonical_fixture_id_1 == canonical_fixture_id_2
        football_portfolio_selection.record_portfolio_evaluation(
            conn,
            policy=services._portfolio_policy(),
            canonical_fixture_id=str(canonical_fixture_id_1),
            evaluated_at="2026-08-31T09:10:00+00:00",
            candidate_snapshot_at="2026-08-31T09:10:00+00:00",
            evaluation_signature=football_portfolio_selection.portfolio_evaluation_signature(
                policy=services._portfolio_policy(),
                canonical_fixture_id=str(canonical_fixture_id_1),
                candidates=[
                    {
                        "paper_trade_id": first_trade_id,
                        "market": "1x2",
                        "selection": "HOME",
                        "line": None,
                        "edge_at_selection": 0.07,
                        "decision_timestamp": "2026-08-31T09:00:00+00:00",
                        "created_at": "2026-08-31T09:00:00+00:00",
                        "safeguard_state": "PASSED",
                    },
                    {
                        "paper_trade_id": second_trade_id,
                        "market": "btts",
                        "selection": "YES",
                        "line": None,
                        "edge_at_selection": 0.06,
                        "decision_timestamp": "2026-08-31T09:05:00+00:00",
                        "created_at": "2026-08-31T09:05:00+00:00",
                        "safeguard_state": "PASSED",
                    },
                ],
            ),
            dispositions=[
                PortfolioDisposition(
                    paper_trade_id=first_trade_id,
                    canonical_fixture_id=str(canonical_fixture_id_1),
                    market="1x2",
                    selection="HOME",
                    line=None,
                    selection_state=PORTFOLIO_STATE_SELECTED,
                    selection_rank=1,
                    selection_reason="Selected for canonical test.",
                    edge_at_selection=0.07,
                ),
                PortfolioDisposition(
                    paper_trade_id=second_trade_id,
                    canonical_fixture_id=str(canonical_fixture_id_1),
                    market="btts",
                    selection="YES",
                    line=None,
                    selection_state=PORTFOLIO_STATE_SELECTED,
                    selection_rank=2,
                    selection_reason="Selected for canonical test.",
                    edge_at_selection=0.06,
                ),
            ],
        )

    first_row = first_fixture.to_dict(timezone_name="UTC")
    second_row = second_fixture.to_dict(timezone_name="UTC")
    monkeypatch.setattr(services, "_current_local_date", lambda: date(2026, 8, 31))
    monkeypatch.setattr(services, "_all_fixtures_with_predictions", lambda: [first_row, second_row])
    monkeypatch.setattr(services, "_future_fixture_rows", lambda rows: [first_row, second_row])
    monkeypatch.setattr(
        services,
        "_filter_fixture_rows",
        lambda *, bucket, fixture_rows: [first_row, second_row] if bucket == "today" else [],
    )
    monkeypatch.setattr(services, "_market_evaluation_map", lambda: {})

    payload = services.get_dashboard_payload()
    metrics = {item["label"]: item for item in payload["glance_metrics"]}

    assert payload["today_summary"]["matches"] == 1
    assert metrics["Matches"]["value"] == "1"


def test_dashboard_get_routes_do_not_create_portfolio_evaluations_when_policy_active(tmp_path: Path, monkeypatch) -> None:
    fixture = _fixture()
    services = DashboardServices(
        config=_portfolio_enabled_config(tmp_path),
        fixture_provider=FakeFixtureProvider([fixture]),
        market_provider=FakeMarketProvider(configured=False),
    )
    fixture_row = fixture.to_dict(timezone_name="UTC")
    paper_rows = [
        {
            "paper_trade_id": "paper_get_safe",
            "fixture_id": fixture.fixture_id,
            "market": "1x2",
            "selection": "HOME",
            "line": None,
            "model_probability": 0.55,
            "model_fair_price": 1.64,
            "entry_odds": 2.18,
            "edge": 0.07,
            "bookmaker": "Pinnacle",
            "kickoff": fixture.kickoff_utc,
            "competition_id": fixture.competition_id,
            "competition_name": fixture.competition_name,
            "home_team_name": fixture.home_team_name,
            "away_team_name": fixture.away_team_name,
            "decision_timestamp": "2026-08-20T10:00:00+00:00",
            "trade_state": "PENDING",
            "latest_update_payload": None,
        }
    ]
    decision_row = _portfolio_decision_row(
        fixture=fixture,
        market_family="1x2",
        selection="HOME",
        line=None,
        paper_trade_id="paper_get_safe",
        edge=0.07,
        model_probability=0.61,
        fair_price=1.64,
        best_price=2.18,
    )
    record_calls = {"count": 0}
    original_record = football_portfolio_selection.record_portfolio_evaluation

    def counted_record(*args, **kwargs):
        record_calls["count"] += 1
        return original_record(*args, **kwargs)

    local_date = datetime.fromisoformat(fixture.kickoff_utc).astimezone(ZoneInfo("Asia/Manila")).date()
    monkeypatch.setattr(services, "_current_local_date", lambda: local_date)
    monkeypatch.setattr(services, "_all_fixtures_with_predictions", lambda: [fixture_row])
    monkeypatch.setattr(services, "_future_fixture_rows", lambda rows: [fixture_row])
    monkeypatch.setattr(services, "_filter_fixture_rows", lambda *, bucket, fixture_rows: [fixture_row] if bucket == "today" else [])
    monkeypatch.setattr(
        services,
        "_market_evaluation_map",
        lambda: {
            fixture.fixture_id: {
                "state": "READY",
                "reason": None,
                "decision_rows": [decision_row],
                "display_rows": [decision_row],
            }
        },
    )
    monkeypatch.setattr(services, "_paper_positions", lambda: (paper_rows, football_paper_trading.paper_trading_status(paper_rows)))
    monkeypatch.setattr(football_portfolio_selection, "record_portfolio_evaluation", counted_record)

    app = create_app(services=services)
    with TestClient(app) as client:
        responses = [
            client.get("/today"),
            client.get("/betting-desk"),
            client.get("/model-performance"),
            client.get("/reports"),
            client.get("/member-card"),
        ]

    assert all(response.status_code == 200 for response in responses)
    assert record_calls["count"] == 0


def test_dashboard_connect_skips_wal_reinitialization_while_writer_holds_transaction(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "dashboard.sqlite"
    with dashboard_storage.connect(db_path) as conn:
        dashboard_storage.initialize_database(conn)

    with sqlite3.connect(db_path) as conn:
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"

    writer = sqlite3.connect(db_path, timeout=dashboard_storage.SQLITE_BUSY_TIMEOUT_MS / 1000)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        """
        INSERT INTO dashboard_runtime_state (state_key, state_value_json, updated_at)
        VALUES (?, ?, ?)
        """,
        ("writer-lock", "{}", "2026-09-01T00:00:00+00:00"),
    )

    statements: list[str] = []
    original_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", _recording_connect(original_connect, statements))

    started = time_module.perf_counter()
    conn = dashboard_storage.connect(db_path)
    elapsed = time_module.perf_counter() - started
    conn.close()

    writer.rollback()
    writer.close()

    assert elapsed < 1.0
    assert _count_sql_containing(statements, "pragma journal_mode = wal") == 0
    assert _count_sql_containing(statements, "pragma busy_timeout = 5000") == 1
    assert _count_sql_containing(statements, "pragma foreign_keys = on") == 1


def test_today_payload_build_does_not_reinitialize_wal_or_schema_after_startup(tmp_path: Path, monkeypatch) -> None:
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([]),
        market_provider=FakeMarketProvider(configured=False),
    )
    asyncio.run(services.initialize())

    statements: list[str] = []
    original_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", _recording_connect(original_connect, statements))

    first_payload = services.get_dashboard_payload()
    second_payload = services.get_dashboard_payload()
    third_payload = services.get_dashboard_payload()

    asyncio.run(services.shutdown())

    assert first_payload["today"] == []
    assert second_payload["today"] == []
    assert third_payload["today"] == []
    assert _count_sql_containing(statements, "pragma journal_mode = wal") == 0
    assert _count_sql_with_prefixes(
        statements,
        "create table",
        "create index",
        "create unique index",
        "alter table",
        "pragma table_info(",
    ) == 0
    assert _count_sql_with_prefixes(statements, "insert ", "update ", "delete ", "replace ") == 0


@pytest.mark.parametrize("position_count", [383, 1000, 2500])
def test_current_portfolio_sync_reads_paper_ledger_once_per_batch(tmp_path: Path, monkeypatch, position_count: int) -> None:
    fixture_count = max(1, position_count // 5)
    fixtures = [
        replace(
            _fixture(
                fixture_id=f"fake:portfolio_perf_{index:04d}",
                home_team_name=f"Home {index:04d}",
                away_team_name=f"Away {index:04d}",
            ),
            provider_fixture_id=f"fixture-{index:04d}",
            kickoff_utc=f"2026-09-{(index % 20) + 1:02d}T12:00:00+00:00",
        )
        for index in range(fixture_count)
    ]
    services = DashboardServices(
        config=_portfolio_enabled_config(tmp_path),
        fixture_provider=FakeFixtureProvider(fixtures),
        market_provider=FakeMarketProvider(configured=False),
    )
    prediction_by_fixture_id = {fixture.fixture_id: _portfolio_prediction(fixture) for fixture in fixtures}
    evaluations: dict[str, dict[str, object]] = {}

    with dashboard_storage.connect(services.config.dashboard_db_path) as conn:
        dashboard_storage.ensure_schema(conn)
        dashboard_storage.upsert_fixtures(conn, fixtures, updated_at="2026-08-31T00:00:00+00:00")

    with football_paper_trading.connect(services.config.paper_trading_db_path) as conn:
        football_paper_trading.ensure_schema(conn)
        football_portfolio_selection.ensure_schema(conn)
        football_canonical_fixture.ensure_schema(conn)
        recorded = 0
        for fixture in fixtures:
            decision_rows = []
            for market, selection, line, edge, fair_price, entry_odds in [
                ("1x2", "HOME", None, 0.08, 1.64, 2.18),
                ("btts", "YES", None, 0.07, 1.90, 2.10),
                ("goals_total", "OVER", 2.5, 0.09, 1.72, 2.04),
                ("corner_total", "OVER", 9.5, 0.05, 1.95, 2.08),
                ("asian_handicap", "HOME", -0.5, 0.06, 1.88, 2.02),
            ]:
                if recorded >= position_count:
                    break
                prediction_id = f"perf_{fixture.fixture_id}_{recorded}"
                paper_trade_id = _record_portfolio_test_trade(
                    conn,
                    fixture=fixture,
                    prediction_id=prediction_id,
                    market=market,
                    selection=selection,
                    line=line,
                    edge=edge,
                    fair_price=fair_price,
                    entry_odds=entry_odds,
                    decision_timestamp=f"2026-08-31T09:{recorded % 60:02d}:00+00:00",
                )
                decision_rows.append(
                    _portfolio_decision_row(
                        fixture=fixture,
                        market_family=market,
                        selection=selection,
                        line=line,
                        paper_trade_id=paper_trade_id,
                        edge=edge,
                        model_probability=0.55,
                        fair_price=fair_price,
                        best_price=entry_odds,
                        source_timestamp=f"2026-08-31T10:{recorded % 60:02d}:00+00:00",
                    )
                )
                recorded += 1
            if decision_rows:
                evaluations[fixture.fixture_id] = {
                    "state": "READY",
                    "reason": None,
                    "decision_rows": decision_rows,
                    "display_rows": list(decision_rows),
                }
            if recorded >= position_count:
                break

        for fixture_id in evaluations:
            assert services._canonical_fixture_for_prediction(conn, prediction_by_fixture_id[fixture_id]) is not None
        services._schema_initialized = True

        counts = {
            "paper_fetch": 0,
            "portfolio_ensure": 0,
            "canonical_ensure": 0,
            "canonical_resolve": 0,
            "fixture_lookup": 0,
            "portfolio_writes": 0,
        }
        original_fetch = football_paper_trading.fetch_paper_trade_rows
        original_portfolio_ensure = football_portfolio_selection.ensure_schema
        original_canonical_ensure = football_canonical_fixture.ensure_schema
        original_resolve = football_canonical_fixture.resolve_or_create_canonical_fixture
        original_fetch_fixture = dashboard_storage.fetch_fixture
        original_record = football_portfolio_selection.record_portfolio_evaluation
        original_dashboard_connect = dashboard_storage.connect
        dashboard_sql = {"count": 0}

        def counted_fetch(*args, **kwargs):
            counts["paper_fetch"] += 1
            return original_fetch(*args, **kwargs)

        def counted_portfolio_ensure(*args, **kwargs):
            counts["portfolio_ensure"] += 1
            return original_portfolio_ensure(*args, **kwargs)

        def counted_canonical_ensure(*args, **kwargs):
            counts["canonical_ensure"] += 1
            return original_canonical_ensure(*args, **kwargs)

        def counted_resolve(*args, **kwargs):
            counts["canonical_resolve"] += 1
            return original_resolve(*args, **kwargs)

        def counted_fetch_fixture(*args, **kwargs):
            counts["fixture_lookup"] += 1
            return original_fetch_fixture(*args, **kwargs)

        def counted_record(*args, **kwargs):
            counts["portfolio_writes"] += 1
            return original_record(*args, **kwargs)

        def counted_dashboard_connect(*args, **kwargs):
            dashboard_conn = original_dashboard_connect(*args, **kwargs)
            dashboard_conn.set_trace_callback(lambda _: dashboard_sql.__setitem__("count", dashboard_sql["count"] + 1))
            return dashboard_conn

        monkeypatch.setattr(football_paper_trading, "fetch_paper_trade_rows", counted_fetch)
        monkeypatch.setattr(football_portfolio_selection, "ensure_schema", counted_portfolio_ensure)
        monkeypatch.setattr(football_canonical_fixture, "ensure_schema", counted_canonical_ensure)
        monkeypatch.setattr(football_canonical_fixture, "resolve_or_create_canonical_fixture", counted_resolve)
        monkeypatch.setattr(dashboard_storage, "fetch_fixture", counted_fetch_fixture)
        monkeypatch.setattr(football_portfolio_selection, "record_portfolio_evaluation", counted_record)
        monkeypatch.setattr(dashboard_storage, "connect", counted_dashboard_connect)
        started = time_module.perf_counter()
        services._sync_current_portfolio_selections(
            conn,
            evaluations=evaluations,
            prediction_by_fixture_id={fixture_id: prediction_by_fixture_id[fixture_id] for fixture_id in evaluations},
        )
        elapsed = time_module.perf_counter() - started
        current_evaluations = football_portfolio_selection.fetch_current_portfolio_evaluations(
            conn,
            policy_id=PORTFOLIO_POLICY_ID_V1,
            policy_version=PORTFOLIO_POLICY_VERSION_V1,
            initialize_schema=False,
        )

    assert counts["paper_fetch"] == 1
    assert counts["portfolio_ensure"] == 0
    assert counts["canonical_ensure"] == 0
    assert counts["canonical_resolve"] == 0
    assert counts["fixture_lookup"] == 0
    assert counts["portfolio_writes"] == len(evaluations)
    assert dashboard_sql["count"] <= 1
    assert len(current_evaluations) == len(evaluations)
    assert elapsed > 0.0


def test_sync_once_runs_blocking_body_off_event_loop_and_today_request_stays_responsive(
    tmp_path: Path,
    monkeypatch,
) -> None:
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([]),
        market_provider=FakeMarketProvider(configured=False),
    )
    app = create_app(services=services)
    started = threading.Event()
    release = threading.Event()

    def fake_sync_once_blocking(*, sync_kind: str = "manual", include_schedule: bool = True) -> dict[str, str]:
        started.set()
        release.wait(timeout=1)
        return {"state": "ok", "kind": sync_kind, "include_schedule": str(include_schedule)}

    monkeypatch.setattr(services, "_sync_once_blocking", fake_sync_once_blocking)

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            sync_task = asyncio.create_task(services.sync_once(sync_kind="manual_refresh", include_schedule=True))
            started_in_time = await asyncio.to_thread(started.wait, 1)
            assert started_in_time is True
            asyncio.get_running_loop().call_later(1.0, release.set)

            request_started = time_module.perf_counter()
            response = await client.get("/today")
            elapsed = time_module.perf_counter() - request_started

            assert response.status_code == 200
            assert elapsed < 0.8

            await asyncio.wait_for(sync_task, timeout=1)

    asyncio.run(scenario())


def test_concurrent_read_routes_stay_responsive_during_slow_sync(
    tmp_path: Path,
    monkeypatch,
) -> None:
    services, _, _ = _build_paginated_dashboard_services(tmp_path, monkeypatch, fixture_count=60)
    app = create_app(services=services)
    started = threading.Event()
    release = threading.Event()

    def fake_sync_once_blocking(*, sync_kind: str = "manual", include_schedule: bool = True) -> dict[str, str]:
        started.set()
        release.wait(timeout=1)
        return {"state": "ok", "kind": sync_kind, "include_schedule": str(include_schedule)}

    monkeypatch.setattr(services, "_sync_once_blocking", fake_sync_once_blocking)

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            before = services.get_system_status()["provider_budget"]["metrics"]["provider_http_requests"]
            sync_task = asyncio.create_task(services.sync_once(sync_kind="scheduled_live", include_schedule=True))
            started_in_time = await asyncio.to_thread(started.wait, 1)
            assert started_in_time is True
            asyncio.get_running_loop().call_later(1.0, release.set)

            request_started = time_module.perf_counter()
            responses = await asyncio.gather(
                client.get("/today"),
                client.get("/matches"),
                client.get("/betting-desk"),
                client.get("/model-performance"),
            )
            elapsed = time_module.perf_counter() - request_started
            after = services.get_system_status()["provider_budget"]["metrics"]["provider_http_requests"]

            assert all(response.status_code == 200 for response in responses)
            assert elapsed < 1.0
            assert before == 0
            assert after == 0

            await asyncio.wait_for(sync_task, timeout=1)

    asyncio.run(scenario())


def test_sync_once_remains_single_flight_when_called_concurrently(
    tmp_path: Path,
    monkeypatch,
) -> None:
    services = DashboardServices(
        config=_dashboard_config(tmp_path),
        fixture_provider=FakeFixtureProvider([]),
        market_provider=FakeMarketProvider(configured=False),
    )
    started = threading.Event()
    release = threading.Event()
    counter_lock = threading.Lock()
    call_order: list[str] = []
    active_calls = 0
    max_active_calls = 0

    def fake_sync_once_blocking(*, sync_kind: str = "manual", include_schedule: bool = True) -> dict[str, str]:
        nonlocal active_calls, max_active_calls
        with counter_lock:
            active_calls += 1
            max_active_calls = max(max_active_calls, active_calls)
            call_order.append(sync_kind)
        started.set()
        release.wait(timeout=1)
        with counter_lock:
            active_calls -= 1
        return {"state": "ok", "kind": sync_kind, "include_schedule": str(include_schedule)}

    monkeypatch.setattr(services, "_sync_once_blocking", fake_sync_once_blocking)

    async def scenario() -> None:
        first = asyncio.create_task(services.sync_once(sync_kind="first", include_schedule=True))
        started_in_time = await asyncio.to_thread(started.wait, 1)
        assert started_in_time is True

        second = asyncio.create_task(services.sync_once(sync_kind="second", include_schedule=False))
        await asyncio.sleep(0.05)
        assert second.done() is False

        release.set()

        first_result, second_result = await asyncio.gather(first, second)
        assert first_result["kind"] == "first"
        assert second_result["kind"] == "second"

    asyncio.run(scenario())

    assert call_order == ["first", "second"]
    assert max_active_calls == 1
