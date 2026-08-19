"""Product-facing Day 10 football pricing outputs derived from authoritative model artifacts."""

from __future__ import annotations

import csv
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

PRODUCT_OUTPUT_REQUIREMENT_VERSION = "product_output_requirement_v1"
PRODUCT_OUTPUT_MANIFEST_VERSION = "football_day10_product_output_manifest_v1"
DAY10_PRODUCT_OUTPUT_VERSION = "football_day10_product_outputs_v1"
NOT_RECOMMENDATION_STATEMENT = (
    "These are model fair prices generated from frozen pre-match football model objects only. "
    "They are not bookmaker prices and they are not betting recommendations."
)
REPRESENTATIVE_FIXTURE_COUNT = 5


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _connect_store(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _load_fixture_lookup(research_db_path: str | Path) -> dict[str, dict[str, Any]]:
    query = """
        SELECT
            event_id,
            competition_id,
            competition_name,
            season_code,
            division_code,
            match_date,
            kickoff_time,
            home_team,
            away_team
        FROM football_events
    """
    with _connect_store(research_db_path) as conn:
        rows = conn.execute(query).fetchall()
    return {
        str(row["event_id"]): {
            "home_team": str(row["home_team"]),
            "away_team": str(row["away_team"]),
            "competition_name_store": str(row["competition_name"]),
            "match_date_store": str(row["match_date"]),
            "kickoff_time_store": None if row["kickoff_time"] is None else str(row["kickoff_time"]),
        }
        for row in rows
    }


def _pricing_sort_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row["season_code"]),
        str(row["match_date"]),
        str(row["kickoff_time"] or ""),
        str(row["event_id"]),
    )


def select_representative_pricing_inputs(
    pricing_inputs: Sequence[dict[str, Any]],
    *,
    count: int = REPRESENTATIVE_FIXTURE_COUNT,
) -> list[dict[str, Any]]:
    latest_by_competition: dict[str, dict[str, Any]] = {}
    for row in pricing_inputs:
        competition_id = str(row["competition_id"])
        current = latest_by_competition.get(competition_id)
        if current is None or _pricing_sort_key(row) > _pricing_sort_key(current):
            latest_by_competition[competition_id] = row
    selected = sorted(latest_by_competition.values(), key=_pricing_sort_key, reverse=True)
    return selected[:count]


def _top_scorelines(
    matrix: Sequence[Sequence[float]],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    cells = [
        {
            "home_goals": home_goals,
            "away_goals": away_goals,
            "probability": float(probability),
        }
        for home_goals, row in enumerate(matrix)
        for away_goals, probability in enumerate(row)
    ]
    return sorted(
        cells,
        key=lambda cell: (-cell["probability"], cell["home_goals"], cell["away_goals"]),
    )[:limit]


def _format_price(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:.2f}"


def _format_probability(value: float) -> str:
    return f"{value * 100.0:.1f}%"


def _market_rows_by_family(rows: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["market_family"]), []).append(row)
    return grouped


def _pick_nearest_lines(available_lines: Iterable[float], *, target: float, count: int = 3) -> list[float]:
    deduped = sorted({float(line) for line in available_lines})
    return sorted(
        deduped,
        key=lambda line: (abs(line - target), line),
    )[:count]


def _sorted_market_subset(
    rows: Sequence[dict[str, Any]],
    *,
    family: str,
    selection_order: Sequence[str] | None = None,
    lines: Sequence[float] | None = None,
    selection_filter: set[str] | None = None,
) -> list[dict[str, Any]]:
    filtered = [row for row in rows if row["market_family"] == family]
    if lines is not None:
        allowed = {float(line) for line in lines}
        filtered = [row for row in filtered if row["line"] in allowed]
    if selection_filter is not None:
        filtered = [row for row in filtered if str(row["selection"]) in selection_filter]

    selection_rank = {selection: index for index, selection in enumerate(selection_order or [])}
    return sorted(
        filtered,
        key=lambda row: (
            float("-inf") if row["line"] is None else float(row["line"]),
            selection_rank.get(str(row["selection"]), 999),
            str(row["selection"]),
        ),
    )


def _format_asian_handicap_label(*, selection: str, line: float) -> str:
    if selection == "HOME":
        return f"Home {line:+.2f}"
    return f"Away {-line:+.2f}"


