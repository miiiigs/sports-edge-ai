"""Diagnostic audit for v3 optimizer convergence behavior."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..agents.football.poisson_1x2_v3 import estimate_expected_goals_opponent_adjusted
from ..data_quality import summarize_float_distribution
from .football_poisson_backtest import (
    _connect_research_store,
    _eligible_history_rows,
    _fetch_events,
)

CHECKPOINTS = (1, 5, 10, 25, 50, 100, 150, 250, 500, 1000)
ITERATION_COMPARISONS = ((250, 500), (500, 1000), (250, 1000))


@dataclass(frozen=True)
class FootballV3OptimizerDiagnosticConfig:
    research_db_path: str | Path
    season_code: str
    competition_ids: tuple[str, ...]
    min_team_matches: int = 6
    prior_weight: float = 5.0
    strength_regularization: float = 0.03
    optimizer_learning_rate: float = 0.6
    optimizer_max_iterations: int = 1000
    history_season_codes: tuple[str, ...] | None = None
    tolerance: float = 1e-5
    output_path: str | Path | None = None


def _distribution_by_key(rows: list[dict[str, Any]], *, value_key: str, group_key: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        value = row.get(value_key)
        if value is None:
            continue
        grouped[str(row.get(group_key) or "UNKNOWN")].append(float(value))
    return {name: summarize_float_distribution(values) for name, values in sorted(grouped.items())}


def _trace_map(trace: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(item["iteration"]): item for item in trace}


def _max_probability_delta(left: dict[str, Any], right: dict[str, Any]) -> float:
    return max(
        abs(float(left["prob_home"]) - float(right["prob_home"])),
        abs(float(left["prob_draw"]) - float(right["prob_draw"])),
        abs(float(left["prob_away"]) - float(right["prob_away"])),
    )


def _trajectory_classification(trace_by_iteration: dict[int, dict[str, Any]], *, tolerance: float) -> str:
    checkpoints = [trace_by_iteration[value] for value in CHECKPOINTS if value in trace_by_iteration]
    if len(checkpoints) < 3:
        return "insufficient_trace"

    objectives = [float(item["objective"]) for item in checkpoints]
    updates = [float(item["max_update"]) for item in checkpoints]
    increasing_objective_steps = sum(
        1 for prev, curr in zip(objectives, objectives[1:], strict=False) if curr > prev + 1e-8
    )
    increasing_update_steps = sum(
        1 for prev, curr in zip(updates, updates[1:], strict=False) if curr > prev + 1e-8
    )

    item_250 = trace_by_iteration.get(250)
    item_1000 = trace_by_iteration.get(1000)
    probability_delta = None
    if item_250 is not None and item_1000 is not None:
        probability_delta = _max_probability_delta(item_250, item_1000)

    final_update = updates[-1]
    if increasing_objective_steps >= 2 or increasing_update_steps >= 3:
        return "oscillating"
    if probability_delta is not None and probability_delta < 0.005 and final_update > tolerance:
        return "stable_nonzero_update"
    if probability_delta is not None and probability_delta >= 0.005:
        return "slowly_converging"
    return "unclear"


def _trace_delta_summary(rows: list[dict[str, Any]], *, left_iteration: int, right_iteration: int) -> dict[str, Any]:
    objective_improvements: list[float] = []
    max_update_ratios: list[float] = []
    probability_deltas: list[float] = []
    expected_goal_deltas: list[float] = []

    for row in rows:
        trace = row.get("trace_by_iteration") or {}
        left = trace.get(left_iteration)
        right = trace.get(right_iteration)
        if not left or not right:
            continue
        objective_improvements.append(float(left["objective"]) - float(right["objective"]))
        if float(left["max_update"]) > 0:
            max_update_ratios.append(float(right["max_update"]) / float(left["max_update"]))
        probability_deltas.append(_max_probability_delta(left, right))
        expected_goal_deltas.append(
            max(
                abs(float(left["expected_home_goals"]) - float(right["expected_home_goals"])),
                abs(float(left["expected_away_goals"]) - float(right["expected_away_goals"])),
            )
        )

    return {
        "pair": f"{left_iteration}_to_{right_iteration}",
        "objective_improvement": summarize_float_distribution(objective_improvements),
        "max_update_ratio": summarize_float_distribution(max_update_ratios),
        "max_probability_delta": summarize_float_distribution(probability_deltas),
        "max_expected_goal_delta": summarize_float_distribution(expected_goal_deltas),
        "material_probability_change_count": sum(1 for value in probability_deltas if value >= 0.005),
        "material_probability_change_rate": (
            sum(1 for value in probability_deltas if value >= 0.005) / len(probability_deltas)
            if probability_deltas
            else None
        ),
    }


def run_football_v3_optimizer_diagnostic(
    config: FootballV3OptimizerDiagnosticConfig,
) -> dict[str, Any]:
    """Measure how v3 optimizer diagnostics behave across historical events."""

    with _connect_research_store(config.research_db_path) as conn:
        all_events = _fetch_events(conn, competition_ids=config.competition_ids)

    by_competition_history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    rows: list[dict[str, Any]] = []
    convergence_counter: Counter[str] = Counter()
    trajectory_counter: Counter[str] = Counter()
    near_tolerance_counter: Counter[str] = Counter()

    for row in all_events:
        event = dict(row)
        competition_id = event["competition_id"]
        history = by_competition_history[competition_id]

        if event["season_code"] != config.season_code:
            history.append(event)
            continue

        eligible_history = _eligible_history_rows(
            history,
            history_season_codes=config.history_season_codes,
        )
        estimate = estimate_expected_goals_opponent_adjusted(
            eligible_history,
            home_team=event["home_team"],
            away_team=event["away_team"],
            min_team_matches=config.min_team_matches,
            strength_regularization=config.strength_regularization,
            learning_rate=config.optimizer_learning_rate,
            max_iterations=config.optimizer_max_iterations,
            tolerance=config.tolerance,
            trajectory_checkpoints=CHECKPOINTS,
        )
        if estimate is None:
            history.append(event)
            continue

        trace = estimate.get("optimization_trace") or []
        trace_by_iteration = _trace_map(trace)
        converged = estimate.get("converged")
        last_max_update = float(estimate.get("last_max_update") or 0.0)
        tolerance_ratio = last_max_update / config.tolerance if config.tolerance > 0 else None
        label = "converged" if converged is True else "non_converged"
        convergence_counter[label] += 1

        if tolerance_ratio is not None:
            if tolerance_ratio <= 2:
                near_tolerance_counter["<=2x_tolerance"] += 1
            if tolerance_ratio <= 5:
                near_tolerance_counter["<=5x_tolerance"] += 1
            if tolerance_ratio <= 10:
                near_tolerance_counter["<=10x_tolerance"] += 1
            if tolerance_ratio <= 25:
                near_tolerance_counter["<=25x_tolerance"] += 1

        trajectory_class = _trajectory_classification(trace_by_iteration, tolerance=config.tolerance)
        trajectory_counter[trajectory_class] += 1

        rows.append(
            {
                "event_id": event["event_id"],
                "competition_id": competition_id,
                "match_date": event["match_date"],
                "home_team": event["home_team"],
                "away_team": event["away_team"],
                "converged": converged,
                "last_max_update": last_max_update,
                "tolerance_ratio": tolerance_ratio,
                "expected_home_goals": estimate.get("expected_home_goals"),
                "expected_away_goals": estimate.get("expected_away_goals"),
                "trajectory_classification": trajectory_class,
                "trace_by_iteration": trace_by_iteration,
            }
        )

        history.append(event)

    report = {
        "evaluation_mode": "v3_optimizer_diagnostic",
        "model_name": "poisson_1x2_v3",
        "season_code": config.season_code,
        "history_season_codes": list(config.history_season_codes) if config.history_season_codes else None,
        "competition_ids": list(config.competition_ids),
        "optimizer_settings": {
            "strength_regularization": config.strength_regularization,
            "optimizer_learning_rate": config.optimizer_learning_rate,
            "optimizer_max_iterations": config.optimizer_max_iterations,
            "tolerance": config.tolerance,
            "checkpoints": list(CHECKPOINTS),
        },
        "coverage": {
            "predicted_events": len(rows),
            "convergence_counts": dict(sorted(convergence_counter.items())),
            "near_tolerance_counts": dict(sorted(near_tolerance_counter.items())),
            "trajectory_classification_counts": dict(sorted(trajectory_counter.items())),
        },
        "last_max_update_distribution": summarize_float_distribution(
            [float(row["last_max_update"]) for row in rows if row.get("last_max_update") is not None]
        ),
        "tolerance_ratio_distribution": summarize_float_distribution(
            [float(row["tolerance_ratio"]) for row in rows if row.get("tolerance_ratio") is not None]
        ),
        "by_competition_last_max_update": _distribution_by_key(
            rows,
            value_key="last_max_update",
            group_key="competition_id",
        ),
        "by_competition_tolerance_ratio": _distribution_by_key(
            rows,
            value_key="tolerance_ratio",
            group_key="competition_id",
        ),
        "checkpoint_distributions": {
            str(iteration): {
                "objective": summarize_float_distribution(
                    [
                        float(row["trace_by_iteration"][iteration]["objective"])
                        for row in rows
                        if iteration in row["trace_by_iteration"]
                    ]
                ),
                "max_update": summarize_float_distribution(
                    [
                        float(row["trace_by_iteration"][iteration]["max_update"])
                        for row in rows
                        if iteration in row["trace_by_iteration"]
                    ]
                ),
                "attack_l2": summarize_float_distribution(
                    [
                        float(row["trace_by_iteration"][iteration]["attack_l2"])
                        for row in rows
                        if iteration in row["trace_by_iteration"]
                    ]
                ),
                "defense_l2": summarize_float_distribution(
                    [
                        float(row["trace_by_iteration"][iteration]["defense_l2"])
                        for row in rows
                        if iteration in row["trace_by_iteration"]
                    ]
                ),
            }
            for iteration in CHECKPOINTS
        },
        "iteration_comparisons": {
            f"{left}_{right}": _trace_delta_summary(rows, left_iteration=left, right_iteration=right)
            for left, right in ITERATION_COMPARISONS
        },
        "interpretation": [
            "This is a numerical optimizer diagnostic, not a model retuning experiment.",
            "The 250 vs 500 vs 1000 comparisons show whether probabilities materially keep moving after the promoted runtime budget.",
            "If probabilities are stable while updates remain above tolerance, the criterion or optimization method may be too brittle.",
            "If probabilities continue to move materially, the current v3 fit should be treated as numerically unstable.",
        ],
        "sample_rows": [
            {
                **{
                    key: value
                    for key, value in row.items()
                    if key != "trace_by_iteration"
                },
                "trace_checkpoints": {
                    str(iteration): row["trace_by_iteration"][iteration]
                    for iteration in CHECKPOINTS
                    if iteration in row["trace_by_iteration"]
                },
            }
            for row in rows[:12]
        ],
    }

    if config.output_path is not None:
        target = Path(config.output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Diagnose historical v3 optimizer convergence behavior.")
    parser.add_argument("--research-db", required=True)
    parser.add_argument("--season", required=True)
    parser.add_argument("--competition", action="append", dest="competitions", required=True)
    parser.add_argument("--history-season", action="append", dest="history_seasons")
    parser.add_argument("--min-team-matches", type=int, default=6)
    parser.add_argument("--prior-weight", type=float, default=5.0)
    parser.add_argument("--strength-regularization", type=float, default=0.03)
    parser.add_argument("--optimizer-learning-rate", type=float, default=0.6)
    parser.add_argument("--optimizer-max-iterations", type=int, default=1000)
    parser.add_argument("--tolerance", type=float, default=1e-5)
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    report = run_football_v3_optimizer_diagnostic(
        FootballV3OptimizerDiagnosticConfig(
            research_db_path=args.research_db,
            season_code=args.season,
            competition_ids=tuple(args.competitions),
            min_team_matches=args.min_team_matches,
            prior_weight=args.prior_weight,
            strength_regularization=args.strength_regularization,
            optimizer_learning_rate=args.optimizer_learning_rate,
            optimizer_max_iterations=args.optimizer_max_iterations,
            history_season_codes=tuple(args.history_seasons) if args.history_seasons else None,
            tolerance=args.tolerance,
            output_path=args.output,
        )
    )
    print(
        json.dumps(
            {
                "output": args.output,
                "coverage": report["coverage"],
                "iteration_comparisons": report["iteration_comparisons"],
                "last_max_update_distribution": report["last_max_update_distribution"],
                "tolerance_ratio_distribution": report["tolerance_ratio_distribution"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
