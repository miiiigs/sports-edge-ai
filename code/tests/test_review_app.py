from sports_ai.review_app import (
    append_grade_action,
    export_dashboard_action,
    load_review_bundle,
    render_review_app_html,
    run_demo_action,
    run_historical_football_lane_action,
    run_live_football_watchlist_action,
    watchlist_state_path,
)
from sports_ai.storage.football_research_store import connect as connect_research, ensure_schema
import json


def test_review_app_actions_and_render(tmp_path):
    ledger_db = tmp_path / "paper_shadow.sqlite"
    research_db = tmp_path / "football_research.sqlite"
    export_html = tmp_path / "paper_shadow_dashboard.html"
    manual_fixtures = tmp_path / "manual_fixtures.json"
    manual_market = tmp_path / "manual_market.json"
    with connect_research(research_db) as conn:
        ensure_schema(conn)
        seed_rows = [
            ("Alpha", "Beta", 2, 0, "H"),
            ("Alpha", "Gamma", 2, 1, "H"),
            ("Delta", "Beta", 1, 0, "H"),
            ("Gamma", "Delta", 1, 1, "D"),
            ("Alpha", "Delta", 3, 1, "H"),
            ("Gamma", "Beta", 1, 0, "H"),
            ("Alpha", "Beta", 2, 1, "H"),
            ("Delta", "Gamma", 0, 1, "A"),
        ]
        for idx in range(24):
            home_team, away_team, hg, ag, result_code = seed_rows[idx % len(seed_rows)]
            conn.execute(
                """
                INSERT INTO football_events (
                    event_id, competition_id, competition_name, season_code, division_code,
                    match_date, kickoff_time, home_team, away_team, home_goals, away_goals,
                    result_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"evt_seed_{idx+1}",
                    "ENG1",
                    "Premier League",
                    "2526",
                    "E0",
                    f"2025-08-{(idx % 28) + 1:02d}",
                    "15:00",
                    home_team,
                    away_team,
                    hg,
                    ag,
                    result_code,
                ),
            )
        conn.execute(
            """
            INSERT INTO football_events (
                event_id, competition_id, competition_name, season_code, division_code,
                match_date, kickoff_time, home_team, away_team, home_goals, away_goals,
                result_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "evt_football_20250816_gamma_delta",
                "ESP1",
                "La Liga",
                "2526",
                "SP1",
                "2025-08-16",
                "20:00",
                "Gamma",
                "Delta",
                1,
                1,
                "D",
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
                "evt_football_20250816_gamma_delta",
                "ESP1",
                "2526",
                "SP1",
                "pinnacle_closing_1x2",
                "1X2",
                "Pinnacle",
                json.dumps({"home": 2.0, "draw": 3.4, "away": 3.8}),
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
                "evt_football_20250816_gamma_delta",
                "ESP1",
                "2526",
                "SP1",
                "bet365_closing_1x2",
                "1X2",
                "Bet365",
                json.dumps({"home": 2.25, "draw": 3.8, "away": 3.2}),
            ),
        )

    manual_fixtures.write_text(
        json.dumps(
            [
                {
                    "division_code": "E0",
                    "match_date": "2026-08-15",
                    "kickoff_time": "15:00",
                    "home_team": "Alpha",
                    "away_team": "Beta",
                    "source_ref": "operator_sheet",
                }
            ]
        ),
        encoding="utf-8",
    )
    manual_market.write_text(
        json.dumps(
            [
                {
                    "division_code": "E0",
                    "match_date": "2026-08-15",
                    "home_team": "Alpha",
                    "away_team": "Beta",
                    "selection": "home",
                    "decimal_odds": 2.60,
                    "captured_at": "2026-08-15T06:00:00+00:00",
                    "source_ref": "manual_capture",
                    "source_kind": "manual-market-import",
                }
            ]
        ),
        encoding="utf-8",
    )

    result = run_demo_action(ledger_db, closing_price=1.66, settlement="WIN")
    decision_id = result["decision_id"]

    update = append_grade_action(
        ledger_db,
        decision_id=decision_id,
        closing_price=1.61,
        settlement="WIN",
        notes="late correction",
    )
    assert update["decision_id"] == decision_id

    exported = export_dashboard_action(ledger_db, export_html)
    assert exported["row_count"] == 1
    assert export_html.exists()
    football_result = run_historical_football_lane_action(
        research_db_path=research_db,
        ledger_db_path=ledger_db,
        season_code="2526",
        competition_ids=["ESP1"],
        retail_bookmaker="Bet365",
        min_ev=0.02,
        min_edge=0.0,
        allowed_selections=["home", "draw", "away"],
        max_fair_odds=None,
        max_available_odds=None,
    )
    assert football_result["matched_events"] == 1
    assert football_result["candidate_count"] >= 1
    live_result = run_live_football_watchlist_action(
        research_db_path=research_db,
        ledger_db_path=ledger_db,
        season_code="2627",
        division_codes=["E0"],
        target_date="2026-08-15",
        bookmaker="Bet365",
        model_name="poisson_1x2_v3",
        min_team_matches=2,
        prior_weight=3.0,
        strength_regularization=0.03,
        edge_threshold=0.0,
        allowed_selections=["home"],
        max_fair_odds=5.0,
        fixture_source="manual",
        manual_fixtures_path=manual_fixtures,
        manual_market_path=manual_market,
    )
    assert live_result["funnel"]["fixtures_discovered"] == 1
    assert live_result["watchlist_state_path"] == str(watchlist_state_path(ledger_db))

    research_dir = tmp_path / "football_research"
    research_dir.mkdir()
    (research_dir / "track_b_model_selection_2026-08-14.json").write_text(
        json.dumps(
            {
                "validation": {
                    "poisson_1x2_v1": {"model_log_loss": 1.04225, "model_brier": 0.62716},
                    "poisson_1x2_v2": {"model_log_loss": 1.07688, "model_brier": 0.65161},
                },
                "test": {
                    "poisson_1x2_v1": {"model_log_loss": 1.05684, "model_brier": 0.63648},
                    "poisson_1x2_v2": {"model_log_loss": 1.07613, "model_brier": 0.65141},
                },
            }
        ),
        encoding="utf-8",
    )
    (research_dir / "track_b_finalization_summary_v1_vs_v3_2026-08-14.json").write_text(
        json.dumps(
            {
                "holdout_status": "previously_observed_holdout_not_pristine",
                "classification": "PROMOTE",
                "v1": {"model_log_loss": 1.05684, "model_brier": 0.63648},
                "v3": {"model_log_loss": 1.04542, "model_brier": 0.62901},
                "deltas_v3_minus_v1": {"log_loss": -0.01142, "brier": -0.00747},
            }
        ),
        encoding="utf-8",
    )
    (research_dir / "track_b_development_v4_rho_neg005_2026-08-14.json").write_text(
        json.dumps(
            {
                "validation": {
                    "poisson_1x2_v3": {"model_log_loss": 1.04067},
                    "poisson_1x2_v4": {"model_log_loss": 1.04011, "model_brier": 0.62594},
                },
                "promotion_assessment": {
                    "poisson_1x2_v4": {"eligible_for_finalization": True}
                },
            }
        ),
        encoding="utf-8",
    )

    exported = export_dashboard_action(ledger_db, export_html)
    assert exported["row_count"] >= 1
    exported_html = export_html.read_text(encoding="utf-8")
    assert "Sports Edge AI - Live Research Dashboard" in exported_html
    assert "Alpha vs Beta" in exported_html
    assert "How the system thinks" in exported_html
    assert "Track B Model Progress" in exported_html
    assert "Opponent-Adjusted Poisson" in exported_html

    bundle = load_review_bundle(ledger_db)
    assert bundle["watchlist_state"]["result"]["model_name"] == "poisson_1x2_v3"
    html = render_review_app_html(
        ledger_db_path=ledger_db,
        export_html_path=export_html,
        research_db_path=research_db,
        bundle=bundle,
        message="ready",
        level="success",
    )
    assert "Sports AI Review App" in html
    assert "Run Live Football Watchlist" in html
    assert "Last Live Watchlist Run" in html
    assert "Run Historical Football Lane" in html
    assert "Run Test Scenario" in html
    assert "Append Result / Grade Decision" in html
    assert "Export Client Dashboard" in html
    assert decision_id in html
