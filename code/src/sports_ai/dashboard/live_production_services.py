"""Final live scheduler policy layered over ProductionDashboardServices."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..storage import football_candidate_evidence, football_paper_trading
from .models import parse_iso_datetime
from .production_services import ProductionDashboardServices


class LiveProductionDashboardServices(ProductionDashboardServices):
    """Keep context evidence fresh without over-polling lineup endpoints."""

    async def sync_once(self, *, sync_kind: str = "manual", include_schedule: bool = True) -> dict[str, Any]:
        # Never let an empty/missing lineup result live for the whole process.
        # The shared provider budget remains the authoritative short-TTL cache.
        self._context_fixture_cache.clear()
        return await super().sync_once(sync_kind=sync_kind, include_schedule=include_schedule)

    def _production_market_evaluator(self, **kwargs: Any) -> dict[str, Any]:
        result = super()._production_market_evaluator(**kwargs)
        if self._context_mode == "shadow":
            for row in result.get("decision_rows") or []:
                shadow_status = row.get("context_shadow_status")
                if shadow_status is None:
                    continue
                diagnostics = dict(row.get("context_diagnostics") or {})
                diagnostics["shadow_context"] = {
                    "status": shadow_status,
                    "reason_codes": list(row.get("context_shadow_reason_codes") or []),
                    "threshold_adjustment": row.get("context_shadow_threshold_adjustment"),
                    "adjusted_threshold": row.get("context_shadow_adjusted_threshold"),
                    "threshold_gate_passed": row.get("context_shadow_threshold_gate_passed"),
                    "diagnostics": dict(row.get("context_shadow_diagnostics") or {}),
                }
                row["context_diagnostics"] = diagnostics
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
