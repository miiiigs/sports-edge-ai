"""Recency-weighted Poisson 1X2 experiment for Track B."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from .poisson_1x2 import poisson_1x2_probabilities


def _shrunk_rate(
    observed_total: float,
    sample_weight: float,
    league_average: float,
    prior_weight: float,
) -> float:
    return (observed_total + prior_weight * league_average) / (sample_weight + prior_weight)


def estimate_expected_goals_recency_weighted(
    history_rows: list[dict[str, Any]],
    *,
    home_team: str,
    away_team: str,
    min_team_matches: int = 6,
    prior_weight: float = 5.0,
    half_life_matches: float = 18.0,
) -> dict[str, Any] | None:
    """Estimate expected goals with exponential recency weighting."""

    if len(history_rows) < 20:
        return None
    if half_life_matches <= 0:
        raise ValueError("half_life_matches must be > 0")

    total_home_goals = 0.0
    total_away_goals = 0.0
    league_match_weight = 0.0
    team_home_gf = defaultdict(float)
    team_home_ga = defaultdict(float)
    team_away_gf = defaultdict(float)
    team_away_ga = defaultdict(float)
    team_home_weight = defaultdict(float)
    team_away_weight = defaultdict(float)
    team_total_weight = defaultdict(float)
    team_total_raw_n = defaultdict(int)

    for index, row in enumerate(history_rows):
        hg = row.get("home_goals")
        ag = row.get("away_goals")
        ht = row.get("home_team")
        at = row.get("away_team")
        if hg is None or ag is None or not ht or not at:
            continue

        recency_gap = len(history_rows) - index - 1
        weight = 0.5 ** (recency_gap / half_life_matches)

        total_home_goals += float(hg) * weight
        total_away_goals += float(ag) * weight
        league_match_weight += weight

        team_home_gf[ht] += float(hg) * weight
        team_home_ga[ht] += float(ag) * weight
        team_away_gf[at] += float(ag) * weight
        team_away_ga[at] += float(hg) * weight
        team_home_weight[ht] += weight
        team_away_weight[at] += weight
        team_total_weight[ht] += weight
        team_total_weight[at] += weight
        team_total_raw_n[ht] += 1
        team_total_raw_n[at] += 1

    if league_match_weight <= 0:
        return None

    if team_total_raw_n[home_team] < min_team_matches or team_total_raw_n[away_team] < min_team_matches:
        return None

    league_home_avg = total_home_goals / league_match_weight
    league_away_avg = total_away_goals / league_match_weight
    if league_home_avg <= 0 or league_away_avg <= 0:
        return None

    home_attack = (
        _shrunk_rate(team_home_gf[home_team], team_home_weight[home_team], league_home_avg, prior_weight)
        / league_home_avg
    )
    home_defense = (
        _shrunk_rate(team_home_ga[home_team], team_home_weight[home_team], league_away_avg, prior_weight)
        / league_away_avg
    )
    away_attack = (
        _shrunk_rate(team_away_gf[away_team], team_away_weight[away_team], league_away_avg, prior_weight)
        / league_away_avg
    )
    away_defense = (
        _shrunk_rate(team_away_ga[away_team], team_away_weight[away_team], league_home_avg, prior_weight)
        / league_home_avg
    )

    expected_home_goals = max(0.2, min(4.0, league_home_avg * home_attack * away_defense))
    expected_away_goals = max(0.2, min(4.0, league_away_avg * away_attack * home_defense))
    return {
        "expected_home_goals": expected_home_goals,
        "expected_away_goals": expected_away_goals,
        "league_home_avg": league_home_avg,
        "league_away_avg": league_away_avg,
        "home_team_history_n": team_total_raw_n[home_team],
        "away_team_history_n": team_total_raw_n[away_team],
        "home_team_history_weight": team_total_weight[home_team],
        "away_team_history_weight": team_total_weight[away_team],
        "home_attack": home_attack,
        "home_defense": home_defense,
        "away_attack": away_attack,
        "away_defense": away_defense,
        "half_life_matches": half_life_matches,
    }


def fit_poisson_1x2_v2_from_history(
    history_rows: list[dict[str, Any]],
    *,
    event_id: str,
    home_team: str,
    away_team: str,
    min_team_matches: int = 6,
    prior_weight: float = 5.0,
    max_goals: int = 10,
    half_life_matches: float = 18.0,
) -> dict[str, Any] | None:
    """Build a blind fair-probability prediction using recency-weighted history."""

    goal_estimate = estimate_expected_goals_recency_weighted(
        history_rows,
        home_team=home_team,
        away_team=away_team,
        min_team_matches=min_team_matches,
        prior_weight=prior_weight,
        half_life_matches=half_life_matches,
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
    weakest_history = min(
        goal_estimate["home_team_history_weight"],
        goal_estimate["away_team_history_weight"],
    )
    data_quality = min(0.97, 0.5 + 0.03 * weakest_history)

    return {
        "event_id": event_id,
        "market": "1X2",
        "probabilities": probs,
        "fair_odds": {key: 1.0 / value for key, value in probs.items()},
        "uncertainty": normalized_uncertainty,
        "data_quality": data_quality,
        "details": goal_estimate,
    }
