import csv
import json
import sqlite3

from sports_ai.experiments.football_day10_product_outputs import (
    build_day10_product_outputs,
    select_representative_pricing_inputs,
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


def _event_rows():
    return [
        ("evt_eng1_old", "ENG1", "Premier League", "2425", "E0", "2025-04-01", "15:00", "Alpha", "Beta", 1, 0, "H", 0, 0, "D", "Ref A", 10, 9, 4, 3, 9, 11, 5, 4, 1, 2, 0, 0),
        ("evt_eng1_new", "ENG1", "Premier League", "2526", "E0", "2026-05-24", "16:00", "Crystal Palace", "Arsenal", 0, 0, "D", 0, 0, "D", "Ref B", 12, 10, 5, 4, 10, 9, 6, 5, 2, 1, 0, 0),
        ("evt_eng2_new", "ENG2", "Championship", "2526", "E1", "2026-05-03", "12:30", "Bournemouth", "West Brom", 0, 0, "D", 0, 0, "D", "Ref C", 13, 11, 5, 4, 11, 10, 7, 4, 1, 2, 0, 0),
    ]


def _pricing_input(event_id, competition_id, competition_name, season_code, match_date, kickoff_time, expected_total_goals, predicted_total_corners):
    return {
        "fixture_id": event_id,
        "event_id": event_id,
        "competition_id": competition_id,
        "competition_name": competition_name,
        "season_code": season_code,
        "division_code": "E0" if competition_id == "ENG1" else "E1",
        "match_date": match_date,
        "kickoff_time": kickoff_time,
        "kickoff_at": f"{match_date}T{kickoff_time}:00",
        "as_of": f"{match_date}T{kickoff_time}:00",
        "day10_input_contract_version": "football_day10_sanitized_prematch_input_v1",
        "sanitized_input_policy_id": "prematch_only_pricing_input_excludes_outcomes_and_market_data_v1",
        "model_versions": {
            "conversion_model_version": "football_day8_goal_conversion_v1",
            "corner_comparison_version": "football_day8_corner_distribution_comparison_v1",
            "day7_gap_baseline_experiment_version": "football_gap_lambda_fitting_v1",
            "day8_experiment_version": "football_day8_goal_conversion_v1",
            "day9_experiment_version": "football_day9_scoreline_model_v1",
            "gap_model_version_corners": "football_gap_corners_fitted_v1",
            "gap_model_version_shots": "football_gap_shots_fitted_v1",
            "gap_model_version_shots_on_target": "football_gap_shots_on_target_fitted_v1",
            "scoreline_model_version": "football_day9_dixon_coles_scoreline_v1",
        },
        "scoreline_object": {
            "object_id": f"scoreline_{event_id}",
            "event_id": event_id,
            "competition_id": competition_id,
            "kickoff_at": f"{match_date}T{kickoff_time}:00",
            "as_of": f"{match_date}T{kickoff_time}:00",
            "scoreline_model_version": "football_day9_dixon_coles_scoreline_v1",
            "conversion_model_version": "football_day8_goal_conversion_v1",
            "gap_model_version_shots": "football_gap_shots_fitted_v1",
            "gap_model_version_shots_on_target": "football_gap_shots_on_target_fitted_v1",
            "expected_goals_home": expected_total_goals / 2,
            "expected_goals_away": expected_total_goals / 2,
            "expected_total_goals": expected_total_goals,
            "rho": 0.0,
            "xi": 0.0,
            "matrix_max_goals": 2,
            "matrix": [
                [0.10, 0.08, 0.04],
                [0.12, 0.20, 0.06],
                [0.05, 0.18, 0.17],
            ],
            "tail_mass_beyond_max_goals": 0.0,
            "raw_matrix_mass": 1.0,
            "normalization_factor": 1.0,
            "normalization_error": 0.0,
            "full_distribution_mass_estimate": 1.0,
            "full_distribution_mass_estimate_error": 0.0,
            "marginal_home_mean": expected_total_goals / 2,
            "marginal_away_mean": expected_total_goals / 2,
            "interface_policy_id": "persisted_day9_scoreline_matrix_is_canonical_goals_object_v1",
        },
        "corner_object": {
            "object_id": f"corner_{event_id}",
            "event_id": event_id,
            "competition_id": competition_id,
            "kickoff_at": f"{match_date}T{kickoff_time}:00",
            "as_of": f"{match_date}T{kickoff_time}:00",
            "gap_model_version_corners": "football_gap_corners_fitted_v1",
            "conversion_model_version": "football_day8_goal_conversion_v1",
            "selected_corner_distribution": "poisson",
            "predicted_home_corners": predicted_total_corners / 2,
            "predicted_away_corners": predicted_total_corners / 2,
            "predicted_total_corners": predicted_total_corners,
            "dispersion": None,
            "interface_policy_id": "persisted_day8_corner_distribution_is_canonical_v1",
        },
    }


def _market_row(event_id, competition_id, competition_name, season_code, match_date, kickoff_time, market_family, selection, fair_price, *, line=None):
    return {
        "fixture_id": event_id,
        "event_id": event_id,
        "competition_id": competition_id,
        "competition_name": competition_name,
        "season_code": season_code,
        "division_code": "E0" if competition_id == "ENG1" else "E1",
        "match_date": match_date,
        "kickoff_time": kickoff_time,
        "kickoff_at": f"{match_date}T{kickoff_time}:00",
        "as_of": f"{match_date}T{kickoff_time}:00",
        "market_family": market_family,
        "line": line,
        "selection": selection,
        "source_scoreline_object_id": None if market_family == "corner_total" else f"scoreline_{event_id}",
        "source_corner_object_id": f"corner_{event_id}" if market_family == "corner_total" else None,
        "day10_input_contract_version": "football_day10_sanitized_prematch_input_v1",
        "pricing_model_version": "football_day10_market_pricing_v1",
        "settlement_engine_version": "football_day10_settlement_engine_v1",
        "output_schema_version": "football_day10_market_price_record_v1",
        "settlement_pricing_policy_id": "zero_ev_with_partial_and_push_settlement_v1",
        "line_catalog_version": "football_day10_engineering_verification_lines_v1",
        "model_versions": {
            "conversion_model_version": "football_day8_goal_conversion_v1",
            "corner_comparison_version": "football_day8_corner_distribution_comparison_v1",
            "day7_gap_baseline_experiment_version": "football_gap_lambda_fitting_v1",
            "day8_experiment_version": "football_day8_goal_conversion_v1",
            "day9_experiment_version": "football_day9_scoreline_model_v1",
            "gap_model_version_corners": "football_gap_corners_fitted_v1",
            "gap_model_version_shots": "football_gap_shots_fitted_v1",
            "gap_model_version_shots_on_target": "football_gap_shots_on_target_fitted_v1",
            "scoreline_model_version": "football_day9_dixon_coles_scoreline_v1",
        },
        "settlement_probabilities": {
            "full_win_probability": 0.5,
            "half_win_probability": 0.0,
            "push_probability": 0.0,
            "half_loss_probability": 0.0,
            "full_loss_probability": 0.5,
        },
        "effective_win_stake_probability": 0.5,
        "effective_loss_stake_probability": 0.5,
        "fair_decimal_price": fair_price,
        "component_lines": [] if line is None else [line],
        "numerical_metadata": {},
    }


def test_select_representative_pricing_inputs_uses_latest_per_competition():
    inputs = [
        _pricing_input("evt_eng1_old", "ENG1", "Premier League", "2425", "2025-04-01", "15:00", 2.4, 9.2),
        _pricing_input("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", 2.6, 9.8),
        _pricing_input("evt_eng2_new", "ENG2", "Championship", "2526", "2026-05-03", "12:30", 2.2, 10.4),
    ]

    selected = select_representative_pricing_inputs(inputs)

    assert [row["event_id"] for row in selected] == ["evt_eng1_new", "evt_eng2_new"]


def test_build_day10_product_outputs_creates_report_csv_and_latest(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _create_store(research_db, _event_rows())

    manifest_path = tmp_path / "manifest.json"
    pricing_inputs_path = tmp_path / "pricing_inputs.jsonl"
    market_prices_path = tmp_path / "market_prices.jsonl"
    output_root = tmp_path / "product_outputs"

    manifest_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-08-18T11:34:23+00:00",
                "pricing_model_version": "football_day10_market_pricing_v1",
                "settlement_engine_version": "football_day10_settlement_engine_v1",
                "output_schema_version": "football_day10_market_price_record_v1",
                "pricing_input_count": 2,
                "market_price_count": 24,
                "sanitized_input_policy_id": "prematch_only_pricing_input_excludes_outcomes_and_market_data_v1",
                "day9_to_day10_interface_policy_id": "persisted_day9_scoreline_matrix_is_canonical_goals_object_v1",
                "day8_to_day10_corner_interface_policy_id": "persisted_day8_corner_distribution_is_canonical_v1",
            }
        ),
        encoding="utf-8",
    )
    pricing_inputs = [
        _pricing_input("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", 2.6, 9.8),
        _pricing_input("evt_eng2_new", "ENG2", "Championship", "2526", "2026-05-03", "12:30", 2.2, 10.4),
    ]
    pricing_inputs_path.write_text(
        "\n".join(json.dumps(row) for row in pricing_inputs) + "\n",
        encoding="utf-8",
    )
    market_rows = [
        _market_row("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", "1x2", "HOME", 2.10),
        _market_row("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", "1x2", "DRAW", 3.40),
        _market_row("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", "1x2", "AWAY", 3.60),
        _market_row("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", "btts", "YES", 1.80),
        _market_row("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", "btts", "NO", 2.05),
        _market_row("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", "goals_total", "OVER", 2.01, line=2.0),
        _market_row("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", "goals_total", "UNDER", 1.99, line=2.0),
        _market_row("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", "goals_total", "OVER", 2.15, line=2.5),
        _market_row("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", "goals_total", "UNDER", 1.87, line=2.5),
        _market_row("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", "goals_total", "OVER", 2.42, line=3.0),
        _market_row("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", "goals_total", "UNDER", 1.70, line=3.0),
        _market_row("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", "asian_handicap", "HOME", 1.95, line=0.0),
        _market_row("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", "asian_handicap", "HOME", 2.12, line=-0.25),
        _market_row("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", "asian_handicap", "HOME", 2.36, line=-0.5),
        _market_row("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", "corner_total", "OVER", 1.92, line=9.0),
        _market_row("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", "corner_total", "UNDER", 2.03, line=9.0),
        _market_row("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", "corner_total", "OVER", 2.07, line=9.5),
        _market_row("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", "corner_total", "UNDER", 1.89, line=9.5),
        _market_row("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", "corner_total", "OVER", 2.24, line=10.0),
        _market_row("evt_eng1_new", "ENG1", "Premier League", "2526", "2026-05-24", "16:00", "corner_total", "UNDER", 1.77, line=10.0),
        _market_row("evt_eng2_new", "ENG2", "Championship", "2526", "2026-05-03", "12:30", "1x2", "HOME", 2.30),
        _market_row("evt_eng2_new", "ENG2", "Championship", "2526", "2026-05-03", "12:30", "1x2", "DRAW", 3.10),
        _market_row("evt_eng2_new", "ENG2", "Championship", "2526", "2026-05-03", "12:30", "1x2", "AWAY", 3.30),
        _market_row("evt_eng2_new", "ENG2", "Championship", "2526", "2026-05-03", "12:30", "btts", "YES", 1.95),
    ]
    market_prices_path.write_text(
        "\n".join(json.dumps(row) for row in market_rows) + "\n",
        encoding="utf-8",
    )

    result = build_day10_product_outputs(
        research_db_path=research_db,
        day10_manifest_path=manifest_path,
        pricing_inputs_path=pricing_inputs_path,
        market_prices_path=market_prices_path,
        output_root=output_root,
    )

    report_text = (output_root / "football" / "day10" / "FOOTBALL_DAY10_MODEL_PRICING_REPORT.md").read_text(
        encoding="utf-8"
    )
    latest_text = (output_root / "football" / "latest" / "FOOTBALL_MODEL_PRICES.md").read_text(encoding="utf-8")
    csv_rows = list(csv.DictReader((output_root / "football" / "day10" / "football_day10_model_prices.csv").open(encoding="utf-8")))
    sample_csv_rows = list(csv.DictReader((output_root / "football" / "day10" / "football_day10_model_prices_sample.csv").open(encoding="utf-8")))
    day10_jsonl_text = (output_root / "football" / "day10" / "football_day10_model_prices.jsonl").read_text(encoding="utf-8")
    latest_jsonl_text = (output_root / "football" / "latest" / "football_model_prices.jsonl").read_text(encoding="utf-8")
    manifest = json.loads((output_root / "football" / "day10" / "football_day10_product_manifest.json").read_text(encoding="utf-8"))
    latest_manifest = json.loads((output_root / "football" / "latest" / "football_pipeline_manifest.json").read_text(encoding="utf-8"))

    assert result["report_path"].endswith("FOOTBALL_DAY10_MODEL_PRICING_REPORT.md")
    assert "Crystal Palace vs Arsenal" in report_text
    assert "MODEL FAIR PRICE" in report_text
    assert "not betting recommendations" in report_text.lower()
    assert latest_text == report_text
    assert len(csv_rows) == 24
    assert len(sample_csv_rows) == 24
    assert day10_jsonl_text == market_prices_path.read_text(encoding="utf-8")
    assert latest_jsonl_text == day10_jsonl_text
    assert csv_rows[0]["home_team"] == "Crystal Palace"
    assert sample_csv_rows[0]["home_team"] == "Crystal Palace"
    assert manifest["representative_fixture_ids"] == ["evt_eng1_new", "evt_eng2_new"]
    assert manifest["day10_outputs"]["sample_csv_path"].endswith("football_day10_model_prices_sample.csv")
    assert manifest["latest_outputs"]["sample_csv_path"].endswith("football_model_prices_sample.csv")
    assert manifest["latest_outputs"]["jsonl_path"].endswith("football_model_prices.jsonl")
    assert latest_manifest["day10_outputs"]["jsonl_path"].endswith("football_day10_model_prices.jsonl")
