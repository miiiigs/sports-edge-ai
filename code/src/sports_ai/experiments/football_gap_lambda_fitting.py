"""Day 7 walk-forward learning-rate fitting for football GAP statistics.

This module fits GAP learning rates separately by competition and statistic for:

- shots
- shots_on_target
- corners

The fitting process:

- preserves the existing warm-up policy
- uses chronological walk-forward validation only
- reserves the latest approved season in each competition as a final holdout
- logs every tested candidate configuration
- produces selected per-competition learning rates for a downstream replay
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .football_gap_shots_walkforward import (
    DEFAULT_LEARNING_RATE,
    LEARNING_RATE_LABEL,
    STATISTIC_DISPLAY_NAMES,
    _approved_scope_rows,
    _diagnostic_bucket,
    run_gap_walkforward_experiment,
    write_gap_walkforward_artifacts,
)
from .football_gap_data_readiness import generate_gap_data_readiness_report

FIT_EXPERIMENT_ID = "EXP-010"
FIT_EXPERIMENT_VERSION = "football_gap_lambda_fitting_v1"
FIT_MODEL_VERSION = "football_gap_lambda_fitting_v1"
FITTED_RUN_LEARNING_RATE_LABEL = "WALK_FORWARD_FITTED"
FITTED_RUN_MODEL_VERSION_BY_STATISTIC = {
    "shots": "football_gap_shots_fitted_v1",
    "shots_on_target": "football_gap_shots_on_target_fitted_v1",
    "corners": "football_gap_corners_fitted_v1",
}
DEFAULT_STATISTICS = ("shots", "shots_on_target", "corners")
DEFAULT_LAMBDA_GRID = (0.02, 0.05, 0.10)
MATERIAL_IMPROVEMENT_THRESHOLD = 0.01
SELECTION_POLICY_ID = "lowest_validation_mae_with_reference_retention_threshold_v1"
SELECTION_POLICY_DESCRIPTION = (
    "For each competition/statistic, test the fixed lambda grid chronologically on pre-holdout "
    "validation folds. Rank candidates by aggregate validation combined MAE, then by lower absolute "
    "home/away bias sum, then by lower lambda. Retain the 0.05 reference lambda unless the best "
    "non-reference candidate improves combined MAE by at least the fixed material threshold."
)
FINAL_HOLDOUT_POLICY_ID = "latest_ready_season_reserved_until_post_selection_check_v1"
FINAL_HOLDOUT_POLICY_DESCRIPTION = (
    "Reserve the latest readiness-approved season per competition as a final holdout while selecting "
    "lambda. Only the finally selected lambda is evaluated on that holdout after selection completes."
)


def _competition_scope(
    *,
    readiness_report: dict[str, Any],
    statistic: str,
) -> dict[str, dict[str, Any]]:
    scope_rows = _approved_scope_rows(readiness_report, statistic)
    grouped: dict[str, dict[str, Any]] = {}
    for row in scope_rows:
        competition_id = str(row["competition_id"])
        grouped.setdefault(
            competition_id,
            {
                "competition_id": competition_id,
                "competition_name": str(row["competition_name"]),
                "division_code": str(row["division_code"]),
                "season_codes": [],
            },
        )
        grouped[competition_id]["season_codes"].append(str(row["season_code"]))
    for competition_id, payload in grouped.items():
        payload["season_codes"] = sorted(set(payload["season_codes"]))
    return dict(sorted(grouped.items()))


def _build_validation_folds(season_codes: Sequence[str]) -> list[dict[str, Any]]:
    if len(season_codes) < 3:
        return []
    folds: list[dict[str, Any]] = []
    holdout_season = season_codes[-1]
    for index in range(1, len(season_codes) - 1):
        validation_season = season_codes[index]
        training_seasons = list(season_codes[:index])
        replay_seasons = list(season_codes[: index + 1])
        folds.append(
            {
                "fold_id": f"fold_{index}",
                "training_seasons": training_seasons,
                "validation_season": validation_season,
                "replay_seasons": replay_seasons,
                "final_holdout_season": holdout_season,
            }
        )
    return folds


def _aggregate_evaluation_metrics(result: dict[str, Any]) -> dict[str, Any]:
    evaluation_rows = [row for row in result["predictions"] if row["included_in_evaluation"]]
    metrics = _diagnostic_bucket(evaluation_rows)
    return {
        "evaluation_sample_count": metrics["prediction_count"],
        "home_mae": metrics["mae_home"],
        "away_mae": metrics["mae_away"],
        "combined_mae": metrics["combined_mae"],
        "home_bias": metrics["mean_error_home"],
        "away_bias": metrics["mean_error_away"],
    }


def _bias_score(variant: dict[str, Any]) -> float:
    home_bias = variant["home_bias"]
    away_bias = variant["away_bias"]
    if home_bias is None or away_bias is None:
        return float("inf")
    return abs(home_bias) + abs(away_bias)


def _selection_sort_key(variant: dict[str, Any]) -> tuple[float, float, float]:
    combined_mae = variant["combined_mae"]
    if combined_mae is None:
        return (float("inf"), float("inf"), float("inf"))
    return (combined_mae, _bias_score(variant), float(variant["candidate_lambda"]))


def _variant_identifier(*, competition_id: str, statistic: str, candidate_lambda: float) -> str:
    return f"{competition_id.lower()}_{statistic}_lambda_{candidate_lambda:.2f}"


def fit_gap_learning_rates(
    *,
    research_db_path: str | Path,
    statistics: Sequence[str] = DEFAULT_STATISTICS,
    lambda_grid: Sequence[float] = DEFAULT_LAMBDA_GRID,
) -> dict[str, Any]:
    lambda_grid_values = [float(value) for value in lambda_grid]
    if DEFAULT_LEARNING_RATE not in lambda_grid_values:
        raise ValueError("lambda grid must include the 0.05 reference learning rate")

    readiness_report = generate_gap_data_readiness_report(research_db_path=research_db_path)
    variants: list[dict[str, Any]] = []
    selections: list[dict[str, Any]] = []
    insufficient_scope: list[dict[str, Any]] = []

    for statistic in statistics:
        competition_scope = _competition_scope(
            readiness_report=readiness_report,
            statistic=statistic,
        )
        for competition_id, scope in competition_scope.items():
            season_codes = scope["season_codes"]
            validation_folds = _build_validation_folds(season_codes)
            if not validation_folds:
                insufficient_scope.append(
                    {
                        "competition_id": competition_id,
                        "competition_name": scope["competition_name"],
                        "statistic": statistic,
                        "season_codes": season_codes,
                        "reason": "fewer than three approved seasons, so walk-forward fitting plus holdout is not possible",
                    }
                )
                continue

            competition_variants: list[dict[str, Any]] = []
            for candidate_lambda in lambda_grid_values:
                fold_records: list[dict[str, Any]] = []
                aggregate_rows: list[dict[str, Any]] = []
                for fold in validation_folds:
                    fold_result = run_gap_walkforward_experiment(
                        research_db_path=research_db_path,
                        statistic=statistic,
                        learning_rate=candidate_lambda,
                        competition_ids=[competition_id],
                        season_codes=fold["replay_seasons"],
                        evaluation_season_codes=[fold["validation_season"]],
                        learning_rate_label=LEARNING_RATE_LABEL,
                        model_version=FIT_MODEL_VERSION,
                        experiment_id=FIT_EXPERIMENT_ID,
                        experiment_version=FIT_EXPERIMENT_VERSION,
                        experiment_module="sports_ai.experiments.football_gap_lambda_fitting",
                        report_title=f"Football GAP {STATISTIC_DISPLAY_NAMES[statistic]} Lambda Fold Evaluation",
                    )
                    fold_metrics = _aggregate_evaluation_metrics(fold_result)
                    aggregate_rows.extend(
                        row
                        for row in fold_result["predictions"]
                        if row["included_in_evaluation"]
                    )
                    fold_records.append(
                        {
                            "fold_id": fold["fold_id"],
                            "training_period": {
                                "start_season": fold["training_seasons"][0],
                                "end_season": fold["training_seasons"][-1],
                                "season_codes": fold["training_seasons"],
                            },
                            "validation_period": {
                                "start_season": fold["validation_season"],
                                "end_season": fold["validation_season"],
                                "season_codes": [fold["validation_season"]],
                            },
                            **fold_metrics,
                            "model_version": FIT_MODEL_VERSION,
                        }
                    )

                aggregate_metrics = _diagnostic_bucket(aggregate_rows)
                competition_variants.append(
                    {
                        "variant_id": _variant_identifier(
                            competition_id=competition_id,
                            statistic=statistic,
                            candidate_lambda=candidate_lambda,
                        ),
                        "statistic": statistic,
                        "competition_id": competition_id,
                        "competition_name": scope["competition_name"],
                        "candidate_lambda": candidate_lambda,
                        "training_period": {
                            "start_season": validation_folds[0]["training_seasons"][0],
                            "end_season": validation_folds[-1]["training_seasons"][-1],
                            "season_codes": season_codes[:-1],
                        },
                        "validation_period": {
                            "start_season": validation_folds[0]["validation_season"],
                            "end_season": validation_folds[-1]["validation_season"],
                            "season_codes": [fold["validation_season"] for fold in validation_folds],
                        },
                        "evaluation_sample_count": aggregate_metrics["prediction_count"],
                        "home_mae": aggregate_metrics["mae_home"],
                        "away_mae": aggregate_metrics["mae_away"],
                        "combined_mae": aggregate_metrics["combined_mae"],
                        "home_bias": aggregate_metrics["mean_error_home"],
                        "away_bias": aggregate_metrics["mean_error_away"],
                        "selected_status": "rejected",
                        "selection_reason": None,
                        "model_version": FIT_MODEL_VERSION,
                        "fold_count": len(validation_folds),
                        "folds": fold_records,
                        "final_holdout_season": season_codes[-1],
                    }
                )

            competition_variants.sort(key=_selection_sort_key)
            reference_variant = next(
                variant
                for variant in competition_variants
                if variant["candidate_lambda"] == DEFAULT_LEARNING_RATE
            )
            best_variant = competition_variants[0]
            selected_variant = best_variant
            selection_reason = "selected lowest aggregate validation combined MAE from the fixed lambda grid"
            if best_variant["candidate_lambda"] != DEFAULT_LEARNING_RATE:
                improvement_vs_reference = reference_variant["combined_mae"] - best_variant["combined_mae"]
                if improvement_vs_reference < MATERIAL_IMPROVEMENT_THRESHOLD:
                    selected_variant = reference_variant
                    selection_reason = (
                        "reference lambda retained because the best non-reference candidate improved aggregate "
                        f"validation combined MAE by only {improvement_vs_reference:.6f}, below the fixed "
                        f"material threshold {MATERIAL_IMPROVEMENT_THRESHOLD:.2f}"
                    )
            for variant in competition_variants:
                if variant["variant_id"] == selected_variant["variant_id"]:
                    variant["selected_status"] = "selected"
                    variant["selection_reason"] = selection_reason
                elif selected_variant["candidate_lambda"] == DEFAULT_LEARNING_RATE:
                    variant["selection_reason"] = (
                        "rejected because the reference lambda was retained under the fixed material-improvement rule"
                    )
                else:
                    variant["selection_reason"] = (
                        "rejected because it ranked behind the selected candidate on aggregate validation quality"
                    )

            holdout_result = run_gap_walkforward_experiment(
                research_db_path=research_db_path,
                statistic=statistic,
                learning_rate=float(selected_variant["candidate_lambda"]),
                competition_ids=[competition_id],
                season_codes=season_codes,
                evaluation_season_codes=[season_codes[-1]],
                learning_rate_label=FITTED_RUN_LEARNING_RATE_LABEL,
                model_version=FITTED_RUN_MODEL_VERSION_BY_STATISTIC[statistic],
                experiment_id=FIT_EXPERIMENT_ID,
                experiment_version=FIT_EXPERIMENT_VERSION,
                experiment_module="sports_ai.experiments.football_gap_lambda_fitting",
                report_title=f"Football GAP {STATISTIC_DISPLAY_NAMES[statistic]} Holdout Check",
            )
            holdout_metrics = _aggregate_evaluation_metrics(holdout_result)
            selections.append(
                {
                    "competition_id": competition_id,
                    "competition_name": scope["competition_name"],
                    "statistic": statistic,
                    "selected_lambda": float(selected_variant["candidate_lambda"]),
                    "reference_lambda": DEFAULT_LEARNING_RATE,
                    "selected_variant_id": selected_variant["variant_id"],
                    "validation_fold_count": len(validation_folds),
                    "warmup_season": season_codes[0],
                    "validation_seasons": [fold["validation_season"] for fold in validation_folds],
                    "final_holdout_season": season_codes[-1],
                    "selected_validation_combined_mae": selected_variant["combined_mae"],
                    "reference_validation_combined_mae": reference_variant["combined_mae"],
                    "selection_reason": selection_reason,
                    "selected_holdout_metrics": holdout_metrics,
                    "model_version": FIT_MODEL_VERSION,
                }
            )
            variants.extend(competition_variants)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "experiment_id": FIT_EXPERIMENT_ID,
        "experiment_version": FIT_EXPERIMENT_VERSION,
        "source_research_db_path": str(Path(research_db_path)),
        "lambda_grid": lambda_grid_values,
        "reference_lambda": DEFAULT_LEARNING_RATE,
        "material_improvement_threshold": MATERIAL_IMPROVEMENT_THRESHOLD,
        "selection_policy_id": SELECTION_POLICY_ID,
        "selection_policy_description": SELECTION_POLICY_DESCRIPTION,
        "final_holdout_policy_id": FINAL_HOLDOUT_POLICY_ID,
        "final_holdout_policy_description": FINAL_HOLDOUT_POLICY_DESCRIPTION,
        "model_version": FIT_MODEL_VERSION,
        "scope_statistics": list(statistics),
        "variant_count": len(variants),
        "variants": variants,
        "selections": sorted(selections, key=lambda row: (row["statistic"], row["competition_id"])),
        "insufficient_scope": insufficient_scope,
        "code_identity": {
            "fitting_module": "sports_ai.experiments.football_gap_lambda_fitting",
            "replay_module": "sports_ai.experiments.football_gap_shots_walkforward",
            "engine_module": "sports_ai.agents.football.gap_ratings",
        },
    }


def _render_fit_markdown(result: dict[str, Any]) -> str:
    selection_lines = [
        "| Statistic | Competition | Selected Lambda | Validation Combined MAE | Holdout Season | Holdout Combined MAE | Reason |",
        "| --- | --- | ---: | ---: | --- | ---: | --- |",
    ]
    for selection in result["selections"]:
        selection_lines.append(
            f"| {selection['statistic']} | {selection['competition_id']} | {selection['selected_lambda']:.2f} | "
            f"{selection['selected_validation_combined_mae']:.4f} | {selection['final_holdout_season']} | "
            f"{selection['selected_holdout_metrics']['combined_mae']:.4f} | {selection['selection_reason']} |"
        )

    return "\n".join(
        [
            "# Football GAP Lambda Fitting",
            "",
            f"Generated at: `{result['generated_at']}`",
            f"Experiment/version: `{result['experiment_id']}` / `{result['experiment_version']}`",
            "",
            "## Fixed Grid",
            "",
            f"- lambda grid: `{', '.join(f'{value:.2f}' for value in result['lambda_grid'])}`",
            f"- variant count: `{result['variant_count']}`",
            f"- selection policy: `{result['selection_policy_id']}`",
            f"- final holdout policy: `{result['final_holdout_policy_id']}`",
            "",
            "## Selections",
            "",
            *selection_lines,
            "",
            "## Notes",
            "",
            "- Validation folds are chronological and stop before the reserved final holdout season.",
            "- Every tested competition/statistic/lambda combination is logged in the JSON artifact.",
        ]
    )


def write_gap_lambda_fit_artifacts(
    result: dict[str, Any],
    *,
    json_path: str | Path,
    markdown_path: str | Path,
) -> dict[str, str]:
    json_target = Path(json_path)
    markdown_target = Path(markdown_path)
    json_target.parent.mkdir(parents=True, exist_ok=True)
    markdown_target.parent.mkdir(parents=True, exist_ok=True)
    json_target.write_text(json.dumps(result, indent=2), encoding="utf-8")
    markdown_target.write_text(_render_fit_markdown(result), encoding="utf-8")
    return {
        "json_path": str(json_target),
        "markdown_path": str(markdown_target),
    }


def _selected_learning_rates_by_statistic(result: dict[str, Any]) -> dict[str, dict[str, float]]:
    learning_rates: dict[str, dict[str, float]] = {}
    for selection in result["selections"]:
        learning_rates.setdefault(selection["statistic"], {})[selection["competition_id"]] = float(
            selection["selected_lambda"]
        )
    return learning_rates


def build_selected_gap_walkforward_results(
    *,
    research_db_path: str | Path,
    fit_result: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    selected_learning_rates = _selected_learning_rates_by_statistic(fit_result)
    results: dict[str, dict[str, Any]] = {}
    for statistic, learning_rates_by_competition in sorted(selected_learning_rates.items()):
        results[statistic] = run_gap_walkforward_experiment(
            research_db_path=research_db_path,
            statistic=statistic,
            learning_rate=DEFAULT_LEARNING_RATE,
            learning_rates_by_competition=learning_rates_by_competition,
            learning_rate_label=FITTED_RUN_LEARNING_RATE_LABEL,
            model_version=FITTED_RUN_MODEL_VERSION_BY_STATISTIC[statistic],
            experiment_id=FIT_EXPERIMENT_ID,
            experiment_version=f"football_gap_{statistic}_walkforward_fitted_v1",
            experiment_module="sports_ai.experiments.football_gap_lambda_fitting",
            report_title=f"Football GAP {STATISTIC_DISPLAY_NAMES[statistic]} Walk-Forward (Fitted Lambda)",
        )
    return results


def write_selected_gap_walkforward_artifacts(
    results: dict[str, dict[str, Any]],
    *,
    output_dir: str | Path,
    date_stamp: str,
) -> dict[str, dict[str, str]]:
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    artifact_paths: dict[str, dict[str, str]] = {}
    for statistic, result in sorted(results.items()):
        stem = f"football_gap_{statistic}_walkforward_fitted_{date_stamp}"
        artifact_paths[statistic] = write_gap_walkforward_artifacts(
            result,
            manifest_path=output_root / f"{stem}_manifest.json",
            predictions_jsonl_path=output_root / f"{stem}_predictions.jsonl",
            final_state_path=output_root / f"{stem}_final_state.json",
            markdown_path=output_root / f"{stem}.md",
        )
    return artifact_paths


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Fit Day 7 football GAP learning rates.")
    parser.add_argument("--research-db", required=True, help="Path to football research SQLite store.")
    parser.add_argument("--fit-json-out", help="Optional fit artifact JSON path.")
    parser.add_argument("--fit-markdown-out", help="Optional fit artifact Markdown path.")
    parser.add_argument(
        "--selected-artifact-dir",
        help="Optional directory for selected fitted walk-forward artifacts.",
    )
    args = parser.parse_args(argv)

    result = fit_gap_learning_rates(research_db_path=args.research_db)

    payload: dict[str, Any] = {
        "experiment_id": result["experiment_id"],
        "variant_count": result["variant_count"],
        "selection_count": len(result["selections"]),
    }
    if args.fit_json_out and args.fit_markdown_out:
        payload["fit_artifacts"] = write_gap_lambda_fit_artifacts(
            result,
            json_path=args.fit_json_out,
            markdown_path=args.fit_markdown_out,
        )
    if args.selected_artifact_dir:
        selected_results = build_selected_gap_walkforward_results(
            research_db_path=args.research_db,
            fit_result=result,
        )
        payload["selected_artifacts"] = write_selected_gap_walkforward_artifacts(
            selected_results,
            output_dir=args.selected_artifact_dir,
            date_stamp=datetime.now(timezone.utc).date().isoformat(),
        )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
