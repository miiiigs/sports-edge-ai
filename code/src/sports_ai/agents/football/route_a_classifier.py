from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from ...evaluation.metrics import brier, calibration_bins, log_loss
from ...experiments.football_gap_data_readiness import generate_gap_data_readiness_report
from ...experiments.football_gap_lambda_fitting import (
    DEFAULT_LAMBDA_GRID,
    DEFAULT_LEARNING_RATE,
    MATERIAL_IMPROVEMENT_THRESHOLD,
    SELECTION_POLICY_DESCRIPTION,
    SELECTION_POLICY_ID,
)
from ...experiments.football_gap_shots_walkforward import (
    AS_OF_POLICY_DESCRIPTION,
    AS_OF_POLICY_ID,
    BASELINE_POLICY_DESCRIPTION,
    BASELINE_POLICY_ID,
    _approved_scope_rows,
    _build_engine_config_entries,
    _load_historical_matches,
    _warmup_season_by_competition,
)
from ...locking import canonical_hash
from .gap_ratings import GapRatingsEngine, GapStateRecord

ROUTE_A_AGENT_ID = "ROUTE_A_DIRECT_CLASSIFIER"
ROUTE_A_AGENT_NAME = "Route A Direct Classifier"
ROUTE_A_METHOD_FAMILY = "GAP_DIRECT_CLASSIFICATION"
ROUTE_A_MODEL_BUNDLE_VERSION = "football_route_a_direct_classifier_bundle_v2"
ROUTE_A_ARTIFACT_SCHEMA_VERSION = "football_route_a_direct_classifier_artifact_v2"
ROUTE_A_MANIFEST_SCHEMA_VERSION = "football_route_a_direct_classifier_manifest_v2"
ROUTE_A_FEATURE_VERSION = "route_a_gap_shots_corners_nested_pit_v2"
ROUTE_A_MODEL_FAMILY = "LOGISTIC_REGRESSION"
ROUTE_A_BASELINE_NAME = "LEAGUE_BASE_RATE"
ROUTE_A_GLOBAL_BASELINE_NAME = "GLOBAL_BASE_RATE"
ROUTE_A_CONSENSUS_CONFIG_ID = "football_route_a_b_consensus"
ROUTE_A_CONSENSUS_VERSION = "FOOTBALL_ROUTE_A_B_V1"
ROUTE_A_CONSENSUS_VALID_FROM = "2026-08-27T00:00:00+00:00"
ROUTE_A_AGENT_VERSION_TAG = "ROUTE_A_LOGISTIC_V2"
ROUTE_A_GAP_REPLAY_VERSION = "football_route_a_gap_nested_pit_v1"
ROUTE_A_REVIEW_STATUS_PENDING = "PENDING_INDEPENDENT_REVIEW"
ROUTE_A_REVIEW_STATUS_APPROVED = "APPROVED"
ROUTE_A_REVIEW_STATUS_REJECTED = "REJECTED"
ROUTE_A_VALIDATION_STATUS_PASSED = "PASSED"
ROUTE_A_VALIDATION_STATUS_FAILED = "FAILED"
ROUTE_A_MARKET_STATUS_VALIDATED = "VALIDATED_CANDIDATE"
ROUTE_A_MARKET_STATUS_REJECTED = "REJECTED"
ROUTE_A_RUNTIME_STATUS_SUPPORTED = "SUPPORTED"
ROUTE_A_RUNTIME_STATUS_PENDING = "PENDING_REVIEW"
ROUTE_A_RUNTIME_STATUS_REJECTED = "REJECTED"
ROUTE_A_RUNTIME_STATUS_INACTIVE = "INACTIVE"
ROUTE_A_MIN_ISOTONIC_ROWS = 500
ROUTE_A_BOOTSTRAP_ITERATIONS = 1000
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
ROUTE_A_REQUIRED_GAP_STATISTICS = ("shots", "corners")

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
        "model_version": "ROUTE_A_LOGISTIC_V2_BTTS",
        "target_field": "btts_target",
    },
    MARKET_KEY_OU25: {
        "market_family": "goals_total",
        "line": 2.5,
        "positive_selection": "OVER",
        "negative_selection": "UNDER",
        "model_version": "ROUTE_A_LOGISTIC_V2_OU25",
        "target_field": "over_2_5_target",
    },
}
ROUTE_A_RUNTIME_REASONS = {
    ROUTE_A_RUNTIME_STATUS_PENDING: "MODEL_REVIEW_PENDING",
    ROUTE_A_RUNTIME_STATUS_REJECTED: "MARKET_REJECTED",
    ROUTE_A_RUNTIME_STATUS_INACTIVE: "MODEL_VALIDATION_BLOCKED",
}


@dataclass(frozen=True)
class RouteATrainingRow:
    event_id: str
    competition_id: str
    competition_name: str
    season_code: str
    division_code: str
    match_date: str
    kickoff_time: str
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
    gap_model_version_shots: str
    gap_model_version_corners: str
    gap_learning_rate_shots: float
    gap_learning_rate_corners: float
    gap_training_cutoff_shots: str
    gap_training_cutoff_corners: str
    gap_selection_cutoff_shots: str
    gap_selection_cutoff_corners: str
    route_a_feature_version: str

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
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _safe_probability(value: float) -> float:
    return float(min(1.0 - 1e-9, max(1e-9, value)))


def _connect_research_store(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _load_goal_outcomes(
    *,
    research_db_path: str | Path,
) -> dict[str, dict[str, int]]:
    query = """
        SELECT event_id, home_goals, away_goals
        FROM football_events
        WHERE home_goals IS NOT NULL
          AND away_goals IS NOT NULL
    """
    with _connect_research_store(research_db_path) as conn:
        rows = conn.execute(query).fetchall()
    return {
        str(row["event_id"]): {
            "actual_home_goals": int(row["home_goals"]),
            "actual_away_goals": int(row["away_goals"]),
        }
        for row in rows
    }


def expected_calibration_error(probs: Sequence[float], outcomes: Sequence[int], *, n_bins: int = 10) -> float:
    if len(probs) == 0:
        return float("nan")
    total = 0.0
    for bucket in calibration_bins(list(probs), list(outcomes), n_bins=n_bins):
        if not bucket["n"]:
            continue
        total += (bucket["n"] / len(probs)) * abs(float(bucket["mean_pred"]) - float(bucket["observed"]))
    return total


def binned_reliability_slope(probs: Sequence[float], outcomes: Sequence[int], *, n_bins: int = 10) -> float:
    points = [
        (float(bucket["mean_pred"]), float(bucket["observed"]))
        for bucket in calibration_bins(list(probs), list(outcomes), n_bins=n_bins)
        if bucket["n"] >= 5 and bucket["mean_pred"] is not None and bucket["observed"] is not None
    ]
    if len(points) < 3:
        return float("nan")
    xs, ys = zip(*points)
    mean_x = float(sum(xs) / len(xs))
    mean_y = float(sum(ys) / len(ys))
    denominator = float(sum((x - mean_x) ** 2 for x in xs))
    if denominator == 0.0:
        return float("nan")
    numerator = float(sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)))
    return numerator / denominator


def _sigmoid(values: np.ndarray) -> np.ndarray:
    clipped = np.clip(values, -35.0, 35.0)
    return 1.0 / (1.0 + np.exp(-clipped))


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
    means = np.mean(numeric_matrix, axis=0).astype(np.float64) if mean_by_feature is None else np.asarray(mean_by_feature, dtype=np.float64)
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


def _logistic_calibration_diagnostics(probabilities: Sequence[float], outcomes: Sequence[int]) -> dict[str, float]:
    if len(probabilities) == 0 or len(set(int(value) for value in outcomes)) < 2:
        return {"intercept": float("nan"), "slope": float("nan")}
    probs = np.asarray([_safe_probability(float(value)) for value in probabilities], dtype=np.float64)
    y_vector = np.asarray([int(value) for value in outcomes], dtype=np.float64)
    logits = np.log(probs / (1.0 - probs))
    x_matrix = np.column_stack([np.ones(len(logits), dtype=np.float64), logits])
    beta = _fit_logistic_regression(x_matrix, y_vector, l2_regularization=1e-6)
    return {"intercept": float(beta[0]), "slope": float(beta[1])}


