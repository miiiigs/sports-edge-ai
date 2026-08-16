"""Historical football GAP-input data-readiness reporting.

This report answers two different questions:

1. Field readiness:
   Do the loaded historical rows actually contain the required GAP inputs?
2. Scope readiness:
   Does the research store currently cover the full competition scope required
   by the August 15, 2026 football specification?

The report is intentionally fail-closed and does not invent null-rate
thresholds that are not explicitly present in the governing PDFs available in
this repo snapshot.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

FIELD_READINESS_COMPLETE = "COMPLETE"
FIELD_READINESS_INCOMPLETE = "INCOMPLETE"
FIELD_READINESS_NOT_LOADED = "NOT_LOADED"

SCOPE_STATUS_LOADED = "LOADED"
SCOPE_STATUS_NOT_LOADED = "NOT_LOADED"

GAP_STATISTIC_COMPONENTS = {
    "shots": ("home_shots", "away_shots"),
    "shots_on_target": ("home_shots_on_target", "away_shots_on_target"),
    "corners": ("home_corners", "away_corners"),
}


@dataclass(frozen=True)
class ExpectedCompetitionScope:
    competition_id: str
    competition_name: str
    division_code: str | None
    scope_group: str


EXPECTED_FOOTBALL_SCOPE = (
    ExpectedCompetitionScope("ENG1", "Premier League", "E0", "league"),
    ExpectedCompetitionScope("ENG2", "Championship", "E1", "league"),
    ExpectedCompetitionScope("ENG3", "League One", "E2", "league"),
    ExpectedCompetitionScope("ENG4", "League Two", "E3", "league"),
    ExpectedCompetitionScope("ESP1", "La Liga", "SP1", "league"),
    ExpectedCompetitionScope("GER1", "Bundesliga", "D1", "league"),
    ExpectedCompetitionScope("ITA1", "Serie A", "I1", "league"),
    ExpectedCompetitionScope("FRA1", "Ligue 1", "F1", "league"),
    ExpectedCompetitionScope("SCO1", "Scottish Premiership", "SC0", "league"),
    ExpectedCompetitionScope("UCL", "Champions League", None, "europe"),
    ExpectedCompetitionScope("UEL", "Europa League", None, "europe"),
    ExpectedCompetitionScope("FAC", "FA Cup", None, "domestic_cup"),
    ExpectedCompetitionScope("EFLC", "EFL Cup", None, "domestic_cup"),
)


@dataclass(frozen=True)
class GapReadinessRow:
    competition_id: str
    competition_name: str
    division_code: str | None
    season_code: str
    scope_group: str
    scope_status: str
    event_count: int
    first_match_date: str | None
    last_match_date: str | None
    home_shots_null_count: int | None
    home_shots_null_rate: float | None
    away_shots_null_count: int | None
    away_shots_null_rate: float | None
    home_shots_on_target_null_count: int | None
    home_shots_on_target_null_rate: float | None
    away_shots_on_target_null_count: int | None
    away_shots_on_target_null_rate: float | None
    home_corners_null_count: int | None
    home_corners_null_rate: float | None
    away_corners_null_count: int | None
    away_corners_null_rate: float | None
    referee_null_count: int | None
    referee_null_rate: float | None
    shots_ready: str
    shots_on_target_ready: str
    corners_ready: str
    referee_ready: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect_research_store(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_rate(null_count: int | None, event_count: int) -> float | None:
    if null_count is None or event_count <= 0:
        return None
    return null_count / event_count


def _component_ready(
    *,
    home_null_count: int | None,
    away_null_count: int | None,
    scope_status: str,
) -> str:
    if scope_status == SCOPE_STATUS_NOT_LOADED:
        return FIELD_READINESS_NOT_LOADED
    if home_null_count == 0 and away_null_count == 0:
        return FIELD_READINESS_COMPLETE
    return FIELD_READINESS_INCOMPLETE


def _single_field_ready(
    *,
    null_count: int | None,
    scope_status: str,
) -> str:
    if scope_status == SCOPE_STATUS_NOT_LOADED:
        return FIELD_READINESS_NOT_LOADED
    if null_count == 0:
        return FIELD_READINESS_COMPLETE
    return FIELD_READINESS_INCOMPLETE


def _loaded_season_codes(conn: sqlite3.Connection) -> tuple[str, ...]:
    rows = conn.execute(
        """
        SELECT DISTINCT season_code
        FROM football_events
        ORDER BY season_code
        """
    ).fetchall()
    return tuple(str(row["season_code"]) for row in rows)


def _loaded_scope_map(conn: sqlite3.Connection) -> dict[tuple[str, str], sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT
            competition_id,
            competition_name,
            division_code,
            season_code,
            COUNT(*) AS event_count,
            MIN(match_date) AS first_match_date,
            MAX(match_date) AS last_match_date,
            SUM(CASE WHEN home_shots IS NULL THEN 1 ELSE 0 END) AS home_shots_null_count,
            SUM(CASE WHEN away_shots IS NULL THEN 1 ELSE 0 END) AS away_shots_null_count,
            SUM(CASE WHEN home_shots_on_target IS NULL THEN 1 ELSE 0 END) AS home_shots_on_target_null_count,
            SUM(CASE WHEN away_shots_on_target IS NULL THEN 1 ELSE 0 END) AS away_shots_on_target_null_count,
            SUM(CASE WHEN home_corners IS NULL THEN 1 ELSE 0 END) AS home_corners_null_count,
            SUM(CASE WHEN away_corners IS NULL THEN 1 ELSE 0 END) AS away_corners_null_count,
            SUM(CASE WHEN referee IS NULL OR TRIM(referee) = '' THEN 1 ELSE 0 END) AS referee_null_count
        FROM football_events
        GROUP BY competition_id, competition_name, division_code, season_code
        ORDER BY season_code, competition_id
        """
    ).fetchall()
    return {
        (str(row["competition_id"]), str(row["season_code"])): row
        for row in rows
    }