def _representative_ah_rows(
    *,
    all_market_rows: Sequence[dict[str, Any]],
    asian_handicap_rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    one_x_two = {row["selection"]: row for row in all_market_rows if row["market_family"] == "1x2"}
    home_price = float(one_x_two["HOME"]["fair_decimal_price"])
    away_price = float(one_x_two["AWAY"]["fair_decimal_price"])
    stronger_selection = "HOME" if home_price <= away_price else "AWAY"
    if stronger_selection == "HOME":
        target_lines = [0.0, -0.25, -0.5]
        return _sorted_market_subset(
            asian_handicap_rows,
            family="asian_handicap",
            lines=target_lines,
            selection_filter={"HOME"},
        )
    target_lines = [0.0, 0.25, 0.5]
    return _sorted_market_subset(
        asian_handicap_rows,
        family="asian_handicap",
        lines=target_lines,
        selection_filter={"AWAY"},
    )


def _flatten_csv_row(
    market_row: dict[str, Any],
    *,
    pricing_input: dict[str, Any],
    fixture_meta: dict[str, Any] | None,
) -> dict[str, Any]:
    scoreline_object = pricing_input["scoreline_object"]
    corner_object = pricing_input["corner_object"]
    settlement = market_row["settlement_probabilities"]
    return {
        "fixture_id": market_row["fixture_id"],
        "event_id": market_row["event_id"],
        "home_team": None if fixture_meta is None else fixture_meta["home_team"],
        "away_team": None if fixture_meta is None else fixture_meta["away_team"],
        "competition_id": market_row["competition_id"],
        "competition_name": market_row["competition_name"],
        "season_code": market_row["season_code"],
        "division_code": market_row["division_code"],
        "match_date": market_row["match_date"],
        "kickoff_time": market_row["kickoff_time"],
        "kickoff_at": market_row["kickoff_at"],
        "as_of": market_row["as_of"],
        "market_family": market_row["market_family"],
        "line": market_row["line"],
        "selection": market_row["selection"],
        "fair_decimal_price": market_row["fair_decimal_price"],
        "full_win_probability": settlement["full_win_probability"],
        "half_win_probability": settlement["half_win_probability"],
        "push_probability": settlement["push_probability"],
        "half_loss_probability": settlement["half_loss_probability"],
        "full_loss_probability": settlement["full_loss_probability"],
        "expected_goals_home": scoreline_object["expected_goals_home"],
        "expected_goals_away": scoreline_object["expected_goals_away"],
        "expected_total_goals": scoreline_object["expected_total_goals"],
        "predicted_home_corners": corner_object["predicted_home_corners"],
        "predicted_away_corners": corner_object["predicted_away_corners"],
        "predicted_total_corners": corner_object["predicted_total_corners"],
        "scoreline_object_id": pricing_input["scoreline_object"]["object_id"],
        "corner_object_id": pricing_input["corner_object"]["object_id"],
        "pricing_model_version": market_row["pricing_model_version"],
        "settlement_engine_version": market_row["settlement_engine_version"],
        "line_catalog_version": market_row["line_catalog_version"],
    }


def _write_csv_rows(
    *,
    path: Path,
    fieldnames: Sequence[str],
    rows: Sequence[dict[str, Any]],
) -> None:
    with path.open("w", encoding="utf-8", newline="") as csv_handle:
        writer = csv.DictWriter(csv_handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def render_day10_pricing_report(
    *,
    manifest: dict[str, Any],
    pricing_inputs: Sequence[dict[str, Any]],
    representative_prices_by_event: dict[str, list[dict[str, Any]]],
    fixture_lookup: dict[str, dict[str, Any]],
) -> str:
    competitions = ", ".join(sorted({str(row["competition_name"]) for row in pricing_inputs}))
    representative_fixture_ids = [row["event_id"] for row in pricing_inputs]
    lines = [
        "# FOOTBALL MODEL FAIR-PRICE REPORT",
        "",
        f"Generated at: `{manifest['generated_at']}`",
        f"Pricing model version: `{manifest['pricing_model_version']}`",
        f"Settlement engine version: `{manifest['settlement_engine_version']}`",
        "",
        "## Summary",
        "",
        f"- fixtures represented: `{manifest['pricing_input_count']}`",
        f"- competitions: `{competitions}`",
        f"- model as-of policy: `{manifest['sanitized_input_policy_id']}`",
        f"- goals interface: `{manifest['day9_to_day10_interface_policy_id']}`",
        f"- corners interface: `{manifest['day8_to_day10_corner_interface_policy_id']}`",
        f"- fair-price records: `{manifest['market_price_count']}`",
        f"- statement: {NOT_RECOMMENDATION_STATEMENT}",
        "",
        "## Representative Fixtures",
        "",
        f"- representative fixture ids: `{representative_fixture_ids}`",
        "",
    ]

    for pricing_input in pricing_inputs:
        event_id = str(pricing_input["event_id"])
        fixture_meta = fixture_lookup.get(event_id, {})
        scoreline_object = pricing_input["scoreline_object"]
        corner_object = pricing_input["corner_object"]
        top_scores = _top_scorelines(scoreline_object["matrix"])
        market_rows = representative_prices_by_event[event_id]
        grouped = _market_rows_by_family(market_rows)

        one_x_two_rows = _sorted_market_subset(
            grouped.get("1x2", []),
            family="1x2",
            selection_order=("HOME", "DRAW", "AWAY"),
        )
        btts_rows = _sorted_market_subset(
            grouped.get("btts", []),
            family="btts",
            selection_order=("YES", "NO"),
        )
        goals_lines = _pick_nearest_lines(
            (row["line"] for row in grouped.get("goals_total", []) if row["line"] is not None),
            target=float(scoreline_object["expected_total_goals"]),
        )
        goal_total_rows = _sorted_market_subset(
            grouped.get("goals_total", []),
            family="goals_total",
            lines=goals_lines,
            selection_order=("OVER", "UNDER"),
        )
        ah_rows = _representative_ah_rows(
            all_market_rows=market_rows,
            asian_handicap_rows=grouped.get("asian_handicap", []),
        )
        corner_lines = _pick_nearest_lines(
            (row["line"] for row in grouped.get("corner_total", []) if row["line"] is not None),
            target=float(corner_object["predicted_total_corners"]),
        )
        corner_rows = _sorted_market_subset(
            grouped.get("corner_total", []),
            family="corner_total",
            lines=corner_lines,
            selection_order=("OVER", "UNDER"),
        )

        lines.extend(
            [
                f"## {fixture_meta.get('home_team', 'UNKNOWN')} vs {fixture_meta.get('away_team', 'UNKNOWN')}",
                "",
                "### Fixture identity",
                "",
                f"- home team: `{fixture_meta.get('home_team', 'UNKNOWN')}`",
                f"- away team: `{fixture_meta.get('away_team', 'UNKNOWN')}`",
                f"- competition: `{pricing_input['competition_name']}`",
                f"- kickoff: `{pricing_input['kickoff_at']}`",
                f"- model as_of: `{pricing_input['as_of']}`",
                "",
                "### Match model",
                "",
                f"- expected goals home: `{scoreline_object['expected_goals_home']:.2f}`",
                f"- expected goals away: `{scoreline_object['expected_goals_away']:.2f}`",
                f"- expected total goals: `{scoreline_object['expected_total_goals']:.2f}`",
                f"- expected corners home: `{corner_object['predicted_home_corners']:.2f}`",
                f"- expected corners away: `{corner_object['predicted_away_corners']:.2f}`",
                f"- expected total corners: `{corner_object['predicted_total_corners']:.2f}`",
                "",
                "### Most likely scores",
                "",
            ]
        )
        for score in top_scores:
            lines.append(
                f"- {score['home_goals']}-{score['away_goals']}: `{_format_probability(score['probability'])}`"
            )

        lines.extend(["", "### MODEL FAIR PRICE — 1X2", ""])
        for row in one_x_two_rows:
            lines.append(f"- {row['selection'].title()}: `{_format_price(row['fair_decimal_price'])}`")

        lines.extend(["", "### MODEL FAIR PRICE — BTTS", ""])
        for row in btts_rows:
            label = "Yes" if row["selection"] == "YES" else "No"
            lines.append(f"- {label}: `{_format_price(row['fair_decimal_price'])}`")

        lines.extend(["", "### MODEL FAIR PRICE — Goals Totals", ""])
        for row in goal_total_rows:
            lines.append(
                f"- {row['selection'].title()} {row['line']:.2f}: `{_format_price(row['fair_decimal_price'])}`"
            )

        lines.extend(["", "### MODEL FAIR PRICE — Asian Handicap", ""])
        for row in ah_rows:
            label = _format_asian_handicap_label(selection=str(row["selection"]), line=float(row["line"]))
            lines.append(f"- {label}: `{_format_price(row['fair_decimal_price'])}`")

        lines.extend(["", "### MODEL FAIR PRICE — Corner Totals", ""])
        for row in corner_rows:
            lines.append(
                f"- {row['selection'].title()} {row['line']:.2f}: `{_format_price(row['fair_decimal_price'])}`"
            )
        lines.append("")

    lines.extend(
        [
            "## Important note",
            "",
            f"- {NOT_RECOMMENDATION_STATEMENT}",
        ]
    )
    return "\n".join(lines)


def build_day10_product_outputs(
    *,
    research_db_path: str | Path,
    day10_manifest_path: str | Path,
    pricing_inputs_path: str | Path,
    market_prices_path: str | Path,
    output_root: str | Path,
) -> dict[str, Any]:
    manifest = _read_json(day10_manifest_path)
    pricing_inputs = _read_jsonl(pricing_inputs_path)
    fixture_lookup = _load_fixture_lookup(research_db_path)
    pricing_input_by_event = {
        str(row["event_id"]): row
        for row in pricing_inputs
    }
    representative_inputs = select_representative_pricing_inputs(pricing_inputs)
    representative_event_ids = {str(row["event_id"]) for row in representative_inputs}

    output_root_path = Path(output_root)
    day10_dir = output_root_path / "football" / "day10"
    latest_dir = output_root_path / "football" / "latest"
    day10_dir.mkdir(parents=True, exist_ok=True)
    latest_dir.mkdir(parents=True, exist_ok=True)

    csv_path = day10_dir / "football_day10_model_prices.csv"
    latest_csv_path = latest_dir / "football_model_prices.csv"
    jsonl_path = day10_dir / "football_day10_model_prices.jsonl"
    latest_jsonl_path = latest_dir / "football_model_prices.jsonl"
    sample_csv_path = day10_dir / "football_day10_model_prices_sample.csv"
    latest_sample_csv_path = latest_dir / "football_model_prices_sample.csv"
    fieldnames = [
        "fixture_id",
        "event_id",
        "home_team",
        "away_team",
        "competition_id",
        "competition_name",
        "season_code",
        "division_code",
        "match_date",
        "kickoff_time",
        "kickoff_at",
        "as_of",
        "market_family",
        "line",
        "selection",
        "fair_decimal_price",
        "full_win_probability",
        "half_win_probability",
        "push_probability",
        "half_loss_probability",
        "full_loss_probability",
        "expected_goals_home",
        "expected_goals_away",
        "expected_total_goals",
        "predicted_home_corners",
        "predicted_away_corners",
        "predicted_total_corners",
        "scoreline_object_id",
        "corner_object_id",
        "pricing_model_version",
        "settlement_engine_version",
        "line_catalog_version",
    ]

    representative_prices_by_event: dict[str, list[dict[str, Any]]] = {
        event_id: []
        for event_id in representative_event_ids
    }
    sample_rows: list[dict[str, Any]] = []
    with csv_path.open("w", encoding="utf-8", newline="") as csv_handle:
        writer = csv.DictWriter(csv_handle, fieldnames=fieldnames)
        writer.writeheader()
        for market_row in _read_jsonl(market_prices_path):
            event_id = str(market_row["event_id"])
            flattened = _flatten_csv_row(
                market_row,
                pricing_input=pricing_input_by_event[event_id],
                fixture_meta=fixture_lookup.get(event_id),
            )
            writer.writerow(flattened)
            if event_id in representative_prices_by_event:
                representative_prices_by_event[event_id].append(market_row)
                sample_rows.append(flattened)

    shutil.copyfile(csv_path, latest_csv_path)
    shutil.copyfile(Path(market_prices_path), jsonl_path)
    shutil.copyfile(jsonl_path, latest_jsonl_path)
    _write_csv_rows(path=sample_csv_path, fieldnames=fieldnames, rows=sample_rows)
    shutil.copyfile(sample_csv_path, latest_sample_csv_path)

    report_markdown = render_day10_pricing_report(
        manifest=manifest,
        pricing_inputs=representative_inputs,
        representative_prices_by_event=representative_prices_by_event,
        fixture_lookup=fixture_lookup,
    )
    report_path = day10_dir / "FOOTBALL_DAY10_MODEL_PRICING_REPORT.md"
    latest_report_path = latest_dir / "FOOTBALL_MODEL_PRICES.md"
    report_path.write_text(report_markdown, encoding="utf-8")
    latest_report_path.write_text(report_markdown, encoding="utf-8")

    product_manifest = {
        "generated_at": _utc_now(),
        "product_output_requirement_version": PRODUCT_OUTPUT_REQUIREMENT_VERSION,
        "product_output_manifest_version": PRODUCT_OUTPUT_MANIFEST_VERSION,
        "product_output_version": DAY10_PRODUCT_OUTPUT_VERSION,
        "source_research_db_path": str(Path(research_db_path)),
        "source_day10_manifest_path": str(Path(day10_manifest_path)),
        "source_pricing_inputs_path": str(Path(pricing_inputs_path)),
        "source_market_prices_path": str(Path(market_prices_path)),
        "source_day10_pricing_model_version": manifest["pricing_model_version"],
        "source_day10_settlement_engine_version": manifest["settlement_engine_version"],
        "source_day10_output_schema_version": manifest["output_schema_version"],
        "pricing_input_count": manifest["pricing_input_count"],
        "market_price_count": manifest["market_price_count"],
        "competitions": sorted({str(row["competition_name"]) for row in pricing_inputs}),
        "representative_fixture_ids": [str(row["event_id"]) for row in representative_inputs],
        "latest_outputs": {
            "markdown_path": str(latest_report_path),
            "csv_path": str(latest_csv_path),
            "sample_csv_path": str(latest_sample_csv_path),
            "jsonl_path": str(latest_jsonl_path),
            "manifest_path": str(latest_dir / "football_pipeline_manifest.json"),
        },
        "day10_outputs": {
            "markdown_path": str(report_path),
            "csv_path": str(csv_path),
            "sample_csv_path": str(sample_csv_path),
            "jsonl_path": str(jsonl_path),
            "manifest_path": str(day10_dir / "football_day10_product_manifest.json"),
        },
        "statement": NOT_RECOMMENDATION_STATEMENT,
    }
    manifest_path = day10_dir / "football_day10_product_manifest.json"
    manifest_path.write_text(json.dumps(product_manifest, indent=2), encoding="utf-8")
    latest_manifest_path = latest_dir / "football_pipeline_manifest.json"
    latest_manifest_path.write_text(json.dumps(product_manifest, indent=2), encoding="utf-8")

    return {
        "report_path": str(report_path),
        "csv_path": str(csv_path),
        "sample_csv_path": str(sample_csv_path),
        "jsonl_path": str(jsonl_path),
        "manifest_path": str(manifest_path),
        "latest_report_path": str(latest_report_path),
        "latest_csv_path": str(latest_csv_path),
        "latest_sample_csv_path": str(latest_sample_csv_path),
        "latest_jsonl_path": str(latest_jsonl_path),
        "latest_manifest_path": str(latest_manifest_path),
        "representative_fixture_ids": product_manifest["representative_fixture_ids"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate product-facing Day 10 football pricing outputs.")
    parser.add_argument("--research-db", required=True, help="Path to football research SQLite store.")
    parser.add_argument("--day10-manifest", required=True, help="Path to Day 10 manifest JSON.")
    parser.add_argument("--pricing-inputs", required=True, help="Path to Day 10 pricing inputs JSONL.")
    parser.add_argument("--market-prices", required=True, help="Path to Day 10 market prices JSONL.")
    parser.add_argument("--output-root", required=True, help="Root directory for product outputs.")
    args = parser.parse_args(argv)

    result = build_day10_product_outputs(
        research_db_path=args.research_db,
        day10_manifest_path=args.day10_manifest,
        pricing_inputs_path=args.pricing_inputs,
        market_prices_path=args.market_prices,
        output_root=args.output_root,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
