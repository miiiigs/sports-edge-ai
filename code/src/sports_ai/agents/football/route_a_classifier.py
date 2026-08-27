"""Football Route A direct classification built from GAP shots and corners."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

import numpy as np

from ...evaluation.metrics import brier, calibration_bins, calibration_slope, log_loss
from ...experiments.football_day8_goal_conversion import _join_day7_predictions, _load_day7_selected_gap_results
from ...locking import canonical_hash

ROUTE_A_AGENT_ID = "ROUTE_A_DIRECT_CLASSIFIER"
ROUTE_A_AGENT_NAME = "Route A Direct Classifier"
ROUTE_A_METHOD_FAMILY = "GAP_DIRECT_CLASSIFICATION"
ROUTE_A_ARTIFACT_VERSION = "football_route_a_direct_classifier_bundle_v1"
ROUTE_A_FEATURE_VERSION = "route_a_gap_shots_corners_v1"
ROUTE_A_MODEL_FAMILY = "LOGISTIC_REGRESSION"
ROUTE_A_BASELINE_NAME = "LEAGUE_BASE_RATE"
ROUTE_A_CONSENSUS_CONFIG_ID = "football_route_a_b_consensus"
ROUTE_A_CONSENSUS_VERSION = "FOOTBALL_ROUTE_A_B_V1"
ROUTE_A_CONSENSUS_VALID_FROM = "2026-08-27T00:00:00+00:00"
ROUTE_A_AGENT_VERSION_TAG = "ROUTE_A_LOGISTIC_V1"
ROUTE_A_MIN_HISTORY_SEASONS = 1
ROUTE_A_MIN_CALIBRATION_HISTORY_SEASONS = 2
ROUTE_A_MIN_ISOTONIC_ROWS = 500
ROUTE_A_NUMERIC_FEATURES = (
    "predicted_home_shots",
    "predicted_away_shots",
    "predicted_home_corners",
    "predicted_away_corners",
    "predicted_total_shots",
    "predicted_shot_difference",
    "predicted_total_corners",
    "predicted_corner_difference",
)
ROUTE_A_COMPETITION_FEATURE = "competition_id"
ROUTE_A_CONFIGURATION_COUNT = 6
_EPS = 1e-12

MARKET_KEY_BTTS = "btts"
MARKET_KEY_OU25 = "goals_total_2.5"
SUPPORTED_MARKETS = (
    MARKET_KEY_BTTS,
    MARKET_KEY_OU25,
)
MARKET_DEFINITIONS = {
    MARKET_KEY_BTTS: {
        "market_family": "btts",
        "line": None,
        "positive_selection": "YES",
        "negative_selection": "NO",
        "model_version": "ROUTE_A_LOGISTIC_V1_BTTS",
        "target_field": "btts_target",
    },
    MARKET_KEY_OU25: {
        "market_family": "goals_total",
        "line": 2.5,
        "positive_selection": "OVER",
        "negative_selection": "UNDER",
        "model_version": "ROUTE_A_LOGISTIC_V1_OU25",
        "target_field": "over_2_5_target",
    },
}


@dataclass(frozen=True)
class RouteATrainingRow:
    event_id: str
    competition_id: str
    competition_name: str
    season_code: str
    division_code: str
    match_date: str
    kickoff_time: str | None
    kickoff_at: str
    as_of: str
    predicted_home_shots: float
    predicted_away_shots: float
    predicted_home_corners: float
    predicted_away_corners: float
    home_team_pre_match_competition_matches: int
    away_team_pre_match_competition_matches: int
    actual_home_goals: int
    actual_away_goals: int

    @property
    def predicted_total_shots(self) -> float:
        return self.predicted_home_shots + self.predicted_away_shots

    @property
    def predicted_shot_difference(self) -> float:
        return self.predicted_home_shots - self.predicted_away_shots

    @property
    def predicted_total_corners(self) -> float:
        return self.predicted_home_corners + self.predicted_away_corners

    @property
    def predicted_corner_difference(self) -> float:
        return self.predicted_home_corners - self.predicted_away_corners

    @property
    def btts_target(self) -> int:
        return int(self.actual_home_goals >= 1 and self.actual_away_goals >= 1)

    @property
    def over_2_5_target(self) -> int:
        return int((self.actual_home_goals + self.actual_away_goals) > 2.5)

    def feature_dict(self) -> dict[str, Any]:
        return {
            "competition_id": self.competition_id,
            "predicted_home_shots": self.predicted_home_shots,
            "predicted_away_shots": self.predicted_away_shots,
            "predicted_home_corners": self.predicted_home_corners,
            "predicted_away_corners": self.predicted_away_corners,
            "predicted_total_shots": self.predicted_total_shots,
            "predicted_shot_difference": self.predicted_shot_difference,
            "predicted_total_corners": self.predicted_total_corners,
            "predicted_corner_difference": self.predicted_corner_difference,
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _kickoff_at(match_date: str, kickoff_time: str | None) -> str:
    return f"{match_date}T{kickoff_time or '00:00'}:00+00:00"


def _parse_utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def build_route_a_training_rows(
    *,
    research_db_path: str,
    fit_result: dict[str, Any] | None = None,
) -> list[RouteATrainingRow]:
    resolved_fit_result, selected_gap_results = _load_day7_selected_gap_results(
        research_db_path=research_db_path,
        fit_result=fit_result,
    )
    _ = resolved_fit_result
    joined_rows = _join_day7_predictions(
        research_db_path=research_db_path,
        selected_gap_results=selected_gap_results,
    )
    training_rows = [
        RouteATrainingRow(
            event_id=str(row.event_id),
            competition_id=str(row.competition_id),
            competition_name=str(row.competition_name),
            season_code=str(row.season_code),
            division_code=str(row.division_code),
            match_date=str(row.match_date),
            kickoff_time=None if row.kickoff_time is None else str(row.kickoff_time),
            kickoff_at=_kickoff_at(str(row.match_date), None if row.kickoff_time is None else str(row.kickoff_time)),
            as_of=str(row.as_of),
            predicted_home_shots=float(row.predicted_home_shots),
            predicted_away_shots=float(row.predicted_away_shots),
            predicted_home_corners=float(row.predicted_home_corners),
            predicted_away_corners=float(row.predicted_away_corners),
            home_team_pre_match_competition_matches=int(row.home_team_pre_match_competition_matches),
            away_team_pre_match_competition_matches=int(row.away_team_pre_match_competition_matches),
            actual_home_goals=int(row.actual_home_goals),
            actual_away_goals=int(row.actual_away_goals),
        )
        for row in joined_rows
    ]
    for row in training_rows:
        if _parse_utc_timestamp(row.as_of) > _parse_utc_timestamp(row.kickoff_at):
            raise ValueError(f"Route A chronology violation for event_id={row.event_id!r}: as_of exceeds kickoff")
    return sorted(training_rows, key=lambda row: (row.match_date, row.kickoff_time or "", row.event_id))


def expected_calibration_error(probs: Sequence[float], outcomes: Sequence[int], *, n_bins: int = 10) -> float:
    if len(probs) == 0:
        return float("nan")
    total = 0.0
    for bucket in calibration_bins(list(probs), list(outcomes), n_bins=n_bins):
        if not bucket["n"]:
            continue
        total += (bucket["n"] / len(probs)) * abs(float(bucket["mean_pred"]) - float(bucket["observed"]))
    return total


def _sigmoid(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, -35.0, 35.0)
    return 1.0 / (1.0 + np.exp(-clipped))


def _safe_probability(value: float) -> float:
    return float(min(1.0 - 1e-9, max(1e-9, value)))


def _fit_logistic_regression(
    x_matrix: np.ndarray,
    y_vector: np.ndarray,
    *,
    l2_regularization: float = 1.0,
    max_iterations: int = 100,
    tolerance: float = 1e-8,
) -> np.ndarray:
    if len(x_matrix) != len(y_vector):
        raise ValueError("x_matrix and y_vector must have equal length")
    if len(x_matrix) == 0:
        raise ValueError("at least one row is required")
    if len(set(int(value) for value in y_vector.tolist())) < 2:
        raise ValueError("logistic regression requires both classes")

    beta = np.zeros(x_matrix.shape[1], dtype=np.float64)
    identity = np.eye(x_matrix.shape[1], dtype=np.float64)
    identity[0, 0] = 0.0
    for _ in range(max_iterations):
        probabilities = _sigmoid(x_matrix @ beta)
        weights = np.clip(probabilities * (1.0 - probabilities), 1e-9, None)
        gradient = (x_matrix.T @ (probabilities - y_vector)) + (l2_regularization * (identity @ beta))
        hessian = ((x_matrix.T * weights) @ x_matrix) + (l2_regularization * identity)
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError:
            step = np.linalg.pinv(hessian) @ gradient
        next_beta = beta - step
        if float(np.max(np.abs(next_beta - beta))) <= tolerance:
            beta = next_beta
            break
        beta = next_beta
    return beta


def _predict_logistic_probabilities(x_matrix: np.ndarray, coefficients: np.ndarray) -> np.ndarray:
    return _sigmoid(x_matrix @ coefficients)


def _fit_platt_scaler(
    probabilities: Sequence[float],
    outcomes: Sequence[int],
) -> dict[str, float]:
    probs = np.asarray([_safe_probability(float(value)) for value in probabilities], dtype=np.float64)
    y_vector = np.asarray([int(value) for value in outcomes], dtype=np.float64)
    logits = np.log(probs / (1.0 - probs))
    x_matrix = np.column_stack([np.ones(len(logits), dtype=np.float64), logits])
    beta = _fit_logistic_regression(x_matrix, y_vector, l2_regularization=1e-6)
    return {
        "method": "platt",
        "intercept": float(beta[0]),
        "slope": float(beta[1]),
    }


def _apply_platt_scaler(probabilities: Sequence[float], calibrator: dict[str, Any]) -> list[float]:
    probs = np.asarray([_safe_probability(float(value)) for value in probabilities], dtype=np.float64)
    logits = np.log(probs / (1.0 - probs))
    scaled = calibrator["intercept"] + (calibrator["slope"] * logits)
    return [float(value) for value in _sigmoid(scaled)]


def _fit_isotonic_regression(
    probabilities: Sequence[float],
    outcomes: Sequence[int],
) -> dict[str, Any]:
    pairs = sorted(
        ((_safe_probability(float(probability)), int(outcome)) for probability, outcome in zip(probabilities, outcomes)),
        key=lambda item: item[0],
    )
    if not pairs:
        raise ValueError("isotonic calibration requires at least one row")

    blocks = [
        {
            "lower": probability,
            "upper": probability,
            "weight": 1.0,
            "sum_outcomes": float(outcome),
        }
        for probability, outcome in pairs
    ]
    index = 0
    while index < len(blocks) - 1:
        left = blocks[index]
        right = blocks[index + 1]
        left_mean = left["sum_outcomes"] / left["weight"]
        right_mean = right["sum_outcomes"] / right["weight"]
        if left_mean <= right_mean + 1e-15:
            index += 1
            continue
        merged = {
            "lower": left["lower"],
            "upper": right["upper"],
            "weight": left["weight"] + right["weight"],
            "sum_outcomes": left["sum_outcomes"] + right["sum_outcomes"],
        }
        blocks[index : index + 2] = [merged]
        index = max(index - 1, 0)
    return {
        "method": "isotonic",
        "upper_bounds": [float(block["upper"]) for block in blocks],
        "values": [float(block["sum_outcomes"] / block["weight"]) for block in blocks],
    }


def _apply_isotonic_regression(probabilities: Sequence[float], calibrator: dict[str, Any]) -> list[float]:
    upper_bounds = list(calibrator["upper_bounds"])
    values = list(calibrator["values"])
    calibrated = []
    for probability in probabilities:
        clipped = _safe_probability(float(probability))
        bucket_index = int(np.searchsorted(np.asarray(upper_bounds, dtype=np.float64), clipped, side="left"))
        bucket_index = min(bucket_index, len(values) - 1)
        calibrated.append(_safe_probability(float(values[bucket_index])))
    return calibrated


def _competition_order(rows: Sequence[RouteATrainingRow]) -> list[str]:
    return sorted({row.competition_id for row in rows})


def _season_order(rows: Sequence[RouteATrainingRow]) -> list[str]:
    return sorted({row.season_code for row in rows})


def _rows_for_seasons(rows: Sequence[RouteATrainingRow], season_codes: Sequence[str]) -> list[RouteATrainingRow]:
    allowed = set(season_codes)
    return [row for row in rows if row.season_code in allowed]


def _target_for_market(row: RouteATrainingRow, market_key: str) -> int:
    target_field = str(MARKET_DEFINITIONS[market_key]["target_field"])
    return int(getattr(row, target_field))


def _design_matrix(
    rows: Sequence[RouteATrainingRow],
    *,
    competition_categories: Sequence[str],
    mean_by_feature: Sequence[float] | None = None,
    scale_by_feature: Sequence[float] | None = None,
) -> tuple[np.ndarray, list[float], list[float]]:
    numeric_matrix = np.asarray(
        [
            [float(row.feature_dict()[feature_name]) for feature_name in ROUTE_A_NUMERIC_FEATURES]
            for row in rows
        ],
        dtype=np.float64,
    )
    if mean_by_feature is None:
        means = np.mean(numeric_matrix, axis=0).astype(np.float64)
    else:
        means = np.asarray(mean_by_feature, dtype=np.float64)
    if scale_by_feature is None:
        scales = np.std(numeric_matrix, axis=0).astype(np.float64)
        scales[scales == 0.0] = 1.0
    else:
        scales = np.asarray(scale_by_feature, dtype=np.float64)
        scales[scales == 0.0] = 1.0
    normalized_numeric = (numeric_matrix - means) / scales

    category_index = {competition_id: index for index, competition_id in enumerate(competition_categories)}
    categorical_columns = np.zeros((len(rows), max(0, len(competition_categories) - 1)), dtype=np.float64)
    for row_index, row in enumerate(rows):
        competition_index = category_index[row.competition_id]
        if competition_index > 0:
            categorical_columns[row_index, competition_index - 1] = 1.0
    intercept = np.ones((len(rows), 1), dtype=np.float64)
    x_matrix = np.concatenate([intercept, normalized_numeric, categorical_columns], axis=1)
    return x_matrix, [float(value) for value in means.tolist()], [float(value) for value in scales.tolist()]


def _summarize_probabilities(probabilities: Sequence[float], outcomes: Sequence[int]) -> dict[str, Any]:
    return {
        "sample_count": len(outcomes),
        "log_loss": log_loss(list(probabilities), list(outcomes)),
        "brier": brier(list(probabilities), list(outcomes)),
        "calibration_slope": calibration_slope(list(probabilities), list(outcomes)),
        "expected_calibration_error": expected_calibration_error(list(probabilities), list(outcomes)),
    }


def _league_base_rate_predictions(
    *,
    training_rows: Sequence[RouteATrainingRow],
    test_rows: Sequence[RouteATrainingRow],
    market_key: str,
) -> list[float]:
    global_rate = float(np.mean([_target_for_market(row, market_key) for row in training_rows]))
    rates_by_competition: dict[str, float] = {}
    for competition_id in sorted({row.competition_id for row in training_rows}):
        competition_rows = [row for row in training_rows if row.competition_id == competition_id]
        rates_by_competition[competition_id] = float(
            np.mean([_target_for_market(row, market_key) for row in competition_rows])
        )
    return [
        _safe_probability(rates_by_competition.get(row.competition_id, global_rate))
        for row in test_rows
    ]


def _choose_calibration_method(candidate_metrics: dict[str, dict[str, Any]]) -> str:
    ranked = sorted(
        candidate_metrics.items(),
        key=lambda item: (
            float(item[1]["log_loss"]),
            float(item[1]["brier"]),
            str(item[0]) != "none",
        ),
    )
    return str(ranked[0][0])


def _accept_market(
    *,
    naive_metrics: dict[str, Any],
    chosen_metrics: dict[str, Any],
) -> bool:
    return (
        float(chosen_metrics["log_loss"]) + 1e-9 < float(naive_metrics["log_loss"])
        and float(chosen_metrics["brier"]) + 1e-9 < float(naive_metrics["brier"])
    )


def _final_model_rows(
    *,
    rows: Sequence[RouteATrainingRow],
    calibration_method: str,
) -> tuple[list[RouteATrainingRow], list[RouteATrainingRow] | None]:
    seasons = _season_order(rows)
    if calibration_method == "none":
        return list(rows), None
    if len(seasons) < 2:
        raise ValueError("calibrated final model requires at least two seasons")
    calibration_rows = _rows_for_seasons(rows, [seasons[-1]])
    fitting_rows = _rows_for_seasons(rows, seasons[:-1])
    if not fitting_rows or not calibration_rows:
        raise ValueError("final model split produced an empty training or calibration slice")
    return fitting_rows, calibration_rows


def evaluate_route_a_validation(
    rows: Sequence[RouteATrainingRow],
) -> dict[str, Any]:
    competition_categories = _competition_order(rows)
    seasons = _season_order(rows)
    if len(seasons) < 2:
        raise ValueError("Route A validation requires at least two chronological seasons")

    validation: dict[str, Any] = {
        "dataset_rows": len(rows),
        "date_range": {
            "start_match_date": rows[0].match_date,
            "end_match_date": rows[-1].match_date,
        },
        "competition_categories": competition_categories,
        "seasons": seasons,
        "minimum_history_requirements": {
            "uncalibrated_logistic_prior_seasons": ROUTE_A_MIN_HISTORY_SEASONS,
            "calibrated_logistic_prior_seasons": ROUTE_A_MIN_CALIBRATION_HISTORY_SEASONS,
            "minimum_isotonic_rows": ROUTE_A_MIN_ISOTONIC_ROWS,
        },
        "market_results": {},
    }

    for market_key in SUPPORTED_MARKETS:
        fold_rows: list[dict[str, Any]] = []
        naive_all: list[float] = []
        uncalibrated_all: list[float] = []
        platt_all: list[float] = []
        platt_outcomes: list[int] = []
        isotonic_all: list[float] = []
        isotonic_outcomes: list[int] = []
        evaluation_outcomes: list[int] = []

        for season_index in range(1, len(seasons)):
            test_season = seasons[season_index]
            training_seasons = seasons[:season_index]
            training_rows = _rows_for_seasons(rows, training_seasons)
            test_rows = _rows_for_seasons(rows, [test_season])
            if not training_rows or not test_rows:
                continue

            x_train, means, scales = _design_matrix(
                training_rows,
                competition_categories=competition_categories,
            )
            x_test, _, _ = _design_matrix(
                test_rows,
                competition_categories=competition_categories,
                mean_by_feature=means,
                scale_by_feature=scales,
            )
            y_train = np.asarray([_target_for_market(row, market_key) for row in training_rows], dtype=np.float64)
            y_test = [int(_target_for_market(row, market_key)) for row in test_rows]

            coefficients = _fit_logistic_regression(x_train, y_train)
            uncalibrated_predictions = [
                _safe_probability(float(value))
                for value in _predict_logistic_probabilities(x_test, coefficients).tolist()
            ]
            naive_predictions = _league_base_rate_predictions(
                training_rows=training_rows,
                test_rows=test_rows,
                market_key=market_key,
            )

            fold_summary = {
                "test_season": test_season,
                "train_seasons": list(training_seasons),
                "calibration_seasons": [],
                "sample_count": len(test_rows),
                "naive": _summarize_probabilities(naive_predictions, y_test),
                "uncalibrated_logistic": _summarize_probabilities(uncalibrated_predictions, y_test),
                "platt": None,
                "isotonic": None,
            }

            if len(training_seasons) >= ROUTE_A_MIN_CALIBRATION_HISTORY_SEASONS:
                calibration_season = training_seasons[-1]
                fit_rows = _rows_for_seasons(training_rows, training_seasons[:-1])
                calibration_rows = _rows_for_seasons(training_rows, [calibration_season])
                if fit_rows and calibration_rows:
                    x_fit, fit_means, fit_scales = _design_matrix(
                        fit_rows,
                        competition_categories=competition_categories,
                    )
                    x_calibration, _, _ = _design_matrix(
                        calibration_rows,
                        competition_categories=competition_categories,
                        mean_by_feature=fit_means,
                        scale_by_feature=fit_scales,
                    )
                    y_fit = np.asarray([_target_for_market(row, market_key) for row in fit_rows], dtype=np.float64)
                    y_calibration = [int(_target_for_market(row, market_key)) for row in calibration_rows]
                    fit_coefficients = _fit_logistic_regression(x_fit, y_fit)
                    calibration_inputs = _predict_logistic_probabilities(x_calibration, fit_coefficients).tolist()
                    test_inputs = _predict_logistic_probabilities(
                        _design_matrix(
                            test_rows,
                            competition_categories=competition_categories,
                            mean_by_feature=fit_means,
                            scale_by_feature=fit_scales,
                        )[0],
                        fit_coefficients,
                    ).tolist()
                    fold_summary["calibration_seasons"] = [calibration_season]

                    if len(set(y_calibration)) >= 2:
                        platt_calibrator = _fit_platt_scaler(calibration_inputs, y_calibration)
                        platt_predictions = _apply_platt_scaler(test_inputs, platt_calibrator)
                        fold_summary["platt"] = {
                            **_summarize_probabilities(platt_predictions, y_test),
                            "calibrator": platt_calibrator,
                        }
                        platt_all.extend(platt_predictions)
                        platt_outcomes.extend(y_test)

                        if len(calibration_rows) >= ROUTE_A_MIN_ISOTONIC_ROWS:
                            isotonic_calibrator = _fit_isotonic_regression(calibration_inputs, y_calibration)
                            isotonic_predictions = _apply_isotonic_regression(test_inputs, isotonic_calibrator)
                            fold_summary["isotonic"] = {
                                **_summarize_probabilities(isotonic_predictions, y_test),
                                "calibrator": isotonic_calibrator,
                            }
                            isotonic_all.extend(isotonic_predictions)
                            isotonic_outcomes.extend(y_test)

            fold_rows.append(fold_summary)
            naive_all.extend(naive_predictions)
            uncalibrated_all.extend(uncalibrated_predictions)
            evaluation_outcomes.extend(y_test)

        candidate_metrics = {
            "none": _summarize_probabilities(uncalibrated_all, evaluation_outcomes),
        }
        if platt_all:
            candidate_metrics["platt"] = _summarize_probabilities(platt_all, platt_outcomes)
        if isotonic_all:
            candidate_metrics["isotonic"] = _summarize_probabilities(isotonic_all, isotonic_outcomes)
        chosen_calibration = _choose_calibration_method(candidate_metrics)
        naive_metrics = _summarize_probabilities(naive_all, evaluation_outcomes)
        chosen_metrics = candidate_metrics[chosen_calibration]
        validation["market_results"][market_key] = {
            "market_definition": dict(MARKET_DEFINITIONS[market_key]),
            "folds": fold_rows,
            "naive_baseline": {
                "name": ROUTE_A_BASELINE_NAME,
                **naive_metrics,
            },
            "uncalibrated_logistic": candidate_metrics["none"],
            "calibrated_candidates": {
                calibration_name: metrics
                for calibration_name, metrics in candidate_metrics.items()
                if calibration_name != "none"
            },
            "selected_calibration_method": chosen_calibration,
            "selected_metrics": chosen_metrics,
            "accepted": _accept_market(
                naive_metrics=naive_metrics,
                chosen_metrics=chosen_metrics,
            ),
        }

    accepted_markets = [
        market_key
        for market_key, market_result in validation["market_results"].items()
        if market_result["accepted"]
    ]
    validation["overall_route_a_status"] = "ACTIVE" if len(accepted_markets) == len(SUPPORTED_MARKETS) else "NOT_ACTIVE"
    validation["accepted_markets"] = accepted_markets
    return validation


def build_route_a_artifact(
    *,
    research_db_path: str,
    fit_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = build_route_a_training_rows(research_db_path=research_db_path, fit_result=fit_result)
    validation = evaluate_route_a_validation(rows)
    competition_categories = _competition_order(rows)
    artifact_markets: dict[str, Any] = {}

    for market_key in SUPPORTED_MARKETS:
        market_result = validation["market_results"][market_key]
        calibration_method = str(market_result["selected_calibration_method"])
        fit_rows, calibration_rows = _final_model_rows(rows=rows, calibration_method=calibration_method)
        x_fit, means, scales = _design_matrix(fit_rows, competition_categories=competition_categories)
        y_fit = np.asarray([_target_for_market(row, market_key) for row in fit_rows], dtype=np.float64)
        coefficients = _fit_logistic_regression(x_fit, y_fit)
        calibration_payload = {"method": "none"}
        calibration_window = None

        if calibration_rows is not None:
            x_calibration, _, _ = _design_matrix(
                calibration_rows,
                competition_categories=competition_categories,
                mean_by_feature=means,
                scale_by_feature=scales,
            )
            y_calibration = [int(_target_for_market(row, market_key)) for row in calibration_rows]
            calibration_inputs = _predict_logistic_probabilities(x_calibration, coefficients).tolist()
            if calibration_method == "platt":
                calibration_payload = _fit_platt_scaler(calibration_inputs, y_calibration)
            elif calibration_method == "isotonic":
                calibration_payload = _fit_isotonic_regression(calibration_inputs, y_calibration)
            calibration_window = {
                "start_season": calibration_rows[0].season_code,
                "end_season": calibration_rows[-1].season_code,
                "season_codes": sorted({row.season_code for row in calibration_rows}),
                "sample_count": len(calibration_rows),
            }

        artifact_markets[market_key] = {
            "market_key": market_key,
            "market_family": MARKET_DEFINITIONS[market_key]["market_family"],
            "line": MARKET_DEFINITIONS[market_key]["line"],
            "positive_selection": MARKET_DEFINITIONS[market_key]["positive_selection"],
            "negative_selection": MARKET_DEFINITIONS[market_key]["negative_selection"],
            "feature_version": ROUTE_A_FEATURE_VERSION,
            "model_family": ROUTE_A_MODEL_FAMILY,
            "model_version": MARKET_DEFINITIONS[market_key]["model_version"],
            "agent_version_tag": ROUTE_A_AGENT_VERSION_TAG,
            "selected_calibration_method": calibration_method,
            "preprocessing": {
                "competition_categories": competition_categories,
                "numeric_feature_order": list(ROUTE_A_NUMERIC_FEATURES),
                "mean_by_feature": means,
                "scale_by_feature": scales,
            },
            "coefficients": [float(value) for value in coefficients.tolist()],
            "calibration": calibration_payload,
            "training_window": {
                "start_season": fit_rows[0].season_code,
                "end_season": fit_rows[-1].season_code,
                "season_codes": sorted({row.season_code for row in fit_rows}),
                "sample_count": len(fit_rows),
            },
            "calibration_window": calibration_window,
            "validation_summary": market_result,
        }

    model_as_of = max(row.as_of for row in rows)
    artifact = {
        "artifact_version": ROUTE_A_ARTIFACT_VERSION,
        "generated_at": _utc_now(),
        "agent_id": ROUTE_A_AGENT_ID,
        "agent_name": ROUTE_A_AGENT_NAME,
        "agent_version_tag": ROUTE_A_AGENT_VERSION_TAG,
        "method_family": ROUTE_A_METHOD_FAMILY,
        "model_as_of": model_as_of,
        "feature_version": ROUTE_A_FEATURE_VERSION,
        "model_family": ROUTE_A_MODEL_FAMILY,
        "configuration_count": ROUTE_A_CONFIGURATION_COUNT,
        "supported_competitions": competition_categories,
        "dataset_summary": {
            "row_count": len(rows),
            "start_match_date": rows[0].match_date,
            "end_match_date": rows[-1].match_date,
            "season_codes": _season_order(rows),
            "competition_ids": competition_categories,
            "point_in_time_contract": "Day 7 GAP pre-match predictions joined to realized targets only after as_of <= kickoff validation.",
            "leakage_controls": [
                "features originate from chronological Day 7 GAP predictions only",
                "targets are derived from realized match goals after feature generation",
                "walk-forward folds are season-ordered and calibration uses only prior seasons",
                "no closing prices, final scores, future fixtures, or future ratings enter the feature set",
            ],
        },
        "validation": validation,
        "markets": artifact_markets,
        "overall_route_a_status": validation["overall_route_a_status"],
    }
    artifact["artifact_id"] = f"route_a_artifact_{canonical_hash(artifact)}"
    return artifact


def _market_feature_vector(
    *,
    competition_id: str,
    predicted_home_shots: float,
    predicted_away_shots: float,
    predicted_home_corners: float,
    predicted_away_corners: float,
) -> dict[str, Any]:
    return {
        "competition_id": competition_id,
        "predicted_home_shots": float(predicted_home_shots),
        "predicted_away_shots": float(predicted_away_shots),
        "predicted_home_corners": float(predicted_home_corners),
        "predicted_away_corners": float(predicted_away_corners),
        "predicted_total_shots": float(predicted_home_shots + predicted_away_shots),
        "predicted_shot_difference": float(predicted_home_shots - predicted_away_shots),
        "predicted_total_corners": float(predicted_home_corners + predicted_away_corners),
        "predicted_corner_difference": float(predicted_home_corners - predicted_away_corners),
    }


def _single_feature_matrix(feature_payload: dict[str, Any], preprocessing: dict[str, Any]) -> np.ndarray:
    competition_categories = list(preprocessing["competition_categories"])
    means = list(preprocessing["mean_by_feature"])
    scales = list(preprocessing["scale_by_feature"])
    numeric_values = np.asarray(
        [float(feature_payload[feature_name]) for feature_name in ROUTE_A_NUMERIC_FEATURES],
        dtype=np.float64,
    )
    normalized_numeric = (numeric_values - np.asarray(means, dtype=np.float64)) / np.asarray(scales, dtype=np.float64)
    x_matrix = np.ones((1, 1 + len(ROUTE_A_NUMERIC_FEATURES) + max(0, len(competition_categories) - 1)), dtype=np.float64)
    x_matrix[0, 1 : 1 + len(ROUTE_A_NUMERIC_FEATURES)] = normalized_numeric
    try:
        category_index = competition_categories.index(str(feature_payload["competition_id"]))
    except ValueError as exc:
        raise KeyError("unsupported competition") from exc
    if category_index > 0:
        x_matrix[0, 1 + len(ROUTE_A_NUMERIC_FEATURES) + category_index - 1] = 1.0
    return x_matrix


def _apply_calibration(probability: float, calibration_payload: dict[str, Any]) -> float:
    method = str(calibration_payload.get("method") or "none")
    if method == "none":
        return _safe_probability(probability)
    if method == "platt":
        return _safe_probability(_apply_platt_scaler([probability], calibration_payload)[0])
    if method == "isotonic":
        return _safe_probability(_apply_isotonic_regression([probability], calibration_payload)[0])
    raise ValueError(f"unsupported calibration method {method!r}")


def predict_route_a_market(
    artifact: dict[str, Any] | None,
    *,
    market_key: str,
    competition_id: str,
    predicted_home_shots: float | None,
    predicted_away_shots: float | None,
    predicted_home_corners: float | None,
    predicted_away_corners: float | None,
    as_of: str,
) -> dict[str, Any]:
    if artifact is None:
        return {
            "market_key": market_key,
            "prediction_status": "BLOCKED",
            "block_reason": "MODEL_ARTIFACT_MISSING",
        }
    if market_key not in SUPPORTED_MARKETS:
        return {
            "market_key": market_key,
            "prediction_status": "NOT_APPLICABLE",
            "block_reason": "MARKET_NOT_SUPPORTED",
        }
    if artifact.get("overall_route_a_status") != "ACTIVE":
        return {
            "market_key": market_key,
            "prediction_status": "BLOCKED",
            "block_reason": "MODEL_VALIDATION_BLOCKED",
        }
    required_values = {
        "predicted_home_shots": predicted_home_shots,
        "predicted_away_shots": predicted_away_shots,
        "predicted_home_corners": predicted_home_corners,
        "predicted_away_corners": predicted_away_corners,
    }
    missing_inputs = [key for key, value in required_values.items() if value is None or not math.isfinite(float(value))]
    if missing_inputs:
        return {
            "market_key": market_key,
            "prediction_status": "BLOCKED",
            "block_reason": "MISSING_GAP_FEATURES",
            "missing_inputs": missing_inputs,
        }

    market_artifact = dict((artifact.get("markets") or {}).get(market_key) or {})
    if not market_artifact:
        return {
            "market_key": market_key,
            "prediction_status": "NOT_APPLICABLE",
            "block_reason": "MARKET_NOT_SUPPORTED",
        }

    try:
        feature_payload = _market_feature_vector(
            competition_id=competition_id,
            predicted_home_shots=float(predicted_home_shots),
            predicted_away_shots=float(predicted_away_shots),
            predicted_home_corners=float(predicted_home_corners),
            predicted_away_corners=float(predicted_away_corners),
        )
        x_matrix = _single_feature_matrix(feature_payload, market_artifact["preprocessing"])
    except KeyError:
        return {
            "market_key": market_key,
            "prediction_status": "BLOCKED",
            "block_reason": "UNSUPPORTED_COMPETITION",
        }

    coefficients = np.asarray(list(market_artifact["coefficients"]), dtype=np.float64)
    raw_probability = float(_predict_logistic_probabilities(x_matrix, coefficients)[0])
    calibrated_probability = _apply_calibration(raw_probability, dict(market_artifact["calibration"]))
    positive_probability = _safe_probability(calibrated_probability)
    negative_probability = _safe_probability(1.0 - positive_probability)
    positive_selection = str(market_artifact["positive_selection"])
    negative_selection = str(market_artifact["negative_selection"])
    chosen_selection = positive_selection if positive_probability >= negative_probability else negative_selection
    chosen_probability = positive_probability if chosen_selection == positive_selection else negative_probability
    return {
        "market_key": market_key,
        "prediction_status": "PREDICTED",
        "market_family": market_artifact["market_family"],
        "line": market_artifact["line"],
        "positive_selection": positive_selection,
        "negative_selection": negative_selection,
        "positive_probability": positive_probability,
        "negative_probability": negative_probability,
        "chosen_selection": chosen_selection,
        "chosen_probability": chosen_probability,
        "as_of": as_of,
        "model_version": market_artifact["model_version"],
        "feature_version": market_artifact["feature_version"],
        "calibration_method": market_artifact["selected_calibration_method"],
        "agent_version_tag": market_artifact["agent_version_tag"],
        "feature_payload": feature_payload,
    }


def _binary_fair_price(probability: float) -> float:
    return math.inf if probability <= 0.0 else float(1.0 / probability)


def build_route_a_market_rows(
    artifact: dict[str, Any] | None,
    *,
    competition_id: str,
    predicted_home_shots: float | None,
    predicted_away_shots: float | None,
    predicted_home_corners: float | None,
    predicted_away_corners: float | None,
    as_of: str,
) -> dict[str, Any]:
    market_predictions = [
        predict_route_a_market(
            artifact,
            market_key=market_key,
            competition_id=competition_id,
            predicted_home_shots=predicted_home_shots,
            predicted_away_shots=predicted_away_shots,
            predicted_home_corners=predicted_home_corners,
            predicted_away_corners=predicted_away_corners,
            as_of=as_of,
        )
        for market_key in SUPPORTED_MARKETS
    ]
    if artifact is None or artifact.get("overall_route_a_status") != "ACTIVE":
        return {
            "prediction_status": "BLOCKED",
            "blockers": sorted({str(item["block_reason"]) for item in market_predictions}),
            "market_contexts": [
                {
                    "market_family": MARKET_DEFINITIONS[market_key]["market_family"],
                    "line": MARKET_DEFINITIONS[market_key]["line"],
                }
                for market_key in SUPPORTED_MARKETS
            ],
            "market_predictions": market_predictions,
        }

    if any(str(item["prediction_status"]) != "PREDICTED" for item in market_predictions):
        blockers: list[str] = []
        contexts: list[dict[str, Any]] = []
        for item in market_predictions:
            if str(item["prediction_status"]) == "PREDICTED":
                continue
            blockers.append(str(item["block_reason"]))
            market_definition = dict(MARKET_DEFINITIONS[str(item["market_key"])])
            contexts.append(
                {
                    "market_family": market_definition["market_family"],
                    "line": market_definition["line"],
                }
            )
        return {
            "prediction_status": "BLOCKED",
            "blockers": sorted(set(blockers)),
            "market_contexts": contexts,
            "market_predictions": market_predictions,
        }

    market_rows: list[dict[str, Any]] = []
    for item in market_predictions:
        positive_probability = float(item["positive_probability"])
        negative_probability = float(item["negative_probability"])
        for selection, probability, complementary_probability in (
            (item["positive_selection"], positive_probability, negative_probability),
            (item["negative_selection"], negative_probability, positive_probability),
        ):
            market_rows.append(
                {
                    "market_family": item["market_family"],
                    "line": item["line"],
                    "selection": selection,
                    "effective_win_stake_probability": probability,
                    "effective_loss_stake_probability": complementary_probability,
                    "fair_decimal_price": _binary_fair_price(probability),
                    "settlement_probabilities": {
                        "full_win_probability": probability,
                        "half_win_probability": 0.0,
                        "push_probability": 0.0,
                        "half_loss_probability": 0.0,
                        "full_loss_probability": complementary_probability,
                    },
                    "component_lines": [] if item["line"] is None else [item["line"]],
                    "as_of": as_of,
                    "model_version": item["model_version"],
                    "feature_version": item["feature_version"],
                    "calibration_method": item["calibration_method"],
                    "route_a_market_key": item["market_key"],
                }
            )
    return {
        "prediction_status": "PREDICTED",
        "market_rows": market_rows,
        "market_predictions": market_predictions,
    }


def summarize_market_capabilities(artifact: dict[str, Any] | None) -> dict[str, str]:
    if artifact is None or artifact.get("overall_route_a_status") != "ACTIVE":
        return {
            "1x2": "NOT_SUPPORTED",
            "btts": "INACTIVE",
            "goals_total": "INACTIVE",
            "asian_handicap": "NOT_SUPPORTED",
            "corner_total": "NOT_SUPPORTED",
        }
    return {
        "1x2": "NOT_SUPPORTED",
        "btts": "SUPPORTED",
        "goals_total": "SUPPORTED",
        "asian_handicap": "NOT_SUPPORTED",
        "corner_total": "NOT_SUPPORTED",
    }


def route_a_contract_summary(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": ROUTE_A_MODEL_FAMILY,
        "features": list(ROUTE_A_NUMERIC_FEATURES) + [ROUTE_A_COMPETITION_FEATURE],
        "targets": ["BTTS", "OVER_2_5"],
        "markets": [
            {"market_family": "btts", "line": None},
            {"market_family": "goals_total", "line": 2.5},
        ],
        "calibration": {
            market_key: artifact["markets"][market_key]["selected_calibration_method"]
            for market_key in SUPPORTED_MARKETS
        },
        "version": {
            market_key: artifact["markets"][market_key]["model_version"]
            for market_key in SUPPORTED_MARKETS
        },
    }


def fold_windows(validation: dict[str, Any]) -> list[dict[str, Any]]:
    by_window: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}
    for market_result in validation["market_results"].values():
        for fold in market_result["folds"]:
            key = (tuple(fold["train_seasons"]), str(fold["test_season"]))
            by_window.setdefault(
                key,
                {
                    "train_seasons": list(fold["train_seasons"]),
                    "calibration_seasons": list(fold["calibration_seasons"]),
                    "test_season": str(fold["test_season"]),
                },
            )
    return list(by_window.values())