def _unexpected_loaded_competitions(
    loaded_scope_map: dict[tuple[str, str], sqlite3.Row],
    expected_scope: Iterable[ExpectedCompetitionScope],
) -> list[str]:
    expected_ids = {item.competition_id for item in expected_scope}
    return sorted({competition_id for competition_id, _ in loaded_scope_map if competition_id not in expected_ids})


def generate_gap_data_readiness_report(
    *,
    research_db_path: str | Path,
    expected_scope: Sequence[ExpectedCompetitionScope] = EXPECTED_FOOTBALL_SCOPE,
) -> dict[str, Any]:
    """Build the football GAP-input readiness report from stored rows."""

    with _connect_research_store(research_db_path) as conn:
        season_codes = _loaded_season_codes(conn)
        loaded_scope_map = _loaded_scope_map(conn)

    rows: list[GapReadinessRow] = []
    for expected in expected_scope:
        for season_code in season_codes:
            loaded = loaded_scope_map.get((expected.competition_id, season_code))
            if loaded is None:
                rows.append(
                    GapReadinessRow(
                        competition_id=expected.competition_id,
                        competition_name=expected.competition_name,
                        division_code=expected.division_code,
                        season_code=season_code,
                        scope_group=expected.scope_group,
                        scope_status=SCOPE_STATUS_NOT_LOADED,
                        event_count=0,
                        first_match_date=None,
                        last_match_date=None,
                        home_shots_null_count=None,
                        home_shots_null_rate=None,
                        away_shots_null_count=None,
                        away_shots_null_rate=None,
                        home_shots_on_target_null_count=None,
                        home_shots_on_target_null_rate=None,
                        away_shots_on_target_null_count=None,
                        away_shots_on_target_null_rate=None,
                        home_corners_null_count=None,
                        home_corners_null_rate=None,
                        away_corners_null_count=None,
                        away_corners_null_rate=None,
                        referee_null_count=None,
                        referee_null_rate=None,
                        shots_ready=FIELD_READINESS_NOT_LOADED,
                        shots_on_target_ready=FIELD_READINESS_NOT_LOADED,
                        corners_ready=FIELD_READINESS_NOT_LOADED,
                        referee_ready=FIELD_READINESS_NOT_LOADED,
                    )
                )
                continue

            event_count = int(loaded["event_count"])
            home_shots_null_count = int(loaded["home_shots_null_count"])
            away_shots_null_count = int(loaded["away_shots_null_count"])
            home_shots_on_target_null_count = int(loaded["home_shots_on_target_null_count"])
            away_shots_on_target_null_count = int(loaded["away_shots_on_target_null_count"])
            home_corners_null_count = int(loaded["home_corners_null_count"])
            away_corners_null_count = int(loaded["away_corners_null_count"])
            referee_null_count = int(loaded["referee_null_count"])

            rows.append(
                GapReadinessRow(
                    competition_id=expected.competition_id,
                    competition_name=str(loaded["competition_name"]),
                    division_code=str(loaded["division_code"]) if loaded["division_code"] is not None else expected.division_code,
                    season_code=season_code,
                    scope_group=expected.scope_group,
                    scope_status=SCOPE_STATUS_LOADED,
                    event_count=event_count,
                    first_match_date=str(loaded["first_match_date"]) if loaded["first_match_date"] is not None else None,
                    last_match_date=str(loaded["last_match_date"]) if loaded["last_match_date"] is not None else None,
                    home_shots_null_count=home_shots_null_count,
                    home_shots_null_rate=_safe_rate(home_shots_null_count, event_count),
                    away_shots_null_count=away_shots_null_count,
                    away_shots_null_rate=_safe_rate(away_shots_null_count, event_count),
                    home_shots_on_target_null_count=home_shots_on_target_null_count,
                    home_shots_on_target_null_rate=_safe_rate(home_shots_on_target_null_count, event_count),
                    away_shots_on_target_null_count=away_shots_on_target_null_count,
                    away_shots_on_target_null_rate=_safe_rate(away_shots_on_target_null_count, event_count),
                    home_corners_null_count=home_corners_null_count,
                    home_corners_null_rate=_safe_rate(home_corners_null_count, event_count),
                    away_corners_null_count=away_corners_null_count,
                    away_corners_null_rate=_safe_rate(away_corners_null_count, event_count),
                    referee_null_count=referee_null_count,
                    referee_null_rate=_safe_rate(referee_null_count, event_count),
                    shots_ready=_component_ready(
                        home_null_count=home_shots_null_count,
                        away_null_count=away_shots_null_count,
                        scope_status=SCOPE_STATUS_LOADED,
                    ),
                    shots_on_target_ready=_component_ready(
                        home_null_count=home_shots_on_target_null_count,
                        away_null_count=away_shots_on_target_null_count,
                        scope_status=SCOPE_STATUS_LOADED,
                    ),
                    corners_ready=_component_ready(
                        home_null_count=home_corners_null_count,
                        away_null_count=away_corners_null_count,
                        scope_status=SCOPE_STATUS_LOADED,
                    ),
                    referee_ready=_single_field_ready(
                        null_count=referee_null_count,
                        scope_status=SCOPE_STATUS_LOADED,
                    ),
                )
            )

    loaded_rows = [row for row in rows if row.scope_status == SCOPE_STATUS_LOADED]
    loaded_scope_complete = all(
        row.shots_ready == FIELD_READINESS_COMPLETE
        and row.shots_on_target_ready == FIELD_READINESS_COMPLETE
        and row.corners_ready == FIELD_READINESS_COMPLETE
        for row in loaded_rows
    )

    report = {
        "generated_at": _utc_now(),
        "research_db_path": str(Path(research_db_path)),
        "loaded_season_codes": list(season_codes),
        "expected_competition_ids": [item.competition_id for item in expected_scope],
        "unexpected_loaded_competitions": _unexpected_loaded_competitions(loaded_scope_map, expected_scope),
        "scope_summary": {
            "expected_scope_entry_count": len(expected_scope) * len(season_codes),
            "loaded_scope_entry_count": sum(row.scope_status == SCOPE_STATUS_LOADED for row in rows),
            "not_loaded_scope_entry_count": sum(row.scope_status == SCOPE_STATUS_NOT_LOADED for row in rows),
            "loaded_competitions": sorted({row.competition_id for row in loaded_rows}),
            "missing_competitions": sorted(
                {
                    item.competition_id
                    for item in expected_scope
                    if item.competition_id not in {row.competition_id for row in loaded_rows}
                }
            ),
            "data_present_for_currently_loaded_scope": loaded_scope_complete,
            "full_spec_historical_scope_complete": (
                loaded_scope_complete
                and all(row.scope_status == SCOPE_STATUS_LOADED for row in rows)
            ),
        },
        "rows": [asdict(row) for row in rows],
    }
    return report


