"""Final live scheduler policy layered over ProductionDashboardServices."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import logging
from typing import Any, Callable

from ..locking import canonical_hash
from ..storage import football_candidate_evidence, football_paper_trading
from .football_context_veto import REASON_NEWLY_PROMOTED_RELEGATED
from .football_transition_maturity import (
    TRANSITION_BASELINE_VERSION,
    transition_maturity_for_fixture,
)
from .models import parse_iso_datetime
from .production_services import ProductionDashboardServices

logger = logging.getLogger(__name__)


def _summarize_prediction_rows(
    rows: list[dict[str, Any]],
    *,
    eligibility_resolver: Callable[[dict[str, Any] | None], str],
) -> dict[str, Any]:
    """Summarize eligibility and blocker reasons for an already-scoped fixture set."""
    eligibility_counts: Counter[str] = Counter()
    blocker_reasons: Counter[str] = Counter()
    blocked_examples: list[dict[str, Any]] = []

    for row in rows:
        prediction = row.get("prediction")
        eligibility = str(eligibility_resolver(prediction))
        eligibility_counts[eligibility] += 1
        if eligibility in {"PREDICTION_READY", "PREDICTION_PENDING"}:
            continue

        blockers = [] if prediction is None else [str(value) for value in (prediction.get("blockers") or []) if str(value)]
        if not blockers:
            blockers = [eligibility or "UNKNOWN_BLOCKER"]
        blocker_reasons.update(blockers)
        if len(blocked_examples) < 5:
            blocked_examples.append(
                {
                    "fixture_id": str(row.get("fixture_id") or ""),
                    "competition_id": str(row.get("competition_id") or ""),
                    "home_team_name": row.get("home_team_name"),
                    "away_team_name": row.get("away_team_name"),
                    "kickoff_utc": row.get("kickoff_utc"),
                    "eligibility": eligibility,
                    "blockers": blockers,
                }
            )

    return {
        "fixtures": len(rows),
        "ready": int(eligibility_counts.get("PREDICTION_READY", 0)),
        "pending": int(eligibility_counts.get("PREDICTION_PENDING", 0)),
        "blocked": int(
            sum(
                count
                for status, count in eligibility_counts.items()
                if status not in {"PREDICTION_READY", "PREDICTION_PENDING"}
            )
        ),
        "eligibility": dict(sorted(eligibility_counts.items())),
        "blocker_reasons": dict(sorted(blocker_reasons.items(), key=lambda item: (-item[1], item[0]))),
        "blocked_examples": blocked_examples,
    }


def _enforce_transition_maturity_from_shadow(row: dict[str, Any]) -> bool:
    """Promote only the deterministic <8-match transition veto out of shadow mode."""
    shadow_reason_codes = list(row.get("context_shadow_reason_codes") or [])
    if REASON_NEWLY_PROMOTED_RELEGATED not in shadow_reason_codes:
        return False

    shadow_diagnostics = dict(row.get("context_shadow_diagnostics") or {})
    diagnostics = dict(row.get("context_diagnostics") or {})
    row["context_status"] = "VETO"
    row["context_reason_codes"] = [REASON_NEWLY_PROMOTED_RELEGATED]
    row["context_threshold_adjustment"] = 0.0
    row["context_threshold_gate_passed"] = None
    row["context_enforced"] = True
    row["context_enforcement_scope"] = "transition_maturity"
    row["paper_entry_eligible"] = False
    row["current_decision_state"] = "NO_BET_CONTEXT_VETO"
    diagnostics["enforced_transition_maturity"] = shadow_diagnostics
    row["context_diagnostics"] = diagnostics
    return True


class LiveProductionDashboardServices(ProductionDashboardServices):
    """Keep live safeguards fresh while preserving Route B model truth."""

    @staticmethod
    def _prediction_version_key(fixture: Any, snapshot: Any) -> str:
        """Refresh persisted predictions when deterministic identity policy changes."""
        return canonical_hash(
            {
                "fixture_id": fixture.fixture_id,
                "model_snapshot_id": snapshot.snapshot_id,
                "kickoff_utc": fixture.kickoff_utc,
                "live_identity_policy_version": TRANSITION_BASELINE_VERSION,
            }
        )

    def _update_prediction_state(self, rows_with_predictions: list[dict[str, Any]]) -> None:
        """Production health describes current/future pre-match fixtures, not archive rows."""
        super()._update_prediction_state(self._future_fixture_rows(rows_with_predictions))

    async def sync_once(self, *, sync_kind: str = "manual", include_schedule: bool = True) -> dict[str, Any]:
        # Never let an empty/missing lineup result live for the whole process.
        # The shared provider budget remains the authoritative short-TTL cache.
        self._context_fixture_cache.clear()
        result = await super().sync_once(sync_kind=sync_kind, include_schedule=include_schedule)
        self._log_current_prediction_diagnostics(sync_kind=sync_kind)
        return result

    def _log_current_prediction_diagnostics(self, *, sync_kind: str) -> None:
        """Log current/future blocker truth separately from historical persisted rows."""
        try:
            persisted_rows = self._all_fixtures_with_predictions()
            future_rows = self._future_fixture_rows(persisted_rows)
            diagnostics = _summarize_prediction_rows(
                future_rows,
                eligibility_resolver=self._prediction_eligibility_status,
            )
            logger.info(
                "football current prediction diagnostics: kind=%s persisted=%s future=%s ready=%s pending=%s blocked=%s blocker_reasons=%s blocked_examples=%s",
                sync_kind,
                len(persisted_rows),
                diagnostics["fixtures"],
                diagnostics["ready"],
                diagnostics["pending"],
                diagnostics["blocked"],
                diagnostics["blocker_reasons"],
                diagnostics["blocked_examples"],
            )
        except Exception:
            # Diagnostics must never affect prediction generation or fixture sync.
            logger.exception("football current prediction diagnostics failed")

    def _production_market_evaluator(self, **kwargs: Any) -> dict[str, Any]:
        result = super()._production_market_evaluator(**kwargs)
        if self._context_mode == "shadow":
            for row in result.get("decision_rows") or []:
                shadow_status = row.get("context_shadow_status")
                if shadow_status is None:
                    continue
                shadow_reason_codes = list(row.get("context_shadow_reason_codes") or [])
                shadow_diagnostics = dict(row.get("context_shadow_diagnostics") or {})
                diagnostics = dict(row.get("context_diagnostics") or {})
                diagnostics["shadow_context"] = {
                    "status": shadow_status,
                    "reason_codes": shadow_reason_codes,
                    "threshold_adjustment": row.get("context_shadow_threshold_adjustment"),
                    "adjusted_threshold": row.get("context_shadow_adjusted_threshold"),
                    "threshold_gate_passed": row.get("context_shadow_threshold_gate_passed"),
                    "diagnostics": shadow_diagnostics,
                }
                row["context_diagnostics"] = diagnostics
                _enforce_transition_maturity_from_shadow(row)
        return result

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

        transition = transition_maturity_for_fixture(
            research_db_path=self.config.research_db_path,
            competition_id=str(fixture.get("competition_id") or ""),
            home_team_name=str(fixture.get("home_team_name") or ""),
            away_team_name=str(fixture.get("away_team_name") or ""),
        )
        if transition is not None:
            signals["promoted_or_relegated"] = True
            signals["season_matches_played"] = transition["season_matches_played"]
            signals["transition_baseline_version"] = transition["baseline_version"]
            signals["transition_teams"] = list(transition["teams"])
            signals["transition_minimum_required_matches"] = transition["minimum_required_matches"]

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

        if hours_to_kickoff <= 0:
            return signals

        # A deterministic transition-maturity veto is already sufficient.  Do
        # not spend paid injury/lineup quota on a fixture that cannot become
        # actionable until the eight-match threshold is reached.
        if transition is not None and bool(transition.get("under_maturity_threshold")):
            return signals

        lineup_window_hours = self._lineup_window_minutes / 60.0
        if self._context_mode == "enforce" and self._require_final_lineup and hours_to_kickoff > lineup_window_hours:
            signals["required_evidence"].append("final_lineup_window")
            signals["available_evidence"]["final_lineup_window"] = False
            return signals

        if hours_to_kickoff > self._context_lookahead_hours:
            return signals

        provider_fixture_id = self._provider_fixture_id_for_context(fixture)
        if provider_fixture_id is None or self._context_provider is None or not self._context_provider.configured():
            signals["required_evidence"].append("context_provider")
            signals["available_evidence"]["context_provider"] = False
            return signals

        signals["available_evidence"]["context_provider"] = True
        signals["context_provider_fixture_id"] = provider_fixture_id

        injuries = self._context_provider.get_injuries(provider_fixture_id)
        injuries_request_ok = str(injuries.get("request_state") or "") in {"OK", "CACHE_HIT"}
        signals["required_evidence"].append("injuries")
        signals["available_evidence"]["injuries"] = injuries_request_ok
        signals["injury_evidence_count"] = len(injuries.get("injuries") or [])
        if injuries_request_ok:
            signals["source_states"]["injuries"] = "FRESH"

        if hours_to_kickoff <= lineup_window_hours:
            lineups = self._context_provider.get_lineups(provider_fixture_id)
            lineups_available = str(lineups.get("evidence_state") or "") == "AVAILABLE"
            signals["required_evidence"].append("lineups")
            signals["available_evidence"]["lineups"] = lineups_available
            signals["confirmed_lineup_team_count"] = len(lineups.get("lineups") or [])
            if lineups_available:
                signals["source_states"]["lineups"] = "FRESH"
        return signals
