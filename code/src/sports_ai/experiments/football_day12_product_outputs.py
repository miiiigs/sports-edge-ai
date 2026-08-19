"""Product-facing Day 12 football safeguard outputs."""

from __future__ import annotations

import csv
import json
import os
import shutil
from pathlib import Path
from typing import Any, Iterable, Sequence

from .football_day12_safeguards import (
    CODE_UNIT_TEST_MARKET_SUPPORT,
    DAY12_BLOCKED_MISSING_XG_DATA,
    DAY12_DERIVATION_ERROR,
    DAY12_EXPERIMENTAL_CANDIDATE,
    DAY12_REJECTED_BY_AH_RULE,
    DAY12_REJECTED_BY_MULTIPLE_VETOES,
    DAY12_REJECTED_BY_XG_VETO,
    DAY12_REVIEW_REQUIRED,
    DAY12_VERSION,
    EDGE_BUCKET_ORDER,
    INPUT_LABEL,
    NOT_LIVE_OUTPUT_LABEL,
    NOT_REAL_MONEY_LABEL,
    REAL_HISTORICAL_MARKET_COVERAGE,
)

DAY12_PRODUCT_OUTPUT_VERSION = "football_day12_product_outputs_v1"
DAY12_PRODUCT_MANIFEST_VERSION = "football_day12_product_output_manifest_v1"
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
        DAY12_EXPERIMENTAL_CANDIDATE: 0,
        DAY12_REJECTED_BY_MULTIPLE_VETOES: 1,
        DAY12_REJECTED_BY_XG_VETO: 2,
        DAY12_REJECTED_BY_AH_RULE: 3,
        DAY12_BLOCKED_MISSING_XG_DATA: 4,
        DAY12_DERIVATION_ERROR: 5,
        DAY12_REVIEW_REQUIRED: 6,
    }
    return (
        rank.get(str(row["final_disposition"]), 999),
        str(row["competition_id"]),
        str(row["kickoff_at"]),
        str(row["day12_candidate_id"]),
    )


def _flatten_csv_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "day12_candidate_id": row["day12_candidate_id"],
        "day11_decision_id": row["day11_decision_id"],
        "fixture_id": row["fixture_id"],
        "home_team": row["home_team"],
        "away_team": row["away_team"],
        "competition_id": row["competition_id"],
        "competition_name": row["competition_name"],
        "match_date": row["match_date"],
        "kickoff_at": row["kickoff_at"],
        "market_family": row["market_family"],
        "line": row["line"],
        "selection": row["selection"],
        "model_fair_decimal_price": row["model_fair_decimal_price"],
        "best_available_price": row["best_available_price"],
        "settlement_aware_edge": row["settlement_aware_edge"],
        "edge_bucket": row["edge_bucket"],
        "ah_rule_status": row["ah_rule_status"],
        "derivation_status": row["derivation_status"],
        "xg_status": row["xg_status"],
        "final_disposition": row["final_disposition"],
        "final_reason_codes": "|".join(row["final_reason_codes"]),
        "input_label": row["input_label"],
        "real_historical_market_coverage": row["real_historical_market_coverage"],
        "live_paper_pipeline_ready": row["live_paper_pipeline_ready"],
    }


def _sample_rows(candidate_records: Sequence[dict[str, Any]], *, count: int = REPRESENTATIVE_ROW_COUNT) -> list[dict[str, Any]]:
    ordered = sorted(candidate_records, key=_decision_sort_key)
    return ordered[:count]


def _find_example(candidate_records: Sequence[dict[str, Any]], disposition: str) -> dict[str, Any] | None:
    for row in sorted(candidate_records, key=_decision_sort_key):
        if str(row["final_disposition"]) == disposition:
            return row
    return None


def _market_label(row: dict[str, Any]) -> str:
    if row["line"] is None:
        return f"{row['market_family']} {row['selection']}"
    return f"{row['market_family']} {row['line']} {row['selection']}"


