"""Application services for the operational football dashboard."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import logging
import os
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..football_market_focus import configured_live_market_focus
from ..market_data.api_football import ApiFootballOddsProvider
from ..market_data.providers import MarketProvider as CurrentMarketProvider
from ..market_data.providers import NotConfiguredMarketProvider
from ..market_data.the_odds_api import TheOddsApiProvider
from ..storage import (
    football_canonical_fixture,
    football_edge_desk,
    football_paper_trading,
    football_portfolio_selection,
    football_prospective_cohort,
)
from . import storage
from .current_market import build_fixture_market_evaluation
from .day12_rolling_xg import DEFAULT_XG_MIN_WINDOW_MATCHES, build_fixture_xg_rows, missing_xg_inspection
from .fixture_provider import (
    API_FOOTBALL_PROVIDER,
    ApiFootballFixtureProvider,
    AuthoritativeFixtureStatistics,
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
    normalize_text_key,
    parse_iso_datetime,
    utc_now_iso,
)
from .prediction_service import PredictionContext, attach_edge_desk_shadow_bundle, build_prediction_for_fixture
from .provider_budget import QUOTA_EXHAUSTED, ProviderRequestBudget
from .portfolio_selection import (
    PORTFOLIO_LEGACY_PRE_POLICY,
    PORTFOLIO_POLICY_ID_V1,
    PORTFOLIO_POLICY_VERSION_V1,
    PORTFOLIO_STATE_NOT_APPLICABLE,
    PORTFOLIO_STATE_PENDING,
    PORTFOLIO_STATE_POLICY_INACTIVE,
    PORTFOLIO_STATE_SELECTED,
    PortfolioCandidate,
    PortfolioPolicy,
    select_fixture_portfolio,
)
from .runtime_paths import ensure_operational_data_dir, resolve_dashboard_operational_paths
from .snapshot_service import CurrentFootballModelSnapshot, CurrentSnapshotError, build_current_football_model_snapshot
from ..experiments.football_day12_safeguards import XG_APPLICABLE_COMPETITIONS

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
DEFAULT_STARTUP_SYNC_TIMEOUT_ENV_VAR = "SPORTS_AI_STARTUP_SYNC_TIMEOUT_SECONDS"
DEFAULT_ALLOW_PAID_PROVIDER_CALLS_ENV_VAR = "SPORTS_AI_ALLOW_PAID_PROVIDER_CALLS"
DEFAULT_API_FOOTBALL_DAILY_BUDGET_ENV_VAR = "SPORTS_AI_API_FOOTBALL_DAILY_BUDGET"
DEFAULT_API_FOOTBALL_QUOTA_RESET_HOUR_UTC_ENV_VAR = "SPORTS_AI_API_FOOTBALL_QUOTA_RESET_HOUR_UTC"
DEFAULT_API_FOOTBALL_OPERATIONAL_DAILY_CAP_ENV_VAR = "SPORTS_AI_API_FOOTBALL_OPERATIONAL_DAILY_CAP"
DEFAULT_FOOTBALL_PORTFOLIO_SELECTION_V1_EFFECTIVE_FROM_ENV_VAR = "SPORTS_AI_FOOTBALL_PORTFOLIO_SELECTION_V1_EFFECTIVE_FROM"
DEFAULT_RUNTIME_ROLE_ENV_VAR = "SPORTS_AI_RUNTIME_ROLE"

PAID_PROVIDER_DISABLED_STATE = "PAID_PROVIDER_CALLS_DISABLED"
RUNTIME_ROLE_COMBINED = "combined"
RUNTIME_ROLE_WEB = "web"
RUNTIME_ROLE_SCHEDULER = "scheduler"
RUNTIME_ROLES = {RUNTIME_ROLE_COMBINED, RUNTIME_ROLE_WEB, RUNTIME_ROLE_SCHEDULER}

logger = logging.getLogger(__name__)
PAPER_ACTIVE_STATES = {"PENDING", "OPEN", "UNSETTLED"}
PAPER_FINAL_STATES = {"WON", "LOST", "PUSH", "HALF_WIN", "HALF_LOSS", "VOID"}
PAPER_MARKET_DISPLAY_ORDER = {
    "1x2": 0,
    "btts": 1,
    "goals_total": 2,
    "corner_total": 3,
    "asian_handicap": 4,
}
CLIENT_PRIMARY_MARKETS = ("1x2", "btts", "goals_total", "asian_handicap", "corner_total")
CLIENT_PRIMARY_LEAGUES = ("ENG1", "ENG2", "ENG3", "ENG4", "ESP1")
TODAY_PRIMARY_KPI_LABELS = (
    "Analysed",
    "Predictions",
    "Approved",
    "Suggested Bets",
    "Settled",
    "Won",
    "Lost",
    "Pending",
    "Strike Rate",
    "P/L",
    "ROI",
)
CLIENT_MATCH_CARD_MARKETS = (
    ("1x2", "MATCH WINNER / 1X2"),
    ("btts", "BTTS"),
    ("goals_total", "GOALS"),
    ("corner_total", "CORNERS"),
    ("asian_handicap", "ASIAN HANDICAP"),
)
CLIENT_MATCH_CARD_MARKET_ORDER = {
    market_family: index
    for index, (market_family, _label) in enumerate(CLIENT_MATCH_CARD_MARKETS)
}


def _fixture_sort_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("kickoff_utc") or ""), str(row.get("fixture_id") or ""))


def _pagination_window(*, page: int, total_pages: int, max_pages: int = 5) -> list[int | None]:
    if total_pages <= max_pages + 2:
        return list(range(1, total_pages + 1))
    window_radius = max(1, max_pages // 2)
    start = max(2, page - window_radius)
    end = min(total_pages - 1, page + window_radius)
    if page <= window_radius + 2:
        end = min(total_pages - 1, max_pages)
    if page >= total_pages - (window_radius + 1):
        start = max(2, total_pages - (max_pages - 1))
    pages: list[int | None] = [1]
    if start > 2:
        pages.append(None)
    pages.extend(range(start, end + 1))
    if end < total_pages - 1:
        pages.append(None)
    pages.append(total_pages)
    return pages


def _paginate_collection(items: list[Any], *, page: int, page_size: int) -> dict[str, Any]:
    safe_page_size = max(1, int(page_size))
    total_items = len(items)
    total_pages = max(1, (total_items + safe_page_size - 1) // safe_page_size)
    current_page = min(max(1, int(page)), total_pages)
    start_index = (current_page - 1) * safe_page_size
    end_index = min(start_index + safe_page_size, total_items)
    return {
        "items": items[start_index:end_index],
        "page": current_page,
        "page_size": safe_page_size,
        "total_items": total_items,
        "total_pages": total_pages,
        "has_previous": current_page > 1,
        "has_next": current_page < total_pages,
        "previous_page": current_page - 1 if current_page > 1 else None,
        "next_page": current_page + 1 if current_page < total_pages else None,
        "showing_from": 0 if total_items == 0 else start_index + 1,
        "showing_to": end_index,
        "visible_pages": _pagination_window(page=current_page, total_pages=total_pages),
    }


def _market_identity(row: dict[str, Any]) -> tuple[str, str, float | None]:
    market = str(row.get("market_family") or row.get("market") or "")
    selection = str(row.get("selection") or "")
    line = row.get("line")
    return (market, selection, None if line is None else float(line))


def _xg_status_label(value: Any) -> str:
    status = str(value or "").strip().upper()
    if not status:
        return "N/A"
    return status.replace("_", " ")


def _default_fixture_provider() -> FixtureProvider:
    primary = ApiFootballFixtureProvider()
    if primary.configured():
        return primary
    fallback = FootballDataOrgFixtureProvider()
    if fallback.configured():
        return fallback
    return primary


def _default_market_provider(config: DashboardConfig) -> CurrentMarketProvider:
    provider = TheOddsApiProvider(
        persisted_snapshot_reader=football_paper_trading.persisted_snapshot_reader(
            config.paper_trading_db_path
        )
    )
    if provider.configured():
        return provider
    return NotConfiguredMarketProvider(
        provider_label="no-odds-provider-key" if config.current_odds_connected else "waiting-for-approved-odds-provider"
    )


def _default_statistics_provider() -> ApiFootballFixtureProvider:
    return ApiFootballFixtureProvider()


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


def _runtime_role_from_env() -> str:
    value = _env_str(DEFAULT_RUNTIME_ROLE_ENV_VAR, RUNTIME_ROLE_COMBINED).lower()
    if value not in RUNTIME_ROLES:
        raise ValueError(
            f"{DEFAULT_RUNTIME_ROLE_ENV_VAR} must be one of {sorted(RUNTIME_ROLES)}, got {value!r}"
        )
    return value


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
        display_line = None if line is None else (float(line) if normalized_selection == "HOME" else -float(line))
        handicap_text = _format_signed_display_line(display_line)
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


def _portfolio_enforced_decision_state(row: dict[str, Any]) -> str:
    if row.get("portfolio_selected"):
        return "BET"
    return "NO_BET_PORTFOLIO_NOT_SELECTED"


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


def _safeguard_reason_from_row(row: dict[str, Any]) -> str:
    day12_state = str(row.get("day12_state") or "")
    if day12_state == "BLOCKED_MISSING_XG_DATA":
        return "POINT-IN-TIME XG NOT AVAILABLE"
    if day12_state == "REJECTED_BY_XG_VETO":
        return "POINT-IN-TIME XG VETO"
    if day12_state == "DAY12_REVIEW_REQUIRED":
        return "POINT-IN-TIME XG REVIEW REQUIRED"
    market_freshness = str(row.get("market_freshness") or "")
    if market_freshness and market_freshness != "FRESH":
        return market_freshness.replace("_", " ").title()
    if row.get("paper_entry_eligible"):
        return "PASSED"
    reason_codes = [str(code).replace("_", " ").title() for code in (row.get("reason_codes") or [])[:2]]
    if reason_codes:
        return " / ".join(reason_codes)
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
    startup_sync_timeout_seconds: int = 15
    allow_paid_provider_calls: bool = False
    api_football_daily_budget: int | None = None
    api_football_quota_reset_hour_utc: int = 0
    api_football_operational_daily_cap: int | None = None
    portfolio_policy_id: str = PORTFOLIO_POLICY_ID_V1
    portfolio_policy_version: str = PORTFOLIO_POLICY_VERSION_V1
    portfolio_policy_effective_from: str = ""
    runtime_role: str = RUNTIME_ROLE_COMBINED

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
            startup_sync_timeout_seconds=_env_int(DEFAULT_STARTUP_SYNC_TIMEOUT_ENV_VAR, 15),
            allow_paid_provider_calls=_env_bool(DEFAULT_ALLOW_PAID_PROVIDER_CALLS_ENV_VAR, False),
            api_football_daily_budget=_env_int(DEFAULT_API_FOOTBALL_DAILY_BUDGET_ENV_VAR, None),
            api_football_quota_reset_hour_utc=_env_int(DEFAULT_API_FOOTBALL_QUOTA_RESET_HOUR_UTC_ENV_VAR, 0) or 0,
            api_football_operational_daily_cap=_env_int(DEFAULT_API_FOOTBALL_OPERATIONAL_DAILY_CAP_ENV_VAR, None),
            portfolio_policy_effective_from=_env_str(DEFAULT_FOOTBALL_PORTFOLIO_SELECTION_V1_EFFECTIVE_FROM_ENV_VAR, ""),
            runtime_role=_runtime_role_from_env(),
        )


class DashboardServices:
    """Coordinates fixture sync, prediction generation, and read APIs."""

    def __init__(
        self,
        config: DashboardConfig | None = None,
        *,
        fixture_provider: FixtureProvider | None = None,
        market_provider: CurrentMarketProvider | None = None,
        statistics_provider: ApiFootballFixtureProvider | None = None,
        xg_profile_factory: Any | None = None,
    ):
        self.config = DashboardConfig.default() if config is None else config
        if self.config.runtime_role not in RUNTIME_ROLES:
            raise ValueError(
                f"runtime_role must be one of {sorted(RUNTIME_ROLES)}, got {self.config.runtime_role!r}"
            )
        self.fixture_provider = fixture_provider or _default_fixture_provider()
        self.market_provider = market_provider or _default_market_provider(self.config)
        self.statistics_provider = statistics_provider or _default_statistics_provider()
        self._provider_budget = ProviderRequestBudget(
            self.config.dashboard_db_path,
            allow_paid_provider_calls=self.config.allow_paid_provider_calls,
            daily_budget=self.config.api_football_daily_budget,
            quota_reset_hour_utc=self.config.api_football_quota_reset_hour_utc,
            operational_daily_cap=self.config.api_football_operational_daily_cap,
        )
        for provider in (self.fixture_provider, self.market_provider, self.statistics_provider):
            attach = getattr(provider, "attach_request_budget", None)
            if callable(attach):
                attach(self._provider_budget)
        self._xg_profile_factory = xg_profile_factory
        self.snapshot: CurrentFootballModelSnapshot | None = None
        self.snapshot_error: str | None = None
        self._background_task: asyncio.Task[None] | None = None
        self._startup_sync_task: asyncio.Task[Any] | None = None
        self._schema_initialized = False
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
            "xg_readiness": {
                "state": "NOT_AUDITED",
                "audited_at": None,
                "fixtures_considered": 0,
                "fixtures_ready": 0,
                "fixtures_missing": 0,
                "competition_counts": {},
                "missing_examples": [],
                "reason": None,
            },
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
        self._startup_sync_state: dict[str, Any] = {
            "state": "IDLE",
            "started_at": None,
            "completed_at": None,
            "last_error": None,
        }

    async def initialize(self) -> None:
        ensure_operational_data_dir(self.config.operational_data_dir)
        ensure_operational_data_dir(self.config.performance_output_dir)
        initialized_at = utc_now_iso()
        logger.info(
            "dashboard startup: role=%s storage_mode=%s data_dir=%s scheduler_enabled=%s fixture_provider=%s market_provider=%s",
            self.config.runtime_role,
            self.config.storage_mode,
            self.config.operational_data_dir,
            self.config.enable_scheduler,
            self.fixture_provider.provider_name(),
            self.market_provider.provider_name(),
        )
        with storage.connect(self.config.dashboard_db_path) as conn:
            storage.initialize_database(conn)
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
            football_paper_trading.initialize_database(conn)
            football_canonical_fixture.ensure_schema(conn)
            football_portfolio_selection.ensure_schema(conn)
            football_prospective_cohort.ensure_schema(conn)
            self._schema_initialized = True
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
        if self.config.runtime_role != RUNTIME_ROLE_WEB:
            self._startup_sync_task = asyncio.create_task(self._run_startup_sync())
            startup_timeout_seconds = max(0, int(self.config.startup_sync_timeout_seconds))
            if startup_timeout_seconds > 0:
                try:
                    await asyncio.wait_for(asyncio.shield(self._startup_sync_task), timeout=startup_timeout_seconds)
                except asyncio.TimeoutError:
                    logger.warning(
                        "dashboard startup sync still running after %ss; continuing startup with persisted state",
                        startup_timeout_seconds,
                    )
        if (
            self.config.runtime_role != RUNTIME_ROLE_WEB
            and self.config.enable_scheduler
            and self._background_task is None
        ):
            self._background_task = asyncio.create_task(self._run_scheduler())

    async def shutdown(self) -> None:
        self._shutdown = True
        if self._startup_sync_task is not None and not self._startup_sync_task.done():
            self._startup_sync_task.cancel()
            try:
                await self._startup_sync_task
            except asyncio.CancelledError:
                pass
        if self._background_task is not None:
            self._background_task.cancel()
            try:
                await self._background_task
            except asyncio.CancelledError:
                pass
        logger.info("dashboard shutdown complete")

    async def _run_startup_sync(self) -> None:
        started_at = utc_now_iso()
        self._startup_sync_state.update(
            {
                "state": "RUNNING",
                "started_at": started_at,
                "completed_at": None,
                "last_error": None,
            }
        )
        try:
            await self.sync_once(sync_kind="startup", include_schedule=True)
        except Exception as exc:
            self._startup_sync_state.update(
                {
                    "state": "FAILED",
                    "completed_at": utc_now_iso(),
                    "last_error": f"{type(exc).__name__}: {exc}",
                }
            )
            logger.exception("dashboard startup sync failed: %s", exc)
            raise
        self._startup_sync_state.update(
            {
                "state": "COMPLETED",
                "completed_at": utc_now_iso(),
                "last_error": None,
            }
        )

    def _load_runtime_state(self, conn: Any) -> None:
        provider_state = storage.get_runtime_state(conn, "provider_state", initialize_schema=False)
        prediction_state = storage.get_runtime_state(conn, "prediction_state", initialize_schema=False)
        market_runtime_state = storage.get_runtime_state(conn, "market_runtime_state", initialize_schema=False)
        storage_runtime_state = storage.get_runtime_state(conn, "storage_runtime_state", initialize_schema=False)
        one_shot_sync_state = storage.get_runtime_state(conn, "one_shot_sync_state", initialize_schema=False)
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

    def _refresh_persisted_runtime_state_for_web(self) -> None:
        if self.config.runtime_role != RUNTIME_ROLE_WEB:
            return
        with storage.connect(self.config.dashboard_db_path) as conn:
            self._load_runtime_state(conn)

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
            "corner_result_sync": dict(existing.get("corner_result_sync") or self._empty_corner_result_sync_state()),
        }
        storage.put_runtime_state(
            conn,
            state_key="storage_runtime_state",
            state_value=payload,
            updated_at=updated_at,
            initialize_schema=self._read_requires_schema_init(),
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

    def _empty_corner_result_sync_state(self) -> dict[str, Any]:
        configured = bool(self.statistics_provider.configured())
        return {
            "provider": self.statistics_provider.provider_name(),
            "configured": configured,
            "state": "IDLE" if configured else "NOT_CONFIGURED",
            "last_attempt_at": None,
            "last_success_at": None,
            "last_error": None,
            "fixtures_considered": 0,
            "fixtures_updated": 0,
            "results_available": 0,
            "results_unavailable": 0,
            "fixtures_unmatched": 0,
            "fixtures_ambiguous": 0,
            "query_count": 0,
        }

    def _statistics_provider_sync_diagnostics(self) -> dict[str, Any]:
        diagnostics = getattr(self.statistics_provider, "sync_diagnostics", None)
        if callable(diagnostics):
            return diagnostics()
        return {
            "provider": self.statistics_provider.provider_name(),
            "query_count": 0,
            "has_errors": False,
            "last_error": None,
            "queries": [],
        }

    @staticmethod
    def _apply_corner_result_to_fixture(fixture: dict[str, Any], result_row: dict[str, Any]) -> None:
        fixture["statistics_provider"] = result_row.get("statistics_provider")
        fixture["statistics_provider_fixture_id"] = result_row.get("statistics_provider_fixture_id")
        fixture["home_corners"] = result_row.get("home_corners")
        fixture["away_corners"] = result_row.get("away_corners")
        fixture["total_corners"] = result_row.get("total_corners")
        fixture["corner_result_source_timestamp"] = result_row.get("source_timestamp")
        fixture["corner_result_captured_at"] = result_row.get("captured_at")

    def _sync_authoritative_corner_results(self, rows_with_predictions: list[dict[str, Any]]) -> None:
        diagnostics_state = self._empty_corner_result_sync_state()
        diagnostics_state["last_attempt_at"] = utc_now_iso()
        fixture_rows = {str(row["fixture_id"]): row for row in rows_with_predictions}
        with football_paper_trading.connect(self.config.paper_trading_db_path) as paper_conn:
            paper_rows = football_paper_trading.fetch_paper_trade_rows(
                paper_conn,
                initialize_schema=self._read_requires_schema_init(),
            )

        eligible_fixture_ids = sorted(
            {
                str(row["fixture_id"])
                for row in paper_rows
                if str(row.get("market") or "") == "corner_total"
                and str(row.get("trade_state") or "PENDING") in PAPER_ACTIVE_STATES
                and str((fixture_rows.get(str(row["fixture_id"])) or {}).get("status") or "") == FIXTURE_STATUS_FINISHED
            }
        )
        diagnostics_state["fixtures_considered"] = len(eligible_fixture_ids)
        if not eligible_fixture_ids:
            self._storage_runtime_state["corner_result_sync"] = diagnostics_state
            with storage.connect(self.config.dashboard_db_path) as conn:
                self._persist_storage_runtime_state(conn, updated_at=diagnostics_state["last_attempt_at"])
            return

        with storage.connect(self.config.dashboard_db_path) as conn:
            for fixture_id in eligible_fixture_ids:
                existing_result = storage.fetch_fixture_result(
                    conn,
                    fixture_id,
                    initialize_schema=self._read_requires_schema_init(),
                )
                if existing_result is not None:
                    self._apply_corner_result_to_fixture(fixture_rows[fixture_id], existing_result)
                fixture = fixture_rows[fixture_id]
                if fixture.get("home_corners") is not None and fixture.get("away_corners") is not None:
                    diagnostics_state["results_available"] += 1
                    continue
                if not self.statistics_provider.configured():
                    diagnostics_state["state"] = "NOT_CONFIGURED"
                    diagnostics_state["last_error"] = "statistics provider not configured"
                    continue
                try:
                    result = self.statistics_provider.get_authoritative_corner_statistics(fixture)
                except FixtureProviderNotConfiguredError as exc:
                    diagnostics_state["state"] = "NOT_CONFIGURED"
                    diagnostics_state["last_error"] = str(exc)
                    continue
                except FixtureProviderError as exc:
                    message = str(exc)
                    diagnostics_state["state"] = "DEGRADED"
                    diagnostics_state["last_error"] = message
                    if "STATS_FIXTURE_AMBIGUOUS" in message:
                        diagnostics_state["fixtures_ambiguous"] += 1
                    elif "STATS_FIXTURE_UNMATCHED" in message:
                        diagnostics_state["fixtures_unmatched"] += 1
                    continue
                storage.upsert_fixture_result(
                    conn,
                    fixture_id=str(fixture["fixture_id"]),
                    statistics_provider=result.provider,
                    statistics_provider_fixture_id=result.provider_fixture_id,
                    home_corners=result.home_corners,
                    away_corners=result.away_corners,
                    total_corners=result.total_corners,
                    source_timestamp=result.source_timestamp,
                    captured_at=result.captured_at,
                    initialize_schema=self._read_requires_schema_init(),
                )
                stored_result = storage.fetch_fixture_result(
                    conn,
                    str(fixture["fixture_id"]),
                    initialize_schema=self._read_requires_schema_init(),
                )
                if stored_result is not None:
                    self._apply_corner_result_to_fixture(fixture, stored_result)
                diagnostics_state["fixtures_updated"] += 1
                diagnostics_state["last_success_at"] = result.captured_at
                if result.home_corners is None or result.away_corners is None:
                    diagnostics_state["results_unavailable"] += 1
                else:
                    diagnostics_state["results_available"] += 1
            if diagnostics_state["state"] == "IDLE" and diagnostics_state["fixtures_updated"] > 0:
                diagnostics_state["state"] = "READY"
            diagnostics = self._statistics_provider_sync_diagnostics()
            diagnostics_state["query_count"] = int(diagnostics.get("query_count") or 0)
            self._storage_runtime_state["corner_result_sync"] = diagnostics_state
            self._persist_storage_runtime_state(conn, updated_at=diagnostics_state["last_attempt_at"])

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
        reference_bookmakers = os.environ.get("SPORTS_AI_MARKET_REFERENCE_BOOKMAKERS", "").strip() or "pinnacle"
        current_provider = self.market_provider.provider_name()
        if current_provider == "api-football":
            provider_probe = ApiFootballOddsProvider()
            freshness = os.environ.get("SPORTS_AI_API_FOOTBALL_MARKET_MAX_AGE_MINUTES", "").strip() or "240"
            return {
                "implementation_ready": "YES",
                "provider_key_configured": "YES" if provider_probe.configured() else "NO",
                "approval_enabled": "YES" if self.config.current_odds_connected else "NO",
                "automatic_sync_enabled": "YES" if self.config.enable_odds_sync else "NO",
                "required_env_vars": [
                    "SPORTS_AI_API_FOOTBALL_TOKEN",
                    "SPORTS_AI_API_FOOTBALL_LEAGUE_MAP",
                    "SPORTS_AI_CURRENT_ODDS_CONNECTED",
                    "SPORTS_AI_ENABLE_ODDS_SYNC",
                ],
                "optional_env_vars": [
                    "SPORTS_AI_MARKET_REFERENCE_BOOKMAKERS",
                    "SPORTS_AI_API_FOOTBALL_MARKET_MAX_AGE_MINUTES",
                ],
                "supported_markets": ["1x2", "goals_total", "btts", "corner_total", "asian_handicap"],
                "reference_price_method": f"Reference bookmaker fallback ({reference_bookmakers})",
                "freshness_rule": (
                    "Provider source timestamp must be within "
                    f"SPORTS_AI_API_FOOTBALL_MARKET_MAX_AGE_MINUTES (current: {freshness})"
                ),
            }
        if current_provider not in {"the-odds-api", "no-odds-provider-key", "waiting-for-approved-odds-provider", "api-football-not-configured"}:
            return {
                "implementation_ready": "UNKNOWN",
                "required_env_vars": [],
                "optional_env_vars": [],
                "supported_markets": [],
                "reference_price_method": None,
                "freshness_rule": None,
            }
        provider_probe = TheOddsApiProvider()
        freshness = os.environ.get("SPORTS_AI_MARKET_MAX_AGE_MINUTES", "").strip() or "unset"
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
            "supported_markets": ["1x2", "goals_total", "btts", "corner_total", "asian_handicap"],
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
            "current_slate_readiness": dict(self._provider_state.get("xg_readiness") or {}),
            "missing_requirement": None
            if enabled
            else (
                "Set SPORTS_AI_CURRENT_XG_CONNECTED=1 to enable the existing derived rolling xG safeguard input."
                if derived_available
                else "Historical research store access is required for the derived rolling xG safeguard input."
            ),
        }

    def _audit_current_xg_readiness(self, rows_with_predictions: list[dict[str, Any]]) -> dict[str, Any]:
        """Audit current xG-required fixtures without enabling the safeguard or calling a provider."""
        audited_at = utc_now_iso()
        if self.snapshot is None or not self.config.research_db_path.exists():
            return {
                "state": "UNAVAILABLE",
                "audited_at": audited_at,
                "fixtures_considered": 0,
                "fixtures_ready": 0,
                "fixtures_missing": 0,
                "competition_counts": {},
                "missing_examples": [],
                "reason": "CURRENT_MODEL_OR_RESEARCH_STORE_UNAVAILABLE",
            }

        applicable_rows = [
            row
            for row in self._future_fixture_rows(rows_with_predictions)
            if self._prediction_eligibility_status(row.get("prediction")) == "PREDICTION_READY"
            and str(row.get("competition_id") or "") in XG_APPLICABLE_COMPETITIONS
        ]
        if not applicable_rows:
            return {
                "state": "NO_APPLICABLE_FIXTURES",
                "audited_at": audited_at,
                "fixtures_considered": 0,
                "fixtures_ready": 0,
                "fixtures_missing": 0,
                "competition_counts": {},
                "missing_examples": [],
                "reason": None,
            }

        ready_count = 0
        missing_count = 0
        competition_counts: dict[str, dict[str, int]] = defaultdict(
            lambda: {"considered": 0, "ready": 0, "missing": 0}
        )
        missing_examples: list[dict[str, Any]] = []

        for row in applicable_rows:
            prediction = row.get("prediction") or {}
            competition_id = str(row.get("competition_id") or "")
            counts = competition_counts[competition_id]
            counts["considered"] += 1
            coefficients = (self.snapshot.conversion_coefficients or {}).get(competition_id)
            team_resolution = prediction.get("team_resolution") or {}
            home_team = str(
                (team_resolution.get("home") or {}).get("canonical_team_name")
                or row.get("home_team_name")
                or ""
            )
            away_team = str(
                (team_resolution.get("away") or {}).get("canonical_team_name")
                or row.get("away_team_name")
                or ""
            )

            rows_by_role: dict[tuple[str, str], dict[str, Any]] = {}
            inspection: dict[str, Any] = {}
            if coefficients:
                rows_by_role, inspection = build_fixture_xg_rows(
                    db_path=self.config.research_db_path,
                    competition_id=competition_id,
                    fixture_id=str(row.get("fixture_id") or ""),
                    home_team=home_team,
                    away_team=away_team,
                    decision_as_of=audited_at,
                    coefficients=coefficients,
                    min_window_matches=DEFAULT_XG_MIN_WINDOW_MATCHES,
                )

            fixture_id = str(row.get("fixture_id") or "")
            home_row = rows_by_role.get((fixture_id, "HOME"))
            away_row = rows_by_role.get((fixture_id, "AWAY"))
            home_matches = int((home_row or {}).get("matches_in_window") or 0)
            away_matches = int((away_row or {}).get("matches_in_window") or 0)
            ready = bool(
                inspection.get("sufficient_for_day12_rule")
                and home_row is not None
                and away_row is not None
                and home_matches >= DEFAULT_XG_MIN_WINDOW_MATCHES
                and away_matches >= DEFAULT_XG_MIN_WINDOW_MATCHES
            )
            if ready:
                ready_count += 1
                counts["ready"] += 1
                continue

            missing_count += 1
            counts["missing"] += 1
            if len(missing_examples) < 5:
                missing_examples.append(
                    {
                        "fixture_id": fixture_id,
                        "competition_id": competition_id,
                        "home_team_name": home_team,
                        "away_team_name": away_team,
                        "home_matches": home_matches,
                        "away_matches": away_matches,
                        "reason": inspection.get("reason")
                        or ("CONVERSION_COEFFICIENTS_MISSING" if not coefficients else "INSUFFICIENT_XG_HISTORY"),
                    }
                )

        state = "READY" if missing_count == 0 else "PARTIAL"
        return {
            "state": state,
            "audited_at": audited_at,
            "fixtures_considered": len(applicable_rows),
            "fixtures_ready": ready_count,
            "fixtures_missing": missing_count,
            "competition_counts": {
                key: dict(competition_counts[key]) for key in sorted(competition_counts)
            },
            "missing_examples": missing_examples,
            "reason": None,
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

    def _read_requires_schema_init(self) -> bool:
        return not self._schema_initialized

    def _raw_paper_positions(self) -> list[dict[str, Any]]:
        with football_paper_trading.connect(self.config.paper_trading_db_path) as conn:
            return football_paper_trading.fetch_paper_trade_rows(
                conn,
                initialize_schema=self._read_requires_schema_init(),
            )

    def _paper_positions(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        rows = self._raw_paper_positions()
        with football_paper_trading.connect(self.config.paper_trading_db_path) as conn:
            excluded = football_canonical_fixture.fetch_excluded_paper_trade_ids(
                conn,
                initialize_schema=self._read_requires_schema_init(),
            )
        counted_rows = [
            row
            for row in rows
            if str(row.get("paper_trade_id") or "") not in excluded
        ]
        status = football_paper_trading.paper_trading_status(counted_rows)
        status["raw_paper_rows"] = len(rows)
        status["reconciled_excluded_positions"] = len(excluded)
        status["reconciliation_applied"] = len(excluded) > 0
        return counted_rows, status

    def _paper_fixture_identity_map(self) -> dict[str, dict[str, Any]]:
        """Map each paper fixture_id to its persisted fixture identity."""
        rows = self._raw_paper_positions()
        fixture_ids = sorted({str(row["fixture_id"]) for row in rows if row.get("fixture_id") not in {None, ""}})
        identity: dict[str, dict[str, Any]] = {}
        if fixture_ids:
            with storage.connect(self.config.dashboard_db_path) as conn:
                for fixture_id in fixture_ids:
                    fixture = storage.fetch_fixture(
                        conn,
                        fixture_id,
                        initialize_schema=self._read_requires_schema_init(),
                    )
                    if fixture is not None:
                        identity[fixture_id] = {
                            "fixture_id": fixture_id,
                            "competition_id": fixture.get("competition_id"),
                            "home_team_name": fixture.get("home_team_name"),
                            "away_team_name": fixture.get("away_team_name"),
                            "kickoff_utc": fixture.get("kickoff_utc"),
                            "season": fixture.get("season"),
                        }
        missing = [fixture_id for fixture_id in fixture_ids if fixture_id not in identity]
        if missing:
            with football_paper_trading.connect(self.config.paper_trading_db_path) as conn:
                for fixture_id in missing:
                    row = conn.execute(
                        "SELECT competition_id, home_team_name, away_team_name, kickoff_utc "
                        "FROM market_snapshots WHERE fixture_id = ? LIMIT 1",
                        (fixture_id,),
                    ).fetchone()
                    if row is not None:
                        identity[fixture_id] = {
                            "fixture_id": fixture_id,
                            "competition_id": row["competition_id"],
                            "home_team_name": row["home_team_name"],
                            "away_team_name": row["away_team_name"],
                            "kickoff_utc": row["kickoff_utc"],
                            "season": None,
                        }
        return identity

    def get_reconciliation_report(self) -> dict[str, Any]:
        """Deterministic DRY-RUN report over the persisted paper ledger (non-mutating)."""
        rows = self._raw_paper_positions()
        identity = self._paper_fixture_identity_map()
        plan = football_canonical_fixture.build_reconciliation_plan(rows, identity)
        before = football_canonical_fixture.summarize_performance(rows)
        after = football_canonical_fixture.summarize_performance(plan.counted_rows(rows))

        by_match: dict[str, dict[str, Any]] = {}
        for group in plan.duplicate_groups:
            match = by_match.setdefault(
                group.canonical_fixture_id,
                {
                    "canonical_fixture_id": group.canonical_fixture_id,
                    "match": group.canonical_row.get("fixture_label")
                    or "{} vs {}".format(
                        (identity.get(str(group.canonical_row.get("fixture_id") or "")) or {}).get("home_team_name") or "?",
                        (identity.get(str(group.canonical_row.get("fixture_id") or "")) or {}).get("away_team_name") or "?",
                    ),
                    "kickoff_utc": group.canonical_row.get("kickoff"),
                    "competition_id": (identity.get(str(group.canonical_row.get("fixture_id") or "")) or {}).get("competition_id"),
                    "fixture_ids": set(),
                    "positions_by_fixture": {},
                    "duplicate_business_positions": 0,
                    "settlement_consistent": True,
                    "settlement_conflict": False,
                },
            )
            match["fixture_ids"].add(str(group.canonical_row.get("fixture_id") or ""))
            match["duplicate_business_positions"] += 1
            for duplicate in group.duplicate_rows:
                match["fixture_ids"].add(str(duplicate.get("fixture_id") or ""))
            if group.canonical_row.get("fixture_id"):
                match["positions_by_fixture"][str(group.canonical_row["fixture_id"])] = (
                    match["positions_by_fixture"].get(str(group.canonical_row["fixture_id"]), 0) + 1
                )
            for duplicate in group.duplicate_rows:
                if duplicate.get("fixture_id"):
                    match["positions_by_fixture"][str(duplicate["fixture_id"])] = (
                        match["positions_by_fixture"].get(str(duplicate["fixture_id"]), 0) + 1
                    )
            conflict = next(
                (
                    c
                    for c in plan.conflicts
                    if c.get("canonical_fixture_id") == group.canonical_fixture_id
                    and c.get("market") == group.market
                    and c.get("selection") == group.selection
                ),
                None,
            )
            if conflict is not None:
                match["settlement_consistent"] = False
                match["settlement_conflict"] = True

        for match in by_match.values():
            match["fixture_ids"] = sorted(match["fixture_ids"])

        return {
            "reconciliation_version": football_canonical_fixture.CANONICAL_RECONCILIATION_VERSION,
            "kickoff_tolerance_seconds": football_canonical_fixture.DEFAULT_KICKOFF_TOLERANCE_SECONDS,
            "total_rows": plan.total_rows,
            "duplicate_groups": len(plan.duplicate_groups),
            "duplicate_representations": plan.duplicate_representation_count,
            "ambiguous_fixture_ids": list(plan.ambiguous_fixture_ids),
            "unresolved_fixture_ids": list(plan.unresolved_fixture_ids),
            "settlement_conflicts": list(plan.conflicts),
            "settlement_inconsistencies": list(plan.inconsistencies),
            "canonical_fixture_count": plan.canonical_fixture_count,
            "cross_provider_fixture_groups": plan.cross_provider_fixture_groups,
            "before": before,
            "after_projected": after,
            "by_match": by_match,
        }

    def apply_paper_reconciliation(self) -> dict[str, Any]:
        """Validate invariants and apply the additive reconciliation once.

        NOT called during normal operation; intended for the reviewed apply
        step after the dry-run projection has been confirmed.
        """
        rows = self._raw_paper_positions()
        identity = self._paper_fixture_identity_map()
        with football_paper_trading.connect(self.config.paper_trading_db_path) as conn:
            result = football_canonical_fixture.apply_reconciliation(conn, identity)
        return {
            "applied": result.applied,
            "records_written": result.records_written,
            "existing_excluded": result.existing_excluded,
            "violations": list(result.violations),
            "total_rows": len(rows),
            "duplicate_representations": result.plan.duplicate_representation_count,
            "after_projected": football_canonical_fixture.summarize_performance(
                result.plan.counted_rows(rows)
            ),
        }

    def _market_evaluation_map(self) -> dict[str, Any]:
        return dict(self._market_runtime_state.get("evaluations") or {})

    def _market_evaluation_for_fixture(self, fixture_id: str) -> dict[str, Any] | None:
        return self._market_evaluation_map().get(str(fixture_id))

    def _performance_status(self, summary_payload: dict[str, Any] | None, paper_status: dict[str, Any]) -> dict[str, Any]:
        performance = paper_status.get("performance", {})
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
                "wins": int(performance.get("wins") or 0),
                "losses": int(performance.get("losses") or 0),
                "pushes": int(performance.get("pushes") or 0),
            },
            "closing_price_tracking": {
                "state": "READY",
            },
            "clv": {
                "state": paper_status["clv_state"],
            },
        }

    @staticmethod
    def _paper_market_sort_key(market: str) -> tuple[int, str]:
        return (PAPER_MARKET_DISPLAY_ORDER.get(market, 999), market)

    @staticmethod
    def _paper_result_tone(trade_state: str) -> str:
        if trade_state in {"WON", "HALF_WIN"}:
            return "bet"
        if trade_state in {"LOST", "HALF_LOSS"}:
            return "lost"
        if trade_state in {"PUSH", "VOID"}:
            return "final"
        return "pending"

    def _paper_fixture_lookup(self, paper_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        fixture_ids = sorted(
            {
                str(row["fixture_id"])
                for row in paper_rows
                if row.get("fixture_id") not in {None, ""}
            }
        )
        if not fixture_ids:
            return {}
        with storage.connect(self.config.dashboard_db_path) as conn:
            fixture_lookup = {
                fixture_id: fixture
                for fixture_id in fixture_ids
                if (
                    fixture := storage.fetch_fixture(
                        conn,
                        fixture_id,
                        initialize_schema=self._read_requires_schema_init(),
                    )
                ) is not None
            }
        missing_fixture_ids = [fixture_id for fixture_id in fixture_ids if fixture_id not in fixture_lookup]
        if missing_fixture_ids:
            fallback_rows = {
                str(row["fixture_id"]): self.serialize_fixture_row(row)
                for row in self._all_fixtures_with_predictions()
                if str(row["fixture_id"]) in missing_fixture_ids
            }
            fixture_lookup.update(fallback_rows)
        return fixture_lookup

    def _paper_position_display_rows(self, paper_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        fixture_lookup = self._paper_fixture_lookup(paper_rows)
        display_rows: list[dict[str, Any]] = []
        for row in paper_rows:
            fixture_id = None if row.get("fixture_id") in {None, ""} else str(row["fixture_id"])
            fixture = None if fixture_id is None else fixture_lookup.get(fixture_id)
            fixture_home = str((fixture or {}).get("home_team_name") or "Home")
            fixture_away = str((fixture or {}).get("away_team_name") or "Away")
            labels = _humanize_market_selection(
                market_family=str(row.get("market") or "1x2"),
                selection=str(row.get("selection") or "HOME"),
                line=None if row.get("line") is None else float(row["line"]),
                home_team_name=fixture_home,
                away_team_name=fixture_away,
            )
            latest_payload = row.get("latest_update_payload") or {}
            settlement_context = latest_payload.get("settlement_context") or {}
            actual_home_goals = settlement_context.get("actual_home_goals")
            actual_away_goals = settlement_context.get("actual_away_goals")
            actual_home_corners = settlement_context.get("actual_home_corners")
            actual_away_corners = settlement_context.get("actual_away_corners")
            actual_total_corners = settlement_context.get("actual_total_corners")
            final_score = None
            if actual_home_goals is not None and actual_away_goals is not None:
                final_score = f"{int(actual_home_goals)}-{int(actual_away_goals)}"
            final_corners = None
            if actual_home_corners is not None and actual_away_corners is not None:
                final_corners = f"{int(actual_home_corners)}-{int(actual_away_corners)}"
            result_summary = final_score
            if str(row.get("market") or "") == "corner_total" and final_corners is not None and actual_total_corners is not None:
                result_summary = f"{final_corners} (Total {int(actual_total_corners)})"
            trade_state = str(row.get("trade_state") or "PENDING")
            kickoff_value = row.get("kickoff") or (fixture or {}).get("kickoff_utc") or row.get("decision_timestamp")
            kickoff_local = "N/A"
            if kickoff_value:
                kickoff_local = parse_iso_datetime(str(kickoff_value)).astimezone(ZoneInfo(self.config.display_timezone)).strftime(
                    "%Y-%m-%d %H:%M"
                )
            display_rows.append(
                {
                    **row,
                    "fixture_label": (
                        f"{fixture_home} vs {fixture_away}"
                        if fixture is not None
                        else str(row.get("fixture_id") or "Unknown Fixture")
                    ),
                    "kickoff_local": kickoff_local,
                    "market_label": labels["market_label"],
                    "selection_label": labels["selection_label"],
                    "bet_label": labels["bet_label"],
                    "result_tone": self._paper_result_tone(trade_state),
                    "is_settled": trade_state in PAPER_FINAL_STATES,
                    "is_open": trade_state in PAPER_ACTIVE_STATES,
                    "final_score": final_score,
                    "final_corners": final_corners,
                    "final_total_corners": actual_total_corners,
                    "result_summary": result_summary,
                    "settled_at": row.get("updated_at") if trade_state in PAPER_FINAL_STATES else None,
                    "result_provider": settlement_context.get("statistics_provider")
                    if str(row.get("market") or "") == "corner_total" and settlement_context.get("statistics_provider")
                    else settlement_context.get("provider"),
                    "result_provider_fixture_id": settlement_context.get("statistics_provider_fixture_id")
                    if str(row.get("market") or "") == "corner_total" and settlement_context.get("statistics_provider_fixture_id")
                    else settlement_context.get("provider_fixture_id"),
                    "result_captured_at": settlement_context.get("corner_result_captured_at")
                    if str(row.get("market") or "") == "corner_total" and settlement_context.get("corner_result_captured_at")
                    else settlement_context.get("result_captured_at"),
                    "final_status": settlement_context.get("final_status"),
                }
            )
        return display_rows

    def _current_local_date(self) -> date:
        return datetime.now(timezone.utc).astimezone(ZoneInfo(self.config.display_timezone)).date()

    def _local_date_for_timestamp(self, value: Any) -> date | None:
        if value in {None, ""}:
            return None
        try:
            return parse_iso_datetime(str(value)).astimezone(ZoneInfo(self.config.display_timezone)).date()
        except Exception:
            return None

    def _paper_rows_for_local_date(
        self,
        paper_rows: list[dict[str, Any]],
        *,
        target_date: date | None = None,
    ) -> list[dict[str, Any]]:
        local_date = self._current_local_date() if target_date is None else target_date
        return [
            row
            for row in paper_rows
            if self._local_date_for_timestamp(row.get("kickoff") or row.get("decision_timestamp")) == local_date
        ]

    def _paper_rows_by_trade_id(self, paper_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {
            str(row["paper_trade_id"]): row
            for row in paper_rows
            if row.get("paper_trade_id") is not None
        }

    def _portfolio_policy(self) -> PortfolioPolicy | None:
        effective_from = str(self.config.portfolio_policy_effective_from or "").strip()
        if not effective_from:
            return None
        try:
            parse_iso_datetime(effective_from)
        except Exception:
            logger.warning("ignoring invalid portfolio policy effective_from: %s", effective_from)
            return None
        return PortfolioPolicy(
            policy_id=str(self.config.portfolio_policy_id or PORTFOLIO_POLICY_ID_V1),
            policy_version=str(self.config.portfolio_policy_version or PORTFOLIO_POLICY_VERSION_V1),
            effective_from=effective_from,
        )

    @staticmethod
    def _portfolio_policy_applies_to_timestamp(value: Any, policy: PortfolioPolicy | None) -> bool:
        if policy is None or value in {None, ""}:
            return False
        try:
            return parse_iso_datetime(str(value)) >= parse_iso_datetime(policy.effective_from)
        except Exception:
            return False

    @staticmethod
    def _portfolio_observed_at(row: dict[str, Any]) -> str | None:
        for key in ("market_as_of", "best_available_source_timestamp", "decision_timestamp", "created_at"):
            value = row.get(key)
            if value not in {None, ""}:
                return str(value)
        return None

    @staticmethod
    def _latest_portfolio_timestamp(values: list[str | None], *, fallback: str) -> str:
        latest_value: str | None = None
        latest_dt: datetime | None = None
        for value in values:
            if value in {None, ""}:
                continue
            try:
                parsed = parse_iso_datetime(str(value))
            except Exception:
                continue
            if latest_dt is None or parsed > latest_dt:
                latest_dt = parsed
                latest_value = str(value)
        return fallback if latest_value is None else latest_value

    @staticmethod
    def _portfolio_current_row_is_actionable(row: dict[str, Any]) -> bool:
        current_state = str(row.get("current_decision_state") or _current_decision_state(row))
        freshness = str(row.get("market_freshness") or row.get("freshness_state") or "")
        return (
            current_state == "BET"
            and bool(row.get("paper_entry_eligible"))
            and freshness == "FRESH"
            and DashboardServices._safeguard_approved_row(row)
        )

    def _portfolio_selection_records(self) -> dict[str, Any]:
        policy = self._portfolio_policy()
        if policy is None:
            return {
                "policy": None,
                "rows": [],
                "evaluation_rows": [],
                "by_trade_id": {},
                "selected_trade_ids": set(),
                "publish_state": "DISABLED",
                "publish_note": "Portfolio selection is implemented but inactive until an explicit effective_from is configured.",
                "mode": "legacy",
            }
        with football_paper_trading.connect(self.config.paper_trading_db_path) as conn:
            evaluation_rows = football_portfolio_selection.fetch_current_portfolio_evaluations(
                conn,
                policy_id=policy.policy_id,
                policy_version=policy.policy_version,
                initialize_schema=self._read_requires_schema_init(),
            )
            rows = football_portfolio_selection.fetch_current_portfolio_selections(
                conn,
                policy_id=policy.policy_id,
                policy_version=policy.policy_version,
                initialize_schema=self._read_requires_schema_init(),
            )
        by_trade_id = {
            str(row["paper_trade_id"]): row
            for row in rows
            if row.get("paper_trade_id") is not None
        }
        selected_trade_ids = {
            str(row["paper_trade_id"])
            for row in rows
            if str(row.get("selection_state") or "") == PORTFOLIO_STATE_SELECTED
        }
        if not evaluation_rows:
            publish_state = "CURRENT PORTFOLIO PENDING"
            publish_note = "Portfolio selection pending for current qualified opportunities."
        elif not selected_trade_ids:
            publish_state = "CURRENT PORTFOLIO EMPTY"
            publish_note = "No current portfolio-selected bets are available."
        else:
            publish_state = "CURRENT PORTFOLIO"
            publish_note = (
                "Current deterministic portfolio selection. "
                "A separate frozen publication lifecycle is not active yet."
            )
        return {
            "policy": policy,
            "rows": rows,
            "evaluation_rows": evaluation_rows,
            "by_trade_id": by_trade_id,
            "selected_trade_ids": selected_trade_ids,
            "publish_state": publish_state,
            "publish_note": publish_note,
            "mode": "current_portfolio_selection",
        }

    def _portfolio_selection_context(self, paper_rows: list[dict[str, Any]]) -> dict[str, Any]:
        records = self._portfolio_selection_records()
        paper_rows_by_trade_id = {
            str(row["paper_trade_id"]): row
            for row in paper_rows
            if row.get("paper_trade_id") is not None
        }
        selected_rows = [
            row
            for row in paper_rows
            if str(row.get("paper_trade_id") or "") in records["selected_trade_ids"]
        ]
        selected_performance = football_paper_trading.paper_trading_performance(selected_rows)
        selected_canonical_fixture_ids = {
            str(row["canonical_fixture_id"])
            for row in records.get("rows", [])
            if str(row.get("selection_state") or "") == PORTFOLIO_STATE_SELECTED
            and row.get("canonical_fixture_id") not in {None, ""}
        }
        return {
            **records,
            "paper_rows_by_trade_id": paper_rows_by_trade_id,
            "selected_rows": selected_rows,
            "selected_count": len(selected_rows),
            "selected_fixture_count": len(selected_canonical_fixture_ids),
            "selected_canonical_fixture_ids": selected_canonical_fixture_ids,
            "open_exposure_units": sum(
                1.0
                for row in selected_rows
                if str(row.get("trade_state") or "PENDING") in PAPER_ACTIVE_STATES
            ),
            "performance": selected_performance,
        }

    def _portfolio_metadata_for_row(
        self,
        row: dict[str, Any],
        *,
        records: dict[str, Any],
        qualified_for_portfolio: bool,
    ) -> dict[str, Any]:
        policy = records.get("policy")
        canonical_fixture_id = None
        trade_id = str(row.get("paper_trade_id") or "")
        paper_row = (records.get("paper_rows_by_trade_id") or {}).get(trade_id)
        policy_timestamp = None if paper_row is None else paper_row.get("decision_timestamp")
        if policy_timestamp in {None, ""}:
            policy_timestamp = row.get("decision_timestamp")
        if not qualified_for_portfolio:
            state = PORTFOLIO_STATE_NOT_APPLICABLE
            reason = "Only BET-qualified paper opportunities enter the portfolio selector."
            rank = None
        elif policy is None:
            state = PORTFOLIO_STATE_POLICY_INACTIVE
            reason = records["publish_note"]
            rank = None
        elif not self._portfolio_policy_applies_to_timestamp(policy_timestamp, policy):
            state = PORTFOLIO_LEGACY_PRE_POLICY
            reason = f"Qualified before policy effective_from {policy.effective_from}."
            rank = None
        else:
            selection_row = records["by_trade_id"].get(trade_id)
            if selection_row is None:
                state = PORTFOLIO_STATE_PENDING
                reason = "Qualified opportunity is waiting for an auditable portfolio evaluation record."
                rank = None
            else:
                state = str(selection_row.get("selection_state") or PORTFOLIO_STATE_PENDING)
                reason = str(selection_row.get("selection_reason") or "")
                rank = None if selection_row.get("selection_rank") is None else int(selection_row["selection_rank"])
                canonical_fixture_id = None if selection_row.get("canonical_fixture_id") is None else str(selection_row["canonical_fixture_id"])
        return {
            "portfolio_selection_state": state,
            "portfolio_selection_state_label": state.replace("_", " "),
            "portfolio_selection_reason": reason,
            "portfolio_selection_rank": rank,
            "portfolio_selected": state == PORTFOLIO_STATE_SELECTED,
            "portfolio_canonical_fixture_id": canonical_fixture_id,
        }

    def _annotate_rows_with_portfolio(
        self,
        rows: list[dict[str, Any]],
        *,
        records: dict[str, Any],
        qualified_predicate: Any,
    ) -> list[dict[str, Any]]:
        annotated_rows: list[dict[str, Any]] = []
        for row in rows:
            annotated_rows.append(
                {
                    **row,
                    **self._portfolio_metadata_for_row(
                        row,
                        records=records,
                        qualified_for_portfolio=bool(qualified_predicate(row)),
                    ),
                }
            )
        return annotated_rows

    def _enforce_portfolio_recommendation_truth(
        self,
        rows: list[dict[str, Any]],
        *,
        records: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if records.get("policy") is None:
            return rows
        enforced_rows: list[dict[str, Any]] = []
        for row in rows:
            if str(row.get("current_decision_state") or row.get("desk_decision") or "") != "BET":
                enforced_rows.append(row)
                continue
            if row.get("portfolio_selected"):
                enforced_rows.append({**row, "actual_recommendation_state": "BET"})
                continue
            state = _portfolio_enforced_decision_state(row)
            reason = str(row.get("portfolio_selection_reason") or "Not selected by current portfolio policy.")
            enforced_rows.append(
                {
                    **row,
                    "actual_recommendation_state": state,
                    "current_decision_state": state,
                    "current_decision_group": _decision_group(state),
                    "desk_decision": state,
                    "decision_group": _decision_group(state),
                    "desk_label": "NO BET",
                    "is_actionable": False,
                    "is_featured": False,
                    "paper_qualification_state": "NOT_PUBLISHED",
                    "reason": reason,
                    "desk_note": reason,
                }
            )
        return enforced_rows

    def _paper_fixture_identity_map_from_rows(
        self,
        conn: sqlite3.Connection,
        paper_rows: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        fixture_ids = sorted(
            {
                str(row["fixture_id"])
                for row in paper_rows
                if row.get("fixture_id") not in {None, ""}
            }
        )
        identity: dict[str, dict[str, Any]] = {}
        with storage.connect(self.config.dashboard_db_path) as dashboard_conn:
            fixtures = storage.fetch_fixtures_by_ids(
                dashboard_conn,
                fixture_ids,
                initialize_schema=self._read_requires_schema_init(),
            )
            for fixture_id in fixture_ids:
                fixture = fixtures.get(fixture_id)
                if fixture is not None:
                    provider, provider_fixture_id = football_canonical_fixture.split_provider_fixture_id(fixture_id)
                    identity[fixture_id] = {
                        "fixture_id": fixture_id,
                        "provider": provider,
                        "provider_fixture_id": provider_fixture_id,
                        "competition_id": fixture.get("competition_id"),
                        "home_team_name": fixture.get("home_team_name"),
                        "away_team_name": fixture.get("away_team_name"),
                        "kickoff_utc": fixture.get("kickoff_utc"),
                        "season": fixture.get("season"),
                    }
        for fixture_id in fixture_ids:
            if fixture_id in identity:
                continue
            provider, provider_fixture_id = football_canonical_fixture.split_provider_fixture_id(fixture_id)
            snapshot_row = conn.execute(
                """
                SELECT provider, provider_fixture_id, competition_id, home_team_name, away_team_name, kickoff_utc
                FROM market_snapshots
                WHERE fixture_id = ?
                ORDER BY captured_at, snapshot_id
                LIMIT 1
                """,
                (fixture_id,),
            ).fetchone()
            identity[fixture_id] = {
                "fixture_id": fixture_id,
                "provider": str(snapshot_row["provider"]) if snapshot_row is not None and snapshot_row["provider"] is not None else provider,
                "provider_fixture_id": (
                    str(snapshot_row["provider_fixture_id"])
                    if snapshot_row is not None and snapshot_row["provider_fixture_id"] is not None
                    else provider_fixture_id
                ),
                "competition_id": None if snapshot_row is None else snapshot_row["competition_id"],
                "home_team_name": None if snapshot_row is None else snapshot_row["home_team_name"],
                "away_team_name": None if snapshot_row is None else snapshot_row["away_team_name"],
                "kickoff_utc": None if snapshot_row is None else snapshot_row["kickoff_utc"],
                "season": None,
            }
        return identity

    def _canonical_fixture_for_identity(
        self,
        conn: sqlite3.Connection,
        identity: dict[str, Any],
    ) -> str | None:
        provider = str(identity.get("provider") or "")
        provider_fixture_id = str(identity.get("provider_fixture_id") or "")
        competition_id = str(identity.get("competition_id") or "")
        home_team_name = str(identity.get("home_team_name") or "")
        away_team_name = str(identity.get("away_team_name") or "")
        kickoff_utc = str(identity.get("kickoff_utc") or "")
        if not (provider and provider_fixture_id and competition_id and home_team_name and away_team_name and kickoff_utc):
            return None
        try:
            canonical_fixture_id, _ = football_canonical_fixture.resolve_or_create_canonical_fixture(
                conn,
                provider=provider,
                provider_fixture_id=provider_fixture_id,
                competition_id=competition_id,
                home_team_name=home_team_name,
                away_team_name=away_team_name,
                kickoff_utc=kickoff_utc,
                season=identity.get("season"),
                initialize_schema=self._read_requires_schema_init(),
            )
        except football_canonical_fixture.CanonicalFixtureError:
            return None
        return canonical_fixture_id

    def _counted_portfolio_paper_rows(
        self,
        conn: sqlite3.Connection,
        *,
        policy: PortfolioPolicy,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        paper_rows = football_paper_trading.fetch_paper_trade_rows(
            conn,
            initialize_schema=self._read_requires_schema_init(),
        )
        excluded = football_canonical_fixture.fetch_excluded_paper_trade_ids(
            conn,
            initialize_schema=self._read_requires_schema_init(),
        )
        counted_rows = [
            row
            for row in paper_rows
            if str(row.get("paper_trade_id") or "") not in excluded
            and self._portfolio_policy_applies_to_timestamp(row.get("decision_timestamp"), policy)
        ]
        canonical_fixture_id_by_fixture_id = football_canonical_fixture.fetch_canonical_fixture_ids_for_fixture_ids(
            conn,
            [
                str(row.get("fixture_id") or "")
                for row in counted_rows
                if row.get("fixture_id") not in {None, ""}
            ],
            initialize_schema=self._read_requires_schema_init(),
        )
        counted_rows_by_trade_id: dict[str, dict[str, Any]] = {}
        canonical_fixture_id_by_trade_id: dict[str, str] = {}
        for row in counted_rows:
            trade_id = str(row.get("paper_trade_id") or "")
            canonical_fixture_id = canonical_fixture_id_by_fixture_id.get(str(row.get("fixture_id") or ""))
            if not trade_id or canonical_fixture_id is None:
                continue
            counted_rows_by_trade_id[trade_id] = row
            canonical_fixture_id_by_trade_id[trade_id] = canonical_fixture_id
        return counted_rows_by_trade_id, canonical_fixture_id_by_trade_id

    def _portfolio_candidates_for_current_evaluations(
        self,
        conn: sqlite3.Connection,
        *,
        evaluations: dict[str, Any],
        prediction_by_fixture_id: dict[str, dict[str, Any]],
        policy: PortfolioPolicy,
    ) -> tuple[dict[str, list[PortfolioCandidate]], dict[str, str], set[str]]:
        counted_rows_by_trade_id, canonical_fixture_id_by_trade_id = self._counted_portfolio_paper_rows(conn, policy=policy)
        if not counted_rows_by_trade_id:
            return {}, {}, set()
        tracked_canonical_fixture_ids: set[str] = set()
        snapshot_at_by_canonical_fixture_id: dict[str, str] = {}
        candidates_by_canonical_fixture_id: dict[str, list[PortfolioCandidate]] = defaultdict(list)
        counted_canonical_fixture_ids = set(canonical_fixture_id_by_trade_id.values())
        canonical_fixture_id_by_fixture_id = football_canonical_fixture.fetch_canonical_fixture_ids_for_fixture_ids(
            conn,
            {str(fixture_id) for fixture_id in prediction_by_fixture_id},
            initialize_schema=self._read_requires_schema_init(),
        )
        for fixture_id, evaluation in evaluations.items():
            decision_rows = list((evaluation or {}).get("decision_rows") or [])
            if not decision_rows:
                continue
            canonical_fixture_id = canonical_fixture_id_by_fixture_id.get(str(fixture_id))
            if canonical_fixture_id is None or canonical_fixture_id not in counted_canonical_fixture_ids:
                continue
            tracked_canonical_fixture_ids.add(canonical_fixture_id)
            snapshot_at_by_canonical_fixture_id[canonical_fixture_id] = self._latest_portfolio_timestamp(
                [snapshot_at_by_canonical_fixture_id.get(canonical_fixture_id)]
                + [self._portfolio_observed_at(row) for row in decision_rows],
                fallback=utc_now_iso(),
            )
            for row in decision_rows:
                if not self._portfolio_current_row_is_actionable(row):
                    continue
                trade_id = str(row.get("paper_trade_id") or "")
                paper_row = counted_rows_by_trade_id.get(trade_id)
                if paper_row is None or canonical_fixture_id_by_trade_id.get(trade_id) != canonical_fixture_id:
                    continue
                observed_at = self._portfolio_observed_at(row) or str(paper_row.get("decision_timestamp") or "")
                candidates_by_canonical_fixture_id[canonical_fixture_id].append(
                    PortfolioCandidate(
                        paper_trade_id=trade_id,
                        canonical_fixture_id=canonical_fixture_id,
                        market=str(row.get("market_family") or paper_row["market"]),
                        selection=str(row.get("selection") or paper_row["selection"]),
                        line=None if row.get("line") is None else float(row["line"]),
                        settlement_aware_edge=None
                        if row.get("settlement_aware_edge") is None
                        else float(row["settlement_aware_edge"]),
                        decision_timestamp=observed_at,
                        created_at=observed_at,
                        safeguard_state=str(row.get("day12_state") or paper_row.get("safeguard_state") or ""),
                        freshness_state=str(row.get("market_freshness") or row.get("freshness_state") or ""),
                    )
                )
        return candidates_by_canonical_fixture_id, snapshot_at_by_canonical_fixture_id, tracked_canonical_fixture_ids

    def _sync_current_portfolio_selections(
        self,
        conn: sqlite3.Connection,
        *,
        evaluations: dict[str, Any],
        prediction_by_fixture_id: dict[str, dict[str, Any]],
    ) -> None:
        policy = self._portfolio_policy()
        if policy is None or not evaluations:
            return
        (
            candidates_by_canonical_fixture_id,
            snapshot_at_by_canonical_fixture_id,
            tracked_canonical_fixture_ids,
        ) = self._portfolio_candidates_for_current_evaluations(
            conn,
            evaluations=evaluations,
            prediction_by_fixture_id=prediction_by_fixture_id,
            policy=policy,
        )
        if not tracked_canonical_fixture_ids:
            return
        latest_evaluation_by_canonical_fixture_id = {
            str(row["canonical_fixture_id"]): row
            for row in football_portfolio_selection.fetch_current_portfolio_evaluations(
                conn,
                policy_id=policy.policy_id,
                policy_version=policy.policy_version,
                initialize_schema=self._read_requires_schema_init(),
            )
        }
        evaluated_at = utc_now_iso()
        for canonical_fixture_id in sorted(tracked_canonical_fixture_ids):
            candidates = candidates_by_canonical_fixture_id.get(canonical_fixture_id, [])
            evaluation_signature = football_portfolio_selection.portfolio_evaluation_signature(
                policy=policy,
                canonical_fixture_id=canonical_fixture_id,
                candidates=[
                    {
                        "paper_trade_id": candidate.paper_trade_id,
                        "market": candidate.market,
                        "selection": candidate.selection,
                        "line": candidate.line,
                        "edge_at_selection": candidate.settlement_aware_edge,
                        "decision_timestamp": candidate.decision_timestamp,
                        "created_at": candidate.created_at,
                        "safeguard_state": candidate.safeguard_state,
                    }
                    for candidate in candidates
                ],
            )
            dispositions = [] if not candidates else list(select_fixture_portfolio(candidates, policy=policy).dispositions)
            latest_row = latest_evaluation_by_canonical_fixture_id.get(canonical_fixture_id)
            football_portfolio_selection.record_portfolio_evaluation(
                conn,
                policy=policy,
                canonical_fixture_id=canonical_fixture_id,
                evaluated_at=evaluated_at,
                candidate_snapshot_at=snapshot_at_by_canonical_fixture_id.get(canonical_fixture_id, evaluated_at),
                evaluation_signature=evaluation_signature,
                dispositions=dispositions,
                initialize_schema=self._read_requires_schema_init(),
                latest_evaluation_id=None if latest_row is None else str(latest_row["evaluation_id"]),
                latest_evaluation_signature=None if latest_row is None else str(latest_row["evaluation_signature"]),
            )

    def _sync_portfolio_selection_for_prediction(
        self,
        conn: sqlite3.Connection,
        *,
        prediction: dict[str, Any],
        evaluation: dict[str, Any] | None = None,
    ) -> None:
        fixture_id = str(prediction.get("fixture_id") or "")
        current_evaluation = self._market_evaluation_for_fixture(fixture_id) if evaluation is None else evaluation
        if not fixture_id or current_evaluation is None:
            return
        self._sync_current_portfolio_selections(
            conn,
            evaluations={fixture_id: current_evaluation},
            prediction_by_fixture_id={fixture_id: prediction},
        )

    def _published_pick_rows(
        self,
        paper_rows: list[dict[str, Any]],
        *,
        selection_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        context = self._portfolio_selection_context(paper_rows) if selection_context is None else selection_context
        source_rows = context["selected_rows"] if context.get("policy") is not None else paper_rows
        display_rows = self._paper_position_display_rows(source_rows)
        fixture_lookup = self._paper_fixture_lookup(source_rows)
        published_rows: list[dict[str, Any]] = []
        for row in display_rows:
            if row.get("fixture_id") in {None, ""}:
                continue
            fixture_id = str(row["fixture_id"])
            fixture = fixture_lookup.get(fixture_id) or {}
            market_evaluation = self._market_evaluation_for_fixture(fixture_id)
            current_rows = {
                _market_identity(current_row): current_row
                for current_row in (market_evaluation or {}).get("decision_rows") or []
            }
            current_row = current_rows.get(_market_identity(row))
            entry_odds = None if row.get("entry_odds") is None else float(row["entry_odds"])
            model_probability = None if row.get("model_probability") is None else float(row["model_probability"])
            fair_odds = None if row.get("model_fair_price") is None else float(row["model_fair_price"])
            entry_edge = None if row.get("edge") is None else float(row["edge"])
            published_rows.append(
                {
                    **row,
                    "kickoff_utc": row.get("kickoff") or fixture.get("kickoff_utc"),
                    "kickoff_time_local": "N/A"
                    if not (row.get("kickoff") or fixture.get("kickoff_utc"))
                    else parse_iso_datetime(str(row.get("kickoff") or fixture.get("kickoff_utc")))
                    .astimezone(ZoneInfo(self.config.display_timezone))
                    .strftime("%H:%M"),
                    "competition_id": str(row.get("competition_id") or fixture.get("competition_id") or ""),
                    "competition_name": str(row.get("competition_name") or fixture.get("competition_name") or "Unknown League"),
                    "home_team_name": str(row.get("home_team_name") or fixture.get("home_team_name") or "Home"),
                    "away_team_name": str(row.get("away_team_name") or fixture.get("away_team_name") or "Away"),
                    "model_version": str(row.get("model_version") or ""),
                    "implied_probability": None if entry_odds is None or entry_odds <= 0 else (1.0 / entry_odds),
                    "confidence_label": str(row.get("safeguard_state") or "N/A").replace("_", " ").title(),
                    "entry_model_probability": model_probability,
                    "model_probability": model_probability,
                    "entry_fair_odds": fair_odds,
                    "fair_odds": fair_odds,
                    "market_odds": entry_odds,
                    "entry_edge": entry_edge,
                    "edge": entry_edge,
                    "paper_trade_state": str(row.get("trade_state") or "PENDING"),
                    "paper_qualification_state": "QUALIFIED",
                    "paper_result_tone": row.get("result_tone"),
                    "current_market_odds": None
                    if current_row is None or current_row.get("best_available_price") is None
                    else float(current_row["best_available_price"]),
                    "current_market_bookmaker": None
                    if current_row is None or current_row.get("best_available_bookmaker") is None
                    else str(current_row["best_available_bookmaker"]),
                    "current_market_timestamp": None
                    if current_row is None
                    else current_row.get("best_available_source_timestamp"),
                    "current_market_edge": None
                    if current_row is None or current_row.get("settlement_aware_edge") is None
                    else float(current_row["settlement_aware_edge"]),
                    "current_decision_state": None
                    if current_row is None
                    else str(current_row.get("current_decision_state") or _current_decision_state(current_row)),
                    "current_decision_reason": None
                    if current_row is None
                    else (_humanize_row_reason_codes(current_row) or _decision_state_detail(
                        str(current_row.get("current_decision_state") or _current_decision_state(current_row))
                    )),
                }
            )
        return self._annotate_rows_with_portfolio(
            sorted(
                published_rows,
                key=lambda row: (
                    str(row.get("kickoff_utc") or ""),
                    str(row.get("decision_timestamp") or ""),
                    str(row["paper_trade_id"]),
                ),
            ),
            records=context,
            qualified_predicate=lambda row: True,
        )

    @staticmethod
    def _paper_summary_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
        counts = Counter(str(row.get("trade_state") or "PENDING") for row in rows)
        return {
            "total": len(rows),
            "pending": sum(counts.get(state, 0) for state in PAPER_ACTIVE_STATES),
            "settled": sum(counts.get(state, 0) for state in PAPER_FINAL_STATES),
            "won": counts.get("WON", 0),
            "lost": counts.get("LOST", 0),
            "push": counts.get("PUSH", 0),
            "half_win": counts.get("HALF_WIN", 0),
            "half_loss": counts.get("HALF_LOSS", 0),
            "void": counts.get("VOID", 0),
        }

    @staticmethod
    def _paper_performance_for_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return football_paper_trading.paper_trading_performance(rows)

    @staticmethod
    def _format_rate(value: Any) -> str:
        if value is None:
            return "NOT AVAILABLE"
        return f"{float(value):.2%}"

    @staticmethod
    def _format_units(value: Any) -> str:
        if value is None:
            return "NOT AVAILABLE"
        return f"{float(value):+.2f}"

    @staticmethod
    def _paper_metric_row(*, label: str, value: Any, note: str, formatter: str = "count") -> dict[str, str]:
        if formatter == "rate":
            display = DashboardServices._format_rate(value)
        elif formatter == "units":
            display = DashboardServices._format_units(value)
        else:
            display = str(int(value or 0))
        return {"label": label, "value": display, "note": note}

    def _today_kpi_strip(
        self,
        *,
        analysed_count: int,
        predictions_count: int,
        approved_count: int,
        no_bet_count: int,
        paper_rows: list[dict[str, Any]],
        portfolio_rows: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        """Today KPI strip split into CURRENT DESK and PAPER TRACKING scopes."""
        desk = [
            self._paper_metric_row(label="Analysed", value=analysed_count, note="current desk · unique today fixtures"),
            self._paper_metric_row(label="Predictions", value=predictions_count, note="current desk rows for today"),
            self._paper_metric_row(label="BET", value=approved_count, note="current desk · passed decision gates"),
            self._paper_metric_row(label="NO BET", value=no_bet_count, note="current desk · retained rejections"),
        ]
        performance = self._paper_performance_for_rows(paper_rows)
        paper = [
            self._paper_metric_row(label="Tracked", value=performance.get("positions"), note="canonical paper positions"),
            self._paper_metric_row(label="Open", value=performance.get("open_positions"), note="unsettled paper positions"),
            self._paper_metric_row(label="Settled", value=performance.get("settled_positions"), note="terminal paper positions"),
            self._paper_metric_row(label="Won", value=performance.get("wins"), note="full wins"),
            self._paper_metric_row(label="Lost", value=performance.get("losses"), note="full losses"),
            self._paper_metric_row(label="P/L", value=performance.get("net_units"), note="net paper units", formatter="units"),
            self._paper_metric_row(label="ROI", value=performance.get("roi"), note="settled stake only", formatter="rate"),
            self._paper_metric_row(label="Strike Rate", value=performance.get("strike_rate"), note="pushes excluded", formatter="rate"),
        ]
        portfolio_performance = self._paper_performance_for_rows(portfolio_rows)
        portfolio = [
            self._paper_metric_row(label="Portfolio Bets", value=len(portfolio_rows), note="current portfolio-selected positions"),
            self._paper_metric_row(
                label="Matches",
                value=len(
                    {
                        str(row.get("portfolio_canonical_fixture_id") or "")
                        for row in portfolio_rows
                        if row.get("portfolio_canonical_fixture_id") not in {None, ""}
                    }
                ),
                note="unique canonical fixtures in current portfolio",
            ),
            self._paper_metric_row(
                label="Open Portfolio",
                value=sum(1 for row in portfolio_rows if str(row.get("trade_state") or "PENDING") in PAPER_ACTIVE_STATES),
                note="open portfolio exposure in 1u positions",
            ),
            self._paper_metric_row(
                label="Portfolio ROI",
                value=portfolio_performance.get("roi"),
                note="selected-only settled stake",
                formatter="rate",
            ),
        ]
        return desk + paper + portfolio

    @staticmethod
    def _settled_bet_sample_label(settled_count: int) -> str:
        noun = "BET" if settled_count == 1 else "BETS"
        return f"{settled_count} SETTLED {noun}"

    def _live_paper_sample_callout(self, paper_rows: list[dict[str, Any]]) -> dict[str, str] | None:
        performance = self._paper_performance_for_rows(paper_rows)
        settled_count = int(performance.get("settled_positions") or 0)
        if settled_count >= 5:
            return None
        return {
            "headline": "LOW SAMPLE",
            "sample_label": self._settled_bet_sample_label(settled_count),
            "detail": "ROI and strike rate are directional only until at least 5 settled live paper bets exist.",
        }

    @staticmethod
    def _value_candidate_row(row: dict[str, Any]) -> bool:
        return str(row.get("decision") or "") == "PRELIMINARY_CANDIDATE"

    @staticmethod
    def _market_eligible_row(row: dict[str, Any]) -> bool:
        return DashboardServices._value_candidate_row(row) and str(row.get("market_freshness") or "") == "FRESH"

    @staticmethod
    def _safeguard_approved_row(row: dict[str, Any]) -> bool:
        day12_state = str(row.get("day12_state") or "")
        return DashboardServices._market_eligible_row(row) and day12_state in {"NOT_APPLICABLE", "PASS", "PASSED"}

    def _blocking_reason_key(self, row: dict[str, Any]) -> str:
        current_state = str(row.get("current_decision_state") or _current_decision_state(row))
        reason_codes = {str(code) for code in (row.get("reason_codes") or [])}
        if current_state == "NO_BET_STALE_MARKET":
            return "NO_BET_STALE_MARKET"
        if current_state == "NO_BET_SAFEGUARD":
            return "NO_BET_SAFEGUARD"
        if "MISSING_REFERENCE_MARKET" in reason_codes or "INCOMPLETE_REFERENCE_MARKET" in reason_codes:
            return "MISSING_REFERENCE_MARKET"
        if current_state == "NO_BET_VALUE":
            return "NO_BET_VALUE"
        if current_state == "NO_BET_MISSING_PRICE" or "MISSING_AVAILABLE_PRICE" in reason_codes:
            return "MARKET_UNAVAILABLE"
        if "LINE_UNSUPPORTED" in reason_codes or "NO_LINE_MATCH" in reason_codes:
            return "LINE_UNSUPPORTED"
        market_state = str(self._market_provider_state().get("state") or "")
        if market_state in {"QUOTA_EXHAUSTED", "QUOTA_GUARD", "RATE_LIMITED", "AUTH_ERROR"}:
            return "PROVIDER_QUOTA_BLOCKED"
        if str(row.get("day12_state") or "") == "BLOCKED_MISSING_XG_DATA":
            return "AGENT_BLOCKED"
        if current_state == "UNSUPPORTED":
            return "LINE_UNSUPPORTED"
        return "Other"

    def _blocking_reason_counts(self, working_rows: list[dict[str, Any]]) -> dict[str, int]:
        counts = {
            "NO_BET_VALUE": 0,
            "NO_BET_SAFEGUARD": 0,
            "NO_BET_STALE_MARKET": 0,
            "MISSING_REFERENCE_MARKET": 0,
            "MARKET_UNAVAILABLE": 0,
            "LINE_UNSUPPORTED": 0,
            "PROVIDER_QUOTA_BLOCKED": 0,
            "AGENT_BLOCKED": 0,
            "Other": 0,
        }
        for row in working_rows:
            if str(row.get("current_decision_group") or "") == "bet":
                continue
            counts[self._blocking_reason_key(row)] = counts.get(self._blocking_reason_key(row), 0) + 1
        return counts

    @staticmethod
    def _client_blocking_reasons(counts: dict[str, int]) -> dict[str, int]:
        client_counts = {
            "Stale market": int(counts.get("NO_BET_STALE_MARKET") or 0),
            "Missing reference": int(counts.get("MISSING_REFERENCE_MARKET") or 0)
            + int(counts.get("MARKET_UNAVAILABLE") or 0),
            "No value": int(counts.get("NO_BET_VALUE") or 0),
            "Safeguard": int(counts.get("NO_BET_SAFEGUARD") or 0),
        }
        optional_labels = (
            ("Unsupported", "LINE_UNSUPPORTED"),
            ("Provider blocked", "PROVIDER_QUOTA_BLOCKED"),
            ("Agent blocked", "AGENT_BLOCKED"),
            ("Other", "Other"),
        )
        for label, key in optional_labels:
            value = int(counts.get(key) or 0)
            if value > 0:
                client_counts[label] = value
        return client_counts

    @staticmethod
    def _today_no_bet_reason_summary(counts: dict[str, int]) -> str:
        return (
            f"stale {int(counts.get('NO_BET_STALE_MARKET') or 0)} · "
            f"missing reference {int(counts.get('MISSING_REFERENCE_MARKET') or 0) + int(counts.get('MARKET_UNAVAILABLE') or 0)} · "
            f"no value {int(counts.get('NO_BET_VALUE') or 0)} · "
            f"safeguard {int(counts.get('NO_BET_SAFEGUARD') or 0)}"
        )

    def _today_funnel(
        self,
        *,
        working_rows: list[dict[str, Any]],
        analysed_count: int,
        blocking_reasons: dict[str, int],
    ) -> list[dict[str, str]]:
        bet_count = sum(1 for row in working_rows if row.get("current_decision_group") == "bet")
        no_bet_count = max(0, len(working_rows) - bet_count)
        return [
            {"label": "Fixtures Analysed", "value": str(analysed_count), "note": "unique in-scope fixtures"},
            {
                "label": "Decision Rows",
                "value": str(len(working_rows)),
                "note": "persisted Route B market rows",
            },
            {
                "label": "BET",
                "value": str(bet_count),
                "note": "passed decision gates",
            },
            {
                "label": "NO BET",
                "value": str(no_bet_count),
                "note": self._today_no_bet_reason_summary(blocking_reasons),
            },
        ]

    def _last_20_payload(self, paper_rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Latest 20 SETTLED canonical counted positions (open/pending excluded)."""
        settled_rows = sorted(
            [
                row
                for row in paper_rows
                if str(row.get("trade_state") or "") in PAPER_FINAL_STATES
            ],
            key=lambda row: (
                str(row.get("updated_at") or row.get("decision_timestamp") or ""),
                str(row["paper_trade_id"]),
            ),
            reverse=True,
        )[:20]
        recent_performance = self._paper_performance_for_rows(settled_rows)
        return {
            "rows": settled_rows,
            "sequence": [str(row.get("trade_state") or "PENDING") for row in settled_rows],
            "summary": {
                "count": len(settled_rows),
                "wins": sum(1 for row in settled_rows if row.get("trade_state") == "WON"),
                "losses": sum(1 for row in settled_rows if row.get("trade_state") == "LOST"),
                "pushes": sum(1 for row in settled_rows if row.get("trade_state") == "PUSH"),
                "half_wins": sum(1 for row in settled_rows if row.get("trade_state") == "HALF_WIN"),
                "half_losses": sum(1 for row in settled_rows if row.get("trade_state") == "HALF_LOSS"),
                "voids": sum(1 for row in settled_rows if row.get("trade_state") == "VOID"),
                "strike_rate": recent_performance.get("strike_rate"),
                "roi": recent_performance.get("roi"),
            },
        }

    def _cumulative_profit_payload(self, paper_rows: list[dict[str, Any]]) -> dict[str, Any]:
        settled_rows = sorted(
            [
                row
                for row in paper_rows
                if str(row.get("trade_state") or "") in PAPER_FINAL_STATES
            ],
            key=lambda row: (
                str(row.get("updated_at") or row.get("decision_timestamp") or ""),
                str(row["paper_trade_id"]),
            ),
        )
        points: list[dict[str, Any]] = []
        cumulative_units = 0.0
        for index, row in enumerate(settled_rows, start=1):
            cumulative_units += float(row.get("net_pnl") or 0.0)
            points.append(
                {
                    "index": index,
                    "paper_trade_id": str(row["paper_trade_id"]),
                    "label": str(row.get("updated_at") or row.get("decision_timestamp") or ""),
                    "trade_state": str(row.get("trade_state") or ""),
                    "net_units": float(row.get("net_pnl") or 0.0),
                    "cumulative_units": cumulative_units,
                }
            )
        performance = self._paper_performance_for_rows(settled_rows)
        chart_points = ""
        if points:
            min_units = min(float(point["cumulative_units"]) for point in points)
            max_units = max(float(point["cumulative_units"]) for point in points)
            spread = max(max_units - min_units, 1.0)
            rendered_points = []
            for index, point in enumerate(points):
                x = 0 if len(points) == 1 else round((index / (len(points) - 1)) * 100, 2)
                y = round(100 - (((float(point["cumulative_units"]) - min_units) / spread) * 100), 2)
                rendered_points.append(f"{x},{y}")
            chart_points = " ".join(rendered_points)
        return {
            "points": points,
            "chart_points": chart_points,
            "net_units": performance.get("net_units"),
            "roi": performance.get("roi"),
            "settled_count": len(settled_rows),
            "low_sample": len(settled_rows) < 5,
        }

    def _age_minutes(self, timestamp_value: Any) -> int | None:
        if timestamp_value in {None, ""}:
            return None
        try:
            age = datetime.now(timezone.utc) - parse_iso_datetime(str(timestamp_value))
        except Exception:
            return None
        return max(0, int(age.total_seconds() // 60))

    def _market_runtime_decision_rows(self) -> list[dict[str, Any]]:
        evaluations = dict(self._market_runtime_state.get("evaluations") or {})
        if not evaluations:
            evaluations = dict(self._market_evaluation_map())
        decision_rows: list[dict[str, Any]] = []
        for evaluation in evaluations.values():
            decision_rows.extend(list(evaluation.get("decision_rows") or []))
        return decision_rows

    def _odds_freshness_summary(self) -> dict[str, str]:
        decision_rows = self._market_runtime_decision_rows()
        if not decision_rows:
            return {"value": "NO DECISIONS", "note": "no persisted decision-row freshness yet"}

        freshness_counts = Counter(
            str(row.get("market_freshness") or row.get("freshness_state") or "UNKNOWN")
            for row in decision_rows
        )
        timestamp_values = [
            str(row["best_available_source_timestamp"])
            for row in decision_rows
            if row.get("best_available_source_timestamp")
        ]
        latest_timestamp = None if not timestamp_values else max(timestamp_values, key=parse_iso_datetime)
        latest_age = self._age_minutes(latest_timestamp)
        latest_note = ""
        if latest_age is not None:
            latest_note = f" · latest source {latest_age}m ago"

        fresh_count = int(freshness_counts.get("FRESH") or 0)
        stale_count = int(freshness_counts.get("STALE") or 0)
        unknown_count = sum(
            count
            for freshness, count in freshness_counts.items()
            if freshness not in {"FRESH", "STALE"}
        )
        if stale_count == 0 and unknown_count == 0:
            return {
                "value": "ALL FRESH",
                "note": f"{fresh_count} decision rows fresh{latest_note}",
            }
        if fresh_count == 0 and unknown_count == 0:
            return {
                "value": "ALL STALE",
                "note": f"{stale_count} decision rows stale{latest_note}",
            }

        note_parts: list[str] = []
        if fresh_count > 0:
            note_parts.append(f"{fresh_count} fresh")
        if stale_count > 0:
            note_parts.append(f"{stale_count} stale")
        if unknown_count > 0:
            note_parts.append(f"{unknown_count} unknown")
        note = ", ".join(note_parts) if note_parts else "freshness mix unavailable"
        return {"value": "MIXED FRESHNESS", "note": f"{note}{latest_note}"}

    def _system_health_strip(
        self,
        *,
        system_status: dict[str, Any],
        paper_rows: list[dict[str, Any]],
        performance_generated_at: str | None,
    ) -> list[dict[str, str]]:
        odds_state = str(system_status["odds_feed"]["state"])
        fixture_state = str(system_status["fixture_feed"]["state"])
        odds_provider = str(system_status["odds_feed"]["provider"] or "N/A")
        market_sync = dict(system_status.get("market_sync") or {})
        last_odds_refresh = (
            market_sync.get("last_sync_at")
            or market_sync.get("last_successful_snapshot_at")
            or market_sync.get("captured_at")
            or system_status["market_provider"].get("captured_at")
        )
        freshness_summary = self._odds_freshness_summary()
        oldest_unsettled = None
        open_rows = [
            row
            for row in paper_rows
            if str(row.get("trade_state") or "") in PAPER_ACTIVE_STATES
        ]
        if open_rows:
            oldest_unsettled = min(
                (row.get("kickoff") or row.get("decision_timestamp") for row in open_rows if row.get("kickoff") or row.get("decision_timestamp")),
                default=None,
            )
        settlement_health = "READY"
        if open_rows and all(str(row.get("trade_state") or "") == "UNSETTLED" for row in open_rows):
            settlement_health = "RESULTS PENDING"
        return [
            {"label": "Fixture Feed", "value": fixture_state, "note": str(system_status["fixture_feed"]["provider"])},
            {"label": "Odds Feed", "value": odds_state, "note": odds_provider},
            {"label": "Odds Provider", "value": odds_provider, "note": str(system_status["market_provider"]["current_market_decisions"])},
            {
                "label": "Odds Data Age",
                "value": freshness_summary["value"],
                "note": freshness_summary["note"],
            },
            {
                "label": "Last Odds Refresh",
                "value": "UNAVAILABLE" if last_odds_refresh is None else str(last_odds_refresh),
                "note": "latest market sync checkpoint",
            },
            {
                "label": "Oldest Unsettled Pick",
                "value": "NONE" if oldest_unsettled is None else str(oldest_unsettled),
                "note": "oldest active paper position",
            },
            {"label": "Settlement Health", "value": settlement_health, "note": "paper result processing"},
            {
                "label": "Analytics Refresh",
                "value": "UNAVAILABLE" if performance_generated_at is None else str(performance_generated_at),
                "note": "performance summary timestamp",
            },
        ]

    def _signal_payload(
        self,
        *,
        system_status: dict[str, Any],
        paper_rows: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        signals: list[dict[str, str]] = []
        odds_state = str(system_status["odds_feed"]["state"])
        if odds_state not in {"CONNECTED", "READY", "IDLE", "NO_CURRENT_EVENTS"}:
            signals.append(
                {
                    "category": "PROVIDER WARNING",
                    "title": "Odds feed degraded",
                    "detail": str(system_status["odds_feed"].get("reason") or odds_state),
                }
            )
        recent_settled = [
            row for row in sorted(
                [row for row in paper_rows if str(row.get("trade_state") or "") in PAPER_FINAL_STATES],
                key=lambda row: (
                    str(row.get("updated_at") or row.get("decision_timestamp") or ""),
                    str(row["paper_trade_id"]),
                ),
                reverse=True,
            )[:5]
        ]
        if len(recent_settled) >= 3 and all(str(row.get("trade_state") or "") in {"LOST", "HALF_LOSS"} for row in recent_settled[:3]):
            signals.append(
                {
                    "category": "LOSING STREAK",
                    "title": "Recent paper form is cold",
                    "detail": "Last three settled paper picks were losses or half-losses.",
                }
            )
        settled_performance = self._paper_performance_for_rows(
            [row for row in paper_rows if str(row.get("trade_state") or "") in PAPER_FINAL_STATES]
        )
        if int(settled_performance.get("settled_positions") or 0) < 5:
            signals.append(
                {
                    "category": "CALIBRATION WARNING",
                    "title": "Low live paper sample",
                    "detail": "Route B needs more settled paper picks before strong tuning conclusions.",
                }
            )
        strong_market = next(
            (
                market
                for market, summary in sorted((settled_performance.get("by_market") or {}).items())
                if int(summary.get("settled_positions") or 0) >= 5 and float(summary.get("roi") or 0.0) > 0.05
            ),
            None,
        )
        if strong_market is not None:
            signals.append(
                {
                    "category": "STRONG AREA",
                    "title": f"{strong_market.replace('_', ' ').title()} is outperforming",
                    "detail": "Current live paper ROI is positive with at least five settled picks.",
                }
            )
        return signals

    @staticmethod
    def _paper_group_label(*, kind: str, key: str) -> str:
        if kind == "market":
            return _humanize_market_selection(
                market_family=key,
                selection="HOME",
                line=None,
                home_team_name="Home",
                away_team_name="Away",
            )["market_label"]
        return key

    def _paper_breakdown_rows(
        self,
        *,
        paper_rows: list[dict[str, Any]],
        kind: str,
        allowed_keys: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in paper_rows:
            if kind == "market":
                key = str(row.get("market") or "")
            else:
                key = str(row.get("competition_id") or "")
            if not key:
                continue
            grouped[key].append(row)
        if allowed_keys is None:
            keys = sorted(grouped)
        else:
            keys = [key for key in allowed_keys if key in grouped]
        breakdown_rows: list[dict[str, Any]] = []
        for key in keys:
            rows = grouped[key]
            performance = self._paper_performance_for_rows(rows)
            breakdown_rows.append(
                {
                    "key": key,
                    "label": self._paper_group_label(kind=kind, key=key),
                    "bets": len(rows),
                    "settled": int(performance.get("settled_positions") or 0),
                    "wins": int(performance.get("wins") or 0),
                    "losses": int(performance.get("losses") or 0),
                    "strike_rate": performance.get("strike_rate"),
                    "net_units": performance.get("net_units"),
                    "roi": performance.get("roi"),
                    "low_sample": int(performance.get("settled_positions") or 0) < 5,
                }
            )
        return breakdown_rows

    def _segment_explorer_payload(self, *, paper_rows: list[dict[str, Any]]) -> dict[str, Any]:
        by_market = self._paper_breakdown_rows(paper_rows=paper_rows, kind="market")
        by_league = self._paper_breakdown_rows(paper_rows=paper_rows, kind="league")
        by_model: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in paper_rows:
            by_model[str(row.get("model_version") or "UNKNOWN")].append(row)
        model_rows = []
        for model_version, grouped_rows in sorted(by_model.items()):
            performance = self._paper_performance_for_rows(grouped_rows)
            model_rows.append(
                {
                    "label": model_version,
                    "bets": len(grouped_rows),
                    "settled": int(performance.get("settled_positions") or 0),
                    "strike_rate": performance.get("strike_rate"),
                    "net_units": performance.get("net_units"),
                    "roi": performance.get("roi"),
                    "low_sample": int(performance.get("settled_positions") or 0) < 5,
                }
            )
        return {
            "dimensions": [
                {"key": "market", "label": "Market"},
                {"key": "league", "label": "League"},
                {"key": "model_version", "label": "Model Version"},
            ],
            "by_market": by_market,
            "by_league": by_league,
            "by_model_version": model_rows,
            "unsupported_dimensions": [
                "probability_band",
                "odds_band",
                "confidence",
                "home_away",
            ],
        }

    def _reports_payload(self, *, paper_rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {"windows": self._report_window_rows(paper_rows)}

    def _report_window_rows(self, paper_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now_local = datetime.now(timezone.utc).astimezone(ZoneInfo(self.config.display_timezone))
        today_local = now_local.date()
        yesterday_local = today_local - timedelta(days=1)
        windows = {
            "Today": lambda row: self._local_date_for_timestamp(row.get("kickoff") or row.get("decision_timestamp")) == today_local,
            "Yesterday": lambda row: self._local_date_for_timestamp(row.get("kickoff") or row.get("decision_timestamp")) == yesterday_local,
            "7D": lambda row: (
                (local_date := self._local_date_for_timestamp(row.get("kickoff") or row.get("decision_timestamp"))) is not None
                and local_date >= today_local - timedelta(days=6)
            ),
            "30D": lambda row: (
                (local_date := self._local_date_for_timestamp(row.get("kickoff") or row.get("decision_timestamp"))) is not None
                and local_date >= today_local - timedelta(days=29)
            ),
            "90D": lambda row: (
                (local_date := self._local_date_for_timestamp(row.get("kickoff") or row.get("decision_timestamp"))) is not None
                and local_date >= today_local - timedelta(days=89)
            ),
            "This Month": lambda row: (
                (local_date := self._local_date_for_timestamp(row.get("kickoff") or row.get("decision_timestamp"))) is not None
                and local_date.year == today_local.year
                and local_date.month == today_local.month
            ),
            "All Time": lambda row: True,
        }
        report_rows: list[dict[str, Any]] = []
        for label, predicate in windows.items():
            scoped_rows = [row for row in paper_rows if predicate(row)]
            performance = self._paper_performance_for_rows(scoped_rows)
            report_rows.append(
                {
                    "window": label,
                    "bets": len(scoped_rows),
                    "settled": int(performance.get("settled_positions") or 0),
                    "wins": int(performance.get("wins") or 0),
                    "losses": int(performance.get("losses") or 0),
                    "strike_rate": performance.get("strike_rate"),
                    "net_units": performance.get("net_units"),
                    "roi": performance.get("roi"),
                }
            )
        return report_rows

    def _published_paper_entries_for_fixture(
        self,
        fixture_id: str,
        market_evaluation: dict[str, Any] | None,
        paper_rows: list[dict[str, Any]],
        *,
        selection_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        context = self._portfolio_selection_context(paper_rows) if selection_context is None else selection_context
        source_rows = context["selected_rows"] if context.get("policy") is not None else paper_rows
        fixture_paper_rows = [row for row in source_rows if str(row["fixture_id"]) == fixture_id]
        display_rows = self._paper_position_display_rows(fixture_paper_rows)
        current_rows = {
            _market_identity(row): row
            for row in (market_evaluation or {}).get("decision_rows") or []
        }
        published_rows: list[dict[str, Any]] = []
        for row in display_rows:
            current_row = current_rows.get(_market_identity(row))
            published_rows.append(
                {
                    **row,
                    "current_market_odds": None
                    if current_row is None or current_row.get("best_available_price") is None
                    else float(current_row["best_available_price"]),
                    "current_market_edge": None
                    if current_row is None or current_row.get("settlement_aware_edge") is None
                    else float(current_row["settlement_aware_edge"]),
                    "current_market_bookmaker": None
                    if current_row is None or current_row.get("best_available_bookmaker") is None
                    else str(current_row["best_available_bookmaker"]),
                    "current_market_timestamp": None
                    if current_row is None
                    else current_row.get("best_available_source_timestamp"),
                    "current_market_state": None
                    if current_row is None
                    else str(current_row.get("current_decision_state") or _current_decision_state(current_row)),
                    "current_market_safeguard": None
                    if current_row is None
                    else _safeguard_label(current_row),
                }
            )
        return self._annotate_rows_with_portfolio(
            sorted(
                published_rows,
                key=lambda row: (
                    str(row.get("decision_timestamp") or ""),
                    str(row["paper_trade_id"]),
                ),
            ),
            records=context,
            qualified_predicate=lambda row: True,
        )

    def _decision_gates_payload(
        self,
        *,
        market_evaluation: dict[str, Any] | None,
        prediction: dict[str, Any] | None,
        system_status: dict[str, Any],
    ) -> dict[str, str]:
        if market_evaluation and market_evaluation.get("display_rows"):
            top_row = dict(market_evaluation["display_rows"][0])
            current_state = str(top_row.get("current_decision_state") or _current_decision_state(top_row))
            return {
                "source": "CURRENT_MARKET_EVALUATION",
                "current_market": current_state,
                "safeguard": _safeguard_label(top_row),
                "safeguard_reason": _safeguard_reason_from_row(top_row),
                "point_in_time_xg": _xg_status_label(top_row.get("xg_status")),
                "odds_feed": str(system_status["odds_feed"]["state"]),
            }
        decorated_prediction = self._decorate_prediction_runtime(prediction)
        return {
            "source": "PREDICTION_RECORD",
            "current_market": "NOT_SYNCED",
            "safeguard": str((decorated_prediction or {}).get("safeguard_status") or "N/A"),
            "safeguard_reason": str((decorated_prediction or {}).get("safeguard_reason") or "N/A"),
            "point_in_time_xg": str(system_status["xg"]["state"]),
            "odds_feed": str(system_status["odds_feed"]["state"]),
        }

    def _live_paper_market_rows(self, performance: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for market, market_row in sorted(
            (performance.get("by_market") or {}).items(),
            key=lambda item: self._paper_market_sort_key(str(item[0])),
        ):
            rows.append(
                {
                    "market": market,
                    "label": _humanize_market_selection(
                        market_family=str(market),
                        selection="HOME",
                        line=None,
                        home_team_name="Home",
                        away_team_name="Away",
                    )["market_label"],
                    **market_row,
                }
            )
        return rows

    @staticmethod
    def _live_paper_kpis(current_paper: dict[str, Any]) -> list[tuple[str, str]]:
        return [
            ("Tracked", str(current_paper.get("positions") or 0)),
            ("Settled", str(current_paper.get("settled_positions") or 0)),
            ("Won", str(current_paper.get("wins") or 0)),
            ("Lost", str(current_paper.get("losses") or 0)),
            ("Push", str(current_paper.get("pushes") or 0)),
            ("Pending", str(current_paper.get("pending_positions") or 0)),
            (
                "Strike Rate",
                "NOT AVAILABLE"
                if current_paper.get("strike_rate") is None
                else f"{float(current_paper['strike_rate']):.2%}",
            ),
            (
                "P/L",
                "NOT AVAILABLE"
                if current_paper.get("net_units") is None
                else f"{float(current_paper['net_units']):+.2f}",
            ),
            (
                "ROI",
                "NOT AVAILABLE"
                if current_paper.get("roi") is None
                else f"{float(current_paper['roi']):+.2%}",
            ),
        ]

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
        if self.config.runtime_role == RUNTIME_ROLE_WEB:
            return {
                "refresh_state": "DISABLED_IN_WEB_ROLE",
                "seconds_remaining": 0,
                "system_status": self.get_system_status(),
            }
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

    def _scheduled_refresh_dates(self, *, today_local: date) -> list[date]:
        refresh_dates: set[date] = {today_local}
        timezone_name = ZoneInfo(self.config.display_timezone)
        with football_paper_trading.connect(self.config.paper_trading_db_path) as conn:
            paper_rows = football_paper_trading.fetch_paper_trade_rows(
                conn,
                initialize_schema=self._read_requires_schema_init(),
            )
        for row in paper_rows:
            if str(row.get("trade_state") or "PENDING") not in PAPER_ACTIVE_STATES:
                continue
            kickoff_value = str(row.get("kickoff") or "").strip()
            if not kickoff_value:
                continue
            try:
                kickoff_local = parse_iso_datetime(kickoff_value).astimezone(timezone_name).date()
            except ValueError:
                continue
            if kickoff_local <= today_local:
                refresh_dates.add(kickoff_local)
        return sorted(refresh_dates)

    async def sync_once(
        self,
        *,
        sync_kind: str = "manual",
        include_schedule: bool = True,
    ) -> dict[str, Any]:
        async with self._sync_lock:
            return await asyncio.to_thread(
                self._sync_once_blocking,
                sync_kind=sync_kind,
                include_schedule=include_schedule,
            )

    def _sync_once_blocking(
        self,
        *,
        sync_kind: str = "manual",
        include_schedule: bool = True,
    ) -> dict[str, Any]:
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
                    initialize_schema=self._read_requires_schema_init(),
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
                    for refresh_date in self._scheduled_refresh_dates(today_local=today_local):
                        today.extend(self.fixture_provider.get_fixtures_for_date(refresh_date))
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
                    initialize_schema=self._read_requires_schema_init(),
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
                    initialize_schema=self._read_requires_schema_init(),
                )
                logger.warning("fixture sync degraded: %s", exc)
                return self._provider_state

            fixture_map = {fixture.fixture_id: fixture for fixture in [*live, *today, *upcoming]}
            sync_fixtures = sorted(fixture_map.values(), key=lambda item: (item.kickoff_utc, item.fixture_id))
            if sync_fixtures:
                storage.upsert_fixtures(
                    conn,
                    sync_fixtures,
                    updated_at=attempted_at,
                    initialize_schema=self._read_requires_schema_init(),
                )

            for fixture in sync_fixtures:
                if fixture.status in FIXTURE_TERMINAL_STATUSES:
                    continue
                self.ensure_prediction(conn, fixture)

            fixture_rows = storage.fetch_all_fixtures(conn, initialize_schema=self._read_requires_schema_init())
            predictions = storage.fetch_all_predictions(conn, initialize_schema=self._read_requires_schema_init())
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
            paid_fixture_provider = self.fixture_provider.provider_name() == "api-football"
            if paid_fixture_provider and not self.config.allow_paid_provider_calls:
                provider_state = PAID_PROVIDER_DISABLED_STATE
                provider_error_text = (
                    f"{PAID_PROVIDER_DISABLED_STATE}: set {DEFAULT_ALLOW_PAID_PROVIDER_CALLS_ENV_VAR}=1 "
                    "to enable paid provider polling"
                )
            elif provider_state == "CONNECTED":
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
            self._provider_state["xg_readiness"] = self._audit_current_xg_readiness(rows_with_predictions)
            xg_readiness = self._provider_state["xg_readiness"]
            logger.info(
                "football xg readiness: state=%s considered=%s ready=%s missing=%s competitions=%s missing_examples=%s",
                xg_readiness.get("state"),
                xg_readiness.get("fixtures_considered"),
                xg_readiness.get("fixtures_ready"),
                xg_readiness.get("fixtures_missing"),
                xg_readiness.get("competition_counts"),
                xg_readiness.get("missing_examples"),
            )
            self._run_market_sync(rows_with_predictions, conn)
            storage.put_runtime_state(
                conn,
                state_key="provider_state",
                state_value=self._provider_state,
                updated_at=attempted_at,
                initialize_schema=self._read_requires_schema_init(),
            )
            storage.put_runtime_state(
                conn,
                state_key="prediction_state",
                state_value=self._prediction_state,
                updated_at=attempted_at,
                initialize_schema=self._read_requires_schema_init(),
            )
            storage.put_runtime_state(
                conn,
                state_key="market_runtime_state",
                state_value=self._market_runtime_state,
                updated_at=attempted_at,
                initialize_schema=self._read_requires_schema_init(),
            )
            storage.put_runtime_state(
                conn,
                state_key="one_shot_sync_state",
                state_value=self._one_shot_sync_state,
                updated_at=attempted_at,
                initialize_schema=self._read_requires_schema_init(),
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
        provider_error = self._provider_state.get("last_error")
        if self._provider_state.get("state") != "CONNECTED" and provider_error:
            logger.warning(
                "fixture sync provider issue: kind=%s state=%s reason=%s",
                sync_kind,
                self._provider_state.get("state"),
                provider_error,
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

    def _canonical_fixture_for_prediction(self, conn: Any, prediction: dict[str, Any]) -> str | None:
        """Resolve (and register) the canonical fixture for a prediction."""
        fixture_id = str(prediction.get("fixture_id") or "")
        if not fixture_id:
            return None
        provider, provider_fixture_id = football_canonical_fixture.split_provider_fixture_id(fixture_id)
        if not (provider and provider_fixture_id):
            return None
        if not (prediction.get("competition_id") and prediction.get("home_team_name") and prediction.get("away_team_name") and prediction.get("kickoff_utc")):
            return None
        try:
            canonical_fixture_id, _ = football_canonical_fixture.resolve_or_create_canonical_fixture(
                conn,
                provider=provider,
                provider_fixture_id=provider_fixture_id,
                competition_id=str(prediction.get("competition_id")),
                home_team_name=str(prediction.get("home_team_name")),
                away_team_name=str(prediction.get("away_team_name")),
                kickoff_utc=str(prediction.get("kickoff_utc")),
                season=prediction.get("season"),
                initialize_schema=self._read_requires_schema_init(),
            )
            return canonical_fixture_id
        except football_canonical_fixture.CanonicalFixtureError:
            return None

    def _canonical_business_key_for_prediction(
        self,
        conn: Any,
        *,
        prediction: dict[str, Any],
        market: str,
        selection: str,
        line: float | None,
    ) -> str | None:
        canonical_fixture_id = self._canonical_fixture_for_prediction(conn, prediction)
        if canonical_fixture_id is None:
            return None
        return football_canonical_fixture.canonical_paper_business_key_signature(
            canonical_fixture_id=canonical_fixture_id,
            market=market,
            selection=selection,
            line=line,
        )

    def _find_canonical_existing_paper_entry(
        self,
        conn: Any,
        *,
        prediction: dict[str, Any],
        market: str,
        selection: str,
        line: float | None,
    ) -> dict[str, Any] | None:
        canonical_business_key = self._canonical_business_key_for_prediction(
            conn,
            prediction=prediction,
            market=market,
            selection=selection,
            line=line,
        )
        if canonical_business_key is None:
            return None
        return football_canonical_fixture.find_counted_paper_trade_by_canonical_key(
            conn,
            canonical_business_key,
            initialize_schema=self._read_requires_schema_init(),
        )

    def _maybe_create_paper_entries(
        self,
        conn: Any,
        *,
        prediction: dict[str, Any],
        evaluation: dict[str, Any],
        defer_portfolio_sync: bool = False,
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
                initialize_schema=self._read_requires_schema_init(),
            )
            if existing is not None:
                row["paper_trade_id"] = existing["paper_trade_id"]
                continue
            canonical_existing = self._find_canonical_existing_paper_entry(
                conn,
                prediction=prediction,
                market=str(row["market_family"]),
                selection=str(row["selection"]),
                line=None if row.get("line") is None else float(row["line"]),
            )
            if canonical_existing is not None:
                row["paper_trade_id"] = canonical_existing["paper_trade_id"]
                continue
            canonical_business_key_signature = self._canonical_business_key_for_prediction(
                conn,
                prediction=prediction,
                market=str(row["market_family"]),
                selection=str(row["selection"]),
                line=None if row.get("line") is None else float(row["line"]),
            )
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
                canonical_business_key_signature=canonical_business_key_signature,
            )
            try:
                football_paper_trading.record_paper_trade_entry(
                    conn,
                    entry,
                    initialize_schema=self._read_requires_schema_init(),
                )
            except sqlite3.IntegrityError:
                canonical_existing = self._find_canonical_existing_paper_entry(
                    conn,
                    prediction=prediction,
                    market=str(row["market_family"]),
                    selection=str(row["selection"]),
                    line=None if row.get("line") is None else float(row["line"]),
                )
                if canonical_existing is not None:
                    row["paper_trade_id"] = canonical_existing["paper_trade_id"]
                    continue
                existing = football_paper_trading.find_existing_paper_trade_entry(
                    conn,
                    prediction_id=str(prediction["prediction_id"]),
                    fixture_id=str(prediction["fixture_id"]),
                    market=str(row["market_family"]),
                    selection=str(row["selection"]),
                    line=None if row.get("line") is None else float(row["line"]),
                    initialize_schema=self._read_requires_schema_init(),
                )
                if existing is None:
                    raise
                row["paper_trade_id"] = existing["paper_trade_id"]
                continue
            row["paper_trade_id"] = entry.paper_trade_id
            created += 1
        if not defer_portfolio_sync:
            self._sync_portfolio_selection_for_prediction(conn, prediction=prediction, evaluation=evaluation)
        return created

    def _settlement_fixture_provider(self, fixture_id: str) -> FixtureProvider | None:
        provider_prefix = fixture_id.split(":", 1)[0]
        if provider_prefix == self.fixture_provider.provider_name():
            return self.fixture_provider
        if provider_prefix == API_FOOTBALL_PROVIDER:
            provider = ApiFootballFixtureProvider()
            return provider if provider.configured() else None
        if provider_prefix == "football-data.org":
            provider = FootballDataOrgFixtureProvider()
            return provider if provider.configured() else None
        return None

    def _load_settlement_fixture(self, fixture_id: str) -> dict[str, Any] | None:
        with storage.connect(self.config.dashboard_db_path) as conn:
            fixture = storage.fetch_fixture(conn, fixture_id, initialize_schema=self._read_requires_schema_init())
            result_row = storage.fetch_fixture_result(
                conn,
                fixture_id,
                initialize_schema=self._read_requires_schema_init(),
            )
        if fixture is not None and result_row is not None:
            self._apply_corner_result_to_fixture(fixture, result_row)
        return fixture

    def _refresh_settlement_fixture(self, fixture_id: str) -> dict[str, Any] | None:
        provider = self._settlement_fixture_provider(fixture_id)
        if provider is None:
            return None
        try:
            refreshed = provider.get_fixture(fixture_id)
        except FixtureProviderError:
            return None
        if refreshed is None:
            return None
        updated_at = utc_now_iso()
        with storage.connect(self.config.dashboard_db_path) as conn:
            storage.upsert_fixtures(
                conn,
                [refreshed],
                updated_at=updated_at,
                initialize_schema=self._read_requires_schema_init(),
            )
            fixture = storage.fetch_fixture(conn, fixture_id, initialize_schema=self._read_requires_schema_init())
            result_row = storage.fetch_fixture_result(
                conn,
                fixture_id,
                initialize_schema=self._read_requires_schema_init(),
            )
        if fixture is not None and result_row is not None:
            self._apply_corner_result_to_fixture(fixture, result_row)
        return fixture

    def _ensure_corner_result_for_settlement(self, fixture: dict[str, Any]) -> dict[str, Any]:
        if str(fixture.get("status") or "") != FIXTURE_STATUS_FINISHED:
            return fixture
        if fixture.get("home_corners") is not None and fixture.get("away_corners") is not None:
            return fixture
        if not self.statistics_provider.configured():
            return fixture
        try:
            result = self.statistics_provider.get_authoritative_corner_statistics(fixture)
        except FixtureProviderError:
            return fixture
        with storage.connect(self.config.dashboard_db_path) as conn:
            storage.upsert_fixture_result(
                conn,
                fixture_id=str(fixture["fixture_id"]),
                statistics_provider=result.provider,
                statistics_provider_fixture_id=result.provider_fixture_id,
                home_corners=result.home_corners,
                away_corners=result.away_corners,
                total_corners=result.total_corners,
                source_timestamp=result.source_timestamp,
                captured_at=result.captured_at,
                initialize_schema=self._read_requires_schema_init(),
            )
            stored_result = storage.fetch_fixture_result(
                conn,
                str(fixture["fixture_id"]),
                initialize_schema=self._read_requires_schema_init(),
            )
        if stored_result is not None:
            self._apply_corner_result_to_fixture(fixture, stored_result)
        return fixture

    def _resolve_fixture_for_paper_settlement(
        self,
        *,
        row: dict[str, Any],
        fixture_rows: dict[str, dict[str, Any]],
    ) -> dict[str, Any] | None:
        fixture_id = str(row["fixture_id"])
        fixture = fixture_rows.get(fixture_id)
        if fixture is None:
            fixture = self._load_settlement_fixture(fixture_id)
        kickoff = row.get("kickoff")
        if (
            fixture is not None
            and kickoff not in {None, ""}
            and parse_iso_datetime(str(kickoff)) <= datetime.now(timezone.utc)
            and str(fixture.get("status") or "") != FIXTURE_STATUS_FINISHED
        ):
            refreshed = self._refresh_settlement_fixture(fixture_id)
            if refreshed is not None:
                fixture = refreshed
        if fixture is None:
            fixture = self._refresh_settlement_fixture(fixture_id)
        if fixture is None:
            return None
        return self._ensure_corner_result_for_settlement(fixture)

    def _process_paper_trading_updates(self, rows_with_predictions: list[dict[str, Any]]) -> None:
        fixture_rows = {str(row["fixture_id"]): row for row in rows_with_predictions}
        now_utc = datetime.now(timezone.utc)
        with football_paper_trading.connect(self.config.paper_trading_db_path) as conn:
            snapshot_rows_by_fixture: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for snapshot_row in football_paper_trading.fetch_market_snapshots(
                conn,
                initialize_schema=self._read_requires_schema_init(),
            ):
                snapshot_rows_by_fixture[str(snapshot_row["fixture_id"])].append(snapshot_row)
            # Settle corners-dependent rows first so mixed-market update ordering stays deterministic
            # when one market can still remain unresolved after a finished fixture.
            paper_rows = sorted(
                football_paper_trading.fetch_paper_trade_rows(
                    conn,
                    initialize_schema=self._read_requires_schema_init(),
                ),
                key=lambda row: (
                    0 if str(row.get("market") or "") == "corner_total" else 1,
                    str(row.get("decision_timestamp") or ""),
                    str(row.get("paper_trade_id") or ""),
                ),
            )
            for row in paper_rows:
                fixture = self._resolve_fixture_for_paper_settlement(row=row, fixture_rows=fixture_rows)
                if fixture is None:
                    continue
                result_context = {
                    "fixture_id": str(fixture["fixture_id"]),
                    "provider": str(fixture.get("provider") or ""),
                    "provider_fixture_id": fixture.get("provider_fixture_id"),
                    "statistics_provider": fixture.get("statistics_provider"),
                    "statistics_provider_fixture_id": fixture.get("statistics_provider_fixture_id"),
                    "final_status": str(fixture.get("status") or ""),
                    "result_captured_at": fixture.get("last_provider_update"),
                    "corner_result_captured_at": fixture.get("corner_result_captured_at"),
                    "corner_result_source_timestamp": fixture.get("corner_result_source_timestamp"),
                }
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
                            initialize_schema=self._read_requires_schema_init(),
                        )
                if row.get("trade_state") == "PENDING" and now_utc >= kickoff and str(fixture["status"]) in FIXTURE_LIVE_STATUSES:
                    football_paper_trading.append_trade_update(
                        conn,
                        football_paper_trading.build_trade_update_payload(
                            paper_trade_id=str(row["paper_trade_id"]),
                            trade_state="OPEN",
                            settlement_context={
                                **result_context,
                                "status": str(fixture["status"]),
                            },
                            odds=float(row.get("entry_odds") or row["available_market_price"]),
                            notes="Fixture started; position is now open.",
                        ),
                        initialize_schema=self._read_requires_schema_init(),
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
                    if str(row.get("trade_state") or "") == "UNSETTLED":
                        continue
                    football_paper_trading.append_trade_update(
                        conn,
                        football_paper_trading.build_trade_update_payload(
                            paper_trade_id=str(row["paper_trade_id"]),
                            trade_state="UNSETTLED",
                            settlement_context={
                                **result_context,
                                "status": str(fixture["status"]),
                            },
                            odds=float(row.get("entry_odds") or row["available_market_price"]),
                            notes="Settlement pending because the required final market result is unavailable.",
                        ),
                        initialize_schema=self._read_requires_schema_init(),
                    )
                    continue
                football_paper_trading.append_trade_update(
                    conn,
                    football_paper_trading.build_trade_update_payload(
                        paper_trade_id=str(row["paper_trade_id"]),
                        trade_state=state,
                        settlement_context={
                            **result_context,
                            "actual_home_goals": int(fixture.get("home_score") or 0),
                            "actual_away_goals": int(fixture.get("away_score") or 0),
                            "actual_home_corners": fixture.get("home_corners"),
                            "actual_away_corners": fixture.get("away_corners"),
                            "actual_total_corners": actual_total_corners,
                        },
                        odds=float(row.get("entry_odds") or row["available_market_price"]),
                    ),
                    initialize_schema=self._read_requires_schema_init(),
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

    def _log_market_sync_summary(self, *, mode: str) -> None:
        diagnostics = self.market_provider.diagnostics()
        quota = diagnostics.get("quota") or {}
        logger.info(
            "football market sync: mode=%s runtime_state=%s provider_state=%s provider_error_code=%s provider_requests=%s "
            "additional_event_requests=%s additional_cap=%s additional_truncated=%s quota_remaining=%s "
            "quota_credits_used=%s fixtures=%s matched=%s snapshots=%s candidates=%s paper_entries=%s",
            mode,
            self._market_runtime_state.get("state"),
            diagnostics.get("state"),
            diagnostics.get("provider_error_code"),
            diagnostics.get("request_count"),
            diagnostics.get("additional_event_requests"),
            diagnostics.get("additional_event_request_cap"),
            diagnostics.get("additional_markets_truncated"),
            quota.get("remaining"),
            quota.get("usage_credits_this_process"),
            self._market_runtime_state.get("fixtures_considered"),
            self._market_runtime_state.get("fixtures_matched"),
            self._market_runtime_state.get("market_snapshots"),
            self._market_runtime_state.get("candidate_count"),
            self._market_runtime_state.get("paper_entry_count"),
        )

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
            self._sync_current_market_state(rows_with_predictions, dashboard_conn=conn)
            self._log_market_sync_summary(mode="automatic" if self.config.enable_odds_sync else "disabled")
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
                self._sync_current_market_state(rows_with_predictions, dashboard_conn=conn)
                self._log_market_sync_summary(mode="automatic")
                return

            claimed_at = utc_now_iso()
            claimed = storage.claim_one_shot_market_sync_token(
                conn,
                token_hash=token_hash,
                token_id=token_id,
                claimed_at=claimed_at,
                initialize_schema=self._read_requires_schema_init(),
            )
            if not claimed:
                existing = storage.get_one_shot_market_sync_claim(
                    conn,
                    token_hash,
                    initialize_schema=self._read_requires_schema_init(),
                )
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
                self._sync_current_market_state(rows_with_predictions, force_once=True, dashboard_conn=conn)
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
                    initialize_schema=self._read_requires_schema_init(),
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
                initialize_schema=self._read_requires_schema_init(),
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
            self._log_market_sync_summary(mode="one_shot")
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

    def _persist_market_runtime_checkpoint(
        self,
        *,
        market_state: dict[str, Any],
        evaluations: dict[str, Any],
        snapshot_count: int,
        matched_count: int,
        candidate_count: int,
        passed_safeguards: int,
        unresolved_fixture_count: int,
        stale_market_count: int,
        markets_covered: set[str],
        freshness_values: set[str],
        fixtures_considered: int,
        paper_entry_count: int,
        settlement_queue_count: int,
        last_successful_snapshot_at: str | None,
        last_sync_at: str,
        dashboard_conn: Any | None = None,
    ) -> None:
        payload = {
            "state": (
                "READY"
                if evaluations and any((value.get("decision_rows") or []) for value in evaluations.values())
                else market_state["state"]
            ),
            "automatic_odds_sync": "ENABLED" if self.config.enable_odds_sync else "DISABLED",
            "last_sync_at": last_sync_at,
            "last_successful_snapshot_at": last_successful_snapshot_at,
            "last_error": market_state.get("last_error") or market_state.get("reason"),
            "fixtures_considered": fixtures_considered,
            "fixtures_matched": matched_count,
            "market_snapshots": snapshot_count,
            "markets_covered": sorted(markets_covered),
            "freshness": "FRESH"
            if freshness_values == {"FRESH"}
            else ("STALE" if freshness_values == {"STALE"} else "UNKNOWN"),
            "unresolved_fixture_count": unresolved_fixture_count,
            "stale_market_count": stale_market_count,
            "candidate_count": candidate_count,
            "paper_entry_count": paper_entry_count,
            "settlement_queue_count": settlement_queue_count,
            "decision_count": sum(len(value.get("decision_rows") or []) for value in evaluations.values()),
            "passed_safeguards_count": passed_safeguards,
            "evaluations": evaluations,
        }
        self._market_runtime_state.update(payload)
        if dashboard_conn is not None:
            storage.put_runtime_state(
                dashboard_conn,
                state_key="market_runtime_state",
                state_value=self._market_runtime_state,
                updated_at=last_sync_at,
                initialize_schema=self._read_requires_schema_init(),
            )

    def _sync_current_market_state(
        self,
        rows_with_predictions: list[dict[str, Any]],
        *,
        force_once: bool = False,
        dashboard_conn: Any | None = None,
    ) -> None:
        market_state = self._market_provider_state()
        now_utc = datetime.now(timezone.utc)
        evaluations: dict[str, Any] = {}
        last_sync_at = utc_now_iso()
        paper_entry_count = 0
        settlement_queue_count = 0
        paid_market_provider = self.market_provider.provider_name() == "api-football"
        if paid_market_provider and not self.config.allow_paid_provider_calls:
            self._persist_market_runtime_checkpoint(
                market_state={
                    **market_state,
                    "state": PAID_PROVIDER_DISABLED_STATE,
                    "reason": f"Set {DEFAULT_ALLOW_PAID_PROVIDER_CALLS_ENV_VAR}=1 to enable paid market polling.",
                },
                evaluations={},
                snapshot_count=0,
                matched_count=0,
                candidate_count=0,
                passed_safeguards=0,
                unresolved_fixture_count=0,
                stale_market_count=0,
                markets_covered=set(),
                freshness_values=set(),
                fixtures_considered=0,
                paper_entry_count=0,
                settlement_queue_count=0,
                last_successful_snapshot_at=None,
                last_sync_at=last_sync_at,
                dashboard_conn=dashboard_conn,
            )
            return

        if paid_market_provider and self._provider_budget.quota_state() == QUOTA_EXHAUSTED:
            self._persist_market_runtime_checkpoint(
                market_state={
                    **market_state,
                    "state": "QUOTA_EXHAUSTED",
                    "reason": "API-Football daily request allowance exhausted; market polling paused until reset.",
                },
                evaluations={},
                snapshot_count=0,
                matched_count=0,
                candidate_count=0,
                passed_safeguards=0,
                unresolved_fixture_count=0,
                stale_market_count=0,
                markets_covered=set(),
                freshness_values=set(),
                fixtures_considered=0,
                paper_entry_count=0,
                settlement_queue_count=0,
                last_successful_snapshot_at=None,
                last_sync_at=last_sync_at,
                dashboard_conn=dashboard_conn,
            )
            return

        if not market_state["configured"]:
            self._persist_market_runtime_checkpoint(
                market_state=market_state,
                evaluations={},
                snapshot_count=0,
                matched_count=0,
                candidate_count=0,
                passed_safeguards=0,
                unresolved_fixture_count=0,
                stale_market_count=0,
                markets_covered=set(),
                freshness_values=set(),
                fixtures_considered=0,
                paper_entry_count=0,
                settlement_queue_count=0,
                last_successful_snapshot_at=None,
                last_sync_at=last_sync_at,
                dashboard_conn=dashboard_conn,
            )
            return

        if not self.config.current_odds_connected and not force_once:
            self._persist_market_runtime_checkpoint(
                market_state={
                    **market_state,
                    "state": "SYNC_DISABLED",
                    "reason": "Set SPORTS_AI_CURRENT_ODDS_CONNECTED=1 to enable the approved live odds path.",
                },
                evaluations={},
                snapshot_count=0,
                matched_count=0,
                candidate_count=0,
                passed_safeguards=0,
                unresolved_fixture_count=0,
                stale_market_count=0,
                markets_covered=set(),
                freshness_values=set(),
                fixtures_considered=0,
                paper_entry_count=0,
                settlement_queue_count=0,
                last_successful_snapshot_at=None,
                last_sync_at=last_sync_at,
                dashboard_conn=dashboard_conn,
            )
            return

        if not self.config.enable_odds_sync and not force_once:
            self._persist_market_runtime_checkpoint(
                market_state={
                    **market_state,
                    "state": "SYNC_DISABLED",
                    "reason": "Set SPORTS_AI_ENABLE_ODDS_SYNC=1 to sync current market snapshots.",
                },
                evaluations={},
                snapshot_count=0,
                matched_count=0,
                candidate_count=0,
                passed_safeguards=0,
                unresolved_fixture_count=0,
                stale_market_count=0,
                markets_covered=set(),
                freshness_values=set(),
                fixtures_considered=0,
                paper_entry_count=0,
                settlement_queue_count=0,
                last_successful_snapshot_at=None,
                last_sync_at=last_sync_at,
                dashboard_conn=dashboard_conn,
            )
            return

        future_rows = [
            row
            for row in self._future_fixture_rows(rows_with_predictions)
            if self._prediction_eligibility_status(row.get("prediction")) == "PREDICTION_READY"
        ]
        begin_market_sync_window = getattr(self.market_provider, "begin_sync_window", None)
        if callable(begin_market_sync_window):
            begin_market_sync_window()
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
        prediction_by_fixture_id: dict[str, dict[str, Any]] = {}

        with football_paper_trading.connect(self.config.paper_trading_db_path) as conn:
            paper_rows = football_paper_trading.fetch_paper_trade_rows(
                conn,
                initialize_schema=self._read_requires_schema_init(),
            )
            paper_entry_count = len(paper_rows)
            settlement_queue_count = sum(
                1
                for row in paper_rows
                if str(row.get("trade_state")) in {"PENDING", "OPEN", "UNSETTLED"}
                and parse_iso_datetime(str(row["kickoff"])) <= now_utc
            )
            for row in future_rows:
                prediction = row.get("prediction")
                if prediction is None:
                    continue
                prediction_by_fixture_id[str(row["fixture_id"])] = prediction
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
                    last_sync_at = utc_now_iso()
                    self._persist_market_runtime_checkpoint(
                        market_state=market_state,
                        evaluations=evaluations,
                        snapshot_count=snapshot_count,
                        matched_count=matched_count,
                        candidate_count=candidate_count,
                        passed_safeguards=passed_safeguards,
                        unresolved_fixture_count=unresolved_fixture_count,
                        stale_market_count=stale_market_count,
                        markets_covered=markets_covered,
                        freshness_values=freshness_values,
                        fixtures_considered=len(future_rows),
                        paper_entry_count=paper_entry_count,
                        settlement_queue_count=settlement_queue_count,
                        last_successful_snapshot_at=last_successful_snapshot_at,
                        last_sync_at=last_sync_at,
                        dashboard_conn=dashboard_conn,
                    )
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
                    last_sync_at = utc_now_iso()
                    self._persist_market_runtime_checkpoint(
                        market_state=market_state,
                        evaluations=evaluations,
                        snapshot_count=snapshot_count,
                        matched_count=matched_count,
                        candidate_count=candidate_count,
                        passed_safeguards=passed_safeguards,
                        unresolved_fixture_count=unresolved_fixture_count,
                        stale_market_count=stale_market_count,
                        markets_covered=markets_covered,
                        freshness_values=freshness_values,
                        fixtures_considered=len(future_rows),
                        paper_entry_count=paper_entry_count,
                        settlement_queue_count=settlement_queue_count,
                        last_successful_snapshot_at=last_successful_snapshot_at,
                        last_sync_at=last_sync_at,
                        dashboard_conn=dashboard_conn,
                    )
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
                        initialize_schema=self._read_requires_schema_init(),
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
                created = self._maybe_create_paper_entries(
                    conn,
                    prediction=prediction,
                    evaluation=evaluation,
                    defer_portfolio_sync=True,
                )
                paper_entries_created += created
                paper_entry_count += created
                candidate_count += int(evaluation["counts"]["preliminary_candidates"])
                passed_safeguards += int(evaluation["counts"]["passed_safeguards"])
                stale_market_count += sum(
                    1
                    for decision_row in evaluation.get("decision_rows") or []
                    if decision_row.get("decision") == "STALE_MARKET"
                )
                evaluations[str(row["fixture_id"])] = evaluation
                last_sync_at = utc_now_iso()
                self._persist_market_runtime_checkpoint(
                    market_state=market_state,
                    evaluations=evaluations,
                    snapshot_count=snapshot_count,
                    matched_count=matched_count,
                    candidate_count=candidate_count,
                    passed_safeguards=passed_safeguards,
                    unresolved_fixture_count=unresolved_fixture_count,
                    stale_market_count=stale_market_count,
                    markets_covered=markets_covered,
                    freshness_values=freshness_values,
                    fixtures_considered=len(future_rows),
                    paper_entry_count=paper_entry_count,
                    settlement_queue_count=settlement_queue_count,
                    last_successful_snapshot_at=last_successful_snapshot_at,
                    last_sync_at=last_sync_at,
                    dashboard_conn=dashboard_conn,
                )

            self._sync_current_portfolio_selections(
                conn,
                evaluations=evaluations,
                prediction_by_fixture_id=prediction_by_fixture_id,
            )
            self._sync_authoritative_corner_results(rows_with_predictions)
            self._process_paper_trading_updates(rows_with_predictions)
            paper_rows = football_paper_trading.fetch_paper_trade_rows(
                conn,
                initialize_schema=self._read_requires_schema_init(),
            )
            paper_entry_count = len(paper_rows)
            settlement_queue_count = sum(
                1
                for row in paper_rows
                if str(row.get("trade_state")) in {"PENDING", "OPEN", "UNSETTLED"}
                and parse_iso_datetime(str(row["kickoff"])) <= now_utc
            )

        market_state = self._market_provider_state()
        self._persist_market_runtime_checkpoint(
            market_state=market_state,
            evaluations=evaluations,
            snapshot_count=snapshot_count,
            matched_count=matched_count,
            candidate_count=candidate_count,
            passed_safeguards=passed_safeguards,
            unresolved_fixture_count=unresolved_fixture_count,
            stale_market_count=stale_market_count,
            markets_covered=markets_covered,
            freshness_values=freshness_values,
            fixtures_considered=len(future_rows),
            paper_entry_count=paper_entry_count,
            settlement_queue_count=settlement_queue_count,
            last_successful_snapshot_at=last_successful_snapshot_at,
            last_sync_at=utc_now_iso(),
            dashboard_conn=dashboard_conn,
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
            storage.save_prediction(
                conn,
                payload,
                updated_at=utc_now_iso(),
                initialize_schema=self._read_requires_schema_init(),
            )
            self._record_edge_desk_shadow(conn, fixture=fixture, prediction=payload)
            return payload

        version_key = self._prediction_version_key(fixture, self.snapshot)
        existing = storage.fetch_prediction_by_version_key(
            conn,
            version_key,
            initialize_schema=self._read_requires_schema_init(),
        )
        if existing is not None:
            self._record_edge_desk_shadow(conn, fixture=fixture, prediction=existing)
            return existing

        kickoff_utc = parse_iso_datetime(fixture.kickoff_utc)
        current_time_utc = datetime.now(timezone.utc)
        latest = storage.fetch_latest_prediction_for_fixture(
            conn,
            fixture.fixture_id,
            initialize_schema=self._read_requires_schema_init(),
        )
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
            storage.save_prediction(
                conn,
                payload,
                updated_at=utc_now_iso(),
                initialize_schema=self._read_requires_schema_init(),
            )
            self._record_edge_desk_shadow(conn, fixture=fixture, prediction=payload)
            return payload

        context = PredictionContext(fixture=fixture, snapshot=self.snapshot)
        prediction = build_prediction_for_fixture(context)
        home_resolution = (prediction.get("team_resolution") or {}).get("home")
        away_resolution = (prediction.get("team_resolution") or {}).get("away")
        for resolution in (home_resolution, away_resolution):
            if isinstance(resolution, dict):
                storage.record_team_resolution(
                    conn,
                    resolution,
                    updated_at=utc_now_iso(),
                    initialize_schema=self._read_requires_schema_init(),
                )
        storage.save_prediction(
            conn,
            prediction,
            updated_at=utc_now_iso(),
            initialize_schema=self._read_requires_schema_init(),
        )
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
                initialize_schema=self._read_requires_schema_init(),
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
            predictions = storage.fetch_all_predictions(conn, initialize_schema=self._read_requires_schema_init())
        prediction_map: dict[str, dict[str, Any]] = {}
        for prediction in predictions:
            prediction_map.setdefault(str(prediction["fixture_id"]), self._decorate_prediction_runtime(prediction))
        return prediction_map

    def _all_fixtures_with_predictions(self) -> list[dict[str, Any]]:
        with storage.connect(self.config.dashboard_db_path) as conn:
            fixture_rows = storage.fetch_all_fixtures(conn, initialize_schema=self._read_requires_schema_init())
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

    @staticmethod
    def _is_fresh_live_fixture(row: dict[str, Any], *, now_utc: datetime) -> bool:
        """Fail closed when a persisted live status has gone stale.

        Live fixture payloads are normalized with last_provider_update set to the
        ingestion time. If provider synchronization stops, an old LIVE/HALFTIME
        row must not remain visible forever. Six hours leaves ample room for
        unusually long matches while preventing multi-day stale live states.
        """
        stale_after = timedelta(hours=6)
        future_tolerance = timedelta(hours=2)
        kickoff = parse_iso_datetime(str(row["kickoff_utc"]))
        if kickoff > now_utc + future_tolerance:
            return False
        raw_update = str(row.get("last_provider_update") or "").strip()
        if raw_update:
            try:
                last_update = parse_iso_datetime(raw_update)
            except ValueError:
                last_update = None
            if last_update is not None:
                return now_utc - stale_after <= last_update <= now_utc + future_tolerance
        return now_utc - stale_after <= kickoff <= now_utc + future_tolerance

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
            if (
                bucket == "live"
                and status in FIXTURE_LIVE_STATUSES
                and self._is_fresh_live_fixture(row, now_utc=now_utc)
            ):
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

    def _strict_today_fixture_rows(
        self,
        fixture_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        today_local = self._current_local_date()
        return sorted(
            [
                row
                for row in fixture_rows
                if self._local_date_for_timestamp(row.get("kickoff_utc")) == today_local
            ],
            key=_fixture_sort_key,
        )

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
                if not path.exists():
                    return False
                with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as conn:
                    conn.execute("SELECT 1").fetchone()
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
        dashboard_db_reachable = self._database_reachable(self.config.dashboard_db_path, initialize_schema=False)
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
            "corner_result_sync": dict(self._storage_runtime_state.get("corner_result_sync") or self._empty_corner_result_sync_state()),
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
            "market_status_label": self._market_status_label(
                prediction=prediction,
                market_evaluation=market_evaluation,
                top_market_row=top_market_row,
            ),
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

    def _market_status_label(
        self,
        *,
        prediction: dict[str, Any] | None,
        market_evaluation: dict[str, Any] | None,
        top_market_row: dict[str, Any] | None,
    ) -> str:
        if top_market_row is not None:
            return str(top_market_row.get("current_decision_state") or _current_decision_state(top_market_row))
        if market_evaluation is not None:
            evaluation_state = str(market_evaluation.get("state") or "")
            if evaluation_state == "MARKET_NOT_AVAILABLE":
                return "MARKET_UNAVAILABLE"
            if evaluation_state == "MARKET_FIXTURE_UNRESOLVED":
                return "MARKET_UNMATCHED"
            return evaluation_state or "NOT_SYNCED"
        legacy_status = "" if prediction is None else str(prediction.get("manager_decision_status") or "")
        if legacy_status == "MARKET_DATA_NOT_CONNECTED" and self._market_provider_state().get("configured"):
            return "NOT_SYNCED"
        return legacy_status or "NOT_SYNCED"

    def _dashboard_agent_statuses(self) -> list[dict[str, str]]:
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
                "status": "REJECTED",
                "note": "Rejected and inactive. Retained only as historical diagnostic shadow output.",
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
                        "implied_probability": None
                        if row.get("best_available_price") in {None, 0}
                        else (1.0 / float(row["best_available_price"])),
                        "edge": None if row.get("settlement_aware_edge") is None else float(row["settlement_aware_edge"]),
                        "bookmaker": None if row.get("best_available_bookmaker") is None else str(row["best_available_bookmaker"]),
                        "market_timestamp": row.get("best_available_source_timestamp"),
                        "minimum_acceptable_odds": row.get("minimum_acceptable_price"),
                        "bet_state": bet_state,
                        "risk_status": _safeguard_label(row),
                        "risk_summary": risk_summary,
                        "market_freshness": None if row.get("market_freshness") is None else str(row["market_freshness"]),
                        "paper_trade_id": row.get("paper_trade_id"),
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

    def _decorate_best_pick_rows(
        self,
        best_picks: list[dict[str, Any]],
        paper_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        paper_row_map = {
            str(row["paper_trade_id"]): row
            for row in paper_rows
            if row.get("paper_trade_id")
        }
        decorated_rows: list[dict[str, Any]] = []
        for row in best_picks:
            paper_row = paper_row_map.get(str(row.get("paper_trade_id") or ""))
            settlement_context = {}
            if paper_row is not None:
                settlement_context = (paper_row.get("latest_update_payload") or {}).get("settlement_context") or {}
            final_score = None
            if settlement_context.get("actual_home_goals") is not None and settlement_context.get("actual_away_goals") is not None:
                final_score = f"{int(settlement_context['actual_home_goals'])}-{int(settlement_context['actual_away_goals'])}"
            final_corners = None
            if settlement_context.get("actual_home_corners") is not None and settlement_context.get("actual_away_corners") is not None:
                final_corners = (
                    f"{int(settlement_context['actual_home_corners'])}-{int(settlement_context['actual_away_corners'])}"
                )
            result_summary = final_score
            if str(row.get("market_family") or "") == "corner_total" and final_corners is not None and settlement_context.get("actual_total_corners") is not None:
                result_summary = f"{final_corners} (Total {int(settlement_context['actual_total_corners'])})"
            entry_model_probability = (
                row["model_probability"]
                if paper_row is None or paper_row.get("model_probability") is None
                else float(paper_row["model_probability"])
            )
            entry_fair_odds = (
                row["fair_odds"]
                if paper_row is None or paper_row.get("model_fair_price") is None
                else float(paper_row["model_fair_price"])
            )
            entry_odds = (
                row["market_odds"]
                if paper_row is None or paper_row.get("entry_odds") is None
                else float(paper_row["entry_odds"])
            )
            entry_edge = (
                row["edge"]
                if paper_row is None or paper_row.get("edge") is None
                else float(paper_row["edge"])
            )
            entry_bookmaker = (
                row["bookmaker"]
                if paper_row is None or paper_row.get("bookmaker") is None
                else str(paper_row["bookmaker"])
            )
            entry_timestamp = None if paper_row is None else (paper_row.get("entry_captured_at") or paper_row.get("decision_timestamp"))
            decorated_rows.append(
                {
                    **row,
                    "model_probability": entry_model_probability,
                    "fair_odds": entry_fair_odds,
                    "market_odds": entry_odds,
                    "edge": entry_edge,
                    "bookmaker": entry_bookmaker,
                    "market_timestamp": entry_timestamp or row.get("market_timestamp"),
                    "entry_model_probability": entry_model_probability,
                    "entry_fair_odds": entry_fair_odds,
                    "entry_odds": entry_odds,
                    "entry_edge": entry_edge,
                    "entry_bookmaker": entry_bookmaker,
                    "entry_timestamp": entry_timestamp or row.get("market_timestamp"),
                    "current_market_odds": row.get("market_odds"),
                    "current_market_edge": row.get("edge"),
                    "current_market_bookmaker": row.get("bookmaker"),
                    "current_market_timestamp": row.get("market_timestamp"),
                    "paper_trade_state": str((paper_row or {}).get("trade_state") or "PENDING"),
                    "paper_result_tone": self._paper_result_tone(str((paper_row or {}).get("trade_state") or "PENDING")),
                    "final_score": final_score,
                    "final_corners": final_corners,
                    "result_summary": result_summary,
                }
            )
        return decorated_rows

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

    def _build_working_rows_for_fixture_map(self, fixture_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        working_rows: list[dict[str, Any]] = []
        for evaluation in self._market_evaluation_map().values():
            for row in evaluation.get("decision_rows") or []:
                fixture = fixture_map.get(str(row["fixture_id"]))
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
                if decision_group != "bet" and status_label == "PENDING":
                    status_label = desk_label
                    status_tone = "final"
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
                        "paper_trade_id": row.get("paper_trade_id"),
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

    def _build_today_working_rows(self, fixture_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        today_fixture_map = {
            str(row["fixture_id"]): self.serialize_fixture_row(row)
            for row in self._filter_fixture_rows(bucket="today", fixture_rows=fixture_rows)
        }
        return self._build_working_rows_for_fixture_map(today_fixture_map)

    def _build_current_slate_working_rows(self, fixture_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        current_slate_fixture_map = {
            str(row["fixture_id"]): self.serialize_fixture_row(row)
            for row in self._future_fixture_rows(fixture_rows)
        }
        return self._build_working_rows_for_fixture_map(current_slate_fixture_map)

    def _canonical_display_id_for_fixture(
        self,
        row: dict[str, Any],
        canonical_id_by_fixture_id: dict[str, str],
    ) -> str:
        fixture_id = str(row.get("fixture_id") or "")
        mapped = canonical_id_by_fixture_id.get(fixture_id)
        if mapped:
            return mapped
        if row.get("competition_id") and row.get("home_team_name") and row.get("away_team_name") and row.get("kickoff_utc"):
            return football_canonical_fixture.canonical_fixture_id_from_identity(
                competition_id=str(row["competition_id"]),
                home_team_name=str(row["home_team_name"]),
                away_team_name=str(row["away_team_name"]),
                kickoff_utc=str(row["kickoff_utc"]),
                season=None if row.get("season") is None else str(row.get("season")),
            )
        return fixture_id

    def _canonical_display_ids_for_fixtures(self, fixture_rows: list[dict[str, Any]]) -> dict[str, str]:
        fixture_ids = [
            str(row["fixture_id"])
            for row in fixture_rows
            if row.get("fixture_id") not in {None, ""}
        ]
        if not fixture_ids:
            return {}
        try:
            with football_paper_trading.connect(self.config.paper_trading_db_path) as conn:
                return football_canonical_fixture.fetch_canonical_fixture_ids_for_fixture_ids(
                    conn,
                    fixture_ids,
                    initialize_schema=self._read_requires_schema_init(),
                )
        except sqlite3.OperationalError:
            return {}

    @staticmethod
    def _client_card_reason(row: dict[str, Any]) -> str:
        state = str(row.get("current_decision_state") or _current_decision_state(row))
        humanized = _humanize_row_reason_codes(row)
        if humanized:
            return humanized
        if state == "NO_BET_VALUE":
            return "Insufficient value"
        if state == "NO_BET_STALE_MARKET":
            return "Waiting for fresh odds"
        if state == "NO_BET_MISSING_PRICE":
            return "Missing reference price"
        if state == "NO_BET_SAFEGUARD":
            return "Safeguard failed"
        if state == "UNSUPPORTED":
            return "Market unavailable"
        if state == "BLOCKED":
            return "Review required"
        return _decision_state_detail(state)

    @staticmethod
    def _client_card_row_sort_key(row: dict[str, Any]) -> tuple[int, int, float, int, str, tuple[str, str, float | None]]:
        current_state = str(row.get("current_decision_state") or _current_decision_state(row))
        portfolio_selected = bool(row.get("portfolio_selected"))
        edge = row.get("settlement_aware_edge")
        edge_value = float(edge) if edge is not None else -1_000_000_000.0
        rank = row.get("portfolio_selection_rank")
        rank_value = int(rank) if rank is not None else 999_999
        return (
            0 if portfolio_selected else 1 if current_state == "BET" else 2,
            rank_value,
            -edge_value,
            CLIENT_MATCH_CARD_MARKET_ORDER.get(str(row.get("market_family") or ""), 999),
            str(row.get("best_available_source_timestamp") or row.get("market_as_of") or row.get("decision_timestamp") or ""),
            _market_identity(row),
        )

    def _client_match_card_market_row(
        self,
        *,
        fixture: dict[str, Any],
        market_family: str,
        market_label: str,
        source_row: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if source_row is None:
            return {
                "market_family": market_family,
                "market_label": market_label,
                "selection_label": "Market unavailable",
                "bet_label": "Market unavailable",
                "line": None,
                "odds": None,
                "odds_label": "",
                "action": "NO BET",
                "decision_state": "NO_BET_UNAVAILABLE",
                "decision_group": "no_bet",
                "reason": "Market unavailable",
                "is_bet": False,
                "portfolio_selected": False,
            }
        current_state = str(source_row.get("current_decision_state") or _current_decision_state(source_row))
        labels = _humanize_market_selection(
            market_family=market_family,
            selection=str(source_row.get("selection") or ""),
            line=None if source_row.get("line") is None else float(source_row["line"]),
            home_team_name=str(fixture["home_team_name"]),
            away_team_name=str(fixture["away_team_name"]),
        )
        odds = None if source_row.get("best_available_price") is None else float(source_row["best_available_price"])
        is_bet = current_state == "BET"
        return {
            "market_family": market_family,
            "market_label": market_label,
            "selection_label": labels["selection_label"],
            "bet_label": labels["bet_label"],
            "line": None if source_row.get("line") is None else float(source_row["line"]),
            "odds": odds,
            "odds_label": "" if odds is None else f"{odds:.2f}",
            "action": "BET" if is_bet else "NO BET",
            "decision_state": current_state,
            "decision_group": _decision_group(current_state),
            "reason": "" if is_bet else self._client_card_reason(source_row),
            "is_bet": is_bet,
            "portfolio_selected": bool(source_row.get("portfolio_selected")),
            "paper_trade_id": source_row.get("paper_trade_id"),
        }

    def _build_client_match_cards(
        self,
        fixture_rows: list[dict[str, Any]],
        *,
        portfolio_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        serialized_rows = [self.serialize_fixture_row(row) for row in sorted(fixture_rows, key=_fixture_sort_key)]
        canonical_id_by_fixture_id = self._canonical_display_ids_for_fixtures(serialized_rows)
        selected_by_canonical_id: dict[str, dict[str, Any]] = {}
        for row in serialized_rows:
            canonical_id = self._canonical_display_id_for_fixture(row, canonical_id_by_fixture_id)
            existing = selected_by_canonical_id.get(canonical_id)
            if existing is None or _fixture_sort_key(row) < _fixture_sort_key(existing):
                selected_by_canonical_id[canonical_id] = row

        cards: list[dict[str, Any]] = []
        portfolio_records = portfolio_context or self._portfolio_selection_records()
        for canonical_id, fixture in sorted(
            selected_by_canonical_id.items(),
            key=lambda item: _fixture_sort_key(item[1]),
        ):
            market_evaluation = fixture.get("market_evaluation") or {}
            annotated_rows = self._annotate_rows_with_portfolio(
                [
                    {
                        **row,
                        "current_decision_state": str(row.get("current_decision_state") or _current_decision_state(row)),
                    }
                    for row in (market_evaluation.get("decision_rows") or [])
                ],
                records=portfolio_records,
                qualified_predicate=lambda row: str(row.get("current_decision_state") or "") == "BET",
            )
            rows_by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in annotated_rows:
                market_family = str(row.get("market_family") or "")
                if market_family in CLIENT_MATCH_CARD_MARKET_ORDER:
                    rows_by_family[market_family].append(row)
            market_rows = []
            for market_family, market_label in CLIENT_MATCH_CARD_MARKETS:
                family_rows = sorted(rows_by_family.get(market_family, []), key=self._client_card_row_sort_key)
                market_rows.append(
                    self._client_match_card_market_row(
                        fixture=fixture,
                        market_family=market_family,
                        market_label=market_label,
                        source_row=None if not family_rows else family_rows[0],
                    )
                )
            valid_bets = sum(1 for row in market_rows if row["is_bet"])
            kickoff_local = parse_iso_datetime(str(fixture["kickoff_utc"])).astimezone(
                ZoneInfo(self.config.display_timezone)
            )
            cards.append(
                {
                    "fixture_id": str(fixture["fixture_id"]),
                    "canonical_fixture_id": canonical_id,
                    "home_team_name": str(fixture["home_team_name"]),
                    "away_team_name": str(fixture["away_team_name"]),
                    "competition_name": str(fixture["competition_name"]),
                    "kickoff_utc": str(fixture["kickoff_utc"]),
                    "kickoff_local_short": kickoff_local.strftime("%b %-d · %-I:%M %p PHT")
                    if os.name != "nt"
                    else kickoff_local.strftime("%b %#d · %#I:%M %p PHT"),
                    "market_rows": market_rows,
                    "valid_bets": valid_bets,
                    "valid_bets_label": "NO VALID BETS" if valid_bets == 0 else f"{valid_bets} VALID BETS",
                    "details_href": f"/matches/{fixture['fixture_id']}",
                    "has_mixed_decisions": any(row["is_bet"] for row in market_rows)
                    and any(not row["is_bet"] for row in market_rows),
                    "all_no_bet": all(not row["is_bet"] for row in market_rows),
                }
            )
        return cards

    def _build_betting_desk_rows_for_fixture_map(self, fixture_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        desk_rows: list[dict[str, Any]] = []
        for evaluation in self._market_evaluation_map().values():
            for row in evaluation.get("decision_rows") or []:
                fixture = fixture_map.get(str(row["fixture_id"]))
                if fixture is None:
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
                best_offer_odds = None if row.get("best_available_price") is None else float(row["best_available_price"])
                desk_rows.append(
                    {
                        "fixture_id": str(row["fixture_id"]),
                        "competition_id": str(fixture["competition_id"]),
                        "competition_name": str(fixture["competition_name"]),
                        "kickoff_utc": str(fixture["kickoff_utc"]),
                        "kickoff_local": str(fixture["kickoff_local"]),
                        "kickoff_time_local": parse_iso_datetime(str(fixture["kickoff_utc"]))
                        .astimezone(ZoneInfo(self.config.display_timezone))
                        .strftime("%H:%M"),
                        "home_team_name": str(fixture["home_team_name"]),
                        "away_team_name": str(fixture["away_team_name"]),
                        "market_family": str(row["market_family"]),
                        "market_label": labels["market_label"],
                        "selection": str(row["selection"]),
                        "selection_label": labels["selection_label"],
                        "bet_label": labels["bet_label"],
                        "line": None if row.get("line") is None else float(row["line"]),
                        "model_probability": float(row["model_probability"]),
                        "fair_odds": None
                        if row.get("model_fair_decimal_price") is None
                        else float(row["model_fair_decimal_price"]),
                        "model_fair_decimal_price": None
                        if row.get("model_fair_decimal_price") is None
                        else float(row["model_fair_decimal_price"]),
                        "reference_bookmaker": None if row.get("reference_bookmaker") is None else str(row["reference_bookmaker"]),
                        "reference_source": None if row.get("reference_source") is None else str(row["reference_source"]),
                        "reference_odds": None,
                        "minimum_acceptable_odds": None
                        if row.get("minimum_acceptable_price") is None
                        else float(row["minimum_acceptable_price"]),
                        "best_offer_bookmaker": None
                        if row.get("best_available_bookmaker") is None
                        else str(row["best_available_bookmaker"]),
                        "best_offer_odds": best_offer_odds,
                        "best_available_price": best_offer_odds,
                        "implied_probability": None if best_offer_odds is None or best_offer_odds <= 0 else (1.0 / best_offer_odds),
                        "edge": None
                        if row.get("settlement_aware_edge") is None
                        else float(row["settlement_aware_edge"]),
                        "settlement_aware_edge": None
                        if row.get("settlement_aware_edge") is None
                        else float(row["settlement_aware_edge"]),
                        "freshness": str(row.get("market_freshness") or row.get("freshness_state") or "UNKNOWN"),
                        "safeguard": _safeguard_label(row),
                        "decision": str(row.get("decision") or ""),
                        "desk_decision": current_state,
                        "current_decision_state": current_state,
                        "paper_entry_eligible": bool(row.get("paper_entry_eligible")),
                        "decision_group": decision_group,
                        "reason": _humanize_row_reason_codes(row) or _decision_state_detail(current_state),
                        "paper_trade_id": row.get("paper_trade_id"),
                        "paper_qualification_state": "QUALIFIED" if current_state == "BET" else "NOT_PUBLISHED",
                        "model_version": str((row.get("model_versions") or {}).get("current_snapshot_model_version") or ""),
                    }
                )
        return sorted(
            desk_rows,
            key=lambda row: (
                0 if row["decision_group"] == "bet" else 1 if row["decision_group"] == "no_bet" else 2,
                str(row["kickoff_utc"]),
                -float(row.get("edge") or 0.0),
                str(row["fixture_id"]),
                str(row["market_family"]),
            ),
        )

    def _performance_windows(self, paper_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        now_local = datetime.now(timezone.utc).astimezone(ZoneInfo(self.config.display_timezone)).date()
        windows = (
            ("7D", 7),
            ("30D", 30),
            ("90D", 90),
            ("All Time", None),
        )
        rows: list[dict[str, Any]] = []
        for label, days in windows:
            if days is None:
                scoped_rows = list(paper_rows)
            else:
                cutoff = now_local - timedelta(days=days - 1)
                scoped_rows = [
                    row
                    for row in paper_rows
                    if (
                        (local_date := self._local_date_for_timestamp(row.get("kickoff") or row.get("decision_timestamp"))) is not None
                        and local_date >= cutoff
                    )
                ]
            performance = self._paper_performance_for_rows(scoped_rows)
            rows.append(
                {
                    "window": label,
                    "predictions": None,
                    "suggested_bets": len(scoped_rows),
                    "wins": int(performance.get("wins") or 0),
                    "losses": int(performance.get("losses") or 0),
                    "strike_rate": performance.get("strike_rate"),
                    "average_odds": performance.get("average_odds"),
                    "roi": performance.get("roi"),
                    "net_units": performance.get("net_units"),
                    "clv": performance.get("average_clv"),
                    "calibration": "INSUFFICIENT SAMPLE" if int(performance.get("settled_positions") or 0) < 20 else "NOT YET AVAILABLE",
                    "brier": "INSUFFICIENT SAMPLE" if int(performance.get("settled_positions") or 0) < 20 else "NOT YET AVAILABLE",
                    "settled": int(performance.get("settled_positions") or 0),
                }
            )
        return rows

    def _current_streak(self, paper_rows: list[dict[str, Any]]) -> str:
        settled_rows = sorted(
            [row for row in paper_rows if str(row.get("trade_state") or "") in PAPER_FINAL_STATES],
            key=lambda row: (
                str(row.get("updated_at") or row.get("decision_timestamp") or ""),
                str(row["paper_trade_id"]),
            ),
            reverse=True,
        )
        if not settled_rows:
            return "NOT AVAILABLE"
        lead_state = str(settled_rows[0].get("trade_state") or "")
        if lead_state not in {"WON", "LOST", "HALF_WIN", "HALF_LOSS"}:
            return lead_state
        streak_type = "W" if lead_state in {"WON", "HALF_WIN"} else "L"
        streak_count = 0
        for row in settled_rows:
            state = str(row.get("trade_state") or "")
            if streak_type == "W" and state in {"WON", "HALF_WIN"}:
                streak_count += 1
                continue
            if streak_type == "L" and state in {"LOST", "HALF_LOSS"}:
                streak_count += 1
                continue
            break
        return f"{streak_type}{streak_count}"

    @staticmethod
    def _current_slate_summary(
        *,
        working_rows: list[dict[str, Any]],
        paper_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        paper_trade_ids = {
            str(row["paper_trade_id"])
            for row in working_rows
            if row["is_actionable"] and row.get("paper_trade_id")
        }
        current_slate_paper_rows = [
            row
            for row in paper_rows
            if str(row.get("paper_trade_id") or "") in paper_trade_ids
        ]
        return {
            "fixture_count": len({str(row["fixture_id"]) for row in working_rows}),
            "count": len(working_rows),
            "bet_count": sum(1 for row in working_rows if row["is_actionable"]),
            "no_bet_count": sum(1 for row in working_rows if not row["is_actionable"]),
            "paper_entry_count": len(current_slate_paper_rows),
            "pending_count": sum(
                1
                for row in current_slate_paper_rows
                if str(row.get("trade_state") or "") in PAPER_ACTIVE_STATES
            ),
            "empty_message": "No current market decision rows are available for the current Football slate.",
        }

    @staticmethod
    def _glance_metrics(
        *,
        analysed_count: int,
        approved_count: int,
        paper_bet_count: int,
        paper_performance: dict[str, Any],
    ) -> list[dict[str, str]]:
        return [
            {"label": "Analysed", "value": str(analysed_count), "note": "current slate fixtures"},
            {"label": "Approved", "value": str(approved_count), "note": "qualified"},
            {"label": "Suggested Bets", "value": str(paper_bet_count), "note": "all paper bets"},
            {"label": "Settled", "value": str(paper_performance.get("settled_positions") or 0), "note": "final results"},
            {"label": "Won", "value": str(paper_performance.get("wins") or 0), "note": "full wins"},
            {"label": "Lost", "value": str(paper_performance.get("losses") or 0), "note": "full losses"},
            {"label": "Push", "value": str(paper_performance.get("pushes") or 0), "note": "stake returned"},
            {"label": "Pending", "value": str(paper_performance.get("pending_positions") or 0), "note": "open paper"},
            {
                "label": "Strike Rate",
                "value": "NOT AVAILABLE"
                if paper_performance.get("strike_rate") is None
                else f"{float(paper_performance['strike_rate']):.2%}",
                "note": "pushes excluded",
            },
            {
                "label": "P/L",
                "value": "NOT AVAILABLE"
                if paper_performance.get("net_units") is None
                else f"{float(paper_performance['net_units']):+.2f}",
                "note": "net units",
            },
            {
                "label": "ROI",
                "value": "NOT AVAILABLE"
                if paper_performance.get("roi") is None
                else f"{float(paper_performance['roi']):+.2%}",
                "note": "settled stake only",
            },
        ]

    @staticmethod
    def _desk_funnel(*, working_rows: list[dict[str, Any]], paper_entry_count: int) -> list[dict[str, str]]:
        bet_count = sum(1 for row in working_rows if row["is_actionable"])
        no_bet_count = sum(1 for row in working_rows if not row["is_actionable"])
        return [
            {"label": "Desk rows", "value": str(len(working_rows)), "note": "current-slate working surface"},
            {"label": "Suggested bets", "value": str(paper_entry_count), "note": "paper entries created"},
            {"label": "BET rows", "value": str(bet_count), "note": "actionable now"},
            {"label": "NO BET rows", "value": str(no_bet_count), "note": "tracked, not actionable"},
        ]

    def get_dashboard_payload(
        self,
        *,
        picks_page: int = 1,
        matches_page: int = 1,
        desk_page: int = 1,
    ) -> dict[str, Any]:
        fixture_rows = self._all_fixtures_with_predictions()
        live_rows = [self.serialize_fixture_row(row) for row in self._filter_fixture_rows(bucket="live", fixture_rows=fixture_rows)]
        today_rows = [
            self.serialize_fixture_row(row)
            for row in self._filter_fixture_rows(bucket="today", fixture_rows=fixture_rows)
            if str(row["status"]) not in FIXTURE_TERMINAL_STATUSES
        ]
        strict_today_rows = [self.serialize_fixture_row(row) for row in self._strict_today_fixture_rows(fixture_rows)]
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
        paper_rows, paper_status = self._paper_positions()
        portfolio_context = self._portfolio_selection_context(paper_rows)
        published_paper_rows = self._published_pick_rows(paper_rows, selection_context=portfolio_context)
        today_published_rows = self._paper_rows_for_local_date(published_paper_rows)
        today_paper_rows = self._paper_rows_for_local_date(paper_rows)
        current_best_picks = self._annotate_rows_with_portfolio(
            self._decorate_best_pick_rows(self._build_best_pick_rows(fixture_rows), paper_rows),
            records=portfolio_context,
            qualified_predicate=lambda row: True,
        )
        today_pending_portfolio_rows = [
            row
            for row in current_best_picks
            if self._local_date_for_timestamp(row.get("kickoff_utc")) == self._current_local_date()
            and str(row.get("portfolio_selection_state") or "") == PORTFOLIO_STATE_PENDING
        ]
        if portfolio_context.get("policy") is not None:
            best_picks = list(today_published_rows)
        else:
            best_picks = current_best_picks
        current_slate_rows = self._enforce_portfolio_recommendation_truth(
            self._annotate_rows_with_portfolio(
                self._build_current_slate_working_rows(fixture_rows),
                records=portfolio_context,
                qualified_predicate=lambda row: str(row.get("current_decision_state") or "") == "BET",
            ),
            records=portfolio_context,
        )
        today_working_rows = self._enforce_portfolio_recommendation_truth(
            self._annotate_rows_with_portfolio(
                self._build_today_working_rows(fixture_rows),
                records=portfolio_context,
                qualified_predicate=lambda row: str(row.get("current_decision_state") or "") == "BET",
            ),
            records=portfolio_context,
        )
        client_match_cards = self._build_client_match_cards(
            self._future_fixture_rows(fixture_rows),
            portfolio_context=portfolio_context,
        )
        other_decision_rows = [row for row in current_slate_rows if not row["is_actionable"]]
        performance_summary_payload = self._load_json_file(self._performance_summary_path())
        current_slate_summary = self._current_slate_summary(working_rows=current_slate_rows, paper_rows=paper_rows)
        system_status = self._build_system_status(
            paper_status=paper_status,
            storage_state=self._storage_status(),
            summary_payload=performance_summary_payload,
        )
        paper_performance = paper_status.get("performance", {})
        analysed_count = len({str(row["fixture_id"]) for row in today_working_rows})
        approved_count = sum(1 for row in today_working_rows if str(row.get("actual_recommendation_state") or row.get("current_decision_state") or "") == "BET")
        no_bet_count = sum(1 for row in today_working_rows if str(row.get("current_decision_state") or "") == "NO BET")
        blocking_reasons = self._blocking_reason_counts(today_working_rows)
        client_blocking_reasons = self._client_blocking_reasons(blocking_reasons)
        best_picks_pagination = _paginate_collection(best_picks, page=picks_page, page_size=8)
        today_matches_pagination = _paginate_collection(strict_today_rows, page=matches_page, page_size=8)
        today_desk_pagination = _paginate_collection(today_working_rows, page=desk_page, page_size=10)
        return {
            "live": live_rows,
            "today": today_rows,
            "today_matches": strict_today_rows,
            "visible_today_matches": today_matches_pagination["items"],
            "today_matches_pagination": today_matches_pagination,
            "upcoming": upcoming_rows[:24],
            "upcoming_count": len(upcoming_rows),
            "next_matches": next_rows,
            "prediction_watchlist": prediction_watchlist,
            "best_picks": best_picks,
            "visible_best_picks": best_picks_pagination["items"],
            "best_picks_pagination": best_picks_pagination,
            "best_picks_summary": {
                "count": len(best_picks),
                "empty_message": (
                    "Portfolio selection pending for current qualified opportunities."
                    if portfolio_context.get("policy") is not None and today_pending_portfolio_rows
                    else "No current portfolio-selected bets are available."
                    if portfolio_context.get("policy") is not None
                    else "No genuine published paper picks are currently attached to today's board."
                ),
                "source": (
                    "Current deterministic portfolio selection. A separate frozen publication lifecycle is not active yet."
                    if portfolio_context.get("policy") is not None
                    else "Immutable published paper picks only."
                ),
            },
            "published_paper_rows": published_paper_rows,
            "today_published_rows": today_published_rows,
            "current_slate_rows": current_slate_rows,
            "client_match_cards": client_match_cards,
            "today_desk_rows": today_working_rows,
            "visible_today_desk_rows": today_desk_pagination["items"],
            "today_desk_pagination": today_desk_pagination,
            "other_decision_rows": other_decision_rows,
            "current_slate_summary": current_slate_summary,
            "today_summary": {
                "analysed": analysed_count,
                "predictions": len(today_working_rows),
                "approved": approved_count,
                "suggested_bets": len(today_published_rows),
                "today_suggested_bets": len(today_published_rows),
                "matches": portfolio_context["selected_fixture_count"],
                "open_portfolio_exposure": portfolio_context["open_exposure_units"],
                "empty_message": "No current Route B desk rows are available for today's in-scope fixtures.",
            },
            "other_decision_summary": {
                "count": len(other_decision_rows),
                "blocked_count": sum(1 for row in other_decision_rows if row["current_decision_group"] == "blocked"),
                "no_bet_count": sum(1 for row in other_decision_rows if row["current_decision_group"] == "no_bet"),
                "unsupported_count": sum(1 for row in other_decision_rows if row["current_decision_group"] == "unsupported"),
                "empty_message": "No additional Football desk decisions are currently on the board.",
            },
            "glance_metrics": self._today_kpi_strip(
                analysed_count=analysed_count,
                predictions_count=len(today_working_rows),
                approved_count=approved_count,
                no_bet_count=no_bet_count,
                paper_rows=today_paper_rows,
                portfolio_rows=today_published_rows,
            ),
            "today_scopes": {
                "desk": {
                    "label": "Current Desk",
                    "note": "Today's live market analysis (fixture, prediction, odds, value).",
                },
                "paper": {
                    "label": "Paper Tracking",
                    "note": "Persisted tracked positions and performance, not live recommendations.",
                },
            },
            "desk_funnel": self._today_funnel(
                working_rows=today_working_rows,
                analysed_count=analysed_count,
                blocking_reasons=blocking_reasons,
            ),
            "blocking_reasons": client_blocking_reasons,
            "last_20": self._last_20_payload(published_paper_rows),
            "cumulative_profit": self._cumulative_profit_payload(paper_rows),
            "live_paper_sample": self._live_paper_sample_callout(paper_rows),
            "signals": self._signal_payload(system_status=system_status, paper_rows=paper_rows),
            "system_health": self._system_health_strip(
                system_status=system_status,
                paper_rows=paper_rows,
                performance_generated_at=None if performance_summary_payload is None else performance_summary_payload.get("generated_at"),
            ),
            "member_card_rows": [
                {
                    "fixture_id": str(row["fixture_id"]),
                    "match": f"{row['home_team_name']} vs {row['away_team_name']}",
                    "pick": row["bet_label"],
                    "model_probability": row.get("model_probability"),
                    "entry_odds": row.get("entry_odds"),
                    "confidence_label": row.get("confidence_label"),
                    "status": row.get("trade_state"),
                }
                for row in best_picks[:8]
            ],
            "agent_statuses": self._dashboard_agent_statuses(),
            "paper_status": paper_status,
            "paper_performance": paper_performance,
            "system_status": system_status,
        }

    def get_matches_payload(
        self,
        *,
        competition: str | None = None,
        team: str | None = None,
        prediction_status: str | None = None,
        status_bucket: str | None = None,
        page: int = 1,
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
        serializable_rows = [self.serialize_fixture_row(row) for row in sorted(rows, key=_fixture_sort_key)]
        client_match_cards = (
            self._build_client_match_cards(rows)
            if status_bucket in {"today", "upcoming", "next"}
            else []
        )
        pagination = _paginate_collection(serializable_rows, page=page, page_size=20)
        card_pagination = _paginate_collection(client_match_cards, page=page, page_size=12)
        return {
            "filters": {
                "competition": competition,
                "team": team,
                "prediction_status": prediction_status,
                "status_bucket": status_bucket,
            },
            "rows": serializable_rows,
            "visible_rows": pagination["items"],
            "client_match_cards": client_match_cards,
            "visible_client_match_cards": card_pagination["items"],
            "pagination": pagination,
            "client_match_cards_pagination": card_pagination,
            "show_client_match_cards": status_bucket in {"today", "upcoming", "next"},
            "system_status": self.get_system_status(),
        }

    def get_fixture_detail_payload(self, fixture_id: str) -> dict[str, Any] | None:
        with storage.connect(self.config.dashboard_db_path) as conn:
            fixture = storage.fetch_fixture(conn, fixture_id, initialize_schema=self._read_requires_schema_init())
            prediction = storage.fetch_latest_prediction_for_fixture(
                conn,
                fixture_id,
                initialize_schema=self._read_requires_schema_init(),
            )
        if fixture is None:
            return None
        serialized_fixture = self.serialize_fixture_row({**fixture, "prediction": prediction})
        market_evaluation = serialized_fixture.get("market_evaluation")
        paper_positions, paper_status = self._paper_positions()
        portfolio_context = self._portfolio_selection_context(paper_positions)
        system_status = self._build_system_status(
            paper_status=paper_status,
            storage_state=self._storage_status(),
        )
        published_entries = self._published_paper_entries_for_fixture(
            fixture_id=str(fixture_id),
            market_evaluation=market_evaluation,
            paper_rows=paper_positions,
            selection_context=portfolio_context,
        )
        decision_rows: list[dict[str, Any]] = []
        for row in (market_evaluation or {}).get("decision_rows") or []:
            current_state = str(row.get("current_decision_state") or _current_decision_state(row))
            labels = _humanize_market_selection(
                market_family=str(row["market_family"]),
                selection=str(row["selection"]),
                line=None if row.get("line") is None else float(row["line"]),
                home_team_name=str(serialized_fixture["home_team_name"]),
                away_team_name=str(serialized_fixture["away_team_name"]),
            )
            decision_rows.append(
                {
                    **row,
                    "current_decision_state": current_state,
                    "market_label": labels["market_label"],
                    "selection_label": labels["selection_label"],
                    "bet_label": labels["bet_label"],
                    "decision_group": _decision_group(current_state),
                    "safeguard_label": _safeguard_label(row),
                    "reason_label": _humanize_row_reason_codes(row) or _decision_state_detail(current_state),
                }
            )
        decision_rows = self._annotate_rows_with_portfolio(
            decision_rows,
            records=portfolio_context,
            qualified_predicate=lambda row: str(row.get("current_decision_state") or "") == "BET",
        )
        return {
            "fixture": serialized_fixture,
            "prediction": self._decorate_prediction_runtime(prediction),
            "market_evaluation": market_evaluation,
            "paper_entries": published_entries,
            "published_entry_count": len(published_entries),
            "headline_paper_entry": None if not published_entries else published_entries[0],
            "decision_rows": decision_rows,
            "decision_gates": self._decision_gates_payload(
                market_evaluation=market_evaluation,
                prediction=prediction,
                system_status=system_status,
            ),
            "model_card": {
                "label": "Route B",
                "version": str(
                    ((prediction or {}).get("model_versions") or {}).get("current_snapshot_model_version")
                    or (prediction or {}).get("prediction_version")
                    or "UNKNOWN"
                ),
                "route_a_state": "REJECTED",
                "agent_3_state": "NOT IMPLEMENTED",
            },
            "system_status": system_status,
        }

    def get_prediction_payload(self, fixture_id: str) -> dict[str, Any] | None:
        with storage.connect(self.config.dashboard_db_path) as conn:
            prediction = storage.fetch_latest_prediction_for_fixture(
                conn,
                fixture_id,
                initialize_schema=self._read_requires_schema_init(),
            )
        return self._decorate_prediction_runtime(prediction)

    def get_predictions_payload(self, *, page: int = 1) -> dict[str, Any]:
        now_utc = datetime.now(timezone.utc)
        with storage.connect(self.config.dashboard_db_path) as conn:
            predictions = storage.fetch_all_predictions(conn, initialize_schema=self._read_requires_schema_init())
            fixture_rows = {
                row["fixture_id"]: row
                for row in storage.fetch_all_fixtures(conn, initialize_schema=self._read_requires_schema_init())
            }
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
        rows.sort(key=lambda row: _fixture_sort_key(row["fixture"]))
        pagination = _paginate_collection(rows, page=page, page_size=20)
        return {
            "rows": rows,
            "visible_rows": pagination["items"],
            "pagination": pagination,
            "system_status": self.get_system_status(),
        }

    def get_betting_desk_payload(self, *, page: int = 1) -> dict[str, Any]:
        fixture_rows = self._all_fixtures_with_predictions()
        market_state = self._market_provider_state()
        desk_fixture_rows = self._future_fixture_rows(fixture_rows)
        fixture_map = {
            str(row["fixture_id"]): self.serialize_fixture_row(row)
            for row in desk_fixture_rows
        }
        portfolio_records = self._portfolio_selection_records()
        desk_rows = self._enforce_portfolio_recommendation_truth(
            self._annotate_rows_with_portfolio(
                self._build_betting_desk_rows_for_fixture_map(fixture_map),
                records=portfolio_records,
                qualified_predicate=lambda row: str(row.get("desk_decision") or "") == "BET",
            ),
            records=portfolio_records,
        )
        live_market_focus = configured_live_market_focus()
        desk_rows = [
            row
            for row in desk_rows
            if str(row.get("market_family") or row.get("market") or "") in live_market_focus
        ]
        preliminary = [row for row in desk_rows if row.get("portfolio_selected") or (portfolio_records.get("policy") is None and row["desk_decision"] == "BET")]
        no_bets = [row for row in desk_rows if row["decision_group"] == "no_bet"]
        blocked = [row for row in desk_rows if row["decision_group"] in {"blocked", "unsupported"}]

        quota_state = self._provider_budget.quota_state()
        circuit_state = self._provider_budget.circuit_snapshot()["state"]
        waiting_for_reset = bool(
            (quota_state == "EXHAUSTED") or (circuit_state == "QUOTA_EXHAUSTED")
        )
        if not self.config.allow_paid_provider_calls:
            desk_reason = "Live market polling is currently disabled."
        elif waiting_for_reset:
            desk_reason = (
                "Live market data is waiting for the provider quota reset. "
                "Historical picks and performance remain available."
            )
        elif circuit_state in {"RATE_LIMITED", "PROVIDER_ERROR", "AUTH_ERROR"}:
            desk_reason = f"Live market data is temporarily paused (provider {circuit_state}). Historical picks and performance remain available."
        elif not market_state["configured"]:
            desk_reason = "No current odds provider is configured for live market snapshots."
        else:
            desk_reason = (
                self._market_runtime_state.get("last_error")
                or market_state["reason"]
                or "Current desk rows are staged as provider market data becomes available."
            )
        pagination = _paginate_collection(desk_rows, page=page, page_size=20)
        return {
            "connected": bool(
                market_state["configured"]
                and self.config.current_odds_connected
                and (self.config.enable_odds_sync or bool(self.config.odds_sync_once_token.strip()))
            ),
            "reason": desk_reason,
            "quota_waiting_reset": waiting_for_reset,
            "quota_state": quota_state,
            "rows": desk_rows,
            "visible_rows": pagination["items"],
            "pagination": pagination,
            "preliminary_candidates": preliminary,
            "blocked_candidates": blocked,
            "no_bet_candidates": no_bets,
            "supported_markets": list(live_market_focus),
            "research_markets": [
                market for market in CLIENT_PRIMARY_MARKETS if market not in live_market_focus
            ],
            "blocking_reasons": self._blocking_reason_counts(desk_rows),
            "summary": {
                "rows": len(desk_rows),
                "bets": len(preliminary),
                "no_bets": len(no_bets),
                "blocked": len(blocked),
            },
            "system_status": self.get_system_status(),
        }

    def get_candidates_payload(self) -> dict[str, Any]:
        return self.get_betting_desk_payload()

    def get_member_card_payload(self, *, page: int = 1) -> dict[str, Any]:
        paper_rows, paper_status = self._paper_positions()
        portfolio_context = self._portfolio_selection_context(paper_rows)
        system_status = self._build_system_status(
            paper_status=paper_status,
            storage_state=self._storage_status(),
        )
        published_rows = self._published_pick_rows(paper_rows, selection_context=portfolio_context)
        live_market_focus = configured_live_market_focus()
        visible_rows = [
            row
            for row in published_rows
            if str(row.get("market") or "") in live_market_focus
            and (
                str(row.get("trade_state") or "") in PAPER_ACTIVE_STATES
                or self._local_date_for_timestamp(row.get("kickoff_utc") or row.get("kickoff")) == self._current_local_date()
            )
        ]
        member_rows = [
            {
                "fixture_id": str(row["fixture_id"]),
                "match": f"{row['home_team_name']} vs {row['away_team_name']}",
                "pick": row["bet_label"],
                "model_probability": row.get("model_probability"),
                "entry_odds": row.get("entry_odds"),
                "confidence_label": row.get("confidence_label"),
                "status": row.get("trade_state"),
                "portfolio_state": row.get("portfolio_selection_state_label"),
                "portfolio_note": row.get("portfolio_selection_reason"),
            }
            for row in visible_rows
        ]
        pagination = _paginate_collection(member_rows, page=page, page_size=12)
        return {
            "rows": member_rows,
            "visible_rows": pagination["items"],
            "pagination": pagination,
            "publish_state": portfolio_context["publish_state"],
            "publish_note": portfolio_context["publish_note"],
            "portfolio_policy": None
            if portfolio_context.get("policy") is None
            else {
                "policy_id": portfolio_context["policy"].policy_id,
                "policy_version": portfolio_context["policy"].policy_version,
                "effective_from": portfolio_context["policy"].effective_from,
            },
            "system_status": system_status,
        }

    def get_performance_payload(self) -> dict[str, Any]:
        summary_payload = self._load_json_file(self._performance_summary_path())
        market_rows = self._load_csv_rows(self._performance_csv_path("football_performance_by_market.csv"))
        competition_rows = self._load_csv_rows(self._performance_csv_path("football_performance_by_competition.csv"))
        season_rows = self._load_csv_rows(self._performance_csv_path("football_performance_by_season.csv"))
        calibration_rows = self._load_csv_rows(self._performance_csv_path("football_calibration.csv"))
        paper_positions, paper_status = self._paper_positions()
        portfolio_context = self._portfolio_selection_context(paper_positions)
        status = self._performance_status(summary_payload, paper_status)
        system_status = self._build_system_status(
            paper_status=paper_status,
            storage_state=self._storage_status(),
            summary_payload=summary_payload,
        )
        current_paper = paper_status.get("performance", {})
        paper_position_rows = self._annotate_rows_with_portfolio(
            self._paper_position_display_rows(paper_positions),
            records=portfolio_context,
            qualified_predicate=lambda row: True,
        )
        open_paper_positions = [row for row in paper_position_rows if row["is_open"]]
        portfolio_position_rows = self._annotate_rows_with_portfolio(
            self._paper_position_display_rows(portfolio_context["selected_rows"]),
            records=portfolio_context,
            qualified_predicate=lambda row: True,
        )
        open_portfolio_positions = [row for row in portfolio_position_rows if row["is_open"]]
        recent_paper_bets = sorted(
            [row for row in paper_position_rows if row["is_settled"]],
            key=lambda row: (str(row.get("settled_at") or ""), str(row["paper_trade_id"])),
            reverse=True,
        )
        recent_form = [str(row["trade_state"]) for row in recent_paper_bets[:5]]

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
        ]
        return {
            "summary": summary,
            "summary_generated_at": None if summary_payload is None else summary_payload.get("generated_at"),
            "status_flags": {} if summary_payload is None else summary_payload.get("status_flags", {}),
            "kpis": kpis,
            "live_paper_kpis": self._live_paper_kpis(current_paper),
            "live_paper_sample": self._live_paper_sample_callout(paper_positions),
            "portfolio_kpis": self._live_paper_kpis(portfolio_context["performance"]),
            "market_rows": market_rows,
            "competition_rows": competition_rows,
            "season_rows": season_rows,
            "calibration_series": calibration_series,
            "paper_positions": paper_position_rows,
            "open_paper_positions": open_paper_positions,
            "portfolio_positions": portfolio_position_rows,
            "open_portfolio_positions": open_portfolio_positions,
            "recent_paper_bets": recent_paper_bets,
            "recent_form": recent_form,
            "current_paper_summary": current_paper,
            "current_portfolio_summary": portfolio_context["performance"],
            "current_paper_by_market": current_paper.get("by_market", {}),
            "current_portfolio_by_market": portfolio_context["performance"].get("by_market", {}),
            "live_paper_market_rows": self._live_paper_market_rows(current_paper),
            "live_portfolio_market_rows": self._live_paper_market_rows(portfolio_context["performance"]),
            "route_b_label": "Route B",
            "route_a_state": "REJECTED",
            "agent_3_state": "NOT IMPLEMENTED",
            "window_rows": self._performance_windows(paper_positions),
            "portfolio_window_rows": self._performance_windows(portfolio_context["selected_rows"]),
            "market_breakdown": self._paper_breakdown_rows(
                paper_rows=paper_positions,
                kind="market",
                allowed_keys=CLIENT_PRIMARY_MARKETS,
            ),
            "portfolio_market_breakdown": self._paper_breakdown_rows(
                paper_rows=portfolio_context["selected_rows"],
                kind="market",
                allowed_keys=CLIENT_PRIMARY_MARKETS,
            ),
            "league_breakdown": self._paper_breakdown_rows(
                paper_rows=paper_positions,
                kind="league",
                allowed_keys=CLIENT_PRIMARY_LEAGUES,
            ),
            "portfolio_policy": None
            if portfolio_context.get("policy") is None
            else {
                "policy_id": portfolio_context["policy"].policy_id,
                "policy_version": portfolio_context["policy"].policy_version,
                "effective_from": portfolio_context["policy"].effective_from,
            },
            "portfolio_publish_state": portfolio_context["publish_state"],
            "portfolio_publish_note": portfolio_context["publish_note"],
            "current_streak": self._current_streak(paper_positions),
            "clv_status_label": "NOT YET AVAILABLE"
            if current_paper.get("average_clv") is None
            else f"{float(current_paper['average_clv']):+.2%}",
            "calibration_status_label": "NOT YET AVAILABLE",
            "brier_status_label": "NOT YET AVAILABLE",
            "performance_status": status,
            "prospective_focus": self._prospective_focus_data(paper_positions),
            "system_status": system_status,
        }

    def get_segment_explorer_payload(self) -> dict[str, Any]:
        paper_rows, paper_status = self._paper_positions()
        return {
            **self._segment_explorer_payload(paper_rows=paper_rows),
            "system_status": self._build_system_status(
                paper_status=paper_status,
                storage_state=self._storage_status(),
            ),
        }

    def get_error_centre_payload(self) -> dict[str, Any]:
        betting_desk = self.get_betting_desk_payload()
        provider_issue_rows = [
            row
            for row in betting_desk["rows"]
            if self._blocking_reason_key(row) in {
                "NO_BET_STALE_MARKET",
                "MISSING_REFERENCE_MARKET",
                "MARKET_UNAVAILABLE",
                "PROVIDER_QUOTA_BLOCKED",
                "LINE_UNSUPPORTED",
            }
        ]
        paper_rows, _ = self._paper_positions()
        signals = self._signal_payload(system_status=betting_desk["system_status"], paper_rows=paper_rows)
        return {
            "performance_signals": [signal for signal in signals if signal["category"] != "PROVIDER WARNING"],
            "provider_signals": [signal for signal in signals if signal["category"] == "PROVIDER WARNING"],
            "provider_issue_rows": provider_issue_rows,
            "blocking_reasons": betting_desk["blocking_reasons"],
            "system_status": betting_desk["system_status"],
        }

    def get_no_bet_archive_payload(self, *, page: int = 1) -> dict[str, Any]:
        desk_payload = self.get_betting_desk_payload()
        archive_rows = [
            row
            for row in desk_payload["rows"]
            if row["decision_group"] != "bet"
        ]
        pagination = _paginate_collection(archive_rows, page=page, page_size=20)
        return {
            "rows": archive_rows,
            "visible_rows": pagination["items"],
            "pagination": pagination,
            "count": len(archive_rows),
            "blocking_reasons": desk_payload["blocking_reasons"],
            "system_status": desk_payload["system_status"],
        }

    def get_reports_payload(self) -> dict[str, Any]:
        paper_rows, paper_status = self._paper_positions()
        portfolio_context = self._portfolio_selection_context(paper_rows)
        return {
            **self._reports_payload(paper_rows=paper_rows),
            "portfolio_windows": self._report_window_rows(portfolio_context["selected_rows"]),
            "portfolio_policy": None
            if portfolio_context.get("policy") is None
            else {
                "policy_id": portfolio_context["policy"].policy_id,
                "policy_version": portfolio_context["policy"].policy_version,
                "effective_from": portfolio_context["policy"].effective_from,
            },
            "portfolio_publish_state": portfolio_context["publish_state"],
            "portfolio_publish_note": portfolio_context["publish_note"],
            "system_status": self._build_system_status(
                paper_status=paper_status,
                storage_state=self._storage_status(),
            ),
        }

    def _prospective_focus_data(self, paper_rows: list[dict[str, Any]]) -> dict[str, Any]:
        with football_paper_trading.connect(self.config.paper_trading_db_path) as conn:
            cohort = football_prospective_cohort.resolve_cohort(
                conn,
                initialize_schema=self._read_requires_schema_init(),
            )
        return football_prospective_cohort.prospective_focus_performance(paper_rows, cohort)

    def get_prospective_focus_payload(self) -> dict[str, Any]:
        """Historical reconciled vs prospective focused (AH + Corners) evidence.

        The prospective cohort is persisted (reproducible after restart) and
        counts only reconciled (duplicate-excluded) focused positions decided at
        or after the cohort effective-from boundary. Historical all-time
        performance is reported separately so the two can never be conflated.
        """
        paper_rows, paper_status = self._paper_positions()
        prospective = self._prospective_focus_data(paper_rows)
        return {
            **prospective,
            "paper_status": paper_status,
            "system_status": self._build_system_status(
                paper_status=paper_status,
                storage_state=self._storage_status(),
            ),
        }

    def get_next_fixtures_payload(self) -> dict[str, Any]:
        payload = self.get_dashboard_payload()
        return {
            "fixtures": payload["next_matches"],
            "count": len(payload["next_matches"]),
            "nearest_supported_fixture": self._provider_state.get("nearest_supported_fixture"),
            "system_status": payload["system_status"],
        }

    def _build_system_status(
        self,
        *,
        paper_status: dict[str, Any] | None = None,
        storage_state: dict[str, Any] | None = None,
        summary_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._refresh_persisted_runtime_state_for_web()
        snapshot_state = "READY" if self.snapshot is not None else "BLOCKED"
        market_state = self._market_provider_state()
        fixture_requirements = self._fixture_feed_runtime_requirements()
        odds_requirements = self._odds_feed_runtime_requirements()
        xg_status = self._xg_runtime_status()
        resolved_summary_payload = summary_payload if summary_payload is not None else self._load_json_file(self._performance_summary_path())
        resolved_paper_status = paper_status
        if resolved_paper_status is None:
            _, resolved_paper_status = self._paper_positions()
        performance_status = self._performance_status(resolved_summary_payload, resolved_paper_status)
        resolved_storage_state = storage_state if storage_state is not None else self._storage_status()
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
            "storage": resolved_storage_state,
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
            "paid_provider_calls_allowed": "YES" if self.config.allow_paid_provider_calls else "NO",
            "provider_budget": self._provider_budget.snapshot(),
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

    def get_system_status(self) -> dict[str, Any]:
        return self._build_system_status()

    def get_health_payload(self) -> dict[str, Any]:
        self._refresh_persisted_runtime_state_for_web()
        storage_state = self._storage_status()
        snapshot_ready = self.snapshot is not None
        startup_sync_task_running = self._startup_sync_task is not None and not self._startup_sync_task.done()
        local_scheduler_running = self._background_task is not None and not self._background_task.done()
        scheduler_execution_mode = "EXTERNAL_PROCESS" if self.config.runtime_role == RUNTIME_ROLE_WEB else "LOCAL_TASK"
        scheduler_running = None if self.config.runtime_role == RUNTIME_ROLE_WEB else local_scheduler_running
        return {
            "status": "ok",
            "web_process_status": "ok",
            "runtime_role": self.config.runtime_role,
            "scheduler_execution_mode": scheduler_execution_mode,
            "timestamp_utc": utc_now_iso(),
            "startup_sync_state": self._startup_sync_state["state"],
            "startup_sync_started_at": self._startup_sync_state["started_at"],
            "startup_sync_completed_at": self._startup_sync_state["completed_at"],
            "startup_sync_last_error": self._startup_sync_state["last_error"],
            "startup_sync_task_running": startup_sync_task_running,
            "scheduler_enabled": self.config.enable_scheduler,
            "local_scheduler_running": local_scheduler_running,
            "scheduler_running": scheduler_running,
            "fixture_provider_state": self._provider_state["state"],
            "last_fixture_sync_at": self._provider_state["last_sync_at"],
            "market_sync_state": self._market_runtime_state["state"],
            "last_market_sync_at": self._market_runtime_state["last_sync_at"],
            "paid_provider_calls_allowed": "YES" if self.config.allow_paid_provider_calls else "NO",
            "provider_budget_quota_state": self._provider_budget.quota_state(),
            "settlement_queue_count": int(self._market_runtime_state.get("settlement_queue_count") or 0),
            "prediction_snapshot_ready": snapshot_ready,
            "dashboard_db_reachable": storage_state["dashboard_db_reachable"],
            "paper_trading_db_reachable": storage_state["paper_trading_db_reachable"],
            "storage_mode": storage_state["mode"],
            "persistent_path_configured": storage_state["persistent_path_configured"],
        }
