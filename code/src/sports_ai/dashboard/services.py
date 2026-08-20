"""Application services for the operational football dashboard."""

from __future__ import annotations

import asyncio
import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..market_data.api_football import ApiFootballOddsProvider
from ..market_data.providers import MarketProvider as CurrentMarketProvider
from ..market_data.providers import NotConfiguredMarketProvider
from ..storage import football_paper_trading
from . import storage
from .current_market import build_fixture_market_evaluation
from .fixture_provider import (
    FootballDataOrgFixtureProvider,
    FixtureProvider,
    FixtureProviderError,
    FixtureProviderNotConfiguredError,
)
from .models import (
    FIXTURE_LIVE_STATUSES,
    FIXTURE_PREMATCH_STATUSES,
    FIXTURE_STATUS_FINISHED,
    FIXTURE_TERMINAL_STATUSES,
    PREDICTION_STATUS_BLOCKED,
    PREDICTION_STATUS_FAILED,
    PREDICTION_STATUS_READY,
    NormalizedFixture,
    parse_iso_datetime,
    utc_now_iso,
)
from .prediction_service import PredictionContext, build_prediction_for_fixture
from .snapshot_service import CurrentFootballModelSnapshot, CurrentSnapshotError, build_current_football_model_snapshot

DEFAULT_UPCOMING_HORIZON_DAYS = 5


def _default_market_provider() -> CurrentMarketProvider:
    provider = ApiFootballOddsProvider()
    return provider if provider.configured() else NotConfiguredMarketProvider()


@dataclass(frozen=True)
class DashboardConfig:
    repo_root: Path
    research_db_path: Path
    dashboard_db_path: Path
    paper_trading_db_path: Path
    performance_output_dir: Path
    display_timezone: str = "Asia/Manila"
    upcoming_horizon_days: int = DEFAULT_UPCOMING_HORIZON_DAYS
    scheduler_interval_seconds: int = 300
    live_refresh_interval_seconds: int = 60
    future_refresh_interval_seconds: int = 300
    manual_refresh_cooldown_seconds: int = 30
    enable_scheduler: bool = True
    current_odds_connected: bool = False
    current_xg_connected: bool = False

    @classmethod
    def default(cls) -> "DashboardConfig":
        repo_root = Path(__file__).resolve().parents[4]
        return cls(
            repo_root=repo_root,
            research_db_path=repo_root / "research" / "football_research" / "store" / "football_research.sqlite",
            dashboard_db_path=repo_root / "research" / "product_outputs" / "dashboard" / "football_dashboard.sqlite",
            paper_trading_db_path=repo_root / "research" / "product_outputs" / "dashboard" / "football_paper_trading.sqlite",
            performance_output_dir=repo_root / "research" / "product_outputs" / "football" / "performance",
            scheduler_interval_seconds=300,
            live_refresh_interval_seconds=60,
            future_refresh_interval_seconds=300,
            manual_refresh_cooldown_seconds=30,
        )


