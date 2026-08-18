import json
import sqlite3

import pytest

from sports_ai.experiments.football_day10_market_pricing import (
    build_corner_total_distribution,
    build_day10_pricing_input,
    derive_1x2_market,
    derive_btts_market,
    fair_decimal_price_from_settlement,
    price_asian_handicap_selection,
    price_corner_total_selection,
    price_goals_total_selection,
    run_day10_market_pricing_experiment,
    write_day10_artifacts,
)
from sports_ai.experiments.football_day9_scoreline_model import run_day9_scoreline_experiment


def _single_state_matrix(home_goals: int, away_goals: int, *, max_goals: int = 2) -> list[list[float]]:
    matrix = [[0.0 for _ in range(max_goals + 1)] for _ in range(max_goals + 1)]
    matrix[home_goals][away_goals] = 1.0
    return matrix


def _base_day9_row(*, matrix=None, max_goals=None):
    scoreline_matrix = matrix or [
        [0.10, 0.08, 0.04],
        [0.12, 0.20, 0.06],
        [0.05, 0.18, 0.17],
    ]
    resolved_max_goals = max_goals if max_goals is not None else (len(scoreline_matrix) - 1)
    return {
        "event_id": "evt_day10_001",
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "season_code": "2526",
        "division_code": "E0",
        "match_date": "2026-08-17",
        "kickoff_time": "15:00",
        "as_of": "2026-08-17T15:00:00",
        "home_team_id": "alpha",
        "away_team_id": "beta",
        "gap_model_version_shots": "football_gap_shots_v1",
        "gap_model_version_shots_on_target": "football_gap_sot_v1",
        "gap_model_version_corners": "football_gap_corners_v1",
        "conversion_model_version": "football_day8_goal_conversion_v1",
        "scoreline_model_version": "football_day9_dixon_coles_scoreline_v1",
        "expected_goals_home": 1.45,
        "expected_goals_away": 1.10,
        "expected_total_goals": 2.55,
        "rho": -0.05,
        "xi": 0.0,
        "selected_corner_distribution": "negative_binomial",
        "corner_distribution_parameters": {"dispersion": 8.5},
        "predicted_home_corners": 5.7,
        "predicted_away_corners": 4.9,
        "scoreline_matrix_max_goals": resolved_max_goals,
        "scoreline_matrix": scoreline_matrix,
        "truncated_tail_mass_beyond_10": 0.00001,
        "scoreline_matrix_raw_mass": 0.99999,
        "scoreline_matrix_normalization_factor": 1.00001,
        "scoreline_matrix_normalization_error": 0.0,
        "full_distribution_mass_estimate": 1.0,
        "full_distribution_mass_estimate_error": 0.0,
        "matrix_marginal_home_mean": 1.30,
        "matrix_marginal_away_mean": 1.05,
        "actual_home_goals": 3,
        "actual_away_goals": 2,
        "actual_total_goals": 5,
        "actual_total_corners": 13,
        "closing_home_odds": 1.82,
        "roi": 0.14,
        "edge_pct": 0.03,
    }


def _pricing_input(*, matrix=None, max_goals=None):
    return build_day10_pricing_input(_base_day9_row(matrix=matrix, max_goals=max_goals))


def _transpose_pricing_input(pricing_input):
    transposed = json.loads(json.dumps(pricing_input))
    matrix = transposed["scoreline_object"]["matrix"]
    transposed["scoreline_object"]["matrix"] = [
        [matrix[away_goals][home_goals] for away_goals in range(len(matrix))]
        for home_goals in range(len(matrix))
    ]
    transposed["event_id"] = f"{transposed['event_id']}_transpose"
    transposed["fixture_id"] = transposed["event_id"]
    transposed["scoreline_object"]["event_id"] = transposed["event_id"]
    return transposed


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


def _historical_rows():
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


def test_day10_sanitized_input_excludes_outcomes_and_market_data():
    base_row = _base_day9_row()
    changed_row = _base_day9_row()
    changed_row["actual_home_goals"] = 9
    changed_row["actual_away_goals"] = 8
    changed_row["closing_home_odds"] = 3.40
    changed_row["roi"] = -0.50
    changed_row["edge_pct"] = 0.99

    base_input = build_day10_pricing_input(base_row)
    changed_input = build_day10_pricing_input(changed_row)

    assert base_input == changed_input
    serialized = json.dumps(base_input, sort_keys=True)
    assert "actual_home_goals" not in serialized
    assert "actual_away_goals" not in serialized
    assert "closing_home_odds" not in serialized
    assert "roi" not in serialized
    assert "edge_pct" not in serialized


def test_day10_1x2_uses_explicit_score_comparisons_and_conserves_mass():
    records = derive_1x2_market(_pricing_input())
    probabilities = {row["selection"]: row["settlement_probabilities"]["full_win_probability"] for row in records}

    assert probabilities["HOME"] == pytest.approx(0.35)
    assert probabilities["DRAW"] == pytest.approx(0.47)
    assert probabilities["AWAY"] == pytest.approx(0.18)
    assert sum(probabilities.values()) == pytest.approx(1.0)


