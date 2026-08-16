import json
import sqlite3

import pytest

from sports_ai.experiments.football_gap_shots_walkforward import (
    DEFAULT_LEARNING_RATE,
    run_gap_walkforward_experiment,
    write_gap_walkforward_artifacts,
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
                referee TEXT,
                home_shots INTEGER,
                away_shots INTEGER,
                home_shots_on_target INTEGER,
                away_shots_on_target INTEGER,
                home_corners INTEGER,
                away_corners INTEGER
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO football_events (
                event_id, competition_id, competition_name, season_code, division_code,
                match_date, kickoff_time, home_team, away_team, referee,
                home_shots, away_shots, home_shots_on_target, away_shots_on_target,
                home_corners, away_corners
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    conn.close()


def _base_rows():
    return [
        (
            "evt_eng1_2122_001",
            "ENG1",
            "Premier League",
            "2122",
            "E0",
            "2021-08-01",
            "12:00",
            "Alpha",
            "Beta",
            "Ref A",
            10,
            8,
            4,
            3,
            6,
            5,
        ),
        (
            "evt_eng1_2122_002",
            "ENG1",
            "Premier League",
            "2122",
            "E0",
            "2021-08-02",
            "15:00",
            "Gamma",
            "Alpha",
            "Ref B",
            12,
            9,
            5,
            4,
            7,
            5,
        ),
        (
            "evt_eng1_2223_001",
            "ENG1",
            "Premier League",
            "2223",
            "E0",
            "2022-08-01",
            "12:30",
            "Alpha",
            "Delta",
            "Ref C",
            11,
            7,
            5,
            2,
            6,
            4,
        ),
        (
            "evt_eng1_2223_002",
            "ENG1",
            "Premier League",
            "2223",
            "E0",
            "2022-08-01",
            "15:00",
            "Beta",
            "Gamma",
            "Ref D",
            9,
            6,
            4,
            2,
            5,
            3,
        ),
        (
            "evt_eng1_2223_003",
            "ENG1",
            "Premier League",
            "2223",
            "E0",
            "2022-08-01",
            "15:00",
            "NewTown",
            "Alpha",
            "Ref E",
            14,
            10,
            6,
            4,
            7,
            5,
        ),
        (
            "evt_eng2_2122_001",
            "ENG2",
            "Championship",
            "2122",
            "E1",
            "2021-08-01",
            "11:00",
            "Xray",
            "Yankee",
            "Ref F",
            20,
            5,
            7,
            2,
            8,
            3,
        ),
        (
            "evt_eng2_2223_001",
            "ENG2",
            "Championship",
            "2223",
            "E1",
            "2022-08-01",
            "11:00",
            "Xray",
            "Zulu",
            "Ref G",
            18,
            4,
            6,
            1,
            7,
            2,
        ),
    ]


def _run(db_path):
    return run_gap_walkforward_experiment(research_db_path=db_path)


def test_walkforward_runner_is_deterministic(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    first = _run(research_db)
    second = _run(research_db)

    assert first["predictions"] == second["predictions"]
    assert first["final_state"]["state_records"] == second["final_state"]["state_records"]
    assert first["manifest"]["prediction_count"] == second["manifest"]["prediction_count"]
    assert first["manifest"]["evaluation_prediction_count"] == second["manifest"]["evaluation_prediction_count"]
    assert first["manifest"]["skip_reason_counts"] == second["manifest"]["skip_reason_counts"]


def test_walkforward_prediction_is_generated_before_current_match_update(tmp_path):
    base_db = tmp_path / "base.sqlite"
    changed_db = tmp_path / "changed.sqlite"
    rows = _base_rows()
    _create_store(base_db, rows)

    changed_rows = list(rows)
    changed_rows[2] = (
        "evt_eng1_2223_001",
        "ENG1",
        "Premier League",
        "2223",
        "E0",
        "2022-08-01",
        "12:30",
        "Alpha",
        "Delta",
        "Ref C",
        30,
        1,
        5,
        2,
        6,
        4,
    )
    _create_store(changed_db, changed_rows)

    base_result = _run(base_db)
    changed_result = _run(changed_db)
    base_first_eval = next(row for row in base_result["predictions"] if row["event_id"] == "evt_eng1_2223_001")
    changed_first_eval = next(row for row in changed_result["predictions"] if row["event_id"] == "evt_eng1_2223_001")

    assert base_first_eval["expected_home"] == pytest.approx(changed_first_eval["expected_home"])
    assert base_first_eval["expected_away"] == pytest.approx(changed_first_eval["expected_away"])
    assert base_first_eval["actual_home"] != changed_first_eval["actual_home"]


def test_walkforward_future_match_changes_do_not_alter_earlier_predictions(tmp_path):
    base_db = tmp_path / "base.sqlite"
    future_changed_db = tmp_path / "future_changed.sqlite"
    rows = _base_rows()
    _create_store(base_db, rows)

    changed_rows = list(rows)
    changed_rows[4] = (
        "evt_eng1_2223_003",
        "ENG1",
        "Premier League",
        "2223",
        "E0",
        "2022-08-01",
        "15:00",
        "NewTown",
        "Alpha",
        "Ref E",
        40,
        2,
        6,
        4,
        7,
        5,
    )
    _create_store(future_changed_db, changed_rows)

    base_result = _run(base_db)
    changed_result = _run(future_changed_db)

    for event_id in ("evt_eng1_2122_001", "evt_eng1_2122_002", "evt_eng1_2223_001", "evt_eng1_2223_002"):
        base_row = next(row for row in base_result["predictions"] if row["event_id"] == event_id)
        changed_row = next(row for row in changed_result["predictions"] if row["event_id"] == event_id)
        assert base_row["expected_home"] == pytest.approx(changed_row["expected_home"])
        assert base_row["expected_away"] == pytest.approx(changed_row["expected_away"])


def test_walkforward_initialization_baseline_isolation(tmp_path):
    early_db = tmp_path / "early.sqlite"
    late_extreme_db = tmp_path / "late_extreme.sqlite"
    rows = _base_rows()
    _create_store(early_db, rows)

    changed_rows = list(rows)
    changed_rows.extend(
        [
            (
                "evt_eng1_2223_004",
                "ENG1",
                "Premier League",
                "2223",
                "E0",
                "2023-04-01",
                "15:00",
                "Delta",
                "Beta",
                "Ref H",
                45,
                1,
                8,
                1,
                10,
                1,
            ),
            (
                "evt_eng1_2223_005",
                "ENG1",
                "Premier League",
                "2223",
                "E0",
                "2023-05-01",
                "15:00",
                "Gamma",
                "Delta",
                "Ref I",
                1,
                40,
                1,
                7,
                1,
                9,
            ),
        ]
    )
    _create_store(late_extreme_db, changed_rows)

    early_result = _run(early_db)
    late_result = _run(late_extreme_db)
    early_first_eval = next(row for row in early_result["predictions"] if row["event_id"] == "evt_eng1_2223_001")
    late_first_eval = next(row for row in late_result["predictions"] if row["event_id"] == "evt_eng1_2223_001")

    assert early_first_eval["expected_home"] == pytest.approx(late_first_eval["expected_home"])
    assert early_first_eval["expected_away"] == pytest.approx(late_first_eval["expected_away"])
    assert early_result["manifest"]["baseline_details"][0]["warmup_season_code"] == "2122"


def test_walkforward_uses_stable_kickoff_and_event_ordering(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    result = _run(research_db)
    eng1_event_ids = [
        row["event_id"]
        for row in result["predictions"]
        if row["competition_id"] == "ENG1" and row["season_code"] == "2223"
    ]

    assert eng1_event_ids == [
        "evt_eng1_2223_001",
        "evt_eng1_2223_002",
        "evt_eng1_2223_003",
    ]


def test_walkforward_preserves_league_isolation_and_tracks_season_transition_counts(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    result = _run(research_db)
    eng1_first_eval = next(row for row in result["predictions"] if row["event_id"] == "evt_eng1_2223_001")
    eng2_first_eval = next(row for row in result["predictions"] if row["event_id"] == "evt_eng2_2223_001")
    newcomer = next(row for row in result["predictions"] if row["event_id"] == "evt_eng1_2223_003")

    assert eng1_first_eval["home_team_pre_match_competition_matches"] == 2
    assert eng1_first_eval["away_team_pre_match_competition_matches"] == 0
    assert newcomer["home_team_pre_match_competition_matches"] == 0
    assert eng2_first_eval["expected_home"] == pytest.approx(12.6875)
    assert eng2_first_eval["expected_away"] == pytest.approx(12.3125)


def test_walkforward_enforces_readiness_gate(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    rows = [
        (
            "evt_eng1_2122_001",
            "ENG1",
            "Premier League",
            "2122",
            "E0",
            "2021-08-01",
            "12:00",
            "Alpha",
            "Beta",
            "Ref A",
            10,
            8,
            4,
            3,
            6,
            5,
        ),
        (
            "evt_eng1_2223_001",
            "ENG1",
            "Premier League",
            "2223",
            "E0",
            "2022-08-01",
            "12:30",
            "Alpha",
            "Gamma",
            "Ref B",
            None,
            7,
            4,
            2,
            6,
            4,
        ),
    ]
    _create_store(research_db, rows)

    result = _run(research_db)

    assert result["manifest"]["seasons_processed"] == ["2122"]
    assert result["manifest"]["prediction_count"] == 1
    assert result["manifest"]["evaluation_prediction_count"] == 0


def test_walkforward_artifact_serialization_is_consistent(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())
    result = _run(research_db)

    manifest_path = tmp_path / "manifest.json"
    predictions_path = tmp_path / "predictions.jsonl"
    final_state_path = tmp_path / "final_state.json"
    markdown_path = tmp_path / "summary.md"
    artifact_paths = write_gap_walkforward_artifacts(
        result,
        manifest_path=manifest_path,
        predictions_jsonl_path=predictions_path,
        final_state_path=final_state_path,
        markdown_path=markdown_path,
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    final_state = json.loads(final_state_path.read_text(encoding="utf-8"))
    prediction_lines = predictions_path.read_text(encoding="utf-8").strip().splitlines()

    assert artifact_paths["manifest_path"] == str(manifest_path)
    assert manifest["prediction_count"] == len(prediction_lines)
    assert len(final_state["state_records"]) == len(result["final_state"]["state_records"])
    assert "Football GAP Shots Walk-Forward" in markdown_path.read_text(encoding="utf-8")
    assert manifest["learning_rate"] == pytest.approx(DEFAULT_LEARNING_RATE)
