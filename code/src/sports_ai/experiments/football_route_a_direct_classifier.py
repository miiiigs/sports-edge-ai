"""Reproducible artifact builder for Football Route A direct classification."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..agents.football.route_a_classifier import (
    MARKET_KEY_BTTS,
    MARKET_KEY_OU25,
    ROUTE_A_AGENT_ID,
    ROUTE_A_ARTIFACT_SCHEMA_VERSION,
    ROUTE_A_BASELINE_NAME,
    ROUTE_A_GLOBAL_BASELINE_NAME,
    ROUTE_A_MODEL_BUNDLE_VERSION,
    ROUTE_A_REVIEW_STATUS_PENDING,
    build_route_a_artifact,
    build_route_a_manifest,
    fold_windows,
    route_a_contract_summary,
)

ARTIFACT_PREFIX = "football_route_a_direct_classifier"
VALIDATION_FILENAME = "ROUTE_A_VALIDATION.md"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _research_root(repo_root: Path) -> Path:
    return repo_root / "research" / "football_research"


def _product_output_root(repo_root: Path) -> Path:
    return repo_root / "research" / "product_outputs" / "football" / "route_a"


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _format_metric(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def _render_fold_table(folds: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Test Period | N | Baseline LL | Route A LL | Delta | Baseline Brier | Route A Brier | Delta | Calibration |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for fold in folds:
        baseline = fold["baseline"]
        selected = fold["selected_metrics"]
        lines.append(
            f"| {fold['test_season']} | {fold['sample_count']} | "
            f"{_format_metric(baseline['log_loss'])} | {_format_metric(selected['log_loss'])} | {_format_metric(fold['delta_log_loss'])} | "
            f"{_format_metric(baseline['brier'])} | {_format_metric(selected['brier'])} | {_format_metric(fold['delta_brier'])} | "
            f"{fold['selected_calibration_method']} |"
        )
    return lines


def render_route_a_validation_markdown(artifact: dict[str, Any]) -> str:
    validation = dict(artifact["validation"])
    contract = route_a_contract_summary(artifact)
    windows = fold_windows(validation)
    dataset_summary = dict(artifact["dataset_summary"])
    provenance = dict(artifact["provenance_summary"])
    lines = [
        "# Route A Validation",
        "",
        "## Previous Validation",
        "",
        "Status: `SUPERSEDED / INVALIDATED`",
        "- prior Route A validation used Day 7 hyperparameters selected with future seasons",
        "- prior Route A calibration choice compared candidates on outer test outcomes",
        "",
        "## Corrected Validation",
        "",
        f"Generated: `{artifact['generated_at']}`",
        f"Artifact id: `{artifact['artifact_id']}`",
        f"Artifact schema version: `{ROUTE_A_ARTIFACT_SCHEMA_VERSION}`",
        f"Model bundle version: `{ROUTE_A_MODEL_BUNDLE_VERSION}`",
        f"Agent id: `{ROUTE_A_AGENT_ID}`",
        f"Overall runtime state: `{artifact['overall_route_a_status']}`",
        "",
        "## Dataset",
        "",
        f"- source: `research/football_research/store/football_research.sqlite`",
        f"- rows: `{dataset_summary['row_count']}`",
        f"- match date range: `{dataset_summary['start_match_date']}` to `{dataset_summary['end_match_date']}`",
        f"- seasons: `{', '.join(dataset_summary['season_codes'])}`",
        f"- competitions: `{', '.join(dataset_summary['competition_ids'])}`",
        "",
        "## Contract",
        "",
        f"- model: `{contract['model']}`",
        f"- features: `{', '.join(contract['features'])}`",
        f"- targets: `{', '.join(contract['targets'])}`",
        f"- markets: `BTTS`, `goals_total 2.5`",
        "",
        "## Corrected GAP Provenance",
        "",
        f"- baseline policy: `{provenance['baseline_policy_id']}`",
        f"- lambda policy: `{provenance['lambda_selection_policy_id']}`",
        f"- as_of policy: `{provenance['as_of_policy_id']}`",
        f"- hyperparameter cutoff violations: `{provenance['hyperparameter_cutoff_violations']}`",
        f"- training cutoff violations: `{provenance['training_cutoff_violations']}`",
        f"- prediction as_of violations: `{provenance['prediction_as_of_violations']}`",
        "",
        "## Walk-Forward Windows",
        "",
    ]
    for window in windows:
        lines.append(
            f"- train `{', '.join(window['train_seasons'])}`; calibration `{', '.join(window['calibration_seasons'])}`; test `{window['test_season']}`"
        )
    lines.extend(
        [
            "",
            "## Selection Guard",
            "",
            validation["outer_test_model_selection_guard"],
        ]
    )

    for market_key, market_title in ((MARKET_KEY_BTTS, "BTTS"), (MARKET_KEY_OU25, "Over 2.5")):
        result = dict(validation["market_results"][market_key])
        lines.extend(
            [
                "",
                f"## {market_title}",
                "",
                f"- baseline: `{ROUTE_A_BASELINE_NAME}`",
                f"- secondary baseline: `{ROUTE_A_GLOBAL_BASELINE_NAME}`",
                f"- selected candidate calibration for the pre-holdout artifact: `{result['selected_calibration_method']}`",
                f"- market verdict: `{result['market_validation_status']}`",
                "",
            ]
        )
        lines.extend(_render_fold_table(list(result["folds"])))
        lines.extend(
            [
                "",
                "Aggregate:",
                f"- baseline log loss: `{_format_metric(result['league_baseline']['log_loss'])}`",
                f"- Route A log loss: `{_format_metric(result['aggregate_route_a']['log_loss'])}`",
                f"- baseline Brier: `{_format_metric(result['league_baseline']['brier'])}`",
                f"- Route A Brier: `{_format_metric(result['aggregate_route_a']['brier'])}`",
                f"- calibration intercept: `{_format_metric(result['aggregate_route_a']['calibration_intercept'])}`",
                f"- calibration slope: `{_format_metric(result['aggregate_route_a']['calibration_slope'])}`",
                f"- binned reliability slope: `{_format_metric(result['aggregate_route_a']['binned_reliability_slope'])}`",
                f"- ECE: `{_format_metric(result['aggregate_route_a']['expected_calibration_error'])}`",
                f"- bootstrap mean delta log loss: `{_format_metric(result['bootstrap']['mean_delta_log_loss'])}`",
                f"- bootstrap log loss 95% CI: `{_format_metric(result['bootstrap']['log_loss_ci95'][0])}` to `{_format_metric(result['bootstrap']['log_loss_ci95'][1])}`",
                "",
                "Competition stability:",
            ]
        )
        for competition_id, competition_result in sorted(result["competition_results"].items()):
            lines.append(
                f"- `{competition_id}`: delta log loss `{_format_metric(competition_result['delta_log_loss'])}`, "
                f"delta Brier `{_format_metric(competition_result['delta_brier'])}`, N `{competition_result['sample_count']}`"
            )
        diversity = dict(result["diversity"])
        lines.extend(
            [
                "",
                "Route A / Route B diversity:",
                f"- probability correlation: `{_format_metric(diversity.get('probability_correlation'))}`",
                f"- direction disagreement: `{_format_metric(diversity.get('direction_disagreement_rate'))}`",
                f"- spread <= 0.06: `{_format_metric(diversity.get('spread_lte_0_06_rate'))}`",
                f"- historical consensus pass rate: `{_format_metric(diversity.get('historical_consensus_pass_rate'))}`",
            ]
        )

    lines.extend(
        [
            "",
            "## Manifest",
            "",
            f"- review status: `{ROUTE_A_REVIEW_STATUS_PENDING}`",
            f"- validation status: `{validation['validation_status']}`",
            f"- validated markets: `{', '.join(validation['validated_markets']) if validation['validated_markets'] else 'none'}`",
            "",
            "## Runtime Gate",
            "",
            "- Route A remains disabled until the exact artifact checksum and independent review are approved.",
            "- Pending review and rejected markets both fail closed.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_route_a_artifacts(
    *,
    research_db_path: str | Path,
    artifact_json_out: str | Path | None = None,
    manifest_json_out: str | Path | None = None,
    validation_markdown_out: str | Path | None = None,
) -> dict[str, Any]:
    repo_root = _repo_root()
    research_root = _research_root(repo_root)
    product_output_root = _product_output_root(repo_root)
    artifact = build_route_a_artifact(research_db_path=research_db_path)
    generated_date = str(artifact["generated_at"])[:10]

    artifact_path = Path(artifact_json_out) if artifact_json_out else research_root / f"{ARTIFACT_PREFIX}_{generated_date}_artifact.json"
    manifest_path = Path(manifest_json_out) if manifest_json_out else research_root / f"{ARTIFACT_PREFIX}_{generated_date}_manifest.json"
    validation_path = Path(validation_markdown_out) if validation_markdown_out else product_output_root / VALIDATION_FILENAME

    _write_json(artifact_path, artifact)
    validation_markdown = render_route_a_validation_markdown(artifact)
    validation_path.parent.mkdir(parents=True, exist_ok=True)
    validation_path.write_text(validation_markdown, encoding="utf-8")
    manifest = build_route_a_manifest(
        artifact=artifact,
        artifact_reference=artifact_path,
        validation_markdown_reference=validation_path,
    )
    _write_json(manifest_path, manifest)
    return {
        "artifact": artifact,
        "manifest": manifest,
        "artifact_path": str(artifact_path),
        "manifest_path": str(manifest_path),
        "validation_path": str(validation_path),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--research-db-path",
        default=str(_research_root(_repo_root()) / "store" / "football_research.sqlite"),
        help="Path to the football research SQLite store.",
    )
    parser.add_argument("--artifact-json-out", help="Optional Route A artifact JSON output path.")
    parser.add_argument("--manifest-json-out", help="Optional Route A manifest JSON output path.")
    parser.add_argument("--validation-markdown-out", help="Optional validation markdown output path.")
    args = parser.parse_args()

    result = write_route_a_artifacts(
        research_db_path=args.research_db_path,
        artifact_json_out=args.artifact_json_out,
        manifest_json_out=args.manifest_json_out,
        validation_markdown_out=args.validation_markdown_out,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
