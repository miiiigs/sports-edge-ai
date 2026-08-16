from sports_ai.agents.football.poisson_1x2 import (
    estimate_expected_goals,
    fit_poisson_1x2_from_history,
    poisson_1x2_probabilities,
)
from sports_ai.agents.football.poisson_1x2_v2 import (
    estimate_expected_goals_recency_weighted,
    fit_poisson_1x2_v2_from_history,
)
from sports_ai.agents.football.poisson_1x2_v3 import (
    estimate_expected_goals_opponent_adjusted,
    fit_poisson_1x2_v3_from_history,
)
import sports_ai.agents.football.poisson_1x2_v3 as poisson_1x2_v3_module
from sports_ai.agents.football.poisson_1x2_v4 import (
    dixon_coles_1x2_probabilities,
    dixon_coles_score_matrix,
    fit_poisson_1x2_v4_from_history,
)
from sports_ai.experiments.football_poisson_backtest import (
    FootballPoissonBacktestConfig,
    run_football_poisson_backtest,
)
from sports_ai.experiments.football_track_b_model_selection import (
    FootballTrackBDevelopmentConfig,
    run_track_b_model_selection,
)
from sports_ai.experiments.football_track_b_finalization import (
    FootballTrackBFinalizationConfig,
    run_track_b_finalization,
)
from sports_ai.experiments.football_decision_policy_validation import (
    FootballDecisionPolicyValidationConfig,
    run_football_decision_policy_validation,
)
from sports_ai.storage.football_research_store import connect as connect_research, ensure_schema
import json


def _sample_history() -> list[dict]:
    rows = []
    for idx in range(12):
        rows.append(
            {
                "event_id": f"evt_h_{idx}",
                "home_team": "Alpha",
                "away_team": "Beta",
                "home_goals": 2,
                "away_goals": 1,
            }
        )
        rows.append(
            {
                "event_id": f"evt_a_{idx}",
                "home_team": "Gamma",
                "away_team": "Delta",
                "home_goals": 1,
                "away_goals": 1,
            }
        )
        rows.append(
            {
                "event_id": f"evt_mix_{idx}",
                "home_team": "Alpha",
                "away_team": "Delta",
                "home_goals": 3,
                "away_goals": 1,
            }
        )
        rows.append(
            {
                "event_id": f"evt_mix2_{idx}",
                "home_team": "Gamma",
                "away_team": "Beta",
                "home_goals": 1,
                "away_goals": 0,
            }
        )
    return rows


def test_poisson_1x2_probabilities_sum_to_one():
    probs = poisson_1x2_probabilities(1.6, 1.1)
    assert abs(sum(probs.values()) - 1.0) < 1e-9
    assert probs["home"] > probs["away"]


def test_estimate_expected_goals_requires_history_and_team_matches():
    assert estimate_expected_goals([], home_team="Alpha", away_team="Beta") is None
    estimate = estimate_expected_goals(_sample_history(), home_team="Alpha", away_team="Beta")
    assert estimate is not None
    assert estimate["expected_home_goals"] > estimate["expected_away_goals"]


def test_fit_poisson_1x2_from_history_returns_prediction():
    prediction = fit_poisson_1x2_from_history(
        _sample_history(),
        event_id="evt_target",
        home_team="Alpha",
        away_team="Beta",
    )
    assert prediction is not None
    assert prediction["market"] == "1X2"
    assert prediction["probabilities"]["home"] > prediction["probabilities"]["away"]
    assert 0 <= prediction["uncertainty"] <= 1


def test_fit_poisson_1x2_v2_from_history_returns_prediction():
    prediction = fit_poisson_1x2_v2_from_history(
        _sample_history(),
        event_id="evt_target_v2",
        home_team="Alpha",
        away_team="Beta",
    )
    assert prediction is not None
    assert prediction["market"] == "1X2"
    assert prediction["probabilities"]["home"] > prediction["probabilities"]["away"]
    assert prediction["details"]["half_life_matches"] == 18.0


def test_estimate_expected_goals_recency_weighted_requires_positive_half_life():
    try:
        estimate_expected_goals_recency_weighted(
            _sample_history(),
            home_team="Alpha",
            away_team="Beta",
            half_life_matches=0,
        )
    except ValueError as exc:
        assert "half_life_matches" in str(exc)
    else:
        raise AssertionError("expected ValueError for non-positive half-life")


