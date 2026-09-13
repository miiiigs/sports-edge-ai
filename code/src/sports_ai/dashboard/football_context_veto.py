"""Deterministic kill-only external-context safeguards for Football Route B.

The context layer sits downstream of model pricing and manager qualification. It
may veto, block, or flag a candidate, but it must never create a bet, increase a
model probability, or override a NO_BET decision.

The rules in this module intentionally accept normalized point-in-time evidence
rather than fetching provider data themselves. Provider adapters can therefore
be quota-governed and tested independently while this policy remains pure and
replayable.
"""

from __future__ import annotations

from typing import Any, Mapping

CONTEXT_VETO_VERSION = "football_context_veto_v1"

CONTEXT_PASS = "PASS"
CONTEXT_FLAG = "FLAG"
CONTEXT_BLOCKED = "BLOCKED"
CONTEXT_VETO = "VETO"

DEFAULT_ADVERSE_ODDS_MOVE_THRESHOLD = 0.15
DEFAULT_CONGESTION_THRESHOLD_ADJUSTMENT = 0.01
DEFAULT_EUROPE_CONGESTION_WINDOW_HOURS = 96.0

REASON_STALE_SOURCE = "STALE_SOURCE"
REASON_REQUIRED_EVIDENCE_MISSING = "REQUIRED_EVIDENCE_MISSING"
REASON_TWO_EXPECTED_STARTERS_MISSING = "TWO_OR_MORE_EXPECTED_STARTERS_MISSING"
REASON_KEY_ATTACKER_RUled_OUT = "KEY_ATTACKER_RULED_OUT_WITHIN_24H"
REASON_NEWLY_PROMOTED_RELEGATED = "PROMOTED_RELEGATED_UNDER_8_MATCHES"
REASON_RECENT_MANAGER_CHANGE = "MANAGER_CHANGE_WITHIN_3_MATCHES"
REASON_EUROPE_CONGESTION = "EUROPEAN_FIXTURE_WITHIN_4_DAYS"
REASON_UNRATED_CUP_OPPONENT = "CUP_TIE_UNRATED_OPPONENT"
REASON_XG_DIVERGENCE = "XG_DIVERGENCE_FAILED"
REASON_ADVERSE_ODDS_MOVE = "ADVERSE_ODDS_MOVE_OVER_15_PERCENT"
REASON_DEAD_RUBBER = "DEAD_RUBBER_BOTH_SIDES"


def _safe_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and parsed not in {float("inf"), float("-inf")} else None


def _safe_int(value: Any) -> int | None:
    parsed = _safe_float(value)
    return None if parsed is None else int(parsed)


def adverse_odds_move(
    *,
    baseline_odds: Any,
    current_odds: Any,
    threshold: float = DEFAULT_ADVERSE_ODDS_MOVE_THRESHOLD,
) -> dict[str, Any]:
    """Measure adverse decimal-price movement since the decision baseline.

    A higher decimal price means the market now assigns lower implied
    probability to the selected outcome. The controlling Football spec states
    that an odds move greater than 15% against the selection is a veto, so this
    function applies that rule directly to decimal-price movement.
    """
    baseline = _safe_float(baseline_odds)
    current = _safe_float(current_odds)
    if baseline is None or current is None or baseline <= 1.0 or current <= 1.0:
        return {
            "available": False,
            "baseline_odds": baseline,
            "current_odds": current,
            "price_move_fraction": None,
            "implied_probability_change_fraction": None,
            "threshold": float(threshold),
            "veto": False,
        }

    price_move = (current - baseline) / baseline
    baseline_implied = 1.0 / baseline
    current_implied = 1.0 / current
    implied_probability_change = (current_implied - baseline_implied) / baseline_implied
    return {
        "available": True,
        "baseline_odds": baseline,
        "current_odds": current,
        "price_move_fraction": price_move,
        "implied_probability_change_fraction": implied_probability_change,
        "threshold": float(threshold),
        "veto": price_move > float(threshold),
    }


def _minimum_absolute_hours(signals: Mapping[str, Any]) -> float | None:
    values: list[float] = []
    for key in (
        "hours_to_european_fixture",
        "hours_since_european_fixture",
        "hours_from_european_fixture",
    ):
        value = _safe_float(signals.get(key))
        if value is not None:
            values.append(abs(value))
    return min(values) if values else None


