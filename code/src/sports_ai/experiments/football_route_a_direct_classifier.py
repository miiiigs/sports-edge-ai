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
    ROUTE_A_ARTIFACT_VERSION,
    ROUTE_A_BASELINE_NAME,
    build_route_a_artifact,
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


def _metric_lines(label: str, metrics: dict[str, Any]) -> list[str]:
    return [
        f"- `{label}` sample count: `{metrics['sample_count']}`",
        f"- `{label}` log loss: `{float(metrics['log_loss']):.6f}`",
        f"- `{label}` Brier: `{float(metrics['brier']):.6f}`",
        f"- `{label}` calibration slope: `{float(metrics['calibration_slope']):.6f}`",
        f"- `{label}` expected calibration error: `{float(metrics['expected_calibration_error']):.6f}`",
    ]


def render_route_a_validation_markdown(artifact: dict[str, Any]) -> str:
    validation = dict(artifact["validation"])
    contract = route_a_contract_summary(artifact)
    windows = fold_windows(validation)
    dataset_summary = dict(artifact["dataset_summary"])
    lines = [
        "# Route A Validation",
        "",
        f"Generated: `{artifact['generated_at']}`",
        f"Artifact id: `{artifact['artifact_id']}`",
        f"Artifact version: `{ROUTE_A_ARTIFACT_VERSION}`",
        f"Agent id: `{ROUTE_A_AGENT_ID}`",
        f"Overall status: `{artifact['overall_route_a_status']}`",
        "",
        "## Dataset",
        "",
        f"- source: `research/football_research/store/football_research.sqlite` via Day 7 selected GAP replay joins",
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
        "## Point-In-Time Controls",
        "",
    ]
    lines.extend(f"- {entry}" for entry in dataset_summary["leakage_controls"])
    lines.extend(
        [
            "",
            "## Walk-Forward Windows",
            "",
        ]
    )
    for window in windows:
        calibration = ", ".join(window["calibration_seasons"]) if window["calibration_seasons"] else "none"
        lines.append(
            f"- train `{', '.join(window['train_seasons'])}`; calibration `{calibration}`; test `{window['test_season']}`"
        )

    for market_key, market_title in (
        (MARKET_KEY_BTTS, "BTTS"),
        (MARKET_KEY_OU25, "Over 2.5"),
    ):
        result = dict(validation["market_results"][market_key])
        selected_method = str(result["selected_calibration_method"])
        selected_metrics = dict(result["selected_metrics"])
        lines.extend(
            [
                "",
                f"## {market_title}",
                "",
                f"- baseline: `{ROUTE_A_BASELINE_NAME}`",
                f"- selected calibration: `{selected_method}`",
                f"- accepted: `{'YES' if result['accepted'] else 'NO'}`",
                "",
                "### Naive baseline",
                "",
            ]
        )
        lines.extend(_metric_lines(ROUTE_A_BASELINE_NAME, dict(result["naive_baseline"])))
        lines.extend(
            [
                "",
                "### Uncalibrated logistic",
                "",
            ]
        )
        lines.extend(_metric_lines("uncalibrated", dict(result["uncalibrated_logistic"])))
        lines.extend(
            [
                "",
                "### Selected calibrated logistic",
                "",
            ]
        )
        lines.extend(_metric_lines(selected_method, selected_metrics))
        if result["calibrated_candidates"]:
            lines.extend(
                [
                    "",
                    "### Other calibration candidates",
                    "",
                ]
            )
            for calibration_name, metrics in sorted(result["calibrated_candidates"].items()):
                lines.extend(_metric_lines(calibration_name, dict(metrics)))

    lines.extend(
        [
            "",
            "## Decision",
            "",
            f"- Route A overall activation: `{artifact['overall_route_a_status']}`",
            f"- accepted markets: `{', '.join(validation['accepted_markets'])}`",
            "- calibration selection stayed evidence-based and was allowed to remain `none` where held-out calibration did not improve the market.",
            "- known limitation: the accepted baseline stays intentionally narrow to original GAP shots/corners plus league identity rather than broader engineered features.",
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

    manifest = {
        "generated_at": artifact["generated_at"],
        "artifact_version": artifact["artifact_version"],
        "artifact_id": artifact["artifact_id"],
        "overall_route_a_status": artifact["overall_route_a_status"],
        "model_as_of": artifact["model_as_of"],
        "technical_artifacts": {
            "artifact_json": str(artifact_path),
            "validation_markdown": str(validation_path),
        },
        "market_summaries": {
            market_key: {
                "accepted": artifact["validation"]["market_results"][market_key]["accepted"],
                "selected_calibration_method": artifact["validation"]["market_results"][market_key][
                    "selected_calibration_method"
                ],
                "log_loss": artifact["validation"]["market_results"][market_key]["selected_metrics"]["log_loss"],
                "brier": artifact["validation"]["market_results"][market_key]["selected_metrics"]["brier"],
            }
            for market_key in (MARKET_KEY_BTTS, MARKET_KEY_OU25)
        },
    }
    _write_json(manifest_path, manifest)
    return {
        "artifact": artifact,
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
