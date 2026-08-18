"""Chronological football GAP walk-forward experiment for football process statistics.

This module provides the bounded Day 7 replay shell for:

- shots
- shots on target
- corners

It enforces chronological replay, warm-up isolation, deterministic ordering, and
explicit persisted `as_of` timestamps for pre-match prediction rows.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Sequence

from ..agents.football.gap_ratings import GapConfigEntry, GapObservation, GapRatingsEngine
from .football_gap_data_readiness import (
    FIELD_READINESS_COMPLETE,
    SCOPE_STATUS_LOADED,
    generate_gap_data_readiness_report,
)

EXPERIMENT_ID = "EXP-007"
EXPERIMENT_VERSION = "football_gap_shots_walkforward_v1"
ENGINE_MODEL_VERSION = "football_gap_reference_v1"
DEFAULT_STATISTIC = "shots"
DEFAULT_LEARNING_RATE = 0.05
LEARNING_RATE_LABEL = "REFERENCE / UNOPTIMIZED"
STATISTIC_DISPLAY_NAMES = {
    "shots": "Shots",
    "shots_on_target": "Shots on Target",
    "corners": "Corners",
}
BASELINE_POLICY_ID = "warmup_first_ready_season_league_venue_average_v1"
BASELINE_POLICY_DESCRIPTION = (
    "For each competition/statistic, estimate home and away baselines from the earliest "
    "readiness-approved season only. Process that season chronologically as warm-up, then start "
    "evaluation on later seasons with no future-season leakage."
)
SEASON_TRANSITION_POLICY = (
    "Carry GAP state forward across seasons within the same competition. Do not reset at season "
    "boundaries. Teams newly appearing in a competition start from the competition baseline with "
    "zero current-competition matches observed."
)
DUPLICATE_EVENT_POLICY = (
    "Reject duplicate observations inside GapRatingsEngine by (competition_id, statistic, event_id) "
    "so the same event cannot move ratings twice."
)
FIRST_MATCH_WARMUP_POLICY = (
    "The earliest readiness-approved season per competition is treated as a warm-up season. "
    "Predictions are exported for reproducibility, but evaluation diagnostics exclude warm-up rows."
)
AS_OF_POLICY_ID = "historical_prediction_as_of_fixture_kickoff_v1"
AS_OF_POLICY_DESCRIPTION = (
    "Prediction-row as_of is the latest source-backed pre-match timestamp the replayed prediction is "
    "allowed to depend on. Because the historical source exposes fixture date plus kickoff time but no "
    "finer observation timestamp for the football-process inputs used here, persist as_of as the fixture "
    "kickoff timestamp when kickoff_time is present, otherwise the conservative day-start timestamp "
    "match_dateT00:00:00. This preserves the rule that historical pre-match as_of never exceeds kickoff."
)

STATISTIC_VALUE_FIELDS = {
    "shots": ("home_shots", "away_shots"),
    "shots_on_target": ("home_shots_on_target", "away_shots_on_target"),
    "corners": ("home_corners", "away_corners"),
}
READINESS_FIELD_NAMES = {
    "shots": "shots_ready",
    "shots_on_target": "shots_on_target_ready",
    "corners": "corners_ready",
}


@dataclass(frozen=True)
class HistoricalGapMatch:
    event_id: str
    competition_id: str
    competition_name: str
    season_code: str
    division_code: str
    match_date: str
    kickoff_time: str | None
    home_team_id: str
    away_team_id: str
    statistic: str
    home_value: float | None
    away_value: float | None

    @property
    def sort_key(self) -> tuple[str, str, str, str]:
        return (
            self.competition_id,
            self.match_date,
            self.kickoff_time or "",
            self.event_id,
        )

    @property
    def as_of(self) -> str:
        return build_historical_prediction_as_of(
            match_date=self.match_date,
            kickoff_time=self.kickoff_time,
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_historical_prediction_as_of(*, match_date: str, kickoff_time: str | None) -> str:
    if not kickoff_time:
        return f"{match_date}T00:00:00"
    time_parts = kickoff_time.split(":")
    if len(time_parts) not in {2, 3}:
        raise ValueError(f"unsupported kickoff_time format {kickoff_time!r}")
    hour = int(time_parts[0])
    minute = int(time_parts[1])
    second = int(time_parts[2]) if len(time_parts) == 3 else 0
    return f"{match_date}T{hour:02d}:{minute:02d}:{second:02d}"


def _connect_research_store(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _approved_scope_rows(
    readiness_report: dict[str, Any],
    statistic: str,
    *,
    competition_ids: Sequence[str] | None = None,
    season_codes: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    readiness_field = READINESS_FIELD_NAMES[statistic]
    competition_filter = {str(value) for value in competition_ids} if competition_ids else None
    season_filter = {str(value) for value in season_codes} if season_codes else None
    rows = []
    for row in readiness_report["rows"]:
        if row["scope_status"] != SCOPE_STATUS_LOADED:
            continue
        if row[readiness_field] != FIELD_READINESS_COMPLETE:
            continue
        competition_id = str(row["competition_id"])
        season_code = str(row["season_code"])
        if competition_filter is not None and competition_id not in competition_filter:
            continue
        if season_filter is not None and season_code not in season_filter:
            continue
        rows.append(row)
    if not rows:
        raise ValueError(f"no readiness-approved scope rows found for statistic {statistic!r}")
    return sorted(rows, key=lambda row: (row["competition_id"], row["season_code"]))


def _warmup_season_by_competition(approved_scope_rows: Iterable[dict[str, Any]]) -> dict[str, str]:
    warmup_by_competition: dict[str, str] = {}
    for row in approved_scope_rows:
        competition_id = str(row["competition_id"])
        season_code = str(row["season_code"])
        current = warmup_by_competition.get(competition_id)
        if current is None or season_code < current:
            warmup_by_competition[competition_id] = season_code
    return warmup_by_competition


def _load_historical_matches(
    *,
    research_db_path: str | Path,
    approved_scope_rows: Sequence[dict[str, Any]],
    statistic: str,
) -> list[HistoricalGapMatch]:
    home_field, away_field = STATISTIC_VALUE_FIELDS[statistic]
    approved_scope = {
        (str(row["competition_id"]), str(row["season_code"]))
        for row in approved_scope_rows
    }
    approved_competitions = sorted({competition_id for competition_id, _ in approved_scope})
    placeholders = ", ".join("?" for _ in approved_competitions)
    query = f"""
        SELECT
            event_id,
            competition_id,
            competition_name,
            season_code,
            division_code,
            match_date,
            kickoff_time,
            home_team,
            away_team,
            {home_field} AS home_value,
            {away_field} AS away_value
        FROM football_events
        WHERE competition_id IN ({placeholders})
        ORDER BY competition_id, match_date, COALESCE(kickoff_time, ''), event_id
    """
    with _connect_research_store(research_db_path) as conn:
        rows = conn.execute(query, approved_competitions).fetchall()

    return [
        HistoricalGapMatch(
            event_id=str(row["event_id"]),
            competition_id=str(row["competition_id"]),
            competition_name=str(row["competition_name"]),
            season_code=str(row["season_code"]),
            division_code=str(row["division_code"]),
            match_date=str(row["match_date"]),
            kickoff_time=str(row["kickoff_time"]) if row["kickoff_time"] is not None else None,
            home_team_id=str(row["home_team"]),
            away_team_id=str(row["away_team"]),
            statistic=statistic,
            home_value=float(row["home_value"]) if row["home_value"] is not None else None,
            away_value=float(row["away_value"]) if row["away_value"] is not None else None,
        )
        for row in rows
        if (str(row["competition_id"]), str(row["season_code"])) in approved_scope
    ]


def _resolve_learning_rates_by_competition(
    *,
    approved_scope_rows: Sequence[dict[str, Any]],
    learning_rate: float,
    learning_rates_by_competition: dict[str, float] | None,
) -> dict[str, float]:
    competition_ids = sorted({str(row["competition_id"]) for row in approved_scope_rows})
    if learning_rates_by_competition is None:
        return {competition_id: learning_rate for competition_id in competition_ids}

    resolved = {str(key): float(value) for key, value in learning_rates_by_competition.items()}
    missing = [competition_id for competition_id in competition_ids if competition_id not in resolved]
    if missing:
        raise ValueError(
            "missing learning-rate entries for competitions: "
            + ", ".join(sorted(missing))
        )
    unexpected = sorted(set(resolved) - set(competition_ids))
    if unexpected:
        raise ValueError(
            "learning-rate map contains competitions outside the approved scope: "
            + ", ".join(unexpected)
        )
    return {competition_id: resolved[competition_id] for competition_id in competition_ids}


def _build_engine_config_entries(
    *,
    matches: Sequence[HistoricalGapMatch],
    warmup_season_by_competition: dict[str, str],
    statistic: str,
    learning_rates_by_competition: dict[str, float],
    model_version: str,
) -> tuple[list[GapConfigEntry], list[dict[str, Any]]]:
    grouped: dict[str, list[HistoricalGapMatch]] = defaultdict(list)
    for match in matches:
        if match.season_code == warmup_season_by_competition[match.competition_id]:
            grouped[match.competition_id].append(match)

    config_entries: list[GapConfigEntry] = []
    baseline_details: list[dict[str, Any]] = []
    for competition_id in sorted(grouped):
        warmup_matches = grouped[competition_id]
        if not warmup_matches:
            raise ValueError(f"missing warm-up matches for competition {competition_id}")
        if any(match.home_value is None or match.away_value is None for match in warmup_matches):
            raise ValueError(f"warm-up matches for competition {competition_id} contain missing statistic values")

        initial_home_baseline = mean(match.home_value for match in warmup_matches if match.home_value is not None)
        initial_away_baseline = mean(match.away_value for match in warmup_matches if match.away_value is not None)
        warmup_season_code = warmup_matches[0].season_code
        competition_learning_rate = learning_rates_by_competition[competition_id]
        config_identity = (
            f"{competition_id.lower()}_{statistic}_"
            f"warmup_{warmup_season_code}_lambda_{competition_learning_rate:.2f}_reference_v1"
        )
        config_entries.append(
            GapConfigEntry(
                competition_id=competition_id,
                statistic=statistic,
                learning_rate=competition_learning_rate,
                initial_home_baseline=initial_home_baseline,
                initial_away_baseline=initial_away_baseline,
                model_version=model_version,
            )
        )
        baseline_details.append(
            {
                "competition_id": competition_id,
                "competition_name": warmup_matches[0].competition_name,
                "warmup_season_code": warmup_season_code,
                "baseline_match_count": len(warmup_matches),
                "initial_home_baseline": initial_home_baseline,
                "initial_away_baseline": initial_away_baseline,
                "baseline_policy_id": BASELINE_POLICY_ID,
                "config_identity": config_identity,
                "learning_rate": competition_learning_rate,
            }
        )
    return config_entries, baseline_details


def _diagnostic_bucket(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "prediction_count": 0,
            "mae_home": None,
            "mae_away": None,
            "combined_mae": None,
            "mean_error_home": None,
            "mean_error_away": None,
            "predicted_home_average": None,
            "observed_home_average": None,
            "predicted_away_average": None,
            "observed_away_average": None,
        }

    abs_home_errors = [abs(float(row["error_home"])) for row in rows]
    abs_away_errors = [abs(float(row["error_away"])) for row in rows]
    home_errors = [float(row["error_home"]) for row in rows]
    away_errors = [float(row["error_away"]) for row in rows]
    return {
        "prediction_count": len(rows),
        "mae_home": mean(abs_home_errors),
        "mae_away": mean(abs_away_errors),
        "combined_mae": (sum(abs_home_errors) + sum(abs_away_errors)) / (2 * len(rows)),
        "mean_error_home": mean(home_errors),
        "mean_error_away": mean(away_errors),
        "predicted_home_average": mean(float(row["expected_home"]) for row in rows),
        "observed_home_average": mean(float(row["actual_home"]) for row in rows),
        "predicted_away_average": mean(float(row["expected_away"]) for row in rows),
        "observed_away_average": mean(float(row["actual_away"]) for row in rows),
    }


def _diagnostics_by_key(
    prediction_rows: Sequence[dict[str, Any]],
    *,
    key: str,
) -> dict[str, dict[str, Any]]:
    evaluation_rows = [row for row in prediction_rows if row["included_in_evaluation"]]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in evaluation_rows:
        grouped[str(row[key])].append(row)
    return {
        group_key: _diagnostic_bucket(group_rows)
        for group_key, group_rows in sorted(grouped.items())
    }


def _write_jsonl(path: str | Path, rows: Sequence[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True))
            handle.write("\n")


def _render_markdown_summary(manifest: dict[str, Any]) -> str:
    diagnostics_by_league = manifest["diagnostics"]["by_competition"]
    diagnostics_by_season = manifest["diagnostics"]["by_season"]

    league_lines = [
        "| Competition | Predictions | MAE Home | MAE Away | Combined MAE | Mean Error Home | Mean Error Away |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for competition_id, metrics in diagnostics_by_league.items():
        league_lines.append(
            f"| {competition_id} | {metrics['prediction_count']} | "
            f"{metrics['mae_home']:.4f} | {metrics['mae_away']:.4f} | {metrics['combined_mae']:.4f} | "
            f"{metrics['mean_error_home']:.4f} | {metrics['mean_error_away']:.4f} |"
        )

    season_lines = [
        "| Season | Predictions | MAE Home | MAE Away | Combined MAE | Mean Error Home | Mean Error Away |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for season_code, metrics in diagnostics_by_season.items():
        season_lines.append(
            f"| {season_code} | {metrics['prediction_count']} | "
            f"{metrics['mae_home']:.4f} | {metrics['mae_away']:.4f} | {metrics['combined_mae']:.4f} | "
            f"{metrics['mean_error_home']:.4f} | {metrics['mean_error_away']:.4f} |"
        )

    learning_rate_lines = [
        "| Competition | Learning Rate |",
        "| --- | ---: |",
    ]
    for competition_id, learning_rate in manifest["learning_rates_by_competition"].items():
        learning_rate_lines.append(f"| {competition_id} | {learning_rate:.4f} |")

    learning_rate_summary = (
        f"`{manifest['learning_rate']}` (`{manifest['learning_rate_label']}`)"
        if manifest["learning_rate"] is not None
        else f"competition-specific (`{manifest['learning_rate_label']}`)"
    )

    return "\n".join(
        [
            f"# {manifest['report_title']}",
            "",
            f"Generated at: `{manifest['generated_at']}`",
            f"Experiment/version: `{manifest['experiment_id']}` / `{manifest['experiment_version']}`",
            "",
            "## Run Summary",
            "",
            f"- competitions processed: `{', '.join(manifest['competitions_processed'])}`",
            f"- seasons processed: `{', '.join(manifest['seasons_processed'])}`",
            f"- statistic: `{manifest['statistic']}`",
            f"- learning rate mode: {learning_rate_summary}",
            f"- baseline policy: `{manifest['baseline_policy_id']}`",
            f"- as_of policy: `{manifest['as_of_policy_id']}`",
            f"- matches processed: `{manifest['matches_processed']}`",
            f"- predictions produced: `{manifest['prediction_count']}`",
            f"- evaluation predictions: `{manifest['evaluation_prediction_count']}`",
            f"- events skipped: `{manifest['skipped_event_count']}`",
            "",
            "## Policies",
            "",
            f"- warm-up policy: {manifest['first_match_warmup_policy']}",
            f"- season-transition policy: {manifest['season_transition_policy']}",
            f"- duplicate-event policy: {manifest['duplicate_event_policy']}",
            f"- as_of policy: {manifest['as_of_policy_description']}",
            "",
            "## Learning Rates by Competition",
            "",
            *learning_rate_lines,
            "",
            "## Diagnostics by Competition",
            "",
            *league_lines,
            "",
            "## Diagnostics by Season",
            "",
            *season_lines,
            "",
            "## Notes",
            "",
            "- Diagnostics are evaluation-only and exclude warm-up-season rows.",
            "- This experiment uses only football-process statistics from the research store.",
            "- No betting simulation, market pricing, or goal conversion is included.",
        ]
    )


def run_gap_walkforward_experiment(
    *,
    research_db_path: str | Path,
    statistic: str = DEFAULT_STATISTIC,
    learning_rate: float = DEFAULT_LEARNING_RATE,
    learning_rates_by_competition: dict[str, float] | None = None,
    learning_rate_label: str = LEARNING_RATE_LABEL,
    model_version: str = ENGINE_MODEL_VERSION,
    experiment_id: str = EXPERIMENT_ID,
    experiment_version: str = EXPERIMENT_VERSION,
    experiment_module: str = "sports_ai.experiments.football_gap_shots_walkforward",
    competition_ids: Sequence[str] | None = None,
    season_codes: Sequence[str] | None = None,
    evaluation_season_codes: Sequence[str] | None = None,
    report_title: str | None = None,
) -> dict[str, Any]:
    if statistic not in STATISTIC_VALUE_FIELDS:
        raise ValueError(f"unsupported GAP walk-forward statistic {statistic!r}")

    readiness_report = generate_gap_data_readiness_report(research_db_path=research_db_path)
    approved_scope_rows = _approved_scope_rows(
        readiness_report,
        statistic,
        competition_ids=competition_ids,
        season_codes=season_codes,
    )
    warmup_by_competition = _warmup_season_by_competition(approved_scope_rows)
    resolved_learning_rates = _resolve_learning_rates_by_competition(
        approved_scope_rows=approved_scope_rows,
        learning_rate=learning_rate,
        learning_rates_by_competition=learning_rates_by_competition,
    )
    matches = _load_historical_matches(
        research_db_path=research_db_path,
        approved_scope_rows=approved_scope_rows,
        statistic=statistic,
    )
    config_entries, baseline_details = _build_engine_config_entries(
        matches=matches,
        warmup_season_by_competition=warmup_by_competition,
        statistic=statistic,
        learning_rates_by_competition=resolved_learning_rates,
        model_version=model_version,
    )
    baseline_details_by_competition = {
        detail["competition_id"]: detail
        for detail in baseline_details
    }
    evaluation_season_filter = (
        {str(value) for value in evaluation_season_codes}
        if evaluation_season_codes is not None
        else None
    )

    engine = GapRatingsEngine(config_entries)
    competition_match_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    prediction_rows: list[dict[str, Any]] = []
    skipped_events: list[dict[str, Any]] = []

    for match in matches:
        if match.home_value is None or match.away_value is None:
            raise ValueError(
                f"readiness-approved {statistic} match {match.event_id!r} has missing statistic values"
            )

        pre_home_state = engine.state_record(
            competition_id=match.competition_id,
            statistic=statistic,
            team_id=match.home_team_id,
            venue="home",
        )
        pre_away_state = engine.state_record(
            competition_id=match.competition_id,
            statistic=statistic,
            team_id=match.away_team_id,
            venue="away",
        )
        pre_home_competition_matches = competition_match_counts[match.competition_id][match.home_team_id]
        pre_away_competition_matches = competition_match_counts[match.competition_id][match.away_team_id]

        update_result = engine.apply_observation(
            GapObservation(
                event_id=match.event_id,
                competition_id=match.competition_id,
                match_date=match.match_date,
                home_team_id=match.home_team_id,
                away_team_id=match.away_team_id,
                statistic=statistic,
                home_value=match.home_value,
                away_value=match.away_value,
            )
        )

        is_warmup = match.season_code == warmup_by_competition[match.competition_id]
        included_in_evaluation = not is_warmup and (
            evaluation_season_filter is None or match.season_code in evaluation_season_filter
        )
        baseline_detail = baseline_details_by_competition[match.competition_id]
        competition_learning_rate = resolved_learning_rates[match.competition_id]
        prediction_rows.append(
            {
                "event_id": match.event_id,
                "competition_id": match.competition_id,
                "competition_name": match.competition_name,
                "season_code": match.season_code,
                "division_code": match.division_code,
                "match_date": match.match_date,
                "kickoff_time": match.kickoff_time,
                "as_of": match.as_of,
                "match_sort_key": list(match.sort_key),
                "home_team_id": match.home_team_id,
                "away_team_id": match.away_team_id,
                "statistic": statistic,
                "phase": "warmup" if is_warmup else "evaluation",
                "included_in_evaluation": included_in_evaluation,
                "expected_home": update_result.prediction.expected_home,
                "expected_away": update_result.prediction.expected_away,
                "actual_home": match.home_value,
                "actual_away": match.away_value,
                "error_home": update_result.error_home,
                "error_away": update_result.error_away,
                "home_team_pre_match_venue_matches": pre_home_state.matches_observed,
                "away_team_pre_match_venue_matches": pre_away_state.matches_observed,
                "home_team_pre_match_competition_matches": pre_home_competition_matches,
                "away_team_pre_match_competition_matches": pre_away_competition_matches,
                "learning_rate": competition_learning_rate,
                "learning_rate_label": learning_rate_label,
                "model_version": model_version,
                "baseline_policy_id": BASELINE_POLICY_ID,
                "baseline_config_identity": baseline_detail["config_identity"],
                "warmup_season_code": baseline_detail["warmup_season_code"],
            }
        )

        competition_match_counts[match.competition_id][match.home_team_id] += 1
        competition_match_counts[match.competition_id][match.away_team_id] += 1

    skip_reason_counts = dict(sorted(Counter(row["reason"] for row in skipped_events).items()))
    diagnostics = {
        "overall_evaluation": _diagnostic_bucket(
            [row for row in prediction_rows if row["included_in_evaluation"]]
        ),
        "by_competition": _diagnostics_by_key(prediction_rows, key="competition_id"),
        "by_season": _diagnostics_by_key(prediction_rows, key="season_code"),
    }
    unique_learning_rates = sorted({round(value, 12) for value in resolved_learning_rates.values()})
    manifest_learning_rate = unique_learning_rates[0] if len(unique_learning_rates) == 1 else None
    final_state = {
        "generated_at": _utc_now(),
        "statistic": statistic,
        "model_version": model_version,
        "baseline_policy_id": BASELINE_POLICY_ID,
        "learning_rate": manifest_learning_rate,
        "learning_rates_by_competition": resolved_learning_rates,
        "learning_rate_label": learning_rate_label,
        "as_of_policy_id": AS_OF_POLICY_ID,
        "as_of_policy_description": AS_OF_POLICY_DESCRIPTION,
        "state_records": engine.export_state_records(),
    }

    manifest = {
        "generated_at": _utc_now(),
        "experiment_id": experiment_id,
        "experiment_version": experiment_version,
        "source_research_db_path": str(Path(research_db_path)),
        "report_title": report_title or f"Football GAP {STATISTIC_DISPLAY_NAMES[statistic]} Walk-Forward",
        "statistic": statistic,
        "competitions_processed": sorted({row["competition_id"] for row in approved_scope_rows}),
        "seasons_processed": sorted({row["season_code"] for row in approved_scope_rows}),
        "approved_scope_pairs": [
            {
                "competition_id": row["competition_id"],
                "season_code": row["season_code"],
                "division_code": row["division_code"],
            }
            for row in approved_scope_rows
        ],
        "competition_filter": sorted({str(value) for value in competition_ids}) if competition_ids else None,
        "season_filter": sorted({str(value) for value in season_codes}) if season_codes else None,
        "evaluation_season_codes": sorted(evaluation_season_filter) if evaluation_season_filter else None,
        "baseline_policy_id": BASELINE_POLICY_ID,
        "baseline_policy_description": BASELINE_POLICY_DESCRIPTION,
        "baseline_details": baseline_details,
        "learning_rate": manifest_learning_rate,
        "learning_rates_by_competition": resolved_learning_rates,
        "learning_rate_label": learning_rate_label,
        "model_version": model_version,
        "matches_processed": len(prediction_rows),
        "prediction_count": len(prediction_rows),
        "evaluation_prediction_count": sum(row["included_in_evaluation"] for row in prediction_rows),
        "skipped_event_count": len(skipped_events),
        "skip_reason_counts": skip_reason_counts,
        "skipped_events": skipped_events,
        "first_match_warmup_policy": FIRST_MATCH_WARMUP_POLICY,
        "season_transition_policy": SEASON_TRANSITION_POLICY,
        "duplicate_event_policy": DUPLICATE_EVENT_POLICY,
        "deterministic_order_policy": (
            "Order by competition_id, match_date, kickoff_time, event_id. "
            "The current research store has populated kickoff_time values for all loaded rows."
        ),
        "as_of_policy_id": AS_OF_POLICY_ID,
        "as_of_policy_description": AS_OF_POLICY_DESCRIPTION,
        "code_identity": {
            "experiment_module": experiment_module,
            "engine_module": "sports_ai.agents.football.gap_ratings",
        },
        "diagnostics": diagnostics,
    }
    return {
        "manifest": manifest,
        "predictions": prediction_rows,
        "final_state": final_state,
        "readiness_report": readiness_report,
    }


def write_gap_walkforward_artifacts(
    result: dict[str, Any],
    *,
    manifest_path: str | Path,
    predictions_jsonl_path: str | Path,
    final_state_path: str | Path,
    markdown_path: str | Path,
) -> dict[str, str]:
    manifest_target = Path(manifest_path)
    predictions_target = Path(predictions_jsonl_path)
    final_state_target = Path(final_state_path)
    markdown_target = Path(markdown_path)

    for target in (manifest_target, predictions_target, final_state_target, markdown_target):
        target.parent.mkdir(parents=True, exist_ok=True)

    manifest_target.write_text(json.dumps(result["manifest"], indent=2), encoding="utf-8")
    _write_jsonl(predictions_target, result["predictions"])
    final_state_target.write_text(json.dumps(result["final_state"], indent=2), encoding="utf-8")
    markdown_target.write_text(_render_markdown_summary(result["manifest"]), encoding="utf-8")
    return {
        "manifest_path": str(manifest_target),
        "predictions_jsonl_path": str(predictions_target),
        "final_state_path": str(final_state_target),
        "markdown_path": str(markdown_target),
    }


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the chronological football GAP shots walk-forward experiment.")
    parser.add_argument("--research-db", required=True, help="Path to football research SQLite store.")
    parser.add_argument("--manifest-out", help="Optional manifest JSON path.")
    parser.add_argument("--predictions-out", help="Optional predictions JSONL path.")
    parser.add_argument("--final-state-out", help="Optional final state JSON path.")
    parser.add_argument("--markdown-out", help="Optional Markdown summary path.")
    parser.add_argument("--learning-rate", type=float, default=DEFAULT_LEARNING_RATE, help="Fixed reference learning rate.")
    args = parser.parse_args(argv)

    result = run_gap_walkforward_experiment(
        research_db_path=args.research_db,
        statistic=DEFAULT_STATISTIC,
        learning_rate=args.learning_rate,
        model_version=ENGINE_MODEL_VERSION,
    )

    payload: dict[str, Any] = {
        "manifest": result["manifest"],
    }
    if args.manifest_out and args.predictions_out and args.final_state_out and args.markdown_out:
        payload["artifacts"] = write_gap_walkforward_artifacts(
            result,
            manifest_path=args.manifest_out,
            predictions_jsonl_path=args.predictions_out,
            final_state_path=args.final_state_out,
            markdown_path=args.markdown_out,
        )
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
