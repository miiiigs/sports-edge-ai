"""Track B finalization-mode evaluation on a held-out season."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .football_poisson_backtest import FootballPoissonBacktestConfig, run_football_poisson_backtest


@dataclass(frozen=True)
class FootballTrackBFinalizationConfig:
    research_db_path: str | Path
    competition_ids: tuple[str, ...]
    history_season_codes: tuple[str, ...]
    test_season_code: str
    model_name: str
    bookmaker: str = "Bet365"
    min_team_matches: int = 6
    prior_weight: float = 5.0
    half_life_matches: float = 18.0
    strength_regularization: float = 0.1
    optimizer_learning_rate: float = 0.6
    optimizer_max_iterations: int = 250
    dixon_coles_rho: float = -0.1
    edge_threshold: float = 0.03
    max_fair_odds: float | None = None
    allowed_selections: tuple[str, ...] = ("home", "draw", "away")


def run_track_b_finalization(config: FootballTrackBFinalizationConfig) -> dict[str, Any]:
    """Run finalization-mode evaluation for one selected model on one held-out season."""

    result = run_football_poisson_backtest(
        FootballPoissonBacktestConfig(
            research_db_path=config.research_db_path,
            season_code=config.test_season_code,
            competition_ids=config.competition_ids,
            bookmaker=config.bookmaker,
            model_name=config.model_name,
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
            history_season_codes=config.history_season_codes,
        )
    )
    return {
        "evaluation_mode": "finalization",
        "history_season_codes": list(config.history_season_codes),
        "test_season_code": config.test_season_code,
        "model_name": config.model_name,
        "test_set_status_note": (
            "If this test season has already been inspected in earlier development cycles, treat this output as historical evidence rather than a perfectly untouched final test."
        ),
        "result": result,
    }


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run Track B football finalization-mode evaluation.")
    parser.add_argument("--research-db", required=True, help="Path to football research SQLite store.")
    parser.add_argument(
        "--competition",
        action="append",
        dest="competitions",
        required=True,
        help="Competition id like ENG1 or ESP1. Repeat for multiple leagues.",
    )
    parser.add_argument(
        "--history-season",
        action="append",
        dest="history_seasons",
        required=True,
        help="History season like 2122. Repeat for multiple seasons.",
    )
    parser.add_argument("--test-season", required=True, help="Held-out season like 2526.")
    parser.add_argument("--model", required=True, help="Selected model name to finalize.")
    parser.add_argument("--bookmaker", default="Bet365", help="Bookmaker used for comparison.")
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

    result = run_track_b_finalization(
        FootballTrackBFinalizationConfig(
            research_db_path=args.research_db,
            competition_ids=tuple(args.competitions),
            history_season_codes=tuple(args.history_seasons),
            test_season_code=args.test_season,
            model_name=args.model,
            bookmaker=args.bookmaker,
            min_team_matches=args.min_team_matches,
            prior_weight=args.prior_weight,
            half_life_matches=args.half_life_matches,
            strength_regularization=args.strength_regularization,
            optimizer_learning_rate=args.optimizer_learning_rate,
            optimizer_max_iterations=args.optimizer_max_iterations,
            dixon_coles_rho=args.dixon_coles_rho,
            edge_threshold=args.edge_threshold,
            max_fair_odds=args.max_fair_odds,
            allowed_selections=tuple(args.selections) if args.selections else ("home", "draw", "away"),
        )
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