class DashboardServices:
    """Coordinates fixture sync, prediction generation, and read APIs."""

    def __init__(
        self,
        config: DashboardConfig | None = None,
        *,
        fixture_provider: FixtureProvider | None = None,
        market_provider: CurrentMarketProvider | None = None,
    ):
        self.config = DashboardConfig.default() if config is None else config
        self.fixture_provider = fixture_provider or FootballDataOrgFixtureProvider()
        self.market_provider = market_provider or _default_market_provider()
        self.snapshot: CurrentFootballModelSnapshot | None = None
        self.snapshot_error: str | None = None
        self._background_task: asyncio.Task[None] | None = None
        self._shutdown = False
        self._provider_state: dict[str, Any] = {
            "provider": self.fixture_provider.provider_name(),
            "configured": self.fixture_provider.configured(),
            "state": "NOT_CONFIGURED" if not self.fixture_provider.configured() else "DEGRADED",
            "last_sync_at": None,
            "last_attempted_sync_at": None,
            "last_successful_sync_at": None,
            "last_sync_kind": None,
            "last_error": None,
            "next_scheduled_refresh_at": None,
            "sync_diagnostics": None,
            "nearest_supported_fixture": None,
            "last_counts": self._empty_counts(),
        }
        self._prediction_state: dict[str, Any] = {
            "state": "BLOCKED",
            "last_prediction_generated_at": None,
            "pending_fixture_count": 0,
            "blocked_fixture_count": 0,
            "ready_fixture_count": 0,
        }
        self._market_runtime_state: dict[str, Any] = {
            "state": "NOT_CONFIGURED" if not self.market_provider.configured() else "CONNECTED",
            "last_sync_at": None,
            "last_successful_snapshot_at": None,
            "last_error": None,
            "fixtures_considered": 0,
            "fixtures_matched": 0,
            "market_snapshots": 0,
            "markets_covered": [],
            "freshness": "UNKNOWN",
            "unresolved_fixture_count": 0,
            "stale_market_count": 0,
            "candidate_count": 0,
            "paper_entry_count": 0,
            "settlement_queue_count": 0,
            "decision_count": 0,
            "evaluations": {},
        }

    async def initialize(self) -> None:
        with storage.connect(self.config.dashboard_db_path) as conn:
            storage.ensure_schema(conn)
        with football_paper_trading.connect(self.config.paper_trading_db_path) as conn:
            football_paper_trading.ensure_schema(conn)
        self._build_snapshot()
        await self.sync_once(sync_kind="startup", include_schedule=True)
        if self.config.enable_scheduler and self._background_task is None:
            self._background_task = asyncio.create_task(self._run_scheduler())

    async def shutdown(self) -> None:
        self._shutdown = True
        if self._background_task is not None:
            self._background_task.cancel()
            try:
                await self._background_task
            except asyncio.CancelledError:
                pass

    def _build_snapshot(self) -> None:
        try:
            self.snapshot = build_current_football_model_snapshot(research_db_path=self.config.research_db_path)
            self.snapshot_error = None
            self._prediction_state["state"] = "READY"
        except CurrentSnapshotError as exc:
            self.snapshot = None
            self.snapshot_error = str(exc)
            self._prediction_state["state"] = "BLOCKED"

    def _empty_counts(self) -> dict[str, int]:
        return {
            "provider_raw": 0,
            "normalized": 0,
            "supported": 0,
            "unsupported_competition": 0,
            "unresolved_team": 0,
            "discarded_malformed": 0,
            "live": 0,
            "today": 0,
            "upcoming": 0,
            "predictions_ready": 0,
            "predictions_blocked": 0,
            "predictions_pending": 0,
            "candidates_available": 0,
        }

    def _provider_reset_sync_diagnostics(self) -> None:
        reset = getattr(self.fixture_provider, "reset_sync_diagnostics", None)
        if callable(reset):
            reset()

    def _provider_sync_diagnostics(self) -> dict[str, Any]:
        diagnostics = getattr(self.fixture_provider, "sync_diagnostics", None)
        if callable(diagnostics):
            return diagnostics()
        return {
            "provider": self.fixture_provider.provider_name(),
            "query_count": 0,
            "competition_codes_queried": [],
            "league_ids_queried": [],
            "provider_seasons_queried": [],
            "has_errors": False,
            "last_error": None,
            "queries": [],
            "plan_note": None,
            "live_status_note": None,
            "cache_entries": 0,
        }

    def _provider_error_text(self, diagnostics: dict[str, Any]) -> str | None:
        last_error = diagnostics.get("last_error")
        if isinstance(last_error, str) and last_error.strip():
            return last_error.strip()
        if not isinstance(last_error, dict):
            return None
        error_map = last_error.get("errors")
        if isinstance(error_map, dict) and error_map:
            details = "; ".join(f"{key}: {value}" for key, value in sorted(error_map.items()))
            season_text = last_error.get("provider_season")
            league_id = last_error.get("provider_league_id")
            competition_codes = last_error.get("competition_codes") or []
            context = []
            if season_text not in {None, ""}:
                context.append(f"season={season_text}")
            if league_id not in {None, ""}:
                context.append(f"league_id={league_id}")
            if competition_codes:
                context.append(f"competitions={','.join(str(code) for code in competition_codes)}")
            suffix = f" ({', '.join(context)})" if context else ""
            return f"{details}{suffix}"
        message = last_error.get("message")
        if isinstance(message, str) and message.strip():
            competition_codes = last_error.get("competition_codes") or []
            suffix = ""
            if competition_codes:
                suffix = f" (competitions={','.join(str(code) for code in competition_codes)})"
            return f"{message.strip()}{suffix}"
        return None

    def _market_provider_state(self) -> dict[str, Any]:
        diagnostics = self.market_provider.diagnostics()
        configured = bool(diagnostics.get("configured", self.market_provider.configured()))
        state = str(diagnostics.get("state") or ("CONNECTED" if configured else "NOT_CONFIGURED"))
        return {
            "provider": self.market_provider.provider_name(),
            "configured": configured,
            "state": state,
            "current_market_decisions": diagnostics.get(
                "current_market_decisions",
                "READY" if configured else "BLOCKED",
            ),
            "reason": diagnostics.get("reason"),
            "captured_at": diagnostics.get("captured_at"),
        }

    def _performance_summary_path(self) -> Path:
        return self.config.performance_output_dir / "football_performance_summary.json"

    def _performance_csv_path(self, filename: str) -> Path:
        return self.config.performance_output_dir / filename

    def _load_json_file(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _load_csv_rows(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def _paper_positions(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        with football_paper_trading.connect(self.config.paper_trading_db_path) as conn:
            rows = football_paper_trading.fetch_paper_trade_rows(conn)
        return rows, football_paper_trading.paper_trading_status(rows)

    def _market_evaluation_map(self) -> dict[str, Any]:
        return dict(self._market_runtime_state.get("evaluations") or {})

    def _market_evaluation_for_fixture(self, fixture_id: str) -> dict[str, Any] | None:
        return self._market_evaluation_map().get(str(fixture_id))

    def _performance_status(self, summary_payload: dict[str, Any] | None, paper_status: dict[str, Any]) -> dict[str, Any]:
        return {
            "historical_validation": {
                "state": "READY" if summary_payload is not None else "BLOCKED",
                "summary_generated_at": None if summary_payload is None else summary_payload.get("generated_at"),
            },
            "paper_ledger": {
                "state": paper_status["ledger_state"],
                "paper_bets": paper_status["paper_bets"],
                "open_positions": paper_status["open_positions"],
                "settled_positions": paper_status["settled_positions"],
            },
            "closing_price_tracking": {
                "state": "READY",
            },
            "clv": {
                "state": paper_status["clv_state"],
            },
        }

    def _next_scheduled_refresh_at(self, *, from_now: datetime | None = None) -> str | None:
        if not self.config.enable_scheduler:
            return None
        base = datetime.now(timezone.utc) if from_now is None else from_now.astimezone(timezone.utc)
        refresh_seconds = max(
            1,
            min(
                int(self.config.live_refresh_interval_seconds),
                int(self.config.future_refresh_interval_seconds),
            ),
        )
        return (base + timedelta(seconds=refresh_seconds)).isoformat(timespec="seconds")

    async def _run_scheduler(self) -> None:
        live_interval = max(15, int(self.config.live_refresh_interval_seconds))
        future_interval = max(live_interval, int(self.config.future_refresh_interval_seconds))
        next_live = datetime.now(timezone.utc) + timedelta(seconds=live_interval)
        next_future = datetime.now(timezone.utc) + timedelta(seconds=future_interval)
        while not self._shutdown:
            now_utc = datetime.now(timezone.utc)
            next_due = min(next_live, next_future)
            sleep_seconds = max(1.0, (next_due - now_utc).total_seconds())
            await asyncio.sleep(sleep_seconds)
            now_utc = datetime.now(timezone.utc)
            full_refresh = now_utc >= next_future
            try:
                await self.sync_once(
                    sync_kind="scheduled_future" if full_refresh else "scheduled_live",
                    include_schedule=full_refresh,
                )
            except Exception as exc:  # pragma: no cover - defensive background loop
                self._provider_state["last_error"] = f"{type(exc).__name__}: {exc}"
            next_live = now_utc + timedelta(seconds=live_interval)
            if full_refresh:
                next_future = now_utc + timedelta(seconds=future_interval)

    async def manual_refresh(self) -> dict[str, Any]:
        now_utc = datetime.now(timezone.utc)
        last_attempted = self._provider_state.get("last_attempted_sync_at")
        if last_attempted:
            elapsed = (now_utc - parse_iso_datetime(str(last_attempted))).total_seconds()
            remaining = int(self.config.manual_refresh_cooldown_seconds - elapsed)
            if remaining > 0:
                return {
                    "refresh_state": "COOLDOWN",
                    "seconds_remaining": remaining,
                    "system_status": self.get_system_status(),
                }
        await self.sync_once(sync_kind="manual_refresh", include_schedule=True)
        return {
            "refresh_state": "SYNCED",
            "seconds_remaining": int(self.config.manual_refresh_cooldown_seconds),
            "system_status": self.get_system_status(),
        }

    async def sync_once(
        self,
        *,
        sync_kind: str = "manual",
        include_schedule: bool = True,
    ) -> dict[str, Any]:
        now_utc = datetime.now(timezone.utc)
        attempted_at = utc_now_iso()
        with storage.connect(self.config.dashboard_db_path) as conn:
            self._provider_state["last_attempted_sync_at"] = attempted_at
            self._provider_state["last_sync_kind"] = sync_kind
            self._provider_state["next_scheduled_refresh_at"] = self._next_scheduled_refresh_at(from_now=now_utc)
            if not self.fixture_provider.configured():
                self._provider_state.update(
                    {
                        "configured": False,
                        "state": "NOT_CONFIGURED",
                        "last_sync_at": attempted_at,
                        "last_error": "FIXTURE PROVIDER NOT CONFIGURED",
                        "sync_diagnostics": None,
                    }
                )
                storage.put_runtime_state(
                    conn,
                    state_key="provider_state",
                    state_value=self._provider_state,
                    updated_at=attempted_at,
                )
                return self._provider_state

            self._provider_reset_sync_diagnostics()
            self._prediction_state["state"] = "RUNNING" if self.snapshot is not None else "BLOCKED"

            live: list[NormalizedFixture] = []
            today: list[NormalizedFixture] = []
            upcoming: list[NormalizedFixture] = []
            try:
                today_local = now_utc.astimezone(ZoneInfo(self.config.display_timezone)).date()
                live = self.fixture_provider.get_live_fixtures()
                if include_schedule:
                    today = self.fixture_provider.get_fixtures_for_date(today_local)
                    next_local_day = today_local + timedelta(days=1)
                    upcoming_start = (
                        datetime.combine(next_local_day, time.min, tzinfo=ZoneInfo(self.config.display_timezone))
                        .astimezone(timezone.utc)
                    )
                    upcoming_end = now_utc + timedelta(days=self.config.upcoming_horizon_days)
                    if upcoming_start <= upcoming_end:
                        upcoming = self.fixture_provider.get_upcoming_fixtures(
                            from_utc=upcoming_start,
                            to_utc=upcoming_end,
                        )
            except FixtureProviderNotConfiguredError as exc:
                self._provider_state.update(
                    {
                        "configured": False,
                        "state": "NOT_CONFIGURED",
                        "last_sync_at": attempted_at,
                        "last_error": str(exc),
                        "sync_diagnostics": self._provider_sync_diagnostics(),
                    }
                )
                storage.put_runtime_state(
                    conn,
                    state_key="provider_state",
                    state_value=self._provider_state,
                    updated_at=attempted_at,
                )
                return self._provider_state
            except FixtureProviderError as exc:
                self._provider_state.update(
                    {
                        "configured": True,
                        "state": "DEGRADED",
                        "last_sync_at": attempted_at,
                        "last_error": str(exc),
                        "sync_diagnostics": self._provider_sync_diagnostics(),
                    }
                )
                storage.put_runtime_state(
                    conn,
                    state_key="provider_state",
                    state_value=self._provider_state,
                    updated_at=attempted_at,
                )
                return self._provider_state

            fixture_map = {fixture.fixture_id: fixture for fixture in [*live, *today, *upcoming]}
            sync_fixtures = sorted(fixture_map.values(), key=lambda item: (item.kickoff_utc, item.fixture_id))
            if sync_fixtures:
                storage.upsert_fixtures(conn, sync_fixtures, updated_at=attempted_at)

            for fixture in sync_fixtures:
                if fixture.status in FIXTURE_TERMINAL_STATUSES:
                    continue
                self.ensure_prediction(conn, fixture)

            fixture_rows = storage.fetch_all_fixtures(conn)
            predictions = storage.fetch_all_predictions(conn)
            prediction_map = {
                str(prediction["fixture_id"]): self._decorate_prediction_runtime(prediction)
                for prediction in predictions
            }
            rows_with_predictions = [
                {
                    **fixture_row,
                    "prediction": prediction_map.get(str(fixture_row["fixture_id"])),
                }
                for fixture_row in fixture_rows
            ]

            provider_diagnostics = self._provider_sync_diagnostics()
            current_counts = self._counts_for_rows(rows_with_predictions)
            current_counts["provider_raw"] = sum(
                int(query.get("raw_fixture_count") or 0)
                for query in provider_diagnostics.get("queries") or []
            )
            current_counts["normalized"] = len(sync_fixtures)
            current_counts["supported"] = sum(
                1
                for row in rows_with_predictions
                if self._prediction_eligibility_status(row.get("prediction"))
                not in {"MODEL_NOT_AVAILABLE", "PREDICTION_PENDING"}
            )
            current_counts["unsupported_competition"] = sum(
                1
                for row in rows_with_predictions
                if self._prediction_eligibility_status(row.get("prediction")) == "MODEL_NOT_AVAILABLE"
            )
            current_counts["unresolved_team"] = sum(
                1
                for row in rows_with_predictions
                if self._prediction_eligibility_status(row.get("prediction")) == "TEAM_IDENTITY_UNRESOLVED"
            )
            current_counts["discarded_malformed"] = 0
            current_counts["candidates_available"] = 0

            provider_error_text = self._provider_error_text(provider_diagnostics)
            provider_state = "DEGRADED" if provider_diagnostics.get("has_errors") else "CONNECTED"
            if provider_state == "CONNECTED":
                self._provider_state["last_successful_sync_at"] = attempted_at

            nearest_supported_fixture = self._nearest_supported_fixture(rows_with_predictions)
            self._provider_state.update(
                {
                    "configured": True,
                    "state": provider_state,
                    "last_sync_at": attempted_at,
                    "last_error": provider_error_text,
                    "sync_diagnostics": provider_diagnostics,
                    "nearest_supported_fixture": nearest_supported_fixture,
                    "last_counts": current_counts,
                }
            )
            self._update_prediction_state(rows_with_predictions)
            self._sync_current_market_state(rows_with_predictions)
            storage.put_runtime_state(
                conn,
                state_key="provider_state",
                state_value=self._provider_state,
                updated_at=attempted_at,
            )
            storage.put_runtime_state(
                conn,
                state_key="prediction_state",
                state_value=self._prediction_state,
                updated_at=attempted_at,
            )
            storage.put_runtime_state(
                conn,
                state_key="market_runtime_state",
                state_value=self._market_runtime_state,
                updated_at=attempted_at,
            )
        return self._provider_state

    def _latest_closing_snapshot(
        self,
        snapshot_rows: list[dict[str, Any]],
        *,
        market: str,
        selection: str,
        line: float | None,
        kickoff_utc: str,
    ) -> dict[str, Any] | None:
        kickoff = parse_iso_datetime(kickoff_utc)
        candidates = [
            row
            for row in snapshot_rows
            if str(row["market"]) == market
            and str(row["selection"]) == selection
            and ((line is None and row.get("line") is None) or row.get("line") == line)
            and bool(row.get("is_pre_match"))
            and parse_iso_datetime(str(row["captured_at"])) <= kickoff
        ]
        if not candidates:
            return None
        return sorted(candidates, key=lambda row: (row["captured_at"], int(row["snapshot_id"])))[-1]

    def _maybe_create_paper_entries(
        self,
        conn: Any,
        *,
        prediction: dict[str, Any],
        evaluation: dict[str, Any],
    ) -> int:
        created = 0
        for row in evaluation.get("decision_rows") or []:
            if not row.get("paper_entry_eligible"):
                continue
            existing = football_paper_trading.find_existing_paper_trade_entry(
                conn,
                prediction_id=str(prediction["prediction_id"]),
                fixture_id=str(prediction["fixture_id"]),
                market=str(row["market_family"]),
                selection=str(row["selection"]),
                line=None if row.get("line") is None else float(row["line"]),
            )
            if existing is not None:
                row["paper_trade_id"] = existing["paper_trade_id"]
                continue
            entry = football_paper_trading.PaperTradeEntry(
                prediction_id=str(prediction["prediction_id"]),
                fixture_id=str(prediction["fixture_id"]),
                model_snapshot_id=str(prediction["model_snapshot_id"]),
                model_version=str(
                    (prediction.get("model_versions") or {}).get("current_snapshot_model_version")
                    or prediction.get("prediction_version")
                ),
                market=str(row["market_family"]),
                selection=str(row["selection"]),
                line=None if row.get("line") is None else float(row["line"]),
                model_probability=float(row["model_probability"]),
                model_fair_price=float(row["model_fair_decimal_price"]),
                available_market_price=float(row["best_available_price"]),
                edge=None if row.get("settlement_aware_edge") is None else float(row["settlement_aware_edge"]),
                minimum_acceptable_price=None
                if row.get("minimum_acceptable_price") is None
                else float(row["minimum_acceptable_price"]),
                decision="PAPER_CANDIDATE",
                decision_timestamp=str(row["market_as_of"]),
                kickoff=str(prediction["kickoff_utc"]),
                safeguard_state=str(row["day12_state"]),
                entry_eligibility="ELIGIBLE",
                bookmaker=None if row.get("best_available_bookmaker") is None else str(row["best_available_bookmaker"]),
                provider=self.market_provider.provider_name(),
                entry_odds=float(row["best_available_price"]),
                entry_captured_at=str(row.get("best_available_source_timestamp") or row["market_as_of"]),
                entry_snapshot_id=row.get("entry_snapshot_id"),
            )
            football_paper_trading.record_paper_trade_entry(conn, entry)
            row["paper_trade_id"] = entry.paper_trade_id
            created += 1
        return created

    def _process_paper_trading_updates(self, rows_with_predictions: list[dict[str, Any]]) -> None:
        fixture_rows = {str(row["fixture_id"]): row for row in rows_with_predictions}
        now_utc = datetime.now(timezone.utc)
        with football_paper_trading.connect(self.config.paper_trading_db_path) as conn:
            snapshot_rows_by_fixture: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for snapshot_row in football_paper_trading.fetch_market_snapshots(conn):
                snapshot_rows_by_fixture[str(snapshot_row["fixture_id"])].append(snapshot_row)
            paper_rows = football_paper_trading.fetch_paper_trade_rows(conn)
            for row in paper_rows:
                fixture = fixture_rows.get(str(row["fixture_id"]))
                if fixture is None:
                    continue
                kickoff_utc = str(row["kickoff"])
                kickoff = parse_iso_datetime(kickoff_utc)
                if row.get("closing_odds") is None and now_utc >= kickoff:
                    closing_snapshot = self._latest_closing_snapshot(
                        snapshot_rows_by_fixture.get(str(row["fixture_id"]), []),
                        market=str(row["market"]),
                        selection=str(row["selection"]),
                        line=row.get("line"),
                        kickoff_utc=kickoff_utc,
                    )
                    if closing_snapshot is not None:
                        football_paper_trading.attach_closing_snapshot(
                            conn,
                            paper_trade_id=str(row["paper_trade_id"]),
                            closing_odds=float(closing_snapshot["odds"]),
                            closing_captured_at=str(closing_snapshot["captured_at"]),
                            closing_snapshot_id=int(closing_snapshot["snapshot_id"]),
                        )
                if row.get("trade_state") == "PENDING" and now_utc >= kickoff and str(fixture["status"]) in FIXTURE_LIVE_STATUSES:
                    football_paper_trading.append_trade_update(
                        conn,
                        football_paper_trading.build_trade_update_payload(
                            paper_trade_id=str(row["paper_trade_id"]),
                            trade_state="OPEN",
                            settlement_context={"status": str(fixture["status"])},
                            odds=float(row.get("entry_odds") or row["available_market_price"]),
                            notes="Fixture started; position is now open.",
                        ),
                    )
                if str(fixture["status"]) != FIXTURE_STATUS_FINISHED:
                    continue
                if str(row.get("trade_state")) in {"WON", "LOST", "PUSH", "HALF_WIN", "HALF_LOSS", "VOID"}:
                    continue
                actual_total_corners = None
                if fixture.get("home_corners") is not None and fixture.get("away_corners") is not None:
                    actual_total_corners = int(fixture["home_corners"]) + int(fixture["away_corners"])
                try:
                    state = football_paper_trading.settlement_state_for_market(
                        market=str(row["market"]),
                        selection=str(row["selection"]),
                        line=None if row.get("line") is None else float(row["line"]),
                        actual_home_goals=int(fixture.get("home_score") or 0),
                        actual_away_goals=int(fixture.get("away_score") or 0),
                        actual_total_corners=actual_total_corners,
                    )
                except ValueError:
                    football_paper_trading.append_trade_update(
                        conn,
                        football_paper_trading.build_trade_update_payload(
                            paper_trade_id=str(row["paper_trade_id"]),
                            trade_state="UNSETTLED",
                            settlement_context={"status": str(fixture["status"])},
                            odds=float(row.get("entry_odds") or row["available_market_price"]),
                            notes="Settlement pending because the required final market result is unavailable.",
                        ),
                    )
                    continue
                football_paper_trading.append_trade_update(
                    conn,
                    football_paper_trading.build_trade_update_payload(
                        paper_trade_id=str(row["paper_trade_id"]),
                        trade_state=state,
                        settlement_context={
                            "actual_home_goals": int(fixture.get("home_score") or 0),
                            "actual_away_goals": int(fixture.get("away_score") or 0),
                            "actual_total_corners": actual_total_corners,
                        },
                        odds=float(row.get("entry_odds") or row["available_market_price"]),
                    ),
                )

    def _sync_current_market_state(self, rows_with_predictions: list[dict[str, Any]]) -> None:
        market_state = self._market_provider_state()
        now_utc = datetime.now(timezone.utc)
        evaluations: dict[str, Any] = {}
        if not market_state["configured"]:
            self._market_runtime_state.update(
                {
                    "state": "NOT_CONFIGURED",
                    "last_sync_at": utc_now_iso(),
                    "last_error": market_state["reason"],
                    "fixtures_considered": 0,
                    "fixtures_matched": 0,
                    "market_snapshots": 0,
                    "markets_covered": [],
                    "freshness": "UNKNOWN",
                    "unresolved_fixture_count": 0,
                    "stale_market_count": 0,
                    "candidate_count": 0,
                    "paper_entry_count": 0,
                    "settlement_queue_count": 0,
                    "decision_count": 0,
                    "evaluations": {},
                }
            )
            return

        future_rows = [
            row
            for row in self._future_fixture_rows(rows_with_predictions)
            if self._prediction_eligibility_status(row.get("prediction")) == "PREDICTION_READY"
        ]
        snapshot_count = 0
        matched_count = 0
        candidate_count = 0
        passed_safeguards = 0
        unresolved_fixture_count = 0
        stale_market_count = 0
        paper_entries_created = 0
        markets_covered: set[str] = set()
        freshness_values: set[str] = set()
        last_successful_snapshot_at: str | None = None

        with football_paper_trading.connect(self.config.paper_trading_db_path) as conn:
            for row in future_rows:
                prediction = row.get("prediction")
                if prediction is None:
                    continue
                try:
                    snapshots = list(
                        self.market_provider.get_market_snapshot(
                            str(row["fixture_id"]),
                            fixture_context=row,
                        )
                    )
                except Exception as exc:
                    reason = str(exc)
                    if "MARKET_FIXTURE_UNRESOLVED" in reason:
                        unresolved_fixture_count += 1
                        evaluations[str(row["fixture_id"])] = {
                            "fixture_id": str(row["fixture_id"]),
                            "state": "MARKET_FIXTURE_UNRESOLVED",
                            "reason": reason,
                            "decision_rows": [],
                            "display_rows": [],
                            "counts": {
                                "predictions_evaluated": 0,
                                "preliminary_candidates": 0,
                                "no_bets": 0,
                                "blocked": 0,
                                "passed_safeguards": 0,
                            },
                        }
                    else:
                        evaluations[str(row["fixture_id"])] = {
                            "fixture_id": str(row["fixture_id"]),
                            "state": "ODDS_PROVIDER_DEGRADED",
                            "reason": reason,
                            "decision_rows": [],
                            "display_rows": [],
                            "counts": {
                                "predictions_evaluated": 0,
                                "preliminary_candidates": 0,
                                "no_bets": 0,
                                "blocked": 0,
                                "passed_safeguards": 0,
                            },
                        }
                    continue

                if not snapshots:
                    evaluations[str(row["fixture_id"])] = {
                        "fixture_id": str(row["fixture_id"]),
                        "state": "MARKET_NOT_AVAILABLE",
                        "reason": "MARKET_NOT_AVAILABLE",
                        "decision_rows": [],
                        "display_rows": [],
                        "counts": {
                            "predictions_evaluated": 0,
                            "preliminary_candidates": 0,
                            "no_bets": 0,
                            "blocked": 0,
                            "passed_safeguards": 0,
                        },
                    }
                    continue

                matched_count += 1
                last_successful_snapshot_at = max(snapshot.captured_at for snapshot in snapshots)
                entry_snapshot_ids: dict[str, int] = {}
                for snapshot in snapshots:
                    db_snapshot_id = football_paper_trading.record_market_snapshot(
                        conn,
                        football_paper_trading.StoredMarketSnapshot(
                            fixture_id=str(snapshot.fixture_id),
                            provider=str(snapshot.provider),
                            bookmaker=str(snapshot.bookmaker),
                            market=str(snapshot.market),
                            selection=str(snapshot.selection),
                            odds=float(snapshot.decimal_odds),
                            ingestion_version=str(snapshot.ingestion_version),
                            line=None if snapshot.line is None else float(snapshot.line),
                            provider_market_id=snapshot.provider_market_id,
                            provider_timestamp=snapshot.source_timestamp,
                            captured_at=str(snapshot.captured_at),
                            is_pre_match=bool(snapshot.is_pre_match),
                            provider_fixture_id=snapshot.provider_fixture_id,
                            home_team_name=snapshot.home_team_name,
                            away_team_name=snapshot.away_team_name,
                            competition_id=snapshot.competition_id,
                            competition_name=snapshot.competition_name,
                            kickoff_utc=snapshot.kickoff_utc,
                            freshness_state=snapshot.freshness_state,
                        ),
                    )
                    if snapshot.provider_market_id:
                        entry_snapshot_ids[str(snapshot.provider_market_id)] = db_snapshot_id
                    snapshot_count += 1
                    markets_covered.add(str(snapshot.market))
                    freshness_values.add(str(snapshot.freshness_state or "UNKNOWN"))

                evaluation = build_fixture_market_evaluation(
                    fixture_context=row,
                    prediction=prediction,
                    snapshots=snapshots,
                    current_xg_connected=self.config.current_xg_connected,
                )
                for decision_row in evaluation.get("decision_rows") or []:
                    provider_market_id = decision_row.get("best_available_provider_market_id")
                    if provider_market_id is not None:
                        decision_row["entry_snapshot_id"] = entry_snapshot_ids.get(str(provider_market_id))
                created = self._maybe_create_paper_entries(conn, prediction=prediction, evaluation=evaluation)
                paper_entries_created += created
                candidate_count += int(evaluation["counts"]["preliminary_candidates"])
                passed_safeguards += int(evaluation["counts"]["passed_safeguards"])
                stale_market_count += sum(
                    1
                    for decision_row in evaluation.get("decision_rows") or []
                    if decision_row.get("decision") == "STALE_MARKET"
                )
                evaluations[str(row["fixture_id"])] = evaluation

            self._process_paper_trading_updates(rows_with_predictions)
            paper_rows = football_paper_trading.fetch_paper_trade_rows(conn)

        self._market_runtime_state.update(
            {
                "state": market_state["state"],
                "last_sync_at": utc_now_iso(),
                "last_successful_snapshot_at": last_successful_snapshot_at,
                "last_error": market_state.get("last_error") or market_state.get("reason"),
                "fixtures_considered": len(future_rows),
                "fixtures_matched": matched_count,
                "market_snapshots": snapshot_count,
                "markets_covered": sorted(markets_covered),
                "freshness": "FRESH"
                if freshness_values == {"FRESH"}
                else ("STALE" if freshness_values == {"STALE"} else "UNKNOWN"),
                "unresolved_fixture_count": unresolved_fixture_count,
                "stale_market_count": stale_market_count,
                "candidate_count": candidate_count,
                "paper_entry_count": len(paper_rows),
                "settlement_queue_count": sum(
                    1
                    for row in paper_rows
                    if str(row.get("trade_state")) in {"PENDING", "OPEN", "UNSETTLED"}
                    and parse_iso_datetime(str(row["kickoff"])) <= now_utc
                ),
                "decision_count": sum(len(value.get("decision_rows") or []) for value in evaluations.values()),
                "passed_safeguards_count": passed_safeguards,
                "evaluations": evaluations,
            }
        )

    def ensure_prediction(self, conn: Any, fixture: NormalizedFixture) -> dict[str, Any]:
        if self.snapshot is None:
            payload = {
                "prediction_id": f"prediction_blocked_{fixture.fixture_id}",
                "fixture_id": fixture.fixture_id,
                "generated_at": utc_now_iso(),
                "model_as_of": None,
                "model_snapshot_id": None,
                "prediction_version": f"blocked|{fixture.fixture_id}",
                "prediction_locked_at": fixture.kickoff_utc,
                "version_key": f"blocked|{fixture.fixture_id}",
                "prediction_status": PREDICTION_STATUS_BLOCKED,
                "blockers": ["CURRENT_MODEL_SNAPSHOT_UNAVAILABLE"],
                "warnings": [self.snapshot_error] if self.snapshot_error else [],
                "manager_decision_status": "MARKET_DATA_NOT_CONNECTED",
                "safeguard_status": "BLOCKED",
                "safeguard_reason": "POINT-IN-TIME XG NOT AVAILABLE",
            }
            storage.save_prediction(conn, payload, updated_at=utc_now_iso())
            return payload

        version_key = self._prediction_version_key(fixture, self.snapshot)
        existing = storage.fetch_prediction_by_version_key(conn, version_key)
        if existing is not None:
            return existing

        kickoff_utc = parse_iso_datetime(fixture.kickoff_utc)
        current_time_utc = datetime.now(timezone.utc)
        latest = storage.fetch_latest_prediction_for_fixture(conn, fixture.fixture_id)
        if latest is not None and kickoff_utc <= current_time_utc:
            return latest
        if kickoff_utc <= current_time_utc:
            payload = {
                "prediction_id": f"prediction_locked_{fixture.fixture_id}",
                "fixture_id": fixture.fixture_id,
                "generated_at": utc_now_iso(),
                "model_as_of": self.snapshot.model_as_of,
                "model_snapshot_id": self.snapshot.snapshot_id,
                "prediction_version": f"missed_lock|{fixture.fixture_id}",
                "prediction_locked_at": fixture.kickoff_utc,
                "version_key": f"missed_lock|{fixture.fixture_id}",
                "prediction_status": PREDICTION_STATUS_BLOCKED,
                "blockers": ["PREDICTION_LOCKED_AFTER_KICKOFF"],
                "warnings": ["fixture passed kickoff before a pre-match prediction version was persisted"],
                "manager_decision_status": "MARKET_DATA_NOT_CONNECTED",
                "safeguard_status": "BLOCKED",
                "safeguard_reason": "POINT-IN-TIME XG NOT AVAILABLE",
                "kickoff_utc": fixture.kickoff_utc,
                "competition_id": fixture.competition_id,
                "competition_name": fixture.competition_name,
                "home_team_name": fixture.home_team_name,
                "away_team_name": fixture.away_team_name,
            }
            storage.save_prediction(conn, payload, updated_at=utc_now_iso())
            return payload

        context = PredictionContext(fixture=fixture, snapshot=self.snapshot)
        prediction = build_prediction_for_fixture(context)
        home_resolution = (prediction.get("team_resolution") or {}).get("home")
        away_resolution = (prediction.get("team_resolution") or {}).get("away")
        for resolution in (home_resolution, away_resolution):
            if isinstance(resolution, dict):
                storage.record_team_resolution(conn, resolution, updated_at=utc_now_iso())
        storage.save_prediction(conn, prediction, updated_at=utc_now_iso())
        return prediction

    @staticmethod
    def _prediction_version_key(
        fixture: NormalizedFixture,
        snapshot: CurrentFootballModelSnapshot,
    ) -> str:
        from ..locking import canonical_hash

        return canonical_hash(
            {
                "fixture_id": fixture.fixture_id,
                "model_snapshot_id": snapshot.snapshot_id,
                "kickoff_utc": fixture.kickoff_utc,
            }
        )

    def _decorate_prediction_runtime(self, prediction: dict[str, Any] | None) -> dict[str, Any] | None:
        if prediction is None:
            return None
        payload = dict(prediction)
        payload.setdefault("prediction_version", str(payload.get("version_key") or payload.get("prediction_id")))
        if payload.get("kickoff_utc") is None:
            return payload
        kickoff_utc = parse_iso_datetime(str(payload["kickoff_utc"]))
        if datetime.now(timezone.utc) >= kickoff_utc:
            payload["lock_state"] = "LOCKED_PRE_MATCH_PREDICTION"
        else:
            payload["lock_state"] = "PRE_MATCH_OPEN"
        payload.setdefault("prediction_locked_at", str(payload["kickoff_utc"]))
        return payload

    def _prediction_map(self) -> dict[str, dict[str, Any]]:
        with storage.connect(self.config.dashboard_db_path) as conn:
            predictions = storage.fetch_all_predictions(conn)
        prediction_map: dict[str, dict[str, Any]] = {}
        for prediction in predictions:
            prediction_map.setdefault(str(prediction["fixture_id"]), self._decorate_prediction_runtime(prediction))
        return prediction_map

    def _all_fixtures_with_predictions(self) -> list[dict[str, Any]]:
        with storage.connect(self.config.dashboard_db_path) as conn:
            fixture_rows = storage.fetch_all_fixtures(conn)
        prediction_map = self._prediction_map()
        return [
            {
                **fixture,
                "prediction": prediction_map.get(str(fixture["fixture_id"])),
            }
            for fixture in fixture_rows
        ]

    def _prediction_eligibility_status(self, prediction: dict[str, Any] | None) -> str:
        if prediction is None:
            return "PREDICTION_PENDING"
        if str(prediction.get("prediction_status")) == PREDICTION_STATUS_READY:
            return "PREDICTION_READY"
        if str(prediction.get("prediction_status")) == PREDICTION_STATUS_FAILED:
            return "PREDICTION_FAILED"
        blockers = {str(value) for value in prediction.get("blockers") or []}
        if "MODEL_NOT_AVAILABLE" in blockers:
            return "MODEL_NOT_AVAILABLE"
        if "PREDICTION_BLOCKED_TEAM_IDENTITY" in blockers:
            return "TEAM_IDENTITY_UNRESOLVED"
        if "CURRENT_MODEL_SNAPSHOT_UNAVAILABLE" in blockers or "CURRENT_STATE_UNAVAILABLE" in blockers:
            return "CURRENT_MODEL_STATE_UNAVAILABLE"
        if "PREDICTION_LOCKED_AFTER_KICKOFF" in blockers:
            return "PREDICTION_PENDING"
        return "PREDICTION_PENDING"

    def _filter_fixture_rows(
        self,
        *,
        bucket: str,
        fixture_rows: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._all_fixtures_with_predictions() if fixture_rows is None else fixture_rows
        now_utc = datetime.now(timezone.utc)
        today_local = now_utc.astimezone(ZoneInfo(self.config.display_timezone)).date()
        filtered: list[dict[str, Any]] = []
        for row in rows:
            kickoff = parse_iso_datetime(str(row["kickoff_utc"]))
            local_kickoff_date = kickoff.astimezone(ZoneInfo(self.config.display_timezone)).date()
            status = str(row["status"])
            if bucket == "live" and status in FIXTURE_LIVE_STATUSES:
                filtered.append(row)
            elif bucket == "today" and local_kickoff_date == today_local and status not in FIXTURE_LIVE_STATUSES:
                filtered.append(row)
            elif (
                bucket == "upcoming"
                and kickoff > now_utc
                and local_kickoff_date > today_local
                and status in FIXTURE_PREMATCH_STATUSES
            ):
                filtered.append(row)
            elif bucket == "finished" and status == FIXTURE_STATUS_FINISHED:
                filtered.append(row)
        return filtered

    def _counts_for_rows(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        counts = self._empty_counts()
        counts["live"] = len(self._filter_fixture_rows(bucket="live", fixture_rows=rows))
        counts["today"] = len(
            [
                row
                for row in self._filter_fixture_rows(bucket="today", fixture_rows=rows)
                if str(row["status"]) not in FIXTURE_TERMINAL_STATUSES
            ]
        )
        counts["upcoming"] = len(self._filter_fixture_rows(bucket="upcoming", fixture_rows=rows))
        for row in rows:
            eligibility = self._prediction_eligibility_status(row.get("prediction"))
            if eligibility == "PREDICTION_READY":
                counts["predictions_ready"] += 1
            elif eligibility == "PREDICTION_PENDING":
                counts["predictions_pending"] += 1
            else:
                counts["predictions_blocked"] += 1
        return counts

    def _future_fixture_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now_utc = datetime.now(timezone.utc)
        return sorted(
            [
                row
                for row in rows
                if parse_iso_datetime(str(row["kickoff_utc"])) > now_utc
                and str(row["status"]) in FIXTURE_PREMATCH_STATUSES
            ],
            key=lambda row: (row["kickoff_utc"], row["fixture_id"]),
        )

    def _update_prediction_state(self, rows_with_predictions: list[dict[str, Any]]) -> None:
        generated_times = [
            str(prediction["generated_at"])
            for row in rows_with_predictions
            for prediction in [row.get("prediction")]
            if isinstance(prediction, dict) and prediction.get("generated_at")
        ]
        ready_count = 0
        blocked_count = 0
        pending_count = 0
        for row in rows_with_predictions:
            eligibility = self._prediction_eligibility_status(row.get("prediction"))
            if eligibility == "PREDICTION_READY":
                ready_count += 1
            elif eligibility == "PREDICTION_PENDING":
                pending_count += 1
            else:
                blocked_count += 1
        self._prediction_state.update(
            {
                "state": "READY" if self.snapshot is not None else "BLOCKED",
                "last_prediction_generated_at": None if not generated_times else max(generated_times),
                "pending_fixture_count": pending_count,
                "blocked_fixture_count": blocked_count,
                "ready_fixture_count": ready_count,
            }
        )

    def _nearest_supported_fixture(self, rows_with_predictions: list[dict[str, Any]]) -> dict[str, Any] | None:
        now_utc = datetime.now(timezone.utc)
        candidates = []
        for row in rows_with_predictions:
            kickoff_utc = parse_iso_datetime(str(row["kickoff_utc"]))
            if kickoff_utc <= now_utc:
                continue
            if self._prediction_eligibility_status(row.get("prediction")) == "MODEL_NOT_AVAILABLE":
                continue
            candidates.append(row)
        if not candidates:
            return None
        row = sorted(candidates, key=lambda item: (item["kickoff_utc"], item["fixture_id"]))[0]
        prediction = row.get("prediction")
        scoreline = None
        if prediction and prediction.get("top_scorelines"):
            top = prediction["top_scorelines"][0]
            scoreline = f"{top['home_goals']}-{top['away_goals']}"
        return {
            "fixture_id": row["fixture_id"],
            "provider_fixture_id": row.get("provider_fixture_id"),
            "competition_id": row["competition_id"],
            "competition_name": row["competition_name"],
            "home_team_name": row["home_team_name"],
            "away_team_name": row["away_team_name"],
            "kickoff_utc": row["kickoff_utc"],
            "kickoff_local": row.get("kickoff_local")
            or parse_iso_datetime(str(row["kickoff_utc"])).astimezone(ZoneInfo(self.config.display_timezone)).strftime(
                "%Y-%m-%d %H:%M"
            ),
            "prediction_status": self._prediction_eligibility_status(prediction),
            "expected_total_goals": None if prediction is None else prediction.get("expected_total_goals"),
            "most_likely_score": scoreline,
        }

    def serialize_fixture_row(self, fixture_row: dict[str, Any]) -> dict[str, Any]:
        kickoff = parse_iso_datetime(str(fixture_row["kickoff_utc"]))
        local_kickoff = kickoff.astimezone(ZoneInfo(self.config.display_timezone)).strftime("%Y-%m-%d %H:%M")
        prediction = self._decorate_prediction_runtime(fixture_row.get("prediction"))
        market_evaluation = self._market_evaluation_for_fixture(str(fixture_row["fixture_id"]))
        top_market_row = None
        if market_evaluation and market_evaluation.get("display_rows"):
            top_market_row = market_evaluation["display_rows"][0]
        return {
            **fixture_row,
            "kickoff_local": local_kickoff,
            "prediction": prediction,
            "prediction_eligibility_status": self._prediction_eligibility_status(prediction),
            "market_evaluation": market_evaluation,
            "market_signal": None
            if top_market_row is None
            else {
                "state": market_evaluation["state"],
                "edge": top_market_row.get("settlement_aware_edge"),
                "decision": top_market_row.get("decision"),
                "day12_state": top_market_row.get("day12_state"),
                "market_family": top_market_row.get("market_family"),
                "selection": top_market_row.get("selection"),
                "line": top_market_row.get("line"),
            },
        }

    def get_dashboard_payload(self) -> dict[str, Any]:
        fixture_rows = self._all_fixtures_with_predictions()
        live_rows = [self.serialize_fixture_row(row) for row in self._filter_fixture_rows(bucket="live", fixture_rows=fixture_rows)]
        today_rows = [
            self.serialize_fixture_row(row)
            for row in self._filter_fixture_rows(bucket="today", fixture_rows=fixture_rows)
            if str(row["status"]) not in FIXTURE_TERMINAL_STATUSES
        ]
        upcoming_rows = [
            self.serialize_fixture_row(row)
            for row in self._filter_fixture_rows(bucket="upcoming", fixture_rows=fixture_rows)
        ]
        next_rows = [self.serialize_fixture_row(row) for row in self._future_fixture_rows(fixture_rows)[:8]]
        prediction_watchlist = [
            row
            for row in next_rows
            if row.get("prediction") and row["prediction"].get("prediction_status") == PREDICTION_STATUS_READY
        ][:6]
        return {
            "live": live_rows,
            "today": today_rows,
            "upcoming": upcoming_rows[:24],
            "next_matches": next_rows,
            "prediction_watchlist": prediction_watchlist,
            "system_status": self.get_system_status(),
        }

    def get_matches_payload(
        self,
        *,
        competition: str | None = None,
        team: str | None = None,
        prediction_status: str | None = None,
        status_bucket: str | None = None,
    ) -> dict[str, Any]:
        rows = self._all_fixtures_with_predictions()
        if status_bucket in {"live", "today", "upcoming", "finished"}:
            rows = self._filter_fixture_rows(bucket=status_bucket, fixture_rows=rows)
        elif status_bucket == "next":
            rows = self._future_fixture_rows(rows)[:24]
        if competition:
            rows = [row for row in rows if str(row["competition_id"]) == competition]
        if team:
            team_key = team.lower()
            rows = [
                row
                for row in rows
                if team_key in str(row["home_team_name"]).lower() or team_key in str(row["away_team_name"]).lower()
            ]
        if prediction_status:
            rows = [
                row
                for row in rows
                if self._prediction_eligibility_status(row.get("prediction")) == prediction_status
                or (row.get("prediction") or {}).get("prediction_status") == prediction_status
            ]
        serializable_rows = [self.serialize_fixture_row(row) for row in rows]
        return {
            "filters": {
                "competition": competition,
                "team": team,
                "prediction_status": prediction_status,
                "status_bucket": status_bucket,
            },
            "rows": serializable_rows,
            "system_status": self.get_system_status(),
        }

    def get_fixture_detail_payload(self, fixture_id: str) -> dict[str, Any] | None:
        with storage.connect(self.config.dashboard_db_path) as conn:
            fixture = storage.fetch_fixture(conn, fixture_id)
            prediction = storage.fetch_latest_prediction_for_fixture(conn, fixture_id)
        if fixture is None:
            return None
        serialized_fixture = self.serialize_fixture_row({**fixture, "prediction": prediction})
        return {
            "fixture": serialized_fixture,
            "prediction": self._decorate_prediction_runtime(prediction),
            "market_evaluation": serialized_fixture.get("market_evaluation"),
            "system_status": self.get_system_status(),
        }

    def get_prediction_payload(self, fixture_id: str) -> dict[str, Any] | None:
        with storage.connect(self.config.dashboard_db_path) as conn:
            prediction = storage.fetch_latest_prediction_for_fixture(conn, fixture_id)
        return self._decorate_prediction_runtime(prediction)

    def get_predictions_payload(self) -> dict[str, Any]:
        now_utc = datetime.now(timezone.utc)
        with storage.connect(self.config.dashboard_db_path) as conn:
            predictions = storage.fetch_all_predictions(conn)
            fixture_rows = {row["fixture_id"]: row for row in storage.fetch_all_fixtures(conn)}
        rows = []
        for prediction in predictions:
            fixture = fixture_rows.get(str(prediction["fixture_id"]))
            if fixture is None:
                continue
            kickoff_utc = parse_iso_datetime(str(fixture["kickoff_utc"]))
            if str(fixture["status"]) in FIXTURE_TERMINAL_STATUSES and kickoff_utc < now_utc:
                continue
            decorated_prediction = self._decorate_prediction_runtime(prediction)
            rows.append(
                {
                    "fixture": self.serialize_fixture_row({**fixture, "prediction": decorated_prediction}),
                    "prediction": decorated_prediction,
                }
            )
        return {
            "rows": rows,
            "system_status": self.get_system_status(),
        }

    def get_candidates_payload(self) -> dict[str, Any]:
        market_state = self._market_provider_state()
        evaluations = list(self._market_evaluation_map().values())
        preliminary = []
        blocked = []
        no_bets = []
        for evaluation in evaluations:
            for row in evaluation.get("decision_rows") or []:
                if row["decision"] == "PRELIMINARY_CANDIDATE":
                    preliminary.append(row)
                elif row["decision"] == "NO_BET":
                    no_bets.append(row)
                else:
                    blocked.append(row)
        return {
            "connected": market_state["configured"],
            "reason": market_state["reason"] or "Current market decision service unavailable until a current odds provider is configured.",
            "experimental_candidates": [],
            "preliminary_candidates": sorted(
                preliminary,
                key=lambda row: (-float(row.get("settlement_aware_edge") or 0.0), str(row["fixture_id"])),
            ),
            "blocked_candidates": blocked,
            "no_bet_candidates": no_bets,
            "rows": [self.serialize_fixture_row(row) for row in self._future_fixture_rows(self._all_fixtures_with_predictions())[:12]],
            "system_status": self.get_system_status(),
        }

    def get_performance_payload(self) -> dict[str, Any]:
        summary_payload = self._load_json_file(self._performance_summary_path())
        market_rows = self._load_csv_rows(self._performance_csv_path("football_performance_by_market.csv"))
        competition_rows = self._load_csv_rows(self._performance_csv_path("football_performance_by_competition.csv"))
        season_rows = self._load_csv_rows(self._performance_csv_path("football_performance_by_season.csv"))
        calibration_rows = self._load_csv_rows(self._performance_csv_path("football_calibration.csv"))
        paper_positions, paper_status = self._paper_positions()
        status = self._performance_status(summary_payload, paper_status)
        current_paper = paper_status.get("performance", {})

        calibration_series: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in calibration_rows:
            if row.get("scope_type") != "overall":
                continue
            if row.get("market") != "1x2":
                continue
            if not row.get("sample_size") or row.get("sample_size") == "0":
                continue
            selection = str(row.get("selection") or "UNKNOWN")
            if not row.get("predicted_mean_probability") or not row.get("observed_hit_rate"):
                continue
            calibration_series[selection].append(
                {
                    "x": float(row["predicted_mean_probability"]),
                    "y": float(row["observed_hit_rate"]),
                    "bucket_label": row["bucket_label"],
                    "sample_size": int(row["sample_size"]),
                }
            )
        for points in calibration_series.values():
            points.sort(key=lambda item: item["x"])

        summary = (summary_payload or {}).get("summary") or {}
        kpis = [
            ("Historical Fixtures", str(summary.get("evaluation_sample") or "NOT AVAILABLE")),
            ("Prediction Sample", str(summary.get("evaluation_sample") or "NOT AVAILABLE")),
            (
                "1X2 Accuracy",
                "NOT AVAILABLE" if summary.get("one_x_two_accuracy") is None else f"{float(summary['one_x_two_accuracy']):.2%}",
            ),
            (
                "Brier Score",
                "NOT AVAILABLE" if summary.get("one_x_two_brier") is None else f"{float(summary['one_x_two_brier']):.4f}",
            ),
            (
                "Log Loss",
                "NOT AVAILABLE" if summary.get("one_x_two_log_loss") is None else f"{float(summary['one_x_two_log_loss']):.4f}",
            ),
            ("Paper Bets", str(paper_status["paper_bets"])),
            (
                "Paper ROI",
                "NOT AVAILABLE" if current_paper.get("roi") is None else f"{float(current_paper['roi']):+.2%}",
            ),
            ("CLV", str(current_paper.get("clv_state", "UNAVAILABLE")).replace("_", " ")),
        ]
        return {
            "summary": summary,
            "summary_generated_at": None if summary_payload is None else summary_payload.get("generated_at"),
            "status_flags": {} if summary_payload is None else summary_payload.get("status_flags", {}),
            "kpis": kpis,
            "market_rows": market_rows,
            "competition_rows": competition_rows,
            "season_rows": season_rows,
            "calibration_series": calibration_series,
            "paper_positions": paper_positions,
            "current_paper_summary": current_paper,
            "current_paper_by_market": current_paper.get("by_market", {}),
            "performance_status": status,
            "system_status": self.get_system_status(),
        }

    def get_next_fixtures_payload(self) -> dict[str, Any]:
        payload = self.get_dashboard_payload()
        return {
            "fixtures": payload["next_matches"],
            "count": len(payload["next_matches"]),
            "nearest_supported_fixture": self._provider_state.get("nearest_supported_fixture"),
            "system_status": payload["system_status"],
        }

    def get_system_status(self) -> dict[str, Any]:
        snapshot_state = "READY" if self.snapshot is not None else "BLOCKED"
        market_state = self._market_provider_state()
        summary_payload = self._load_json_file(self._performance_summary_path())
        _, paper_status = self._paper_positions()
        performance_status = self._performance_status(summary_payload, paper_status)
        return {
            "current_time_utc": utc_now_iso(),
            "current_time_local": datetime.now(timezone.utc)
            .astimezone(ZoneInfo(self.config.display_timezone))
            .strftime("%Y-%m-%d %H:%M %Z"),
            "display_timezone": self.config.display_timezone,
            "fixture_feed": {
                "provider": self.fixture_provider.provider_name(),
                "state": self._provider_state["state"],
                "configured": self._provider_state["configured"],
                "last_sync_at": self._provider_state["last_sync_at"],
                "last_attempted_sync_at": self._provider_state["last_attempted_sync_at"],
                "last_successful_sync_at": self._provider_state["last_successful_sync_at"],
                "last_sync_kind": self._provider_state["last_sync_kind"],
                "last_error": self._provider_state["last_error"],
                "next_scheduled_refresh_at": self._provider_state["next_scheduled_refresh_at"],
                "counts": self._provider_state["last_counts"],
                "sync_diagnostics": self._provider_state["sync_diagnostics"],
                "nearest_supported_fixture": self._provider_state["nearest_supported_fixture"],
                "query_count": int((self._provider_state["sync_diagnostics"] or {}).get("query_count") or 0),
                "plan_note": (self._provider_state["sync_diagnostics"] or {}).get("plan_note"),
                "live_status_note": (self._provider_state["sync_diagnostics"] or {}).get("live_status_note"),
                "cache_entries": int((self._provider_state["sync_diagnostics"] or {}).get("cache_entries") or 0),
                "refresh_policy": {
                    "live_refresh_seconds": int(self.config.live_refresh_interval_seconds),
                    "future_refresh_seconds": int(self.config.future_refresh_interval_seconds),
                    "manual_refresh_cooldown_seconds": int(self.config.manual_refresh_cooldown_seconds),
                },
            },
            "prediction_engine": {
                "state": self._prediction_state["state"],
                "last_prediction_generated_at": self._prediction_state["last_prediction_generated_at"],
                "pending_fixture_count": self._prediction_state["pending_fixture_count"],
                "blocked_fixture_count": self._prediction_state["blocked_fixture_count"],
                "ready_fixture_count": self._prediction_state["ready_fixture_count"],
            },
            "current_model_snapshot": {
                "state": snapshot_state,
                "snapshot_id": None if self.snapshot is None else self.snapshot.snapshot_id,
                "model_as_of": None if self.snapshot is None else self.snapshot.model_as_of,
                "supported_competitions": []
                if self.snapshot is None
                else list(self.snapshot.supported_competitions),
                "source_match_cutoff": None if self.snapshot is None else self.snapshot.diagnostics.get("model_as_of"),
                "error": self.snapshot_error,
            },
            "odds_feed": {
                "state": market_state["state"],
                "provider": market_state["provider"],
                "reason": market_state["reason"],
            },
            "market_sync": dict(self._market_runtime_state),
            "xg": {
                "state": "AVAILABLE" if self.config.current_xg_connected else "MISSING",
            },
            "safeguards": {
                "state": "BLOCKED" if not self.config.current_xg_connected else "READY",
                "xg_availability": "AVAILABLE" if self.config.current_xg_connected else "POINT_IN_TIME_XG_MISSING",
                "blocked_reason": None if self.config.current_xg_connected else "POINT-IN-TIME XG NOT AVAILABLE",
            },
            "dashboard_ui_ready": "YES",
            "historical_validation": performance_status["historical_validation"],
            "market_provider": market_state,
            "paper_ledger": performance_status["paper_ledger"],
            "closing_price_tracking": performance_status["closing_price_tracking"],
            "clv": performance_status["clv"],
            "current_fixture_feed_ready": "YES"
            if self._provider_state["configured"] and self._provider_state["state"] == "CONNECTED"
            else "NO",
            "current_prediction_pipeline_ready": "YES"
            if self.snapshot is not None
            else "NO",
            "current_market_decision_ready": "YES"
            if market_state["configured"] and self._provider_state["configured"]
            else "NO",
            "current_paper_tracking_active": "YES"
            if int(self._market_runtime_state.get("paper_entry_count") or 0) > 0
            else "NO",
            "live_paper_pipeline_ready": "YES"
            if market_state["configured"] and self._provider_state["configured"] and self.config.current_xg_connected
            else "NO",
        }