def test_fit_poisson_1x2_v3_from_history_returns_prediction():
    prediction = fit_poisson_1x2_v3_from_history(
        _sample_history(),
        event_id="evt_target_v3",
        home_team="Alpha",
        away_team="Beta",
        min_team_matches=2,
    )
    assert prediction is not None
    assert prediction["market"] == "1X2"
    assert prediction["probabilities"]["home"] > prediction["probabilities"]["away"]
    assert "strength_regularization" in prediction["details"]


def test_estimate_expected_goals_opponent_adjusted_can_capture_trace():
    estimate = estimate_expected_goals_opponent_adjusted(
        _sample_history(),
        home_team="Alpha",
        away_team="Beta",
        min_team_matches=2,
        trajectory_checkpoints=(1, 5, 10),
    )

    assert estimate is not None
    trace = estimate.get("optimization_trace")
    assert trace is not None
    assert [item["iteration"] for item in trace] == [1, 5, 10]
    assert all("objective" in item for item in trace)
    assert all("prob_home" in item for item in trace)


def test_estimate_expected_goals_opponent_adjusted_requires_positive_regularization():
    try:
        estimate_expected_goals_opponent_adjusted(
            _sample_history(),
            home_team="Alpha",
            away_team="Beta",
            min_team_matches=2,
            strength_regularization=0,
        )
    except ValueError as exc:
        assert "strength_regularization" in str(exc)
    else:
        raise AssertionError("expected ValueError for non-positive regularization")


def test_fit_poisson_1x2_v3_downgrades_data_quality_when_not_converged(monkeypatch):
    monkeypatch.setattr(
        poisson_1x2_v3_module,
        "estimate_expected_goals_opponent_adjusted",
        lambda *args, **kwargs: {
            "expected_home_goals": 1.4,
            "expected_away_goals": 1.0,
            "league_home_avg": 1.3,
            "league_away_avg": 1.1,
            "home_team_history_n": 40,
            "away_team_history_n": 40,
            "home_attack": 0.1,
            "home_defense": -0.1,
            "away_attack": -0.05,
            "away_defense": 0.05,
            "strength_regularization": 0.03,
            "learning_rate": 0.6,
            "max_iterations": 250,
            "converged": False,
            "last_max_update": 0.00026,
        },
    )

    prediction = fit_poisson_1x2_v3_from_history(
        _sample_history(),
        event_id="evt_target_v3_non_converged",
        home_team="Alpha",
        away_team="Beta",
        min_team_matches=2,
    )

    assert prediction is not None
    assert prediction["details"]["converged"] is False
    assert prediction["data_quality"] < 0.90


def test_fit_poisson_1x2_v3_rewards_converged_fit_more_than_non_converged(monkeypatch):
    monkeypatch.setattr(
        poisson_1x2_v3_module,
        "estimate_expected_goals_opponent_adjusted",
        lambda *args, **kwargs: {
            "expected_home_goals": 1.4,
            "expected_away_goals": 1.0,
            "league_home_avg": 1.3,
            "league_away_avg": 1.1,
            "home_team_history_n": 40,
            "away_team_history_n": 40,
            "home_attack": 0.1,
            "home_defense": -0.1,
            "away_attack": -0.05,
            "away_defense": 0.05,
            "strength_regularization": 0.03,
            "learning_rate": 0.6,
            "max_iterations": 250,
            "converged": True,
            "last_max_update": 0.000004,
        },
    )

    prediction = fit_poisson_1x2_v3_from_history(
        _sample_history(),
        event_id="evt_target_v3_converged",
        home_team="Alpha",
        away_team="Beta",
        min_team_matches=2,
    )

    assert prediction is not None
    assert prediction["details"]["converged"] is True
    assert prediction["data_quality"] <= 0.94
    assert prediction["data_quality"] > 0.82


def test_dixon_coles_probabilities_sum_to_one():
    probs = dixon_coles_1x2_probabilities(1.4, 1.0, rho=-0.1)
    assert abs(sum(probs.values()) - 1.0) < 1e-9


def test_dixon_coles_score_matrix_reduces_to_poisson_at_zero_rho():
    plain = poisson_1x2_probabilities(1.4, 1.0)
    corrected = dixon_coles_1x2_probabilities(1.4, 1.0, rho=0.0)
    for key in ("home", "draw", "away"):
        assert abs(plain[key] - corrected[key]) < 1e-9
    matrix = dixon_coles_score_matrix(1.4, 1.0, rho=0.0)
    assert abs(sum(sum(row) for row in matrix) - 1.0) < 1e-9


