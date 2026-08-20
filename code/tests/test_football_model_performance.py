from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sports_ai.experiments.football_model_performance import PerformanceBenchmarkConfig, run_historical_performance_benchmark
from sports_ai.storage.football_research_store import connect as connect_research_store
from sports_ai.storage.football_research_store import ensure_schema as ensure_research_schema


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def test_historical_performance_benchmark_is_deterministic(tmp_path: Path):
    research_db_path = tmp_path / "football_research.sqlite"
    with connect_research_store(research_db_path) as conn:
        ensure_research_schema(conn)
        conn.execute(
            """
            INSERT INTO football_events (
                event_id, competition_id, competition_name, season_code, division_code, match_date, kickoff_time,
                home_team, away_team, home_goals, away_goals, result_code, home_shots, away_shots,
                home_shots_on_target, away_shots_on_target, home_corners, away_corners
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "evt_fixture_1",
                "ENG1",
                "Premier League",
                "2526",
                "E0",
                "2026-08-19",
                "19:00",
                "Home FC",
                "Away FC",
                2,
                1,
                "H",
                11,
                8,
                5,
                3,
                6,
                5,
            ),
        )
        conn.commit()

    day8_path = tmp_path / "day8.jsonl"
    _write_jsonl(
        day8_path,
        [
            {
                "actual_away_goals": 1,
                "actual_home_goals": 2,
                "actual_total_corners": 11,
                "actual_total_goals": 3,
                "as_of": "2026-08-19T19:00:00",
                "competition_id": "ENG1",
                "competition_name": "Premier League",
                "conversion_model_version": "football_day8_goal_conversion_v1",
                "corner_distribution_parameters": {"dispersion": 10.0},
                "division_code": "E0",
                "event_id": "evt_fixture_1",
                "expected_goals_away": 1.1,
                "expected_goals_home": 1.8,
                "expected_total_goals": 2.9,
                "gap_model_version_corners": "corners_v1",
                "gap_model_version_shots": "shots_v1",
                "gap_model_version_shots_on_target": "sot_v1",
                "home_team_id": "home",
                "kickoff_time": "19:00",
                "match_date": "2026-08-19",
                "predicted_away_corners": 4.8,
                "predicted_away_shots": 8.4,
                "predicted_away_shots_on_target": 3.2,
                "predicted_home_corners": 5.6,
                "predicted_home_shots": 10.8,
                "predicted_home_shots_on_target": 4.6,
                "sample_context": {
                    "away_team_pre_match_competition_matches": 10,
                    "home_team_pre_match_competition_matches": 10,
                },
                "season_code": "2526",
                "selected_corner_distribution": "poisson",
            }
        ],
    )

    day10_path = tmp_path / "day10.jsonl"
    _write_jsonl(
        day10_path,
        [
            {"event_id": "evt_fixture_1", "market_family": "1x2", "line": None, "selection": "HOME", "effective_win_stake_probability": 0.55},
            {"event_id": "evt_fixture_1", "market_family": "1x2", "line": None, "selection": "DRAW", "effective_win_stake_probability": 0.2},
            {"event_id": "evt_fixture_1", "market_family": "1x2", "line": None, "selection": "AWAY", "effective_win_stake_probability": 0.25},
            {"event_id": "evt_fixture_1", "market_family": "btts", "line": None, "selection": "YES", "effective_win_stake_probability": 0.58},
            {"event_id": "evt_fixture_1", "market_family": "btts", "line": None, "selection": "NO", "effective_win_stake_probability": 0.42},
            {"event_id": "evt_fixture_1", "market_family": "goals_total", "line": 1.5, "selection": "OVER", "effective_win_stake_probability": 0.7},
            {"event_id": "evt_fixture_1", "market_family": "goals_total", "line": 1.5, "selection": "UNDER", "effective_win_stake_probability": 0.3},
            {"event_id": "evt_fixture_1", "market_family": "corner_total", "line": 8.5, "selection": "OVER", "effective_win_stake_probability": 0.65},
            {"event_id": "evt_fixture_1", "market_family": "corner_total", "line": 8.5, "selection": "UNDER", "effective_win_stake_probability": 0.35},
            {"event_id": "evt_fixture_1", "market_family": "asian_handicap", "line": 0.0, "selection": "HOME", "effective_win_stake_probability": 0.62},
            {"event_id": "evt_fixture_1", "market_family": "asian_handicap", "line": 0.0, "selection": "AWAY", "effective_win_stake_probability": 0.38},
        ],
    )

    day11_path = tmp_path / "day11.jsonl"
    _write_jsonl(
        day11_path,
        [
            {
                "competition_id": "ENG1",
                "selection": "HOME",
                "best_available_price": 2.15,
                "decision": "PRELIMINARY_CANDIDATE",
            }
        ],
    )

    config = PerformanceBenchmarkConfig(
        day8_predictions_path=day8_path,
        day10_market_prices_path=day10_path,
        day11_decisions_path=day11_path,
        research_db_path=research_db_path,
        output_dir=tmp_path / "performance",
    )

    first = run_historical_performance_benchmark(config)
    second = run_historical_performance_benchmark(config)

    assert first["summary"] == second["summary"]
    assert first["chronology"]["chronology_status"] == "PASS"
    assert first["summary"]["clv_availability"] == "UNAVAILABLE_HISTORICALLY"
    assert first["status_flags"]["HISTORICAL_BENCHMARK_COMPLETE"] == "YES"
    assert (config.output_dir / "football_performance_summary.json").exists()
    assert (config.output_dir / "FOOTBALL_MODEL_PERFORMANCE_REPORT.md").exists()
