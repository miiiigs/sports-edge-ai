import json

from sports_ai.experiments.football_1x2_close_value import (
    HistoricalLaneConfig,
    evaluate_historical_football_1x2_close_value,
    run_historical_football_1x2_close_value,
)
from sports_ai.storage.paper_shadow_ledger import connect as connect_ledger, fetch_ledger_rows
from sports_ai.storage.football_research_store import connect as connect_research, ensure_schema


def test_historical_football_close_value_lane_logs_candidates(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    ledger_db = tmp_path / "paper_shadow.sqlite"

    with connect_research(research_db) as conn:
        ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO football_events (
                event_id, competition_id, competition_name, season_code, division_code,
                match_date, kickoff_time, home_team, away_team, home_goals, away_goals,
                result_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "evt_football_20250815_alpha_beta",
                "ENG1",
                "Premier League",
                "2526",
                "E0",
                "2025-08-15",
                "15:00",
                "Alpha",
                "Beta",
                2,
                0,
                "H",
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
                "evt_football_20250815_alpha_beta",
                "ENG1",
                "2526",
                "E0",
                "pinnacle_closing_1x2",
                "1X2",
                "Pinnacle",
                json.dumps({"home": 1.8, "draw": 3.6, "away": 4.6}),
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
                "evt_football_20250815_alpha_beta",
                "ENG1",
                "2526",
                "E0",
                "bet365_closing_1x2",
                "1X2",
                "Bet365",
                json.dumps({"home": 2.2, "draw": 3.0, "away": 3.7}),
            ),
        )

    result = run_historical_football_1x2_close_value(
        HistoricalLaneConfig(
            research_db_path=research_db,
            ledger_db_path=ledger_db,
            competition_ids=("ENG1",),
            season_code="2526",
            retail_bookmaker="Bet365",
            min_ev=0.02,
        )
    )

    assert result["matched_events"] == 1
    assert result["candidate_count"] >= 1

    with connect_ledger(ledger_db) as conn:
        rows = fetch_ledger_rows(conn)

    assert len(rows) == result["candidate_count"]
    assert rows[0]["sport"] == "football"
    assert rows[0]["decision"] == "RESEARCH_SIGNAL"
    assert rows[0]["result_status"] == "SETTLED"
    assert rows[0]["settlement"] in {"WIN", "LOSS"}
    assert "historical_close_only=true" in rows[0]["notes"]


def test_historical_football_close_value_filters_candidates(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    ledger_db = tmp_path / "paper_shadow.sqlite"

    with connect_research(research_db) as conn:
        ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO football_events (
                event_id, competition_id, competition_name, season_code, division_code,
                match_date, kickoff_time, home_team, away_team, home_goals, away_goals,
                result_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "evt_football_20250816_alpha_beta",
                "ENG1",
                "Premier League",
                "2526",
                "E0",
                "2025-08-16",
                "15:00",
                "Alpha",
                "Beta",
                2,
                0,
                "H",
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
                "evt_football_20250816_alpha_beta",
                "ENG1",
                "2526",
                "E0",
                "pinnacle_closing_1x2",
                "1X2",
                "Pinnacle",
                json.dumps({"home": 2.0, "draw": 3.5, "away": 4.2}),
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
                "evt_football_20250816_alpha_beta",
                "ENG1",
                "2526",
                "E0",
                "bet365_closing_1x2",
                "1X2",
                "Bet365",
                json.dumps({"home": 2.45, "draw": 3.7, "away": 4.4}),
            ),
        )

    loose = evaluate_historical_football_1x2_close_value(
        HistoricalLaneConfig(
            research_db_path=research_db,
            ledger_db_path=ledger_db,
            competition_ids=("ENG1",),
            season_code="2526",
            retail_bookmaker="Bet365",
            min_ev=0.02,
        )
    )
    tight = evaluate_historical_football_1x2_close_value(
        HistoricalLaneConfig(
            research_db_path=research_db,
            ledger_db_path=ledger_db,
            competition_ids=("ENG1",),
            season_code="2526",
            retail_bookmaker="Bet365",
            min_ev=0.02,
            min_edge=0.05,
            allowed_selections=("home",),
            max_fair_odds=2.2,
            max_available_odds=2.6,
        )
    )

    assert loose["candidate_count"] >= tight["candidate_count"]
    assert tight["candidate_count"] == 1
