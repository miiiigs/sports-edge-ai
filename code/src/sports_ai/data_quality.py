"""Data-quality scoring helpers.

This module currently serves two parallel purposes:

- the original blueprint-style 0-100 rule-based score for the broader platform
- historical football DQ diagnostics for frozen-model policy research
"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import mean
from typing import Any

from .config import settings

# Deductions from 100. Tuned by backtest later.
DEDUCTIONS = {
    "missing_lineup": 8,
    "missing_injury_data": 8,
    "small_sample": 12,
    "newly_promoted_team": 6,
    "new_manager": 6,
    "returning_from_long_absence": 10,
    "sparse_challenger_history": 12,
    "missing_market_history": 4,
    "uncertain_player_identity": 15,
    "stale_news": 6,
    "single_source_only": 5,
    "missing_weather": 2,
}


def score(flags: list[str]) -> int:
    value = 100 - sum(DEDUCTIONS.get(flag, 0) for flag in flags)
    return max(0, min(100, value))


def passes_floor(dq: int, competition_id: str) -> bool:
    floor = settings.min_data_quality.get(competition_id, 75)
    return dq >= floor and dq >= 50


def band(dq: int) -> str:
    if dq >= 95:
        return "excellent"
    if dq >= 85:
        return "strong"
    if dq >= 70:
        return "acceptable"
    if dq >= 50:
        return "weak"
    return "automatic_pass"


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def summarize_float_distribution(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "n": 0,
            "min": None,
            "p10": None,
            "p25": None,
            "median": None,
            "p75": None,
            "p90": None,
            "max": None,
            "mean": None,
        }

    ordered = sorted(float(value) for value in values)

    def percentile(fraction: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        idx = (len(ordered) - 1) * fraction
        lower = int(idx)
        upper = min(len(ordered) - 1, lower + 1)
        weight = idx - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

    return {
        "n": len(ordered),
        "min": ordered[0],
        "p10": percentile(0.10),
        "p25": percentile(0.25),
        "median": percentile(0.50),
        "p75": percentile(0.75),
        "p90": percentile(0.90),
        "max": ordered[-1],
        "mean": mean(ordered),
    }


def explain_v3_data_quality(
    prediction: dict[str, Any],
    *,
    threshold: float = 0.82,
) -> dict[str, Any]:
    """Explain the existing v3 proxy data-quality score.

    This mirrors the current v3 formula so audits can separate:
    - history/sample contribution
    - convergence branch
    - last-update penalty
    """

    details = prediction.get("details") or {}
    home_history = int(details.get("home_team_history_n") or 0)
    away_history = int(details.get("away_team_history_n") or 0)
    weakest_history = min(home_history, away_history)
    history_quality = min(0.90, 0.5 + 0.02 * weakest_history)
    converged = details.get("converged")
    last_max_update = details.get("last_max_update")
    update_penalty = 0.0

    if converged:
        branch_label = "converged_bonus"
        branch_cap = 0.94
        pre_penalty_score = min(branch_cap, history_quality + 0.04)
        final_score = pre_penalty_score
    else:
        branch_label = "non_converged_cap"
        branch_cap = 0.82
        pre_penalty_score = min(branch_cap, history_quality - 0.04)
        if last_max_update is not None:
            update_penalty = min(0.08, float(last_max_update) * 100.0)
        final_score = max(0.55, pre_penalty_score - update_penalty)

    reasons: list[str] = []
    if converged is False:
        reasons.append("MODEL_NOT_CONVERGED")
    if weakest_history < 12:
        reasons.append("LIMITED_TEAM_HISTORY")
    if update_penalty > 0:
        reasons.append("NONCONVERGENCE_UPDATE_PENALTY")
    if final_score < threshold:
        reasons.append("BELOW_DQ_THRESHOLD")

    return {
        "current_v3_data_quality": prediction.get("data_quality"),
        "recomputed_v3_data_quality": final_score,
        "threshold": threshold,
        "gap_to_threshold": final_score - threshold,
        "home_team_history_n": home_history,
        "away_team_history_n": away_history,
        "weakest_history_n": weakest_history,
        "history_quality_component": history_quality,
        "converged": converged,
        "branch_label": branch_label,
        "branch_cap": branch_cap,
        "pre_penalty_score": pre_penalty_score,
        "last_max_update": last_max_update,
        "last_update_penalty": update_penalty,
        "reasons": reasons,
    }


def historical_football_data_quality_components(
    *,
    prediction: dict[str, Any],
    history_rows: list[dict[str, Any]],
    event: dict[str, Any],
    market_quote: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Build a point-in-time historical DQ contract for football research.

    This is intentionally separate from the live decision-layer DQ concept.
    Historical backtests do not have lineups, injuries, and news confidence,
    so they need an explicit point-in-time contract rather than pretending the
    live gate score is already semantically equivalent.
    """

    details = prediction.get("details") or {}
    home_team = str(event.get("home_team") or "")
    away_team = str(event.get("away_team") or "")
    clean_history = [
        row
        for row in history_rows
        if row.get("home_goals") is not None
        and row.get("away_goals") is not None
        and row.get("home_team")
        and row.get("away_team")
    ]

    weakest_history_n = min(
        int(details.get("home_team_history_n") or 0),
        int(details.get("away_team_history_n") or 0),
    )
    total_clean_history_n = len(clean_history)
    history_coverage = _clamp01(
        0.70 * min(1.0, weakest_history_n / 20.0)
        + 0.30 * min(1.0, total_clean_history_n / 80.0)
    )

    converged = details.get("converged")
    last_max_update = float(details.get("last_max_update") or 0.0)
    if converged is True:
        model_convergence = 1.0
    elif converged is False:
        model_convergence = _clamp01(1.0 - min(1.0, last_max_update / 0.002))
    else:
        model_convergence = 0.5

    required_event_fields = (
        bool(event.get("event_id")),
        bool(event.get("competition_id")),
        bool(event.get("season_code")),
        bool(event.get("match_date")),
        bool(home_team),
        bool(away_team),
    )
    event_field_score = sum(required_event_fields) / len(required_event_fields)
    market_field_score = 0.0
    if market_quote is not None:
        market_field_score = sum(
            1.0
            for selection in ("home", "draw", "away")
            if market_quote.get(selection) not in (None, 0)
        ) / 3.0
    fixture_context_completeness = 0.75 * event_field_score + 0.25 * market_field_score

    def role_count(team: str, role: str) -> int:
        return sum(1 for row in clean_history if row.get(f"{role}_team") == team)

    def season_count(team: str) -> int:
        seasons = {
            str(row.get("season_code"))
            for row in clean_history
            if row.get("home_team") == team or row.get("away_team") == team
        }
        return len({season for season in seasons if season and season != "None"})

    home_home_n = role_count(home_team, "home")
    home_away_n = role_count(home_team, "away")
    away_home_n = role_count(away_team, "home")
    away_away_n = role_count(away_team, "away")

    home_role_balance = min(1.0, min(home_home_n, home_away_n) / 4.0)
    away_role_balance = min(1.0, min(away_home_n, away_away_n) / 4.0)
    home_season_continuity = min(1.0, season_count(home_team) / 2.0)
    away_season_continuity = min(1.0, season_count(away_team) / 2.0)
    team_entity_continuity = (
        0.30 * home_role_balance
        + 0.30 * away_role_balance
        + 0.20 * home_season_continuity
        + 0.20 * away_season_continuity
    )

    historical_data_quality = _clamp01(
        0.38 * history_coverage
        + 0.27 * model_convergence
        + 0.15 * fixture_context_completeness
        + 0.20 * team_entity_continuity
    )

    reasons: list[str] = []
    if history_coverage < 0.70:
        reasons.append("LOW_HISTORY_COVERAGE")
    if model_convergence < 0.75:
        reasons.append("LOW_MODEL_CONVERGENCE")
    if fixture_context_completeness < 0.95:
        reasons.append("INCOMPLETE_HISTORICAL_CONTEXT")
    if team_entity_continuity < 0.70:
        reasons.append("LOW_TEAM_ENTITY_CONTINUITY")

    return {
        "contract_name": "historical_football_point_in_time_dq_v1",
        "historical_data_quality": historical_data_quality,
        "components": {
            "history_coverage": history_coverage,
            "model_convergence": model_convergence,
            "fixture_context_completeness": fixture_context_completeness,
            "team_entity_continuity": team_entity_continuity,
        },
        "live_only_fields_not_in_historical_contract": [
            "lineup_confidence",
            "injury_confidence",
            "news_freshness",
            "late_context_confirmation",
        ],
        "reasons": reasons,
    }


