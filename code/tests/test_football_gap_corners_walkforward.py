import sqlite3

import pytest

from sports_ai.experiments import football_gap_shots_walkforward as generic_gap_walkforward
from sports_ai.experiments.football_gap_corners_walkforward import (
    run_gap_corners_walkforward_experiment,
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
    return run_gap_corners_walkforward_experiment(research_db_path=db_path)


def test_corners_walkforward_runner_is_deterministic(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    first = _run(research_db)
    second = _run(research_db)

    assert first["predictions"] == second["predictions"]
    assert first["final_state"]["state_records"] == second["final_state"]["state_records"]
    assert first["manifest"]["prediction_count"] == second["manifest"]["prediction_count"]


def test_corners_walkforward_prediction_is_generated_before_current_match_update(tmp_path):
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
        11,
        7,
        5,
        2,
        12,
        0,
    )
    _create_store(changed_db, changed_rows)

    base_result = _run(base_db)
    changed_result = _run(changed_db)
    base_first_eval = next(row for row in base_result["predictions"] if row["event_id"] == "evt_eng1_2223_001")
    changed_first_eval = next(row for row in changed_result["predictions"] if row["event_id"] == "evt_eng1_2223_001")

    assert base_first_eval["expected_home"] == pytest.approx(changed_first_eval["expected_home"])
    assert base_first_eval["expected_away"] == pytest.approx(changed_first_eval["expected_away"])
    assert base_first_eval["actual_home"] != changed_first_eval["actual_home"]


def test_corners_walkforward_preserves_statistic_isolation_from_shots_columns(tmp_path):
    base_db = tmp_path / "base.sqlite"
    changed_db = tmp_path / "changed.sqlite"
    rows = _base_rows()
    _create_store(base_db, rows)

    changed_rows = []
    for row in rows:
        mutable = list(row)
        mutable[10] = mutable[10] * 4
        mutable[11] = mutable[11] * 4
        changed_rows.append(tuple(mutable))
    _create_store(changed_db, changed_rows)

    base_result = _run(base_db)
    changed_result = _run(changed_db)

    assert base_result["predictions"] == changed_result["predictions"]
    assert base_result["final_state"]["state_records"] == changed_result["final_state"]["state_records"]


def test_corners_walkforward_preserves_league_isolation(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    result = _run(research_db)
    eng1_first_eval = next(row for row in result["predictions"] if row["event_id"] == "evt_eng1_2223_001")
    eng2_first_eval = next(row for row in result["predictions"] if row["event_id"] == "evt_eng2_2223_001")

    assert eng1_first_eval["competition_id"] == "ENG1"
    assert eng2_first_eval["competition_id"] == "ENG2"
    assert eng1_first_eval["expected_home"] != pytest.approx(eng2_first_eval["expected_home"])


def test_corners_walkforward_tracks_home_away_state_separation(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    result = _run(research_db)
    first_eval = next(row for row in result["predictions"] if row["event_id"] == "evt_eng1_2223_001")
    newcomer = next(row for row in result["predictions"] if row["event_id"] == "evt_eng1_2223_003")

    assert first_eval["home_team_pre_match_venue_matches"] == 1
    assert first_eval["away_team_pre_match_venue_matches"] == 0
    assert first_eval["home_team_pre_match_competition_matches"] == 2
    assert first_eval["away_team_pre_match_competition_matches"] == 0
    assert newcomer["home_team_pre_match_venue_matches"] == 0
    assert newcomer["home_team_pre_match_competition_matches"] == 0


def test_corners_walkforward_rejects_duplicate_event_ids(monkeypatch, tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    original_loader = generic_gap_walkforward._load_historical_matches

    def _duplicate_loader(**kwargs):
        rows = original_loader(**kwargs)
        return list(rows) + [rows[0]]

    monkeypatch.setattr(generic_gap_walkforward, "_load_historical_matches", _duplicate_loader)

    with pytest.raises(ValueError, match="duplicate GAP observation"):
        _run(research_db)


def test_corners_walkforward_missing_input_fails_loudly(monkeypatch, tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    def _fake_readiness_report(*, research_db_path):
        return {
            "rows": [
                {
                    "competition_id": "ENG1",
                    "competition_name": "Premier League",
                    "division_code": "E0",
                    "season_code": "2122",
                    "scope_group": "league",
                    "scope_status": "LOADED",
                    "shots_ready": "COMPLETE",
                    "shots_on_target_ready": "COMPLETE",
                    "corners_ready": "COMPLETE",
                }
            ]
        }

    original_loader = generic_gap_walkforward._load_historical_matches

    def _loader_with_missing(**kwargs):
        rows = list(original_loader(**kwargs))
        first = rows[0]
        rows[0] = first.__class__(
            event_id=first.event_id,
            competition_id=first.competition_id,
            competition_name=first.competition_name,
            season_code=first.season_code,
            division_code=first.division_code,
            match_date=first.match_date,
            kickoff_time=first.kickoff_time,
            home_team_id=first.home_team_id,
            away_team_id=first.away_team_id,
            statistic=first.statistic,
            home_value=None,
            away_value=first.away_value,
        )
        return rows

    monkeypatch.setattr(generic_gap_walkforward, "generate_gap_data_readiness_report", _fake_readiness_report)
    monkeypatch.setattr(generic_gap_walkforward, "_load_historical_matches", _loader_with_missing)

    with pytest.raises(ValueError, match="missing statistic values"):
        _run(research_db)


def test_corners_gap_walkforward_rejects_goals_statistic(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    with pytest.raises(ValueError, match="unsupported GAP walk-forward statistic"):
        generic_gap_walkforward.run_gap_walkforward_experiment(
            research_db_path=research_db,
            statistic="goals",
        )


def test_corners_walkforward_persists_as_of_and_model_version(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _base_rows())

    result = _run(research_db)
    first_row = result["predictions"][0]

    assert "as_of" in first_row
    assert first_row["as_of"] <= f"{first_row['match_date']}T{first_row['kickoff_time'] or '00:00'}:00"
    assert first_row["model_version"] == "football_gap_corners_reference_v1"