def evaluate_football_context_veto(
    signals: Mapping[str, Any] | None,
    *,
    adverse_odds_threshold: float = DEFAULT_ADVERSE_ODDS_MOVE_THRESHOLD,
    strict_required_evidence: bool = False,
) -> dict[str, Any]:
    """Evaluate normalized context evidence without changing model output.

    Expected signal keys are deliberately simple and serializable. Missing
    optional evidence never becomes an implicit PASS. Callers that require a
    particular evidence set can supply ``required_evidence`` plus
    ``available_evidence`` and enable ``strict_required_evidence``; missing
    required evidence then produces BLOCKED rather than a fabricated veto.
    """
    normalized: Mapping[str, Any] = signals or {}
    veto_reasons: list[str] = []
    block_reasons: list[str] = []
    flag_reasons: list[str] = []
    diagnostics: dict[str, Any] = {}

    source_states = normalized.get("source_states") or {}
    stale_sources = sorted(
        str(source)
        for source, state in source_states.items()
        if str(state or "").strip().upper() == "STALE"
    )
    diagnostics["stale_sources"] = stale_sources
    if stale_sources:
        veto_reasons.append(REASON_STALE_SOURCE)

    if strict_required_evidence:
        required_evidence = [str(value) for value in (normalized.get("required_evidence") or [])]
        available_evidence = normalized.get("available_evidence") or {}
        missing_evidence = sorted(
            evidence
            for evidence in required_evidence
            if not bool(available_evidence.get(evidence))
        )
        diagnostics["missing_required_evidence"] = missing_evidence
        if missing_evidence:
            block_reasons.append(REASON_REQUIRED_EVIDENCE_MISSING)
    else:
        diagnostics["missing_required_evidence"] = []

    missing_expected_starters = _safe_int(normalized.get("missing_expected_starters"))
    diagnostics["missing_expected_starters"] = missing_expected_starters
    if missing_expected_starters is not None and missing_expected_starters >= 2:
        veto_reasons.append(REASON_TWO_EXPECTED_STARTERS_MISSING)

    if bool(normalized.get("key_attacker_ruled_out_within_24h")):
        veto_reasons.append(REASON_KEY_ATTACKER_RUled_OUT)

    promoted_or_relegated = bool(normalized.get("promoted_or_relegated"))
    season_matches = _safe_int(normalized.get("season_matches_played"))
    diagnostics["season_matches_played"] = season_matches
    diagnostics["transition_baseline_version"] = normalized.get("transition_baseline_version")
    diagnostics["transition_minimum_required_matches"] = _safe_int(
        normalized.get("transition_minimum_required_matches")
    )
    diagnostics["transition_teams"] = list(normalized.get("transition_teams") or [])
    if promoted_or_relegated and season_matches is not None and season_matches < 8:
        veto_reasons.append(REASON_NEWLY_PROMOTED_RELEGATED)

    manager_change_matches_ago = _safe_int(normalized.get("manager_change_matches_ago"))
    diagnostics["manager_change_matches_ago"] = manager_change_matches_ago
    if manager_change_matches_ago is not None and 0 <= manager_change_matches_ago <= 3:
        veto_reasons.append(REASON_RECENT_MANAGER_CHANGE)

    europe_hours = _minimum_absolute_hours(normalized)
    diagnostics["european_fixture_distance_hours"] = europe_hours
    if europe_hours is not None and europe_hours <= DEFAULT_EUROPE_CONGESTION_WINDOW_HOURS:
        flag_reasons.append(REASON_EUROPE_CONGESTION)

    if bool(normalized.get("cup_tie")) and normalized.get("opponent_rated") is False:
        veto_reasons.append(REASON_UNRATED_CUP_OPPONENT)

    if bool(normalized.get("xg_divergence_failed")):
        veto_reasons.append(REASON_XG_DIVERGENCE)

    odds_move = adverse_odds_move(
        baseline_odds=normalized.get("baseline_odds"),
        current_odds=normalized.get("current_odds"),
        threshold=adverse_odds_threshold,
    )
    diagnostics["adverse_odds_move"] = odds_move
    if odds_move["veto"]:
        veto_reasons.append(REASON_ADVERSE_ODDS_MOVE)

    if bool(normalized.get("dead_rubber_both_sides")):
        veto_reasons.append(REASON_DEAD_RUBBER)

    # Stable ordering and de-duplication keep replay/audit payloads deterministic.
    veto_reasons = list(dict.fromkeys(veto_reasons))
    block_reasons = list(dict.fromkeys(block_reasons))
    flag_reasons = list(dict.fromkeys(flag_reasons))

    if veto_reasons:
        status = CONTEXT_VETO
    elif block_reasons:
        status = CONTEXT_BLOCKED
    elif flag_reasons:
        status = CONTEXT_FLAG
    else:
        status = CONTEXT_PASS

    threshold_adjustment = (
        DEFAULT_CONGESTION_THRESHOLD_ADJUSTMENT
        if REASON_EUROPE_CONGESTION in flag_reasons
        else 0.0
    )
    reason_codes = [*veto_reasons, *block_reasons, *flag_reasons]
    return {
        "version": CONTEXT_VETO_VERSION,
        "status": status,
        "kill_only": True,
        "bet_creation_allowed": False,
        "probability_adjustment": 0.0,
        "threshold_adjustment": threshold_adjustment,
        "reason_codes": reason_codes,
        "veto_reasons": veto_reasons,
        "block_reasons": block_reasons,
        "flag_reasons": flag_reasons,
        "diagnostics": diagnostics,
    }