def test_fit_poisson_1x2_v4_from_history_returns_prediction():
    prediction = fit_poisson_1x2_v4_from_history(
        _sample_history(),
        event_id="evt_target_v4",
        home_team="Alpha",
        away_team="Beta",
        min_team_matches=2,
        strength_regularization=0.03,
        dixon_coles_rho=-0.1,
    )
    assert prediction is not None
    assert prediction["market"] == "1X2"
    assert prediction["probabilities"]["home"] > prediction["probabilities"]["away"]
    assert prediction["details"]["dixon_coles_rho"] == -0.1


def test_football_poisson_backtest_runs_walk_forward(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    with connect_research(research_db) as conn:
        ensure_schema(conn)
        for idx in range(8):
            conn.execute(
                """
                INSERT INTO football_events (
                    event_id, competition_id, competition_name, season_code, division_code,
                    match_date, kickoff_time, home_team, away_team, home_goals, away_goals,
                    result_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"evt_prior_{idx}",
                    "ENG1",
                    "Premier League",
                    "2425",
                    "E0",
                    f"2024-05-{idx+1:02d}",
                    "15:00",
                    "Alpha" if idx % 2 == 0 else "Gamma",
                    "Beta" if idx % 2 == 0 else "Delta",
                    2,
                    1 if idx % 2 == 0 else 0,
                    "H",
                ),
            )
        fixtures = [
            ("Alpha", "Beta", 2, 0, "H"),
            ("Gamma", "Delta", 1, 1, "D"),
            ("Alpha", "Delta", 3, 1, "H"),
            ("Gamma", "Beta", 1, 0, "H"),
        ]
        for idx in range(32):
            home_team, away_team, home_goals, away_goals, result_code = fixtures[idx % len(fixtures)]
            event_id = f"evt_football_202508{idx+1:02d}_{home_team.lower()}_{away_team.lower()}"
            conn.execute(
                """
                INSERT INTO football_events (
                    event_id, competition_id, competition_name, season_code, division_code,
                    match_date, kickoff_time, home_team, away_team, home_goals, away_goals,
                    result_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    "ENG1",
                    "Premier League",
                    "2526",
                    "E0",
                    f"2025-08-{idx+1:02d}",
                    "15:00",
                    home_team,
                    away_team,
                    home_goals,
                    away_goals,
                    result_code,
                ),
            )
            conn.execute(
                """
                INSERT INTO football_market_quotes (
                    event_id, competition_id, season_code, division_code,
                    source, market_type, bookmaker, selection_prices_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    "ENG1",
                    "2526",
                    "E0",
                    "bet365_closing_1x2",
                    "1X2",
                    "Bet365",
                    json.dumps({"home": 2.2, "draw": 3.3, "away": 3.8}),
                ),
            )

    result = run_football_poisson_backtest(
        FootballPoissonBacktestConfig(
            research_db_path=research_db,
            season_code="2526",
            competition_ids=("ENG1",),
            bookmaker="Bet365",
            min_team_matches=2,
            edge_threshold=0.01,
            allowed_selections=("home",),
        )
    )
    assert result["predicted_count"] > 0
    assert result["history_semantics"]["prior_season_rows_used"] > 0
    assert result["prediction_metrics"]["multiclass_log_loss"] is not None
    assert result["market_benchmark"]["multiclass_log_loss"] is not None
    assert "ENG1" in result["reports"]["by_competition"]
    assert "home" in result["reports"]["by_selection"]
    assert "calibration_gap" in result["reports"]["by_selection"]["home"]
    assert "calibration_buckets_by_selection" in result["model_evaluation"]["reports"]
    assert "policy_evaluation" in result
    assert result["candidate_metrics"]["candidate_count"] >= 0


def test_football_poisson_backtest_supports_history_filters_and_v2(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    with connect_research(research_db) as conn:
        ensure_schema(conn)
        fixtures = [
            ("Alpha", "Beta", 2, 0, "H"),
            ("Gamma", "Delta", 1, 1, "D"),
            ("Alpha", "Delta", 3, 1, "H"),
            ("Gamma", "Beta", 1, 0, "H"),
        ]
        for season_code, month in (("2324", "05"), ("2425", "06"), ("2526", "07")):
            for idx in range(24):
                home_team, away_team, home_goals, away_goals, result_code = fixtures[idx % len(fixtures)]
                event_id = f"evt_{season_code}_{idx}"
                conn.execute(
                    """
                    INSERT INTO football_events (
                        event_id, competition_id, competition_name, season_code, division_code,
                        match_date, kickoff_time, home_team, away_team, home_goals, away_goals,
                        result_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        "ENG1",
                        "Premier League",
                        season_code,
                        "E0",
                        f"20{season_code[:2]}-{month}-{idx+1:02d}",
                        "15:00",
                        home_team,
                        away_team,
                        home_goals,
                        away_goals,
                        result_code,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO football_market_quotes (
                        event_id, competition_id, season_code, division_code,
                        source, market_type, bookmaker, selection_prices_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        "ENG1",
                        season_code,
                        "E0",
                        "bet365_closing_1x2",
                        "1X2",
                        "Bet365",
                        json.dumps({"home": 2.2, "draw": 3.3, "away": 3.8}),
                    ),
                )

    result = run_football_poisson_backtest(
        FootballPoissonBacktestConfig(
            research_db_path=research_db,
            season_code="2526",
            competition_ids=("ENG1",),
            bookmaker="Bet365",
            model_name="poisson_1x2_v2",
            min_team_matches=2,
            edge_threshold=0.01,
            history_season_codes=("2324", "2425"),
        )
    )
    assert result["model_name"] == "poisson_1x2_v2"
    assert result["history_season_codes"] == ["2324", "2425"]
    assert result["predicted_count"] > 0


def test_football_decision_policy_validation_compares_old_and_robust_gates(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    with connect_research(research_db) as conn:
        ensure_schema(conn)
        fixtures = [
            ("Alpha", "Beta", 2, 0, "H"),
            ("Gamma", "Delta", 1, 1, "D"),
            ("Alpha", "Delta", 3, 1, "H"),
            ("Gamma", "Beta", 1, 0, "H"),
        ]
        for season_code, month in (("2425", "06"), ("2526", "07")):
            for idx in range(32):
                home_team, away_team, home_goals, away_goals, result_code = fixtures[idx % len(fixtures)]
                event_id = f"evt_policy_{season_code}_{idx}"
                conn.execute(
                    """
                    INSERT INTO football_events (
                        event_id, competition_id, competition_name, season_code, division_code,
                        match_date, kickoff_time, home_team, away_team, home_goals, away_goals,
                        result_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        "ENG1",
                        "Premier League",
                        season_code,
                        "E0",
                        f"20{season_code[:2]}-{month}-{idx+1:02d}",
                        "15:00",
                        home_team,
                        away_team,
                        home_goals,
                        away_goals,
                        result_code,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO football_market_quotes (
                        event_id, competition_id, season_code, division_code,
                        source, market_type, bookmaker, selection_prices_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        "ENG1",
                        season_code,
                        "E0",
                        "bet365_closing_1x2",
                        "1X2",
                        "Bet365",
                        json.dumps({"home": 2.6, "draw": 3.3, "away": 4.5}),
                    ),
                )

    output_path = tmp_path / "policy_validation.json"
    result = run_football_decision_policy_validation(
        FootballDecisionPolicyValidationConfig(
            research_db_path=research_db,
            season_code="2526",
            competition_ids=("ENG1",),
            model_name="poisson_1x2_v1",
            min_team_matches=2,
            edge_threshold=0.0,
            max_fair_odds=5.0,
            allowed_selections=("home", "draw", "away"),
            min_data_quality=0.99,
            require_model_convergence=False,
            output_path=output_path,
        )
    )

    assert output_path.exists()
    assert result["evaluation_mode"] == "historical_decision_policy_validation"
    assert result["coverage"]["predicted_events"] > 0
    assert result["old_policy"]["summary"]["candidate_count"] > 0
    assert result["new_robust_policy"]["summary"]["candidate_count"] == 0
    assert result["comparison"]["old_candidates_rejected_by_new_gate"] == result["old_policy"]["summary"]["candidate_count"]
    assert "PASS_LOW_DATA_QUALITY" in result["comparison"]["selection_reason_counts"]


def test_track_b_model_selection_runs_validation_only(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    with connect_research(research_db) as conn:
        ensure_schema(conn)
        fixtures = [
            ("Alpha", "Beta", 2, 0, "H"),
            ("Gamma", "Delta", 1, 1, "D"),
            ("Alpha", "Delta", 3, 1, "H"),
            ("Gamma", "Beta", 1, 0, "H"),
        ]
        for season_code, month in (("2122", "08"), ("2223", "09"), ("2324", "10"), ("2425", "11"), ("2526", "12")):
            for idx in range(24):
                home_team, away_team, home_goals, away_goals, result_code = fixtures[idx % len(fixtures)]
                event_id = f"evt_{season_code}_{idx}"
                conn.execute(
                    """
                    INSERT INTO football_events (
                        event_id, competition_id, competition_name, season_code, division_code,
                        match_date, kickoff_time, home_team, away_team, home_goals, away_goals,
                        result_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        "ENG1",
                        "Premier League",
                        season_code,
                        "E0",
                        f"20{season_code[:2]}-{month}-{idx+1:02d}",
                        "15:00",
                        home_team,
                        away_team,
                        home_goals,
                        away_goals,
                        result_code,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO football_market_quotes (
                        event_id, competition_id, season_code, division_code,
                        source, market_type, bookmaker, selection_prices_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        "ENG1",
                        season_code,
                        "E0",
                        "bet365_closing_1x2",
                        "1X2",
                        "Bet365",
                        json.dumps({"home": 2.2, "draw": 3.3, "away": 3.8}),
                    ),
                )

    result = run_track_b_model_selection(
        FootballTrackBDevelopmentConfig(
            research_db_path=research_db,
            competition_ids=("ENG1",),
            train_season_codes=("2122", "2223", "2324"),
            validation_season_code="2425",
            model_names=("poisson_1x2_v1", "poisson_1x2_v2", "poisson_1x2_v3", "poisson_1x2_v4"),
            min_team_matches=2,
            edge_threshold=0.01,
        )
    )
    assert result["evaluation_mode"] == "development"
    assert result["selected_model_name"] in {"poisson_1x2_v1", "poisson_1x2_v2", "poisson_1x2_v3", "poisson_1x2_v4"}
    assert "poisson_1x2_v1" in result["validation"]
    assert "poisson_1x2_v3" in result["full_validation_results"]
    assert "poisson_1x2_v4" in result["full_validation_results"]
    assert "promotion_assessment" in result
    assert "test" not in result


def test_track_b_finalization_runs_single_model_on_test(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    with connect_research(research_db) as conn:
        ensure_schema(conn)
        fixtures = [
            ("Alpha", "Beta", 2, 0, "H"),
            ("Gamma", "Delta", 1, 1, "D"),
            ("Alpha", "Delta", 3, 1, "H"),
            ("Gamma", "Beta", 1, 0, "H"),
        ]
        for season_code, month in (("2122", "08"), ("2223", "09"), ("2324", "10"), ("2425", "11"), ("2526", "12")):
            for idx in range(24):
                home_team, away_team, home_goals, away_goals, result_code = fixtures[idx % len(fixtures)]
                event_id = f"evt_{season_code}_{idx}"
                conn.execute(
                    """
                    INSERT INTO football_events (
                        event_id, competition_id, competition_name, season_code, division_code,
                        match_date, kickoff_time, home_team, away_team, home_goals, away_goals,
                        result_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        "ENG1",
                        "Premier League",
                        season_code,
                        "E0",
                        f"20{season_code[:2]}-{month}-{idx+1:02d}",
                        "15:00",
                        home_team,
                        away_team,
                        home_goals,
                        away_goals,
                        result_code,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO football_market_quotes (
                        event_id, competition_id, season_code, division_code,
                        source, market_type, bookmaker, selection_prices_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        "ENG1",
                        season_code,
                        "E0",
                        "bet365_closing_1x2",
                        "1X2",
                        "Bet365",
                        json.dumps({"home": 2.2, "draw": 3.3, "away": 3.8}),
                    ),
                )

    result = run_track_b_finalization(
        FootballTrackBFinalizationConfig(
            research_db_path=research_db,
            competition_ids=("ENG1",),
            history_season_codes=("2122", "2223", "2324", "2425"),
            test_season_code="2526",
            model_name="poisson_1x2_v1",
            min_team_matches=2,
            edge_threshold=0.01,
        )
    )
    assert result["evaluation_mode"] == "finalization"
    assert result["result"]["model_name"] == "poisson_1x2_v1"
    assert result["result"]["predicted_count"] > 0