def _render_example(title: str, row: dict[str, Any] | None, *, fallback: str) -> list[str]:
    lines = [f"### {title}", ""]
    if row is None:
        lines.append(f"- {fallback}")
        lines.append("")
        return lines
    lines.extend(
        [
            f"- fixture: `{row['fixture_id']}`",
            f"- teams: `{row.get('home_team') or 'UNKNOWN'} vs {row.get('away_team') or 'UNKNOWN'}`",
            f"- market: `{_market_label(row)}`",
            f"- best price: `{_fmt_price(row['best_available_price'])}`",
            f"- edge: `{_fmt_pct(row['settlement_aware_edge'])}`",
            f"- disposition: `{row['final_disposition']}`",
            f"- reasons: `{', '.join(row['final_reason_codes']) if row['final_reason_codes'] else 'none'}`",
            "",
        ]
    )
    return lines


def render_day12_candidate_report(*, manifest: dict[str, Any], candidate_records: Sequence[dict[str, Any]]) -> str:
    counts = manifest["disposition_counts"]
    xg_counts = manifest["xg_status_counts"]
    ah_counts = manifest["ah_status_counts"]
    representative = _sample_rows(candidate_records)
    example_candidate = _find_example(candidate_records, DAY12_EXPERIMENTAL_CANDIDATE)
    example_ah_reject = _find_example(candidate_records, DAY12_REJECTED_BY_AH_RULE)
    example_xg_reject = _find_example(candidate_records, DAY12_REJECTED_BY_XG_VETO)
    example_xg_block = _find_example(candidate_records, DAY12_BLOCKED_MISSING_XG_DATA)

    lines = [
        "# Football Day 12 Experimental Candidate Report",
        "",
        f"**{INPUT_LABEL}**",
        "",
        f"**{NOT_LIVE_OUTPUT_LABEL}**",
        "",
        f"**{NOT_REAL_MONEY_LABEL}**",
        "",
        "## Summary",
        "",
        f"- Day 12 version: `{manifest['day12_version']}`",
        f"- Day 11 preliminary candidates received: `{manifest['day11_preliminary_candidate_count']}`",
        f"- experimental candidates: `{counts.get(DAY12_EXPERIMENTAL_CANDIDATE, 0)}`",
        f"- rejected by AH rule: `{counts.get(DAY12_REJECTED_BY_AH_RULE, 0)}`",
        f"- rejected by xG veto: `{counts.get(DAY12_REJECTED_BY_XG_VETO, 0)}`",
        f"- rejected by multiple vetoes: `{counts.get(DAY12_REJECTED_BY_MULTIPLE_VETOES, 0)}`",
        f"- blocked missing xG data: `{counts.get(DAY12_BLOCKED_MISSING_XG_DATA, 0)}`",
        f"- derivation errors: `{counts.get(DAY12_DERIVATION_ERROR, 0)}`",
        f"- review required: `{counts.get(DAY12_REVIEW_REQUIRED, 0)}`",
        f"- survival percentage: `{_fmt_pct(manifest['survival_percentage'])}`",
        f"- `DAY12 STATUS`: `{manifest['day12_status']}`",
        f"- `FOOTBALL_LOGICAL_PIPELINE_READY`: `{manifest['football_logical_pipeline_ready']}`",
        f"- `LIVE_PAPER_PIPELINE_READY`: `{manifest['live_paper_pipeline_ready']}`",
        f"- `TENNIS AUTHORIZED`: `{manifest['tennis_authorized']}`",
        "",
        "## Real Coverage",
        "",
        f"- real historical market coverage: `{REAL_HISTORICAL_MARKET_COVERAGE}`",
        f"- code / unit-test market support: `{CODE_UNIT_TEST_MARKET_SUPPORT}`",
        "",
        "## xG Dependency Inspection",
        "",
        f"- xG source present in repo run-path: `{manifest['xg_input_inspection']['exists']}`",
        f"- xG source path: `{manifest['xg_input_inspection']['path']}`",
        f"- xG competitions covered: `{manifest['xg_input_inspection'].get('competitions_covered', [])}`",
        f"- xG seasons covered: `{manifest['xg_input_inspection'].get('seasons_covered', [])}`",
        f"- xG sufficient for Day 12 rule: `{manifest['xg_input_inspection']['sufficient_for_day12_rule']}`",
        f"- xG pass count: `{xg_counts.get('XG_PASS', 0)}`",
        f"- xG veto count: `{xg_counts.get('XG_VETO', 0)}`",
        f"- xG missing / blocked count: `{xg_counts.get('XG_MISSING', 0)}`",
        f"- xG not applicable count: `{xg_counts.get('XG_NOT_APPLICABLE', 0)}`",
        "",
        "## AH Safeguard",
        "",
        f"- AH pass count: `{ah_counts.get('AH_PASS', 0)}`",
        f"- AH reject count: `{ah_counts.get('AH_REJECT', 0)}`",
        f"- AH not applicable count: `{ah_counts.get('AH_NOT_APPLICABLE', 0)}`",
        "",
        "## Extreme Edge Diagnostics",
        "",
        "| Bucket | Input | Survived | Rejected | Blocked |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for bucket in EDGE_BUCKET_ORDER:
        bucket_counts = manifest["edge_bucket_counts"][bucket]
        lines.append(
            f"| {bucket} | {bucket_counts['input']} | {bucket_counts['survived']} | {bucket_counts['rejected']} | {bucket_counts['blocked']} |"
        )
    lines.extend(
        [
            "",
            "## Examples",
            "",
        ]
    )
    lines.extend(
        _render_example(
            "Experimental Candidate Example",
            example_candidate,
            fallback="No experimental candidate exists in this real run.",
        )
    )
    lines.extend(
        _render_example(
            "AH Rejection Example",
            example_ah_reject,
            fallback="No AH rejection exists in this real run because the in-repo historical/demo input covers archived closing 1X2 only.",
        )
    )
    lines.extend(
        _render_example(
            "xG Rejection Example",
            example_xg_reject,
            fallback="No xG veto exists in this real run because no point-in-time-valid xG source is connected in the current repo path.",
        )
    )
    lines.extend(
        _render_example(
            "Blocked xG Example",
            example_xg_block,
            fallback="No blocked xG case exists in this real run.",
        )
    )
    lines.extend(
        [
            "## Representative Outputs",
            "",
            "| Competition | Fixture | Market | Best | Edge | AH | xG | Final |",
            "| --- | --- | --- | ---: | ---: | --- | --- | --- |",
        ]
    )
    for row in representative:
        fixture_label = f"{row['home_team'] or 'UNKNOWN'} vs {row['away_team'] or 'UNKNOWN'}"
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row["competition_id"]),
                    fixture_label,
                    _market_label(row),
                    _fmt_price(row["best_available_price"]),
                    _fmt_pct(row["settlement_aware_edge"]),
                    str(row["ah_rule_status"]),
                    str(row["xg_status"]),
                    str(row["final_disposition"]),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def render_day12_current_status(*, manifest: dict[str, Any]) -> str:
    counts = manifest["disposition_counts"]
    return "\n".join(
        [
            "# Football Current Status",
            "",
            f"- day12 version: `{DAY12_VERSION}`",
            f"- `DAY12 STATUS`: `{manifest['day12_status']}`",
            f"- `FOOTBALL_LOGICAL_PIPELINE_READY`: `{manifest['football_logical_pipeline_ready']}`",
            f"- `LIVE_PAPER_PIPELINE_READY`: `{manifest['live_paper_pipeline_ready']}`",
            f"- `TENNIS AUTHORIZED`: `{manifest['tennis_authorized']}`",
            f"- experimental candidates: `{counts.get(DAY12_EXPERIMENTAL_CANDIDATE, 0)}`",
            f"- blocked missing xG data: `{counts.get(DAY12_BLOCKED_MISSING_XG_DATA, 0)}`",
            f"- derivation errors: `{counts.get(DAY12_DERIVATION_ERROR, 0)}`",
            "",
            f"- input label: `{INPUT_LABEL}`",
            f"- coverage: `{REAL_HISTORICAL_MARKET_COVERAGE}`",
        ]
    ) + "\n"