def _parse_iso_date(raw_value: str | None) -> date | None:
    text = (raw_value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _previous_season_code(season_code: str | None) -> str | None:
    text = (season_code or "").strip()
    if len(text) != 4 or not text.isdigit():
        return None
    start_year = int(text[:2])
    end_year = int(text[2:])
    return f"{(start_year - 1) % 100:02d}{(end_year - 1) % 100:02d}"


def historical_football_data_quality_components_v2(
    *,
    prediction: dict[str, Any],
    history_rows: list[dict[str, Any]],
    event: dict[str, Any],
    market_quote: dict[str, float] | None = None,
    recent_window_days: int = 180,
) -> dict[str, Any]:
    """Refined historical football DQ contract.

    This version separates:
    - sports_data_quality
    - model_fit_quality
    - market_data_quality
    - decision_data_quality

    It deliberately keeps market completeness out of sports data quality.
    """

    details = prediction.get("details") or {}
    home_team = str(event.get("home_team") or "")
    away_team = str(event.get("away_team") or "")
    event_date = _parse_iso_date(event.get("match_date"))
    current_season = str(event.get("season_code") or "")
    previous_season = _previous_season_code(current_season)

    clean_history = [
        row
        for row in history_rows
        if row.get("home_goals") is not None
        and row.get("away_goals") is not None
        and row.get("home_team")
        and row.get("away_team")
    ]

    def team_rows(team: str) -> list[dict[str, Any]]:
        return [
            row
            for row in clean_history
            if row.get("home_team") == team or row.get("away_team") == team
        ]

    home_rows = team_rows(home_team)
    away_rows = team_rows(away_team)
    home_total_n = len(home_rows)
    away_total_n = len(away_rows)
    weakest_history_n = min(home_total_n, away_total_n)

    def role_count(rows: list[dict[str, Any]], team: str, role: str) -> int:
        return sum(1 for row in rows if row.get(f"{role}_team") == team)

    home_home_n = role_count(home_rows, home_team, "home")
    home_away_n = role_count(home_rows, home_team, "away")
    away_home_n = role_count(away_rows, away_team, "home")
    away_away_n = role_count(away_rows, away_team, "away")

    recent_cutoff = event_date - timedelta(days=recent_window_days) if event_date else None

    def recent_count(rows: list[dict[str, Any]]) -> int:
        if recent_cutoff is None:
            return 0
        total = 0
        for row in rows:
            row_date = _parse_iso_date(row.get("match_date"))
            if row_date is not None and recent_cutoff <= row_date < event_date:
                total += 1
        return total

    home_recent_n = recent_count(home_rows)
    away_recent_n = recent_count(away_rows)
    weakest_recent_n = min(home_recent_n, away_recent_n)

    def prior_season_count(rows: list[dict[str, Any]]) -> int:
        seasons = {
            str(row.get("season_code"))
            for row in rows
            if row.get("season_code") not in (None, "")
        }
        return len(seasons)

    home_prior_season_count = prior_season_count(home_rows)
    away_prior_season_count = prior_season_count(away_rows)

    def seen_previous_season(rows: list[dict[str, Any]]) -> bool:
        if previous_season is None:
            return False
        return any(str(row.get("season_code")) == previous_season for row in rows)

    home_seen_previous_season = seen_previous_season(home_rows)
    away_seen_previous_season = seen_previous_season(away_rows)
    newly_observed_team = home_prior_season_count <= 1 or away_prior_season_count <= 1
    promoted_or_new_league_uncertainty = (not home_seen_previous_season) or (not away_seen_previous_season)

    sample_depth_score = min(1.0, weakest_history_n / 24.0)
    recent_coverage_score = min(1.0, weakest_recent_n / 8.0)
    role_depth_score = (
        min(1.0, min(home_home_n, home_away_n) / 6.0)
        + min(1.0, min(away_home_n, away_away_n) / 6.0)
    ) / 2.0
    continuity_score = (
        min(1.0, home_prior_season_count / 3.0)
        + min(1.0, away_prior_season_count / 3.0)
    ) / 2.0
    previous_season_score = (
        (1.0 if home_seen_previous_season else 0.45)
        + (1.0 if away_seen_previous_season else 0.45)
    ) / 2.0
    sports_data_quality = _clamp01(
        0.28 * sample_depth_score
        + 0.24 * recent_coverage_score
        + 0.20 * role_depth_score
        + 0.16 * continuity_score
        + 0.12 * previous_season_score
    )

    converged = details.get("converged")
    last_max_update = float(details.get("last_max_update") or 0.0)
    if converged is True:
        convergence_score = 1.0
    elif converged is False:
        convergence_score = _clamp01(1.0 - min(1.0, last_max_update / 0.003))
    else:
        convergence_score = 0.6
    uncertainty = float(prediction.get("uncertainty") or 1.0)
    uncertainty_score = _clamp01(1.0 - max(0.0, uncertainty - 0.80) / 0.25)
    stability_score = _clamp01(1.0 - min(1.0, last_max_update / 0.0015))
    model_fit_quality = _clamp01(
        0.45 * convergence_score
        + 0.30 * stability_score
        + 0.25 * uncertainty_score
    )

    bookmaker_available_score = 1.0 if market_quote is not None else 0.0
    all_selections_present_score = 0.0
    if market_quote is not None:
        all_selections_present_score = sum(
            1.0
            for selection in ("home", "draw", "away")
            if market_quote.get(selection) not in (None, 0)
        ) / 3.0
    timestamp_quality_score = 0.25 if market_quote is not None else 0.0
    entry_close_availability_score = 0.0
    market_data_quality = _clamp01(
        0.35 * bookmaker_available_score
        + 0.35 * all_selections_present_score
        + 0.15 * timestamp_quality_score
        + 0.15 * entry_close_availability_score
    )

    decision_data_quality = _clamp01(
        0.50 * sports_data_quality
        + 0.30 * model_fit_quality
        + 0.20 * market_data_quality
    )

    reasons: list[str] = []
    if sample_depth_score < 0.70:
        reasons.append("LOW_TEAM_SAMPLE_DEPTH")
    if recent_coverage_score < 0.60:
        reasons.append("LOW_RECENT_MATCH_COVERAGE")
    if role_depth_score < 0.60:
        reasons.append("LOW_HOME_AWAY_ROLE_DEPTH")
    if continuity_score < 0.70:
        reasons.append("LOW_PRIOR_SEASON_CONTINUITY")
    if promoted_or_new_league_uncertainty:
        reasons.append("PROMOTED_OR_NEW_LEAGUE_UNCERTAINTY")
    if converged is False:
        reasons.append("MODEL_NOT_CONVERGED")
    if uncertainty_score < 0.50:
        reasons.append("HIGH_PREDICTIVE_UNCERTAINTY")

    return {
        "contract_name": "historical_football_point_in_time_dq_v2",
        "sports_data_quality": sports_data_quality,
        "model_fit_quality": model_fit_quality,
        "market_data_quality": market_data_quality,
        "decision_data_quality": decision_data_quality,
        "components": {
            "sample_depth_score": sample_depth_score,
            "recent_coverage_score": recent_coverage_score,
            "role_depth_score": role_depth_score,
            "continuity_score": continuity_score,
            "previous_season_score": previous_season_score,
            "convergence_score": convergence_score,
            "stability_score": stability_score,
            "uncertainty_score": uncertainty_score,
            "bookmaker_available_score": bookmaker_available_score,
            "all_selections_present_score": all_selections_present_score,
            "timestamp_quality_score": timestamp_quality_score,
            "entry_close_availability_score": entry_close_availability_score,
        },
        "flags": {
            "newly_observed_team": newly_observed_team,
            "promoted_or_new_league_uncertainty": promoted_or_new_league_uncertainty,
            "home_seen_previous_season": home_seen_previous_season,
            "away_seen_previous_season": away_seen_previous_season,
            "continuity_flag": (
                "previous_season_gap"
                if promoted_or_new_league_uncertainty
                else "continuous_previous_season"
            ),
        },
        "counts": {
            "home_total_n": home_total_n,
            "away_total_n": away_total_n,
            "weakest_history_n": weakest_history_n,
            "home_recent_n": home_recent_n,
            "away_recent_n": away_recent_n,
            "weakest_recent_n": weakest_recent_n,
            "home_home_n": home_home_n,
            "home_away_n": home_away_n,
            "away_home_n": away_home_n,
            "away_away_n": away_away_n,
            "home_prior_season_count": home_prior_season_count,
            "away_prior_season_count": away_prior_season_count,
        },
        "reasons": reasons,
    }
