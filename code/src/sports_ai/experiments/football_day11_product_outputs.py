"""Product-facing Day 11 football manager outputs."""

from __future__ import annotations

import csv
import json
import os
import shutil
from pathlib import Path
from typing import Any, Iterable, Sequence

from .football_day11_manager_core import (
    DECISION_MISSING_REQUIRED_MARKET_DATA,
    DECISION_NO_BET,
    DECISION_PRELIMINARY_CANDIDATE,
    DECISION_REJECTED_RULE,
    DECISION_STALE_MARKET,
    MANAGER_VERSION,
    _DEMO_INPUT_BANNER,
    _DEMO_NOT_LIVE_BANNER,
    _DEMO_PRELIMINARY_BANNER,
)

DAY11_PRODUCT_OUTPUT_VERSION = "football_day11_product_outputs_v1"
PRODUCT_OUTPUT_MANIFEST_VERSION = "football_day11_product_output_manifest_v1"
REPRESENTATIVE_ROW_COUNT = 18


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _write_csv(path: Path, rows: Sequence[dict[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _hardlink_or_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    try:
        os.link(source, destination)
    except OSError:
        shutil.copyfile(source, destination)


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100.0:.2f}%"


def _fmt_price(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:.3f}"


def _decision_sort_key(row: dict[str, Any]) -> tuple[int, str, str, str]:
    rank = {
        DECISION_PRELIMINARY_CANDIDATE: 0,
        DECISION_NO_BET: 1,
        DECISION_REJECTED_RULE: 2,
        DECISION_MISSING_REQUIRED_MARKET_DATA: 3,
        DECISION_STALE_MARKET: 4,
    }
    return (
        rank.get(str(row["decision"]), 999),
        str(row["competition_id"]),
        str(row["kickoff_at"]),
        str(row["decision_id"]),
    )


def _flatten_csv_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision_id": row["decision_id"],
        "fixture_id": row["fixture_id"],
        "competition_id": row["competition_id"],
        "competition_name": row["competition_name"],
        "match_date": row["match_date"],
        "kickoff_at": row["kickoff_at"],
        "market_family": row["market_family"],
        "line": row["line"],
        "selection": row["selection"],
        "model_fair_decimal_price": row["model_fair_decimal_price"],
        "best_available_price": row["best_available_price"],
        "best_available_bookmaker": row["best_available_bookmaker"],
        "reference_bookmaker": row["reference_bookmaker"],
        "reference_source": row["reference_source"],
        "reference_booksum": row["reference_booksum"],
        "devigged_reference_probability": row["devigged_reference_probability"],
        "effective_win_stake_probability": row["effective_win_stake_probability"],
        "effective_loss_stake_probability": row["effective_loss_stake_probability"],
        "settlement_aware_edge": row["settlement_aware_edge"],
        "competition_threshold": row["competition_threshold"],
        "minimum_acceptable_price": row["minimum_acceptable_price"],
        "decision": row["decision"],
        "reason_codes": "|".join(row["reason_codes"]),
        "input_label": row["input_label"],
        "day11_manager_core_ready": row["day11_manager_core_ready"],
        "live_paper_pipeline_ready": row["live_paper_pipeline_ready"],
    }


def _sample_rows(decision_records: Sequence[dict[str, Any]], *, count: int = REPRESENTATIVE_ROW_COUNT) -> list[dict[str, Any]]:
    ordered = sorted(decision_records, key=_decision_sort_key)
    return ordered[:count]


def render_day11_manager_report(*, manifest: dict[str, Any], decision_records: Sequence[dict[str, Any]]) -> str:
    counts = manifest["decision_counts"]
    candidates = counts.get(DECISION_PRELIMINARY_CANDIDATE, 0)
    no_bets = counts.get(DECISION_NO_BET, 0)
    rejected = counts.get(DECISION_REJECTED_RULE, 0)
    missing = counts.get(DECISION_MISSING_REQUIRED_MARKET_DATA, 0)
    stale = counts.get(DECISION_STALE_MARKET, 0)
    representative = _sample_rows(decision_records)

    lines = [
        "# Football Day 11 Manager Report",
        "",
        f"**{_DEMO_INPUT_BANNER}**",
        "",
        f"**{_DEMO_NOT_LIVE_BANNER}**",
        "",
        f"**{_DEMO_PRELIMINARY_BANNER}**",
        "",
        "## Summary",
        "",
        f"- manager version: `{manifest['manager_version']}`",
        f"- final de-vig method: `{manifest['final_devig_method']}`",
        f"- threshold config version: `{manifest['threshold_config_version']}`",
        f"- decision records: `{manifest['decision_record_count']}`",
        f"- preliminary candidates: `{candidates}`",
        f"- no bets: `{no_bets}`",
        f"- rejected by rule: `{rejected}`",
        f"- missing required market data: `{missing}`",
        f"- stale markets: `{stale}`",
        f"- `DAY11_MANAGER_CORE_READY`: `{manifest['day11_manager_core_ready']}`",
        f"- `LIVE_PAPER_PIPELINE_READY`: `{manifest['live_paper_pipeline_ready']}`",
        "",
        "## Representative Decisions",
        "",
        "| Competition | Fixture | Market | Model | Best | Edge | Threshold | Decision | Reasons |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in representative:
        market_label = str(row["market_family"])
        if row["line"] is not None:
            market_label = f"{market_label} {row['line']} {row['selection']}"
        else:
            market_label = f"{market_label} {row['selection']}"
        fixture = f"{row['fixture_id']}"
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row["competition_id"]),
                    fixture,
                    market_label,
                    _fmt_price(row["model_fair_decimal_price"]),
                    _fmt_price(row["best_available_price"]),
                    _fmt_pct(row["settlement_aware_edge"]),
                    _fmt_pct(row["competition_threshold"]),
                    str(row["decision"]),
                    ", ".join(row["reason_codes"]),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- This report uses frozen Day 10 model prices plus supplied historical/demo market snapshots only.",
            "- No live provider polling, staking, Kelly sizing, or Day 12 veto logic is applied here.",
            "- The current historical/demo market input available in-repo is 1X2 only, so this manager run is a real full-slate 1X2 decision pass rather than a claim of full live-market coverage.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_day11_current_status(*, manifest: dict[str, Any]) -> str:
    counts = manifest["decision_counts"]
    return "\n".join(
        [
            "# Football Current Status",
            "",
            f"- manager version: `{MANAGER_VERSION}`",
            f"- `DAY11_MANAGER_CORE_READY`: `{manifest['day11_manager_core_ready']}`",
            f"- `LIVE_PAPER_PIPELINE_READY`: `{manifest['live_paper_pipeline_ready']}`",
            f"- final de-vig method: `{manifest['final_devig_method']}`",
            f"- decision records: `{manifest['decision_record_count']}`",
            f"- preliminary candidates: `{counts.get(DECISION_PRELIMINARY_CANDIDATE, 0)}`",
            f"- no bets: `{counts.get(DECISION_NO_BET, 0)}`",
            f"- rejected by rule: `{counts.get(DECISION_REJECTED_RULE, 0)}`",
            f"- missing required market data: `{counts.get(DECISION_MISSING_REQUIRED_MARKET_DATA, 0)}`",
            f"- stale markets: `{counts.get(DECISION_STALE_MARKET, 0)}`",
            "",
            f"- input label: `{_DEMO_INPUT_BANNER}`",
            f"- status: `{_DEMO_NOT_LIVE_BANNER}`",
        ]
    ) + "\n"


def write_day11_product_outputs(
    *,
    manifest: dict[str, Any],
    decision_records: Sequence[dict[str, Any]],
    output_dir: str | Path,
    latest_output_dir: str | Path,
) -> dict[str, str]:
    output_root = Path(output_dir)
    latest_root = Path(latest_output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    latest_root.mkdir(parents=True, exist_ok=True)

    report_path = output_root / "FOOTBALL_DAY11_MANAGER_REPORT.md"
    csv_path = output_root / "football_day11_decisions.csv"
    jsonl_path = output_root / "football_day11_decisions.jsonl"
    manifest_path = output_root / "football_day11_product_manifest.json"
    sample_csv_path = latest_root / "football_decisions_sample.csv"
    latest_report_path = latest_root / "FOOTBALL_DECISIONS.md"
    latest_csv_path = latest_root / "football_decisions.csv"
    latest_status_path = latest_root / "FOOTBALL_CURRENT_STATUS.md"

    flattened_rows = [_flatten_csv_row(row) for row in decision_records]
    fieldnames = list(flattened_rows[0].keys()) if flattened_rows else [
        "decision_id",
        "fixture_id",
        "competition_id",
        "competition_name",
        "match_date",
        "kickoff_at",
        "market_family",
        "line",
        "selection",
        "model_fair_decimal_price",
        "best_available_price",
        "best_available_bookmaker",
        "reference_bookmaker",
        "reference_source",
        "reference_booksum",
        "devigged_reference_probability",
        "effective_win_stake_probability",
        "effective_loss_stake_probability",
        "settlement_aware_edge",
        "competition_threshold",
        "minimum_acceptable_price",
        "decision",
        "reason_codes",
        "input_label",
        "day11_manager_core_ready",
        "live_paper_pipeline_ready",
    ]
    sample_rows = [_flatten_csv_row(row) for row in _sample_rows(decision_records)]
    report_path.write_text(
        render_day11_manager_report(manifest=manifest, decision_records=decision_records),
        encoding="utf-8",
    )
    _write_csv(csv_path, flattened_rows, fieldnames)
    _write_jsonl(jsonl_path, decision_records)
    _write_json(
        manifest_path,
        {
            "product_output_manifest_version": PRODUCT_OUTPUT_MANIFEST_VERSION,
            "product_output_version": DAY11_PRODUCT_OUTPUT_VERSION,
            "manager_version": manifest["manager_version"],
            "decision_record_count": manifest["decision_record_count"],
            "output_paths": {
                "report_path": str(report_path),
                "csv_path": str(csv_path),
                "jsonl_path": str(jsonl_path),
                "latest_report_path": str(latest_report_path),
                "latest_csv_path": str(latest_csv_path),
                "latest_sample_csv_path": str(sample_csv_path),
                "latest_status_path": str(latest_status_path),
            },
        },
    )

    _hardlink_or_copy(report_path, latest_report_path)
    _hardlink_or_copy(csv_path, latest_csv_path)
    _write_csv(sample_csv_path, sample_rows, fieldnames)
    latest_status_path.write_text(render_day11_current_status(manifest=manifest), encoding="utf-8")

    return {
        "report_path": str(report_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "manifest_path": str(manifest_path),
        "latest_report_path": str(latest_report_path),
        "latest_csv_path": str(latest_csv_path),
        "latest_sample_csv_path": str(sample_csv_path),
        "latest_status_path": str(latest_status_path),
    }