def _probability_quantiles(probabilities: Sequence[float]) -> dict[str, float]:
    if not probabilities:
        return {}
    values = np.asarray([float(value) for value in probabilities], dtype=np.float64)
    return {
        "p05": float(np.quantile(values, 0.05)),
        "p25": float(np.quantile(values, 0.25)),
        "p50": float(np.quantile(values, 0.50)),
        "p75": float(np.quantile(values, 0.75)),
        "p95": float(np.quantile(values, 0.95)),
    }


def _loss_row(probability: float, outcome: int) -> float:
    p = _safe_probability(probability)
    return float(-(outcome * math.log(p) + (1 - outcome) * math.log(1.0 - p)))


def _summarize_probabilities(probabilities: Sequence[float], outcomes: Sequence[int]) -> dict[str, Any]:
    calibration = _logistic_calibration_diagnostics(probabilities, outcomes)
    return {
        "sample_count": len(outcomes),
        "log_loss": log_loss(list(probabilities), list(outcomes)),
        "brier": brier(list(probabilities), list(outcomes)),
        "calibration_intercept": calibration["intercept"],
        "calibration_slope": calibration["slope"],
        "binned_reliability_slope": binned_reliability_slope(probabilities, outcomes),
        "expected_calibration_error": expected_calibration_error(list(probabilities), list(outcomes)),
        "reliability_bins": calibration_bins(list(probabilities), list(outcomes)),
        "probability_quantiles": _probability_quantiles(probabilities),
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
        rates_by_competition[competition_id] = float(np.mean([_target_for_market(row, market_key) for row in competition_rows]))
    return [_safe_probability(rates_by_competition.get(row.competition_id, global_rate)) for row in test_rows]


def _global_base_rate_predictions(
    *,
    training_rows: Sequence[RouteATrainingRow],
    test_rows: Sequence[RouteATrainingRow],
    market_key: str,
) -> list[float]:
    global_rate = _safe_probability(float(np.mean([_target_for_market(row, market_key) for row in training_rows])))
    return [global_rate for _ in test_rows]


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


def _selection_sort_key(variant: dict[str, Any]) -> tuple[float, float, float]:
    combined_mae = variant["combined_mae"]
    if combined_mae is None:
        return (float("inf"), float("inf"), float("inf"))
    return (float(combined_mae), float(variant["bias_score"]), float(variant["candidate_lambda"]))


def _gap_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "evaluation_sample_count": 0,
            "home_mae": None,
            "away_mae": None,
            "combined_mae": None,
            "home_bias": None,
            "away_bias": None,
        }
    abs_home_errors = [abs(float(row["error_home"])) for row in rows]
    abs_away_errors = [abs(float(row["error_away"])) for row in rows]
    home_errors = [float(row["error_home"]) for row in rows]
    away_errors = [float(row["error_away"]) for row in rows]
    return {
        "evaluation_sample_count": len(rows),
        "home_mae": float(sum(abs_home_errors) / len(abs_home_errors)),
        "away_mae": float(sum(abs_away_errors) / len(abs_away_errors)),
        "combined_mae": float((sum(abs_home_errors) + sum(abs_away_errors)) / (2 * len(rows))),
        "home_bias": float(sum(home_errors) / len(home_errors)),
        "away_bias": float(sum(away_errors) / len(away_errors)),
    }


def _approved_route_a_scope(
    *,
    readiness_report: dict[str, Any],
) -> list[dict[str, Any]]:
    shots_scope = {(str(row["competition_id"]), str(row["season_code"])): row for row in _approved_scope_rows(readiness_report, "shots")}
    corners_scope = {(str(row["competition_id"]), str(row["season_code"])): row for row in _approved_scope_rows(readiness_report, "corners")}
    shared_scope = sorted(set(shots_scope) & set(corners_scope))
    if not shared_scope:
        raise ValueError("Route A requires overlapping readiness-approved shots and corners scope")
    return [shots_scope[key] for key in shared_scope]


