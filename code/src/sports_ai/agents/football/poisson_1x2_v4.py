"""Dixon-Coles score correction on top of v3 opponent-adjusted strengths."""

from __future__ import annotations

import math
from typing import Any

from .poisson_1x2 import poisson_pmf
from .poisson_1x2_v3 import estimate_expected_goals_opponent_adjusted


def dixon_coles_tau(
    home_goals: int,
    away_goals: int,
    *,
    home_goals_mean: float,
    away_goals_mean: float,
    rho: float,
) -> float:
    """Return the Dixon-Coles low-score adjustment factor."""

    if home_goals == 0 and away_goals == 0:
        return 1.0 - (home_goals_mean * away_goals_mean * rho)
    if home_goals == 0 and away_goals == 1:
        return 1.0 + (home_goals_mean * rho)
    if home_goals == 1 and away_goals == 0:
        return 1.0 + (away_goals_mean * rho)
    if home_goals == 1 and away_goals == 1:
        return 1.0 - rho
    return 1.0


def dixon_coles_score_matrix(
    home_goals_mean: float,
    away_goals_mean: float,
    *,
    rho: float,
    max_goals: int = 10,
) -> list[list[float]]:
    """Build a normalized Dixon-Coles-adjusted scoreline matrix."""

    matrix: list[list[float]] = []
    total = 0.0
    for home_goals in range(max_goals + 1):
        row: list[float] = []
        p_home = poisson_pmf(home_goals, home_goals_mean)
        for away_goals in range(max_goals + 1):
            p_away = poisson_pmf(away_goals, away_goals_mean)
            tau = dixon_coles_tau(
                home_goals,
                away_goals,
                home_goals_mean=home_goals_mean,
                away_goals_mean=away_goals_mean,
                rho=rho,
            )
            p = max(0.0, p_home * p_away * tau)
            row.append(p)
            total += p
        matrix.append(row)

    if total <= 0:
        raise ValueError("probability mass collapsed to zero")
    return [[value / total for value in row] for row in matrix]


def dixon_coles_1x2_probabilities(
    home_goals_mean: float,
    away_goals_mean: float,
    *,
    rho: float,
    max_goals: int = 10,
) -> dict[str, float]:
    """Convert adjusted scoreline probabilities into 1X2 probabilities."""

    matrix = dixon_coles_score_matrix(
        home_goals_mean,
        away_goals_mean,
        rho=rho,
        max_goals=max_goals,
    )
    home = 0.0
    draw = 0.0
    away = 0.0
    for home_goals, row in enumerate(matrix):
        for away_goals, probability in enumerate(row):
            if home_goals > away_goals:
                home += probability
            elif home_goals == away_goals:
                draw += probability
            else:
                away += probability
    return {"home": home, "draw": draw, "away": away}


def fit_poisson_1x2_v4_from_history(
    history_rows: list[dict[str, Any]],
    *,
    event_id: str,
    home_team: str,
    away_team: str,
    min_team_matches: int = 6,
    prior_weight: float = 5.0,
    strength_regularization: float = 0.03,
    learning_rate: float = 0.6,
    max_iterations: int = 250,
    dixon_coles_rho: float = -0.1,
    max_goals: int = 10,
) -> dict[str, Any] | None:
    """Build a blind fair-probability prediction using v3 strengths plus Dixon-Coles correction."""

    del prior_weight

    goal_estimate = estimate_expected_goals_opponent_adjusted(
        history_rows,
        home_team=home_team,
        away_team=away_team,
        min_team_matches=min_team_matches,
        strength_regularization=strength_regularization,
        learning_rate=learning_rate,
        max_iterations=max_iterations,
    )
    if goal_estimate is None:
        return None

    probs = dixon_coles_1x2_probabilities(
        goal_estimate["expected_home_goals"],
        goal_estimate["expected_away_goals"],
        rho=dixon_coles_rho,
        max_goals=max_goals,
    )
    entropy = -sum(prob * math.log(prob) for prob in probs.values() if prob > 0)
    normalized_uncertainty = min(1.0, entropy / math.log(3))
    weakest_history = min(goal_estimate["home_team_history_n"], goal_estimate["away_team_history_n"])
    convergence_bonus = 0.03 if goal_estimate["converged"] else 0.0
    data_quality = min(0.97, 0.5 + 0.03 * weakest_history + convergence_bonus)

    details = {
        **goal_estimate,
        "dixon_coles_rho": dixon_coles_rho,
    }
    return {
        "event_id": event_id,
        "market": "1X2",
        "probabilities": probs,
        "fair_odds": {key: 1.0 / value for key, value in probs.items()},
        "uncertainty": normalized_uncertainty,
        "data_quality": data_quality,
        "details": details,
    }
