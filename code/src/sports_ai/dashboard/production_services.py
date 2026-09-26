"""Hardened scheduler services for the live Football product.

This layer keeps the existing Route B model/manager immutable while adding:
- persistent current-season state refresh with per-competition freshness gates;
- optional live external-context enforcement through the existing quota governor;
- an isolated candidate evidence ledger for veto/CLV/settlement validation;
- production health diagnostics that fail visibly when model state is stale.
"""

from __future__ import annotations

import logging
import os
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..football_market_focus import configured_live_market_focus
from ..experiments.football_api_history_probe import (
    run_historical_coverage_probe,
    target_for_competition,
)
from ..locking import canonical_hash
from ..storage import football_candidate_evidence, football_paper_trading, football_research_store
from . import services as services_module
from . import storage
from .football_context_provider import ApiFootballContextProvider
from .models import (
    FIXTURE_LIVE_STATUSES,
    FIXTURE_STATUS_FINISHED,
    PREDICTION_STATUS_BLOCKED,
    NormalizedFixture,
    parse_iso_datetime,
    utc_now_iso,
)
from .prediction_service import attach_edge_desk_shadow_bundle
from .provider_budget import QUOTA_HEALTHY
from .production_research import (
    DEFAULT_PRODUCTION_DIVISIONS,
    ResearchRefreshResult,
    prepare_persistent_research_db,
    refresh_current_season_events,
)
from .services import DashboardConfig, DashboardServices

logger = logging.getLogger(__name__)

_BASE_MARKET_EVALUATOR = services_module.build_fixture_market_evaluation
MODEL_SNAPSHOT_STATE_KEY = "model_snapshot_state"
PRODUCTION_RUNTIME_STATE_KEY = "production_hardening_state"
HISTORY_PROBE_ONCE_ENV_VAR = "SPORTS_AI_FOOTBALL_HISTORY_PROBE_ONCE"
HISTORY_PROBE_STATE_PREFIX = "football_history_probe_once"
HISTORY_PROBE_IMPLEMENTATION_VERSION = "stable_league_id_v2"

_CONTEXT_MODES = {"off", "shadow", "enforce"}
DEFAULT_RESEARCH_REFRESH_SECONDS = 6 * 60 * 60
DEFAULT_MAX_MODEL_STATE_AGE_DAYS = 21
DEFAULT_CONTEXT_LOOKAHEAD_HOURS = 24.0
DEFAULT_LINEUP_WINDOW_MINUTES = 90.0


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "").strip())
    except ValueError:
        return default
    return value if value > 0 else default


def _context_mode() -> str:
    raw = os.environ.get("SPORTS_AI_FOOTBALL_CONTEXT_MODE", "shadow").strip().lower()
    return raw if raw in _CONTEXT_MODES else "shadow"


def _parse_history_probe_once_spec(raw: str) -> tuple[str, int, int] | None:
    value = str(raw or "").strip()
    if not value:
        return None
    parts = [part.strip() for part in value.split(":")]
    if len(parts) not in {2, 3}:
        raise ValueError("history probe spec must be COMPETITION:SEASON[:SAMPLES]")
    competition_id = parts[0].upper()
    if not competition_id:
        raise ValueError("history probe competition must not be empty")
    try:
        season = int(parts[1])
    except ValueError as exc:
        raise ValueError("history probe season must be a four-digit start year") from exc
    if season < 2000 or season > 2100:
        raise ValueError("history probe season must be a four-digit start year")
    sample_size = 3
    if len(parts) == 3:
        try:
            sample_size = int(parts[2])
        except ValueError as exc:
            raise ValueError("history probe sample size must be an integer") from exc
    if sample_size < 1 or sample_size > 3:
        raise ValueError("history probe sample size must be between 1 and 3")
    try:
        target_for_competition(competition_id)
    except KeyError as exc:
        raise ValueError(f"unsupported history probe competition {competition_id}") from exc
    return competition_id, season, sample_size


def _production_divisions() -> tuple[str, ...]:
    raw = os.environ.get("SPORTS_AI_FOOTBALL_STATE_DIVISIONS", "").strip()
    if not raw:
        return DEFAULT_PRODUCTION_DIVISIONS
    values = tuple(dict.fromkeys(value.strip() for value in raw.split(",") if value.strip()))
    return values or DEFAULT_PRODUCTION_DIVISIONS