def _scope_seasons_by_competition(scope_rows: Sequence[dict[str, Any]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for row in scope_rows:
        grouped[str(row["competition_id"])].append(str(row["season_code"]))
    return {competition_id: sorted(set(season_codes)) for competition_id, season_codes in sorted(grouped.items())}


def _route_a_gap_model_version(statistic: str, target_season: str) -> str:
    return f"{ROUTE_A_GAP_REPLAY_VERSION}_{statistic}_{target_season}"


def _run_route_a_gap_replay(
    *,
    research_db_path: str | Path,
    readiness_report: dict[str, Any],
    statistic: str,
    learning_rates_by_competition: dict[str, float],
    season_codes: Sequence[str],
    evaluation_season_codes: Sequence[str] | None,
    competition_ids: Sequence[str] | None,
    model_version: str,
) -> dict[str, Any]:
    approved_scope_rows = _approved_scope_rows(
        readiness_report,
        statistic,
        competition_ids=competition_ids,
        season_codes=season_codes,
    )
    warmup_by_competition = _warmup_season_by_competition(approved_scope_rows)
    matches = _load_historical_matches(
        research_db_path=research_db_path,
        approved_scope_rows=approved_scope_rows,
        statistic=statistic,
    )
    for match in matches:
        if match.kickoff_time is None:
            raise ValueError("Route A nested historical GAP replay requires populated kickoff_time values")
    config_entries, _ = _build_engine_config_entries(
        matches=matches,
        warmup_season_by_competition=warmup_by_competition,
        statistic=statistic,
        learning_rates_by_competition=learning_rates_by_competition,
        model_version=model_version,
    )
    engine = GapRatingsEngine(config_entries)
    evaluation_filter = {str(value) for value in evaluation_season_codes} if evaluation_season_codes is not None else None
    competition_match_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    last_training_cutoff: dict[str, str | None] = {competition_id: None for competition_id in learning_rates_by_competition}
    prediction_rows: list[dict[str, Any]] = []

    index = 0
    while index < len(matches):
        group = [matches[index]]
        index += 1
        while index < len(matches):
            candidate = matches[index]
            if (
                candidate.competition_id != group[0].competition_id
                or candidate.match_date != group[0].match_date
                or candidate.kickoff_time != group[0].kickoff_time
            ):
                break
            group.append(candidate)
            index += 1

        competition_id = group[0].competition_id
        batch_training_cutoff = last_training_cutoff[competition_id]
        staged_updates: list[tuple[Any, float, float, GapStateRecord, GapStateRecord]] = []
        for match in group:
            pre_home_state = engine.state_record(
                competition_id=match.competition_id,
                statistic=statistic,
                team_id=match.home_team_id,
                venue="home",
            )
            pre_away_state = engine.state_record(
                competition_id=match.competition_id,
                statistic=statistic,
                team_id=match.away_team_id,
                venue="away",
            )
            prediction = engine.predict_fixture(
                competition_id=match.competition_id,
                statistic=statistic,
                home_team_id=match.home_team_id,
                away_team_id=match.away_team_id,
            )
            error_home = float(match.home_value) - float(prediction.expected_home)
            error_away = float(match.away_value) - float(prediction.expected_away)
            is_warmup = match.season_code == warmup_by_competition[match.competition_id]
            included_in_evaluation = not is_warmup and (evaluation_filter is None or match.season_code in evaluation_filter)
            prediction_rows.append(
                {
                    "event_id": match.event_id,
                    "competition_id": match.competition_id,
                    "competition_name": match.competition_name,
                    "season_code": match.season_code,
                    "division_code": match.division_code,
                    "match_date": match.match_date,
                    "kickoff_time": match.kickoff_time,
                    "kickoff_at": _kickoff_at(match.match_date, match.kickoff_time),
                    "as_of": match.as_of,
                    "training_cutoff": batch_training_cutoff,
                    "home_team_id": match.home_team_id,
                    "away_team_id": match.away_team_id,
                    "expected_home": float(prediction.expected_home),
                    "expected_away": float(prediction.expected_away),
                    "actual_home": float(match.home_value),
                    "actual_away": float(match.away_value),
                    "error_home": error_home,
                    "error_away": error_away,
                    "phase": "warmup" if is_warmup else "evaluation",
                    "included_in_evaluation": included_in_evaluation,
                    "home_team_pre_match_competition_matches": int(competition_match_counts[match.competition_id][match.home_team_id]),
                    "away_team_pre_match_competition_matches": int(competition_match_counts[match.competition_id][match.away_team_id]),
                    "learning_rate": float(learning_rates_by_competition[match.competition_id]),
                    "model_version": model_version,
                }
            )
            staged_updates.append((match, error_home, error_away, pre_home_state, pre_away_state))

        for match, error_home, error_away, pre_home_state, pre_away_state in staged_updates:
            learning_rate = float(learning_rates_by_competition[match.competition_id])
            updated_home = GapStateRecord(
                team_id=pre_home_state.team_id,
                competition_id=pre_home_state.competition_id,
                statistic=pre_home_state.statistic,
                venue=pre_home_state.venue,
                attack_rating=pre_home_state.attack_rating + learning_rate * error_home,
                defense_rating=pre_home_state.defense_rating + learning_rate * error_away,
                matches_observed=pre_home_state.matches_observed + 1,
                as_of=match.as_of,
                learning_rate=pre_home_state.learning_rate,
                model_version=pre_home_state.model_version,
            )
            updated_away = GapStateRecord(
                team_id=pre_away_state.team_id,
                competition_id=pre_away_state.competition_id,
                statistic=pre_away_state.statistic,
                venue=pre_away_state.venue,
                attack_rating=pre_away_state.attack_rating + learning_rate * error_away,
                defense_rating=pre_away_state.defense_rating + learning_rate * error_home,
                matches_observed=pre_away_state.matches_observed + 1,
                as_of=match.as_of,
                learning_rate=pre_away_state.learning_rate,
                model_version=pre_away_state.model_version,
            )
            engine._states[(updated_home.competition_id, updated_home.statistic, updated_home.team_id, updated_home.venue)] = updated_home
            engine._states[(updated_away.competition_id, updated_away.statistic, updated_away.team_id, updated_away.venue)] = updated_away
            engine._processed_event_ids.add((match.competition_id, statistic, match.event_id))
            competition_match_counts[match.competition_id][match.home_team_id] += 1
            competition_match_counts[match.competition_id][match.away_team_id] += 1
        engine._last_processed_date[(competition_id, statistic)] = group[0].match_date
        last_training_cutoff[competition_id] = max(match.as_of for match in group)

    evaluation_rows = [row for row in prediction_rows if row["included_in_evaluation"]]
    return {
        "predictions": prediction_rows,
        "evaluation_rows": evaluation_rows,
        "metrics": _gap_metrics(evaluation_rows),
        "baseline_policy_id": BASELINE_POLICY_ID,
        "baseline_policy_description": BASELINE_POLICY_DESCRIPTION,
        "as_of_policy_id": AS_OF_POLICY_ID,
        "as_of_policy_description": AS_OF_POLICY_DESCRIPTION,
    }


def _build_gap_selection_folds(season_codes: Sequence[str]) -> list[dict[str, Any]]:
    if len(season_codes) < 3:
        return []
    folds = []
    for index in range(1, len(season_codes) - 1):
        folds.append(
            {
                "fold_id": f"gap_lambda_fold_{index}",
                "training_seasons": list(season_codes[:index]),
                "validation_season": str(season_codes[index]),
                "replay_seasons": list(season_codes[: index + 1]),
            }
        )
    return folds


def _cutoff_for_rows(rows: Sequence[dict[str, Any]]) -> str:
    values = [str(row["as_of"]) for row in rows]
    if not values:
        raise ValueError("cannot determine cutoff from empty rows")
    return max(values)


def _select_gap_lambda_for_target_season(
    *,
    research_db_path: str | Path,
    readiness_report: dict[str, Any],
    statistic: str,
    competition_id: str,
    prior_seasons: Sequence[str],
    target_season: str,
) -> dict[str, Any]:
    reference_probe = _run_route_a_gap_replay(
        research_db_path=research_db_path,
        readiness_report=readiness_report,
        statistic=statistic,
        learning_rates_by_competition={competition_id: DEFAULT_LEARNING_RATE},
        season_codes=prior_seasons,
        evaluation_season_codes=list(prior_seasons[1:]) or list(prior_seasons),
        competition_ids=[competition_id],
        model_version=f"{ROUTE_A_GAP_REPLAY_VERSION}_{statistic}_reference_probe",
    )
    if len(prior_seasons) < 3:
        return {
            "competition_id": competition_id,
            "statistic": statistic,
            "target_season": target_season,
            "selection_mode": "REFERENCE_DEFAULT_INSUFFICIENT_HISTORY",
            "selected_lambda": DEFAULT_LEARNING_RATE,
            "selection_reason": "fewer than three prior seasons available, so Route A keeps the fixed 0.05 reference lambda",
            "selection_cutoff": _cutoff_for_rows(reference_probe["predictions"]),
            "validation_fold_count": 0,
            "candidate_rows": [],
        }

    folds = _build_gap_selection_folds(prior_seasons)
    variants: list[dict[str, Any]] = []
    for candidate_lambda in (float(value) for value in DEFAULT_LAMBDA_GRID):
        aggregate_rows: list[dict[str, Any]] = []
        for fold in folds:
            fold_result = _run_route_a_gap_replay(
                research_db_path=research_db_path,
                readiness_report=readiness_report,
                statistic=statistic,
                learning_rates_by_competition={competition_id: candidate_lambda},
                season_codes=fold["replay_seasons"],
                evaluation_season_codes=[fold["validation_season"]],
                competition_ids=[competition_id],
                model_version=f"{ROUTE_A_GAP_REPLAY_VERSION}_{statistic}_lambda_{candidate_lambda:.2f}",
            )
            aggregate_rows.extend(fold_result["evaluation_rows"])
        metrics = _gap_metrics(aggregate_rows)
        variants.append(
            {
                "competition_id": competition_id,
                "statistic": statistic,
                "candidate_lambda": candidate_lambda,
                "combined_mae": metrics["combined_mae"],
                "home_bias": metrics["home_bias"],
                "away_bias": metrics["away_bias"],
                "bias_score": (
                    float("inf")
                    if metrics["home_bias"] is None or metrics["away_bias"] is None
                    else abs(float(metrics["home_bias"])) + abs(float(metrics["away_bias"]))
                ),
                "validation_fold_count": len(folds),
                "selection_cutoff": _cutoff_for_rows(aggregate_rows),
            }
        )
    variants.sort(key=_selection_sort_key)
    reference_variant = next(variant for variant in variants if float(variant["candidate_lambda"]) == DEFAULT_LEARNING_RATE)
    best_variant = variants[0]
    selected_variant = best_variant
    selection_reason = "selected lowest aggregate validation combined MAE from the fixed lambda grid"
    if float(best_variant["candidate_lambda"]) != DEFAULT_LEARNING_RATE:
        improvement_vs_reference = float(reference_variant["combined_mae"]) - float(best_variant["combined_mae"])
        if improvement_vs_reference < MATERIAL_IMPROVEMENT_THRESHOLD:
            selected_variant = reference_variant
            selection_reason = (
                "reference lambda retained because the best non-reference candidate improved aggregate "
                f"validation combined MAE by only {improvement_vs_reference:.6f}, below the fixed "
                f"material threshold {MATERIAL_IMPROVEMENT_THRESHOLD:.2f}"
            )
    return {
        "competition_id": competition_id,
        "statistic": statistic,
        "target_season": target_season,
        "selection_mode": "NESTED_VALIDATION",
        "selected_lambda": float(selected_variant["candidate_lambda"]),
        "selection_reason": selection_reason,
        "selection_cutoff": str(selected_variant["selection_cutoff"]),
        "validation_fold_count": int(selected_variant["validation_fold_count"]),
        "candidate_rows": variants,
    }


def _target_season_learning_rates(
    *,
    research_db_path: str | Path,
    readiness_report: dict[str, Any],
    statistic: str,
    target_season: str,
    scope_seasons_by_competition: dict[str, list[str]],
) -> tuple[dict[str, float], dict[str, dict[str, Any]]]:
    learning_rates: dict[str, float] = {}
    selection_metadata: dict[str, dict[str, Any]] = {}
    for competition_id, season_codes in scope_seasons_by_competition.items():
        if target_season not in season_codes:
            continue
        target_index = season_codes.index(target_season)
        prior_seasons = season_codes[:target_index]
        if not prior_seasons:
            continue
        selection = _select_gap_lambda_for_target_season(
            research_db_path=research_db_path,
            readiness_report=readiness_report,
            statistic=statistic,
            competition_id=competition_id,
            prior_seasons=prior_seasons,
            target_season=target_season,
        )
        learning_rates[competition_id] = float(selection["selected_lambda"])
        selection_metadata[competition_id] = selection
    return learning_rates, selection_metadata


def build_route_a_training_rows(
    *,
    research_db_path: str,
    fit_result: dict[str, Any] | None = None,
) -> list[RouteATrainingRow]:
    del fit_result
    readiness_report = generate_gap_data_readiness_report(research_db_path=research_db_path)
    route_a_scope = _approved_route_a_scope(readiness_report=readiness_report)
    scope_seasons_by_competition = _scope_seasons_by_competition(route_a_scope)
    clean_seasons = sorted({season for seasons in scope_seasons_by_competition.values() for season in seasons[1:]})
    goal_outcomes = _load_goal_outcomes(research_db_path=research_db_path)
    training_rows: list[RouteATrainingRow] = []

    for target_season in clean_seasons:
        replay_results: dict[str, dict[str, Any]] = {}
        selection_metadata_by_statistic: dict[str, dict[str, dict[str, Any]]] = {}
        for statistic in ROUTE_A_REQUIRED_GAP_STATISTICS:
            learning_rates, selection_metadata = _target_season_learning_rates(
                research_db_path=research_db_path,
                readiness_report=readiness_report,
                statistic=statistic,
                target_season=target_season,
                scope_seasons_by_competition=scope_seasons_by_competition,
            )
            replay_seasons = sorted(
                {
                    season_code
                    for competition_id, season_codes in scope_seasons_by_competition.items()
                    if competition_id in learning_rates
                    for season_code in season_codes[: season_codes.index(target_season) + 1]
                }
            )
            replay_results[statistic] = _run_route_a_gap_replay(
                research_db_path=research_db_path,
                readiness_report=readiness_report,
                statistic=statistic,
                learning_rates_by_competition=learning_rates,
                season_codes=replay_seasons,
                evaluation_season_codes=[target_season],
                competition_ids=sorted(learning_rates),
                model_version=_route_a_gap_model_version(statistic, target_season),
            )
            selection_metadata_by_statistic[statistic] = selection_metadata

        by_stat_event_id = {
            statistic: {
                str(row["event_id"]): row
                for row in replay_results[statistic]["evaluation_rows"]
                if str(row["season_code"]) == target_season
            }
            for statistic in ROUTE_A_REQUIRED_GAP_STATISTICS
        }
        common_event_ids = sorted(set(by_stat_event_id["shots"]) & set(by_stat_event_id["corners"]) & set(goal_outcomes))
        for event_id in common_event_ids:
            shots_row = by_stat_event_id["shots"][event_id]
            corners_row = by_stat_event_id["corners"][event_id]
            if shots_row["training_cutoff"] is None or corners_row["training_cutoff"] is None:
                raise ValueError(f"missing pre-match training cutoff for event_id={event_id!r}")
            competition_id = str(shots_row["competition_id"])
            shot_selection = selection_metadata_by_statistic["shots"][competition_id]
            corner_selection = selection_metadata_by_statistic["corners"][competition_id]
            row = RouteATrainingRow(
                event_id=event_id,
                competition_id=competition_id,
                competition_name=str(shots_row["competition_name"]),
                season_code=str(shots_row["season_code"]),
                division_code=str(shots_row["division_code"]),
                match_date=str(shots_row["match_date"]),
                kickoff_time=str(shots_row["kickoff_time"]),
                kickoff_at=str(shots_row["kickoff_at"]),
                as_of=str(shots_row["as_of"]),
                predicted_home_shots=float(shots_row["expected_home"]),
                predicted_away_shots=float(shots_row["expected_away"]),
                predicted_home_corners=float(corners_row["expected_home"]),
                predicted_away_corners=float(corners_row["expected_away"]),
                home_team_pre_match_competition_matches=int(shots_row["home_team_pre_match_competition_matches"]),
                away_team_pre_match_competition_matches=int(shots_row["away_team_pre_match_competition_matches"]),
                actual_home_goals=int(goal_outcomes[event_id]["actual_home_goals"]),
                actual_away_goals=int(goal_outcomes[event_id]["actual_away_goals"]),
                gap_model_version_shots=str(shots_row["model_version"]),
                gap_model_version_corners=str(corners_row["model_version"]),
                gap_learning_rate_shots=float(shots_row["learning_rate"]),
                gap_learning_rate_corners=float(corners_row["learning_rate"]),
                gap_training_cutoff_shots=str(shots_row["training_cutoff"]),
                gap_training_cutoff_corners=str(corners_row["training_cutoff"]),
                gap_selection_cutoff_shots=str(shot_selection["selection_cutoff"]),
                gap_selection_cutoff_corners=str(corner_selection["selection_cutoff"]),
                route_a_feature_version=ROUTE_A_FEATURE_VERSION,
            )
            kickoff_at = _parse_utc_timestamp(row.kickoff_at)
            if _parse_utc_timestamp(row.as_of) > kickoff_at:
                raise ValueError(f"Route A chronology violation for event_id={row.event_id!r}: as_of exceeds kickoff")
            if _parse_utc_timestamp(row.gap_training_cutoff_shots) >= kickoff_at:
                raise ValueError(f"Route A chronology violation for event_id={row.event_id!r}: shots training cutoff is not historical")
            if _parse_utc_timestamp(row.gap_training_cutoff_corners) >= kickoff_at:
                raise ValueError(f"Route A chronology violation for event_id={row.event_id!r}: corners training cutoff is not historical")
            if _parse_utc_timestamp(row.gap_selection_cutoff_shots) >= kickoff_at:
                raise ValueError(f"Route A chronology violation for event_id={row.event_id!r}: shots selection cutoff is not historical")
            if _parse_utc_timestamp(row.gap_selection_cutoff_corners) >= kickoff_at:
                raise ValueError(f"Route A chronology violation for event_id={row.event_id!r}: corners selection cutoff is not historical")
            training_rows.append(row)
    return sorted(training_rows, key=lambda row: (row.match_date, row.kickoff_time, row.event_id))


def _validation_windows(rows: Sequence[RouteATrainingRow]) -> list[dict[str, Any]]:
    seasons = _season_order(rows)
    if len(seasons) < 3:
        raise ValueError("Route A validation requires at least three clean seasons")
    windows = []
    for test_index in range(2, len(seasons)):
        windows.append(
            {
                "fold_id": f"route_a_fold_{len(windows) + 1}",
                "training_seasons": list(seasons[: test_index - 1]),
                "calibration_seasons": [seasons[test_index - 1]],
                "test_season": seasons[test_index],
            }
        )
    return windows


def _bootstrap_delta_summary(
    baseline_probabilities: Sequence[float],
    model_probabilities: Sequence[float],
    outcomes: Sequence[int],
) -> dict[str, Any]:
    if not baseline_probabilities:
        return {"iterations": 0, "mean_delta_log_loss": float("nan"), "log_loss_ci95": [float("nan"), float("nan")]}
    ll_deltas = np.asarray(
        [
            _loss_row(float(baseline_probability), int(outcome)) - _loss_row(float(model_probability), int(outcome))
            for baseline_probability, model_probability, outcome in zip(baseline_probabilities, model_probabilities, outcomes)
        ],
        dtype=np.float64,
    )
    brier_deltas = np.asarray(
        [
            (float(baseline_probability) - int(outcome)) ** 2 - (float(model_probability) - int(outcome)) ** 2
            for baseline_probability, model_probability, outcome in zip(baseline_probabilities, model_probabilities, outcomes)
        ],
        dtype=np.float64,
    )
    rng = np.random.default_rng(20260828)
    ll_samples: list[float] = []
    brier_samples: list[float] = []
    for _ in range(ROUTE_A_BOOTSTRAP_ITERATIONS):
        sample_indices = rng.integers(0, len(ll_deltas), size=len(ll_deltas))
        ll_samples.append(float(np.mean(ll_deltas[sample_indices])))
        brier_samples.append(float(np.mean(brier_deltas[sample_indices])))
    return {
        "iterations": ROUTE_A_BOOTSTRAP_ITERATIONS,
        "mean_delta_log_loss": float(np.mean(ll_deltas)),
        "mean_delta_brier": float(np.mean(brier_deltas)),
        "log_loss_ci95": [float(np.quantile(ll_samples, 0.025)), float(np.quantile(ll_samples, 0.975))],
        "brier_ci95": [float(np.quantile(brier_samples, 0.025)), float(np.quantile(brier_samples, 0.975))],
    }


def _competition_metrics(
    evaluation_rows: Sequence[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evaluation_rows:
        grouped[str(row["competition_id"])].append(row)
    results: dict[str, dict[str, Any]] = {}
    for competition_id, competition_rows in sorted(grouped.items()):
        outcomes = [int(row["outcome"]) for row in competition_rows]
        baseline_probabilities = [float(row["baseline_probability"]) for row in competition_rows]
        model_probabilities = [float(row["model_probability"]) for row in competition_rows]
        results[competition_id] = {
            "sample_count": len(competition_rows),
            "baseline": _summarize_probabilities(baseline_probabilities, outcomes),
            "route_a": _summarize_probabilities(model_probabilities, outcomes),
            "delta_log_loss": float(log_loss(baseline_probabilities, outcomes) - log_loss(model_probabilities, outcomes)),
            "delta_brier": float(brier(baseline_probabilities, outcomes) - brier(model_probabilities, outcomes)),
        }
    return results


def _market_verdict(
    *,
    aggregate_baseline: dict[str, Any],
    aggregate_model: dict[str, Any],
    folds: Sequence[dict[str, Any]],
) -> str:
    if not folds:
        return ROUTE_A_MARKET_STATUS_REJECTED
    if not all(float(fold["delta_log_loss"]) > 0.0 and float(fold["delta_brier"]) > 0.0 for fold in folds):
        return ROUTE_A_MARKET_STATUS_REJECTED
    if float(aggregate_model["log_loss"]) >= float(aggregate_baseline["log_loss"]):
        return ROUTE_A_MARKET_STATUS_REJECTED
    if float(aggregate_model["brier"]) >= float(aggregate_baseline["brier"]):
        return ROUTE_A_MARKET_STATUS_REJECTED
    return ROUTE_A_MARKET_STATUS_VALIDATED


def _load_route_b_probability_index() -> dict[tuple[str, str], float]:
    path = Path(__file__).resolve().parents[4] / "research" / "product_outputs" / "football" / "latest" / "football_model_prices.jsonl"
    if not path.exists():
        return {}
    index: dict[tuple[str, str], float] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            payload = json.loads(line)
            market_family = str(payload.get("market_family") or "")
            event_id = str(payload.get("event_id") or "")
            if market_family == "btts" and str(payload.get("selection")) == "YES":
                index[(event_id, MARKET_KEY_BTTS)] = float(payload["effective_win_stake_probability"])
            elif market_family == "goals_total" and payload.get("line") == 2.5 and str(payload.get("selection")) == "OVER":
                index[(event_id, MARKET_KEY_OU25)] = float(payload["effective_win_stake_probability"])
    return index


def _diversity_summary(
    *,
    market_key: str,
    evaluation_rows: Sequence[dict[str, Any]],
    route_b_index: dict[tuple[str, str], float],
) -> dict[str, Any]:
    positive_selection = str(MARKET_DEFINITIONS[market_key]["positive_selection"])
    negative_selection = str(MARKET_DEFINITIONS[market_key]["negative_selection"])
    aligned = []
    for row in evaluation_rows:
        route_b_probability = route_b_index.get((str(row["event_id"]), market_key))
        if route_b_probability is None:
            continue
        route_a_probability = float(row["model_probability"])
        route_a_selection = positive_selection if route_a_probability >= 0.5 else negative_selection
        route_b_selection = positive_selection if float(route_b_probability) >= 0.5 else negative_selection
        spread = abs(route_a_probability - float(route_b_probability))
        aligned.append(
            {
                "outcome": int(row["outcome"]),
                "route_a_probability": route_a_probability,
                "route_b_probability": float(route_b_probability),
                "same_direction": route_a_selection == route_b_selection,
                "consensus_pass": route_a_selection == route_b_selection and spread <= 0.06 + 1e-12,
                "spread": spread,
            }
        )
    if not aligned:
        return {"pair_count": 0}

    def subset_metrics(subset: list[dict[str, Any]]) -> dict[str, Any]:
        if not subset:
            return {"sample_count": 0}
        outcomes = [row["outcome"] for row in subset]
        route_a_probabilities = [row["route_a_probability"] for row in subset]
        route_b_probabilities = [row["route_b_probability"] for row in subset]
        return {
            "sample_count": len(subset),
            "route_a_log_loss": log_loss(route_a_probabilities, outcomes),
            "route_b_log_loss": log_loss(route_b_probabilities, outcomes),
            "route_a_brier": brier(route_a_probabilities, outcomes),
            "route_b_brier": brier(route_b_probabilities, outcomes),
            "positive_hit_rate": float(sum(outcomes) / len(outcomes)),
        }

    route_a_probabilities = np.asarray([row["route_a_probability"] for row in aligned], dtype=np.float64)
    route_b_probabilities = np.asarray([row["route_b_probability"] for row in aligned], dtype=np.float64)
    disagreement_rows = [row for row in aligned if not row["same_direction"]]
    consensus_rows = [row for row in aligned if row["consensus_pass"]]
    return {
        "pair_count": len(aligned),
        "probability_correlation": float(np.corrcoef(route_a_probabilities, route_b_probabilities)[0, 1]) if len(aligned) > 1 else float("nan"),
        "same_direction_rate": float(sum(row["same_direction"] for row in aligned) / len(aligned)),
        "direction_disagreement_rate": float(sum(not row["same_direction"] for row in aligned) / len(aligned)),
        "spread_lte_0_06_rate": float(sum(row["spread"] <= 0.06 + 1e-12 for row in aligned) / len(aligned)),
        "spread_gt_0_06_rate": float(sum(row["spread"] > 0.06 + 1e-12 for row in aligned) / len(aligned)),
        "historical_consensus_pass_rate": float(sum(row["consensus_pass"] for row in aligned) / len(aligned)),
        "outcome_comparison": {
            "all_pairs": subset_metrics(aligned),
            "consensus_pass_subset": subset_metrics(consensus_rows),
            "disagreement_subset": subset_metrics(disagreement_rows),
        },
    }


def evaluate_route_a_validation(
    rows: Sequence[RouteATrainingRow],
) -> dict[str, Any]:
    windows = _validation_windows(rows)
    route_b_index = _load_route_b_probability_index()
    validation: dict[str, Any] = {
        "schema_version": "route_a_validation_v2",
        "dataset_rows": len(rows),
        "date_range": {
            "start_match_date": rows[0].match_date,
            "end_match_date": rows[-1].match_date,
        },
        "competition_categories": _competition_order(rows),
        "seasons": _season_order(rows),
        "development_windows": windows[:-1],
        "outer_test_windows": windows,
        "final_holdout_season": windows[-1]["test_season"],
        "outer_test_model_selection_guard": "Calibration selection uses only dedicated calibration seasons inside each fold, so outer test outcomes cannot influence the chosen deployment method.",
        "market_results": {},
    }

    for market_key in SUPPORTED_MARKETS:
        fold_rows: list[dict[str, Any]] = []
        evaluation_rows: list[dict[str, Any]] = []
        baseline_all: list[float] = []
        global_baseline_all: list[float] = []
        model_all: list[float] = []
        outcomes_all: list[int] = []

        for window in windows:
            train_rows = _rows_for_seasons(rows, window["training_seasons"])
            calibration_rows = _rows_for_seasons(rows, window["calibration_seasons"])
            pretest_rows = train_rows + calibration_rows
            test_rows = _rows_for_seasons(rows, [window["test_season"]])
            competition_categories = _competition_order(pretest_rows)
            x_train, means, scales = _design_matrix(train_rows, competition_categories=competition_categories)
            x_calibration, _, _ = _design_matrix(
                calibration_rows,
                competition_categories=competition_categories,
                mean_by_feature=means,
                scale_by_feature=scales,
            )
            x_test, _, _ = _design_matrix(
                test_rows,
                competition_categories=competition_categories,
                mean_by_feature=means,
                scale_by_feature=scales,
            )
            y_train = np.asarray([_target_for_market(row, market_key) for row in train_rows], dtype=np.float64)
            y_calibration = [int(_target_for_market(row, market_key)) for row in calibration_rows]
            y_test = [int(_target_for_market(row, market_key)) for row in test_rows]
            coefficients = _fit_logistic_regression(x_train, y_train)
            calibration_inputs = [_safe_probability(float(value)) for value in _predict_logistic_probabilities(x_calibration, coefficients).tolist()]
            test_inputs = [_safe_probability(float(value)) for value in _predict_logistic_probabilities(x_test, coefficients).tolist()]
            calibration_candidates = {
                "none": {
                    "selection_metrics": _summarize_probabilities(calibration_inputs, y_calibration),
                    "test_probabilities": test_inputs,
                    "calibrator": {"method": "none"},
                }
            }
            if len(set(y_calibration)) >= 2:
                platt_calibrator = _fit_platt_scaler(calibration_inputs, y_calibration)
                calibration_candidates["platt"] = {
                    "selection_metrics": _summarize_probabilities(_apply_platt_scaler(calibration_inputs, platt_calibrator), y_calibration),
                    "test_probabilities": _apply_platt_scaler(test_inputs, platt_calibrator),
                    "calibrator": platt_calibrator,
                }
                if len(calibration_rows) >= ROUTE_A_MIN_ISOTONIC_ROWS:
                    isotonic_calibrator = _fit_isotonic_regression(calibration_inputs, y_calibration)
                    calibration_candidates["isotonic"] = {
                        "selection_metrics": _summarize_probabilities(_apply_isotonic_regression(calibration_inputs, isotonic_calibrator), y_calibration),
                        "test_probabilities": _apply_isotonic_regression(test_inputs, isotonic_calibrator),
                        "calibrator": isotonic_calibrator,
                    }
            chosen_calibration = _choose_calibration_method({name: dict(payload["selection_metrics"]) for name, payload in calibration_candidates.items()})
            selected_test_probabilities = [float(value) for value in calibration_candidates[chosen_calibration]["test_probabilities"]]
            baseline_probabilities = _league_base_rate_predictions(training_rows=pretest_rows, test_rows=test_rows, market_key=market_key)
            global_baseline_probabilities = _global_base_rate_predictions(training_rows=pretest_rows, test_rows=test_rows, market_key=market_key)
            baseline_metrics = _summarize_probabilities(baseline_probabilities, y_test)
            selected_metrics = _summarize_probabilities(selected_test_probabilities, y_test)
            for row, baseline_probability, global_baseline_probability, model_probability, outcome in zip(test_rows, baseline_probabilities, global_baseline_probabilities, selected_test_probabilities, y_test):
                evaluation_rows.append(
                    {
                        "fold_id": window["fold_id"],
                        "event_id": row.event_id,
                        "competition_id": row.competition_id,
                        "season_code": row.season_code,
                        "outcome": outcome,
                        "baseline_probability": float(baseline_probability),
                        "global_baseline_probability": float(global_baseline_probability),
                        "model_probability": float(model_probability),
                    }
                )
            fold_rows.append(
                {
                    "fold_id": window["fold_id"],
                    "train_seasons": list(window["training_seasons"]),
                    "calibration_seasons": list(window["calibration_seasons"]),
                    "test_season": str(window["test_season"]),
                    "sample_count": len(test_rows),
                    "route_a_training_cutoff": max(row.kickoff_at for row in pretest_rows),
                    "gap_hyperparameter_selection_cutoff": {
                        "shots": max(row.gap_selection_cutoff_shots for row in test_rows),
                        "corners": max(row.gap_selection_cutoff_corners for row in test_rows),
                    },
                    "gap_training_cutoff": {
                        "shots": max(row.gap_training_cutoff_shots for row in test_rows),
                        "corners": max(row.gap_training_cutoff_corners for row in test_rows),
                    },
                    "baseline": baseline_metrics,
                    "global_baseline": _summarize_probabilities(global_baseline_probabilities, y_test),
                    "calibration_candidates": {name: dict(payload["selection_metrics"]) for name, payload in calibration_candidates.items()},
                    "selected_calibration_method": chosen_calibration,
                    "selected_metrics": selected_metrics,
                    "delta_log_loss": float(baseline_metrics["log_loss"]) - float(selected_metrics["log_loss"]),
                    "delta_brier": float(baseline_metrics["brier"]) - float(selected_metrics["brier"]),
                }
            )
            baseline_all.extend(float(value) for value in baseline_probabilities)
            global_baseline_all.extend(float(value) for value in global_baseline_probabilities)
            model_all.extend(float(value) for value in selected_test_probabilities)
            outcomes_all.extend(y_test)

        aggregate_baseline = _summarize_probabilities(baseline_all, outcomes_all)
        aggregate_model = _summarize_probabilities(model_all, outcomes_all)
        aggregate_global_baseline = _summarize_probabilities(global_baseline_all, outcomes_all)
        validation["market_results"][market_key] = {
            "market_definition": dict(MARKET_DEFINITIONS[market_key]),
            "folds": fold_rows,
            "league_baseline": {"name": ROUTE_A_BASELINE_NAME, **aggregate_baseline},
            "global_baseline": {"name": ROUTE_A_GLOBAL_BASELINE_NAME, **aggregate_global_baseline},
            "aggregate_route_a": aggregate_model,
            "bootstrap": _bootstrap_delta_summary(baseline_all, model_all, outcomes_all),
            "competition_results": _competition_metrics(evaluation_rows),
            "selected_calibration_method": str(fold_rows[-1]["selected_calibration_method"]),
            "market_validation_status": _market_verdict(
                aggregate_baseline=aggregate_baseline,
                aggregate_model=aggregate_model,
                folds=fold_rows,
            ),
            "evaluation_rows": evaluation_rows,
            "diversity": _diversity_summary(
                market_key=market_key,
                evaluation_rows=evaluation_rows,
                route_b_index=route_b_index,
            ),
        }

    market_validation_status = {market_key: str(result["market_validation_status"]) for market_key, result in validation["market_results"].items()}
    validated_markets = [market_key for market_key, status in market_validation_status.items() if status == ROUTE_A_MARKET_STATUS_VALIDATED]
    validation["market_validation_status"] = market_validation_status
    validation["validated_markets"] = validated_markets
    validation["validation_status"] = ROUTE_A_VALIDATION_STATUS_PASSED if validated_markets else ROUTE_A_VALIDATION_STATUS_FAILED
    validation["review_status"] = ROUTE_A_REVIEW_STATUS_PENDING
    validation["overall_route_a_status"] = ROUTE_A_REVIEW_STATUS_PENDING if validated_markets else ROUTE_A_RUNTIME_STATUS_REJECTED
    return validation


def _artifact_market_runtime_status(artifact: dict[str, Any], *, market_key: str) -> str:
    validation = dict(artifact.get("validation") or {})
    market_status = str((validation.get("market_validation_status") or {}).get(market_key) or ROUTE_A_MARKET_STATUS_REJECTED)
    review_status = str(validation.get("review_status") or ROUTE_A_REVIEW_STATUS_PENDING)
    validation_status = str(validation.get("validation_status") or ROUTE_A_VALIDATION_STATUS_FAILED)
    if market_status == ROUTE_A_MARKET_STATUS_REJECTED:
        return ROUTE_A_RUNTIME_STATUS_REJECTED
    if validation_status != ROUTE_A_VALIDATION_STATUS_PASSED:
        return ROUTE_A_RUNTIME_STATUS_INACTIVE
    if review_status != ROUTE_A_REVIEW_STATUS_APPROVED:
        return ROUTE_A_RUNTIME_STATUS_PENDING
    return ROUTE_A_RUNTIME_STATUS_SUPPORTED


def _artifact_stable_identity_payload(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": artifact["schema_version"],
        "agent_code": artifact["agent_code"],
        "model_bundle_version": artifact["model_bundle_version"],
        "feature_version": artifact["feature_version"],
        "training_cutoff": artifact["training_cutoff"],
        "supported_markets": artifact["supported_markets"],
        "validation_status": artifact["validation"]["validation_status"],
        "market_validation_status": artifact["validation"]["market_validation_status"],
        "markets": {
            market_key: {
                "model_version": artifact["markets"][market_key]["model_version"],
                "selected_calibration_method": artifact["markets"][market_key]["selected_calibration_method"],
                "preprocessing": artifact["markets"][market_key]["preprocessing"],
                "coefficients": artifact["markets"][market_key]["coefficients"],
                "calibration": artifact["markets"][market_key]["calibration"],
                "training_window": artifact["markets"][market_key]["training_window"],
                "calibration_window": artifact["markets"][market_key]["calibration_window"],
                "market_validation_status": artifact["markets"][market_key]["market_validation_status"],
            }
            for market_key in artifact["supported_markets"]
        },
    }


def _final_model_rows(
    *,
    rows: Sequence[RouteATrainingRow],
    calibration_method: str,
) -> tuple[list[RouteATrainingRow], list[RouteATrainingRow] | None]:
    seasons = _season_order(rows)
    pre_holdout_rows = _rows_for_seasons(rows, seasons[:-1])
    if calibration_method == "none":
        return pre_holdout_rows, None
    calibration_rows = _rows_for_seasons(rows, [seasons[-2]])
    fit_rows = _rows_for_seasons(rows, seasons[:-2])
    if not fit_rows or not calibration_rows:
        raise ValueError("calibrated Route A artifact requires pre-holdout fitting and calibration rows")
    return fit_rows, calibration_rows


def build_route_a_artifact(
    *,
    research_db_path: str,
    fit_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = build_route_a_training_rows(research_db_path=research_db_path, fit_result=fit_result)
    validation = evaluate_route_a_validation(rows)
    competition_categories = _competition_order(rows)
    artifact_markets: dict[str, Any] = {}
    training_cutoff = max(row.kickoff_at for row in rows if row.season_code != validation["final_holdout_season"])

    for market_key in SUPPORTED_MARKETS:
        market_result = dict(validation["market_results"][market_key])
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
                "cutoff": max(row.kickoff_at for row in calibration_rows),
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
                "cutoff": max(row.kickoff_at for row in fit_rows),
            },
            "calibration_window": calibration_window,
            "market_validation_status": market_result["market_validation_status"],
            "validation_summary": market_result,
        }

    artifact = {
        "schema_version": ROUTE_A_ARTIFACT_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "agent_code": ROUTE_A_AGENT_ID,
        "agent_name": ROUTE_A_AGENT_NAME,
        "agent_version_tag": ROUTE_A_AGENT_VERSION_TAG,
        "method_family": ROUTE_A_METHOD_FAMILY,
        "model_bundle_version": ROUTE_A_MODEL_BUNDLE_VERSION,
        "feature_version": ROUTE_A_FEATURE_VERSION,
        "model_family": ROUTE_A_MODEL_FAMILY,
        "configuration_count": ROUTE_A_CONFIGURATION_COUNT,
        "training_cutoff": training_cutoff,
        "supported_markets": list(SUPPORTED_MARKETS),
        "supported_competitions": competition_categories,
        "dataset_summary": {
            "row_count": len(rows),
            "start_match_date": rows[0].match_date,
            "end_match_date": rows[-1].match_date,
            "season_codes": _season_order(rows),
            "competition_ids": competition_categories,
            "point_in_time_contract": "Route A uses only batched pre-kickoff Day 7 GAP replays with target-season-specific historical lambda selection.",
            "leakage_controls": [
                "2122 warm-up rows are excluded from Route A evidence",
                "GAP lambda selection is nested before each target season",
                "same-kickoff GAP fixtures are predicted from a shared pre-batch state before any same-time updates are applied",
                "calibration method selection uses only dedicated calibration seasons and never outer test outcomes",
            ],
        },
        "provenance_summary": {
            "baseline_policy_id": BASELINE_POLICY_ID,
            "baseline_policy_description": BASELINE_POLICY_DESCRIPTION,
            "lambda_selection_policy_id": SELECTION_POLICY_ID,
            "lambda_selection_policy_description": SELECTION_POLICY_DESCRIPTION,
            "as_of_policy_id": AS_OF_POLICY_ID,
            "as_of_policy_description": AS_OF_POLICY_DESCRIPTION,
            "hyperparameter_cutoff_violations": 0,
            "training_cutoff_violations": 0,
            "prediction_as_of_violations": 0,
        },
        "validation": validation,
        "markets": artifact_markets,
    }
    artifact["stable_model_id"] = f"route_a_model_{canonical_hash(_artifact_stable_identity_payload(artifact))}"
    artifact["artifact_id"] = artifact["stable_model_id"]
    artifact["overall_route_a_status"] = str(validation["overall_route_a_status"])
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
    numeric_values = np.asarray([float(feature_payload[feature_name]) for feature_name in ROUTE_A_NUMERIC_FEATURES], dtype=np.float64)
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


def _resolve_runtime_status(artifact: dict[str, Any] | None, market_key: str) -> str:
    if artifact is None or market_key not in SUPPORTED_MARKETS:
        return ROUTE_A_RUNTIME_STATUS_INACTIVE
    return _artifact_market_runtime_status(artifact, market_key=market_key)


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
        return {"market_key": market_key, "prediction_status": "BLOCKED", "block_reason": "MODEL_ARTIFACT_MISSING"}
    if market_key not in SUPPORTED_MARKETS:
        return {"market_key": market_key, "prediction_status": "NOT_APPLICABLE", "block_reason": "MARKET_NOT_SUPPORTED"}
    runtime_status = _resolve_runtime_status(artifact, market_key)
    if runtime_status != ROUTE_A_RUNTIME_STATUS_SUPPORTED:
        return {"market_key": market_key, "prediction_status": "BLOCKED", "block_reason": ROUTE_A_RUNTIME_REASONS[runtime_status]}
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
        return {"market_key": market_key, "prediction_status": "NOT_APPLICABLE", "block_reason": "MARKET_NOT_SUPPORTED"}
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
        return {"market_key": market_key, "prediction_status": "BLOCKED", "block_reason": "UNSUPPORTED_COMPETITION"}
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
    if any(str(item["prediction_status"]) != "PREDICTED" for item in market_predictions):
        blockers = sorted({str(item["block_reason"]) for item in market_predictions if item["prediction_status"] != "PREDICTED"})
        return {
            "prediction_status": "BLOCKED",
            "blockers": blockers,
            "market_contexts": [
                {"market_family": MARKET_DEFINITIONS[market_key]["market_family"], "line": MARKET_DEFINITIONS[market_key]["line"]}
                for market_key in SUPPORTED_MARKETS
            ],
            "market_predictions": market_predictions,
        }
    market_rows = []
    for item in market_predictions:
        for selection, probability, complementary_probability in (
            (item["positive_selection"], float(item["positive_probability"]), float(item["negative_probability"])),
            (item["negative_selection"], float(item["negative_probability"]), float(item["positive_probability"])),
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
    return {"prediction_status": "PREDICTED", "market_rows": market_rows, "market_predictions": market_predictions}


def summarize_market_capabilities(artifact: dict[str, Any] | None) -> dict[str, str]:
    return {
        "1x2": "NOT_SUPPORTED",
        "btts": _resolve_runtime_status(artifact, MARKET_KEY_BTTS),
        "goals_total": _resolve_runtime_status(artifact, MARKET_KEY_OU25),
        "asian_handicap": "NOT_SUPPORTED",
        "corner_total": "NOT_SUPPORTED",
    }


def route_a_contract_summary(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": ROUTE_A_MODEL_FAMILY,
        "features": list(ROUTE_A_NUMERIC_FEATURES) + [ROUTE_A_COMPETITION_FEATURE],
        "targets": ["BTTS", "OVER_2_5"],
        "markets": [{"market_family": "btts", "line": None}, {"market_family": "goals_total", "line": 2.5}],
        "calibration": {market_key: artifact["markets"][market_key]["selected_calibration_method"] for market_key in SUPPORTED_MARKETS},
        "version": {market_key: artifact["markets"][market_key]["model_version"] for market_key in SUPPORTED_MARKETS},
    }


def fold_windows(validation: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "fold_id": str(window["fold_id"]),
            "train_seasons": list(window["training_seasons"]),
            "calibration_seasons": list(window["calibration_seasons"]),
            "test_season": str(window["test_season"]),
        }
        for window in validation["outer_test_windows"]
    ]


def _artifact_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_route_a_manifest(
    *,
    artifact: dict[str, Any],
    artifact_reference: str | Path,
    validation_markdown_reference: str | Path,
) -> dict[str, Any]:
    artifact_path = Path(artifact_reference)
    validation = dict(artifact["validation"])
    return {
        "schema_version": ROUTE_A_MANIFEST_SCHEMA_VERSION,
        "agent_code": ROUTE_A_AGENT_ID,
        "artifact_reference": str(artifact_path),
        "artifact_sha256": _artifact_sha256(artifact_path),
        "model_bundle_version": ROUTE_A_MODEL_BUNDLE_VERSION,
        "feature_version": ROUTE_A_FEATURE_VERSION,
        "training_cutoff": artifact["training_cutoff"],
        "validation_status": validation["validation_status"],
        "market_validation_status": dict(validation["market_validation_status"]),
        "review_status": ROUTE_A_REVIEW_STATUS_PENDING,
        "supported_markets": list(SUPPORTED_MARKETS),
        "created_at": artifact["generated_at"],
        "stable_model_id": artifact["stable_model_id"],
        "overall_route_a_status": artifact["overall_route_a_status"],
        "technical_artifacts": {
            "artifact_json": str(artifact_path),
            "validation_markdown": str(validation_markdown_reference),
        },
    }


def validate_route_a_artifact_payload(artifact: dict[str, Any]) -> tuple[bool, str | None]:
    if not isinstance(artifact, dict):
        return False, "artifact must deserialize to a JSON object"
    if artifact.get("schema_version") != ROUTE_A_ARTIFACT_SCHEMA_VERSION:
        return False, "artifact schema_version mismatch"
    if artifact.get("agent_code") != ROUTE_A_AGENT_ID:
        return False, "artifact agent_code mismatch"
    if artifact.get("model_bundle_version") != ROUTE_A_MODEL_BUNDLE_VERSION:
        return False, "artifact model_bundle_version mismatch"
    if artifact.get("feature_version") != ROUTE_A_FEATURE_VERSION:
        return False, "artifact feature_version mismatch"
    if list(artifact.get("supported_markets") or []) != list(SUPPORTED_MARKETS):
        return False, "artifact supported_markets mismatch"
    validation = dict(artifact.get("validation") or {})
    if validation.get("validation_status") not in {ROUTE_A_VALIDATION_STATUS_PASSED, ROUTE_A_VALIDATION_STATUS_FAILED}:
        return False, "artifact validation_status missing or invalid"
    if validation.get("review_status") not in {ROUTE_A_REVIEW_STATUS_PENDING, ROUTE_A_REVIEW_STATUS_APPROVED, ROUTE_A_REVIEW_STATUS_REJECTED}:
        return False, "artifact review_status missing or invalid"
    for market_key in SUPPORTED_MARKETS:
        market = dict((artifact.get("markets") or {}).get(market_key) or {})
        if not market:
            return False, f"artifact market payload missing for {market_key}"
        if market.get("model_version") != MARKET_DEFINITIONS[market_key]["model_version"]:
            return False, f"artifact model_version mismatch for {market_key}"
        if market.get("feature_version") != ROUTE_A_FEATURE_VERSION:
            return False, f"artifact feature_version mismatch for {market_key}"
    if artifact.get("artifact_id") != artifact.get("stable_model_id"):
        return False, "artifact stable model identity mismatch"
    return True, None


def validate_route_a_manifest_payload(manifest: dict[str, Any]) -> tuple[bool, str | None]:
    if not isinstance(manifest, dict):
        return False, "manifest must deserialize to a JSON object"
    if manifest.get("schema_version") != ROUTE_A_MANIFEST_SCHEMA_VERSION:
        return False, "manifest schema_version mismatch"
    if manifest.get("agent_code") != ROUTE_A_AGENT_ID:
        return False, "manifest agent_code mismatch"
    if manifest.get("model_bundle_version") != ROUTE_A_MODEL_BUNDLE_VERSION:
        return False, "manifest model_bundle_version mismatch"
    if manifest.get("feature_version") != ROUTE_A_FEATURE_VERSION:
        return False, "manifest feature_version mismatch"
    if list(manifest.get("supported_markets") or []) != list(SUPPORTED_MARKETS):
        return False, "manifest supported_markets mismatch"
    if str(manifest.get("validation_status") or "") not in {ROUTE_A_VALIDATION_STATUS_PASSED, ROUTE_A_VALIDATION_STATUS_FAILED}:
        return False, "manifest validation_status mismatch"
    if str(manifest.get("review_status") or "") not in {ROUTE_A_REVIEW_STATUS_PENDING, ROUTE_A_REVIEW_STATUS_APPROVED, ROUTE_A_REVIEW_STATUS_REJECTED}:
        return False, "manifest review_status mismatch"
    market_validation_status = dict(manifest.get("market_validation_status") or {})
    for market_key in SUPPORTED_MARKETS:
        if str(market_validation_status.get(market_key) or "") not in {ROUTE_A_MARKET_STATUS_VALIDATED, ROUTE_A_MARKET_STATUS_REJECTED}:
            return False, f"manifest market_validation_status missing for {market_key}"
    artifact_sha256 = str(manifest.get("artifact_sha256") or "")
    if len(artifact_sha256) != 64 or any(character not in "0123456789abcdef" for character in artifact_sha256):
        return False, "manifest artifact_sha256 must be lowercase hex"
    if not str(manifest.get("artifact_reference") or "").strip():
        return False, "manifest artifact_reference missing"
    return True, None


def verify_route_a_manifest_against_artifact(
    *,
    manifest: dict[str, Any],
    artifact: dict[str, Any],
    artifact_path: str | Path,
    artifact_sha256: str,
) -> tuple[bool, str | None]:
    if str(manifest.get("artifact_reference")) != str(Path(artifact_path)):
        return False, "manifest artifact_reference does not pin the loaded artifact path"
    if str(manifest.get("artifact_sha256")) != artifact_sha256:
        return False, "artifact checksum mismatch"
    if str(manifest.get("stable_model_id")) != str(artifact.get("stable_model_id")):
        return False, "stable model id mismatch"
    if str(manifest.get("training_cutoff")) != str(artifact.get("training_cutoff")):
        return False, "training cutoff mismatch"
    if dict(manifest.get("market_validation_status") or {}) != dict((artifact.get("validation") or {}).get("market_validation_status") or {}):
        return False, "market validation status mismatch"
    return True, None
