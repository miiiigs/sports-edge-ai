import json
import math
import sqlite3

import pytest

from sports_ai.experiments.football_day8_goal_conversion import run_day8_goal_conversion_experiment
from sports_ai.experiments.football_day9_scoreline_model import (
    build_dixon_coles_scoreline_distribution,
    build_plain_poisson_scoreline_distribution,
    dixon_coles_tau,
    run_day9_scoreline_experiment,
    write_day9_artifacts,
)


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
        ("eng1_2122_001", "ENG1", "Premier League", "2122", "E0", "2021-08-01", "12:00", "Alpha", "Beta", 0, 0, "D", 0, 0, "D", "Ref A", 10, 8, 4, 3, 10, 12, 6, 5, 1, 2, 0, 0),
        ("eng1_2122_002", "ENG1", "Premier League", "2122", "E0", "2021-08-08", "12:00", "Gamma", "Alpha", 1, 0, "H", 1, 0, "H", "Ref B", 11, 9, 5, 4, 9, 10, 7, 5, 2, 1, 0, 0),
        ("eng1_2223_001", "ENG1", "Premier League", "2223", "E0", "2022-08-01", "12:00", "Alpha", "Gamma", 1, 1, "D", 1, 0, "H", "Ref C", 12, 7, 5, 2, 11, 10, 6, 4, 1, 2, 0, 0),
        ("eng1_2223_002", "ENG1", "Premier League", "2223", "E0", "2022-08-08", "12:00", "Beta", "Alpha", 0, 1, "A", 0, 1, "A", "Ref D", 9, 10, 3, 4, 12, 11, 4, 6, 2, 1, 0, 0),
        ("eng1_2324_001", "ENG1", "Premier League", "2324", "E0", "2023-08-01", "12:00", "Alpha", "Beta", 1, 1, "D", 1, 1, "D", "Ref E", 13, 8, 6, 3, 10, 12, 10, 2, 1, 2, 0, 0),
        ("eng1_2324_002", "ENG1", "Premier League", "2324", "E0", "2023-08-08", "12:00", "Gamma", "Alpha", 0, 1, "A", 0, 1, "A", "Ref F", 10, 11, 4, 5, 11, 10, 3, 8, 1, 1, 0, 0),
        ("eng1_2425_001", "ENG1", "Premier League", "2425", "E0", "2024-08-01", "12:00", "Alpha", "Gamma", 2, 1, "H", 1, 0, "H", "Ref G", 12, 9, 5, 4, 10, 11, 12, 1, 1, 2, 0, 0),
        ("eng1_2425_002", "ENG1", "Premier League", "2425", "E0", "2024-08-08", "12:00", "Beta", "Alpha", 1, 2, "A", 0, 1, "A", "Ref H", 8, 12, 3, 5, 13, 10, 2, 9, 2, 1, 0, 0),
        ("eng2_2122_001", "ENG2", "Championship", "2122", "E1", "2021-08-02", "15:00", "Xray", "Yankee", 0, 0, "D", 0, 0, "D", "Ref I", 18, 6, 6, 2, 10, 13, 8, 3, 1, 2, 0, 0),
        ("eng2_2122_002", "ENG2", "Championship", "2122", "E1", "2021-08-09", "15:00", "Zulu", "Xray", 1, 1, "D", 1, 1, "D", "Ref J", 7, 16, 2, 5, 12, 11, 3, 7, 2, 1, 0, 0),
        ("eng2_2223_001", "ENG2", "Championship", "2223", "E1", "2022-08-02", "15:00", "Xray", "Zulu", 1, 1, "D", 1, 0, "H", "Ref K", 17, 5, 6, 2, 9, 12, 8, 3, 1, 2, 0, 0),
        ("eng2_2223_002", "ENG2", "Championship", "2223", "E1", "2022-08-09", "15:00", "Yankee", "Xray", 0, 1, "A", 0, 1, "A", "Ref L", 6, 17, 2, 6, 13, 9, 3, 8, 2, 1, 0, 0),
        ("eng2_2324_001", "ENG2", "Championship", "2324", "E1", "2023-08-02", "15:00", "Xray", "Yankee", 1, 1, "D", 1, 1, "D", "Ref M", 19, 7, 7, 3, 11, 12, 11, 2, 1, 2, 0, 0),
        ("eng2_2324_002", "ENG2", "Championship", "2324", "E1", "2023-08-09", "15:00", "Zulu", "Xray", 1, 2, "A", 1, 1, "D", "Ref N", 8, 18, 3, 6, 12, 10, 2, 10, 2, 1, 0, 0),
        ("eng2_2425_001", "ENG2", "Championship", "2425", "E1", "2024-08-02", "15:00", "Xray", "Zulu", 2, 1, "H", 1, 1, "D", "Ref O", 18, 6, 6, 2, 10, 12, 9, 3, 1, 2, 0, 0),
        ("eng2_2425_002", "ENG2", "Championship", "2425", "E1", "2024-08-09", "15:00", "Yankee", "Xray", 1, 2, "A", 0, 1, "A", "Ref P", 7, 18, 2, 6, 13, 10, 3, 9, 2, 1, 0, 0),
    ]


