import sqlite3

from sports_ai.experiments.football_gap_data_readiness import (
    EXPECTED_FOOTBALL_SCOPE,
    FIELD_READINESS_COMPLETE,
    FIELD_READINESS_NOT_LOADED,
    SCOPE_STATUS_LOADED,
    SCOPE_STATUS_NOT_LOADED,
    generate_gap_data_readiness_report,
)


def _seed_research_store(db_path):
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
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                home_shots INTEGER,
                away_shots INTEGER,
                home_shots_on_target INTEGER,
                away_shots_on_target INTEGER,
                home_corners INTEGER,
                away_corners INTEGER,
                referee TEXT
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO football_events (
                event_id, competition_id, competition_name, season_code, division_code,
                match_date, home_team, away_team, home_shots, away_shots,
                home_shots_on_target, away_shots_on_target, home_corners, away_corners, referee
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "evt1",
                    "ENG1",
                    "Premier League",
                    "2526",
                    "E0",
                    "2025-08-10",
                    "A",
                    "B",
                    12,
                    8,
                    5,
                    3,
                    7,
                    4,
                    "Ref One",
                ),
                (
                    "evt2",
                    "ENG1",
                    "Premier League",
                    "2526",
                    "E0",
                    "2025-08-11",
                    "C",
                    "D",
                    11,
                    9,
                    4,
                    2,
                    6,
                    5,
                    "Ref Two",
                ),
                (
                    "evt3",
                    "ESP1",
                    "La Liga",
                    "2526",
                    "SP1",
                    "2025-08-12",
                    "E",
                    "F",
                    10,
                    7,
                    3,
                    1,
                    5,
                    3,
                    None,
                ),
            ],
        )
    conn.close()


def test_gap_data_readiness_marks_loaded_rows_complete_for_gap_fields(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _seed_research_store(research_db)

    report = generate_gap_data_readiness_report(research_db_path=research_db)
    eng1_row = next(
        row
        for row in report["rows"]
        if row["competition_id"] == "ENG1" and row["season_code"] == "2526"
    )

    assert eng1_row["scope_status"] == SCOPE_STATUS_LOADED
    assert eng1_row["event_count"] == 2
    assert eng1_row["shots_ready"] == FIELD_READINESS_COMPLETE
    assert eng1_row["shots_on_target_ready"] == FIELD_READINESS_COMPLETE
    assert eng1_row["corners_ready"] == FIELD_READINESS_COMPLETE
    assert eng1_row["referee_ready"] == FIELD_READINESS_COMPLETE


def test_gap_data_readiness_preserves_referee_as_separate_non_gap_dependency(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _seed_research_store(research_db)

    report = generate_gap_data_readiness_report(research_db_path=research_db)
    esp1_row = next(
        row
        for row in report["rows"]
        if row["competition_id"] == "ESP1" and row["season_code"] == "2526"
    )

    assert esp1_row["scope_status"] == SCOPE_STATUS_LOADED
    assert esp1_row["shots_ready"] == FIELD_READINESS_COMPLETE
    assert esp1_row["shots_on_target_ready"] == FIELD_READINESS_COMPLETE
    assert esp1_row["corners_ready"] == FIELD_READINESS_COMPLETE
    assert esp1_row["referee_ready"] != FIELD_READINESS_COMPLETE


def test_gap_data_readiness_marks_expected_but_missing_scope_not_loaded(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _seed_research_store(research_db)

    report = generate_gap_data_readiness_report(research_db_path=research_db)
    fra1_row = next(
        row
        for row in report["rows"]
        if row["competition_id"] == "FRA1" and row["season_code"] == "2526"
    )

    assert fra1_row["scope_status"] == SCOPE_STATUS_NOT_LOADED
    assert fra1_row["shots_ready"] == FIELD_READINESS_NOT_LOADED
    assert fra1_row["shots_on_target_ready"] == FIELD_READINESS_NOT_LOADED
    assert fra1_row["corners_ready"] == FIELD_READINESS_NOT_LOADED


def test_gap_data_readiness_distinguishes_loaded_scope_from_full_spec_scope(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _seed_research_store(research_db)

    report = generate_gap_data_readiness_report(
        research_db_path=research_db,
        expected_scope=EXPECTED_FOOTBALL_SCOPE,
    )

    assert report["scope_summary"]["data_present_for_currently_loaded_scope"] is True
    assert report["scope_summary"]["full_spec_historical_scope_complete"] is False
    assert "FRA1" in report["scope_summary"]["missing_competitions"]
