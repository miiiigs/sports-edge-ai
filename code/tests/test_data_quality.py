from sports_ai.data_quality import (
    explain_v3_data_quality,
    historical_football_data_quality_components,
    historical_football_data_quality_components_v2,
    summarize_float_distribution,
)
from sports_ai.experiments.football_data_quality_audit import (
    FootballDataQualityAuditConfig,
    build_football_data_quality_audit,
)
from sports_ai.experiments.football_v3_optimizer_diagnostic import (
    FootballV3OptimizerDiagnosticConfig,
    run_football_v3_optimizer_diagnostic,
)
from sports_ai.storage.football_research_store import connect as connect_research, ensure_schema
import json


def test_summarize_float_distribution_returns_percentiles():
    result = summarize_float_distribution([0.1, 0.2, 0.3, 0.4, 0.5])

    assert result["n"] == 5
    assert result["min"] == 0.1
    assert result["max"] == 0.5
    assert result["median"] == 0.3


def test_explain_v3_data_quality_identifies_nonconverged_penalty():
    prediction = {
        "data_quality": 0.74,
        "details": {
            "home_team_history_n": 18,
            "away_team_history_n": 16,
            "converged": False,
            "last_max_update": 0.0004,
        },
    }

    result = explain_v3_data_quality(prediction, threshold=0.82)

    assert result["converged"] is False
    assert "MODEL_NOT_CONVERGED" in result["reasons"]
    assert "BELOW_DQ_THRESHOLD" in result["reasons"]
    assert result["last_update_penalty"] > 0


def test_historical_football_data_quality_components_separates_contract_from_live_proxy():
    prediction = {
        "data_quality": 0.74,
        "details": {
            "home_team_history_n": 18,
            "away_team_history_n": 16,
            "converged": False,
            "last_max_update": 0.0004,
        },
    }
    history_rows = [
        {
            "season_code": "2425",
            "home_team": "Alpha",
            "away_team": "Beta",
            "home_goals": 2,
            "away_goals": 1,
        },
        {
            "season_code": "2425",
            "home_team": "Beta",
            "away_team": "Alpha",
            "home_goals": 1,
            "away_goals": 1,
        },
    ] * 20
    event = {
        "event_id": "evt_test",
        "competition_id": "ENG1",
        "season_code": "2526",
        "match_date": "2026-08-15",
        "home_team": "Alpha",
        "away_team": "Beta",
    }
    market_quote = {"home": 2.4, "draw": 3.2, "away": 3.6}

    result = historical_football_data_quality_components(
        prediction=prediction,
        history_rows=history_rows,
        event=event,
        market_quote=market_quote,
    )

    assert result["contract_name"] == "historical_football_point_in_time_dq_v1"
    assert 0 <= result["historical_data_quality"] <= 1
    assert "history_coverage" in result["components"]
    assert result["live_only_fields_not_in_historical_contract"]


def test_historical_football_data_quality_components_v2_separates_sports_and_market_quality():
    prediction = {
        "data_quality": 0.74,
        "uncertainty": 0.96,
        "details": {
            "home_team_history_n": 18,
            "away_team_history_n": 16,
            "converged": False,
            "last_max_update": 0.0004,
        },
    }
    history_rows = [
        {
            "season_code": "2425",
            "match_date": "2025-04-02",
            "home_team": "Alpha",
            "away_team": "Beta",
            "home_goals": 2,
            "away_goals": 1,
        },
        {
            "season_code": "2425",
            "match_date": "2025-04-10",
            "home_team": "Beta",
            "away_team": "Alpha",
            "home_goals": 1,
            "away_goals": 1,
        },
        {
            "season_code": "2324",
            "match_date": "2024-05-05",
            "home_team": "Alpha",
            "away_team": "Gamma",
            "home_goals": 1,
            "away_goals": 0,
        },
        {
            "season_code": "2324",
            "match_date": "2024-05-11",
            "home_team": "Delta",
            "away_team": "Beta",
            "home_goals": 0,
            "away_goals": 1,
        },
    ] * 10
    event = {
        "event_id": "evt_test",
        "competition_id": "ENG1",
        "season_code": "2526",
        "match_date": "2026-08-15",
        "home_team": "Alpha",
        "away_team": "Beta",
    }
    market_quote = {"home": 2.4, "draw": 3.2, "away": 3.6}

    result = historical_football_data_quality_components_v2(
        prediction=prediction,
        history_rows=history_rows,
        event=event,
        market_quote=market_quote,
    )

    assert result["contract_name"] == "historical_football_point_in_time_dq_v2"
    assert 0 <= result["sports_data_quality"] <= 1
    assert 0 <= result["model_fit_quality"] <= 1
    assert 0 <= result["market_data_quality"] <= 1
    assert 0 <= result["decision_data_quality"] <= 1
    assert "sample_depth_score" in result["components"]
    assert "bookmaker_available_score" in result["components"]
    assert "newly_observed_team" in result["flags"]


def test_build_football_data_quality_audit_reports_current_and_historical_distributions(tmp_path):
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
                event_id = f"evt_dq_{season_code}_{idx}"
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

    output_path = tmp_path / "dq_audit.json"
    result = build_football_data_quality_audit(
        FootballDataQualityAuditConfig(
            research_db_path=research_db,
            season_code="2526",
            competition_ids=("ENG1",),
            model_name="poisson_1x2_v3",
            min_team_matches=2,
            edge_threshold=0.0,
            max_fair_odds=5.0,
            output_path=output_path,
        )
    )

    assert output_path.exists()
    assert result["coverage"]["predicted_events"] > 0
    assert result["coverage"]["old_policy_candidate_count"] > 0
    assert result["current_v3_proxy"]["all_predicted"]["n"] == result["coverage"]["predicted_events"]
    assert result["historical_point_in_time_contract_v1"]["all_predicted"]["n"] == result["coverage"]["predicted_events"]
    assert result["historical_point_in_time_contract_v2"]["decision_data_quality_all_predicted"]["n"] == result["coverage"]["predicted_events"]
    assert "by_season_phase_decision_quality_all_predicted" in result["historical_point_in_time_contract_v2"]


def test_run_football_v3_optimizer_diagnostic_reports_convergence_metrics(tmp_path):
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
                event_id = f"evt_opt_{season_code}_{idx}"
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

    output_path = tmp_path / "optimizer_diagnostic.json"
    result = run_football_v3_optimizer_diagnostic(
        FootballV3OptimizerDiagnosticConfig(
            research_db_path=research_db,
            season_code="2526",
            competition_ids=("ENG1",),
            min_team_matches=2,
            output_path=output_path,
        )
    )

    assert output_path.exists()
    assert result["coverage"]["predicted_events"] > 0
    assert "last_max_update_distribution" in result
    assert "tolerance_ratio_distribution" in result
