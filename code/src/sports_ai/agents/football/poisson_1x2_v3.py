"""Opponent-adjusted Poisson 1X2 experiment for Track B."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

import numpy as np

from .poisson_1x2 import poisson_1x2_probabilities


def estimate_expected_goals_opponent_adjusted(
    history_rows: list[dict[str, Any]],
    *,
    home_team: str,
    away_team: str,
    min_team_matches: int = 6,
    strength_regularization: float = 0.1,
    learning_rate: float = 0.6,
    max_iterations: int = 250,
    tolerance: float = 1e-5,
) -> dict[str, Any] | None:
    """Estimate expected goals using regularized opponent-adjusted team strengths."""

    clean_rows: list[dict[str, Any]] = []
    team_match_counts = defaultdict(int)
    for row in history_rows:
        hg = row.get("home_goals")
        ag = row.get("away_goals")
        ht = row.get("home_team")
        at = row.get("away_team")
        if hg is None or ag is None or not ht or not at:
            continue
        clean_rows.append(row)
        team_match_counts[ht] += 1
        team_match_counts[at] += 1

    if len(clean_rows) < 20:
        return None
    if team_match_counts[home_team] < min_team_matches or team_match_counts[away_team] < min_team_matches:
        return None
    if strength_regularization <= 0:
        raise ValueError("strength_regularization must be > 0")
    if learning_rate <= 0:
        raise ValueError("learning_rate must be > 0")
    if max_iterations <= 0:
        raise ValueError("max_iterations must be > 0")

    teams = sorted({str(row["home_team"]) for row in clean_rows} | {str(row["away_team"]) for row in clean_rows})
    team_to_idx = {team: idx for idx, team in enumerate(teams)}

    home_idx = np.array([team_to_idx[str(row["home_team"])] for row in clean_rows], dtype=np.int64)
    away_idx = np.array([team_to_idx[str(row["away_team"])] for row in clean_rows], dtype=np.int64)
    home_goals = np.array([float(row["home_goals"]) for row in clean_rows], dtype=np.float64)
    away_goals = np.array([float(row["away_goals"]) for row in clean_rows], dtype=np.float64)

    league_home_avg = float(home_goals.mean())
    league_away_avg = float(away_goals.mean())
    if league_home_avg <= 0 or league_away_avg <= 0:
        return None

    attack = np.zeros(len(teams), dtype=np.float64)
    defense = np.zeros(len(teams), dtype=np.float64)
    mu_home = math.log(league_home_avg)
    mu_away = math.log(league_away_avg)
    sample_scale = max(1.0, float(len(clean_rows)))
    converged = False
    last_max_update = None

    for iteration in range(max_iterations):
        eta_home = np.clip(mu_home + attack[home_idx] - defense[away_idx], -3.5, 3.5)
        eta_away = np.clip(mu_away + attack[away_idx] - defense[home_idx], -3.5, 3.5)
        lambda_home = np.exp(eta_home)
        lambda_away = np.exp(eta_away)

        err_home = lambda_home - home_goals
        err_away = lambda_away - away_goals

        grad_mu_home = float(err_home.sum()) / sample_scale
        grad_mu_away = float(err_away.sum()) / sample_scale
        grad_attack = np.zeros(len(teams), dtype=np.float64)
        grad_defense = np.zeros(len(teams), dtype=np.float64)

        np.add.at(grad_attack, home_idx, err_home / sample_scale)
        np.add.at(grad_attack, away_idx, err_away / sample_scale)
        np.add.at(grad_defense, away_idx, -err_home / sample_scale)
        np.add.at(grad_defense, home_idx, -err_away / sample_scale)

        grad_attack += strength_regularization * attack
        grad_defense += strength_regularization * defense

        step = learning_rate / math.sqrt(iteration + 1)
        mu_home -= step * grad_mu_home
        mu_away -= step * grad_mu_away
        attack -= step * grad_attack
        defense -= step * grad_defense

        attack -= attack.mean()
        defense -= defense.mean()

        max_update = max(
            abs(step * grad_mu_home),
            abs(step * grad_mu_away),
            float(np.max(np.abs(step * grad_attack))),
            float(np.max(np.abs(step * grad_defense))),
        )
        last_max_update = max_update
        if max_update < tolerance:
            converged = True
            break

    expected_home_goals = float(
        max(0.2, min(4.0, math.exp(mu_home + attack[team_to_idx[home_team]] - defense[team_to_idx[away_team]])))
    )
    expected_away_goals = float(
        max(0.2, min(4.0, math.exp(mu_away + attack[team_to_idx[away_team]] - defense[team_to_idx[home_team]])))
    )

    return {
        "expected_home_goals": expected_home_goals,
        "expected_away_goals": expected_away_goals,
        "league_home_avg": league_home_avg,
        "league_away_avg": league_away_avg,
        "home_team_history_n": team_match_counts[home_team],
        "away_team_history_n": team_match_counts[away_team],
        "home_attack": float(attack[team_to_idx[home_team]]),
        "home_defense": float(defense[team_to_idx[home_team]]),
        "away_attack": float(attack[team_to_idx[away_team]]),
        "away_defense": float(defense[team_to_idx[away_team]]),
        "strength_regularization": strength_regularization,
        "learning_rate": learning_rate,
        "max_iterations": max_iterations,
        "converged": converged,
        "last_max_update": last_max_update,
    }


def fit_poisson_1x2_v3_from_history(
    history_rows: list[dict[str, Any]],
    *,
    event_id: str,
    home_team: str,
    away_team: str,
    min_team_matches: int = 6,
    prior_weight: float = 5.0,
    strength_regularization: float = 0.1,
    learning_rate: float = 0.6,
    max_iterations: int = 250,
    max_goals: int = 10,
) -> dict[str, Any] | None:
    """Build a blind fair-probability prediction using opponent-adjusted strengths."""

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

    probs = poisson_1x2_probabilities(
        goal_estimate["expected_home_goals"],
        goal_estimate["expected_away_goals"],
        max_goals=max_goals,
    )
    entropy = -sum(prob * math.log(prob) for prob in probs.values() if prob > 0)
    normalized_uncertainty = min(1.0, entropy / math.log(3))
    weakest_history = min(goal_estimate["home_team_history_n"], goal_estimate["away_team_history_n"])
    history_quality = min(0.90, 0.5 + 0.02 * weakest_history)
    if goal_estimate["converged"]:
        data_quality = min(0.94, history_quality + 0.04)
    else:
        data_quality = min(0.82, history_quality - 0.04)
        if goal_estimate.get("last_max_update") is not None:
            data_quality = max(0.55, data_quality - min(0.08, float(goal_estimate["last_max_update"]) * 100.0))

    return {
        "event_id": event_id,
        "market": "1X2",
        "probabilities": probs,
        "fair_odds": {key: 1.0 / value for key, value in probs.items()},
        "uncertainty": normalized_uncertainty,
        "data_quality": data_quality,
        "details": goal_estimate,
    }
