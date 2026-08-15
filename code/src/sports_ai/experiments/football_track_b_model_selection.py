"""Track B development-mode model selection on validation only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .football_poisson_backtest import FootballPoissonBacktestConfig, MODEL_NAMES, run_football_poisson_backtest

MODEL_PROMOTION_SELECTIONS = ("home", "draw", "away")


@dataclass(frozen=True)
class FootballTrackBDevelopmentConfig:
    research_db_path: str | Path
    competition_ids: tuple[str, ...]
    train_season_codes: tuple[str, ...]
    validation_season_code: str
    bookmaker: str = "Bet365"
    model_names: tuple[str, ...] = MODEL_NAMES
    benchmark_model_name: str = "poisson_1x2_v1"
    min_team_matches: int = 6
    prior_weight: float = 5.0
    half_life_matches: float = 18.0
    strength_regularization: float = 0.1
    optimizer_learning_rate: float = 0.6
    optimizer_max_iterations: int = 250
    dixon_coles_rho: float = -0.1
    edge_threshold: float = 0.03
    max_fair_odds: float | None = None
    allowed_selections: tuple[str, ...] = MODEL_PROMOTION_SELECTIONS
    league_log_loss_guardrail: float = 0.02
    selection_binary_log_loss_guardrail: float = 0.02
    selection_calibration_gap_guardrail: float = 0.03


def _candidate_quality(result: dict[str, Any]) -> dict[str, Any]:
    metrics = result["policy_evaluation"]["candidate_metrics"]
    candidate_count = metrics["candidate_count"]
    roi = metrics["roi_per_bet"]
    return {
        "candidate_count": candidate_count,
        "roi_per_bet": roi,
        "enough_volume": candidate_count >= 50,
        "positive_roi": roi is not None and roi > 0,
    }


def _evaluation_summary(result: dict[str, Any]) -> dict[str, Any]:
    model_metrics = result["model_evaluation"]["prediction_metrics"]
    market_metrics = result["model_evaluation"]["market_benchmark"]
    return {
        "predicted_count": result["model_evaluation"]["predicted_count"],
        "model_log_loss": model_metrics["multiclass_log_loss"],
        "market_log_loss": market_metrics["multiclass_log_loss"],
        "log_loss_gap_vs_market": (
            None
            if model_metrics["multiclass_log_loss"] is None or market_metrics["multiclass_log_loss"] is None
            else model_metrics["multiclass_log_loss"] - market_metrics["multiclass_log_loss"]
        ),
        "model_brier": model_metrics["multiclass_brier"],
        "market_brier": market_metrics["multiclass_brier"],
        "candidate_quality": _candidate_quality(result),
    }


def _promotion_assessment(
    config: FootballTrackBDevelopmentConfig,
    *,
    benchmark_result: dict[str, Any],
    candidate_result: dict[str, Any],
) -> dict[str, Any]:
    benchmark_summary = _evaluation_summary(benchmark_result)
    candidate_summary = _evaluation_summary(candidate_result)

    benchmark_competitions = benchmark_result["model_evaluation"]["reports"]["by_competition"]
    candidate_competitions = candidate_result["model_evaluation"]["reports"]["by_competition"]
    competition_deltas = {
        competition_id: (
            None
            if benchmark_competitions[competition_id]["model_log_loss"] is None
            or candidate_competitions[competition_id]["model_log_loss"] is None
            else candidate_competitions[competition_id]["model_log_loss"]
            - benchmark_competitions[competition_id]["model_log_loss"]
        )
        for competition_id in config.competition_ids
    }

    benchmark_selections = benchmark_result["model_evaluation"]["reports"]["by_selection"]
    candidate_selections = candidate_result["model_evaluation"]["reports"]["by_selection"]
    selection_log_loss_deltas = {
        selection: (
            None
            if benchmark_selections[selection]["binary_log_loss"] is None
            or candidate_selections[selection]["binary_log_loss"] is None
            else candidate_selections[selection]["binary_log_loss"]
            - benchmark_selections[selection]["binary_log_loss"]
        )
        for selection in MODEL_PROMOTION_SELECTIONS
    }
    selection_calibration_gap_deltas = {
        selection: (
            None
            if benchmark_selections[selection]["calibration_gap"] is None
            or candidate_selections[selection]["calibration_gap"] is None
            else abs(candidate_selections[selection]["calibration_gap"])
            - abs(benchmark_selections[selection]["calibration_gap"])
        )
        for selection in MODEL_PROMOTION_SELECTIONS
    }

    better_log_loss = (
        candidate_summary["model_log_loss"] is not None
        and benchmark_summary["model_log_loss"] is not None
        and candidate_summary["model_log_loss"] < benchmark_summary["model_log_loss"]
    )
    no_worse_brier = (
        candidate_summary["model_brier"] is not None
        and benchmark_summary["model_brier"] is not None
        and candidate_summary["model_brier"] <= benchmark_summary["model_brier"]
    )
    no_major_league_degradation = all(
        delta is None or delta <= config.league_log_loss_guardrail
        for delta in competition_deltas.values()
    )
    no_major_selection_degradation = all(
        (selection_log_loss_deltas[selection] is None or selection_log_loss_deltas[selection] <= config.selection_binary_log_loss_guardrail)
        and (
            selection_calibration_gap_deltas[selection] is None
            or selection_calibration_gap_deltas[selection] <= config.selection_calibration_gap_guardrail
        )
        for selection in MODEL_PROMOTION_SELECTIONS
    )

    eligible = (
        better_log_loss
        and no_worse_brier
        and no_major_league_degradation
        and no_major_selection_degradation
    )
    return {
        "benchmark_model_name": benchmark_result["model_name"],
        "candidate_model_name": candidate_result["model_name"],
        "better_log_loss_than_benchmark": better_log_loss,
        "brier_no_worse_than_benchmark": no_worse_brier,
        "no_major_league_degradation": no_major_league_degradation,
        "no_major_selection_degradation": no_major_selection_degradation,
        "competition_log_loss_deltas_vs_benchmark": competition_deltas,
        "selection_binary_log_loss_deltas_vs_benchmark": selection_log_loss_deltas,
        "selection_abs_calibration_gap_deltas_vs_benchmark": selection_calibration_gap_deltas,
        "eligible_for_finalization": eligible,
    }


def run_track_b_model_selection(config: FootballTrackBDevelopmentConfig) -> dict[str, Any]:
    """Compare Track B football models in development mode using validation only."""

    if config.benchmark_model_name not in config.model_names:
        raise ValueError("benchmark_model_name must be included in model_names")

    validation_results: dict[str, Any] = {}
    for model_name in config.model_names:
        validation_results[model_name] = run_football_poisson_backtest(
            FootballPoissonBacktestConfig(
                research_db_path=config.research_db_path,
                season_code=config.validation_season_code,
                competition_ids=config.competition_ids,
                bookmaker=config.bookmaker,
                model_name=model_name,
                min_team_matches=config.min_team_matches,
                prior_weight=config.prior_weight,
                half_life_matches=config.half_life_matches,
                strength_regularization=config.strength_regularization,
                optimizer_learning_rate=config.optimizer_learning_rate,
                optimizer_max_iterations=config.optimizer_max_iterations,
                dixon_coles_rho=config.dixon_coles_rho,
                edge_threshold=config.edge_threshold,
                max_fair_odds=config.max_fair_odds,
                allowed_selections=config.allowed_selections,
                history_season_codes=config.train_season_codes,
            )
        )

    benchmark_result = validation_results[config.benchmark_model_name]
    promotion = {
        model_name: _promotion_assessment(
            config,
            benchmark_result=benchmark_result,
            candidate_result=result,
        )
        for model_name, result in validation_results.items()
        if model_name != config.benchmark_model_name
    }
    eligible_models = [
        model_name
        for model_name, assessment in promotion.items()
        if assessment["eligible_for_finalization"]
    ]
    selected_model_name = (
        min(
            eligible_models,
            key=lambda name: validation_results[name]["model_evaluation"]["prediction_metrics"]["multiclass_log_loss"],
        )
        if eligible_models
        else config.benchmark_model_name
    )

    return {
        "evaluation_mode": "development",
        "research_protocol": {
            "train_season_codes": list(config.train_season_codes),
            "validation_season_code": config.validation_season_code,
            "final_test_policy": "Do not inspect the test season during model development.",
            "test_set_status_note": (
                "The 2025/26 season has already been viewed historically and should not be treated as perfectly untouched for future model-family selection."
            ),
        },
        "competition_ids": list(config.competition_ids),
        "benchmark_model_name": config.benchmark_model_name,
        "selected_model_name": selected_model_name,
        "validation": {
            model_name: _evaluation_summary(result)
            for model_name, result in validation_results.items()
        },
        "promotion_assessment": promotion,
        "full_validation_results": validation_results,
    }


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run Track B football development-mode model selection.")
    parser.add_argument("--research-db", required=True, help="Path to football research SQLite store.")
    parser.add_argument(
        "--competition",
        action="append",
        dest="competitions",
        required=True,
        help="Competition id like ENG1 or ESP1. Repeat for multiple leagues.",
    )
    parser.add_argument(
        "--train-season",
        action="append",
        dest="train_seasons",
        required=True,
        help="Training season like 2122. Repeat for multiple seasons.",
    )
    parser.add_argument("--validation-season", required=True, help="Validation season like 2425.")
    parser.add_argument("--bookmaker", default="Bet365", help="Bookmaker used for comparison.")
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        choices=MODEL_NAMES,
        help="Optional model name filter. Repeat to compare multiple models.",
    )
    parser.add_argument(
        "--benchmark-model",
        default="poisson_1x2_v1",
        choices=MODEL_NAMES,
        help="Frozen benchmark used for promotion checks.",
    )
    parser.add_argument("--min-team-matches", type=int, default=6, help="Minimum prior matches per team.")
    parser.add_argument("--prior-weight", type=float, default=5.0, help="Shrinkage weight toward league average.")
    parser.add_argument("--half-life-matches", type=float, default=18.0, help="Recency half-life for v2.")
    parser.add_argument("--strength-regularization", type=float, default=0.1, help="L2 shrinkage strength for v3.")
    parser.add_argument("--optimizer-learning-rate", type=float, default=0.6, help="Optimizer learning rate for v3.")
    parser.add_argument("--optimizer-max-iterations", type=int, default=250, help="Optimizer iteration cap for v3.")
    parser.add_argument("--dixon-coles-rho", type=float, default=-0.1, help="Low-score correction parameter for v4.")
    parser.add_argument("--edge-threshold", type=float, default=0.03, help="Minimum edge for policy diagnostics.")
    parser.add_argument("--max-fair-odds", type=float, help="Optional cap on fair odds for policy diagnostics.")
    parser.add_argument(
        "--selection",
        action="append",
        dest="selections",
        help="Allowed selection key like home, draw, away. Repeat to narrow policy diagnostics.",
    )
    args = parser.parse_args(argv)

    result = run_track_b_model_selection(
        FootballTrackBDevelopmentConfig(
            research_db_path=args.research_db,
            competition_ids=tuple(args.competitions),
            train_season_codes=tuple(args.train_seasons),
            validation_season_code=args.validation_season,
            bookmaker=args.bookmaker,
            model_names=tuple(args.models) if args.models else MODEL_NAMES,
            benchmark_model_name=args.benchmark_model,
            min_team_matches=args.min_team_matches,
            prior_weight=args.prior_weight,
            half_life_matches=args.half_life_matches,
            strength_regularization=args.strength_regularization,
            optimizer_learning_rate=args.optimizer_learning_rate,
            optimizer_max_iterations=args.optimizer_max_iterations,
            dixon_coles_rho=args.dixon_coles_rho,
            edge_threshold=args.edge_threshold,
            max_fair_odds=args.max_fair_odds,
            allowed_selections=tuple(args.selections) if args.selections else MODEL_PROMOTION_SELECTIONS,
        )
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
