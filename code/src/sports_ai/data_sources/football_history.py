"""Historical football intake helpers.

This module converts a football-data.co.uk CSV into a clean historical data
bundle with:

1. sports/event rows
2. separate market/odds rows
3. a simple coverage summary

The goal is not to create final modeling payloads yet. The goal is to create a
reliable intake spine that preserves the firewall boundary from the start.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import httpx

from ..firewall import scan_for_market_contamination
from .football_data_co_uk import build_csv_url, profile_csv_text

DIVISION_TO_COMPETITION = {
    "E0": ("ENG1", "Premier League"),
    "E1": ("ENG2", "Championship"),
    "E2": ("ENG3", "League One"),
    "E3": ("ENG4", "League Two"),
    "SP1": ("ESP1", "La Liga"),
    "SP2": ("ESP2", "Segunda Division"),
    "SC0": ("SCO1", "Scottish Premiership"),
    "SC1": ("SCO2", "Scottish Championship"),
    "SC2": ("SCO3", "Scottish League One"),
    "SC3": ("SCO4", "Scottish League Two"),
}

SPORTS_FIELDS = (
    "Date",
    "Time",
    "HomeTeam",
    "AwayTeam",
    "FTHG",
    "FTAG",
    "FTR",
    "HTHG",
    "HTAG",
    "HTR",
    "Referee",
    "HS",
    "AS",
    "HST",
    "AST",
    "HF",
    "AF",
    "HC",
    "AC",
    "HY",
    "AY",
    "HR",
    "AR",
)

PRIMARY_CLOSING_1X2_FIELDS = (
    "B365CH",
    "B365CD",
    "B365CA",
    "PSCH",
    "PSCD",
    "PSCA",
    "MaxCH",
    "MaxCD",
    "MaxCA",
    "AvgCH",
    "AvgCD",
    "AvgCA",
)

KEY_BLIND_COVERAGE_FIELDS = (
    "Referee",
    "HS",
    "HST",
    "HC",
    "HY",
)


@dataclass(frozen=True)
class HistoricalEventRow:
    event_id: str
    competition_id: str
    competition_name: str
    season_code: str
    division_code: str
    match_date: str
    kickoff_time: str | None
    home_team: str
    away_team: str
    home_goals: int | None
    away_goals: int | None
    result_code: str | None
    half_time_home_goals: int | None
    half_time_away_goals: int | None
    half_time_result_code: str | None
    referee: str | None
    home_shots: int | None
    away_shots: int | None
    home_shots_on_target: int | None
    away_shots_on_target: int | None
    home_fouls: int | None
    away_fouls: int | None
    home_corners: int | None
    away_corners: int | None
    home_yellow_cards: int | None
    away_yellow_cards: int | None
    home_red_cards: int | None
    away_red_cards: int | None


@dataclass(frozen=True)
class HistoricalMarketRow:
    event_id: str
    competition_id: str
    season_code: str
    division_code: str
    source: str
    market_type: str
    bookmaker: str
    selection_prices: dict[str, float | None]


@dataclass(frozen=True)
class HistoricalCoverageSummary:
    event_count: int
    market_row_count: int
    first_match_date: str | None
    last_match_date: str | None
    missing_profile_fields: tuple[str, ...]
    key_blind_field_coverage: dict[str, float]
    available_closing_columns: tuple[str, ...]


@dataclass(frozen=True)
class HistoricalIngestBundle:
    source_url: str
    season_code: str
    division_code: str
    competition_id: str
    competition_name: str
    sports_rows: tuple[HistoricalEventRow, ...]
    market_rows: tuple[HistoricalMarketRow, ...]
    coverage_summary: HistoricalCoverageSummary

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def _to_int(value: str | None) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _to_float(value: str | None) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _build_event_id(match_date: str, home_team: str, away_team: str) -> str:
    compact_date = match_date.replace("-", "")
    return f"evt_football_{compact_date}_{_slug(home_team)}_{_slug(away_team)}"


def _normalize_date(raw_date: str) -> str:
    parts = raw_date.strip().split("/")
    if len(parts) != 3:
        return raw_date.strip()
    day, month, year = parts
    return f"{year}-{month.zfill(2)}-{day.zfill(2)}"


def _competition_identity(division_code: str) -> tuple[str, str]:
    return DIVISION_TO_COMPETITION.get(division_code, (division_code, division_code))


def _extract_market_rows(
    *,
    event_id: str,
    competition_id: str,
    season_code: str,
    division_code: str,
    row: dict[str, str],
) -> list[HistoricalMarketRow]:
    market_rows: list[HistoricalMarketRow] = []

    bookmaker_sets = (
        ("bet365_closing_1x2", "1X2", "Bet365", ("B365CH", "B365CD", "B365CA")),
        ("pinnacle_closing_1x2", "1X2", "Pinnacle", ("PSCH", "PSCD", "PSCA")),
        ("max_closing_1x2", "1X2", "MarketMax", ("MaxCH", "MaxCD", "MaxCA")),
        ("avg_closing_1x2", "1X2", "MarketAvg", ("AvgCH", "AvgCD", "AvgCA")),
    )

    for source, market_type, bookmaker, columns in bookmaker_sets:
        prices = {
            "home": _to_float(row.get(columns[0])),
            "draw": _to_float(row.get(columns[1])),
            "away": _to_float(row.get(columns[2])),
        }
        if any(value is not None for value in prices.values()):
            market_rows.append(
                HistoricalMarketRow(
                    event_id=event_id,
                    competition_id=competition_id,
                    season_code=season_code,
                    division_code=division_code,
                    source=source,
                    market_type=market_type,
                    bookmaker=bookmaker,
                    selection_prices=prices,
                )
            )

    return market_rows


def ingest_historical_csv_text(
    csv_text: str,
    *,
    season_code: str,
    division_code: str,
    source_url: str,
) -> HistoricalIngestBundle:
    """Parse a football-data.co.uk CSV into separated historical rows."""

    normalized_text = csv_text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(normalized_text))
    rows = list(reader)
    competition_id, competition_name = _competition_identity(division_code)
    sports_rows: list[HistoricalEventRow] = []
    market_rows: list[HistoricalMarketRow] = []

    coverage_counts = {field: 0 for field in KEY_BLIND_COVERAGE_FIELDS}

    for raw_row in rows:
        match_date = _normalize_date(raw_row.get("Date", ""))
        home_team = (raw_row.get("HomeTeam") or "").strip()
        away_team = (raw_row.get("AwayTeam") or "").strip()
        event_id = _build_event_id(match_date, home_team, away_team)

        sports_row = HistoricalEventRow(
            event_id=event_id,
            competition_id=competition_id,
            competition_name=competition_name,
            season_code=season_code,
            division_code=division_code,
            match_date=match_date,
            kickoff_time=(raw_row.get("Time") or "").strip() or None,
            home_team=home_team,
            away_team=away_team,
            home_goals=_to_int(raw_row.get("FTHG")),
            away_goals=_to_int(raw_row.get("FTAG")),
            result_code=(raw_row.get("FTR") or "").strip() or None,
            half_time_home_goals=_to_int(raw_row.get("HTHG")),
            half_time_away_goals=_to_int(raw_row.get("HTAG")),
            half_time_result_code=(raw_row.get("HTR") or "").strip() or None,
            referee=(raw_row.get("Referee") or "").strip() or None,
            home_shots=_to_int(raw_row.get("HS")),
            away_shots=_to_int(raw_row.get("AS")),
            home_shots_on_target=_to_int(raw_row.get("HST")),
            away_shots_on_target=_to_int(raw_row.get("AST")),
            home_fouls=_to_int(raw_row.get("HF")),
            away_fouls=_to_int(raw_row.get("AF")),
            home_corners=_to_int(raw_row.get("HC")),
            away_corners=_to_int(raw_row.get("AC")),
            home_yellow_cards=_to_int(raw_row.get("HY")),
            away_yellow_cards=_to_int(raw_row.get("AY")),
            home_red_cards=_to_int(raw_row.get("HR")),
            away_red_cards=_to_int(raw_row.get("AR")),
        )
        # Guardrail: the sports row must remain safe to store in the blind/sports domain.
        scan_for_market_contamination(asdict(sports_row))
        sports_rows.append(sports_row)

        for field in KEY_BLIND_COVERAGE_FIELDS:
            if (raw_row.get(field) or "").strip():
                coverage_counts[field] += 1

        market_rows.extend(
            _extract_market_rows(
                event_id=event_id,
                competition_id=competition_id,
                season_code=season_code,
                division_code=division_code,
                row=raw_row,
            )
        )

    profile = profile_csv_text(
        normalized_text,
        season_code=season_code,
        division_code=division_code,
        source_url=source_url,
        required_fields=SPORTS_FIELDS + PRIMARY_CLOSING_1X2_FIELDS,
    )

    denominator = len(rows) or 1
    coverage_summary = HistoricalCoverageSummary(
        event_count=len(sports_rows),
        market_row_count=len(market_rows),
        first_match_date=profile.first_match_date,
        last_match_date=profile.last_match_date,
        missing_profile_fields=profile.missing_required_fields,
        key_blind_field_coverage={
            field: round(coverage_counts[field] / denominator, 4) for field in KEY_BLIND_COVERAGE_FIELDS
        },
        available_closing_columns=tuple(
            column for column in PRIMARY_CLOSING_1X2_FIELDS if column in profile.column_names
        ),
    )

    return HistoricalIngestBundle(
        source_url=source_url,
        season_code=season_code,
        division_code=division_code,
        competition_id=competition_id,
        competition_name=competition_name,
        sports_rows=tuple(sports_rows),
        market_rows=tuple(market_rows),
        coverage_summary=coverage_summary,
    )


def fetch_historical_bundle(
    season_code: str,
    division_code: str,
    *,
    timeout: float = 20.0,
    client: httpx.Client | None = None,
) -> HistoricalIngestBundle:
    """Download and ingest one historical football CSV."""

    source_url = build_csv_url(season_code, division_code)
    close_client = False
    if client is None:
        client = httpx.Client(follow_redirects=True, timeout=timeout)
        close_client = True

    try:
        response = client.get(source_url)
        response.raise_for_status()
    finally:
        if close_client:
            client.close()

    return ingest_historical_csv_text(
        response.text,
        season_code=season_code,
        division_code=division_code,
        source_url=source_url,
    )


def bundle_preview(bundle: HistoricalIngestBundle, limit: int = 3) -> dict[str, Any]:
    """Return a compact preview for CLI/debug use."""

    sports_preview = [asdict(row) for row in bundle.sports_rows[:limit]]
    market_preview = [asdict(row) for row in bundle.market_rows[:limit]]
    return {
        "source_url": bundle.source_url,
        "season_code": bundle.season_code,
        "division_code": bundle.division_code,
        "competition_id": bundle.competition_id,
        "competition_name": bundle.competition_name,
        "coverage_summary": asdict(bundle.coverage_summary),
        "sports_preview": sports_preview,
        "market_preview": market_preview,
    }


def export_bundle(bundle: HistoricalIngestBundle, output_dir: str | Path) -> dict[str, str]:
    """Write the bundle to a local directory for research reuse."""

    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)

    sports_path = target / "sports_rows.jsonl"
    market_path = target / "market_rows.jsonl"
    summary_path = target / "coverage_summary.json"
    manifest_path = target / "bundle_manifest.json"

    sports_path.write_text(
        "\n".join(json.dumps(asdict(row), sort_keys=True) for row in bundle.sports_rows) + "\n",
        encoding="utf-8",
    )
    market_path.write_text(
        "\n".join(json.dumps(asdict(row), sort_keys=True) for row in bundle.market_rows) + "\n",
        encoding="utf-8",
    )
    summary_path.write_text(json.dumps(asdict(bundle.coverage_summary), indent=2), encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            {
                "source_url": bundle.source_url,
                "season_code": bundle.season_code,
                "division_code": bundle.division_code,
                "competition_id": bundle.competition_id,
                "competition_name": bundle.competition_name,
                "event_count": bundle.coverage_summary.event_count,
                "market_row_count": bundle.coverage_summary.market_row_count,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    return {
        "output_dir": str(target),
        "sports_rows": str(sports_path),
        "market_rows": str(market_path),
        "coverage_summary": str(summary_path),
        "bundle_manifest": str(manifest_path),
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CLI for the first football historical intake slice."""

    import argparse

    parser = argparse.ArgumentParser(description="Build a first historical football intake bundle.")
    parser.add_argument("--season", required=True, help="Season code like 2526 or 2627.")
    parser.add_argument("--division", required=True, help="Division code like E0 or SC0.")
    parser.add_argument("--preview-limit", type=int, default=2, help="Number of preview rows to print.")
    parser.add_argument("--output-dir", help="Optional directory to write JSONL/JSON bundle files.")
    args = parser.parse_args(argv)

    bundle = fetch_historical_bundle(args.season, args.division)
    payload = {"preview": bundle_preview(bundle, limit=max(args.preview_limit, 0))}
    if args.output_dir:
        payload["exported_files"] = export_bundle(bundle, args.output_dir)
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