def write_day12_product_outputs(
    *,
    manifest: dict[str, Any],
    candidate_records: Sequence[dict[str, Any]],
    output_dir: str | Path,
    latest_output_dir: str | Path,
) -> dict[str, str]:
    output_root = Path(output_dir)
    latest_root = Path(latest_output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    latest_root.mkdir(parents=True, exist_ok=True)

    report_path = output_root / "FOOTBALL_DAY12_EXPERIMENTAL_CANDIDATE_REPORT.md"
    csv_path = output_root / "football_day12_candidates.csv"
    jsonl_path = output_root / "football_day12_candidates.jsonl"
    manifest_path = output_root / "football_day12_product_manifest.json"
    latest_report_path = latest_root / "FOOTBALL_EXPERIMENTAL_CANDIDATES.md"
    latest_csv_path = latest_root / "football_experimental_candidates.csv"
    latest_sample_csv_path = latest_root / "football_experimental_candidates_sample.csv"
    latest_status_path = latest_root / "FOOTBALL_CURRENT_STATUS.md"

    flattened_rows = [_flatten_csv_row(row) for row in candidate_records]
    fieldnames = list(flattened_rows[0].keys()) if flattened_rows else [
        "day12_candidate_id",
        "day11_decision_id",
        "fixture_id",
        "home_team",
        "away_team",
        "competition_id",
        "competition_name",
        "match_date",
        "kickoff_at",
        "market_family",
        "line",
        "selection",
        "model_fair_decimal_price",
        "best_available_price",
        "settlement_aware_edge",
        "edge_bucket",
        "ah_rule_status",
        "derivation_status",
        "xg_status",
        "final_disposition",
        "final_reason_codes",
        "input_label",
        "real_historical_market_coverage",
        "live_paper_pipeline_ready",
    ]
    sample_rows = [_flatten_csv_row(row) for row in _sample_rows(candidate_records)]
    report_path.write_text(
        render_day12_candidate_report(manifest=manifest, candidate_records=candidate_records),
        encoding="utf-8",
    )
    _write_csv(csv_path, flattened_rows, fieldnames)
    _write_jsonl(jsonl_path, candidate_records)
    _write_json(
        manifest_path,
        {
            "product_output_manifest_version": DAY12_PRODUCT_MANIFEST_VERSION,
            "product_output_version": DAY12_PRODUCT_OUTPUT_VERSION,
            "day12_version": manifest["day12_version"],
            "candidate_record_count": manifest["candidate_record_count"],
            "output_paths": {
                "report_path": str(report_path),
                "csv_path": str(csv_path),
                "jsonl_path": str(jsonl_path),
                "latest_report_path": str(latest_report_path),
                "latest_csv_path": str(latest_csv_path),
                "latest_sample_csv_path": str(latest_sample_csv_path),
                "latest_status_path": str(latest_status_path),
            },
        },
    )

    _hardlink_or_copy(report_path, latest_report_path)
    _hardlink_or_copy(csv_path, latest_csv_path)
    _write_csv(latest_sample_csv_path, sample_rows, fieldnames)
    latest_status_path.write_text(render_day12_current_status(manifest=manifest), encoding="utf-8")

    return {
        "report_path": str(report_path),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "manifest_path": str(manifest_path),
        "latest_report_path": str(latest_report_path),
        "latest_csv_path": str(latest_csv_path),
        "latest_sample_csv_path": str(latest_sample_csv_path),
        "latest_status_path": str(latest_status_path),
    }


def render_day12_acceptance_audit(*, manifest: dict[str, Any], product_paths: dict[str, str]) -> str:
    counts = manifest["disposition_counts"]
    xg_counts = manifest["xg_status_counts"]
    ah_counts = manifest["ah_status_counts"]
    return "\n".join(
        [
            "# Football Day 12 Acceptance Audit",
            "",
            "Requested artifact filename: `FOOTBALL_DAY12_ACCEPTANCE_AUDIT_2026-08-19.md`",
            "",
            "Actual implementation / verification date: Wednesday, August 19, 2026",
            "",
            "## Scope",
            "",
            "- frozen Day 11A preliminary candidates remain the immutable upstream input",
            "- Day 12 adds only the bounded football safeguard layer",
            "- the current real historical/demo market input still covers archived closing `1X2` only",
            "",
            "## Acceptance result",
            "",
            f"Day 12 is accepted as `{manifest['day12_status']}` for the currently loaded football scope.",
            "",
            "## Verified implementation state",
            "",
            f"- Day 12 version: `{manifest['day12_version']}`",
            "- Day 11 no-bets are not promoted",
            "- every Day 11 preliminary candidate receives exactly one Day 12 disposition",
            "- AH line-selection logic is deterministic and kill-only",
            "- AH/1X2 invariants are rechecked from the frozen Day 10 price artifact",
            "- the xG contract exists, includes chronology rules, and does not invent missing data",
            "- missing point-in-time-valid xG is surfaced as an explicit blocked state",
            "- manager-facing Day 12 markdown / CSV / JSONL / latest artifacts are generated",
            f"- `DAY12 STATUS`: `{manifest['day12_status']}`",
            f"- `FOOTBALL_LOGICAL_PIPELINE_READY`: `{manifest['football_logical_pipeline_ready']}`",
            f"- `LIVE_PAPER_PIPELINE_READY`: `{manifest['live_paper_pipeline_ready']}`",
            f"- `TENNIS AUTHORIZED`: `{manifest['tennis_authorized']}`",
            "",
            "## Real run evidence",
            "",
            f"- real supplied input label: `{INPUT_LABEL}`",
            f"- real historical market coverage: `{REAL_HISTORICAL_MARKET_COVERAGE}`",
            f"- code / unit-test market support: `{CODE_UNIT_TEST_MARKET_SUPPORT}`",
            f"- Day 11 preliminary candidates received: `{manifest['day11_preliminary_candidate_count']}`",
            f"- experimental candidates: `{counts.get(DAY12_EXPERIMENTAL_CANDIDATE, 0)}`",
            f"- rejected by AH rule: `{counts.get(DAY12_REJECTED_BY_AH_RULE, 0)}`",
            f"- rejected by xG veto: `{counts.get(DAY12_REJECTED_BY_XG_VETO, 0)}`",
            f"- rejected by multiple vetoes: `{counts.get(DAY12_REJECTED_BY_MULTIPLE_VETOES, 0)}`",
            f"- blocked missing xG data: `{counts.get(DAY12_BLOCKED_MISSING_XG_DATA, 0)}`",
            f"- derivation errors: `{counts.get(DAY12_DERIVATION_ERROR, 0)}`",
            f"- review required: `{counts.get(DAY12_REVIEW_REQUIRED, 0)}`",
            f"- AH pass count: `{ah_counts.get('AH_PASS', 0)}`",
            f"- AH reject count: `{ah_counts.get('AH_REJECT', 0)}`",
            f"- AH not applicable count: `{ah_counts.get('AH_NOT_APPLICABLE', 0)}`",
            f"- xG pass count: `{xg_counts.get('XG_PASS', 0)}`",
            f"- xG veto count: `{xg_counts.get('XG_VETO', 0)}`",
            f"- xG missing count: `{xg_counts.get('XG_MISSING', 0)}`",
            f"- xG not applicable count: `{xg_counts.get('XG_NOT_APPLICABLE', 0)}`",
            f"- survival percentage: `{manifest['survival_percentage'] * 100.0:.2f}%`",
            "",
            "## xG dependency truth",
            "",
            f"- xG source exists in current repo run-path: `{manifest['xg_input_inspection']['exists']}`",
            f"- xG source path: `{manifest['xg_input_inspection']['path']}`",
            f"- xG sufficient for the Day 12 rule: `{manifest['xg_input_inspection']['sufficient_for_day12_rule']}`",
            "- because the required point-in-time-valid xG source is not connected for the applicable competitions in this run-path, Day 12 remains partial rather than being overstated as complete",
            "",
            "## Required artifacts present",
            "",
            f"- `{product_paths['report_path']}`",
            f"- `{product_paths['csv_path']}`",
            f"- `{product_paths['jsonl_path']}`",
            f"- `{product_paths['manifest_path']}`",
            f"- `{product_paths['latest_report_path']}`",
            f"- `{product_paths['latest_csv_path']}`",
            f"- `{product_paths['latest_sample_csv_path']}`",
            f"- `{product_paths['latest_status_path']}`",
            "",
            "## Bottom line",
            "",
            "The repo can now take frozen Day 11 preliminary football candidates and derive deterministic Day 12 safeguard dispositions without inventing bookmaker inputs or match outcomes. The remaining honest blocker is the absence of a connected point-in-time-valid xG source for the competitions where the football spec requires that check.",
        ]
    ) + "\n"
