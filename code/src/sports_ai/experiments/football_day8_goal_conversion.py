"""Day 8 football statistics-to-goals conversion and corner-distribution comparison.

This module consumes the frozen fitted Day 7 GAP outputs and adds:

- league-specific conversion from predicted shots / shots on target to expected goals
- chronological expected-goals replay by competition and season
- Poisson versus negative-binomial corner-distribution comparison

The Day 8 layer is model-only:

- no market odds
- no de-vig
- no edge or staking logic
- no mutation of GAP state
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Sequence

import numpy as np

from .football_gap_lambda_fitting import build_selected_gap_walkforward_results, fit_gap_learning_rates

EXPERIMENT_ID = "EXP-011"
EXPERIMENT_VERSION = "football_day8_goal_conversion_v1"
CONVERSION_MODEL_VERSION = "football_day8_goal_conversion_v1"
CONVERSION_MODEL_FORM = "league_and_venue_nonnegative_linear_shots_plus_sot_v1"
CONVERSION_FEATURE_DEFINITION = (
    "expected_goals_side = beta_shots * predicted_shots_side + beta_shots_on_target * predicted_shots_on_target_side; "
    "coefficients are fit separately by competition and venue with non-negative least squares and no intercept."
)
CORNER_COMPARISON_VERSION = "football_day8_corner_distribution_comparison_v1"
CORNER_SELECTION_POLICY_ID = "prior_out_of_sample_log_likelihood_v1"
CORNER_SELECTION_POLICY_DESCRIPTION = (
    "For each competition and season, select the corner distribution family using only previously completed "
    "out-of-sample fold evidence for that competition. Use cumulative validation log likelihood as the primary "
    "criterion and prefer Poisson on ties."
)
CORNER_DEFAULT_DISTRIBUTION = "poisson"
DAY7_BASELINE_EXPERIMENT_VERSION = "football_gap_lambda_fitting_v1"
RESOLVED_DEVELOPER_BRIEF_BLOCKER_ID = "developer_brief_pdf_present_supersedes_missing_markdown_mirror_v1"


@dataclass(frozen=True)
class Day8JoinedRow:
    event_id: str
    competition_id: str
    competition_name: str
    season_code: str
    division_code: str
    match_date: str
    kickoff_time: str | None
    as_of: str
    home_team_id: str
    away_team_id: str
    gap_model_version_shots: str
    gap_model_version_shots_on_target: str
    gap_model_version_corners: str
    predicted_home_shots: float
    predicted_away_shots: float
    predicted_home_shots_on_target: float
    predicted_away_shots_on_target: float
    predicted_home_corners: float
    predicted_away_corners: float
    home_team_pre_match_competition_matches: int
    away_team_pre_match_competition_matches: int
    actual_home_goals: int
    actual_away_goals: int
    actual_home_corners: int
    actual_away_corners: int

    @property
    def predicted_total_corners(self) -> float:
        return self.predicted_home_corners + self.predicted_away_corners

    @property
    def actual_total_corners(self) -> int:
        return self.actual_home_corners + self.actual_away_corners


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect_research_store(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _load_actual_outcomes(
    *,
    research_db_path: str | Path,
) -> dict[str, dict[str, Any]]:
    query = """
        SELECT
            event_id,
            home_goals,
            away_goals,
            home_corners,
            away_corners
        FROM football_events
        WHERE home_goals IS NOT NULL
          AND away_goals IS NOT NULL
          AND home_corners IS NOT NULL
          AND away_corners IS NOT NULL
    """
    with _connect_research_store(research_db_path) as conn:
        rows = conn.execute(query).fetchall()
    return {
        str(row["event_id"]): {
            "home_goals": int(row["home_goals"]),
            "away_goals": int(row["away_goals"]),
            "home_corners": int(row["home_corners"]),
            "away_corners": int(row["away_corners"]),
        }
        for row in rows
    }


def _load_day7_selected_gap_results(
    *,
    research_db_path: str | Path,
    fit_result: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    resolved_fit_result = fit_result or fit_gap_learning_rates(research_db_path=research_db_path)
    selected_gap_results = build_selected_gap_walkforward_results(
        research_db_path=research_db_path,
        fit_result=resolved_fit_result,
    )
    return resolved_fit_result, selected_gap_results


def _join_day7_predictions(
    *,
    research_db_path: str | Path,
    selected_gap_results: dict[str, dict[str, Any]],
) -> list[Day8JoinedRow]:
    actual_outcomes = _load_actual_outcomes(research_db_path=research_db_path)
    shots_rows = {
        row["event_id"]: row
        for row in selected_gap_results["shots"]["predictions"]
    }
    sot_rows = {
        row["event_id"]: row
        for row in selected_gap_results["shots_on_target"]["predictions"]
    }
    corner_rows = {
        row["event_id"]: row
        for row in selected_gap_results["corners"]["predictions"]
    }

    common_event_ids = sorted(set(shots_rows) & set(sot_rows) & set(corner_rows) & set(actual_outcomes))
    joined_rows: list[Day8JoinedRow] = []
    for event_id in common_event_ids:
        shots = shots_rows[event_id]
        sot = sot_rows[event_id]
        corners = corner_rows[event_id]
        actual = actual_outcomes[event_id]

        if not (
            shots["competition_id"] == sot["competition_id"] == corners["competition_id"]
            and shots["season_code"] == sot["season_code"] == corners["season_code"]
        ):
            raise ValueError(f"inconsistent joined Day 7 predictions for event {event_id!r}")

        joined_rows.append(
            Day8JoinedRow(
                event_id=event_id,
                competition_id=str(shots["competition_id"]),
                competition_name=str(shots["competition_name"]),
                season_code=str(shots["season_code"]),
                division_code=str(shots["division_code"]),
                match_date=str(shots["match_date"]),
                kickoff_time=str(shots["kickoff_time"]) if shots["kickoff_time"] is not None else None,
                as_of=str(shots["as_of"]),
                home_team_id=str(shots["home_team_id"]),
                away_team_id=str(shots["away_team_id"]),
                gap_model_version_shots=str(shots["model_version"]),
                gap_model_version_shots_on_target=str(sot["model_version"]),
                gap_model_version_corners=str(corners["model_version"]),
                predicted_home_shots=float(shots["expected_home"]),
                predicted_away_shots=float(shots["expected_away"]),
                predicted_home_shots_on_target=float(sot["expected_home"]),
                predicted_away_shots_on_target=float(sot["expected_away"]),
                predicted_home_corners=float(corners["expected_home"]),
                predicted_away_corners=float(corners["expected_away"]),
                home_team_pre_match_competition_matches=int(shots["home_team_pre_match_competition_matches"]),
                away_team_pre_match_competition_matches=int(shots["away_team_pre_match_competition_matches"]),
                actual_home_goals=int(actual["home_goals"]),
                actual_away_goals=int(actual["away_goals"]),
                actual_home_corners=int(actual["home_corners"]),
                actual_away_corners=int(actual["away_corners"]),
            )
        )
    return sorted(
        joined_rows,
        key=lambda row: (row.competition_id, row.match_date, row.kickoff_time or "", row.event_id),
    )


def _group_rows_by_competition(rows: Iterable[Day8JoinedRow]) -> dict[str, list[Day8JoinedRow]]:
    grouped: dict[str, list[Day8JoinedRow]] = defaultdict(list)
    for row in rows:
        grouped[row.competition_id].append(row)
    return {
        competition_id: sorted(group_rows, key=lambda row: (row.match_date, row.kickoff_time or "", row.event_id))
        for competition_id, group_rows in sorted(grouped.items())
    }


def _season_codes(rows: Sequence[Day8JoinedRow]) -> list[str]:
    return sorted({row.season_code for row in rows})


def _rows_for_seasons(rows: Sequence[Day8JoinedRow], season_codes: Sequence[str]) -> list[Day8JoinedRow]:
    allowed = set(season_codes)
    return [row for row in rows if row.season_code in allowed]


def _nonnegative_linear_fit(
    *,
    feature_rows: Sequence[tuple[float, float]],
    targets: Sequence[float],
) -> dict[str, float]:
    if len(feature_rows) != len(targets):
        raise ValueError("feature rows and targets must have equal length")
    if not feature_rows:
        raise ValueError("at least one training row is required")

    x = np.asarray(feature_rows, dtype=np.float64)
    y = np.asarray(targets, dtype=np.float64)
    xtx = x.T @ x
    xty = x.T @ y

    candidates: list[tuple[float, float]] = []

    try:
        full = np.linalg.solve(xtx, xty)
        if np.all(full >= 0):
            candidates.append((float(full[0]), float(full[1])))
    except np.linalg.LinAlgError:
        pass

    for index in (0, 1):
        denominator = float(xtx[index, index])
        coefficient = 0.0 if denominator <= 0 else max(0.0, float(xty[index] / denominator))
        if index == 0:
            candidates.append((coefficient, 0.0))
        else:
            candidates.append((0.0, coefficient))

    candidates.append((0.0, 0.0))

    best = min(
        candidates,
        key=lambda pair: float(np.square(y - (x @ np.asarray(pair, dtype=np.float64))).sum()),
    )
    return {
        "beta_shots": float(best[0]),
        "beta_shots_on_target": float(best[1]),
    }


def _predict_expected_goals(
    *,
    coefficients: dict[str, float],
    predicted_shots: float,
    predicted_shots_on_target: float,
) -> float:
    value = (
        coefficients["beta_shots"] * predicted_shots
        + coefficients["beta_shots_on_target"] * predicted_shots_on_target
    )
    return float(max(0.0, value))


def _fit_conversion_for_training_rows(
    *,
    competition_id: str,
    training_rows: Sequence[Day8JoinedRow],
    current_season: str,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, float]]]:
    home_features = [
        (row.predicted_home_shots, row.predicted_home_shots_on_target)
        for row in training_rows
    ]
    away_features = [
        (row.predicted_away_shots, row.predicted_away_shots_on_target)
        for row in training_rows
    ]
    home_targets = [float(row.actual_home_goals) for row in training_rows]
    away_targets = [float(row.actual_away_goals) for row in training_rows]
    home_coefficients = _nonnegative_linear_fit(feature_rows=home_features, targets=home_targets)
    away_coefficients = _nonnegative_linear_fit(feature_rows=away_features, targets=away_targets)
    as_of = max(row.as_of for row in training_rows)
    season_codes = _season_codes(training_rows)
    records = [
        {
            "variant_id": f"{competition_id.lower()}_{current_season}_home_conversion_fixed_form_v1",
            "competition_id": competition_id,
            "venue": "home",
            "feature_definition": CONVERSION_FEATURE_DEFINITION,
            "train_period": {
                "start_season": season_codes[0],
                "end_season": season_codes[-1],
                "season_codes": season_codes,
            },
            "validation_period": {
                "start_season": current_season,
                "end_season": current_season,
                "season_codes": [current_season],
            },
            "model_version": CONVERSION_MODEL_VERSION,
            "as_of": as_of,
            "coefficients": home_coefficients,
            "sample_size": len(training_rows),
        },
        {
            "variant_id": f"{competition_id.lower()}_{current_season}_away_conversion_fixed_form_v1",
            "competition_id": competition_id,
            "venue": "away",
            "feature_definition": CONVERSION_FEATURE_DEFINITION,
            "train_period": {
                "start_season": season_codes[0],
                "end_season": season_codes[-1],
                "season_codes": season_codes,
            },
            "validation_period": {
                "start_season": current_season,
                "end_season": current_season,
                "season_codes": [current_season],
            },
            "model_version": CONVERSION_MODEL_VERSION,
            "as_of": as_of,
            "coefficients": away_coefficients,
            "sample_size": len(training_rows),
        },
    ]
    return records, {"home": home_coefficients, "away": away_coefficients}


def _log_poisson_pmf(k: int, mean_value: float) -> float:
    if mean_value <= 0:
        return 0.0 if k == 0 else float("-inf")
    return (k * math.log(mean_value)) - mean_value - math.lgamma(k + 1.0)


def _fit_nb_dispersion(
    *,
    rows: Sequence[Day8JoinedRow],
) -> float | None:
    if not rows:
        return None
    means = np.asarray([row.predicted_total_corners for row in rows], dtype=np.float64)
    observed = np.asarray([row.actual_total_corners for row in rows], dtype=np.float64)
    numerator = float(np.mean(means ** 2))
    delta = float(np.mean(((observed - means) ** 2) - observed))
    if delta <= 1e-12 or numerator <= 0:
        return None
    dispersion = numerator / delta
    if not math.isfinite(dispersion) or dispersion <= 0:
        return None
    return float(dispersion)


def _log_negative_binomial_pmf(k: int, mean_value: float, dispersion: float | None) -> float:
    if dispersion is None:
        return _log_poisson_pmf(k, mean_value)
    if mean_value <= 0:
        return 0.0 if k == 0 else float("-inf")
    r = dispersion
    p = r / (r + mean_value)
    return (
        math.lgamma(k + r)
        - math.lgamma(r)
        - math.lgamma(k + 1.0)
        + (r * math.log(p))
        + (k * math.log(1.0 - p))
    )


def _distribution_metrics(
    *,
    rows: Sequence[Day8JoinedRow],
    distribution_name: str,
    dispersion: float | None,
) -> dict[str, Any]:
    if not rows:
        return {
            "distribution": distribution_name,
            "sample_size": 0,
            "mean_predicted_total_corners": None,
            "mean_observed_total_corners": None,
            "mean_predicted_variance": None,
            "observed_total_corner_variance": None,
            "variance_gap": None,
            "total_log_likelihood": None,
            "average_log_likelihood": None,
        }
    means = [row.predicted_total_corners for row in rows]
    observed = [row.actual_total_corners for row in rows]
    if distribution_name == "poisson":
        log_likelihoods = [
            _log_poisson_pmf(y, mu)
            for y, mu in zip(observed, means)
        ]
        predicted_variances = means
    elif distribution_name == "negative_binomial":
        log_likelihoods = [
            _log_negative_binomial_pmf(y, mu, dispersion)
            for y, mu in zip(observed, means)
        ]
        predicted_variances = [
            mu if dispersion is None else mu + ((mu ** 2) / dispersion)
            for mu in means
        ]
    else:
        raise ValueError(f"unsupported distribution {distribution_name!r}")
    total_log_likelihood = float(sum(log_likelihoods))
    observed_variance = float(np.var(np.asarray(observed, dtype=np.float64)))
    mean_predicted_variance = float(mean(predicted_variances))
    return {
        "distribution": distribution_name,
        "sample_size": len(rows),
        "mean_predicted_total_corners": float(mean(means)),
        "mean_observed_total_corners": float(mean(observed)),
        "mean_predicted_variance": mean_predicted_variance,
        "observed_total_corner_variance": observed_variance,
        "variance_gap": abs(mean_predicted_variance - observed_variance),
        "total_log_likelihood": total_log_likelihood,
        "average_log_likelihood": total_log_likelihood / len(rows),
        "dispersion": dispersion,
    }


def _goal_diagnostics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "prediction_count": 0,
            "mean_predicted_home_goals": None,
            "mean_observed_home_goals": None,
            "mean_predicted_away_goals": None,
            "mean_observed_away_goals": None,
            "mean_predicted_total_goals": None,
            "mean_observed_total_goals": None,
            "mae_home_goals": None,
            "mae_away_goals": None,
            "mae_total_goals": None,
            "bias_home_goals": None,
            "bias_away_goals": None,
            "bias_total_goals": None,
            "season_total_gap": None,
        }
    predicted_home = [float(row["expected_goals_home"]) for row in rows]
    observed_home = [float(row["actual_home_goals"]) for row in rows]
    predicted_away = [float(row["expected_goals_away"]) for row in rows]
    observed_away = [float(row["actual_away_goals"]) for row in rows]
    predicted_total = [float(row["expected_total_goals"]) for row in rows]
    observed_total = [float(row["actual_total_goals"]) for row in rows]
    return {
        "prediction_count": len(rows),
        "mean_predicted_home_goals": float(mean(predicted_home)),
        "mean_observed_home_goals": float(mean(observed_home)),
        "mean_predicted_away_goals": float(mean(predicted_away)),
        "mean_observed_away_goals": float(mean(observed_away)),
        "mean_predicted_total_goals": float(mean(predicted_total)),
        "mean_observed_total_goals": float(mean(observed_total)),
        "mae_home_goals": float(mean(abs(left - right) for left, right in zip(predicted_home, observed_home))),
        "mae_away_goals": float(mean(abs(left - right) for left, right in zip(predicted_away, observed_away))),
        "mae_total_goals": float(mean(abs(left - right) for left, right in zip(predicted_total, observed_total))),
        "bias_home_goals": float(mean(left - right for left, right in zip(predicted_home, observed_home))),
        "bias_away_goals": float(mean(left - right for left, right in zip(predicted_away, observed_away))),
        "bias_total_goals": float(mean(left - right for left, right in zip(predicted_total, observed_total))),
        "season_total_gap": float(sum(predicted_total) - sum(observed_total)),
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
        group_key: _goal_diagnostics(group_rows)
        for group_key, group_rows in sorted(grouped.items())
    }


def run_day8_goal_conversion_experiment(
    *,
    research_db_path: str | Path,
    fit_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_fit_result, selected_gap_results = _load_day7_selected_gap_results(
        research_db_path=research_db_path,
        fit_result=fit_result,
    )
    joined_rows = _join_day7_predictions(
        research_db_path=research_db_path,
        selected_gap_results=selected_gap_results,
    )
    by_competition = _group_rows_by_competition(joined_rows)

    prediction_rows: list[dict[str, Any]] = []
    conversion_fit_records: list[dict[str, Any]] = []
    corner_fold_records: list[dict[str, Any]] = []
    corner_selection_summary: list[dict[str, Any]] = []

    for competition_id, competition_rows in by_competition.items():
        competition_name = competition_rows[0].competition_name
        seasons = _season_codes(competition_rows)
        if len(seasons) < 2:
            raise ValueError(f"competition {competition_id} does not have enough seasons for Day 8 replay")

        prior_corner_oos_records: list[dict[str, Any]] = []
        for season_index in range(1, len(seasons)):
            current_season = seasons[season_index]
            training_seasons = seasons[:season_index]
            training_rows = _rows_for_seasons(competition_rows, training_seasons)
            season_rows = _rows_for_seasons(competition_rows, [current_season])
            if not training_rows or not season_rows:
                continue

            fit_records, coefficients_by_venue = _fit_conversion_for_training_rows(
                competition_id=competition_id,
                training_rows=training_rows,
                current_season=current_season,
            )
            conversion_fit_records.extend(fit_records)

            if not prior_corner_oos_records:
                selected_distribution = CORNER_DEFAULT_DISTRIBUTION
                corner_selection_reason = (
                    "first evaluation season for this competition, so select Poisson as the simpler default "
                    "before any prior out-of-sample comparison exists"
                )
            else:
                cumulative_poisson_ll = sum(
                    record["poisson_metrics"]["total_log_likelihood"]
                    for record in prior_corner_oos_records
                )
                cumulative_nb_ll = sum(
                    record["negative_binomial_metrics"]["total_log_likelihood"]
                    for record in prior_corner_oos_records
                )
                if cumulative_nb_ll > cumulative_poisson_ll:
                    selected_distribution = "negative_binomial"
                    corner_selection_reason = (
                        "selected negative binomial because prior out-of-sample cumulative log likelihood "
                        f"{cumulative_nb_ll:.6f} exceeded Poisson {cumulative_poisson_ll:.6f}"
                    )
                else:
                    selected_distribution = "poisson"
                    corner_selection_reason = (
                        "selected Poisson because it matched or beat prior out-of-sample negative-binomial "
                        f"log likelihood ({cumulative_poisson_ll:.6f} vs {cumulative_nb_ll:.6f})"
                    )

            current_dispersion = _fit_nb_dispersion(rows=training_rows)
            for row in season_rows:
                expected_goals_home = _predict_expected_goals(
                    coefficients=coefficients_by_venue["home"],
                    predicted_shots=row.predicted_home_shots,
                    predicted_shots_on_target=row.predicted_home_shots_on_target,
                )
                expected_goals_away = _predict_expected_goals(
                    coefficients=coefficients_by_venue["away"],
                    predicted_shots=row.predicted_away_shots,
                    predicted_shots_on_target=row.predicted_away_shots_on_target,
                )
                prediction_rows.append(
                    {
                        "event_id": row.event_id,
                        "competition_id": row.competition_id,
                        "competition_name": row.competition_name,
                        "season_code": row.season_code,
                        "division_code": row.division_code,
                        "match_date": row.match_date,
                        "kickoff_time": row.kickoff_time,
                        "as_of": row.as_of,
                        "home_team_id": row.home_team_id,
                        "away_team_id": row.away_team_id,
                        "gap_model_version_shots": row.gap_model_version_shots,
                        "gap_model_version_shots_on_target": row.gap_model_version_shots_on_target,
                        "gap_model_version_corners": row.gap_model_version_corners,
                        "conversion_model_version": CONVERSION_MODEL_VERSION,
                        "predicted_home_shots": row.predicted_home_shots,
                        "predicted_away_shots": row.predicted_away_shots,
                        "predicted_home_shots_on_target": row.predicted_home_shots_on_target,
                        "predicted_away_shots_on_target": row.predicted_away_shots_on_target,
                        "predicted_home_corners": row.predicted_home_corners,
                        "predicted_away_corners": row.predicted_away_corners,
                        "expected_goals_home": expected_goals_home,
                        "expected_goals_away": expected_goals_away,
                        "expected_total_goals": expected_goals_home + expected_goals_away,
                        "selected_corner_distribution": selected_distribution,
                        "corner_distribution_parameters": {
                            "dispersion": current_dispersion,
                        },
                        "sample_context": {
                            "home_team_pre_match_competition_matches": row.home_team_pre_match_competition_matches,
                            "away_team_pre_match_competition_matches": row.away_team_pre_match_competition_matches,
                        },
                        "actual_home_goals": row.actual_home_goals,
                        "actual_away_goals": row.actual_away_goals,
                        "actual_total_goals": row.actual_home_goals + row.actual_away_goals,
                        "actual_total_corners": row.actual_total_corners,
                    }
                )

            poisson_metrics = _distribution_metrics(
                rows=season_rows,
                distribution_name="poisson",
                dispersion=None,
            )
            negative_binomial_metrics = _distribution_metrics(
                rows=season_rows,
                distribution_name="negative_binomial",
                dispersion=current_dispersion,
            )
            fold_record = {
                "competition_id": competition_id,
                "competition_name": competition_name,
                "season_code": current_season,
                "train_period": {
                    "start_season": training_seasons[0],
                    "end_season": training_seasons[-1],
                    "season_codes": training_seasons,
                },
                "validation_period": {
                    "start_season": current_season,
                    "end_season": current_season,
                    "season_codes": [current_season],
                },
                "selection_used_for_predictions": selected_distribution,
                "selection_reason": corner_selection_reason,
                "poisson_metrics": poisson_metrics,
                "negative_binomial_metrics": negative_binomial_metrics,
                "dispersion_fitted_from_training": current_dispersion,
                "comparison_version": CORNER_COMPARISON_VERSION,
            }
            corner_fold_records.append(fold_record)
            prior_corner_oos_records.append(fold_record)

        cumulative_poisson_ll = sum(
            record["poisson_metrics"]["total_log_likelihood"]
            for record in prior_corner_oos_records
        )
        cumulative_nb_ll = sum(
            record["negative_binomial_metrics"]["total_log_likelihood"]
            for record in prior_corner_oos_records
        )
        overall_selected_distribution = (
            "negative_binomial" if cumulative_nb_ll > cumulative_poisson_ll else "poisson"
        )
        corner_selection_summary.append(
            {
                "competition_id": competition_id,
                "competition_name": competition_name,
                "selected_distribution": overall_selected_distribution,
                "selection_policy_id": CORNER_SELECTION_POLICY_ID,
                "selection_policy_description": CORNER_SELECTION_POLICY_DESCRIPTION,
                "cumulative_poisson_log_likelihood": cumulative_poisson_ll,
                "cumulative_negative_binomial_log_likelihood": cumulative_nb_ll,
                "fold_count": len(prior_corner_oos_records),
            }
        )

    diagnostics = {
        "overall": _goal_diagnostics(prediction_rows),
        "by_competition": _group_prediction_rows_by_key(prediction_rows, key="competition_id"),
        "by_season": _group_prediction_rows_by_key(prediction_rows, key="season_code"),
    }

    manifest = {
        "generated_at": _utc_now(),
        "experiment_id": EXPERIMENT_ID,
        "experiment_version": EXPERIMENT_VERSION,
        "source_research_db_path": str(Path(research_db_path)),
        "day7_gap_baseline_experiment_version": DAY7_BASELINE_EXPERIMENT_VERSION,
        "day7_fit_result_generated_at": resolved_fit_result["generated_at"],
        "conversion_model_version": CONVERSION_MODEL_VERSION,
        "conversion_model_form": CONVERSION_MODEL_FORM,
        "conversion_feature_definition": CONVERSION_FEATURE_DEFINITION,
        "conversion_variant_count": len(conversion_fit_records),
        "corner_comparison_version": CORNER_COMPARISON_VERSION,
        "corner_selection_policy_id": CORNER_SELECTION_POLICY_ID,
        "corner_selection_policy_description": CORNER_SELECTION_POLICY_DESCRIPTION,
        "prediction_count": len(prediction_rows),
        "competitions_processed": sorted({row["competition_id"] for row in prediction_rows}),
        "seasons_processed": sorted({row["season_code"] for row in prediction_rows}),
        "resolved_developer_brief_blocker_id": RESOLVED_DEVELOPER_BRIEF_BLOCKER_ID,
        "diagnostics": diagnostics,
        "corner_selection_summary": corner_selection_summary,
    }
    return {
        "manifest": manifest,
        "predictions": prediction_rows,
        "conversion_fit_records": conversion_fit_records,
        "corner_fold_records": corner_fold_records,
        "selected_gap_results": selected_gap_results,
        "fit_result": resolved_fit_result,
    }


def _render_markdown_summary(result: dict[str, Any]) -> str:
    manifest = result["manifest"]
    overall = manifest["diagnostics"]["overall"]
    selection_lines = [
        "| Competition | Selected Distribution | Cum. Poisson LL | Cum. NB LL | Folds |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for row in manifest["corner_selection_summary"]:
        selection_lines.append(
            f"| {row['competition_id']} | {row['selected_distribution']} | "
            f"{row['cumulative_poisson_log_likelihood']:.4f} | {row['cumulative_negative_binomial_log_likelihood']:.4f} | "
            f"{row['fold_count']} |"
        )
    return "\n".join(
        [
            "# Football Day 8 Goal Conversion",
            "",
            f"Generated at: `{manifest['generated_at']}`",
            f"Experiment/version: `{manifest['experiment_id']}` / `{manifest['experiment_version']}`",
            "",
            "## Goal Conversion",
            "",
            f"- conversion model version: `{manifest['conversion_model_version']}`",
            f"- conversion form: `{manifest['conversion_model_form']}`",
            f"- conversion variant count: `{manifest['conversion_variant_count']}`",
            "",
            "## Overall Goal Diagnostics",
            "",
            f"- mean predicted home goals: `{overall['mean_predicted_home_goals']:.4f}`",
            f"- mean observed home goals: `{overall['mean_observed_home_goals']:.4f}`",
            f"- mean predicted away goals: `{overall['mean_predicted_away_goals']:.4f}`",
            f"- mean observed away goals: `{overall['mean_observed_away_goals']:.4f}`",
            f"- mean predicted total goals: `{overall['mean_predicted_total_goals']:.4f}`",
            f"- mean observed total goals: `{overall['mean_observed_total_goals']:.4f}`",
            f"- total-goal MAE: `{overall['mae_total_goals']:.4f}`",
            f"- total-goal bias: `{overall['bias_total_goals']:.4f}`",
            "",
            "## Corner Distribution Selection",
            "",
            *selection_lines,
        ]
    )


def _write_jsonl(path: str | Path, rows: Sequence[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True))
            handle.write("\n")


def write_day8_artifacts(
    result: dict[str, Any],
    *,
    manifest_path: str | Path,
    predictions_jsonl_path: str | Path,
    conversion_records_path: str | Path,
    corner_records_path: str | Path,
    markdown_path: str | Path,
) -> dict[str, str]:
    manifest_target = Path(manifest_path)
    predictions_target = Path(predictions_jsonl_path)
    conversion_target = Path(conversion_records_path)
    corner_target = Path(corner_records_path)
    markdown_target = Path(markdown_path)
    for target in (manifest_target, predictions_target, conversion_target, corner_target, markdown_target):
        target.parent.mkdir(parents=True, exist_ok=True)

    manifest_target.write_text(json.dumps(result["manifest"], indent=2), encoding="utf-8")
    _write_jsonl(predictions_target, result["predictions"])
    conversion_target.write_text(json.dumps(result["conversion_fit_records"], indent=2), encoding="utf-8")
    corner_target.write_text(json.dumps(result["corner_fold_records"], indent=2), encoding="utf-8")
    markdown_target.write_text(_render_markdown_summary(result), encoding="utf-8")
    return {
        "manifest_path": str(manifest_target),
        "predictions_jsonl_path": str(predictions_target),
        "conversion_records_path": str(conversion_target),
        "corner_records_path": str(corner_target),
        "markdown_path": str(markdown_target),
    }


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the Day 8 football goal-conversion experiment.")
    parser.add_argument("--research-db", required=True, help="Path to football research SQLite store.")
    parser.add_argument("--manifest-out", help="Optional manifest JSON path.")
    parser.add_argument("--predictions-out", help="Optional predictions JSONL path.")
    parser.add_argument("--conversion-records-out", help="Optional conversion records JSON path.")
    parser.add_argument("--corner-records-out", help="Optional corner comparison records JSON path.")
    parser.add_argument("--markdown-out", help="Optional Markdown summary path.")
    args = parser.parse_args(argv)

    result = run_day8_goal_conversion_experiment(research_db_path=args.research_db)
    payload: dict[str, Any] = {
        "manifest": result["manifest"],
    }
    if (
        args.manifest_out
        and args.predictions_out
        and args.conversion_records_out
        and args.corner_records_out
        and args.markdown_out
    ):
        payload["artifacts"] = write_day8_artifacts(
            result,
            manifest_path=args.manifest_out,
            predictions_jsonl_path=args.predictions_out,
            conversion_records_path=args.conversion_records_out,
            corner_records_path=args.corner_records_out,
            markdown_path=args.markdown_out,
        )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
