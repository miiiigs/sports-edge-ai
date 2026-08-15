"""Blind football fair-probability scaffold using a simple Poisson 1X2 model.

This is the first step toward a football model that actually learns from data.
It is intentionally simple and interpretable:

- use only prior football match results
- estimate team attack / defense strength with shrinkage
- convert expected goals into 1X2 probabilities via Poisson scorelines

This is not the final model. It is a baseline scaffold that can be improved
later with xG, team ratings, player availability, and stronger calibration.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any


def poisson_pmf(k: int, lam: float) -> float:
    """Return P(X=k) for Poisson(lambda)."""

    if lam <= 0:
        raise ValueError("lam must be > 0")
    return math.exp(-lam) * (lam**k) / math.factorial(k)


def poisson_1x2_probabilities(
    home_goals_mean: float,
    away_goals_mean: float,
    *,
    max_goals: int = 10,
) -> dict[str, float]:
    """Convert expected goals into 1X2 probabilities."""

    home_win = 0.0
    draw = 0.0
    away_win = 0.0
    total = 0.0
    for home_goals in range(max_goals + 1):
        p_home = poisson_pmf(home_goals, home_goals_mean)
        for away_goals in range(max_goals + 1):
            p_away = poisson_pmf(away_goals, away_goals_mean)
            p = p_home * p_away
            total += p
            if home_goals > away_goals:
                home_win += p
            elif home_goals == away_goals:
                draw += p
            else:
                away_win += p

    if total <= 0:
        raise ValueError("probability mass collapsed to zero")
    return {
        "home": home_win / total,
        "draw": draw / total,
        "away": away_win / total,
    }


def _shrunk_rate(
    observed_total: float,
    sample_count: int,
    league_average: float,
    prior_weight: float,
) -> float:
    """Shrink a team rate toward the league average."""

    return (observed_total + prior_weight * league_average) / (sample_count + prior_weight)


def estimate_expected_goals(
    history_rows: list[dict[str, Any]],
    *,
    home_team: str,
    away_team: str,
    min_team_matches: int = 6,
    prior_weight: float = 5.0,
) -> dict[str, Any] | None:
    """Estimate expected goals for one fixture from prior match history."""

    if len(history_rows) < 20:
        return None

    total_home_goals = 0
    total_away_goals = 0
    team_home_gf = defaultdict(float)
    team_home_ga = defaultdict(float)
    team_away_gf = defaultdict(float)
    team_away_ga = defaultdict(float)
    team_home_n = defaultdict(int)
    team_away_n = defaultdict(int)
    team_total_n = defaultdict(int)

    for row in history_rows:
        hg = row.get("home_goals")
        ag = row.get("away_goals")
        ht = row.get("home_team")
        at = row.get("away_team")
        if hg is None or ag is None or not ht or not at:
            continue

        total_home_goals += hg
        total_away_goals += ag

        team_home_gf[ht] += hg
        team_home_ga[ht] += ag
        team_away_gf[at] += ag
        team_away_ga[at] += hg
        team_home_n[ht] += 1
        team_away_n[at] += 1
        team_total_n[ht] += 1
        team_total_n[at] += 1

    league_match_count = sum(team_home_n.values())
    if league_match_count == 0:
        return None

    if team_total_n[home_team] < min_team_matches or team_total_n[away_team] < min_team_matches:
        return None

    league_home_avg = total_home_goals / league_match_count
    league_away_avg = total_away_goals / league_match_count
    if league_home_avg <= 0 or league_away_avg <= 0:
        return None

    home_attack = _shrunk_rate(team_home_gf[home_team], team_home_n[home_team], league_home_avg, prior_weight) / league_home_avg
    home_defense = _shrunk_rate(team_home_ga[home_team], team_home_n[home_team], league_away_avg, prior_weight) / league_away_avg
    away_attack = _shrunk_rate(team_away_gf[away_team], team_away_n[away_team], league_away_avg, prior_weight) / league_away_avg
    away_defense = _shrunk_rate(team_away_ga[away_team], team_away_n[away_team], league_home_avg, prior_weight) / league_home_avg

    expected_home_goals = max(0.2, min(4.0, league_home_avg * home_attack * away_defense))
    expected_away_goals = max(0.2, min(4.0, league_away_avg * away_attack * home_defense))
    return {
        "expected_home_goals": expected_home_goals,
        "expected_away_goals": expected_away_goals,
        "league_home_avg": league_home_avg,
        "league_away_avg": league_away_avg,
        "home_team_history_n": team_total_n[home_team],
        "away_team_history_n": team_total_n[away_team],
        "home_attack": home_attack,
        "home_defense": home_defense,
        "away_attack": away_attack,
        "away_defense": away_defense,
    }


def fit_poisson_1x2_from_history(
    history_rows: list[dict[str, Any]],
    *,
    event_id: str,
    home_team: str,
    away_team: str,
    min_team_matches: int = 6,
    prior_weight: float = 5.0,
    max_goals: int = 10,
) -> dict[str, Any] | None:
    """Build a blind fair-probability prediction for one football event."""

    goal_estimate = estimate_expected_goals(
        history_rows,
        home_team=home_team,
        away_team=away_team,
        min_team_matches=min_team_matches,
        prior_weight=prior_weight,
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
    data_quality = min(0.95, 0.45 + 0.04 * weakest_history)

    return {
        "event_id": event_id,
        "market": "1X2",
        "probabilities": probs,
        "fair_odds": {key: 1.0 / value for key, value in probs.items()},
        "uncertainty": normalized_uncertainty,
        "data_quality": data_quality,
        "details": goal_estimate,
    }
