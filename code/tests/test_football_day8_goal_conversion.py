import json
import math
import sqlite3

import pytest

from sports_ai.experiments.football_day8_goal_conversion import (
    run_day8_goal_conversion_experiment,
    write_day8_artifacts,
)
from sports_ai.experiments.football_gap_lambda_fitting import build_selected_gap_walkforward_results


def _create_store(db_path, rows):
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            """
            CREATE TABLE football_events (
                event_id TEXT PRIMARY KEY,
                competition_id TEXT NOT NULL,
                competition_name TEXT NOT NULL,
                season_code TEXT NOT NULL,
                division_code TEXT NOT NULL,
                match_date TEXT NOT NULL,
                kickoff_time TEXT,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                home_goals INTEGER,
                away_goals INTEGER,
                result_code TEXT,
                half_time_home_goals INTEGER,
                half_time_away_goals INTEGER,
                half_time_result_code TEXT,
                referee TEXT,
                home_shots INTEGER,
                away_shots INTEGER,
                home_shots_on_target INTEGER,
                away_shots_on_target INTEGER,
                home_fouls INTEGER,
                away_fouls INTEGER,
                home_corners INTEGER,
                away_corners INTEGER,
                home_yellow_cards INTEGER,
                away_yellow_cards INTEGER,
                home_red_cards INTEGER,
                away_red_cards INTEGER
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO football_events (
                event_id, competition_id, competition_name, season_code, division_code,
                match_date, kickoff_time, home_team, away_team, home_goals, away_goals,
                result_code, half_time_home_goals, half_time_away_goals, half_time_result_code,
                referee, home_shots, away_shots, home_shots_on_target, away_shots_on_target,
                home_fouls, away_fouls, home_corners, away_corners, home_yellow_cards,
                away_yellow_cards, home_red_cards, away_red_cards
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    conn.close()


def _base_rows():
    return [
        ("eng1_2122_001", "ENG1", "Premier League", "2122", "E0", "2021-08-01", "12:00", "Alpha", "Beta", 2, 1, "H", 1, 0, "H", "Ref A", 10, 8, 4, 3, 10, 12, 6, 5, 1, 2, 0, 0),
        ("eng1_2122_002", "ENG1", "Premier League", "2122", "E0", "2021-08-08", "12:00", "Gamma", "Alpha", 1, 2, "A", 1, 1, "D", "Ref B", 11, 9, 5, 4, 9, 10, 7, 5, 2, 1, 0, 0),
        ("eng1_2223_001", "ENG1", "Premier League", "2223", "E0", "2022-08-01", "12:00", "Alpha", "Gamma", 2, 0, "H", 1, 0, "H", "Ref C", 12, 7, 5, 2, 11, 10, 6, 4, 1, 2, 0, 0),
        ("eng1_2223_002", "ENG1", "Premier League", "2223", "E0", "2022-08-08", "12:00", "Beta", "Alpha", 1, 2, "A", 0, 1, "A", "Ref D", 9, 10, 3, 4, 12, 11, 4, 6, 2, 1, 0, 0),
        ("eng1_2324_001", "ENG1", "Premier League", "2324", "E0", "2023-08-01", "12:00", "Alpha", "Beta", 3, 1, "H", 2, 1, "H", "Ref E", 13, 8, 6, 3, 10, 12, 10, 2, 1, 2, 0, 0),
        ("eng1_2324_002", "ENG1", "Premier League", "2324", "E0", "2023-08-08", "12:00", "Gamma", "Alpha", 0, 2, "A", 0, 1, "A", "Ref F", 10, 11, 4, 5, 11, 10, 3, 8, 1, 1, 0, 0),
        ("eng1_2425_001", "ENG1", "Premier League", "2425", "E0", "2024-08-01", "12:00", "Alpha", "Gamma", 2, 1, "H", 1, 0, "H", "Ref G", 12, 9, 5, 4, 10, 11, 12, 1, 1, 2, 0, 0),
        ("eng1_2425_002", "ENG1", "Premier League", "2425", "E0", "2024-08-08", "12:00", "Beta", "Alpha", 0, 3, "A", 0, 1, "A", "Ref H", 8, 12, 3, 5, 13, 10, 2, 9, 2, 1, 0, 0),
        ("eng2_2122_001", "ENG2", "Championship", "2122", "E1", "2021-08-02", "15:00", "Xray", "Yankee", 3, 0, "H", 1, 0, "H", "Ref I", 18, 6, 6, 2, 10, 13, 8, 3, 1, 2, 0, 0),
        ("eng2_2122_002", "ENG2", "Championship", "2122", "E1", "2021-08-09", "15:00", "Zulu", "Xray", 1, 2, "A", 1, 1, "D", "Ref J", 7, 16, 2, 5, 12, 11, 3, 7, 2, 1, 0, 0),
        ("eng2_2223_001", "ENG2", "Championship", "2223", "E1", "2022-08-02", "15:00", "Xray", "Zulu", 2, 0, "H", 1, 0, "H", "Ref K", 17, 5, 6, 2, 9, 12, 8, 3, 1, 2, 0, 0),
        ("eng2_2223_002", "ENG2", "Championship", "2223", "E1", "2022-08-09", "15:00", "Yankee", "Xray", 0, 2, "A", 0, 1, "A", "Ref L", 6, 17, 2, 6, 13, 9, 3, 8, 2, 1, 0, 0),
        ("eng2_2324_001", "ENG2", "Championship", "2324", "E1", "2023-08-02", "15:00", "Xray", "Yankee", 4, 1, "H", 2, 1, "H", "Ref M", 19, 7, 7, 3, 11, 12, 11, 2, 1, 2, 0, 0),
        ("eng2_2324_002", "ENG2", "Championship", "2324", "E1", "2023-08-09", "15:00", "Zulu", "Xray", 1, 3, "A", 1, 1, "D", "Ref N", 8, 18, 3, 6, 12, 10, 2, 10, 2, 1, 0, 0),
        ("eng2_2425_001", "ENG2", "Championship", "2425", "E1", "2024-08-02", "15:00", "Xray", "Zulu", 2, 1, "H", 1, 1, "D", "Ref O", 18, 6, 6, 2, 10, 12, 9, 3, 1, 2, 0, 0),
        ("eng2_2425_002", "ENG2", "Championship", "2425", "E1", "2024-08-09", "15:00", "Yankee", "Xray", 1, 2, "A", 0, 1, "A", "Ref P", 7, 18, 2, 6, 13, 10, 3, 9, 2, 1, 0, 0),
    ]


def _run(db_path):
    return run_day8_goal_conversion_experiment(research_db_path=db_path)


def test_day8_goal_conversion_is_deterministic(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    first = _run(research_db)
    second = _run(research_db)

    assert first["predictions"] == second["predictions"]
    assert first["conversion_fit_records"] == second["conversion_fit_records"]
    assert first["corner_fold_records"] == second["corner_fold_records"]
    assert first["manifest"]["diagnostics"] == second["manifest"]["diagnostics"]


def test_day8_future_goals_do_not_change_earlier_predictions(tmp_path):
    base_db = tmp_path / "base.sqlite"
    changed_db = tmp_path / "changed.sqlite"
    _create_store(base_db, _base_rows())

    changed_rows = list(_base_rows())
    changed_rows[6] = (
        "eng1_2425_001", "ENG1", "Premier League", "2425", "E0", "2024-08-01", "12:00",
        "Alpha", "Gamma", 8, 7, "H", 1, 0, "H", "Ref G", 12, 9, 5, 4, 10, 11, 12, 1, 1, 2, 0, 0
    )
    _create_store(changed_db, changed_rows)

    base_result = _run(base_db)
    changed_result = _run(changed_db)
    base_row = next(row for row in base_result["predictions"] if row["event_id"] == "eng1_2223_001")
    changed_row = next(row for row in changed_result["predictions"] if row["event_id"] == "eng1_2223_001")

    assert base_row["expected_goals_home"] == pytest.approx(changed_row["expected_goals_home"])
    assert base_row["expected_goals_away"] == pytest.approx(changed_row["expected_goals_away"])


def test_day8_market_odds_are_not_required(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    result = _run(research_db)

    assert result["manifest"]["prediction_count"] > 0


def test_day8_preserves_league_isolation(tmp_path):
    base_db = tmp_path / "base.sqlite"
    changed_db = tmp_path / "changed.sqlite"
    _create_store(base_db, _base_rows())

    changed_rows = list(_base_rows())
    changed_rows[4] = (
        "eng1_2324_001", "ENG1", "Premier League", "2324", "E0", "2023-08-01", "12:00",
        "Alpha", "Beta", 7, 6, "H", 2, 1, "H", "Ref E", 13, 8, 6, 3, 10, 12, 10, 2, 1, 2, 0, 0
    )
    _create_store(changed_db, changed_rows)

    base_result = _run(base_db)
    changed_result = _run(changed_db)
    base_eng2 = [row for row in base_result["predictions"] if row["competition_id"] == "ENG2"]
    changed_eng2 = [row for row in changed_result["predictions"] if row["competition_id"] == "ENG2"]

    assert base_eng2 == changed_eng2


def test_day8_conversion_records_are_chronological(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    result = _run(research_db)

    for record in result["conversion_fit_records"]:
        assert record["train_period"]["end_season"] < record["validation_period"]["start_season"]


def test_day8_predictions_preserve_as_of_and_kickoff_order(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    result = _run(research_db)

    for row in result["predictions"]:
        assert row["as_of"] <= f"{row['match_date']}T{row['kickoff_time'] or '00:00'}:00"


def test_day8_does_not_modify_gap_state(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    result = _run(research_db)
    rebuilt_gap_results = build_selected_gap_walkforward_results(
        research_db_path=research_db,
        fit_result=result["fit_result"],
    )

    for statistic in sorted(rebuilt_gap_results):
        assert result["selected_gap_results"][statistic]["predictions"] == rebuilt_gap_results[statistic]["predictions"]
        assert (
            result["selected_gap_results"][statistic]["final_state"]["state_records"]
            == rebuilt_gap_results[statistic]["final_state"]["state_records"]
        )


def test_day8_expected_goals_are_non_negative_and_finite(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    result = _run(research_db)

    for row in result["predictions"]:
        assert row["expected_goals_home"] >= 0.0
        assert row["expected_goals_away"] >= 0.0
        assert math.isfinite(row["expected_goals_home"])
        assert math.isfinite(row["expected_goals_away"])


def test_day8_home_and_away_outputs_remain_distinct(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    result = _run(research_db)

    assert any(
        row["expected_goals_home"] != pytest.approx(row["expected_goals_away"])
        for row in result["predictions"]
    )


def test_day8_corner_distribution_comparison_is_deterministic(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    first = _run(research_db)
    second = _run(research_db)

    assert first["corner_fold_records"] == second["corner_fold_records"]
    assert first["manifest"]["corner_selection_summary"] == second["manifest"]["corner_selection_summary"]


def test_day8_distribution_selection_matches_recorded_evidence(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    result = _run(research_db)

    for summary in result["manifest"]["corner_selection_summary"]:
        if summary["selected_distribution"] == "negative_binomial":
            assert summary["cumulative_negative_binomial_log_likelihood"] > summary["cumulative_poisson_log_likelihood"]
        else:
            assert summary["cumulative_poisson_log_likelihood"] >= summary["cumulative_negative_binomial_log_likelihood"]


def test_day8_generated_artifacts_preserve_model_versions(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())
    result = _run(research_db)

    manifest_path = tmp_path / "manifest.json"
    predictions_path = tmp_path / "predictions.jsonl"
    conversion_path = tmp_path / "conversion.json"
    corner_path = tmp_path / "corners.json"
    markdown_path = tmp_path / "summary.md"
    artifact_paths = write_day8_artifacts(
        result,
        manifest_path=manifest_path,
        predictions_jsonl_path=predictions_path,
        conversion_records_path=conversion_path,
        corner_records_path=corner_path,
        markdown_path=markdown_path,
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    prediction_rows = [
        json.loads(line)
        for line in predictions_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert artifact_paths["manifest_path"] == str(manifest_path)
    assert manifest["conversion_model_version"] == "football_day8_goal_conversion_v1"
    assert all(row["conversion_model_version"] == "football_day8_goal_conversion_v1" for row in prediction_rows)
    assert all("gap_model_version_shots" in row for row in prediction_rows)


def test_day8_goals_do_not_feed_back_into_gap_predictions(tmp_path):
    base_db = tmp_path / "base.sqlite"
    changed_db = tmp_path / "changed.sqlite"
    _create_store(base_db, _base_rows())

    changed_rows = []
    for row in _base_rows():
        mutable = list(row)
        mutable[9] = mutable[9] + 3
        mutable[10] = mutable[10] + 2
        changed_rows.append(tuple(mutable))
    _create_store(changed_db, changed_rows)

    base_result = _run(base_db)
    changed_result = _run(changed_db)

    for statistic in sorted(base_result["selected_gap_results"]):
        assert (
            base_result["selected_gap_results"][statistic]["predictions"]
            == changed_result["selected_gap_results"][statistic]["predictions"]
        )
        assert (
            base_result["selected_gap_results"][statistic]["final_state"]["state_records"]
            == changed_result["selected_gap_results"][statistic]["final_state"]["state_records"]
        )