def test_day10_btts_is_complementary():
    records = derive_btts_market(_pricing_input())
    probabilities = {row["selection"]: row["settlement_probabilities"]["full_win_probability"] for row in records}

    assert probabilities["YES"] == pytest.approx(0.61)
    assert probabilities["NO"] == pytest.approx(0.39)
    assert probabilities["YES"] + probabilities["NO"] == pytest.approx(1.0)


def test_day10_integer_goals_total_handles_pushes_correctly():
    record = price_goals_total_selection(_pricing_input(), line=2.0, selection="OVER")

    assert record["settlement_probabilities"]["full_win_probability"] == pytest.approx(0.41)
    assert record["settlement_probabilities"]["push_probability"] == pytest.approx(0.29)
    assert record["settlement_probabilities"]["full_loss_probability"] == pytest.approx(0.30)
    assert record["component_lines"] == [2.0]
    assert record["fair_decimal_price"] == pytest.approx(1.0 + (0.30 / 0.41))


def test_day10_quarter_goals_total_splits_stake_into_adjacent_lines():
    record = price_goals_total_selection(_pricing_input(), line=2.25, selection="OVER")

    assert record["component_lines"] == [2.0, 2.5]
    assert record["settlement_probabilities"]["full_win_probability"] == pytest.approx(0.41)
    assert record["settlement_probabilities"]["half_loss_probability"] == pytest.approx(0.29)
    assert record["settlement_probabilities"]["full_loss_probability"] == pytest.approx(0.30)
    assert record["settlement_probabilities"]["push_probability"] == pytest.approx(0.0)
    assert record["fair_decimal_price"] == pytest.approx(1.0 + ((0.30 + (0.5 * 0.29)) / 0.41))


def test_day10_fair_price_formula_reduces_to_binary_and_push_only_cases():
    assert fair_decimal_price_from_settlement(full_win_probability=0.40, full_loss_probability=0.60) == pytest.approx(
        1.0 / 0.40
    )
    assert fair_decimal_price_from_settlement(
        full_win_probability=0.30,
        push_probability=0.20,
        full_loss_probability=0.50,
    ) == pytest.approx(1.0 + (0.50 / 0.30))
    assert fair_decimal_price_from_settlement(
        full_win_probability=0.25,
        half_win_probability=0.10,
        push_probability=0.05,
        half_loss_probability=0.20,
        full_loss_probability=0.40,
    ) == pytest.approx(1.0 + ((0.40 + (0.5 * 0.20)) / (0.25 + (0.5 * 0.10))))


def test_day10_asian_handicap_matches_1x2_invariants():
    pricing_input = _pricing_input()
    home_1x2, draw_1x2, away_1x2 = derive_1x2_market(pricing_input)
    home_minus_half = price_asian_handicap_selection(pricing_input, line=-0.5, selection="HOME")
    away_minus_half = price_asian_handicap_selection(pricing_input, line=0.5, selection="AWAY")
    home_zero = price_asian_handicap_selection(pricing_input, line=0.0, selection="HOME")

    assert home_minus_half["settlement_probabilities"]["full_win_probability"] == pytest.approx(
        home_1x2["settlement_probabilities"]["full_win_probability"]
    )
    assert home_minus_half["fair_decimal_price"] == pytest.approx(home_1x2["fair_decimal_price"])
    assert away_minus_half["settlement_probabilities"]["full_win_probability"] == pytest.approx(
        away_1x2["settlement_probabilities"]["full_win_probability"]
    )
    assert away_minus_half["fair_decimal_price"] == pytest.approx(away_1x2["fair_decimal_price"])
    assert home_zero["settlement_probabilities"]["full_win_probability"] == pytest.approx(
        home_1x2["settlement_probabilities"]["full_win_probability"]
    )
    assert home_zero["settlement_probabilities"]["push_probability"] == pytest.approx(
        draw_1x2["settlement_probabilities"]["full_win_probability"]
    )
    assert home_zero["settlement_probabilities"]["full_loss_probability"] == pytest.approx(
        away_1x2["settlement_probabilities"]["full_win_probability"]
    )


@pytest.mark.parametrize(
    ("home_goals", "away_goals", "line", "selection", "expected_bucket"),
    [
        (1, 1, 0.0, "HOME", "push_probability"),
        (1, 1, 0.25, "HOME", "half_win_probability"),
        (1, 1, -0.25, "HOME", "half_loss_probability"),
        (1, 0, -0.5, "HOME", "full_win_probability"),
        (1, 0, -1.0, "HOME", "push_probability"),
        (0, 1, 0.75, "AWAY", "half_win_probability"),
    ],
)
def test_day10_asian_handicap_settlement_examples(home_goals, away_goals, line, selection, expected_bucket):
    pricing_input = _pricing_input(matrix=_single_state_matrix(home_goals, away_goals), max_goals=2)

    record = price_asian_handicap_selection(pricing_input, line=line, selection=selection)

    assert record["settlement_probabilities"][expected_bucket] == pytest.approx(1.0)