def _run(db_path):
    return run_day9_scoreline_experiment(research_db_path=db_path)


def test_day9_rho_zero_collapses_to_plain_poisson():
    plain = build_plain_poisson_scoreline_distribution(1.4, 1.0)
    corrected = build_dixon_coles_scoreline_distribution(1.4, 1.0, rho=0.0)

    for home_goals in range(11):
        for away_goals in range(11):
            assert corrected["matrix"][home_goals][away_goals] == pytest.approx(
                plain["matrix"][home_goals][away_goals]
            )
    assert corrected["tail_mass_beyond_max_goals"] == pytest.approx(plain["tail_mass_beyond_max_goals"])


def test_day9_tau_touches_only_four_low_score_cells():
    changed_cells = []
    for home_goals in range(4):
        for away_goals in range(4):
            tau = dixon_coles_tau(
                home_goals,
                away_goals,
                home_goals_mean=1.4,
                away_goals_mean=1.1,
                rho=-0.1,
            )
            if tau != pytest.approx(1.0):
                changed_cells.append((home_goals, away_goals))

    assert sorted(changed_cells) == [(0, 0), (0, 1), (1, 0), (1, 1)]


def test_day9_matrix_probabilities_stay_finite_and_non_negative():
    distribution = build_dixon_coles_scoreline_distribution(1.7, 1.2, rho=-0.1)

    for row in distribution["matrix"]:
        for value in row:
            assert value >= 0.0
            assert math.isfinite(value)


def test_day9_matrix_mass_policy_is_explicit():
    distribution = build_plain_poisson_scoreline_distribution(1.6, 1.1)

    assert distribution["raw_matrix_mass"] + distribution["tail_mass_beyond_max_goals"] == pytest.approx(1.0)
    assert sum(sum(row) for row in distribution["matrix"]) == pytest.approx(1.0)
    assert distribution["normalization_factor"] == pytest.approx(1.0 / distribution["raw_matrix_mass"])


