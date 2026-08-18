"""Day 10 football market pricing derived from frozen Day 8 and Day 9 model objects.

This module consumes the persisted Day 9 scoreline matrix and the Day 8 corner-distribution
selection, then derives fair prices for the bounded Day 10 market set:

- `1X2`
- `BTTS`
- goals totals across integer / half / quarter lines
- Asian handicap across integer / half / quarter lines
- corner totals across integer / half / quarter lines

The Day 10 layer remains model-only:

- no sportsbook odds
- no de-vig
- no edge or threshold logic
- no staking or recommendations
- no mutation of Day 8, Day 9, or GAP state
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from ..locking import canonical_hash
from .football_day8_goal_conversion import (
    CONVERSION_MODEL_VERSION,
    CORNER_COMPARISON_VERSION,
    DAY7_BASELINE_EXPERIMENT_VERSION,
    EXPERIMENT_VERSION as DAY8_EXPERIMENT_VERSION,
)
from .football_day9_scoreline_model import (
    EXPERIMENT_VERSION as DAY9_EXPERIMENT_VERSION,
    SCORELINE_MODEL_VERSION,
    run_day9_scoreline_experiment,
)

EXPERIMENT_ID = "EXP-013"
EXPERIMENT_VERSION = "football_day10_market_pricing_v1"
PRICING_MODEL_VERSION = "football_day10_market_pricing_v1"
SETTLEMENT_ENGINE_VERSION = "football_day10_settlement_engine_v1"
OUTPUT_SCHEMA_VERSION = "football_day10_market_price_record_v1"
DAY10_INPUT_CONTRACT_VERSION = "football_day10_sanitized_prematch_input_v1"
DAY9_TO_DAY10_INTERFACE_POLICY_ID = "persisted_day9_scoreline_matrix_is_canonical_goals_object_v1"
DAY9_TO_DAY10_INTERFACE_POLICY_DESCRIPTION = (
    "Day 10 uses the persisted Day 9 scoreline matrix directly as the single canonical goals "
    "probability object. It does not recompute Poisson probabilities from expected goals or rho."
)
DAY8_TO_DAY10_CORNER_INTERFACE_POLICY_ID = "persisted_day8_corner_distribution_is_canonical_v1"
DAY8_TO_DAY10_CORNER_INTERFACE_POLICY_DESCRIPTION = (
    "Day 10 uses the Day 8 predicted corner means, selected corner distribution family, and "
    "persisted dispersion parameter directly as the canonical corners object."
)
SANITIZED_INPUT_POLICY_ID = "prematch_only_pricing_input_excludes_outcomes_and_market_data_v1"
SANITIZED_INPUT_POLICY_DESCRIPTION = (
    "Construct a Day 10 pricing input from pre-match model content only. Exclude realized outcomes, "
    "sportsbook odds, close prices, ROI, edges, thresholds, and decision fields."
)
ASIAN_HANDICAP_LINE_CONVENTION = "home_handicap"
SETTLEMENT_PRICING_POLICY_ID = "zero_ev_with_partial_and_push_settlement_v1"
SETTLEMENT_PRICING_POLICY_DESCRIPTION = (
    "Fair decimal price is 1 + L/W, where W = P(full_win) + 0.5*P(half_win) and "
    "L = P(full_loss) + 0.5*P(half_loss)."
)
CORNER_SUPPORT_POLICY_ID = "adaptive_support_measure_tail_then_renormalize_v1"
CORNER_SUPPORT_TAIL_TOLERANCE = 1e-12
CORNER_SUPPORT_MAX = 200
LINE_CATALOG_VERSION = "football_day10_engineering_verification_lines_v1"
GOALS_TOTAL_LINES: tuple[float, ...] = (1.5, 2.0, 2.25, 2.5, 2.75, 3.0)
ASIAN_HANDICAP_LINES: tuple[float, ...] = (-1.0, -0.75, -0.5, -0.25, 0.0, 0.25, 0.5, 0.75, 1.0)
CORNER_TOTAL_LINES: tuple[float, ...] = (8.5, 9.0, 9.25, 9.5, 9.75, 10.0, 10.5)
_SETTLEMENT_ORDER = ("full_win", "half_win", "push", "half_loss", "full_loss")
_SETTLEMENT_UNIT_RETURN = {
    "full_win": 1.0,
    "half_win": 0.5,
    "push": 0.0,
    "half_loss": -0.5,
    "full_loss": -1.0,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _kickoff_at(match_date: str, kickoff_time: str | None) -> str:
    return f"{match_date}T{kickoff_time or '00:00'}:00"


def _normalize_matrix(matrix: Sequence[Sequence[Any]]) -> list[list[float]]:
    normalized: list[list[float]] = []
    expected_width: int | None = None
    for row in matrix:
        normalized_row = [float(value) for value in row]
        if expected_width is None:
            expected_width = len(normalized_row)
        elif len(normalized_row) != expected_width:
            raise ValueError("scoreline matrix rows must have equal length")
        normalized.append(normalized_row)
    if not normalized or expected_width in {None, 0} or len(normalized) != expected_width:
        raise ValueError("scoreline matrix must be a non-empty square matrix")
    return normalized


def _line_to_decimal(line: float | int | str | Decimal) -> Decimal:
    try:
        decimal_line = line if isinstance(line, Decimal) else Decimal(str(line))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid line {line!r}") from exc
    quarter_units = decimal_line * Decimal("4")
    if quarter_units != quarter_units.to_integral_value():
        raise ValueError("line must be an integer, half, or quarter step")
    return decimal_line


def _component_lines(line: float | int | str | Decimal) -> list[Decimal]:
    decimal_line = _line_to_decimal(line)
    quarter_units = int(decimal_line * Decimal("4"))
    if abs(quarter_units) % 2 == 1:
        return [
            Decimal(quarter_units - 1) / Decimal("4"),
            Decimal(quarter_units + 1) / Decimal("4"),
        ]
    return [decimal_line]


def _component_result(adjusted_margin: float, *, epsilon: float = 1e-12) -> str:
    if adjusted_margin > epsilon:
        return "full_win"
    if adjusted_margin < -epsilon:
        return "full_loss"
    return "push"


def _aggregate_component_results(component_results: Sequence[str]) -> str:
    mean_unit_return = sum(_SETTLEMENT_UNIT_RETURN[result] for result in component_results) / len(component_results)
    if mean_unit_return == 1.0:
        return "full_win"
    if mean_unit_return == 0.5:
        return "half_win"
    if mean_unit_return == 0.0:
        return "push"
    if mean_unit_return == -0.5:
        return "half_loss"
    if mean_unit_return == -1.0:
        return "full_loss"
    raise ValueError(f"unsupported split-stake unit return {mean_unit_return!r}")


def fair_decimal_price_from_settlement(
    *,
    full_win_probability: float,
    half_win_probability: float = 0.0,
    push_probability: float = 0.0,
    half_loss_probability: float = 0.0,
    full_loss_probability: float = 0.0,
) -> float:
    win_stake = float(full_win_probability) + (0.5 * float(half_win_probability))
    loss_stake = float(full_loss_probability) + (0.5 * float(half_loss_probability))
    if win_stake <= 0.0:
        return math.inf
    return 1.0 + (loss_stake / win_stake)


def _complete_settlement_summary(bucket_probabilities: dict[str, float]) -> dict[str, float]:
    total_probability = sum(bucket_probabilities[bucket] for bucket in _SETTLEMENT_ORDER)
    if not math.isclose(total_probability, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError(f"settlement probability mass must equal 1, got {total_probability}")
    summary = {
        f"{bucket}_probability": float(bucket_probabilities[bucket])
        for bucket in _SETTLEMENT_ORDER
    }
    summary["effective_win_stake_probability"] = (
        summary["full_win_probability"] + (0.5 * summary["half_win_probability"])
    )
    summary["effective_loss_stake_probability"] = (
        summary["full_loss_probability"] + (0.5 * summary["half_loss_probability"])
    )
    summary["fair_decimal_price"] = fair_decimal_price_from_settlement(
        full_win_probability=summary["full_win_probability"],
        half_win_probability=summary["half_win_probability"],
        push_probability=summary["push_probability"],
        half_loss_probability=summary["half_loss_probability"],
        full_loss_probability=summary["full_loss_probability"],
    )
    return summary


def _settlement_summary_from_states(
    *,
    state_probabilities: Iterable[tuple[Any, float]],
    component_margin: Callable[[Any, Decimal], float],
    line: float | int | str | Decimal,
) -> dict[str, float]:
    components = _component_lines(line)
    bucket_probabilities = {bucket: 0.0 for bucket in _SETTLEMENT_ORDER}
    for state, probability in state_probabilities:
        probability_value = float(probability)
        if probability_value < 0.0:
            raise ValueError("state probability must be non-negative")
        component_results = [
            _component_result(component_margin(state, component_line))
            for component_line in components
        ]
        bucket_probabilities[_aggregate_component_results(component_results)] += probability_value
    summary = _complete_settlement_summary(bucket_probabilities)
    summary["component_lines"] = [float(component) for component in components]
    return summary


def _poisson_pmf(k: int, mean_value: float) -> float:
    if mean_value < 0.0:
        raise ValueError("Poisson mean must be non-negative")
    if mean_value == 0.0:
        return 1.0 if k == 0 else 0.0
    return math.exp((k * math.log(mean_value)) - mean_value - math.lgamma(k + 1.0))


def _negative_binomial_pmf(k: int, mean_value: float, dispersion: float | None) -> float:
    if dispersion is None:
        raise ValueError("negative-binomial pricing requires a persisted dispersion parameter")
    if dispersion <= 0.0:
        raise ValueError("negative-binomial dispersion must be positive")
    if mean_value < 0.0:
        raise ValueError("negative-binomial mean must be non-negative")
    if mean_value == 0.0:
        return 1.0 if k == 0 else 0.0
    r = float(dispersion)
    p = r / (r + mean_value)
    return math.exp(
        math.lgamma(k + r)
        - math.lgamma(r)
        - math.lgamma(k + 1.0)
        + (r * math.log(p))
        + (k * math.log(1.0 - p))
    )


def build_corner_total_distribution(
    *,
    mean_value: float,
    distribution_name: str,
    dispersion: float | None,
    tail_tolerance: float = CORNER_SUPPORT_TAIL_TOLERANCE,
    support_max: int = CORNER_SUPPORT_MAX,
) -> dict[str, Any]:
    if tail_tolerance <= 0.0:
        raise ValueError("tail tolerance must be positive")
    if support_max <= 0:
        raise ValueError("support max must be positive")

    if distribution_name == "poisson":
        pmf = lambda count: _poisson_pmf(count, mean_value)
    elif distribution_name == "negative_binomial":
        pmf = lambda count: _negative_binomial_pmf(count, mean_value, dispersion)
    else:
        raise ValueError(f"unsupported corner distribution {distribution_name!r}")

    support_probabilities: dict[int, float] = {}
    cumulative_mass = 0.0
    count = 0
    while count <= support_max:
        probability = float(pmf(count))
        support_probabilities[count] = probability
        cumulative_mass += probability
        residual_tail_mass = max(0.0, 1.0 - cumulative_mass)
        if residual_tail_mass <= tail_tolerance:
            break
        count += 1

    residual_tail_mass = max(0.0, 1.0 - cumulative_mass)
    if residual_tail_mass > tail_tolerance:
        raise ValueError(
            f"corner support tail mass {residual_tail_mass} exceeds tolerance {tail_tolerance} at {support_max}"
        )
    if cumulative_mass <= 0.0:
        raise ValueError("corner distribution support mass collapsed to zero")

    normalization_factor = 1.0 / cumulative_mass
    normalized_support = {
        support_count: probability * normalization_factor
        for support_count, probability in sorted(support_probabilities.items())
    }
    return {
        "distribution_name": distribution_name,
        "mean_value": float(mean_value),
        "dispersion": None if dispersion is None else float(dispersion),
        "support_probabilities": normalized_support,
        "support_max": max(normalized_support),
        "raw_support_mass": cumulative_mass,
        "residual_tail_mass": residual_tail_mass,
        "tail_tolerance": tail_tolerance,
        "normalization_factor": normalization_factor,
        "normalization_error": abs(1.0 - sum(normalized_support.values())),
        "support_policy_id": CORNER_SUPPORT_POLICY_ID,
    }


def build_day10_pricing_input(day9_row: dict[str, Any]) -> dict[str, Any]:
    kickoff_at = _kickoff_at(str(day9_row["match_date"]), day9_row.get("kickoff_time"))
    as_of = str(day9_row["as_of"])
    if as_of > kickoff_at:
        raise ValueError(f"as_of {as_of!r} must not be later than kickoff {kickoff_at!r}")

    matrix = _normalize_matrix(day9_row["scoreline_matrix"])
    scoreline_object_content = {
        "event_id": str(day9_row["event_id"]),
        "competition_id": str(day9_row["competition_id"]),
        "kickoff_at": kickoff_at,
        "as_of": as_of,
        "scoreline_model_version": str(day9_row["scoreline_model_version"]),
        "conversion_model_version": str(day9_row["conversion_model_version"]),
        "gap_model_version_shots": str(day9_row["gap_model_version_shots"]),
        "gap_model_version_shots_on_target": str(day9_row["gap_model_version_shots_on_target"]),
        "expected_goals_home": float(day9_row["expected_goals_home"]),
        "expected_goals_away": float(day9_row["expected_goals_away"]),
        "expected_total_goals": float(day9_row["expected_total_goals"]),
        "rho": float(day9_row["rho"]),
        "xi": float(day9_row["xi"]),
        "matrix_max_goals": int(day9_row["scoreline_matrix_max_goals"]),
        "matrix": matrix,
        "tail_mass_beyond_max_goals": float(day9_row["truncated_tail_mass_beyond_10"]),
        "raw_matrix_mass": float(day9_row["scoreline_matrix_raw_mass"]),
        "normalization_factor": float(day9_row["scoreline_matrix_normalization_factor"]),
        "normalization_error": float(day9_row["scoreline_matrix_normalization_error"]),
        "full_distribution_mass_estimate": float(day9_row["full_distribution_mass_estimate"]),
        "full_distribution_mass_estimate_error": float(day9_row["full_distribution_mass_estimate_error"]),
        "marginal_home_mean": float(day9_row["matrix_marginal_home_mean"]),
        "marginal_away_mean": float(day9_row["matrix_marginal_away_mean"]),
        "interface_policy_id": DAY9_TO_DAY10_INTERFACE_POLICY_ID,
    }
    scoreline_object_id = f"scoreline_{canonical_hash(scoreline_object_content)}"

    corner_dispersion = day9_row["corner_distribution_parameters"].get("dispersion")
    corner_object_content = {
        "event_id": str(day9_row["event_id"]),
        "competition_id": str(day9_row["competition_id"]),
        "kickoff_at": kickoff_at,
        "as_of": as_of,
        "gap_model_version_corners": str(day9_row["gap_model_version_corners"]),
        "conversion_model_version": str(day9_row["conversion_model_version"]),
        "selected_corner_distribution": str(day9_row["selected_corner_distribution"]),
        "predicted_home_corners": float(day9_row["predicted_home_corners"]),
        "predicted_away_corners": float(day9_row["predicted_away_corners"]),
        "predicted_total_corners": float(day9_row["predicted_home_corners"])
        + float(day9_row["predicted_away_corners"]),
        "dispersion": None if corner_dispersion is None else float(corner_dispersion),
        "interface_policy_id": DAY8_TO_DAY10_CORNER_INTERFACE_POLICY_ID,
    }
    corner_object_id = f"corner_{canonical_hash(corner_object_content)}"

    return {
        "fixture_id": str(day9_row["event_id"]),
        "event_id": str(day9_row["event_id"]),
        "competition_id": str(day9_row["competition_id"]),
        "competition_name": str(day9_row["competition_name"]),
        "season_code": str(day9_row["season_code"]),
        "division_code": str(day9_row["division_code"]),
        "match_date": str(day9_row["match_date"]),
        "kickoff_time": None if day9_row.get("kickoff_time") is None else str(day9_row["kickoff_time"]),
        "kickoff_at": kickoff_at,
        "as_of": as_of,
        "day10_input_contract_version": DAY10_INPUT_CONTRACT_VERSION,
        "sanitized_input_policy_id": SANITIZED_INPUT_POLICY_ID,
        "model_versions": {
            "day7_gap_baseline_experiment_version": DAY7_BASELINE_EXPERIMENT_VERSION,
            "day8_experiment_version": DAY8_EXPERIMENT_VERSION,
            "day9_experiment_version": DAY9_EXPERIMENT_VERSION,
            "conversion_model_version": str(day9_row["conversion_model_version"]),
            "scoreline_model_version": str(day9_row["scoreline_model_version"]),
            "gap_model_version_shots": str(day9_row["gap_model_version_shots"]),
            "gap_model_version_shots_on_target": str(day9_row["gap_model_version_shots_on_target"]),
            "gap_model_version_corners": str(day9_row["gap_model_version_corners"]),
            "corner_comparison_version": CORNER_COMPARISON_VERSION,
        },
        "scoreline_object": {
            "object_id": scoreline_object_id,
            **scoreline_object_content,
        },
        "corner_object": {
            "object_id": corner_object_id,
            **corner_object_content,
        },
    }


def _scoreline_state_probabilities(matrix: Sequence[Sequence[float]]) -> list[tuple[tuple[int, int], float]]:
    return [
        ((home_goals, away_goals), float(probability))
        for home_goals, row in enumerate(matrix)
        for away_goals, probability in enumerate(row)
    ]


def _binary_market_record(
    *,
    pricing_input: dict[str, Any],
    market_family: str,
    selection: str,
    probability: float,
    source_scoreline_object_id: str,
) -> dict[str, Any]:
    settlement_summary = _complete_settlement_summary(
        {
            "full_win": float(probability),
            "half_win": 0.0,
            "push": 0.0,
            "half_loss": 0.0,
            "full_loss": max(0.0, 1.0 - float(probability)),
        }
    )
    return _market_record(
        pricing_input=pricing_input,
        market_family=market_family,
        selection=selection,
        line=None,
        settlement_summary=settlement_summary,
        source_scoreline_object_id=source_scoreline_object_id,
        source_corner_object_id=None,
        numerical_metadata={
            "probability_mass_check": settlement_summary["full_win_probability"]
            + settlement_summary["full_loss_probability"],
        },
    )


def derive_1x2_market(pricing_input: dict[str, Any]) -> list[dict[str, Any]]:
    matrix = pricing_input["scoreline_object"]["matrix"]
    home_probability = 0.0
    draw_probability = 0.0
    away_probability = 0.0
    for home_goals, row in enumerate(matrix):
        for away_goals, probability in enumerate(row):
            if home_goals > away_goals:
                home_probability += probability
            elif home_goals == away_goals:
                draw_probability += probability
            else:
                away_probability += probability
    total_probability = home_probability + draw_probability + away_probability
    if not math.isclose(total_probability, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError(f"1X2 probability mass must equal 1, got {total_probability}")

    source_scoreline_object_id = pricing_input["scoreline_object"]["object_id"]
    return [
        _binary_market_record(
            pricing_input=pricing_input,
            market_family="1x2",
            selection="HOME",
            probability=home_probability,
            source_scoreline_object_id=source_scoreline_object_id,
        ),
        _binary_market_record(
            pricing_input=pricing_input,
            market_family="1x2",
            selection="DRAW",
            probability=draw_probability,
            source_scoreline_object_id=source_scoreline_object_id,
        ),
        _binary_market_record(
            pricing_input=pricing_input,
            market_family="1x2",
            selection="AWAY",
            probability=away_probability,
            source_scoreline_object_id=source_scoreline_object_id,
        ),
    ]


def derive_btts_market(pricing_input: dict[str, Any]) -> list[dict[str, Any]]:
    matrix = pricing_input["scoreline_object"]["matrix"]
    yes_probability = 0.0
    for home_goals, row in enumerate(matrix):
        if home_goals == 0:
            continue
        for away_goals, probability in enumerate(row):
            if away_goals >= 1:
                yes_probability += probability
    no_probability = 1.0 - yes_probability
    if not math.isclose(yes_probability + no_probability, 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("BTTS probabilities must sum to 1")

    source_scoreline_object_id = pricing_input["scoreline_object"]["object_id"]
    return [
        _binary_market_record(
            pricing_input=pricing_input,
            market_family="btts",
            selection="YES",
            probability=yes_probability,
            source_scoreline_object_id=source_scoreline_object_id,
        ),
        _binary_market_record(
            pricing_input=pricing_input,
            market_family="btts",
            selection="NO",
            probability=no_probability,
            source_scoreline_object_id=source_scoreline_object_id,
        ),
    ]


def price_goals_total_selection(
    pricing_input: dict[str, Any],
    *,
    line: float | int | str | Decimal,
    selection: str,
) -> dict[str, Any]:
    normalized_selection = str(selection).upper()
    if normalized_selection not in {"OVER", "UNDER"}:
        raise ValueError("goals-total selection must be OVER or UNDER")
    state_probabilities = _scoreline_state_probabilities(pricing_input["scoreline_object"]["matrix"])
    summary = _settlement_summary_from_states(
        state_probabilities=state_probabilities,
        line=line,
        component_margin=(
            lambda state, component_line: (state[0] + state[1]) - float(component_line)
            if normalized_selection == "OVER"
            else float(component_line) - (state[0] + state[1])
        ),
    )
    return _market_record(
        pricing_input=pricing_input,
        market_family="goals_total",
        selection=normalized_selection,
        line=float(_line_to_decimal(line)),
        settlement_summary=summary,
        source_scoreline_object_id=pricing_input["scoreline_object"]["object_id"],
        source_corner_object_id=None,
        numerical_metadata={
            "line_step_validation": "quarter",
            "scoreline_tail_mass_beyond_max_goals": pricing_input["scoreline_object"]["tail_mass_beyond_max_goals"],
            "scoreline_matrix_normalization_error": pricing_input["scoreline_object"]["normalization_error"],
        },
    )


def price_asian_handicap_selection(
    pricing_input: dict[str, Any],
    *,
    line: float | int | str | Decimal,
    selection: str,
) -> dict[str, Any]:
    normalized_selection = str(selection).upper()
    if normalized_selection not in {"HOME", "AWAY"}:
        raise ValueError("Asian handicap selection must be HOME or AWAY")
    home_line = _line_to_decimal(line)
    state_probabilities = _scoreline_state_probabilities(pricing_input["scoreline_object"]["matrix"])

    if normalized_selection == "HOME":
        component_margin = lambda state, component_line: (state[0] - state[1]) + float(component_line)
    else:
        component_margin = lambda state, component_line: (state[1] - state[0]) - float(component_line)

    summary = _settlement_summary_from_states(
        state_probabilities=state_probabilities,
        line=home_line,
        component_margin=component_margin,
    )
    return _market_record(
        pricing_input=pricing_input,
        market_family="asian_handicap",
        selection=normalized_selection,
        line=float(home_line),
        settlement_summary=summary,
        source_scoreline_object_id=pricing_input["scoreline_object"]["object_id"],
        source_corner_object_id=None,
        numerical_metadata={
            "line_convention": ASIAN_HANDICAP_LINE_CONVENTION,
            "scoreline_tail_mass_beyond_max_goals": pricing_input["scoreline_object"]["tail_mass_beyond_max_goals"],
            "scoreline_matrix_normalization_error": pricing_input["scoreline_object"]["normalization_error"],
        },
    )


def price_corner_total_selection(
    pricing_input: dict[str, Any],
    *,
    line: float | int | str | Decimal,
    selection: str,
) -> dict[str, Any]:
    normalized_selection = str(selection).upper()
    if normalized_selection not in {"OVER", "UNDER"}:
        raise ValueError("corner-total selection must be OVER or UNDER")
    corner_object = pricing_input["corner_object"]
    support = build_corner_total_distribution(
        mean_value=float(corner_object["predicted_total_corners"]),
        distribution_name=str(corner_object["selected_corner_distribution"]),
        dispersion=corner_object["dispersion"],
    )
    state_probabilities = list(support["support_probabilities"].items())
    summary = _settlement_summary_from_states(
        state_probabilities=state_probabilities,
        line=line,
        component_margin=(
            lambda count, component_line: float(count) - float(component_line)
            if normalized_selection == "OVER"
            else float(component_line) - float(count)
        ),
    )
    return _market_record(
        pricing_input=pricing_input,
        market_family="corner_total",
        selection=normalized_selection,
        line=float(_line_to_decimal(line)),
        settlement_summary=summary,
        source_scoreline_object_id=None,
        source_corner_object_id=corner_object["object_id"],
        numerical_metadata={
            "selected_corner_distribution": support["distribution_name"],
            "predicted_total_corners": support["mean_value"],
            "corner_dispersion": support["dispersion"],
            "corner_support_max": support["support_max"],
            "corner_residual_tail_mass": support["residual_tail_mass"],
            "corner_tail_tolerance": support["tail_tolerance"],
            "corner_normalization_error": support["normalization_error"],
            "corner_support_policy_id": support["support_policy_id"],
        },
    )


def _market_record(
    *,
    pricing_input: dict[str, Any],
    market_family: str,
    selection: str,
    line: float | None,
    settlement_summary: dict[str, Any],
    source_scoreline_object_id: str | None,
    source_corner_object_id: str | None,
    numerical_metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "fixture_id": pricing_input["fixture_id"],
        "event_id": pricing_input["event_id"],
        "competition_id": pricing_input["competition_id"],
        "competition_name": pricing_input["competition_name"],
        "season_code": pricing_input["season_code"],
        "division_code": pricing_input["division_code"],
        "match_date": pricing_input["match_date"],
        "kickoff_time": pricing_input["kickoff_time"],
        "kickoff_at": pricing_input["kickoff_at"],
        "as_of": pricing_input["as_of"],
        "market_family": market_family,
        "line": line,
        "selection": selection,
        "source_scoreline_object_id": source_scoreline_object_id,
        "source_corner_object_id": source_corner_object_id,
        "day10_input_contract_version": pricing_input["day10_input_contract_version"],
        "pricing_model_version": PRICING_MODEL_VERSION,
        "settlement_engine_version": SETTLEMENT_ENGINE_VERSION,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "settlement_pricing_policy_id": SETTLEMENT_PRICING_POLICY_ID,
        "line_catalog_version": LINE_CATALOG_VERSION,
        "model_versions": pricing_input["model_versions"],
        "settlement_probabilities": {
            key: settlement_summary[key]
            for key in (
                "full_win_probability",
                "half_win_probability",
                "push_probability",
                "half_loss_probability",
                "full_loss_probability",
            )
        },
        "effective_win_stake_probability": settlement_summary["effective_win_stake_probability"],
        "effective_loss_stake_probability": settlement_summary["effective_loss_stake_probability"],
        "fair_decimal_price": settlement_summary["fair_decimal_price"],
        "component_lines": settlement_summary.get("component_lines", []),
        "numerical_metadata": numerical_metadata,
    }


def derive_goals_markets_for_fixture(
    pricing_input: dict[str, Any],
    *,
    goals_total_lines: Sequence[float] = GOALS_TOTAL_LINES,
    asian_handicap_lines: Sequence[float] = ASIAN_HANDICAP_LINES,
) -> list[dict[str, Any]]:
    market_rows = []
    market_rows.extend(derive_1x2_market(pricing_input))
    market_rows.extend(derive_btts_market(pricing_input))
    for line in goals_total_lines:
        market_rows.append(price_goals_total_selection(pricing_input, line=line, selection="OVER"))
        market_rows.append(price_goals_total_selection(pricing_input, line=line, selection="UNDER"))
    for line in asian_handicap_lines:
        market_rows.append(price_asian_handicap_selection(pricing_input, line=line, selection="HOME"))
        market_rows.append(price_asian_handicap_selection(pricing_input, line=line, selection="AWAY"))
    return market_rows


def derive_corner_markets_for_fixture(
    pricing_input: dict[str, Any],
    *,
    corner_total_lines: Sequence[float] = CORNER_TOTAL_LINES,
) -> list[dict[str, Any]]:
    market_rows = []
    for line in corner_total_lines:
        market_rows.append(price_corner_total_selection(pricing_input, line=line, selection="OVER"))
        market_rows.append(price_corner_total_selection(pricing_input, line=line, selection="UNDER"))
    return market_rows


def run_day10_market_pricing_experiment(
    *,
    research_db_path: str | Path,
    day9_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_day9_result = day9_result or run_day9_scoreline_experiment(research_db_path=research_db_path)
    pricing_inputs = [build_day10_pricing_input(row) for row in resolved_day9_result["predictions"]]

    market_prices: list[dict[str, Any]] = []
    for pricing_input in pricing_inputs:
        market_prices.extend(derive_goals_markets_for_fixture(pricing_input))
        market_prices.extend(derive_corner_markets_for_fixture(pricing_input))

    market_family_counts: dict[str, int] = {}
    for row in market_prices:
        market_family_counts[row["market_family"]] = market_family_counts.get(row["market_family"], 0) + 1

    manifest = {
        "generated_at": _utc_now(),
        "experiment_id": EXPERIMENT_ID,
        "experiment_version": EXPERIMENT_VERSION,
        "source_research_db_path": str(Path(research_db_path)),
        "pricing_model_version": PRICING_MODEL_VERSION,
        "settlement_engine_version": SETTLEMENT_ENGINE_VERSION,
        "output_schema_version": OUTPUT_SCHEMA_VERSION,
        "day10_input_contract_version": DAY10_INPUT_CONTRACT_VERSION,
        "sanitized_input_policy_id": SANITIZED_INPUT_POLICY_ID,
        "sanitized_input_policy_description": SANITIZED_INPUT_POLICY_DESCRIPTION,
        "day9_to_day10_interface_policy_id": DAY9_TO_DAY10_INTERFACE_POLICY_ID,
        "day9_to_day10_interface_policy_description": DAY9_TO_DAY10_INTERFACE_POLICY_DESCRIPTION,
        "day8_to_day10_corner_interface_policy_id": DAY8_TO_DAY10_CORNER_INTERFACE_POLICY_ID,
        "day8_to_day10_corner_interface_policy_description": DAY8_TO_DAY10_CORNER_INTERFACE_POLICY_DESCRIPTION,
        "settlement_pricing_policy_id": SETTLEMENT_PRICING_POLICY_ID,
        "settlement_pricing_policy_description": SETTLEMENT_PRICING_POLICY_DESCRIPTION,
        "asian_handicap_line_convention": ASIAN_HANDICAP_LINE_CONVENTION,
        "corner_support_policy_id": CORNER_SUPPORT_POLICY_ID,
        "corner_support_tail_tolerance": CORNER_SUPPORT_TAIL_TOLERANCE,
        "line_catalog_version": LINE_CATALOG_VERSION,
        "engineering_verification_lines": {
            "goals_total_lines": list(GOALS_TOTAL_LINES),
            "asian_handicap_lines": list(ASIAN_HANDICAP_LINES),
            "corner_total_lines": list(CORNER_TOTAL_LINES),
        },
        "pricing_input_count": len(pricing_inputs),
        "market_price_count": len(market_prices),
        "market_family_counts": market_family_counts,
        "source_day9_prediction_count": resolved_day9_result["manifest"]["prediction_count"],
        "source_day9_scoreline_model_version": SCORELINE_MODEL_VERSION,
        "source_day8_conversion_model_version": CONVERSION_MODEL_VERSION,
        "source_day8_experiment_version": DAY8_EXPERIMENT_VERSION,
        "source_day9_experiment_version": DAY9_EXPERIMENT_VERSION,
        "day11_probability_semantics_note": (
            "Settlement-aware fair prices are preserved directly. Day 11 must define how any "
            "derived model_probability or edge field represents push and quarter-line markets."
        ),
        "forward_production_fit_dependency_note": (
            "Historical Day 8 and Day 9 parameters in the loaded archive are valid for walk-forward "
            "evaluation, but they are not automatically a current forward-production snapshot."
        ),
    }
    return {
        "manifest": manifest,
        "pricing_inputs": pricing_inputs,
        "market_prices": market_prices,
        "day9_result": resolved_day9_result,
    }


def _render_markdown_summary(result: dict[str, Any]) -> str:
    manifest = result["manifest"]
    family_lines = [
        "| Market Family | Rows |",
        "| --- | ---: |",
    ]
    for market_family, row_count in sorted(manifest["market_family_counts"].items()):
        family_lines.append(f"| {market_family} | {row_count} |")
    return "\n".join(
        [
            "# Football Day 10 Market Pricing",
            "",
            f"Generated at: `{manifest['generated_at']}`",
            f"Experiment/version: `{manifest['experiment_id']}` / `{manifest['experiment_version']}`",
            "",
            "## Interface",
            "",
            f"- sanitized input policy: `{manifest['sanitized_input_policy_id']}`",
            f"- goals interface: `{manifest['day9_to_day10_interface_policy_id']}`",
            f"- corners interface: `{manifest['day8_to_day10_corner_interface_policy_id']}`",
            f"- settlement pricing: `{manifest['settlement_pricing_policy_id']}`",
            "",
            "## Engineering Verification Line Catalog",
            "",
            f"- goals totals: `{manifest['engineering_verification_lines']['goals_total_lines']}`",
            f"- Asian handicap: `{manifest['engineering_verification_lines']['asian_handicap_lines']}`",
            f"- corner totals: `{manifest['engineering_verification_lines']['corner_total_lines']}`",
            "",
            "## Market Rows",
            "",
            *family_lines,
            "",
            "## Day 11 Note",
            "",
            f"- {manifest['day11_probability_semantics_note']}",
        ]
    )


def _write_jsonl(path: str | Path, rows: Sequence[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True))
            handle.write("\n")


def write_day10_artifacts(
    result: dict[str, Any],
    *,
    manifest_path: str | Path,
    pricing_inputs_jsonl_path: str | Path,
    market_prices_jsonl_path: str | Path,
    markdown_path: str | Path,
) -> dict[str, str]:
    manifest_target = Path(manifest_path)
    pricing_inputs_target = Path(pricing_inputs_jsonl_path)
    market_prices_target = Path(market_prices_jsonl_path)
    markdown_target = Path(markdown_path)
    for target in (manifest_target, pricing_inputs_target, market_prices_target, markdown_target):
        target.parent.mkdir(parents=True, exist_ok=True)

    manifest_target.write_text(json.dumps(result["manifest"], indent=2), encoding="utf-8")
    _write_jsonl(pricing_inputs_target, result["pricing_inputs"])
    _write_jsonl(market_prices_target, result["market_prices"])
    markdown_target.write_text(_render_markdown_summary(result), encoding="utf-8")
    return {
        "manifest_path": str(manifest_target),
        "pricing_inputs_path": str(pricing_inputs_target),
        "market_prices_path": str(market_prices_target),
        "markdown_path": str(markdown_target),
    }


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the Day 10 football market-pricing experiment.")
    parser.add_argument("--research-db", required=True, help="Path to football research SQLite store.")
    parser.add_argument("--manifest-out", help="Optional manifest JSON path.")
    parser.add_argument("--pricing-inputs-out", help="Optional Day 10 pricing-input JSONL path.")
    parser.add_argument("--market-prices-out", help="Optional Day 10 market-prices JSONL path.")
    parser.add_argument("--markdown-out", help="Optional Markdown summary path.")
    args = parser.parse_args(argv)

    result = run_day10_market_pricing_experiment(research_db_path=args.research_db)
    payload: dict[str, Any] = {
        "experiment_id": result["manifest"]["experiment_id"],
        "pricing_input_count": result["manifest"]["pricing_input_count"],
        "market_price_count": result["manifest"]["market_price_count"],
    }
    if args.manifest_out and args.pricing_inputs_out and args.market_prices_out and args.markdown_out:
        payload["artifacts"] = write_day10_artifacts(
            result,
            manifest_path=args.manifest_out,
            pricing_inputs_jsonl_path=args.pricing_inputs_out,
            market_prices_jsonl_path=args.market_prices_out,
            markdown_path=args.markdown_out,
        )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