class ProductionDashboardServices(DashboardServices):
    """Scheduler service with current-state freshness and evidence hardening."""

    def __init__(self, config: DashboardConfig | None = None, **kwargs: Any) -> None:
        requested = DashboardConfig.default() if config is None else config
        self._bundled_research_db_path = Path(requested.research_db_path)
        self._research_refresh_interval_seconds = _env_int(
            "SPORTS_AI_FOOTBALL_STATE_REFRESH_SECONDS",
            DEFAULT_RESEARCH_REFRESH_SECONDS,
        )
        self._max_model_state_age_days = _env_int(
            "SPORTS_AI_FOOTBALL_MAX_MODEL_STATE_AGE_DAYS",
            DEFAULT_MAX_MODEL_STATE_AGE_DAYS,
        )
        self._context_mode = _context_mode()
        self._context_lookahead_hours = _env_float(
            "SPORTS_AI_FOOTBALL_CONTEXT_LOOKAHEAD_HOURS",
            DEFAULT_CONTEXT_LOOKAHEAD_HOURS,
        )
        self._lineup_window_minutes = _env_float(
            "SPORTS_AI_FOOTBALL_LINEUP_WINDOW_MINUTES",
            DEFAULT_LINEUP_WINDOW_MINUTES,
        )
        self._require_final_lineup = _env_bool("SPORTS_AI_FOOTBALL_REQUIRE_FINAL_LINEUP", False)
        self._last_research_refresh_attempt: datetime | None = None
        self._research_refresh_state: dict[str, Any] = {
            "state": "NOT_RUN",
            "last_attempt_at": None,
            "last_success_at": None,
            "last_error": None,
            "season_code": None,
            "successful_divisions": [],
            "failed_divisions": {},
            "changed_rows": 0,
            "latest_match_date": None,
        }
        self._competition_model_freshness: dict[str, dict[str, Any]] = {}
        self._context_provider: ApiFootballContextProvider | None = None
        self._context_fixture_cache: dict[str, dict[str, Any]] = {}
        self._history_probe_once_spec = os.environ.get(HISTORY_PROBE_ONCE_ENV_VAR, "").strip()
        self._history_probe_state: dict[str, Any] = {
            "configured": bool(self._history_probe_once_spec),
            "state": "NOT_CONFIGURED" if not self._history_probe_once_spec else "PENDING",
            "token_id": None,
            "result": None,
            "error_code": None,
        }

        resolved = requested
        if self._bundled_research_db_path.exists():
            live_research = prepare_persistent_research_db(
                base_db_path=self._bundled_research_db_path,
                operational_data_dir=Path(requested.operational_data_dir),
            )
            resolved = replace(requested, research_db_path=live_research)
        super().__init__(config=resolved, **kwargs)

        self._context_provider = ApiFootballContextProvider()
        self._context_provider.attach_request_budget(self._provider_budget)
        # The base service imports the evaluator as a module global. The scheduler
        # is a dedicated process, so replace that hook only in this process and
        # restore it on shutdown.
        services_module.build_fixture_market_evaluation = self._production_market_evaluator

    async def initialize(self) -> None:
        self._restore_persisted_research_refresh_state()
        self._refresh_research_if_due(force=False, rebuild_snapshot=False)
        # Prime model-state freshness before the base startup sync. The base
        # initialize() launches startup synchronization, which calls
        # ensure_prediction(); leaving this map empty until afterwards makes
        # every otherwise-fresh fixture fail closed as MODEL_STATE_STALE.
        self._refresh_competition_freshness()
        await super().initialize()
        self._refresh_competition_freshness()
        self._run_history_probe_once()
        self._log_live_market_evidence()
        self._log_production_freshness()
        self._persist_production_runtime_state()

    def _log_live_market_evidence(self) -> None:
        focus = configured_live_market_focus()
        try:
            _rows, paper_status = self._paper_positions()
            performance = paper_status.get("performance") or {}
            by_market = performance.get("by_market") or {}
        except Exception as exc:
            logger.warning(
                "football live market evidence unavailable: focus=%s error=%s",
                list(focus),
                type(exc).__name__,
            )
            return

        evidence: list[str] = []
        for market in focus:
            row = by_market.get(market) or {}
            evidence.append(
                f"{market}:positions={int(row.get('positions') or 0)}"
                f",settled={int(row.get('settled_positions') or 0)}"
                f",net_units={row.get('net_units')}"
                f",roi={row.get('roi')}"
                f",strike_rate={row.get('strike_rate')}"
            )
        logger.info(
            "football live market evidence: focus=%s %s",
            list(focus),
            " | ".join(evidence),
        )

    def _run_history_probe_once(self) -> None:
        raw = str(getattr(self, "_history_probe_once_spec", "") or "").strip()
        if not raw:
            return
        token_id = canonical_hash(
            {
                "history_probe_once": raw,
                "implementation_version": HISTORY_PROBE_IMPLEMENTATION_VERSION,
            }
        )[:12]
        state_key = f"{HISTORY_PROBE_STATE_PREFIX}:{token_id}"
        self._history_probe_state.update({"token_id": token_id, "state": "CHECKING"})

        with storage.connect(self.config.dashboard_db_path) as conn:
            existing = storage.get_runtime_state(
                conn,
                state_key,
                initialize_schema=False,
            )
            if isinstance(existing, dict):
                self._history_probe_state = dict(existing)
                logger.info(
                    "football history probe: token_id=%s state=%s reused=true",
                    token_id,
                    self._history_probe_state.get("state"),
                )
                return

            claimed = {
                "configured": True,
                "state": "CLAIMED",
                "token_id": token_id,
                "spec": raw,
                "claimed_at": utc_now_iso(),
                "result": None,
                "error_code": None,
            }
            storage.put_runtime_state(
                conn,
                state_key=state_key,
                state_value=claimed,
                updated_at=claimed["claimed_at"],
                initialize_schema=False,
            )

        try:
            parsed = _parse_history_probe_once_spec(raw)
            if parsed is None:
                return
            competition_id, season, sample_size = parsed
            quota_state = self._provider_budget.quota_state()
            if quota_state != QUOTA_HEALTHY:
                result_payload = {
                    "competition_id": competition_id,
                    "season": season,
                    "state": "BLOCKED",
                    "bounded_probe_requests": 0,
                    "projected_backfill_requests": None,
                    "sampled_gap_ready_count": 0,
                    "sampled_fixture_count": 0,
                    "reason": f"LOW_PRIORITY_QUOTA_{quota_state}",
                }
            else:
                target = target_for_competition(competition_id)
                result = run_historical_coverage_probe(
                    self.fixture_provider,
                    target=target,
                    season=season,
                    sample_size=sample_size,
                )
                result_payload = result.to_dict()
            final = {
                "configured": True,
                "state": "COMPLETED",
                "token_id": token_id,
                "spec": raw,
                "claimed_at": claimed["claimed_at"],
                "completed_at": utc_now_iso(),
                "result": result_payload,
                "error_code": None,
            }
        except Exception as exc:
            final = {
                "configured": True,
                "state": "FAILED",
                "token_id": token_id,
                "spec": raw,
                "claimed_at": claimed["claimed_at"],
                "completed_at": utc_now_iso(),
                "result": None,
                "error_code": type(exc).__name__,
            }

        self._history_probe_state = final
        with storage.connect(self.config.dashboard_db_path) as conn:
            storage.put_runtime_state(
                conn,
                state_key=state_key,
                state_value=final,
                updated_at=str(final["completed_at"]),
                initialize_schema=False,
            )

        result_payload = final.get("result") or {}
        logger.info(
            "football history probe: token_id=%s state=%s competition=%s season=%s "
            "probe_requests=%s projected_backfill_requests=%s sampled_ready=%s/%s reason=%s error_code=%s",
            token_id,
            final.get("state"),
            result_payload.get("competition_id"),
            result_payload.get("season"),
            result_payload.get("bounded_probe_requests"),
            result_payload.get("projected_backfill_requests"),
            result_payload.get("sampled_gap_ready_count"),
            result_payload.get("sampled_fixture_count"),
            result_payload.get("reason"),
            final.get("error_code"),
        )

    def _restore_persisted_research_refresh_state(self) -> None:
        """Restore the research cadence across sleeping-process cold starts."""
        path = Path(self.config.dashboard_db_path)
        if not path.exists():
            return
        try:
            with storage.connect(path) as conn:
                persisted = storage.get_runtime_state(
                    conn,
                    PRODUCTION_RUNTIME_STATE_KEY,
                    initialize_schema=False,
                )
        except Exception:
            logger.exception("failed to restore Football production refresh cadence")
            return
        if not isinstance(persisted, dict):
            return
        research_refresh = persisted.get("research_refresh")
        if not isinstance(research_refresh, dict):
            return
        self._research_refresh_state.update(research_refresh)
        raw_last_attempt = str(research_refresh.get("last_attempt_at") or "").strip()
        if not raw_last_attempt:
            return
        try:
            self._last_research_refresh_attempt = parse_iso_datetime(raw_last_attempt)
        except ValueError:
            logger.warning(
                "ignoring invalid persisted Football research refresh timestamp: %s",
                raw_last_attempt,
            )

    async def shutdown(self) -> None:
        try:
            await super().shutdown()
        finally:
            services_module.build_fixture_market_evaluation = _BASE_MARKET_EVALUATOR

    async def sync_once(self, *, sync_kind: str = "manual", include_schedule: bool = True) -> dict[str, Any]:
        refreshed = self._refresh_research_if_due(force=False, rebuild_snapshot=True)
        if refreshed:
            self._persist_production_runtime_state()
        result = await super().sync_once(sync_kind=sync_kind, include_schedule=include_schedule)
        self._persist_production_runtime_state()
        return result

    def _refresh_research_if_due(self, *, force: bool, rebuild_snapshot: bool) -> bool:
        now = datetime.now(timezone.utc)
        if (
            not force
            and self._last_research_refresh_attempt is not None
            and (now - self._last_research_refresh_attempt).total_seconds() < self._research_refresh_interval_seconds
        ):
            return False
        self._last_research_refresh_attempt = now
        self._research_refresh_state["last_attempt_at"] = now.isoformat(timespec="seconds")
        try:
            result = refresh_current_season_events(
                Path(self.config.research_db_path),
                divisions=_production_divisions(),
            )
        except Exception as exc:
            self._research_refresh_state.update(
                {
                    "state": "FAILED",
                    "last_error": f"{type(exc).__name__}: {exc}",
                }
            )
            logger.exception("Football production research refresh failed")
            self._refresh_competition_freshness()
            return False

        self._record_research_refresh(result)
        if rebuild_snapshot:
            previous_snapshot_id = None if self.snapshot is None else self.snapshot.snapshot_id
            super()._build_snapshot()
            self._refresh_competition_freshness()
            current_snapshot_id = None if self.snapshot is None else self.snapshot.snapshot_id
            if current_snapshot_id != previous_snapshot_id:
                self._persist_model_snapshot_state()
        else:
            self._refresh_competition_freshness()
        return True

    def _record_research_refresh(self, result: ResearchRefreshResult) -> None:
        self._research_refresh_state.update(
            {
                "state": "READY" if result.fully_successful else "DEGRADED",
                "last_success_at": result.refreshed_at if result.successful_divisions else self._research_refresh_state.get("last_success_at"),
                "last_error": None if result.fully_successful else "one or more current-season divisions failed refresh",
                "season_code": result.season_code,
                "successful_divisions": list(result.successful_divisions),
                "failed_divisions": dict(result.failed_divisions),
                "changed_rows": int(result.changed_rows),
                "latest_match_date": result.latest_match_date,
            }
        )

    def _refresh_competition_freshness(self) -> None:
        path = Path(self.config.research_db_path)
        if not path.exists():
            self._competition_model_freshness = {}
            return
        current_day = datetime.now(timezone.utc).date()
        with football_research_store.connect(path) as conn:
            rows = conn.execute(
                """
                SELECT competition_id, MAX(match_date) AS latest_match_date
                FROM football_events
                WHERE home_goals IS NOT NULL
                  AND away_goals IS NOT NULL
                  AND home_shots IS NOT NULL
                  AND away_shots IS NOT NULL
                  AND home_shots_on_target IS NOT NULL
                  AND away_shots_on_target IS NOT NULL
                  AND home_corners IS NOT NULL
                  AND away_corners IS NOT NULL
                GROUP BY competition_id
                """
            ).fetchall()
        freshness: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest = str(row["latest_match_date"])
            latest_day = date.fromisoformat(latest)
            age_days = max(0, (current_day - latest_day).days)
            freshness[str(row["competition_id"])] = {
                "latest_complete_match_date": latest,
                "age_days": age_days,
                "max_age_days": self._max_model_state_age_days,
                "state": "FRESH" if age_days <= self._max_model_state_age_days else "STALE",
            }
        self._competition_model_freshness = freshness

    def _competition_is_fresh(self, competition_id: str) -> bool:
        row = self._competition_model_freshness.get(str(competition_id))
        return bool(row is not None and row.get("state") == "FRESH")

    def ensure_prediction(self, conn: Any, fixture: NormalizedFixture) -> dict[str, Any]:
        if self.snapshot is not None and not self._competition_is_fresh(fixture.competition_id):
            freshness = self._competition_model_freshness.get(str(fixture.competition_id)) or {}
            version_key = canonical_hash(
                {
                    "fixture_id": fixture.fixture_id,
                    "model_snapshot_id": self.snapshot.snapshot_id,
                    "kickoff_utc": fixture.kickoff_utc,
                    "model_state_freshness": freshness,
                }
            )
            existing = storage.fetch_prediction_by_version_key(
                conn,
                version_key,
                initialize_schema=self._read_requires_schema_init(),
            )
            if existing is not None:
                return existing
            latest = freshness.get("latest_complete_match_date") or "unavailable"
            payload = attach_edge_desk_shadow_bundle(
                {
                    "prediction_id": f"prediction_stale_{fixture.fixture_id}_{version_key[:12]}",
                    "fixture_id": fixture.fixture_id,
                    "generated_at": utc_now_iso(),
                    "model_as_of": self.snapshot.model_as_of,
                    "model_snapshot_id": self.snapshot.snapshot_id,
                    "prediction_version": f"stale_model_state|{version_key[:16]}",
                    "prediction_locked_at": fixture.kickoff_utc,
                    "version_key": version_key,
                    "prediction_status": PREDICTION_STATUS_BLOCKED,
                    "blockers": ["MODEL_STATE_STALE"],
                    "warnings": [
                        f"Latest complete {fixture.competition_id} model-state match is {latest}; "
                        f"maximum allowed age is {self._max_model_state_age_days} days."
                    ],
                    "manager_decision_status": "MODEL_STATE_STALE",
                    "safeguard_status": "BLOCKED",
                    "safeguard_reason": "CURRENT-SEASON MODEL STATE STALE",
                    "kickoff_utc": fixture.kickoff_utc,
                    "competition_id": fixture.competition_id,
                    "competition_name": fixture.competition_name,
                    "home_team_name": fixture.home_team_name,
                    "away_team_name": fixture.away_team_name,
                    "model_state_freshness": freshness,
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
        return super().ensure_prediction(conn, fixture)

    def _prediction_eligibility_status(self, prediction: dict[str, Any] | None) -> str:
        blockers = set() if prediction is None else {str(value) for value in prediction.get("blockers") or []}
        if "MODEL_STATE_STALE" in blockers:
            return "MODEL_STATE_STALE"
        return super()._prediction_eligibility_status(prediction)

    def _production_market_evaluator(self, **kwargs: Any) -> dict[str, Any]:
        mode = self._context_mode
        if mode == "off":
            return _BASE_MARKET_EVALUATOR(**kwargs)

        baseline = _BASE_MARKET_EVALUATOR(**kwargs)
        contextual = _BASE_MARKET_EVALUATOR(
            **kwargs,
            context_signals_provider=self._context_signals,
            strict_context_evidence=(mode == "enforce"),
        )
        if mode == "enforce":
            for row in contextual.get("decision_rows") or []:
                row["context_enforced"] = True
                row["context_mode"] = "enforce"
            return contextual

        contextual_by_id = {
            str(row.get("decision_id") or ""): row
            for row in contextual.get("decision_rows") or []
        }
        for row in baseline.get("decision_rows") or []:
            shadow = contextual_by_id.get(str(row.get("decision_id") or ""))
            row["context_mode"] = "shadow"
            row["context_enforced"] = False
            if shadow is None:
                continue
            row["context_shadow_status"] = shadow.get("context_status")
            row["context_shadow_reason_codes"] = list(shadow.get("context_reason_codes") or [])
            row["context_shadow_threshold_adjustment"] = shadow.get("context_threshold_adjustment")
            row["context_shadow_adjusted_threshold"] = shadow.get("context_adjusted_threshold")
            row["context_shadow_threshold_gate_passed"] = shadow.get("context_threshold_gate_passed")
            row["context_shadow_diagnostics"] = dict(shadow.get("context_diagnostics") or {})
        return baseline

    def _provider_fixture_id_for_context(self, fixture_context: dict[str, Any]) -> str | None:
        provider = str(fixture_context.get("provider") or "").strip().lower()
        provider_fixture_id = str(fixture_context.get("provider_fixture_id") or "").strip()
        if provider == "api-football" and provider_fixture_id and not provider_fixture_id.startswith("generated:"):
            return provider_fixture_id
        resolver = getattr(self.statistics_provider, "_resolve_statistics_fixture_id", None)
        if callable(resolver):
            try:
                return str(resolver(fixture_context))
            except Exception:
                return None
        return None

    def _european_congestion_hours(self, fixture_context: dict[str, Any]) -> float | None:
        target_kickoff = parse_iso_datetime(str(fixture_context["kickoff_utc"]))
        home = str(fixture_context.get("home_team_name") or "").strip().casefold()
        away = str(fixture_context.get("away_team_name") or "").strip().casefold()
        if not home or not away:
            return None
        try:
            with storage.connect(self.config.dashboard_db_path) as conn:
                fixtures = storage.fetch_all_fixtures(conn, initialize_schema=self._read_requires_schema_init())
        except Exception:
            return None
        distances: list[float] = []
        for row in fixtures:
            if str(row.get("fixture_id") or "") == str(fixture_context.get("fixture_id") or ""):
                continue
            if str(row.get("competition_id") or "") not in {"UCL", "UEL"}:
                continue
            row_home = str(row.get("home_team_name") or "").strip().casefold()
            row_away = str(row.get("away_team_name") or "").strip().casefold()
            if home not in {row_home, row_away} and away not in {row_home, row_away}:
                continue
            try:
                distance = abs((parse_iso_datetime(str(row["kickoff_utc"])) - target_kickoff).total_seconds()) / 3600.0
            except Exception:
                continue
            if distance <= 96.0:
                distances.append(distance)
        return min(distances) if distances else None

    def _context_signals(self, candidate_context: dict[str, Any]) -> dict[str, Any]:
        fixture = dict(candidate_context.get("fixture_context") or {})
        best_offer = dict(candidate_context.get("best_offer") or {})
        kickoff = parse_iso_datetime(str(fixture["kickoff_utc"]))
        hours_to_kickoff = (kickoff - datetime.now(timezone.utc)).total_seconds() / 3600.0
        signals: dict[str, Any] = {
            "required_evidence": [],
            "available_evidence": {},
            "source_states": {},
            "current_odds": best_offer.get("decimal_odds"),
        }

        europe_hours = self._european_congestion_hours(fixture)
        if europe_hours is not None:
            signals["hours_from_european_fixture"] = europe_hours

        model_row = dict(candidate_context.get("model_row") or {})
        with football_paper_trading.connect(self.config.paper_trading_db_path) as conn:
            existing = football_candidate_evidence.fetch_observation(
                conn,
                fixture_id=str(fixture.get("fixture_id") or model_row.get("fixture_id") or ""),
                market=str(model_row.get("market_family") or ""),
                selection=str(model_row.get("selection") or ""),
                line=None if model_row.get("line") is None else float(model_row["line"]),
            )
        if existing is not None:
            signals["baseline_odds"] = existing.get("entry_odds")

        if hours_to_kickoff <= 0 or hours_to_kickoff > self._context_lookahead_hours:
            if self._context_mode == "enforce" and self._require_final_lineup and hours_to_kickoff > (self._lineup_window_minutes / 60.0):
                signals["required_evidence"].append("final_lineup_window")
                signals["available_evidence"]["final_lineup_window"] = False
            return signals

        provider_fixture_id = self._provider_fixture_id_for_context(fixture)
        if provider_fixture_id is None or self._context_provider is None or not self._context_provider.configured():
            signals["required_evidence"].append("context_provider")
            signals["available_evidence"]["context_provider"] = False
            return signals

        cached = self._context_fixture_cache.get(provider_fixture_id)
        if cached is None:
            cached = self._context_provider.get_fixture_context(provider_fixture_id)
            self._context_fixture_cache[provider_fixture_id] = cached
        injuries = dict(cached.get("injuries") or {})
        lineups = dict(cached.get("lineups") or {})

        signals["required_evidence"].append("injuries")
        injuries_request_ok = str(injuries.get("request_state") or "") in {"OK", "CACHE_HIT"}
        signals["available_evidence"]["injuries"] = injuries_request_ok
        if injuries_request_ok:
            signals["source_states"]["injuries"] = "FRESH"

        in_lineup_window = hours_to_kickoff <= (self._lineup_window_minutes / 60.0)
        if in_lineup_window or self._require_final_lineup:
            signals["required_evidence"].append("lineups")
            lineups_available = str(lineups.get("evidence_state") or "") == "AVAILABLE"
            signals["available_evidence"]["lineups"] = lineups_available
            if lineups_available:
                signals["source_states"]["lineups"] = "FRESH"
        signals["available_evidence"]["context_provider"] = True
        signals["context_provider_fixture_id"] = provider_fixture_id
        signals["injury_evidence_count"] = len(injuries.get("injuries") or [])
        signals["confirmed_lineup_team_count"] = len(lineups.get("lineups") or [])
        return signals

    def _maybe_create_paper_entries(
        self,
        conn: Any,
        *,
        prediction: dict[str, Any],
        evaluation: dict[str, Any],
        defer_portfolio_sync: bool = False,
    ) -> int:
        for row in evaluation.get("decision_rows") or []:
            observation_id = football_candidate_evidence.record_candidate_observation(
                conn,
                prediction=prediction,
                decision_row=row,
                provider=self.market_provider.provider_name(),
            )
            if observation_id is not None:
                row["candidate_observation_id"] = observation_id
        return super()._maybe_create_paper_entries(
            conn,
            prediction=prediction,
            evaluation=evaluation,
            defer_portfolio_sync=defer_portfolio_sync,
        )

    def _process_paper_trading_updates(self, rows_with_predictions: list[dict[str, Any]]) -> None:
        super()._process_paper_trading_updates(rows_with_predictions)
        self._process_candidate_evidence_updates(rows_with_predictions)

    def _process_candidate_evidence_updates(self, rows_with_predictions: list[dict[str, Any]]) -> None:
        fixture_rows = {str(row["fixture_id"]): row for row in rows_with_predictions}
        now_utc = datetime.now(timezone.utc)
        with football_paper_trading.connect(self.config.paper_trading_db_path) as conn:
            observations = football_candidate_evidence.fetch_observations(conn, active_only=True)
            snapshots_by_fixture: dict[str, list[dict[str, Any]]] = {}
            for snapshot in football_paper_trading.fetch_market_snapshots(
                conn,
                initialize_schema=self._read_requires_schema_init(),
            ):
                snapshots_by_fixture.setdefault(str(snapshot["fixture_id"]), []).append(snapshot)

            for row in observations:
                fixture = self._resolve_fixture_for_paper_settlement(row=row, fixture_rows=fixture_rows)
                if fixture is None:
                    continue
                kickoff_utc = str(row["kickoff"])
                kickoff = parse_iso_datetime(kickoff_utc)
                if row.get("closing_odds") is None and now_utc >= kickoff:
                    closing = self._latest_closing_snapshot(
                        snapshots_by_fixture.get(str(row["fixture_id"]), []),
                        market=str(row["market"]),
                        selection=str(row["selection"]),
                        line=row.get("line"),
                        kickoff_utc=kickoff_utc,
                    )
                    if closing is not None:
                        football_candidate_evidence.attach_closing_snapshot(
                            conn,
                            observation_id=str(row["observation_id"]),
                            closing_odds=float(closing["odds"]),
                            closing_captured_at=str(closing["captured_at"]),
                            closing_snapshot_id=int(closing["snapshot_id"]),
                        )

                state = str(row.get("trade_state") or "PENDING")
                if state == "PENDING" and now_utc >= kickoff and str(fixture.get("status") or "") in FIXTURE_LIVE_STATUSES:
                    football_candidate_evidence.update_trade_state(
                        conn,
                        observation_id=str(row["observation_id"]),
                        trade_state="OPEN",
                        gross_return=None,
                        net_pnl=None,
                        settlement_payload={"fixture_status": fixture.get("status")},
                    )
                    state = "OPEN"
                if str(fixture.get("status") or "") != FIXTURE_STATUS_FINISHED:
                    continue

                actual_total_corners = None
                if fixture.get("home_corners") is not None and fixture.get("away_corners") is not None:
                    actual_total_corners = int(fixture["home_corners"]) + int(fixture["away_corners"])
                settlement_payload = {
                    "fixture_id": str(fixture["fixture_id"]),
                    "provider": fixture.get("provider"),
                    "provider_fixture_id": fixture.get("provider_fixture_id"),
                    "actual_home_goals": int(fixture.get("home_score") or 0),
                    "actual_away_goals": int(fixture.get("away_score") or 0),
                    "actual_home_corners": fixture.get("home_corners"),
                    "actual_away_corners": fixture.get("away_corners"),
                    "actual_total_corners": actual_total_corners,
                }
                try:
                    settled_state = football_paper_trading.settlement_state_for_market(
                        market=str(row["market"]),
                        selection=str(row["selection"]),
                        line=None if row.get("line") is None else float(row["line"]),
                        actual_home_goals=settlement_payload["actual_home_goals"],
                        actual_away_goals=settlement_payload["actual_away_goals"],
                        actual_total_corners=actual_total_corners,
                    )
                except ValueError:
                    football_candidate_evidence.update_trade_state(
                        conn,
                        observation_id=str(row["observation_id"]),
                        trade_state="UNSETTLED",
                        gross_return=None,
                        net_pnl=None,
                        settlement_payload=settlement_payload,
                    )
                    continue
                gross_return, net_pnl = football_paper_trading.profit_for_state(
                    state=settled_state,
                    odds=float(row["entry_odds"]),
                )
                football_candidate_evidence.update_trade_state(
                    conn,
                    observation_id=str(row["observation_id"]),
                    trade_state=settled_state,
                    gross_return=gross_return,
                    net_pnl=net_pnl,
                    settlement_payload=settlement_payload,
                )

    def _persist_model_snapshot_state(self) -> None:
        if self.snapshot is None:
            return
        route_a = dict(self.snapshot.diagnostics.get("route_a") or {})
        payload = {
            "snapshot_id": self.snapshot.snapshot_id,
            "model_as_of": self.snapshot.model_as_of,
            "supported_competitions": list(self.snapshot.supported_competitions),
            "source_match_cutoff": self.snapshot.diagnostics.get("model_as_of"),
            "model_version": self.snapshot.model_version,
            "route_a_status": route_a.get("overall_status"),
            "route_a_artifact_id": route_a.get("artifact_id"),
            "competition_model_freshness": dict(self._competition_model_freshness),
        }
        with storage.connect(self.config.dashboard_db_path) as conn:
            storage.put_runtime_state(
                conn,
                state_key=MODEL_SNAPSHOT_STATE_KEY,
                state_value=payload,
                updated_at=utc_now_iso(),
                initialize_schema=self._read_requires_schema_init(),
            )

    def _log_production_freshness(self) -> None:
        stale = {
            competition: {
                "latest_complete_match_date": row.get("latest_complete_match_date"),
                "age_days": row.get("age_days"),
                "max_age_days": row.get("max_age_days"),
            }
            for competition, row in sorted(self._competition_model_freshness.items())
            if row.get("state") != "FRESH"
        }
        logger.info(
            "football production freshness: research_state=%s last_attempt=%s last_success=%s "
            "latest_refresh_match=%s successful_divisions=%s failed_divisions=%s stale_competitions=%s",
            self._research_refresh_state.get("state"),
            self._research_refresh_state.get("last_attempt_at"),
            self._research_refresh_state.get("last_success_at"),
            self._research_refresh_state.get("latest_match_date"),
            self._research_refresh_state.get("successful_divisions"),
            sorted((self._research_refresh_state.get("failed_divisions") or {}).keys()),
            stale,
        )

    def _persist_production_runtime_state(self) -> None:
        evidence_summary: dict[str, Any]
        try:
            with football_paper_trading.connect(self.config.paper_trading_db_path) as conn:
                evidence_summary = football_candidate_evidence.evidence_summary(conn)
        except Exception as exc:
            evidence_summary = {"error": f"{type(exc).__name__}: {exc}"}
        payload = {
            "state": "READY" if all(
                row.get("state") == "FRESH"
                for row in self._competition_model_freshness.values()
            ) and self._competition_model_freshness else "DEGRADED",
            "research_refresh": dict(self._research_refresh_state),
            "competition_model_freshness": dict(self._competition_model_freshness),
            "context": {
                "mode": self._context_mode,
                "provider_configured": bool(self._context_provider and self._context_provider.configured()),
                "lookahead_hours": self._context_lookahead_hours,
                "lineup_window_minutes": self._lineup_window_minutes,
                "require_final_lineup": self._require_final_lineup,
                "expected_starter_baseline": "NOT_AVAILABLE_YET",
                "manager_change_signal": "NOT_AVAILABLE_YET",
                "dead_rubber_signal": "NOT_AVAILABLE_YET",
            },
            "candidate_evidence": evidence_summary,
            "history_probe_once": dict(self._history_probe_state),
            "live_market_focus": list(configured_live_market_focus()),
            "updated_at": utc_now_iso(),
        }
        try:
            with storage.connect(self.config.dashboard_db_path) as conn:
                storage.put_runtime_state(
                    conn,
                    state_key=PRODUCTION_RUNTIME_STATE_KEY,
                    state_value=payload,
                    updated_at=payload["updated_at"],
                    initialize_schema=self._read_requires_schema_init(),
                )
        except Exception:
            logger.exception("failed to persist Football production hardening state")

    def _build_system_status(self, **kwargs: Any) -> dict[str, Any]:
        payload = super()._build_system_status(**kwargs)
        stale_competitions = sorted(
            competition
            for competition, row in self._competition_model_freshness.items()
            if row.get("state") != "FRESH"
        )
        payload["production_hardening"] = {
            "state": "READY" if not stale_competitions and self._competition_model_freshness else "DEGRADED",
            "research_refresh": dict(self._research_refresh_state),
            "competition_model_freshness": dict(self._competition_model_freshness),
            "stale_competitions": stale_competitions,
            "context_mode": self._context_mode,
            "context_provider_configured": bool(self._context_provider and self._context_provider.configured()),
        }
        if stale_competitions:
            payload["current_prediction_pipeline_ready"] = "NO"
            payload["live_paper_pipeline_ready"] = "NO"
        return payload

    def get_health_payload(self) -> dict[str, Any]:
        payload = super().get_health_payload()
        stale_competitions = sorted(
            competition
            for competition, row in self._competition_model_freshness.items()
            if row.get("state") != "FRESH"
        )
        payload["model_state_freshness"] = {
            "state": "READY" if not stale_competitions and self._competition_model_freshness else "DEGRADED",
            "stale_competitions": stale_competitions,
            "competitions": dict(self._competition_model_freshness),
        }
        payload["research_refresh_state"] = self._research_refresh_state.get("state")
        payload["context_mode"] = self._context_mode
        return payload