def test_day9_is_deterministic(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    first = _run(research_db)
    second = _run(research_db)

    assert first["predictions"] == second["predictions"]
    assert first["rho_variant_records"] == second["rho_variant_records"]
    assert first["fold_records"] == second["fold_records"]
    assert first["manifest"]["diagnostics"] == second["manifest"]["diagnostics"]
    assert first["manifest"]["time_decay_summary"] == second["manifest"]["time_decay_summary"]


def test_day9_no_market_odds_are_required(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    result = _run(research_db)

    assert result["manifest"]["prediction_count"] > 0


def test_day9_preserves_league_isolation(tmp_path):
    base_db = tmp_path / "base.sqlite"
    changed_db = tmp_path / "changed.sqlite"
    _create_store(base_db, _base_rows())

    changed_rows = list(_base_rows())
    changed_rows[4] = (
        "eng1_2324_001", "ENG1", "Premier League", "2324", "E0", "2023-08-01", "12:00",
        "Alpha", "Beta", 4, 4, "D", 1, 1, "D", "Ref E", 13, 8, 6, 3, 10, 12, 10, 2, 1, 2, 0, 0
    )
    _create_store(changed_db, changed_rows)

    base_result = _run(base_db)
    changed_result = _run(changed_db)
    base_eng2 = [row for row in base_result["predictions"] if row["competition_id"] == "ENG2"]
    changed_eng2 = [row for row in changed_result["predictions"] if row["competition_id"] == "ENG2"]

    assert base_eng2 == changed_eng2


def test_day9_rho_variant_records_are_chronological(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    result = _run(research_db)

    for record in result["rho_variant_records"]:
        assert record["train_period"]["end_season"] < record["evaluation_period"]["start_season"]


def test_day9_future_results_do_not_change_earlier_predictions(tmp_path):
    base_db = tmp_path / "base.sqlite"
    changed_db = tmp_path / "changed.sqlite"
    _create_store(base_db, _base_rows())

    changed_rows = list(_base_rows())
    changed_rows[6] = (
        "eng1_2425_001", "ENG1", "Premier League", "2425", "E0", "2024-08-01", "12:00",
        "Alpha", "Gamma", 7, 6, "H", 1, 0, "H", "Ref G", 12, 9, 5, 4, 10, 11, 12, 1, 1, 2, 0, 0
    )
    _create_store(changed_db, changed_rows)

    base_result = _run(base_db)
    changed_result = _run(changed_db)
    base_row = next(row for row in base_result["predictions"] if row["event_id"] == "eng1_2324_001")
    changed_row = next(row for row in changed_result["predictions"] if row["event_id"] == "eng1_2324_001")

    assert base_row["rho"] == pytest.approx(changed_row["rho"])
    assert base_row["xi"] == pytest.approx(changed_row["xi"])
    assert base_row["scoreline_matrix"] == changed_row["scoreline_matrix"]


def test_day9_predictions_preserve_as_of_and_kickoff_order(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    result = _run(research_db)

    for row in result["predictions"]:
        assert row["as_of"] <= f"{row['match_date']}T{row['kickoff_time'] or '00:00'}:00"


def test_day9_model_versions_persist_in_artifacts(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())
    result = _run(research_db)

    manifest_path = tmp_path / "manifest.json"
    predictions_path = tmp_path / "predictions.jsonl"
    variants_path = tmp_path / "rho_variants.json"
    folds_path = tmp_path / "folds.json"
    markdown_path = tmp_path / "summary.md"
    artifact_paths = write_day9_artifacts(
        result,
        manifest_path=manifest_path,
        predictions_jsonl_path=predictions_path,
        rho_variants_path=variants_path,
        fold_records_path=folds_path,
        markdown_path=markdown_path,
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    prediction_rows = [
        json.loads(line)
        for line in predictions_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert artifact_paths["manifest_path"] == str(manifest_path)
    assert manifest["scoreline_model_version"] == "football_day9_dixon_coles_scoreline_v1"
    assert all(row["scoreline_model_version"] == "football_day9_dixon_coles_scoreline_v1" for row in prediction_rows)
    assert all("conversion_model_version" in row for row in prediction_rows)


def test_day9_fitted_rho_is_reproducible(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    first = _run(research_db)
    second = _run(research_db)

    assert first["manifest"]["final_rho_by_competition"] == second["manifest"]["final_rho_by_competition"]


def test_day9_time_decay_comparison_is_reproducible(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    first = _run(research_db)
    second = _run(research_db)

    assert first["manifest"]["time_decay_summary"] == second["manifest"]["time_decay_summary"]


def test_day9_does_not_mutate_day8_outputs(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    day8_before = run_day8_goal_conversion_experiment(research_db_path=research_db)
    day9_result = _run(research_db)
    day8_after = run_day8_goal_conversion_experiment(research_db_path=research_db)

    assert day8_before["predictions"] == day8_after["predictions"]
    assert day8_before["conversion_fit_records"] == day8_after["conversion_fit_records"]
    assert day9_result["day8_result"]["predictions"] == day8_before["predictions"]


def test_day9_draw_correction_is_measured_explicitly(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    result = _run(research_db)
    overall = result["manifest"]["diagnostics"]["overall"]

    assert overall["average_plain_draw_probability"] is not None
    assert overall["average_dixon_coles_draw_probability"] is not None
    assert overall["average_draw_probability_difference"] is not None
    assert all("draw_probability_difference" in row for row in result["predictions"])