def render_gap_data_readiness_markdown(report: dict[str, Any]) -> str:
    """Render the readiness report as a Markdown summary."""

    scope = report["scope_summary"]
    rows = report["rows"]
    loaded_rows = [row for row in rows if row["scope_status"] == SCOPE_STATUS_LOADED]
    missing_rows = [row for row in rows if row["scope_status"] == SCOPE_STATUS_NOT_LOADED]

    loaded_table_lines = [
        "| Competition | Season | Division | Events | Shots | Shots on Target | Corners | Referee | First Match | Last Match |",
        "| --- | --- | --- | ---: | --- | --- | --- | --- | --- | --- |",
    ]
    for row in loaded_rows:
        loaded_table_lines.append(
            "| "
            + " | ".join(
                [
                    str(row["competition_id"]),
                    str(row["season_code"]),
                    str(row["division_code"] or "N/A"),
                    str(row["event_count"]),
                    str(row["shots_ready"]),
                    str(row["shots_on_target_ready"]),
                    str(row["corners_ready"]),
                    str(row["referee_ready"]),
                    str(row["first_match_date"] or "N/A"),
                    str(row["last_match_date"] or "N/A"),
                ]
            )
            + " |"
        )

    missing_scope_lines = [
        "| Competition | Season | Scope Group | Status |",
        "| --- | --- | --- | --- |",
    ]
    for row in missing_rows:
        missing_scope_lines.append(
            f"| {row['competition_id']} | {row['season_code']} | {row['scope_group']} | {row['scope_status']} |"
        )

    return "\n".join(
        [
            "# Football GAP Data Readiness",
            "",
            f"Generated at: `{report['generated_at']}`",
            "",
            "## Summary",
            "",
            f"- currently loaded seasons: `{', '.join(report['loaded_season_codes'])}`",
            f"- loaded scope entries: `{scope['loaded_scope_entry_count']}`",
            f"- not-loaded expected scope entries: `{scope['not_loaded_scope_entry_count']}`",
            f"- `DATA_PRESENT_FOR_CURRENTLY_LOADED_SCOPE`: `{scope['data_present_for_currently_loaded_scope']}`",
            f"- `FULL_SPEC_HISTORICAL_SCOPE_COMPLETE`: `{scope['full_spec_historical_scope_complete']}`",
            f"- missing competitions from current store: `{', '.join(scope['missing_competitions']) or 'none'}`",
            "",
            "## Loaded Scope Field Readiness",
            "",
            *loaded_table_lines,
            "",
            "## Missing Expected Scope",
            "",
            *missing_scope_lines,
            "",
            "## Notes",
            "",
            "- This report measures actual stored-row completeness, not CSV-header availability.",
            "- `COMPLETE` means the loaded rows for that league-season currently have zero nulls for the relevant stored field(s).",
            "- `INCOMPLETE` means the league-season is loaded but at least one required stored field is null.",
            "- `NOT_LOADED` means the expected competition-season pair is absent from the current research store.",
            "- Referee availability is recorded separately and does not gate the GAP engine itself.",
        ]
    )


def write_gap_data_readiness_artifacts(
    report: dict[str, Any],
    *,
    markdown_path: str | Path,
    json_path: str | Path,
) -> dict[str, str]:
    """Persist JSON and Markdown artifacts."""

    markdown_target = Path(markdown_path)
    json_target = Path(json_path)
    markdown_target.parent.mkdir(parents=True, exist_ok=True)
    json_target.parent.mkdir(parents=True, exist_ok=True)
    markdown_target.write_text(render_gap_data_readiness_markdown(report), encoding="utf-8")
    json_target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return {
        "markdown_path": str(markdown_target),
        "json_path": str(json_target),
    }


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Build the football GAP data-readiness report.")
    parser.add_argument("--research-db", required=True, help="Path to football research SQLite store.")
    parser.add_argument("--markdown-out", help="Optional Markdown output path.")
    parser.add_argument("--json-out", help="Optional JSON output path.")
    args = parser.parse_args(argv)

    report = generate_gap_data_readiness_report(research_db_path=args.research_db)
    payload: dict[str, Any] = {"report": report}
    if args.markdown_out and args.json_out:
        payload["artifacts"] = write_gap_data_readiness_artifacts(
            report,
            markdown_path=args.markdown_out,
            json_path=args.json_out,
        )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
