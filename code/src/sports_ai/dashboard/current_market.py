"""Current market evaluation helpers for the football dashboard."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Iterable, Sequence

from ..experiments.football_day11_manager_core import (
    DECISION_MISSING_REQUIRED_MARKET_DATA,
    DECISION_NO_BET,
    DECISION_PRELIMINARY_CANDIDATE,
    DECISION_REJECTED_RULE,
    DECISION_STALE_MARKET,
    FRESHNESS_STATE_FRESH,
    REASON_INCOMPLETE_REFERENCE_MARKET,
    REASON_MISSING_REFERENCE_MARKET,
    apply_day11_manager_rules,
    build_reference_market_booksum,
    canonical_market_identity,
    normalize_market_snapshot,
    validate_reference_market_rows,
)
from ..experiments.football_day12_safeguards import (
    DAY12_BLOCKED_MISSING_XG_DATA,
    DAY12_REJECTED_BY_XG_VETO,
    DAY12_REVIEW_REQUIRED,
    DEFAULT_XG_MAX_ABS_GAP_PER_TEAM,
    DEFAULT_XG_MIN_WINDOW_MATCHES,
    XG_APPLICABLE_COMPETITIONS,
    XG_STATUS_MISSING,
    XG_STATUS_NOT_APPLICABLE,
    XG_STATUS_PASS,
    XG_STATUS_REVIEW,
    XG_STATUS_VETO,
    evaluate_xg_rule,
)
from ..locking import canonical_hash
from ..market_data.providers import NormalizedMarketSnapshot
from ..market_data.the_odds_api import classify_freshness_state, resolve_market_max_age_minutes
from .day12_rolling_xg import XgRowsByRole, missing_xg_inspection
from .models import utc_now_iso

XgProfileProvider = Callable[[], tuple[XgRowsByRole, dict[str, Any]]]

CURRENT_INPUT_LABEL = "CURRENT MARKET INPUT"
CURRENT_DECISION_SCHEMA_VERSION = "football_current_market_decision_v1"

_CURRENT_REFERENCE_REASON_BY_VALIDATION = {
    None: None,
    REASON_INCOMPLETE_REFERENCE_MARKET: REASON_INCOMPLETE_REFERENCE_MARKET,
    REASON_MISSING_REFERENCE_MARKET: REASON_MISSING_REFERENCE_MARKET,
}


def _source_snapshot_version_id(snapshot: NormalizedMarketSnapshot) -> str:
    return canonical_hash(
        {
            "provider": snapshot.provider,
            "fixture_id": snapshot.fixture_id,
            "provider_fixture_id": snapshot.provider_fixture_id,
            "bookmaker": snapshot.bookmaker,
            "market": snapshot.market,
            "selection": snapshot.selection,
            "line": snapshot.line,
            "decimal_odds": snapshot.decimal_odds,
            "source_timestamp": snapshot.source_timestamp,
            "captured_at": snapshot.captured_at,
        }
    )


def current_snapshot_to_day11_row(
    snapshot: NormalizedMarketSnapshot,
    *,
    fixture_context: dict[str, Any],
) -> dict[str, Any]:
    row = normalize_market_snapshot(
        {
            "fixture_id": snapshot.fixture_id,
            "event_id": snapshot.fixture_id,
            "competition_id": str(fixture_context["competition_id"]),
            "competition_name": str(fixture_context["competition_name"]),
            "season_code": str(fixture_context["season"]),
            "division_code": str(fixture_context.get("division_code") or ""),
            "home_team": str(fixture_context["home_team_name"]),
            "away_team": str(fixture_context["away_team_name"]),
            "kickoff_at": str(fixture_context["kickoff_utc"]),
            "captured_at": snapshot.captured_at,
            "market_as_of": snapshot.source_timestamp or snapshot.captured_at,
            "source": snapshot.provider,
            "bookmaker": snapshot.bookmaker,
            "reference_designation": snapshot.reference_designation or "available",
            "market_family": snapshot.market,
            "line": snapshot.line,
            "selection": snapshot.selection,
            "decimal_odds": snapshot.decimal_odds,
            "provider_market_id": snapshot.provider_market_id
            or f"{snapshot.provider}:{snapshot.bookmaker}:{snapshot.market}:{snapshot.selection}:{snapshot.line}",
            "freshness_state": snapshot.freshness_state or "UNKNOWN",
            "input_label": CURRENT_INPUT_LABEL,
            "source_snapshot_version_id": _source_snapshot_version_id(snapshot),
        }
    )
    # Carry the raw provider update timestamp so the paper-eligibility gate can
    # recompute freshness at decision time rather than trusting capture-time state.
    row["source_timestamp"] = snapshot.source_timestamp
    return row


def _reference_market_by_bookmaker(rows: Sequence[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    reference_rows = [row for row in rows if row.get("reference_designation") == "reference"]
    if not reference_rows:
        return None, REASON_MISSING_REFERENCE_MARKET
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in reference_rows:
        rank = row.get("reference_rank")
        grouped[(999 if rank is None else int(rank), str(row["bookmaker"]))].append(row)
    for group_key in sorted(grouped):
        candidate_rows = grouped[group_key]
        validation = validate_reference_market_rows(candidate_rows)
        if validation["is_complete"]:
            return build_reference_market_booksum(candidate_rows, final_method="shin"), None
        if validation["reason"] is not None:
            return None, validation["reason"]
    return None, REASON_INCOMPLETE_REFERENCE_MARKET


def _group_current_rows(rows: Iterable[dict[str, Any]]) -> dict[tuple[str, float | None], list[dict[str, Any]]]:
    grouped: dict[tuple[str, float | None], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["market_family"]), row["normalized_line"])].append(row)
    return grouped


def _index_model_rows(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        indexed[
            canonical_market_identity(
                fixture_id=str(row["fixture_id"]),
                market_family=str(row["market_family"]),
                normalized_line=None if row.get("line") is None else float(row["line"]),
                normalized_selection=str(row["selection"]),
            )
        ] = row
    return indexed


def _best_display_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    priority = {
        "goals_total": 0,
        "1x2": 1,
        "btts": 2,
        "asian_handicap": 3,
        "corner_total": 4,
    }
    return sorted(
        rows,
        key=lambda row: (
            0 if row["decision"] == DECISION_PRELIMINARY_CANDIDATE else 1,
            priority.get(str(row["market_family"]), 99),
            0 if row.get("line") is None else abs(float(row["line"]) - 2.5),
            -float(row.get("settlement_aware_edge") or 0.0),
            str(row["selection"]),
        ),
    )[:12]


def build_fixture_market_evaluation(
    *,
    fixture_context: dict[str, Any],
    prediction: dict[str, Any],
    snapshots: Sequence[NormalizedMarketSnapshot],
    current_xg_connected: bool,
    xg_profile_provider: XgProfileProvider | None = None,
    xg_max_abs_gap_per_team: float = DEFAULT_XG_MAX_ABS_GAP_PER_TEAM,
    xg_min_window_matches: int = DEFAULT_XG_MIN_WINDOW_MATCHES,
    market_freshness_evaluated_at: str | None = None,
) -> dict[str, Any]:
    if str(prediction.get("prediction_status")) != "READY":
        return {
            "fixture_id": str(fixture_context["fixture_id"]),
            "state": "PREDICTION_NOT_READY",
            "reason": "PREDICTION_NOT_READY",
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
    if not snapshots:
        return {
            "fixture_id": str(fixture_context["fixture_id"]),
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

    current_rows = [current_snapshot_to_day11_row(snapshot, fixture_context=fixture_context) for snapshot in snapshots]
    grouped_current_rows = _group_current_rows(current_rows)
    model_rows = list(prediction.get("market_rows") or [])
    model_index = _index_model_rows(model_rows)
    fixture_competition_id = str(fixture_context.get("competition_id") or "")
    fixture_xg: tuple[XgRowsByRole, dict[str, Any]] | None = None
    if current_xg_connected and fixture_competition_id in XG_APPLICABLE_COMPETITIONS:
        if xg_profile_provider is not None:
            fixture_xg = xg_profile_provider()
        else:
            fixture_xg = ({}, missing_xg_inspection())
    decision_rows: list[dict[str, Any]] = []
    market_provider_fixture_id = next((snapshot.provider_fixture_id for snapshot in snapshots if snapshot.provider_fixture_id), None)
    for group_key, group_rows in sorted(grouped_current_rows.items()):
        reference_market, reference_reason = _reference_market_by_bookmaker(group_rows)
        by_selection: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in group_rows:
            by_selection[str(row["canonical_market_identity"])].append(row)
        for canonical_id, selection_rows in sorted(by_selection.items()):
            model_row = model_index.get(canonical_id)
            if model_row is None:
                continue
            available_offer_rows = [row for row in selection_rows if row.get("reference_designation") != "reference"]
            decision_as_of = max(
                str(model_row["as_of"]),
                max(str(row["market_as_of"]) for row in selection_rows),
            )
            if reference_market is None:
                decision_payload = {
                    "decision": DECISION_MISSING_REQUIRED_MARKET_DATA,
                    "reason_codes": [_CURRENT_REFERENCE_REASON_BY_VALIDATION.get(reference_reason) or REASON_MISSING_REFERENCE_MARKET],
                }
            else:
                decision_payload = apply_day11_manager_rules(
                    model_row=model_row,
                    reference_market=reference_market,
                    available_offer_rows=available_offer_rows,
                    decision_as_of=decision_as_of,
                )
            best_offer = decision_payload.get("best_available_offer")
            # Recompute freshness at decision time (not capture time) so a stored
            # snapshot cannot retain an obsolete FRESH classification. The raw
            # provider update timestamp of the selected best offer is authoritative.
            market_freshness = classify_freshness_state(
                source_timestamp=None if best_offer is None else best_offer.get("source_timestamp"),
                evaluated_at=market_freshness_evaluated_at or utc_now_iso(),
                threshold_minutes=resolve_market_max_age_minutes(),
                kickoff_utc=fixture_context.get("kickoff_utc"),
            )
            freshness_gate_passed = market_freshness == FRESHNESS_STATE_FRESH
            day12_state = "NOT_APPLICABLE"
            paper_entry_eligible = False
            xg_rule = None
            if decision_payload["decision"] == DECISION_PRELIMINARY_CANDIDATE:
                if fixture_competition_id not in XG_APPLICABLE_COMPETITIONS:
                    day12_state = "PASSED"
                    paper_entry_eligible = True
                elif not current_xg_connected:
                    day12_state = DAY12_BLOCKED_MISSING_XG_DATA
                else:
                    xg_rows, xg_inspection = fixture_xg or ({}, missing_xg_inspection())
                    try:
                        xg_rule = evaluate_xg_rule(
                            candidate={
                                "competition_id": str(model_row["competition_id"]),
                                "fixture_id": str(model_row["fixture_id"]),
                                "decision_as_of": decision_as_of,
                            },
                            fixture_support=_live_fixture_support(prediction),
                            xg_rows_by_fixture_role=xg_rows,
                            xg_input_inspection=xg_inspection,
                            max_abs_gap_per_team=xg_max_abs_gap_per_team,
                            min_window_matches=xg_min_window_matches,
                        )
                    except Exception:
                        xg_rule = {
                            "status": XG_STATUS_MISSING,
                            "reason_codes": ["XG_EVALUATION_FAILED"],
                            "required": True,
                        }
                    day12_state = _day12_state_from_xg_rule(xg_rule)
                    paper_entry_eligible = xg_rule["status"] == XG_STATUS_PASS
                # Market freshness is an explicit market-quality gate: a paper entry
                # may only ever be created against a FRESH market. STALE or UNKNOWN
                # market data fails closed regardless of Day 12 outcome.
                if not freshness_gate_passed:
                    paper_entry_eligible = False
            decision_rows.append(
                {
                    "current_decision_schema_version": CURRENT_DECISION_SCHEMA_VERSION,
                    "fixture_id": str(model_row["fixture_id"]),
                    "competition_id": str(model_row["competition_id"]),
                    "competition_name": str(model_row["competition_name"]),
                    "decision_id": canonical_hash(
                        {
                            "fixture_id": model_row["fixture_id"],
                            "market_family": model_row["market_family"],
                            "line": model_row["line"],
                            "selection": model_row["selection"],
                            "market_as_of": decision_as_of,
                        }
                    ),
                    "market_family": str(model_row["market_family"]),
                    "line": None if model_row.get("line") is None else float(model_row["line"]),
                    "selection": str(model_row["selection"]),
                    "model_probability": float(model_row["effective_win_stake_probability"]),
                    "model_fair_decimal_price": float(model_row["fair_decimal_price"]),
                    "effective_win_stake_probability": float(model_row["effective_win_stake_probability"]),
                    "effective_loss_stake_probability": float(model_row["effective_loss_stake_probability"]),
                    "best_available_price": None if best_offer is None else float(best_offer["decimal_odds"]),
                    "best_available_bookmaker": None if best_offer is None else str(best_offer["bookmaker"]),
                    "best_available_market_snapshot_id": None if best_offer is None else str(best_offer["market_snapshot_id"]),
                    "best_available_provider_market_id": None if best_offer is None else str(best_offer["provider_market_id"]),
                    "best_available_source_timestamp": None if best_offer is None else str(best_offer["market_as_of"]),
                    "minimum_acceptable_price": decision_payload.get("minimum_acceptable_price"),
                    "settlement_aware_edge": decision_payload.get("settlement_aware_edge"),
                    "decision": str(decision_payload["decision"]),
                    "reason_codes": list(decision_payload["reason_codes"]),
                    "market_as_of": decision_as_of,
                    "freshness_state": None if best_offer is None else best_offer.get("freshness_state"),
                    "provider_fixture_id": market_provider_fixture_id,
                    "source_scoreline_object_id": model_row.get("source_scoreline_object_id"),
                    "source_corner_object_id": model_row.get("source_corner_object_id"),
                    "model_versions": dict(model_row.get("model_versions") or {}),
                    "pricing_model_version": model_row.get("pricing_model_version"),
                    "settlement_engine_version": model_row.get("settlement_engine_version"),
                    "day12_state": day12_state,
                    "paper_entry_eligible": paper_entry_eligible,
                    "market_freshness": market_freshness,
                    "freshness_gate_passed": freshness_gate_passed,
                    "xg_status": None if xg_rule is None else xg_rule.get("status"),
                    "xg_reason_codes": [] if xg_rule is None else list(xg_rule.get("reason_codes") or []),
                    "xg_home_recent_for_per_match": None
                    if xg_rule is None
                    else xg_rule.get("home_recent_xg_for_per_match"),
                    "xg_away_recent_for_per_match": None
                    if xg_rule is None
                    else xg_rule.get("away_recent_xg_for_per_match"),
                }
            )

    counts = {
        "predictions_evaluated": len(decision_rows),
        "preliminary_candidates": sum(1 for row in decision_rows if row["decision"] == DECISION_PRELIMINARY_CANDIDATE),
        "no_bets": sum(1 for row in decision_rows if row["decision"] == DECISION_NO_BET),
        "blocked": sum(
            1
            for row in decision_rows
            if row["decision"] in {DECISION_MISSING_REQUIRED_MARKET_DATA, DECISION_REJECTED_RULE, DECISION_STALE_MARKET}
            or row["day12_state"] == DAY12_BLOCKED_MISSING_XG_DATA
            or (row["decision"] == DECISION_PRELIMINARY_CANDIDATE and not row["freshness_gate_passed"])
        ),
        "passed_safeguards": sum(1 for row in decision_rows if row["paper_entry_eligible"]),
    }
    freshness_values = {str(snapshot.freshness_state or "UNKNOWN") for snapshot in snapshots}
    return {
        "fixture_id": str(fixture_context["fixture_id"]),
        "state": "READY",
        "reason": None,
        "provider_fixture_id": market_provider_fixture_id,
        "captured_at": max(snapshot.captured_at for snapshot in snapshots),
        "market_snapshot_count": len(snapshots),
        "covered_markets": sorted({snapshot.market for snapshot in snapshots}),
        "freshness": "FRESH" if freshness_values == {"FRESH"} else ("STALE" if freshness_values == {"STALE"} else "UNKNOWN"),
        "decision_rows": decision_rows,
        "display_rows": _best_display_rows(decision_rows),
        "counts": counts,
    }


def _live_fixture_support(prediction: dict[str, Any]) -> dict[str, Any] | None:
    """Build the minimal Day 12 fixture_support from a live prediction."""
    expected_home = prediction.get("expected_home_goals", prediction.get("expected_goals_home"))
    expected_away = prediction.get("expected_away_goals", prediction.get("expected_goals_away"))
    if expected_home is None or expected_away is None:
        return None
    return {
        "expected_goals_home": float(expected_home),
        "expected_goals_away": float(expected_away),
    }


def _day12_state_from_xg_rule(xg_rule: dict[str, Any]) -> str:
    status = str(xg_rule.get("status") or "")
    if status == XG_STATUS_PASS:
        return "PASSED"
    if status == XG_STATUS_VETO:
        return DAY12_REJECTED_BY_XG_VETO
    if status == XG_STATUS_REVIEW:
        return DAY12_REVIEW_REQUIRED
    if status == XG_STATUS_MISSING:
        return DAY12_BLOCKED_MISSING_XG_DATA
    if status == XG_STATUS_NOT_APPLICABLE:
        return "NOT_APPLICABLE"
    return DAY12_BLOCKED_MISSING_XG_DATA