def test_day10_asian_handicap_home_away_symmetry_holds_under_team_swap():
    pricing_input = _pricing_input()
    swapped_input = _transpose_pricing_input(pricing_input)

    original_away = price_asian_handicap_selection(pricing_input, line=0.25, selection="AWAY")
    swapped_home = price_asian_handicap_selection(swapped_input, line=-0.25, selection="HOME")

    assert original_away["settlement_probabilities"] == pytest.approx(swapped_home["settlement_probabilities"])
    assert original_away["fair_decimal_price"] == pytest.approx(swapped_home["fair_decimal_price"])


def test_day10_invalid_lines_are_rejected():
    with pytest.raises(ValueError):
        price_goals_total_selection(_pricing_input(), line=2.3, selection="OVER")
    with pytest.raises(ValueError):
        price_asian_handicap_selection(_pricing_input(), line="bad", selection="HOME")
    with pytest.raises(ValueError):
        price_corner_total_selection(_pricing_input(), line=9.1, selection="UNDER")


def test_day10_corner_distribution_tracks_tail_mass_and_support():
    distribution = build_corner_total_distribution(
        mean_value=12.4,
        distribution_name="negative_binomial",
        dispersion=8.0,
    )
    pricing_record = price_corner_total_selection(_pricing_input(), line=9.5, selection="OVER")

    assert distribution["residual_tail_mass"] <= distribution["tail_tolerance"]
    assert distribution["normalization_error"] == pytest.approx(0.0, abs=1e-9)
    assert distribution["support_max"] > 10
    assert pricing_record["numerical_metadata"]["corner_residual_tail_mass"] <= pricing_record["numerical_metadata"][
        "corner_tail_tolerance"
    ]


def test_day10_rejects_as_of_after_kickoff():
    late_row = _base_day9_row()
    late_row["as_of"] = "2026-08-17T15:01:00"

    with pytest.raises(ValueError):
        build_day10_pricing_input(late_row)


def test_day10_is_deterministic_and_repeatable(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _historical_rows())

    first = run_day10_market_pricing_experiment(research_db_path=research_db)
    second = run_day10_market_pricing_experiment(research_db_path=research_db)

    assert first["pricing_inputs"] == second["pricing_inputs"]
    assert first["market_prices"] == second["market_prices"]
    first_manifest = dict(first["manifest"])
    second_manifest = dict(second["manifest"])
    first_manifest.pop("generated_at")
    second_manifest.pop("generated_at")
    assert first_manifest == second_manifest


def test_day10_model_versions_and_object_ids_persist_in_artifacts(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _historical_rows())
    result = run_day10_market_pricing_experiment(research_db_path=research_db)

    manifest_path = tmp_path / "manifest.json"
    inputs_path = tmp_path / "pricing_inputs.jsonl"
    prices_path = tmp_path / "market_prices.jsonl"
    markdown_path = tmp_path / "summary.md"
    artifact_paths = write_day10_artifacts(
        result,
        manifest_path=manifest_path,
        pricing_inputs_jsonl_path=inputs_path,
        market_prices_jsonl_path=prices_path,
        markdown_path=markdown_path,
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pricing_inputs = [
        json.loads(line)
        for line in inputs_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    market_prices = [
        json.loads(line)
        for line in prices_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert artifact_paths["manifest_path"] == str(manifest_path)
    assert manifest["pricing_model_version"] == "football_day10_market_pricing_v1"
    assert all(row["scoreline_object"]["object_id"].startswith("scoreline_") for row in pricing_inputs)
    assert all(row["corner_object"]["object_id"].startswith("corner_") for row in pricing_inputs)
    assert all(row["pricing_model_version"] == "football_day10_market_pricing_v1" for row in market_prices)
    assert all(row["output_schema_version"] == "football_day10_market_price_record_v1" for row in market_prices)
    assert all(row["line_catalog_version"] for row in market_prices)
    assert any(row["source_scoreline_object_id"] is not None for row in market_prices)
    assert any(row["source_corner_object_id"] is not None for row in market_prices)


def test_day10_does_not_mutate_day9_outputs(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _historical_rows())

    day9_result = run_day9_scoreline_experiment(research_db_path=research_db)
    day9_snapshot = json.dumps(day9_result, sort_keys=True)
    day10_result = run_day10_market_pricing_experiment(research_db_path=research_db, day9_result=day9_result)

    assert json.dumps(day9_result, sort_keys=True) == day9_snapshot
    assert day10_result["day9_result"]["predictions"] == day9_result["predictions"]
