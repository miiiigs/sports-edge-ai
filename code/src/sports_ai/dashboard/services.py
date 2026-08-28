"""Application services for the operational football dashboard."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..market_data.providers import MarketProvider as CurrentMarketProvider
from ..market_data.providers import NotConfiguredMarketProvider
from ..market_data.the_odds_api import TheOddsApiProvider
from ..storage import football_edge_desk, football_paper_trading
from . import storage
from .current_market import build_fixture_market_evaluation
from .day12_rolling_xg import DEFAULT_XG_MIN_WINDOW_MATCHES, build_fixture_xg_rows, missing_xg_inspection
from .fixture_provider import (
    ApiFootballFixtureProvider,
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
from .prediction_service import PredictionContext, attach_edge_desk_shadow_bundle, build_prediction_for_fixture
from .runtime_paths import ensure_operational_data_dir, resolve_dashboard_operational_paths
from .snapshot_service import CurrentFootballModelSnapshot, CurrentSnapshotError, build_current_football_model_snapshot

DEFAULT_UPCOMING_HORIZON_DAYS = 5
DEFAULT_ENABLE_AUTOMATED_SYNC_ENV_VAR = "SPORTS_AI_ENABLE_AUTOMATED_SYNC"
DEFAULT_LIVE_REFRESH_ENV_VAR = "SPORTS_AI_FIXTURE_LIVE_REFRESH_SECONDS"
DEFAULT_FUTURE_REFRESH_ENV_VAR = "SPORTS_AI_FIXTURE_FUTURE_REFRESH_SECONDS"
DEFAULT_MANUAL_REFRESH_COOLDOWN_ENV_VAR = "SPORTS_AI_MANUAL_REFRESH_COOLDOWN_SECONDS"
DEFAULT_UPCOMING_HORIZON_ENV_VAR = "SPORTS_AI_UPCOMING_HORIZON_DAYS"
DEFAULT_CURRENT_XG_CONNECTED_ENV_VAR = "SPORTS_AI_CURRENT_XG_CONNECTED"
DEFAULT_CURRENT_ODDS_CONNECTED_ENV_VAR = "SPORTS_AI_CURRENT_ODDS_CONNECTED"
DEFAULT_ENABLE_ODDS_SYNC_ENV_VAR = "SPORTS_AI_ENABLE_ODDS_SYNC"
DEFAULT_ODDS_SYNC_ONCE_TOKEN_ENV_VAR = "SPORTS_AI_ODDS_SYNC_ONCE_TOKEN"

logger = logging.getLogger(__name__)


def _default_fixture_provider() -> FixtureProvider:
    primary = FootballDataOrgFixtureProvider()
    if primary.configured():
        return primary
    fallback = ApiFootballFixtureProvider()
    if fallback.configured():
        return fallback
    return primary


def _default_market_provider(config: DashboardConfig) -> CurrentMarketProvider:
    provider = TheOddsApiProvider()
    if provider.configured():
        return provider
    return NotConfiguredMarketProvider(
        provider_label="no-odds-provider-key" if config.current_odds_connected else "waiting-for-approved-odds-provider"
    )


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name, "").strip()
    return raw if raw else default


def _hash_one_shot_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _classify_one_shot_outcome(provider_state: str) -> tuple[str, str | None]:
    """Map a post-sync provider state to a one-shot (state, error_code) pair.

    A genuine provider failure or blocking state must not be reported as a
    successful checkpoint. A successful request that simply returned no eligible
    market rows (state IDLE/CONNECTED/NO_CURRENT_EVENTS) maps to SUCCESS with a
    None error code.
    """
    if provider_state == "AUTH_ERROR":
        return "FAILED", "AUTH_ERROR"
    if provider_state == "RATE_LIMITED":
        return "FAILED", "RATE_LIMITED"
    if provider_state == "DEGRADED":
        return "FAILED", "PROVIDER_ERROR"
    if provider_state == "QUOTA_EXHAUSTED":
        return "BLOCKED", "QUOTA_EXHAUSTED"
    if provider_state == "QUOTA_GUARD":
        return "BLOCKED", "QUOTA_GUARD"
    if provider_state == "NOT_CONFIGURED":
        return "BLOCKED", "PROVIDER_NOT_CONFIGURED"
    return "SUCCESS", None


def _format_display_line(line: float | None) -> str | None:
    if line is None:
        return None
    return f"{float(line):g}"


def _format_signed_display_line(line: float | None) -> str | None:
    if line is None:
        return None
    value = float(line)
    return f"{value:+g}" if value > 0 else f"{value:g}"


def _humanize_market_selection(
    *,
    market_family: str,
    selection: str,
    line: float | None,
    home_team_name: str,
    away_team_name: str,
) -> dict[str, str | None]:
    normalized_market = str(market_family)
    normalized_selection = str(selection).upper()
    line_text = _format_display_line(line)
    if normalized_market == "1x2":
        label = {
            "HOME": f"{home_team_name} to Win",
            "DRAW": "Match Draw",
            "AWAY": f"{away_team_name} to Win",
        }.get(normalized_selection, normalized_selection.title())
        return {
            "market_label": "Match Winner",
            "selection_label": label,
            "bet_label": label,
        }
    if normalized_market == "btts":
        selection_label = "Yes" if normalized_selection == "YES" else "No" if normalized_selection == "NO" else normalized_selection.title()
        return {
            "market_label": "Both Teams to Score",
            "selection_label": selection_label,
            "bet_label": f"Both Teams to Score - {selection_label}",
        }
    if normalized_market == "goals_total":
        selection_label = "Over" if normalized_selection == "OVER" else "Under" if normalized_selection == "UNDER" else normalized_selection.title()
        goal_text = f"{line_text} Goals" if line_text is not None else "Goals"
        return {
            "market_label": "Goals Total",
            "selection_label": selection_label,
            "bet_label": f"{selection_label} {goal_text}",
        }
    if normalized_market == "corner_total":
        selection_label = "Over" if normalized_selection == "OVER" else "Under" if normalized_selection == "UNDER" else normalized_selection.title()
        corner_text = f"{line_text} Corners" if line_text is not None else "Corners"
        return {
            "market_label": "Corners Total",
            "selection_label": selection_label,
            "bet_label": f"{selection_label} {corner_text}",
        }
    if normalized_market == "asian_handicap":
        team_label = home_team_name if normalized_selection == "HOME" else away_team_name if normalized_selection == "AWAY" else normalized_selection.title()
        handicap_text = _format_signed_display_line(line)
        return {
            "market_label": "Asian Handicap",
            "selection_label": team_label,
            "bet_label": team_label if handicap_text is None else f"{team_label} {handicap_text}",
        }
    selection_label = normalized_selection.title()
    bet_label = normalized_market.replace("_", " ").title()
    if line_text is not None:
        bet_label = f"{bet_label} {line_text}"
    return {
        "market_label": normalized_market.replace("_", " ").title(),
        "selection_label": selection_label,
        "bet_label": f"{bet_label} - {selection_label}",
    }


def _humanize_pick_status(row: dict[str, Any]) -> tuple[str, str]:
    notes: list[str] = []
    if row.get("paper_entry_eligible"):
        notes.append("Paper eligible")
    market_freshness = str(row.get("market_freshness") or "").strip()
    if market_freshness:
        notes.append(f"{market_freshness.replace('_', ' ').title()} market")
    day12_state = str(row.get("day12_state") or "").strip()
    if day12_state and day12_state != "NOT_APPLICABLE":
        notes.append(f"Day 12 {day12_state.replace('_', ' ').title()}")
    reason_codes = [str(code).replace("_", " ").title() for code in (row.get("reason_codes") or [])[:2]]
    if not notes:
        notes.extend(reason_codes)
    return ("BET" if row.get("paper_entry_eligible") else "NO BET", " · ".join(note for note in notes if note))


def _humanize_row_reason_codes(row: dict[str, Any]) -> str:
    reason_codes = [str(code).replace("_", " ").title() for code in (row.get("reason_codes") or [])[:2]]
    return " / ".join(reason_codes)


_MISSING_PRICE_REASON_CODES = {
    "INCOMPLETE_REFERENCE_MARKET",
    "MISSING_AVAILABLE_PRICE",
    "MISSING_REFERENCE_MARKET",
}
_UNSUPPORTED_REASON_CODES = {
    "REJECT_CUP_MARKET_NOT_ALLOWED",
    "REJECT_TEAM_LEVEL_OUT_OF_SCOPE",
}


def _current_decision_state(row: dict[str, Any]) -> str:
    decision = str(row.get("decision") or "")
    day12_state = str(row.get("day12_state") or "")
    reason_codes = {str(code) for code in (row.get("reason_codes") or [])}
    market_freshness = str(row.get("market_freshness") or row.get("freshness_state") or "")

    if row.get("paper_entry_eligible"):
        return "BET"
    if decision == "PRELIMINARY_CANDIDATE":
        if market_freshness != "FRESH":
            return "NO_BET_STALE_MARKET"
        if day12_state == "BLOCKED_MISSING_XG_DATA":
            return "BLOCKED"
        if day12_state in {"DAY12_REVIEW_REQUIRED", "REJECTED_BY_XG_VETO"}:
            return "NO_BET_SAFEGUARD"
        return "BLOCKED"
    if decision == "NO_BET":
        return "NO_BET_VALUE"
    if decision == "STALE_MARKET":
        return "NO_BET_STALE_MARKET"
    if decision == "MISSING_REQUIRED_MARKET_DATA":
        return "NO_BET_MISSING_PRICE" if reason_codes & _MISSING_PRICE_REASON_CODES else "BLOCKED"
    if decision == "REJECTED_RULE":
        return "UNSUPPORTED" if reason_codes & _UNSUPPORTED_REASON_CODES else "BLOCKED"
    return "BLOCKED"


def _decision_group(state: str) -> str:
    if state == "BET":
        return "bet"
    if state.startswith("NO_BET"):
        return "no_bet"
    if state == "UNSUPPORTED":
        return "unsupported"
    return "blocked"


def _decision_state_detail(state: str) -> str:
    return state.replace("_", " ").title()


def _safeguard_label(row: dict[str, Any]) -> str:
    day12_state = str(row.get("day12_state") or "")
    if day12_state in {"PASS", "PASSED"}:
        return "PASS"
    if day12_state == "REJECTED_BY_XG_VETO":
        return "FAIL"
    if day12_state == "DAY12_REVIEW_REQUIRED":
        return "REVIEW"
    if day12_state == "BLOCKED_MISSING_XG_DATA":
        return "BLOCKED"
    market_freshness = str(row.get("market_freshness") or "")
    if market_freshness and market_freshness != "FRESH":
        return market_freshness
    return "N/A"


@dataclass(frozen=True)
class DashboardConfig:
    repo_root: Path
    research_db_path: Path
    dashboard_db_path: Path
    paper_trading_db_path: Path
    performance_output_dir: Path
    operational_data_dir: Path
    storage_mode: str
    persistent_data_dir_configured: bool
    display_timezone: str = "Asia/Manila"
    upcoming_horizon_days: int = DEFAULT_UPCOMING_HORIZON_DAYS
    scheduler_interval_seconds: int = 300
    live_refresh_interval_seconds: int = 60
    future_refresh_interval_seconds: int = 300
    manual_refresh_cooldown_seconds: int = 30
    enable_scheduler: bool = True
    current_odds_connected: bool = False
    enable_odds_sync: bool = False
    odds_sync_once_token: str = ""
    current_xg_connected: bool = False

    @classmethod
    def default(cls) -> "DashboardConfig":
        repo_root = Path(__file__).resolve().parents[4]
        operational_paths = resolve_dashboard_operational_paths(repo_root)
        return cls(
            repo_root=repo_root,
            research_db_path=repo_root / "research" / "football_research" / "store" / "football_research.sqlite",
            dashboard_db_path=operational_paths.dashboard_db_path,
            paper_trading_db_path=operational_paths.paper_trading_db_path,
            performance_output_dir=repo_root / "research" / "product_outputs" / "football" / "performance",
            operational_data_dir=operational_paths.data_dir,
            storage_mode=operational_paths.mode,
            persistent_data_dir_configured=operational_paths.persistent_path_configured,
            upcoming_horizon_days=_env_int(DEFAULT_UPCOMING_HORIZON_ENV_VAR, DEFAULT_UPCOMING_HORIZON_DAYS),
            scheduler_interval_seconds=_env_int(DEFAULT_FUTURE_REFRESH_ENV_VAR, 300),
            live_refresh_interval_seconds=_env_int(DEFAULT_LIVE_REFRESH_ENV_VAR, 60),
            future_refresh_interval_seconds=_env_int(DEFAULT_FUTURE_REFRESH_ENV_VAR, 300),
            manual_refresh_cooldown_seconds=_env_int(DEFAULT_MANUAL_REFRESH_COOLDOWN_ENV_VAR, 30),
            enable_scheduler=_env_bool(DEFAULT_ENABLE_AUTOMATED_SYNC_ENV_VAR, True),
            current_odds_connected=_env_bool(DEFAULT_CURRENT_ODDS_CONNECTED_ENV_VAR, False),
            enable_odds_sync=_env_bool(DEFAULT_ENABLE_ODDS_SYNC_ENV_VAR, False),
            odds_sync_once_token=_env_str(DEFAULT_ODDS_SYNC_ONCE_TOKEN_ENV_VAR, ""),
            current_xg_connected=_env_bool(DEFAULT_CURRENT_XG_CONNECTED_ENV_VAR, False),
        )


class DashboardServices:
    """Coordinates fixture sync, prediction generation, and read APIs."""

    def __init__(
        self,
        config: DashboardConfig | None = None,
        *,
        fixture_provider: FixtureProvider | None = None,
        market_provider: CurrentMarketProvider | None = None,
        xg_profile_factory: Any | None = None,
    ):
        self.config = DashboardConfig.default() if config is None else config
        self.fixture_provider = fixture_provider or _default_fixture_provider()
        self.market_provider = market_provider or _default_market_provider(self.config)
        self._xg_profile_factory = xg_profile_factory
        self.snapshot: CurrentFootballModelSnapshot | None = None
        self.snapshot_error: str | None = None
        self._background_task: asyncio.Task[None] | None = None
        self._shutdown = False
        self._sync_lock = asyncio.Lock()
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
            "last_sync_duration_seconds": None,
            "sync_diagnostics": None,
            "nearest_supported_fixture": None,
            "team_resolution_counts": {
                "resolved": 0,
                "unresolved": 0,
                "model_history_missing": 0,
                "unresolved_examples": [],
                "model_history_missing_examples": [],
            },
            "last_counts": self._empty_counts(),
        }
        self._prediction_state: dict[str, Any] = {
            "state": "BLOCKED",
            "last_prediction_generated_at": None,
            "last_successful_prediction_at": None,
            "last_prediction_error": None,
            "pending_fixture_count": 0,
            "blocked_fixture_count": 0,
            "ready_fixture_count": 0,
        }
        self._market_runtime_state: dict[str, Any] = {
            "state": "NOT_CONFIGURED" if not self.market_provider.configured() else "CONNECTED",
            "automatic_odds_sync": "ENABLED" if self.config.enable_odds_sync else "DISABLED",
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
        self._one_shot_sync_state: dict[str, Any] = {
            "configured": False,
            "token_id": None,
            "state": "NOT_CONFIGURED",
            "claimed_at": None,
            "completed_at": None,
            "provider_requests": 0,
            "snapshots": 0,
            "decisions": 0,
            "paper_entries": 0,
            "error": None,
        }
        self._storage_runtime_state: dict[str, Any] = {}
        self._edge_desk_available = True
        self._edge_desk_last_error: str | None = None

    async def initialize(self) -> None:
        ensure_operational_data_dir(self.config.operational_data_dir)
        initialized_at = utc_now_iso()
        logger.info(
            "dashboard startup: storage_mode=%s data_dir=%s scheduler_enabled=%s fixture_provider=%s market_provider=%s",
            self.config.storage_mode,
            self.config.operational_data_dir,
            self.config.enable_scheduler,
            self.fixture_provider.provider_name(),
            self.market_provider.provider_name(),
        )
        with storage.connect(self.config.dashboard_db_path) as conn:
            storage.ensure_schema(conn)
            try:
                football_edge_desk.ensure_schema(conn)
                self._edge_desk_available = True
                self._edge_desk_last_error = None
            except Exception as exc:
                self._edge_desk_available = False
                self._edge_desk_last_error = (
                    f"initialization failure: {self._sanitize_edge_desk_error(exc)}"
                )
                logger.exception("edge desk shadow initialization failed; continuing in fail-open mode")
            self._load_runtime_state(conn)
        with football_paper_trading.connect(self.config.paper_trading_db_path) as conn:
            football_paper_trading.ensure_schema(conn)
        self._record_storage_runtime_state(initialized_at=initialized_at)
        self._build_snapshot()
        if self.snapshot is not None and self._edge_desk_available:
            with storage.connect(self.config.dashboard_db_path) as conn:
                try:
                    football_edge_desk.ensure_route_a_foundation(conn, artifact=self.snapshot.route_a_artifact)
                except Exception as exc:
                    self._mark_edge_desk_degraded(
                        conn,
                        reason="route a foundation registration failure",
                        exc=exc,
                        updated_at=utc_now_iso(),
                    )
                    logger.exception("edge desk route a registration failed during startup")
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
        logger.info("dashboard shutdown complete")

    def _load_runtime_state(self, conn: Any) -> None:
        provider_state = storage.get_runtime_state(conn, "provider_state")
        prediction_state = storage.get_runtime_state(conn, "prediction_state")
        market_runtime_state = storage.get_runtime_state(conn, "market_runtime_state")
        storage_runtime_state = storage.get_runtime_state(conn, "storage_runtime_state")
        one_shot_sync_state = storage.get_runtime_state(conn, "one_shot_sync_state")
        if isinstance(provider_state, dict):
            merged_provider_state = dict(self._provider_state)
            merged_provider_state.update(provider_state)
            merged_provider_state["last_counts"] = {
                **self._empty_counts(),
                **dict(provider_state.get("last_counts") or {}),
            }
            merged_provider_state["team_resolution_counts"] = {
                "resolved": 0,
                "unresolved": 0,
                "model_history_missing": 0,
                "unresolved_examples": [],
                "model_history_missing_examples": [],
                **dict(provider_state.get("team_resolution_counts") or {}),
            }
            self._provider_state.update(merged_provider_state)
        if isinstance(prediction_state, dict):
            self._prediction_state.update(prediction_state)
        if isinstance(market_runtime_state, dict):
            self._market_runtime_state.update(market_runtime_state)
        if isinstance(storage_runtime_state, dict):
            self._storage_runtime_state.update(storage_runtime_state)
        if isinstance(one_shot_sync_state, dict):
            self._one_shot_sync_state.update(one_shot_sync_state)

    def _edge_desk_shadow_state(self) -> dict[str, Any]:
        return {
            "available": self._edge_desk_available,
            "state": "READY" if self._edge_desk_available else "DEGRADED",
            "last_error": self._edge_desk_last_error,
        }

    @staticmethod
    def _sanitize_edge_desk_error(exc: Exception) -> str:
        message = " ".join(str(exc).split()).strip()
        if not message:
            return type(exc).__name__
        return f"{type(exc).__name__}: {message[:200]}"

    def _persist_storage_runtime_state(
        self,
        conn: Any,
        *,
        updated_at: str,
        initialized_at: str | None = None,
    ) -> None:
        existing = dict(self._storage_runtime_state)
        payload = {
            "first_initialized_at": existing.get("first_initialized_at") or initialized_at or updated_at,
            "last_db_initialization_at": existing.get("last_db_initialization_at") or initialized_at or updated_at,
            "dashboard_db_path": str(self.config.dashboard_db_path),
            "paper_trading_db_path": str(self.config.paper_trading_db_path),
            "research_db_path": str(self.config.research_db_path),
            "operational_db_paths": [
                {"role": "dashboard", "path": str(self.config.dashboard_db_path)},
                {"role": "paper_trading", "path": str(self.config.paper_trading_db_path)},
            ],
            "edge_desk_shadow": self._edge_desk_shadow_state(),
        }
        storage.put_runtime_state(
            conn,
            state_key="storage_runtime_state",
            state_value=payload,
            updated_at=updated_at,
        )
        self._storage_runtime_state = payload

    def _record_storage_runtime_state(self, *, initialized_at: str) -> None:
        with storage.connect(self.config.dashboard_db_path) as conn:
            self._persist_storage_runtime_state(conn, updated_at=initialized_at, initialized_at=initialized_at)

    def _mark_edge_desk_degraded(
        self,
        conn: Any,
        *,
        reason: str,
        exc: Exception,
        updated_at: str,
    ) -> None:
        self._edge_desk_available = False
        self._edge_desk_last_error = f"{reason}: {self._sanitize_edge_desk_error(exc)}"
        self._persist_storage_runtime_state(conn, updated_at=updated_at)

    def _build_snapshot(self) -> None:
        try:
            self.snapshot = build_current_football_model_snapshot(research_db_path=self.config.research_db_path)
            self.snapshot_error = None
            self._prediction_state["state"] = "READY"
            logger.info(
                "current model snapshot ready: snapshot_id=%s model_as_of=%s route_a_status=%s",
                self.snapshot.snapshot_id,
                self.snapshot.model_as_of,
                self.snapshot.diagnostics.get("route_a", {}).get("overall_status"),
            )
        except CurrentSnapshotError as exc:
            self.snapshot = None
            self.snapshot_error = str(exc)
            self._prediction_state["state"] = "BLOCKED"
            logger.warning("current model snapshot unavailable: %s", exc)

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
            "next": 0,
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
            "automatic_odds_sync": "ENABLED" if self.config.enable_odds_sync else "DISABLED",
            "quota": diagnostics.get("quota"),
        }

    def _fixture_feed_runtime_requirements(self) -> dict[str, Any]:
        provider = self.fixture_provider.provider_name()
        if provider == "football-data.org":
            competitions = os.environ.get("SPORTS_AI_FOOTBALL_DATA_COMPETITIONS", "").strip()
            return {
                "implementation_ready": "YES",
                "required_env_vars": ["SPORTS_AI_FOOTBALL_DATA_TOKEN"],
                "optional_env_vars": ["SPORTS_AI_FOOTBALL_DATA_COMPETITIONS"],
                "configuration_path": None,
                "configured_competitions": competitions or "default supported football-data.org competition map",
                "fallback_provider_available": "YES" if ApiFootballFixtureProvider().configured() else "NO",
            }
        if provider == "api-football":
            return {
                "implementation_ready": "YES",
                "required_env_vars": [
                    "SPORTS_AI_API_FOOTBALL_TOKEN",
                    "SPORTS_AI_API_FOOTBALL_LEAGUE_MAP",
                ],
                "optional_env_vars": [],
                "configuration_path": os.environ.get("SPORTS_AI_API_FOOTBALL_LEAGUE_MAP", "").strip() or None,
                "configured_competitions": "league map driven",
                "fallback_provider_available": "NO",
            }
        return {
            "implementation_ready": "UNKNOWN",
            "required_env_vars": [],
            "optional_env_vars": [],
            "configuration_path": None,
            "configured_competitions": None,
            "fallback_provider_available": "UNKNOWN",
        }

    def _odds_feed_runtime_requirements(self) -> dict[str, Any]:
        provider_probe = TheOddsApiProvider()
        reference_bookmakers = os.environ.get("SPORTS_AI_MARKET_REFERENCE_BOOKMAKERS", "").strip() or "pinnacle"
        freshness = os.environ.get("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "").strip() or "unset"
        current_provider = self.market_provider.provider_name()
        if current_provider not in {"the-odds-api", "no-odds-provider-key", "waiting-for-approved-odds-provider"}:
            return {
                "implementation_ready": "UNKNOWN",
                "required_env_vars": [],
                "optional_env_vars": [],
                "supported_markets": [],
                "reference_price_method": None,
                "freshness_rule": None,
            }
        return {
            "implementation_ready": "YES",
            "provider_key_configured": "YES" if provider_probe.configured() else "NO",
            "approval_enabled": "YES" if self.config.current_odds_connected else "NO",
            "automatic_sync_enabled": "YES" if self.config.enable_odds_sync else "NO",
            "required_env_vars": [
                "SPORTS_AI_THE_ODDS_API_KEY",
                "SPORTS_AI_CURRENT_ODDS_CONNECTED",
                "SPORTS_AI_ENABLE_ODDS_SYNC",
            ],
            "optional_env_vars": [
                "SPORTS_AI_ODDS_REGION",
                "SPORTS_AI_MARKET_REFERENCE_BOOKMAKERS",
                "SPORTS_AI_MARKET_MAX_AGE_MINUTES",
            ],
            "supported_markets": ["1x2", "goals_total", "btts"],
            "reference_price_method": f"Shin de-vig on reference bookmakers ({reference_bookmakers})",
            "freshness_rule": f"Market source timestamp must be within SPORTS_AI_MARKET_MAX_AGE_MINUTES (current: {freshness})",
        }

    def _xg_runtime_status(self) -> dict[str, Any]:
        derived_available = bool(self.snapshot is not None and self.config.research_db_path.exists())
        enabled = bool(self.config.current_xg_connected)
        return {
            "state": "AVAILABLE" if enabled else "MISSING",
            "implementation_ready": "YES",
            "enabled": "YES" if enabled else "NO",
            "derived_input_available": "YES" if derived_available else "NO",
            "source": "sports_edge_day8_historical_rolling_xg_v1" if derived_available else None,
            "path": str(self.config.research_db_path) if derived_available else None,
            "missing_requirement": None
            if enabled
            else (
                "Set SPORTS_AI_CURRENT_XG_CONNECTED=1 to enable the existing derived rolling xG safeguard input."
                if derived_available
                else "Historical research store access is required for the derived rolling xG safeguard input."
            ),
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
        logger.info(
            "automatic fixture sync scheduler started: live_refresh_seconds=%s future_refresh_seconds=%s",
            live_interval,
            future_interval,
        )
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
                logger.exception("automatic fixture sync failed: %s", exc)
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
        async with self._sync_lock:
            now_utc = datetime.now(timezone.utc)
            attempted_at = utc_now_iso()
            sync_started = datetime.now(timezone.utc)
            logger.info("fixture sync started: kind=%s include_schedule=%s", sync_kind, include_schedule)
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
                            "last_sync_duration_seconds": round(
                                (datetime.now(timezone.utc) - sync_started).total_seconds(),
                                3,
                            ),
                        }
                    )
                    storage.put_runtime_state(
                        conn,
                        state_key="provider_state",
                        state_value=self._provider_state,
                        updated_at=attempted_at,
                    )
                    logger.info("fixture sync skipped: fixture provider not configured")
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
                            "last_sync_duration_seconds": round(
                                (datetime.now(timezone.utc) - sync_started).total_seconds(),
                                3,
                            ),
                        }
                    )
                    storage.put_runtime_state(
                        conn,
                        state_key="provider_state",
                        state_value=self._provider_state,
                        updated_at=attempted_at,
                    )
                    logger.warning("fixture sync not configured: %s", exc)
                    return self._provider_state
                except FixtureProviderError as exc:
                    self._provider_state.update(
                        {
                            "configured": True,
                            "state": "DEGRADED",
                            "last_sync_at": attempted_at,
                            "last_error": str(exc),
                            "sync_diagnostics": self._provider_sync_diagnostics(),
                            "last_sync_duration_seconds": round(
                                (datetime.now(timezone.utc) - sync_started).total_seconds(),
                                3,
                            ),
                        }
                    )
                    storage.put_runtime_state(
                        conn,
                        state_key="provider_state",
                        state_value=self._provider_state,
                        updated_at=attempted_at,
                    )
                    logger.warning("fixture sync degraded: %s", exc)
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
                current_counts["next"] = len(self._future_fixture_rows(rows_with_predictions)[:8])
                current_counts["supported"] = sum(
                    1
                    for row in rows_with_predictions
                    if self.snapshot is not None and str(row["competition_id"]) in self.snapshot.supported_competitions
                )
                current_counts["unsupported_competition"] = sum(
                    1
                    for row in rows_with_predictions
                    if self.snapshot is not None and str(row["competition_id"]) not in self.snapshot.supported_competitions
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
                        "team_resolution_counts": self._team_resolution_counts(rows_with_predictions),
                        "last_counts": current_counts,
                        "last_sync_duration_seconds": round(
                            (datetime.now(timezone.utc) - sync_started).total_seconds(),
                            3,
                        ),
                    }
                )
                self._update_prediction_state(rows_with_predictions)
                self._run_market_sync(rows_with_predictions, conn)
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
                storage.put_runtime_state(
                    conn,
                    state_key="one_shot_sync_state",
                    state_value=self._one_shot_sync_state,
                    updated_at=attempted_at,
                )
            logger.info(
                "fixture sync completed: kind=%s state=%s discovered=%s live=%s today=%s upcoming=%s predictions_ready=%s blocked=%s",
                sync_kind,
                self._provider_state["state"],
                self._provider_state["last_counts"]["normalized"],
                self._provider_state["last_counts"]["live"],
                self._provider_state["last_counts"]["today"],
                self._provider_state["last_counts"]["upcoming"],
                self._prediction_state["ready_fixture_count"],
                self._prediction_state["blocked_fixture_count"],
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
            if str(row.get("market_freshness") or "UNKNOWN") != "FRESH":
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

    def _xg_profile_provider(self, row: dict[str, Any], prediction: dict[str, Any], snapshots: list[Any]):
        """Return a zero-arg provider building point-in-time xG rows for one fixture."""
        if self._xg_profile_factory is not None:
            return self._xg_profile_factory(row, prediction, snapshots)
        competition_id = str(row.get("competition_id") or "")
        research_db_path = self.config.research_db_path
        snapshot = self.snapshot
        team_resolution = prediction.get("team_resolution") or {}
        home_team = str(
            (team_resolution.get("home") or {}).get("canonical_team_name") or row.get("home_team_name") or ""
        )
        away_team = str(
            (team_resolution.get("away") or {}).get("canonical_team_name") or row.get("away_team_name") or ""
        )

        def build():
            try:
                if snapshot is None:
                    return {}, missing_xg_inspection()
                coefficients = (snapshot.conversion_coefficients or {}).get(competition_id)
                if not coefficients:
                    return {}, missing_xg_inspection()
                model_as_of = str(prediction.get("model_as_of") or "")
                decision_as_of = max(
                    model_as_of,
                    max((str(snapshot_row.captured_at) for snapshot_row in snapshots), default=model_as_of),
                )
                return build_fixture_xg_rows(
                    db_path=research_db_path,
                    competition_id=competition_id,
                    fixture_id=str(row.get("fixture_id") or ""),
                    home_team=home_team,
                    away_team=away_team,
                    decision_as_of=decision_as_of,
                    coefficients=coefficients,
                    min_window_matches=DEFAULT_XG_MIN_WINDOW_MATCHES,
                )
            except Exception:
                return {}, missing_xg_inspection()

        return build

    def _run_market_sync(self, rows_with_predictions: list[dict[str, Any]], conn: Any) -> None:
        """Run the current-market sync, honoring the one-shot checkpoint token.

        When no one-shot token is configured the normal automatic-sync gate
        applies unchanged. When a token is configured it is claimed persistently
        (atomic) before any provider request; the same token can therefore only
        ever execute one provider checkpoint, even across scheduler cycles,
        process restarts, or redeploys.
        """
        token = self.config.odds_sync_once_token.strip()
        if not token:
            self._one_shot_sync_state = {
                "configured": False,
                "token_id": None,
                "state": "NOT_CONFIGURED",
                "claimed_at": None,
                "completed_at": None,
                "provider_requests": 0,
                "snapshots": 0,
                "decisions": 0,
                "paper_entries": 0,
                "error": None,
            }
            self._sync_current_market_state(rows_with_predictions)
            return

        token_id: str | None = None
        try:
            if len(token) > 256:
                self._one_shot_sync_state = {
                    "configured": True,
                    "token_id": None,
                    "state": "BLOCKED",
                    "claimed_at": None,
                    "completed_at": None,
                    "provider_requests": 0,
                    "snapshots": 0,
                    "decisions": 0,
                    "paper_entries": 0,
                    "error": "TOKEN_TOO_LONG",
                }
                return

            token_hash = _hash_one_shot_token(token)
            token_id = token_hash[:12]

            if self.config.enable_odds_sync:
                # Automatic mode owns provider synchronization; never double-fire.
                self._one_shot_sync_state = {
                    "configured": True,
                    "token_id": token_id,
                    "state": "BLOCKED",
                    "claimed_at": None,
                    "completed_at": None,
                    "provider_requests": 0,
                    "snapshots": 0,
                    "decisions": 0,
                    "paper_entries": 0,
                    "error": "AUTOMATIC_SYNC_ENABLED",
                }
                self._sync_current_market_state(rows_with_predictions)
                return

            claimed_at = utc_now_iso()
            claimed = storage.claim_one_shot_market_sync_token(
                conn,
                token_hash=token_hash,
                token_id=token_id,
                claimed_at=claimed_at,
            )
            if not claimed:
                existing = storage.get_one_shot_market_sync_claim(conn, token_hash)
                self._one_shot_sync_state = {
                    "configured": True,
                    "token_id": token_id,
                    "state": "ALREADY_CONSUMED",
                    "claimed_at": existing["claimed_at"] if existing else None,
                    "completed_at": existing["completed_at"] if existing else None,
                    "provider_requests": int(existing["provider_requests"]) if existing else 0,
                    "snapshots": int(existing["snapshots"]) if existing else 0,
                    "decisions": int(existing["decisions"]) if existing else 0,
                    "paper_entries": int(existing["paper_entries"]) if existing else 0,
                    "error": existing["error"] if existing else None,
                }
                return

            try:
                self._sync_current_market_state(rows_with_predictions, force_once=True)
            except Exception:
                completed_at = utc_now_iso()
                error_code = "INTERNAL_ERROR"
                storage.update_one_shot_market_sync_claim(
                    conn,
                    token_hash=token_hash,
                    state="FAILED",
                    completed_at=completed_at,
                    provider_requests=0,
                    snapshots=0,
                    decisions=0,
                    paper_entries=0,
                    error=error_code,
                )
                self._one_shot_sync_state = {
                    "configured": True,
                    "token_id": token_id,
                    "state": "FAILED",
                    "claimed_at": claimed_at,
                    "completed_at": completed_at,
                    "provider_requests": 0,
                    "snapshots": 0,
                    "decisions": 0,
                    "paper_entries": 0,
                    "error": error_code,
                }
                return

            completed_at = utc_now_iso()
            diagnostics = self.market_provider.diagnostics()
            provider_requests = int(diagnostics.get("request_count") or 0)
            snapshots = int(self._market_runtime_state.get("market_snapshots") or 0)
            decisions = int(self._market_runtime_state.get("decision_count") or 0)
            paper_entries = int(self._market_runtime_state.get("paper_entry_count") or 0)
            outcome_state, error_code = _classify_one_shot_outcome(str(diagnostics.get("state") or ""))

            storage.update_one_shot_market_sync_claim(
                conn,
                token_hash=token_hash,
                state=outcome_state,
                completed_at=completed_at,
                provider_requests=provider_requests,
                snapshots=snapshots,
                decisions=decisions,
                paper_entries=paper_entries,
                error=error_code,
            )
            self._one_shot_sync_state = {
                "configured": True,
                "token_id": token_id,
                "state": outcome_state,
                "claimed_at": claimed_at,
                "completed_at": completed_at,
                "provider_requests": provider_requests,
                "snapshots": snapshots,
                "decisions": decisions,
                "paper_entries": paper_entries,
                "error": error_code,
            }
        except Exception:
            # Defensive: one-shot bookkeeping must never break the normal sync path.
            self._one_shot_sync_state = {
                "configured": True,
                "token_id": token_id,
                "state": "FAILED",
                "claimed_at": None,
                "completed_at": utc_now_iso(),
                "provider_requests": 0,
                "snapshots": 0,
                "decisions": 0,
                "paper_entries": 0,
                "error": "INTERNAL_ERROR",
            }

    def _sync_current_market_state(
        self,
        rows_with_predictions: list[dict[str, Any]],
        *,
        force_once: bool = False,
    ) -> None:
        market_state = self._market_provider_state()
        now_utc = datetime.now(timezone.utc)
        evaluations: dict[str, Any] = {}
        if not market_state["configured"]:
            self._market_runtime_state.update(
                {
                    "state": "NOT_CONFIGURED",
                    "automatic_odds_sync": "ENABLED" if self.config.enable_odds_sync else "DISABLED",
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

        if not self.config.current_odds_connected and not force_once:
            self._market_runtime_state.update(
                {
                    "state": "SYNC_DISABLED",
                    "automatic_odds_sync": "DISABLED",
                    "last_sync_at": utc_now_iso(),
                    "last_error": "Set SPORTS_AI_CURRENT_ODDS_CONNECTED=1 to enable the approved live odds path.",
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

        if not self.config.enable_odds_sync and not force_once:
            self._market_runtime_state.update(
                {
                    "state": "SYNC_DISABLED",
                    "automatic_odds_sync": "DISABLED",
                    "last_sync_at": utc_now_iso(),
                    "last_error": "Set SPORTS_AI_ENABLE_ODDS_SYNC=1 to sync current market snapshots.",
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
                    xg_profile_provider=self._xg_profile_provider(row, prediction, snapshots)
                    if self.config.current_xg_connected
                    else None,
                )
                for decision_row in evaluation.get("decision_rows") or []:
                    provider_market_id = decision_row.get("best_available_provider_market_id")
                    if provider_market_id is not None:
                        decision_row["entry_snapshot_id"] = entry_snapshot_ids.get(str(provider_market_id))
                    current_state = _current_decision_state(decision_row)
                    decision_row["current_decision_state"] = current_state
                    decision_row["current_decision_group"] = _decision_group(current_state)
                    decision_row["safeguard_label"] = _safeguard_label(decision_row)
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
                "state": (
                    "READY"
                    if evaluations and any((value.get("decision_rows") or []) for value in evaluations.values())
                    else market_state["state"]
                ),
                "automatic_odds_sync": "ENABLED" if self.config.enable_odds_sync else "DISABLED",
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
            payload = attach_edge_desk_shadow_bundle(
                {
                    "prediction_id": f"prediction_blocked_{fixture.fixture_id}",
                    "fixture_id": fixture.fixture_id,
                    "generated_at": utc_now_iso(),
                    "model_as_of": None,
                    "model_snapshot_id": None,
                    "prediction_version": f"blocked|{fixture.fixture_id}",
                    "prediction_locked_at": fixture.kickoff_utc,
                    "version_key": f"blocked|{fixture.fixture_id}",
                    "prediction_status": PREDICTION_STATUS_BLOCKED,
                    "blockers": ["MODEL_SNAPSHOT_UNAVAILABLE"],
                    "warnings": [self.snapshot_error] if self.snapshot_error else [],
                    "manager_decision_status": "MARKET_DATA_NOT_CONNECTED",
                    "safeguard_status": "BLOCKED",
                    "safeguard_reason": "POINT-IN-TIME XG NOT AVAILABLE",
                },
                fixture=fixture,
                snapshot=None,
            )
            storage.save_prediction(conn, payload, updated_at=utc_now_iso())
            self._record_edge_desk_shadow(conn, fixture=fixture, prediction=payload)
            return payload

        version_key = self._prediction_version_key(fixture, self.snapshot)
        existing = storage.fetch_prediction_by_version_key(conn, version_key)
        if existing is not None:
            self._record_edge_desk_shadow(conn, fixture=fixture, prediction=existing)
            return existing

        kickoff_utc = parse_iso_datetime(fixture.kickoff_utc)
        current_time_utc = datetime.now(timezone.utc)
        latest = storage.fetch_latest_prediction_for_fixture(conn, fixture.fixture_id)
        if latest is not None and kickoff_utc <= current_time_utc:
            self._record_edge_desk_shadow(conn, fixture=fixture, prediction=latest)
            return latest
        if kickoff_utc <= current_time_utc:
            payload = attach_edge_desk_shadow_bundle(
                {
                    "prediction_id": f"prediction_locked_{fixture.fixture_id}",
                    "fixture_id": fixture.fixture_id,
                    "generated_at": utc_now_iso(),
                    "model_as_of": self.snapshot.model_as_of,
                    "model_snapshot_id": self.snapshot.snapshot_id,
                    "prediction_version": f"missed_lock|{fixture.fixture_id}",
                    "prediction_locked_at": fixture.kickoff_utc,
                    "version_key": f"missed_lock|{fixture.fixture_id}",
                    "prediction_status": PREDICTION_STATUS_BLOCKED,
                    "blockers": ["PREDICTION_AFTER_KICKOFF_BLOCKED"],
                    "warnings": ["fixture passed kickoff before a pre-match prediction version was persisted"],
                    "manager_decision_status": "MARKET_DATA_NOT_CONNECTED",
                    "safeguard_status": "BLOCKED",
                    "safeguard_reason": "POINT-IN-TIME XG NOT AVAILABLE",
                    "kickoff_utc": fixture.kickoff_utc,
                    "competition_id": fixture.competition_id,
                    "competition_name": fixture.competition_name,
                    "home_team_name": fixture.home_team_name,
                    "away_team_name": fixture.away_team_name,
                },
                fixture=fixture,
                snapshot=self.snapshot,
            )
            storage.save_prediction(conn, payload, updated_at=utc_now_iso())
            self._record_edge_desk_shadow(conn, fixture=fixture, prediction=payload)
            return payload

        context = PredictionContext(fixture=fixture, snapshot=self.snapshot)
        prediction = build_prediction_for_fixture(context)
        home_resolution = (prediction.get("team_resolution") or {}).get("home")
        away_resolution = (prediction.get("team_resolution") or {}).get("away")
        for resolution in (home_resolution, away_resolution):
            if isinstance(resolution, dict):
                storage.record_team_resolution(conn, resolution, updated_at=utc_now_iso())
        storage.save_prediction(conn, prediction, updated_at=utc_now_iso())
        self._record_edge_desk_shadow(conn, fixture=fixture, prediction=prediction)
        return prediction

    def _record_edge_desk_shadow(
        self,
        conn: Any,
        *,
        fixture: NormalizedFixture,
        prediction: dict[str, Any],
    ) -> None:
        if not self._edge_desk_available:
            return
        try:
            football_edge_desk.record_shadow_prediction_bundle(
                conn,
                fixture=fixture.to_dict(timezone_name="UTC"),
                prediction=prediction,
            )
        except Exception as exc:
            self._mark_edge_desk_degraded(
                conn,
                reason="runtime shadow-write failure",
                exc=exc,
                updated_at=utc_now_iso(),
            )
            logger.exception("edge desk shadow persistence failed for fixture_id=%s", fixture.fixture_id)

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
        if "COMPETITION_UNSUPPORTED" in blockers or "MODEL_NOT_AVAILABLE" in blockers:
            return "COMPETITION_UNSUPPORTED"
        if {"HOME_TEAM_IDENTITY_UNRESOLVED", "AWAY_TEAM_IDENTITY_UNRESOLVED"} & blockers or "PREDICTION_BLOCKED_TEAM_IDENTITY" in blockers:
            return "TEAM_IDENTITY_UNRESOLVED"
        if {"HOME_MODEL_HISTORY_MISSING", "AWAY_MODEL_HISTORY_MISSING"} & blockers:
            return "MODEL_HISTORY_MISSING"
        if {
            "MODEL_SNAPSHOT_UNAVAILABLE",
            "CURRENT_MODEL_SNAPSHOT_UNAVAILABLE",
            "MODEL_STATE_UNAVAILABLE",
            "CURRENT_STATE_UNAVAILABLE",
        } & blockers:
            return "CURRENT_MODEL_STATE_UNAVAILABLE"
        if "PREDICTION_AFTER_KICKOFF_BLOCKED" in blockers or "PREDICTION_LOCKED_AFTER_KICKOFF" in blockers:
            return "PREDICTION_AFTER_KICKOFF_BLOCKED"
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

    def _supported_fixture_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.snapshot is None:
            return []
        return [
            row
            for row in rows
            if str(row["competition_id"]) in self.snapshot.supported_competitions
            and str(row["status"]) not in FIXTURE_TERMINAL_STATUSES
        ]

    def _team_resolution_example(
        self,
        *,
        row: dict[str, Any],
        side: str,
        resolution: dict[str, Any],
        reason: str,
        missing_statistics: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "fixture_id": str(row["fixture_id"]),
            "competition_id": str(row["competition_id"]),
            "competition_name": str(row["competition_name"]),
            "side": side.upper(),
            "provider_team_id": resolution.get("provider_team_id"),
            "provider_team_name": str(resolution.get("provider_team_name") or ""),
            "reason": reason,
            "diagnostic_category": resolution.get("diagnostic_category"),
            "suggested_canonical_name": resolution.get("suggested_canonical_name"),
            "canonical_candidates": list(resolution.get("canonical_candidates") or []),
            "missing_statistics": list(missing_statistics or []),
        }

    def _current_team_resolution_state(
        self,
        *,
        row: dict[str, Any],
        side: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if self.snapshot is None:
            return None, None
        team_id_key = f"{side}_team_id"
        team_name_key = f"{side}_team_name"
        resolution = self.snapshot.resolve_team(
            provider=str(row["provider"]),
            competition_id=str(row["competition_id"]),
            provider_team_id=None if row.get(team_id_key) is None else str(row[team_id_key]),
            provider_team_name=str(row.get(team_name_key) or ""),
        )
        resolution_payload = resolution.to_dict()
        if not resolution.is_resolved:
            return resolution_payload, None
        history = self.snapshot.team_model_history_status(
            competition_id=str(row["competition_id"]),
            canonical_team_id=str(resolution.canonical_team_id),
            venue=side,
        )
        return resolution_payload, history

    def _team_resolution_counts(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        resolved = 0
        unresolved = 0
        model_history_missing = 0
        unresolved_examples: list[dict[str, Any]] = []
        model_history_examples: list[dict[str, Any]] = []
        unresolved_seen: set[tuple[str, str, str]] = set()
        model_history_seen: set[tuple[str, str, str]] = set()
        for row in self._supported_fixture_rows(rows):
            for side in ("home", "away"):
                resolution, history_row = self._current_team_resolution_state(row=row, side=side)
                if not isinstance(resolution, dict):
                    continue
                canonical_team_id = resolution.get("canonical_team_id")
                if not canonical_team_id:
                    unresolved += 1
                    seen_key = (
                        str(row["competition_id"]),
                        str(resolution.get("provider_team_id") or resolution.get("provider_team_name") or ""),
                        side,
                    )
                    if seen_key not in unresolved_seen and len(unresolved_examples) < 8:
                        unresolved_examples.append(
                            self._team_resolution_example(
                                row=row,
                                side=side,
                                resolution=resolution,
                                reason=str(resolution.get("reason") or "team identity unresolved"),
                            )
                        )
                        unresolved_seen.add(seen_key)
                    continue
                history_available = None
                missing_statistics: list[str] = []
                if isinstance(history_row, dict):
                    history_available = bool(history_row.get("available"))
                    missing_statistics = [str(value) for value in history_row.get("missing_statistics") or []]
                if history_available is False or resolution.get("model_history_available") is False:
                    model_history_missing += 1
                    seen_key = (
                        str(row["competition_id"]),
                        str(resolution.get("provider_team_id") or resolution.get("provider_team_name") or ""),
                        side,
                    )
                    if seen_key not in model_history_seen and len(model_history_examples) < 8:
                        model_history_examples.append(
                            self._team_resolution_example(
                                row=row,
                                side=side,
                                resolution=resolution,
                                reason=str(
                                    resolution.get("model_history_reason")
                                    or "resolved identity has no usable current competition history"
                                ),
                                missing_statistics=missing_statistics,
                            )
                        )
                        model_history_seen.add(seen_key)
                    continue
                resolved += 1
        return {
            "resolved": resolved,
            "unresolved": unresolved,
            "model_history_missing": model_history_missing,
            "unresolved_examples": unresolved_examples,
            "model_history_missing_examples": model_history_examples,
        }

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
        generated_times = []
        successful_times = []
        latest_error: str | None = None
        latest_error_generated_at: str | None = None
        ready_count = 0
        blocked_count = 0
        pending_count = 0
        for row in rows_with_predictions:
            prediction = row.get("prediction")
            if isinstance(prediction, dict) and prediction.get("generated_at"):
                generated_times.append(str(prediction["generated_at"]))
                if str(prediction.get("prediction_status")) == PREDICTION_STATUS_READY:
                    successful_times.append(str(prediction["generated_at"]))
                elif prediction.get("warnings") or prediction.get("blockers"):
                    generated_at = str(prediction["generated_at"])
                    if latest_error_generated_at is None or generated_at >= latest_error_generated_at:
                        latest_error_generated_at = generated_at
                        latest_error = ", ".join(
                            str(value)
                            for value in [*(prediction.get("blockers") or []), *(prediction.get("warnings") or [])]
                            if value
                        ) or str(prediction.get("prediction_status") or "PREDICTION_BLOCKED")
            eligibility = self._prediction_eligibility_status(prediction)
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
                "last_successful_prediction_at": None if not successful_times else max(successful_times),
                "last_prediction_error": latest_error,
                "pending_fixture_count": pending_count,
                "blocked_fixture_count": blocked_count,
                "ready_fixture_count": ready_count,
            }
        )

    def _database_reachable(self, path: Path, *, initialize_schema: bool) -> bool:
        try:
            if initialize_schema:
                with storage.connect(path) as conn:
                    storage.ensure_schema(conn)
            else:
                with football_paper_trading.connect(path) as conn:
                    football_paper_trading.ensure_schema(conn)
            return True
        except Exception:
            return False

    @staticmethod
    def _path_writable(path: Path) -> bool:
        try:
            return os.access(path, os.W_OK)
        except Exception:
            return False

    def _storage_status(self) -> dict[str, Any]:
        dashboard_db_reachable = self._database_reachable(self.config.dashboard_db_path, initialize_schema=True)
        paper_trading_db_reachable = self._database_reachable(self.config.paper_trading_db_path, initialize_schema=False)
        research_db_reachable = Path(self.config.research_db_path).exists()
        operational_paths = list(self._storage_runtime_state.get("operational_db_paths") or [])
        return {
            "mode": self.config.storage_mode,
            "persistence_state": "PERSISTENT" if self.config.persistent_data_dir_configured else "NOT_PERSISTENT",
            "data_directory": str(self.config.operational_data_dir),
            "data_directory_exists": self.config.operational_data_dir.exists(),
            "data_directory_writable": self._path_writable(self.config.operational_data_dir),
            "persistent_path_configured": "YES" if self.config.persistent_data_dir_configured else "NO",
            "dashboard_db_exists": self.config.dashboard_db_path.exists(),
            "dashboard_db_path": str(self.config.dashboard_db_path),
            "dashboard_db_reachable": dashboard_db_reachable,
            "paper_trading_db_exists": self.config.paper_trading_db_path.exists(),
            "paper_trading_db_path": str(self.config.paper_trading_db_path),
            "paper_trading_db_reachable": paper_trading_db_reachable,
            "research_db_exists": Path(self.config.research_db_path).exists(),
            "research_db_path": str(self.config.research_db_path),
            "research_db_reachable": research_db_reachable,
            "last_db_initialization_at": self._storage_runtime_state.get("last_db_initialization_at"),
            "first_initialized_at": self._storage_runtime_state.get("first_initialized_at"),
            "operational_db_paths": operational_paths,
            "edge_desk_shadow": dict(self._storage_runtime_state.get("edge_desk_shadow") or {}),
            "mutable_storage_class": "OPERATIONAL_RUNTIME",
            "immutable_storage_class": "RESEARCH_REFERENCE",
        }

    def _nearest_supported_fixture(self, rows_with_predictions: list[dict[str, Any]]) -> dict[str, Any] | None:
        now_utc = datetime.now(timezone.utc)
        candidates = []
        for row in rows_with_predictions:
            kickoff_utc = parse_iso_datetime(str(row["kickoff_utc"]))
            if kickoff_utc <= now_utc:
                continue
            if self._prediction_eligibility_status(row.get("prediction")) == "COMPETITION_UNSUPPORTED":
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
        if top_market_row is not None:
            current_state = str(top_market_row.get("current_decision_state") or _current_decision_state(top_market_row))
            top_market_row["current_decision_state"] = current_state
            top_market_row["current_decision_group"] = _decision_group(current_state)
            top_market_row["safeguard_label"] = _safeguard_label(top_market_row)
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
                "current_decision_state": top_market_row.get("current_decision_state"),
                "day12_state": top_market_row.get("day12_state"),
                "safeguard_label": top_market_row.get("safeguard_label"),
                "market_family": top_market_row.get("market_family"),
                "selection": top_market_row.get("selection"),
                "line": top_market_row.get("line"),
            },
        }

    def _dashboard_agent_statuses(self) -> list[dict[str, str]]:
        route_a_diagnostics = {} if self.snapshot is None else dict(self.snapshot.diagnostics.get("route_a") or {})
        route_a_status = str(route_a_diagnostics.get("overall_status") or "UNAVAILABLE")
        route_a_note = "Rejected clean validation artifact remains inactive."
        if route_a_status == "PENDING_INDEPENDENT_REVIEW":
            route_a_note = "Validated artifact is awaiting independent review before activation."
        elif route_a_status == "SUPPORTED":
            route_a_note = "Approved artifact is available for overlapping markets."
        elif route_a_status == "UNAVAILABLE":
            route_a_note = "No verified Route A runtime artifact is currently loaded."
        return [
            {
                "agent_id": football_edge_desk.ROUTE_B_COMPOSITE_AGENT_ID,
                "label": "Route B Composite",
                "status": football_edge_desk.ACTIVE_AGENT_STATUS,
                "note": "Only active Football predictive agent in runtime.",
            },
            {
                "agent_id": "ROUTE_A_DIRECT_CLASSIFIER",
                "label": "Route A Direct Classifier",
                "status": route_a_status,
                "note": route_a_note,
            },
            {
                "agent_id": "AGENT_3",
                "label": "Agent 3",
                "status": "NOT IMPLEMENTED",
                "note": "Not implemented yet.",
            },
        ]

    def _build_best_pick_rows(self, fixture_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        fixture_map = {
            str(row["fixture_id"]): self.serialize_fixture_row(row)
            for row in fixture_rows
        }
        best_picks: list[dict[str, Any]] = []
        for evaluation in self._market_evaluation_map().values():
            for row in evaluation.get("decision_rows") or []:
                current_state = str(row.get("current_decision_state") or _current_decision_state(row))
                if current_state != "BET":
                    continue
                fixture = fixture_map.get(str(row["fixture_id"]))
                if fixture is None:
                    continue
                if str(fixture.get("status")) not in FIXTURE_PREMATCH_STATUSES:
                    continue
                labels = _humanize_market_selection(
                    market_family=str(row["market_family"]),
                    selection=str(row["selection"]),
                    line=None if row.get("line") is None else float(row["line"]),
                    home_team_name=str(fixture["home_team_name"]),
                    away_team_name=str(fixture["away_team_name"]),
                )
                bet_state, risk_summary = _humanize_pick_status(row)
                best_picks.append(
                    {
                        "fixture_id": str(row["fixture_id"]),
                        "competition_id": str(row["competition_id"]),
                        "competition_name": str(row["competition_name"]),
                        "kickoff_utc": str(fixture["kickoff_utc"]),
                        "kickoff_local": str(fixture["kickoff_local"]),
                        "kickoff_time_local": parse_iso_datetime(str(fixture["kickoff_utc"]))
                        .astimezone(ZoneInfo(self.config.display_timezone))
                        .strftime("%H:%M"),
                        "home_team_name": str(fixture["home_team_name"]),
                        "away_team_name": str(fixture["away_team_name"]),
                        "market_family": str(row["market_family"]),
                        "market_label": labels["market_label"],
                        "selection_label": labels["selection_label"],
                        "bet_label": labels["bet_label"],
                        "line": None if row.get("line") is None else float(row["line"]),
                        "model_probability": float(row["model_probability"]),
                        "fair_odds": None
                        if row.get("model_fair_decimal_price") is None
                        else float(row["model_fair_decimal_price"]),
                        "market_odds": None if row.get("best_available_price") is None else float(row["best_available_price"]),
                        "edge": None if row.get("settlement_aware_edge") is None else float(row["settlement_aware_edge"]),
                        "bookmaker": None if row.get("best_available_bookmaker") is None else str(row["best_available_bookmaker"]),
                        "market_timestamp": row.get("best_available_source_timestamp"),
                        "minimum_acceptable_odds": row.get("minimum_acceptable_price"),
                        "bet_state": bet_state,
                        "risk_status": _safeguard_label(row),
                        "risk_summary": risk_summary,
                        "market_freshness": None if row.get("market_freshness") is None else str(row["market_freshness"]),
                        "paper_qualification_state": "QUALIFIED",
                        "current_decision_state": current_state,
                        "day12_state": str(row.get("day12_state") or "NOT_APPLICABLE"),
                        "decision": str(row["decision"]),
                        "reason_codes": list(row.get("reason_codes") or []),
                    }
                )
        return sorted(
            best_picks,
            key=lambda row: (
                -float(row.get("edge") or 0.0),
                str(row["kickoff_utc"]),
                str(row["fixture_id"]),
                str(row["market_family"]),
                str(row["bet_label"]),
            ),
        )

    def _working_row_status(self, fixture: dict[str, Any]) -> tuple[str, str]:
        status = str(fixture.get("status") or "")
        if status in FIXTURE_LIVE_STATUSES:
            elapsed_minute = fixture.get("elapsed_minute")
            if elapsed_minute is not None:
                try:
                    minute = int(elapsed_minute)
                except (TypeError, ValueError):
                    minute = None
                if minute is not None:
                    return (f"LIVE {minute}'", "live")
            return ("LIVE", "live")
        if status == FIXTURE_STATUS_FINISHED:
            return ("FINAL", "final")
        return ("PENDING", "pending")

    def _build_today_working_rows(self, fixture_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        today_fixture_map = {
            str(row["fixture_id"]): self.serialize_fixture_row(row)
            for row in self._filter_fixture_rows(bucket="today", fixture_rows=fixture_rows)
        }
        working_rows: list[dict[str, Any]] = []
        for evaluation in self._market_evaluation_map().values():
            for row in evaluation.get("decision_rows") or []:
                fixture = today_fixture_map.get(str(row["fixture_id"]))
                if fixture is None:
                    continue
                if str(fixture.get("status")) in FIXTURE_TERMINAL_STATUSES:
                    continue
                current_state = str(row.get("current_decision_state") or _current_decision_state(row))
                decision_group = _decision_group(current_state)
                labels = _humanize_market_selection(
                    market_family=str(row["market_family"]),
                    selection=str(row["selection"]),
                    line=None if row.get("line") is None else float(row["line"]),
                    home_team_name=str(fixture["home_team_name"]),
                    away_team_name=str(fixture["away_team_name"]),
                )
                if decision_group == "bet":
                    desk_label = "BET"
                elif decision_group == "no_bet":
                    desk_label = "NO BET"
                elif decision_group == "unsupported":
                    desk_label = "UNSUPPORTED"
                else:
                    desk_label = "BLOCKED"
                _, risk_summary = _humanize_pick_status(row)
                status_label, status_tone = self._working_row_status(fixture)
                market_odds = None if row.get("best_available_price") is None else float(row["best_available_price"])
                implied_probability = None
                if market_odds is not None and market_odds > 0:
                    implied_probability = 1.0 / market_odds
                working_rows.append(
                    {
                        "fixture_id": str(row["fixture_id"]),
                        "kickoff_utc": str(fixture["kickoff_utc"]),
                        "kickoff_local": str(fixture["kickoff_local"]),
                        "kickoff_time_local": parse_iso_datetime(str(fixture["kickoff_utc"]))
                        .astimezone(ZoneInfo(self.config.display_timezone))
                        .strftime("%H:%M"),
                        "competition_name": str(fixture["competition_name"]),
                        "home_team_name": str(fixture["home_team_name"]),
                        "away_team_name": str(fixture["away_team_name"]),
                        "market_label": labels["market_label"],
                        "bet_label": labels["bet_label"],
                        "selection_label": labels["selection_label"],
                        "model_probability": float(row["model_probability"]),
                        "market_odds": market_odds,
                        "fair_odds": None
                        if row.get("model_fair_decimal_price") is None
                        else float(row["model_fair_decimal_price"]),
                        "implied_probability": implied_probability,
                        "edge": None if row.get("settlement_aware_edge") is None else float(row["settlement_aware_edge"]),
                        "bookmaker": None if row.get("best_available_bookmaker") is None else str(row["best_available_bookmaker"]),
                        "market_timestamp": row.get("best_available_source_timestamp"),
                        "minimum_acceptable_odds": row.get("minimum_acceptable_price"),
                        "market_freshness": None if row.get("market_freshness") is None else str(row["market_freshness"]),
                        "confidence_label": _safeguard_label(row),
                        "risk_summary": risk_summary,
                        "desk_label": desk_label,
                        "desk_note": _humanize_row_reason_codes(row) or _decision_state_detail(current_state),
                        "current_decision_state": current_state,
                        "current_decision_group": decision_group,
                        "status_label": status_label,
                        "status_tone": status_tone,
                        "is_actionable": current_state == "BET",
                        "is_featured": False,
                    }
                )
        working_rows.sort(
            key=lambda row: (
                0
                if row["current_decision_group"] == "bet"
                else 1
                if row["current_decision_group"] == "no_bet"
                else 2,
                row["kickoff_utc"],
                -float(row.get("edge") or 0.0),
                str(row["fixture_id"]),
                str(row["bet_label"]),
            )
        )
        for row in working_rows:
            if row["is_actionable"]:
                row["is_featured"] = True
                break
        return working_rows

    @staticmethod
    def _glance_metrics(
        *,
        analysed_count: int,
        working_rows: list[dict[str, Any]],
        approved_count: int,
        paper_entry_count: int,
        live_count: int,
        pending_count: int,
    ) -> list[dict[str, str]]:
        return [
            {"label": "Analysed", "value": str(analysed_count), "note": "today fixtures"},
            {"label": "Predictions", "value": str(len(working_rows)), "note": "desk rows"},
            {"label": "Approved", "value": str(approved_count), "note": "qualified"},
            {"label": "Suggested Bets", "value": str(paper_entry_count), "note": "paper tracked"},
            {"label": "Live", "value": str(live_count), "note": "in play"},
            {"label": "Pending", "value": str(pending_count), "note": "awaiting kickoff"},
        ]

    @staticmethod
    def _desk_funnel(*, working_rows: list[dict[str, Any]], paper_entry_count: int) -> list[dict[str, str]]:
        bet_count = sum(1 for row in working_rows if row["is_actionable"])
        no_bet_count = sum(1 for row in working_rows if not row["is_actionable"])
        return [
            {"label": "Desk rows", "value": str(len(working_rows)), "note": "today working surface"},
            {"label": "Suggested bets", "value": str(paper_entry_count), "note": "paper entries created"},
            {"label": "BET rows", "value": str(bet_count), "note": "actionable now"},
            {"label": "NO BET rows", "value": str(no_bet_count), "note": "tracked, not actionable"},
        ]

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
        best_picks = self._build_best_pick_rows(fixture_rows)
        today_working_rows = self._build_today_working_rows(fixture_rows)
        other_decision_rows = [row for row in today_working_rows if not row["is_actionable"]]
        system_status = self.get_system_status()
        paper_entry_count = int(system_status["market_sync"].get("paper_entry_count") or 0)
        live_working_count = sum(1 for row in today_working_rows if row["status_tone"] == "live")
        pending_working_count = sum(1 for row in today_working_rows if row["status_tone"] == "pending")
        return {
            "live": live_rows,
            "today": today_rows,
            "upcoming": upcoming_rows[:24],
            "next_matches": next_rows,
            "prediction_watchlist": prediction_watchlist,
            "best_picks": best_picks,
            "best_picks_summary": {
                "count": len(best_picks),
                "empty_message": "No qualified picks currently meet the model, value and risk requirements.",
                "source": "Current qualified BET rows.",
            },
            "today_working_rows": today_working_rows,
            "other_decision_rows": other_decision_rows,
            "today_working_summary": {
                "count": len(today_working_rows),
                "bet_count": sum(1 for row in today_working_rows if row["is_actionable"]),
                "no_bet_count": sum(1 for row in today_working_rows if not row["is_actionable"]),
                "empty_message": "No current market decision rows are available for today's Football board.",
            },
            "other_decision_summary": {
                "count": len(other_decision_rows),
                "blocked_count": sum(1 for row in other_decision_rows if row["current_decision_group"] == "blocked"),
                "no_bet_count": sum(1 for row in other_decision_rows if row["current_decision_group"] == "no_bet"),
                "unsupported_count": sum(1 for row in other_decision_rows if row["current_decision_group"] == "unsupported"),
                "empty_message": "No additional Football desk decisions are currently on the board.",
            },
            "glance_metrics": self._glance_metrics(
                analysed_count=int((system_status["fixture_feed"].get("counts") or {}).get("today") or len(today_rows)),
                working_rows=today_working_rows,
                approved_count=len(best_picks),
                paper_entry_count=paper_entry_count,
                live_count=live_working_count,
                pending_count=pending_working_count,
            ),
            "desk_funnel": self._desk_funnel(
                working_rows=today_working_rows,
                paper_entry_count=paper_entry_count,
            ),
            "agent_statuses": self._dashboard_agent_statuses(),
            "system_status": system_status,
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
                current_state = str(row.get("current_decision_state") or _current_decision_state(row))
                row["current_decision_state"] = current_state
                row["current_decision_group"] = _decision_group(current_state)
                row["safeguard_label"] = _safeguard_label(row)
                if current_state == "BET":
                    preliminary.append(row)
                elif current_state in {"NO_BET_VALUE", "NO_BET_SAFEGUARD"}:
                    no_bets.append(row)
                else:
                    blocked.append(row)
        return {
            "connected": bool(
                market_state["configured"]
                and self.config.current_odds_connected
                and (self.config.enable_odds_sync or bool(self.config.odds_sync_once_token.strip()))
            ),
            "reason": self._market_runtime_state.get("last_error")
            or market_state["reason"]
            or "Current market decision service unavailable until a current odds provider is configured.",
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
        fixture_requirements = self._fixture_feed_runtime_requirements()
        odds_requirements = self._odds_feed_runtime_requirements()
        xg_status = self._xg_runtime_status()
        summary_payload = self._load_json_file(self._performance_summary_path())
        _, paper_status = self._paper_positions()
        performance_status = self._performance_status(summary_payload, paper_status)
        storage_state = self._storage_status()
        team_resolution_state = dict(
            self._provider_state.get(
                "team_resolution_counts",
                {
                    "resolved": 0,
                    "unresolved": 0,
                    "model_history_missing": 0,
                    "unresolved_examples": [],
                    "model_history_missing_examples": [],
                },
            )
        )
        team_resolution_state.setdefault("resolved_count", int(team_resolution_state.get("resolved", 0)))
        team_resolution_state.setdefault("unresolved_count", int(team_resolution_state.get("unresolved", 0)))
        team_resolution_state.setdefault(
            "model_history_missing_count",
            int(team_resolution_state.get("model_history_missing", 0)),
        )
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
                "last_sync_duration_seconds": self._provider_state["last_sync_duration_seconds"],
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
                "requirements": fixture_requirements,
            },
            "current_fixture_state": {
                "fixtures_discovered": int((self._provider_state.get("last_counts") or {}).get("normalized", 0)),
                "supported_fixtures": int((self._provider_state.get("last_counts") or {}).get("supported", 0)),
                "live_count": int((self._provider_state.get("last_counts") or {}).get("live", 0)),
                "today_count": int((self._provider_state.get("last_counts") or {}).get("today", 0)),
                "upcoming_count": int((self._provider_state.get("last_counts") or {}).get("upcoming", 0)),
                "next_count": int((self._provider_state.get("last_counts") or {}).get("next", 0)),
            },
            "team_resolution": team_resolution_state,
            "prediction_engine": {
                "state": self._prediction_state["state"],
                "last_prediction_generated_at": self._prediction_state["last_prediction_generated_at"],
                "last_successful_prediction_at": self._prediction_state["last_successful_prediction_at"],
                "last_prediction_error": self._prediction_state["last_prediction_error"],
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
                "requirements": odds_requirements,
            },
            "storage": storage_state,
            "market_sync": dict(self._market_runtime_state),
            "one_shot_sync": dict(self._one_shot_sync_state),
            "xg": xg_status,
            "safeguards": {
                "state": "READY" if self.config.current_xg_connected else "BLOCKED",
                "xg_availability": "AVAILABLE" if self.config.current_xg_connected else "DERIVED_XG_DISABLED",
                "blocked_reason": xg_status["missing_requirement"],
            },
            "dashboard_ui_ready": "YES",
            "historical_validation": performance_status["historical_validation"],
            "market_provider": market_state,
            "paper_tracking": {
                "ledger_ready": "YES" if performance_status["paper_ledger"]["state"] == "READY" else "NO",
                "waiting_for_market_provider": "YES" if not market_state["configured"] else "NO",
                "paper_bets": performance_status["paper_ledger"]["paper_bets"],
                "open_positions": performance_status["paper_ledger"]["open_positions"],
                "settled_positions": performance_status["paper_ledger"]["settled_positions"],
            },
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
            if market_state["configured"] and self._provider_state["configured"] and self.config.current_odds_connected
            else "NO",
            "current_paper_tracking_active": "YES"
            if int(self._market_runtime_state.get("paper_entry_count") or 0) > 0
            else "NO",
            "live_paper_pipeline_ready": "YES"
            if (
                market_state["configured"]
                and self._provider_state["configured"]
                and self.config.current_odds_connected
                and self.config.enable_odds_sync
                and self.config.current_xg_connected
            )
            else "NO",
        }

    def get_health_payload(self) -> dict[str, Any]:
        storage_state = self._storage_status()
        snapshot_ready = self.snapshot is not None
        return {
            "status": "ok",
            "timestamp_utc": utc_now_iso(),
            "fixture_provider_state": self._provider_state["state"],
            "prediction_snapshot_ready": snapshot_ready,
            "dashboard_db_reachable": storage_state["dashboard_db_reachable"],
            "paper_trading_db_reachable": storage_state["paper_trading_db_reachable"],
            "storage_mode": storage_state["mode"],
            "persistent_path_configured": storage_state["persistent_path_configured"],
        }
