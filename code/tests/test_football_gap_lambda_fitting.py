import sqlite3

from sports_ai.experiments.football_gap_lambda_fitting import (
    DEFAULT_LAMBDA_GRID,
    FITTED_RUN_MODEL_VERSION_BY_STATISTIC,
    build_selected_gap_walkforward_results,
    fit_gap_learning_rates,
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


def _fitting_rows():
    return [
        ("eng1_2122_001", "ENG1", "Premier League", "2122", "E0", "2021-08-01", "12:00", "Alpha", "Beta", "Ref A", 10, 8, 4, 3, 6, 5),
        ("eng1_2122_002", "ENG1", "Premier League", "2122", "E0", "2021-08-08", "12:00", "Gamma", "Alpha", "Ref B", 11, 9, 5, 4, 7, 5),
        ("eng1_2223_001", "ENG1", "Premier League", "2223", "E0", "2022-08-01", "12:00", "Alpha", "Gamma", "Ref C", 12, 7, 5, 2, 6, 4),
        ("eng1_2223_002", "ENG1", "Premier League", "2223", "E0", "2022-08-08", "12:00", "Beta", "Alpha", "Ref D", 9, 10, 3, 4, 4, 6),
        ("eng1_2324_001", "ENG1", "Premier League", "2324", "E0", "2023-08-01", "12:00", "Alpha", "Beta", "Ref E", 13, 8, 6, 3, 7, 5),
        ("eng1_2324_002", "ENG1", "Premier League", "2324", "E0", "2023-08-08", "12:00", "Gamma", "Alpha", "Ref F", 10, 11, 4, 5, 5, 6),
        ("eng1_2425_001", "ENG1", "Premier League", "2425", "E0", "2024-08-01", "12:00", "Alpha", "Gamma", "Ref G", 12, 9, 5, 4, 6, 5),
        ("eng1_2425_002", "ENG1", "Premier League", "2425", "E0", "2024-08-08", "12:00", "Beta", "Alpha", "Ref H", 8, 12, 3, 5, 4, 7),
        ("eng2_2122_001", "ENG2", "Championship", "2122", "E1", "2021-08-02", "15:00", "Xray", "Yankee", "Ref I", 18, 6, 6, 2, 8, 3),
        ("eng2_2122_002", "ENG2", "Championship", "2122", "E1", "2021-08-09", "15:00", "Zulu", "Xray", "Ref J", 7, 16, 2, 5, 3, 7),
        ("eng2_2223_001", "ENG2", "Championship", "2223", "E1", "2022-08-02", "15:00", "Xray", "Zulu", "Ref K", 17, 5, 6, 2, 8, 3),
        ("eng2_2223_002", "ENG2", "Championship", "2223", "E1", "2022-08-09", "15:00", "Yankee", "Xray", "Ref L", 6, 17, 2, 6, 3, 8),
        ("eng2_2324_001", "ENG2", "Championship", "2324", "E1", "2023-08-02", "15:00", "Xray", "Yankee", "Ref M", 19, 7, 7, 3, 9, 4),
        ("eng2_2324_002", "ENG2", "Championship", "2324", "E1", "2023-08-09", "15:00", "Zulu", "Xray", "Ref N", 8, 18, 3, 6, 4, 8),
        ("eng2_2425_001", "ENG2", "Championship", "2425", "E1", "2024-08-02", "15:00", "Xray", "Zulu", "Ref O", 18, 6, 6, 2, 8, 3),
        ("eng2_2425_002", "ENG2", "Championship", "2425", "E1", "2024-08-09", "15:00", "Yankee", "Xray", "Ref P", 7, 18, 2, 6, 3, 8),
    ]


def _fit(db_path):
    return fit_gap_learning_rates(research_db_path=db_path)


def _variants_by_scope(result, *, competition_id, statistic):
    return [
        variant
        for variant in result["variants"]
        if variant["competition_id"] == competition_id and variant["statistic"] == statistic
    ]


def test_lambda_fitting_counts_every_variant(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _fitting_rows())

    result = _fit(research_db)

    assert result["variant_count"] == 2 * 3 * len(DEFAULT_LAMBDA_GRID)
    assert len(result["variants"]) == result["variant_count"]
    assert len(result["selections"]) == 2 * 3


def test_lambda_fitting_is_reproducible(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _fitting_rows())

    first = _fit(research_db)
    second = _fit(research_db)

    assert first["lambda_grid"] == second["lambda_grid"]
    assert first["variant_count"] == second["variant_count"]
    assert first["variants"] == second["variants"]
    assert first["selections"] == second["selections"]


def test_lambda_fitting_records_chronological_walkforward_folds(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _fitting_rows())

    result = _fit(research_db)
    variant = _variants_by_scope(result, competition_id="ENG1", statistic="shots")[0]

    assert variant["validation_period"]["season_codes"] == ["2223", "2324"]
    assert variant["final_holdout_season"] == "2425"
    for fold in variant["folds"]:
        assert fold["training_period"]["end_season"] < fold["validation_period"]["start_season"]
        assert fold["validation_period"]["end_season"] < variant["final_holdout_season"]


def test_lambda_fitting_preserves_competition_and_statistic_isolation(tmp_path):
    base_db = tmp_path / "base.sqlite"
    changed_db = tmp_path / "changed.sqlite"
    _create_store(base_db, _fitting_rows())

    changed_rows = list(_fitting_rows())
    changed_rows[4] = (
        "eng1_2324_001",
        "ENG1",
        "Premier League",
        "2324",
        "E0",
        "2023-08-01",
        "12:00",
        "Alpha",
        "Beta",
        "Ref E",
        30,
        2,
        6,
        3,
        7,
        5,
    )
    _create_store(changed_db, changed_rows)

    base_result = _fit(base_db)
    changed_result = _fit(changed_db)

    assert _variants_by_scope(base_result, competition_id="ENG1", statistic="shots") != _variants_by_scope(
        changed_result,
        competition_id="ENG1",
        statistic="shots",
    )
    assert _variants_by_scope(base_result, competition_id="ENG1", statistic="corners") == _variants_by_scope(
        changed_result,
        competition_id="ENG1",
        statistic="corners",
    )
    assert _variants_by_scope(base_result, competition_id="ENG2", statistic="shots") == _variants_by_scope(
        changed_result,
        competition_id="ENG2",
        statistic="shots",
    )


def test_lambda_fitting_keeps_final_holdout_out_of_selection_variants(tmp_path):
    base_db = tmp_path / "base.sqlite"
    changed_db = tmp_path / "changed.sqlite"
    _create_store(base_db, _fitting_rows())

    changed_rows = list(_fitting_rows())
    changed_rows[6] = (
        "eng1_2425_001",
        "ENG1",
        "Premier League",
        "2425",
        "E0",
        "2024-08-01",
        "12:00",
        "Alpha",
        "Gamma",
        "Ref G",
        35,
        1,
        5,
        4,
        6,
        5,
    )
    _create_store(changed_db, changed_rows)

    base_result = _fit(base_db)
    changed_result = _fit(changed_db)

    assert _variants_by_scope(base_result, competition_id="ENG1", statistic="shots") == _variants_by_scope(
        changed_result,
        competition_id="ENG1",
        statistic="shots",
    )
    base_selection = next(
        row for row in base_result["selections"] if row["competition_id"] == "ENG1" and row["statistic"] == "shots"
    )
    changed_selection = next(
        row for row in changed_result["selections"] if row["competition_id"] == "ENG1" and row["statistic"] == "shots"
    )
    assert base_selection["selected_lambda"] == changed_selection["selected_lambda"]


def test_lambda_fitting_runs_without_any_market_tables(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _fitting_rows())

    result = _fit(research_db)

    assert result["variant_count"] > 0
    assert result["insufficient_scope"] == []


def test_selected_gap_walkforwards_persist_as_of_and_model_version(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _fitting_rows())

    fit_result = _fit(research_db)
    selected_results = build_selected_gap_walkforward_results(
        research_db_path=research_db,
        fit_result=fit_result,
    )

    assert sorted(selected_results) == ["corners", "shots", "shots_on_target"]
    for statistic, result in selected_results.items():
        assert result["predictions"]
        first_row = result["predictions"][0]
        assert "as_of" in first_row
        assert first_row["as_of"] <= f"{first_row['match_date']}T{first_row['kickoff_time'] or '00:00'}:00"
        assert first_row["model_version"] == FITTED_RUN_MODEL_VERSION_BY_STATISTIC[statistic]
