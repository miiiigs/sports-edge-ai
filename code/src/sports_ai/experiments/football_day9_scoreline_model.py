"""Day 9 football Dixon-Coles scoreline layer built downstream of Day 8 expected goals.

This module consumes the frozen Day 8 expected-goal outputs and adds:

- an explicit Day 8 -> Day 9 interface contract
- independent Poisson baseline scoreline matrices over 0..10 x 0..10
- Dixon-Coles low-score correction through rho
- chronological, competition-isolated rho fitting
- a bounded time-decay comparison through xi

The Day 9 layer remains model-only:

- no sportsbook odds
- no de-vig
- no edge or staking logic
- no mutation of Day 8 or GAP state
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Sequence

from .football_day8_goal_conversion import (
    CONVERSION_MODEL_VERSION,
    DAY7_BASELINE_EXPERIMENT_VERSION,
    EXPERIMENT_VERSION as DAY8_EXPERIMENT_VERSION,
    run_day8_goal_conversion_experiment,
)

EXPERIMENT_ID = "EXP-012"
EXPERIMENT_VERSION = "football_day9_scoreline_model_v1"
SCORELINE_MODEL_VERSION = "football_day9_dixon_coles_scoreline_v1"
DAY8_TO_DAY9_INTERFACE_POLICY_ID = "day8_expected_goals_are_day9_intensities_v1"
DAY8_TO_DAY9_INTERFACE_DECISION = (
    "Day 8 expected_goals_home and expected_goals_away are the canonical pre-match goal intensities "
    "for Day 9. The Day 9 scoreline layer does not fit a second competing goals model. It applies "
    "Dixon-Coles low-score dependence around those fixed intensities."
)
DAY8_TO_DAY9_INTERFACE_JUSTIFICATION = (
    "This avoids bypassing the closed Day 8 conversion layer and avoids double-counting the venue "
    "effect already present in the competition-and-venue Day 8 conversion."
)
GAMMA_HANDLING_POLICY_ID = "gamma_omitted_to_avoid_double_counting_day8_venue_effects_v1"
GAMMA_HANDLING_DESCRIPTION = (
    "The explicit Dixon-Coles home-advantage gamma term is not re-estimated in Day 9 because the "
    "Day 8 competition-and-venue conversion already produces asymmetric home and away goal "
    "intensities. Adding gamma here would create a second home-advantage channel and risk "
    "double-counting."
)
RHO_GRID = (-0.20, -0.15, -0.13, -0.10, -0.05, 0.00, 0.05)
TIME_DECAY_GRID = (0.0, 0.001)
DEFAULT_RHO = 0.0
DEFAULT_XI = 0.0
MAX_GOALS = 10
TRUNCATION_POLICY_ID = "truncate_to_0_10_measure_tail_then_renormalize_in_matrix_v1"
TRUNCATION_POLICY_DESCRIPTION = (
    "Construct the 0..10 x 0..10 matrix, record the raw in-matrix mass and the tail mass beyond "
    "10 from the independent Poisson baseline, then renormalize the in-matrix probabilities to sum "
    "to one. Tail mass is preserved as a separate diagnostic field rather than silently discarded."
)
AS_OF_POLICY_ID = "persisted_prediction_as_of_from_day8_v1"
AS_OF_POLICY_DESCRIPTION = (
    "Reuse the persisted Day 8 historical pre-match as_of field directly. Day 9 does not invent a "
    "new timestamp and does not move as_of later than kickoff."
)
RHO_FIT_OBJECTIVE_ID = "weighted_dixon_coles_observed_score_log_likelihood_v1"
RHO_FIT_OBJECTIVE_DESCRIPTION = (
    "Fit rho on prior historical score outcomes only by maximizing the canonical Dixon-Coles "
    "low-score-adjusted observed-score log likelihood. No betting ROI objective is used."
)
TIME_DECAY_SELECTION_POLICY_ID = "prior_out_of_sample_log_likelihood_with_flat_reference_default_v1"
TIME_DECAY_SELECTION_POLICY_DESCRIPTION = (
    "Compare the fixed xi grid using only prior out-of-sample fold log likelihood. Default to the "
    "simpler flat-weight xi=0.0 before any comparison evidence exists and prefer xi=0.0 on ties."
)
PDF_REFERENCE_GAMMA = 0.29
PDF_REFERENCE_RHO = -0.13


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _poisson_pmf(k: int, mean_value: float) -> float:
    if mean_value < 0:
        raise ValueError("Poisson mean must be non-negative")
    if mean_value == 0.0:
        return 1.0 if k == 0 else 0.0
    return math.exp((k * math.log(mean_value)) - mean_value - math.lgamma(k + 1.0))


def _log_poisson_pmf(k: int, mean_value: float) -> float:
    if mean_value < 0:
        raise ValueError("Poisson mean must be non-negative")
    if mean_value == 0.0:
        return 0.0 if k == 0 else float("-inf")
    return (k * math.log(mean_value)) - mean_value - math.lgamma(k + 1.0)


def dixon_coles_tau(
    home_goals: int,
    away_goals: int,
    *,
    home_goals_mean: float,
    away_goals_mean: float,
    rho: float,
) -> float:
    """Return the canonical Dixon-Coles low-score adjustment factor."""

    if home_goals == 0 and away_goals == 0:
        return 1.0 - (home_goals_mean * away_goals_mean * rho)
    if home_goals == 0 and away_goals == 1:
        return 1.0 + (home_goals_mean * rho)
    if home_goals == 1 and away_goals == 0:
        return 1.0 + (away_goals_mean * rho)
    if home_goals == 1 and away_goals == 1:
        return 1.0 - rho
    return 1.0


def _score_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row["competition_id"]),
        str(row["match_date"]),
        str(row["kickoff_time"] or ""),
        str(row["event_id"]),
    )


def _group_rows_by_competition(rows: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["competition_id"])].append(row)
    return {
        competition_id: sorted(group_rows, key=_score_key)
        for competition_id, group_rows in sorted(grouped.items())
    }


def _season_codes(rows: Sequence[dict[str, Any]]) -> list[str]:
    return sorted({str(row["season_code"]) for row in rows})


def _rows_for_seasons(rows: Sequence[dict[str, Any]], season_codes: Sequence[str]) -> list[dict[str, Any]]:
    allowed = set(season_codes)
    return [row for row in rows if str(row["season_code"]) in allowed]


def _parse_as_of(as_of_value: str) -> datetime:
    return datetime.fromisoformat(as_of_value)


def _time_weight(*, latest_as_of: str, row_as_of: str, xi: float) -> float:
    if xi < 0:
        raise ValueError("xi must be non-negative")
    if xi == 0.0:
        return 1.0
    latest = _parse_as_of(latest_as_of)
    current = _parse_as_of(row_as_of)
    age_days = max(0.0, (latest - current).total_seconds() / 86400.0)
    return math.exp(-xi * age_days)


def _matrix_sum(matrix: Sequence[Sequence[float]]) -> float:
    return float(sum(sum(row) for row in matrix))


def _matrix_draw_probability(matrix: Sequence[Sequence[float]]) -> float:
    return float(sum(matrix[index][index] for index in range(len(matrix))))


def _matrix_home_mean(matrix: Sequence[Sequence[float]]) -> float:
    total = 0.0
    for home_goals, row in enumerate(matrix):
        total += home_goals * sum(row)
    return float(total)


def _matrix_away_mean(matrix: Sequence[Sequence[float]]) -> float:
    total = 0.0
    for row in matrix:
        for away_goals, probability in enumerate(row):
            total += away_goals * probability
    return float(total)


def _safe_log_likelihood_improvement(
    dixon_coles_log_likelihood: float | None,
    plain_poisson_log_likelihood: float | None,
) -> float | None:
    if dixon_coles_log_likelihood is None or plain_poisson_log_likelihood is None:
        return None
    if not (math.isfinite(dixon_coles_log_likelihood) and math.isfinite(plain_poisson_log_likelihood)):
        return None
    return float(dixon_coles_log_likelihood - plain_poisson_log_likelihood)


def build_plain_poisson_scoreline_distribution(
    home_goals_mean: float,
    away_goals_mean: float,
    *,
    max_goals: int = MAX_GOALS,
) -> dict[str, Any]:
    """Build the truncated independent-Poisson matrix and record tail handling."""

    home_probs = [_poisson_pmf(goal_count, home_goals_mean) for goal_count in range(max_goals + 1)]
    away_probs = [_poisson_pmf(goal_count, away_goals_mean) for goal_count in range(max_goals + 1)]
    raw_matrix = [
        [home_probability * away_probability for away_probability in away_probs]
        for home_probability in home_probs
    ]
    raw_matrix_mass = _matrix_sum(raw_matrix)
    tail_mass = max(0.0, 1.0 - raw_matrix_mass)
    if raw_matrix_mass <= 0.0:
        raise ValueError("plain Poisson matrix mass collapsed to zero")
    normalization_factor = 1.0 / raw_matrix_mass
    matrix = [
        [value * normalization_factor for value in row]
        for row in raw_matrix
    ]
    return {
        "matrix": matrix,
        "raw_matrix_mass": raw_matrix_mass,
        "tail_mass_beyond_max_goals": tail_mass,
        "normalization_factor": normalization_factor,
        "normalization_error": abs(1.0 - _matrix_sum(matrix)),
        "draw_probability": _matrix_draw_probability(matrix),
        "marginal_home_mean": _matrix_home_mean(matrix),
        "marginal_away_mean": _matrix_away_mean(matrix),
        "max_goals": max_goals,
        "truncation_policy_id": TRUNCATION_POLICY_ID,
        "truncation_policy_description": TRUNCATION_POLICY_DESCRIPTION,
    }


def build_dixon_coles_scoreline_distribution(
    home_goals_mean: float,
    away_goals_mean: float,
    *,
    rho: float,
    max_goals: int = MAX_GOALS,
) -> dict[str, Any]:
    """Build the truncated Dixon-Coles matrix and record normalization policy."""

    plain = build_plain_poisson_scoreline_distribution(
        home_goals_mean,
        away_goals_mean,
        max_goals=max_goals,
    )
    raw_matrix: list[list[float]] = []
    for home_goals, plain_row in enumerate(plain["matrix"]):
        raw_row: list[float] = []
        for away_goals, plain_probability in enumerate(plain_row):
            tau = dixon_coles_tau(
                home_goals,
                away_goals,
                home_goals_mean=home_goals_mean,
                away_goals_mean=away_goals_mean,
                rho=rho,
            )
            raw_row.append((plain_probability / plain["normalization_factor"]) * tau)
        raw_matrix.append(raw_row)

    raw_matrix_mass = _matrix_sum(raw_matrix)
    if raw_matrix_mass <= 0.0:
        raise ValueError("Dixon-Coles matrix mass collapsed to zero")
    normalization_factor = 1.0 / raw_matrix_mass
    matrix = [
        [value * normalization_factor for value in row]
        for row in raw_matrix
    ]
    full_distribution_mass_estimate = raw_matrix_mass + plain["tail_mass_beyond_max_goals"]
    return {
        "matrix": matrix,
        "raw_matrix_mass": raw_matrix_mass,
        "tail_mass_beyond_max_goals": plain["tail_mass_beyond_max_goals"],
        "normalization_factor": normalization_factor,
        "normalization_error": abs(1.0 - _matrix_sum(matrix)),
        "draw_probability": _matrix_draw_probability(matrix),
        "marginal_home_mean": _matrix_home_mean(matrix),
        "marginal_away_mean": _matrix_away_mean(matrix),
        "max_goals": max_goals,
        "truncation_policy_id": TRUNCATION_POLICY_ID,
        "truncation_policy_description": TRUNCATION_POLICY_DESCRIPTION,
        "full_distribution_mass_estimate": full_distribution_mass_estimate,
        "full_distribution_mass_estimate_error": full_distribution_mass_estimate - 1.0,
        "rho": rho,
    }


def dixon_coles_observed_score_log_likelihood(
    *,
    home_goals: int,
    away_goals: int,
    home_goals_mean: float,
    away_goals_mean: float,
    rho: float,
) -> float:
    """Return the canonical Dixon-Coles observed-score log likelihood for one fixture."""

    tau = dixon_coles_tau(
        home_goals,
        away_goals,
        home_goals_mean=home_goals_mean,
        away_goals_mean=away_goals_mean,
        rho=rho,
    )
    if tau <= 0.0 or not math.isfinite(tau):
        return float("-inf")
    return (
        _log_poisson_pmf(home_goals, home_goals_mean)
        + _log_poisson_pmf(away_goals, away_goals_mean)
        + math.log(tau)
    )


def _plain_poisson_observed_score_log_likelihood(*, row: dict[str, Any]) -> float:
    return (
        _log_poisson_pmf(int(row["actual_home_goals"]), float(row["expected_goals_home"]))
        + _log_poisson_pmf(int(row["actual_away_goals"]), float(row["expected_goals_away"]))
    )


def _weighted_log_likelihood(
    *,
    rows: Sequence[dict[str, Any]],
    rho: float,
    xi: float,
) -> dict[str, float]:
    if not rows:
        return {
            "weighted_dixon_coles_log_likelihood": float("-inf"),
            "weighted_plain_poisson_log_likelihood": float("-inf"),
        }
    latest_as_of = max(str(row["as_of"]) for row in rows)
    weighted_dc = 0.0
    weighted_plain = 0.0
    for row in rows:
        weight = _time_weight(latest_as_of=latest_as_of, row_as_of=str(row["as_of"]), xi=xi)
        plain_ll = _plain_poisson_observed_score_log_likelihood(row=row)
        dc_ll = dixon_coles_observed_score_log_likelihood(
            home_goals=int(row["actual_home_goals"]),
            away_goals=int(row["actual_away_goals"]),
            home_goals_mean=float(row["expected_goals_home"]),
            away_goals_mean=float(row["expected_goals_away"]),
            rho=rho,
        )
        if not math.isfinite(dc_ll):
            return {
                "weighted_dixon_coles_log_likelihood": float("-inf"),
                "weighted_plain_poisson_log_likelihood": weighted_plain + (weight * plain_ll),
            }
        weighted_plain += weight * plain_ll
        weighted_dc += weight * dc_ll
    return {
        "weighted_dixon_coles_log_likelihood": float(weighted_dc),
        "weighted_plain_poisson_log_likelihood": float(weighted_plain),
    }


def _evaluation_summary(
    *,
    rows: Sequence[dict[str, Any]],
    rho: float,
) -> dict[str, Any]:
    if not rows:
        return {
            "evaluation_sample_size": 0,
            "plain_poisson_log_likelihood": None,
            "dixon_coles_log_likelihood": None,
            "log_likelihood_improvement": None,
            "average_plain_draw_probability": None,
            "average_dixon_coles_draw_probability": None,
            "average_draw_probability_difference": None,
            "draw_probability_increase_fixture_share": None,
        }

    plain_log_likelihoods: list[float] = []
    dc_log_likelihoods: list[float] = []
    plain_draws: list[float] = []
    dc_draws: list[float] = []
    for row in rows:
        plain_matrix = build_plain_poisson_scoreline_distribution(
            float(row["expected_goals_home"]),
            float(row["expected_goals_away"]),
            max_goals=MAX_GOALS,
        )
        dc_matrix = build_dixon_coles_scoreline_distribution(
            float(row["expected_goals_home"]),
            float(row["expected_goals_away"]),
            rho=rho,
            max_goals=MAX_GOALS,
        )
        plain_draws.append(float(plain_matrix["draw_probability"]))
        dc_draws.append(float(dc_matrix["draw_probability"]))
        plain_log_likelihoods.append(_plain_poisson_observed_score_log_likelihood(row=row))
        dc_log_likelihoods.append(
            dixon_coles_observed_score_log_likelihood(
                home_goals=int(row["actual_home_goals"]),
                away_goals=int(row["actual_away_goals"]),
                home_goals_mean=float(row["expected_goals_home"]),
                away_goals_mean=float(row["expected_goals_away"]),
                rho=rho,
            )
        )
    draw_differences = [right - left for left, right in zip(plain_draws, dc_draws)]
    total_plain_ll = float(sum(plain_log_likelihoods))
    total_dc_ll = float(sum(dc_log_likelihoods))
    return {
        "evaluation_sample_size": len(rows),
        "plain_poisson_log_likelihood": total_plain_ll,
        "dixon_coles_log_likelihood": total_dc_ll,
        "log_likelihood_improvement": _safe_log_likelihood_improvement(total_dc_ll, total_plain_ll),
        "average_plain_draw_probability": float(mean(plain_draws)),
        "average_dixon_coles_draw_probability": float(mean(dc_draws)),
        "average_draw_probability_difference": float(mean(draw_differences)),
        "draw_probability_increase_fixture_share": float(
            sum(1 for value in draw_differences if value > 0.0) / len(draw_differences)
        ),
    }


def _rho_variant_sort_key(variant: dict[str, Any]) -> tuple[float, float, float]:
    likelihood = variant["training_weighted_dixon_coles_log_likelihood"]
    if likelihood is None or not math.isfinite(likelihood):
        return (float("inf"), float("inf"), float("inf"))
    return (
        -float(likelihood),
        abs(float(variant["candidate_rho"])),
        float(variant["candidate_rho"]),
    )


def _fit_fold_variants(
    *,
    competition_id: str,
    competition_name: str,
    current_season: str,
    training_rows: Sequence[dict[str, Any]],
    evaluation_rows: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    variants: list[dict[str, Any]] = []
    xi_candidate_summaries: list[dict[str, Any]] = []

    training_seasons = _season_codes(training_rows)
    baseline_plain_by_xi: dict[float, float] = {}
    best_variant_by_xi: dict[float, dict[str, Any]] = {}

    for xi in TIME_DECAY_GRID:
        xi_variants: list[dict[str, Any]] = []
        baseline_plain_ll: float | None = None
        for rho in RHO_GRID:
            fit_metrics = _weighted_log_likelihood(rows=training_rows, rho=rho, xi=xi)
            baseline_plain_ll = fit_metrics["weighted_plain_poisson_log_likelihood"]
            variant = {
                "variant_id": (
                    f"{competition_id.lower()}_{current_season}_xi_{xi:.4f}_rho_{rho:+.2f}_"
                    "dixon_coles_scoreline_v1"
                ),
                "competition_id": competition_id,
                "competition_name": competition_name,
                "season_code": current_season,
                "train_period": {
                    "start_season": training_seasons[0],
                    "end_season": training_seasons[-1],
                    "season_codes": training_seasons,
                },
                "evaluation_period": {
                    "start_season": current_season,
                    "end_season": current_season,
                    "season_codes": [current_season],
                },
                "candidate_rho": float(rho),
                "candidate_xi": float(xi),
                "sample_size": len(training_rows),
                "training_weighted_dixon_coles_log_likelihood": float(
                    fit_metrics["weighted_dixon_coles_log_likelihood"]
                ),
                "training_weighted_plain_poisson_log_likelihood": float(
                    fit_metrics["weighted_plain_poisson_log_likelihood"]
                ),
                "training_weighted_log_likelihood_improvement": float(
                    fit_metrics["weighted_dixon_coles_log_likelihood"]
                    - fit_metrics["weighted_plain_poisson_log_likelihood"]
                )
                if math.isfinite(fit_metrics["weighted_dixon_coles_log_likelihood"])
                else float("-inf"),
                "fit_objective_id": RHO_FIT_OBJECTIVE_ID,
                "fit_objective_description": RHO_FIT_OBJECTIVE_DESCRIPTION,
                "as_of_policy_id": AS_OF_POLICY_ID,
                "as_of_policy_description": AS_OF_POLICY_DESCRIPTION,
                "model_version": SCORELINE_MODEL_VERSION,
                "selected_status": "rejected",
                "selection_reason": None,
            }
            xi_variants.append(variant)
            variants.append(variant)

        xi_variants.sort(key=_rho_variant_sort_key)
        best_variant = xi_variants[0]
        best_variant["selected_status"] = "best_for_time_decay_candidate"
        best_variant["selection_reason"] = "best training weighted log likelihood within this xi candidate"
        baseline_plain_by_xi[float(xi)] = float(baseline_plain_ll if baseline_plain_ll is not None else float("-inf"))
        best_variant_by_xi[float(xi)] = best_variant

        evaluation_metrics = _evaluation_summary(
            rows=evaluation_rows,
            rho=float(best_variant["candidate_rho"]),
        )
        xi_candidate_summaries.append(
            {
                "candidate_xi": float(xi),
                "best_rho": float(best_variant["candidate_rho"]),
                "training_weighted_dixon_coles_log_likelihood": float(
                    best_variant["training_weighted_dixon_coles_log_likelihood"]
                ),
                "training_weighted_plain_poisson_log_likelihood": float(
                    best_variant["training_weighted_plain_poisson_log_likelihood"]
                ),
                "training_weighted_log_likelihood_improvement": float(
                    best_variant["training_weighted_log_likelihood_improvement"]
                ),
                **evaluation_metrics,
            }
        )

    xi_candidate_summaries.sort(key=lambda row: float(row["candidate_xi"]))
    return variants, xi_candidate_summaries


def _select_xi_for_predictions(
    *,
    prior_xi_oos_records: Sequence[dict[str, Any]],
) -> tuple[float, str]:
    if not prior_xi_oos_records:
        return (
            DEFAULT_XI,
            "no prior out-of-sample xi comparison exists yet, so keep the simpler flat-weight xi=0.0 default",
        )

    cumulative_by_xi: dict[float, float] = {float(xi): 0.0 for xi in TIME_DECAY_GRID}
    for fold_record in prior_xi_oos_records:
        for summary in fold_record["xi_candidate_summaries"]:
            cumulative_by_xi[float(summary["candidate_xi"])] += float(summary["dixon_coles_log_likelihood"])

    best_xi = min(
        cumulative_by_xi,
        key=lambda candidate_xi: (-cumulative_by_xi[candidate_xi], candidate_xi),
    )
    pieces = ", ".join(
        f"xi={candidate_xi:.4f}: {cumulative_by_xi[candidate_xi]:.6f}"
        for candidate_xi in sorted(cumulative_by_xi)
    )
    return (
        float(best_xi),
        f"selected xi by cumulative prior out-of-sample log likelihood ({pieces}); ties favor xi=0.0",
    )


def _scoreline_diagnostics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "prediction_count": 0,
            "plain_poisson_log_likelihood": None,
            "dixon_coles_log_likelihood": None,
            "log_likelihood_improvement": None,
            "average_plain_draw_probability": None,
            "average_dixon_coles_draw_probability": None,
            "average_draw_probability_difference": None,
            "draw_probability_increase_fixture_share": None,
            "average_tail_mass_beyond_10": None,
            "max_tail_mass_beyond_10": None,
            "average_matrix_normalization_error": None,
            "max_matrix_normalization_error": None,
            "average_full_distribution_mass_estimate_error": None,
            "max_abs_full_distribution_mass_estimate_error": None,
            "average_marginal_home_mean_gap": None,
            "average_marginal_away_mean_gap": None,
            "observed_score_outside_matrix_count": None,
        }

    plain_ll = [float(row["plain_poisson_log_likelihood"]) for row in rows]
    dc_ll = [float(row["dixon_coles_log_likelihood"]) for row in rows]
    plain_draw = [float(row["plain_poisson_draw_probability"]) for row in rows]
    dc_draw = [float(row["dixon_coles_draw_probability"]) for row in rows]
    draw_diff = [float(row["draw_probability_difference"]) for row in rows]
    tail_mass = [float(row["truncated_tail_mass_beyond_10"]) for row in rows]
    normalization_errors = [float(row["scoreline_matrix_normalization_error"]) for row in rows]
    mass_estimate_errors = [float(row["full_distribution_mass_estimate_error"]) for row in rows]
    home_mean_gaps = [abs(float(row["matrix_marginal_home_mean"]) - float(row["expected_goals_home"])) for row in rows]
    away_mean_gaps = [abs(float(row["matrix_marginal_away_mean"]) - float(row["expected_goals_away"])) for row in rows]
    observed_outside = [
        1
        for row in rows
        if int(row["actual_home_goals"]) > MAX_GOALS or int(row["actual_away_goals"]) > MAX_GOALS
    ]
    total_plain_ll = float(sum(plain_ll))
    total_dc_ll = float(sum(dc_ll))
    return {
        "prediction_count": len(rows),
        "plain_poisson_log_likelihood": total_plain_ll,
        "dixon_coles_log_likelihood": total_dc_ll,
        "log_likelihood_improvement": _safe_log_likelihood_improvement(total_dc_ll, total_plain_ll),
        "average_plain_draw_probability": float(mean(plain_draw)),
        "average_dixon_coles_draw_probability": float(mean(dc_draw)),
        "average_draw_probability_difference": float(mean(draw_diff)),
        "draw_probability_increase_fixture_share": float(sum(1 for value in draw_diff if value > 0.0) / len(draw_diff)),
        "average_tail_mass_beyond_10": float(mean(tail_mass)),
        "max_tail_mass_beyond_10": float(max(tail_mass)),
        "average_matrix_normalization_error": float(mean(normalization_errors)),
        "max_matrix_normalization_error": float(max(normalization_errors)),
        "average_full_distribution_mass_estimate_error": float(mean(mass_estimate_errors)),
        "max_abs_full_distribution_mass_estimate_error": float(max(abs(value) for value in mass_estimate_errors)),
        "average_marginal_home_mean_gap": float(mean(home_mean_gaps)),
        "average_marginal_away_mean_gap": float(mean(away_mean_gaps)),
        "observed_score_outside_matrix_count": len(observed_outside),
    }


def _group_prediction_rows_by_key(
    rows: Sequence[dict[str, Any]],
    *,
    key: str,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row[key])].append(row)
    return {
        group_key: _scoreline_diagnostics(group_rows)
        for group_key, group_rows in sorted(grouped.items())
    }


def run_day9_scoreline_experiment(
    *,
    research_db_path: str | Path,
    day8_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_day8_result = day8_result or run_day8_goal_conversion_experiment(research_db_path=research_db_path)
    day8_predictions = sorted(resolved_day8_result["predictions"], key=_score_key)
    by_competition = _group_rows_by_competition(day8_predictions)

    prediction_rows: list[dict[str, Any]] = []
    rho_variant_records: list[dict[str, Any]] = []
    fold_records: list[dict[str, Any]] = []
    final_rho_by_competition: list[dict[str, Any]] = []
    time_decay_summary: list[dict[str, Any]] = []

    for competition_id, competition_rows in by_competition.items():
        competition_name = str(competition_rows[0]["competition_name"])
        seasons = _season_codes(competition_rows)
        prior_xi_oos_records: list[dict[str, Any]] = []
        latest_selected_rho = DEFAULT_RHO
        latest_selected_xi = DEFAULT_XI

        for season_index, current_season in enumerate(seasons):
            evaluation_rows = _rows_for_seasons(competition_rows, [current_season])
            training_rows = _rows_for_seasons(competition_rows, seasons[:season_index])
            if not evaluation_rows:
                continue

            if not training_rows:
                selected_xi = DEFAULT_XI
                selected_rho = DEFAULT_RHO
                xi_selection_reason = (
                    "first Day 8 evaluation season for this competition, so default to the simpler flat-weight "
                    "xi=0.0 and the plain-Poisson-equivalent rho=0.0 before any prior scoreline evidence exists"
                )
                rho_selection_reason = (
                    "no prior Day 8 expected-goal outcomes exist yet for this competition, so rho defaults to 0.0"
                )
                xi_candidate_summaries: list[dict[str, Any]] = []
            else:
                fold_variants, xi_candidate_summaries = _fit_fold_variants(
                    competition_id=competition_id,
                    competition_name=competition_name,
                    current_season=current_season,
                    training_rows=training_rows,
                    evaluation_rows=evaluation_rows,
                )
                rho_variant_records.extend(fold_variants)
                selected_xi, xi_selection_reason = _select_xi_for_predictions(
                    prior_xi_oos_records=prior_xi_oos_records,
                )
                selected_summary = next(
                    summary
                    for summary in xi_candidate_summaries
                    if float(summary["candidate_xi"]) == float(selected_xi)
                )
                selected_rho = float(selected_summary["best_rho"])
                rho_selection_reason = (
                    f"fit rho chronologically on prior Day 8 expected-goal outcomes under xi={selected_xi:.4f}; "
                    f"selected rho={selected_rho:+.2f} by weighted training log likelihood"
                )
                for variant in fold_variants:
                    if (
                        float(variant["candidate_xi"]) == float(selected_xi)
                        and float(variant["candidate_rho"]) == float(selected_rho)
                    ):
                        variant["selected_status"] = "selected_for_predictions"
                        variant["selection_reason"] = (
                            "selected for this season because its xi won on prior out-of-sample evidence and "
                            "its rho won inside that xi on chronological training likelihood"
                        )
                    elif (
                        float(variant["candidate_xi"]) == float(selected_xi)
                        and variant["selected_status"] == "best_for_time_decay_candidate"
                    ):
                        variant["selection_reason"] = (
                            "best rho inside the selected xi candidate for this season"
                        )

            latest_selected_rho = float(selected_rho)
            latest_selected_xi = float(selected_xi)

            evaluation_diagnostics = _evaluation_summary(rows=evaluation_rows, rho=float(selected_rho))
            fold_record = {
                "competition_id": competition_id,
                "competition_name": competition_name,
                "season_code": current_season,
                "train_period": {
                    "start_season": seasons[0] if training_rows else None,
                    "end_season": seasons[season_index - 1] if training_rows else None,
                    "season_codes": seasons[:season_index],
                },
                "evaluation_period": {
                    "start_season": current_season,
                    "end_season": current_season,
                    "season_codes": [current_season],
                },
                "selected_xi_for_predictions": float(selected_xi),
                "selected_rho_for_predictions": float(selected_rho),
                "xi_selection_reason": xi_selection_reason,
                "rho_selection_reason": rho_selection_reason,
                "sample_size_train": len(training_rows),
                "sample_size_evaluation": len(evaluation_rows),
                "as_of_policy_id": AS_OF_POLICY_ID,
                "as_of_policy_description": AS_OF_POLICY_DESCRIPTION,
                "model_version": SCORELINE_MODEL_VERSION,
                "xi_candidate_summaries": xi_candidate_summaries,
                **evaluation_diagnostics,
            }
            fold_records.append(fold_record)
            if training_rows:
                prior_xi_oos_records.append(fold_record)

            for row in evaluation_rows:
                plain_distribution = build_plain_poisson_scoreline_distribution(
                    float(row["expected_goals_home"]),
                    float(row["expected_goals_away"]),
                    max_goals=MAX_GOALS,
                )
                dc_distribution = build_dixon_coles_scoreline_distribution(
                    float(row["expected_goals_home"]),
                    float(row["expected_goals_away"]),
                    rho=float(selected_rho),
                    max_goals=MAX_GOALS,
                )
                plain_ll = _plain_poisson_observed_score_log_likelihood(row=row)
                dc_ll = dixon_coles_observed_score_log_likelihood(
                    home_goals=int(row["actual_home_goals"]),
                    away_goals=int(row["actual_away_goals"]),
                    home_goals_mean=float(row["expected_goals_home"]),
                    away_goals_mean=float(row["expected_goals_away"]),
                    rho=float(selected_rho),
                )
                prediction_rows.append(
                    {
                        "event_id": row["event_id"],
                        "competition_id": row["competition_id"],
                        "competition_name": row["competition_name"],
                        "season_code": row["season_code"],
                        "division_code": row["division_code"],
                        "match_date": row["match_date"],
                        "kickoff_time": row["kickoff_time"],
                        "as_of": row["as_of"],
                        "home_team_id": row["home_team_id"],
                        "away_team_id": row["away_team_id"],
                        "gap_model_version_shots": row["gap_model_version_shots"],
                        "gap_model_version_shots_on_target": row["gap_model_version_shots_on_target"],
                        "gap_model_version_corners": row["gap_model_version_corners"],
                        "conversion_model_version": row["conversion_model_version"],
                        "scoreline_model_version": SCORELINE_MODEL_VERSION,
                        "day8_to_day9_interface_policy_id": DAY8_TO_DAY9_INTERFACE_POLICY_ID,
                        "gamma_handling_policy_id": GAMMA_HANDLING_POLICY_ID,
                        "expected_goals_home": row["expected_goals_home"],
                        "expected_goals_away": row["expected_goals_away"],
                        "expected_total_goals": row["expected_total_goals"],
                        "rho": float(selected_rho),
                        "xi": float(selected_xi),
                        "xi_selection_reason": xi_selection_reason,
                        "rho_selection_reason": rho_selection_reason,
                        "predicted_home_corners": row["predicted_home_corners"],
                        "predicted_away_corners": row["predicted_away_corners"],
                        "selected_corner_distribution": row["selected_corner_distribution"],
                        "corner_distribution_parameters": row["corner_distribution_parameters"],
                        "plain_poisson_draw_probability": plain_distribution["draw_probability"],
                        "dixon_coles_draw_probability": dc_distribution["draw_probability"],
                        "draw_probability_difference": (
                            dc_distribution["draw_probability"] - plain_distribution["draw_probability"]
                        ),
                        "scoreline_matrix_max_goals": MAX_GOALS,
                        "scoreline_matrix": dc_distribution["matrix"],
                        "truncated_tail_mass_beyond_10": plain_distribution["tail_mass_beyond_max_goals"],
                        "scoreline_matrix_raw_mass": dc_distribution["raw_matrix_mass"],
                        "scoreline_matrix_normalization_factor": dc_distribution["normalization_factor"],
                        "scoreline_matrix_normalization_error": dc_distribution["normalization_error"],
                        "full_distribution_mass_estimate": dc_distribution["full_distribution_mass_estimate"],
                        "full_distribution_mass_estimate_error": dc_distribution[
                            "full_distribution_mass_estimate_error"
                        ],
                        "matrix_marginal_home_mean": dc_distribution["marginal_home_mean"],
                        "matrix_marginal_away_mean": dc_distribution["marginal_away_mean"],
                        "plain_poisson_log_likelihood": plain_ll,
                        "dixon_coles_log_likelihood": dc_ll,
                        "log_likelihood_improvement": _safe_log_likelihood_improvement(dc_ll, plain_ll),
                        "sample_context": row["sample_context"],
                        "actual_home_goals": row["actual_home_goals"],
                        "actual_away_goals": row["actual_away_goals"],
                        "actual_total_goals": row["actual_total_goals"],
                        "actual_total_corners": row["actual_total_corners"],
                    }
                )

        cumulative_xi_ll: dict[float, float] = {float(xi): 0.0 for xi in TIME_DECAY_GRID}
        for fold_record in prior_xi_oos_records:
            for summary in fold_record["xi_candidate_summaries"]:
                cumulative_xi_ll[float(summary["candidate_xi"])] += float(summary["dixon_coles_log_likelihood"])

        best_overall_xi = min(
            cumulative_xi_ll,
            key=lambda candidate_xi: (-cumulative_xi_ll[candidate_xi], candidate_xi),
        )
        time_decay_summary.append(
            {
                "competition_id": competition_id,
                "competition_name": competition_name,
                "selected_xi": float(best_overall_xi),
                "selected_xi_reason": (
                    "selected by cumulative out-of-sample fold log likelihood with xi=0.0 as the simpler tie-break"
                ),
                "cumulative_out_of_sample_log_likelihood_by_xi": {
                    f"{candidate_xi:.4f}": cumulative_xi_ll[candidate_xi]
                    for candidate_xi in sorted(cumulative_xi_ll)
                },
                "fold_count": len(prior_xi_oos_records),
            }
        )
        final_rho_by_competition.append(
            {
                "competition_id": competition_id,
                "competition_name": competition_name,
                "selected_rho": float(latest_selected_rho),
                "selected_xi": float(latest_selected_xi),
                "latest_season_evaluated": seasons[-1],
                "pdf_reference_rho": PDF_REFERENCE_RHO,
                "gamma_handling": GAMMA_HANDLING_POLICY_ID,
            }
        )

    diagnostics = {
        "overall": _scoreline_diagnostics(prediction_rows),
        "by_competition": _group_prediction_rows_by_key(prediction_rows, key="competition_id"),
        "by_season": _group_prediction_rows_by_key(prediction_rows, key="season_code"),
        "by_fold": fold_records,
    }

    manifest = {
        "generated_at": _utc_now(),
        "experiment_id": EXPERIMENT_ID,
        "experiment_version": EXPERIMENT_VERSION,
        "source_research_db_path": str(Path(research_db_path)),
        "day7_gap_baseline_experiment_version": DAY7_BASELINE_EXPERIMENT_VERSION,
        "day8_experiment_version": DAY8_EXPERIMENT_VERSION,
        "day8_conversion_model_version": CONVERSION_MODEL_VERSION,
        "scoreline_model_version": SCORELINE_MODEL_VERSION,
        "day8_to_day9_interface_policy_id": DAY8_TO_DAY9_INTERFACE_POLICY_ID,
        "day8_to_day9_interface_decision": DAY8_TO_DAY9_INTERFACE_DECISION,
        "day8_to_day9_interface_justification": DAY8_TO_DAY9_INTERFACE_JUSTIFICATION,
        "gamma_handling_policy_id": GAMMA_HANDLING_POLICY_ID,
        "gamma_handling_description": GAMMA_HANDLING_DESCRIPTION,
        "rho_grid": list(RHO_GRID),
        "time_decay_grid": list(TIME_DECAY_GRID),
        "default_rho": DEFAULT_RHO,
        "default_xi": DEFAULT_XI,
        "time_decay_time_unit": "days",
        "max_goals": MAX_GOALS,
        "truncation_policy_id": TRUNCATION_POLICY_ID,
        "truncation_policy_description": TRUNCATION_POLICY_DESCRIPTION,
        "rho_fit_objective_id": RHO_FIT_OBJECTIVE_ID,
        "rho_fit_objective_description": RHO_FIT_OBJECTIVE_DESCRIPTION,
        "time_decay_selection_policy_id": TIME_DECAY_SELECTION_POLICY_ID,
        "time_decay_selection_policy_description": TIME_DECAY_SELECTION_POLICY_DESCRIPTION,
        "prediction_count": len(prediction_rows),
        "rho_variant_count": len(rho_variant_records),
        "competitions_processed": sorted({row["competition_id"] for row in prediction_rows}),
        "seasons_processed": sorted({row["season_code"] for row in prediction_rows}),
        "final_rho_by_competition": final_rho_by_competition,
        "time_decay_summary": time_decay_summary,
        "pdf_reference_sanity": {
            "gamma_approx": PDF_REFERENCE_GAMMA,
            "rho_approx": PDF_REFERENCE_RHO,
            "gamma_handling": "intentionally omitted in Day 9 to avoid double-counting Day 8 home/away effects",
        },
        "diagnostics": diagnostics,
    }
    return {
        "manifest": manifest,
        "predictions": prediction_rows,
        "rho_variant_records": rho_variant_records,
        "fold_records": fold_records,
        "day8_result": resolved_day8_result,
    }


def _render_markdown_summary(result: dict[str, Any]) -> str:
    manifest = result["manifest"]
    overall = manifest["diagnostics"]["overall"]
    rho_lines = [
        "| Competition | Final Rho | Selected Xi | Latest Season |",
        "| --- | ---: | ---: | --- |",
    ]
    for row in manifest["final_rho_by_competition"]:
        rho_lines.append(
            f"| {row['competition_id']} | {row['selected_rho']:+.2f} | {row['selected_xi']:.4f} | "
            f"{row['latest_season_evaluated']} |"
        )

    xi_lines = [
        "| Competition | Selected Xi | Cumulative OOS LL |",
        "| --- | ---: | --- |",
    ]
    for row in manifest["time_decay_summary"]:
        xi_lines.append(
            f"| {row['competition_id']} | {row['selected_xi']:.4f} | "
            f"{row['cumulative_out_of_sample_log_likelihood_by_xi']} |"
        )

    return "\n".join(
        [
            "# Football Day 9 Scoreline Model",
            "",
            f"Generated at: `{manifest['generated_at']}`",
            f"Experiment/version: `{manifest['experiment_id']}` / `{manifest['experiment_version']}`",
            "",
            "## Interface",
            "",
            f"- policy: `{manifest['day8_to_day9_interface_policy_id']}`",
            f"- gamma handling: `{manifest['gamma_handling_policy_id']}`",
            "",
            "## Overall Diagnostics",
            "",
            f"- plain Poisson log likelihood: `{_format_metric(overall['plain_poisson_log_likelihood'], '.4f')}`",
            f"- Dixon-Coles log likelihood: `{_format_metric(overall['dixon_coles_log_likelihood'], '.4f')}`",
            f"- improvement: `{_format_metric(overall['log_likelihood_improvement'], '.4f')}`",
            f"- average draw probability difference: `{_format_metric(overall['average_draw_probability_difference'], '.6f')}`",
            f"- average tail mass beyond 10: `{_format_metric(overall['average_tail_mass_beyond_10'], '.6f')}`",
            f"- max normalization error: `{_format_metric(overall['max_matrix_normalization_error'], '.12f')}`",
            "",
            "## Final Rho By Competition",
            "",
            *rho_lines,
            "",
            "## Time Decay Summary",
            "",
            *xi_lines,
        ]
    )


def _write_jsonl(path: str | Path, rows: Sequence[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True))
            handle.write("\n")


def _format_metric(value: Any, pattern: str) -> str:
    if value is None:
        return "null"
    return format(value, pattern)


def write_day9_artifacts(
    result: dict[str, Any],
    *,
    manifest_path: str | Path,
    predictions_jsonl_path: str | Path,
    rho_variants_path: str | Path,
    fold_records_path: str | Path,
    markdown_path: str | Path,
) -> dict[str, str]:
    manifest_target = Path(manifest_path)
    predictions_target = Path(predictions_jsonl_path)
    rho_variants_target = Path(rho_variants_path)
    fold_records_target = Path(fold_records_path)
    markdown_target = Path(markdown_path)
    for target in (
        manifest_target,
        predictions_target,
        rho_variants_target,
        fold_records_target,
        markdown_target,
    ):
        target.parent.mkdir(parents=True, exist_ok=True)

    manifest_target.write_text(json.dumps(result["manifest"], indent=2), encoding="utf-8")
    _write_jsonl(predictions_target, result["predictions"])
    rho_variants_target.write_text(json.dumps(result["rho_variant_records"], indent=2), encoding="utf-8")
    fold_records_target.write_text(json.dumps(result["fold_records"], indent=2), encoding="utf-8")
    markdown_target.write_text(_render_markdown_summary(result), encoding="utf-8")
    return {
        "manifest_path": str(manifest_target),
        "predictions_path": str(predictions_target),
        "rho_variants_path": str(rho_variants_target),
        "fold_records_path": str(fold_records_target),
        "markdown_path": str(markdown_target),
    }


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the Day 9 football scoreline model.")
    parser.add_argument("--research-db", required=True, help="Path to football research SQLite store.")
    parser.add_argument("--manifest-out", help="Optional manifest JSON path.")
    parser.add_argument("--predictions-out", help="Optional predictions JSONL path.")
    parser.add_argument("--rho-variants-out", help="Optional rho variants JSON path.")
    parser.add_argument("--fold-records-out", help="Optional fold records JSON path.")
    parser.add_argument("--markdown-out", help="Optional Markdown summary path.")
    args = parser.parse_args(argv)

    result = run_day9_scoreline_experiment(research_db_path=args.research_db)
    payload: dict[str, Any] = {
        "experiment_id": result["manifest"]["experiment_id"],
        "prediction_count": result["manifest"]["prediction_count"],
        "rho_variant_count": result["manifest"]["rho_variant_count"],
    }
    if (
        args.manifest_out
        and args.predictions_out
        and args.rho_variants_out
        and args.fold_records_out
        and args.markdown_out
    ):
        payload["artifacts"] = write_day9_artifacts(
            result,
            manifest_path=args.manifest_out,
            predictions_jsonl_path=args.predictions_out,
            rho_variants_path=args.rho_variants_out,
            fold_records_path=args.fold_records_out,
            markdown_path=args.markdown_out,
        )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
