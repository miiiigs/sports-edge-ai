from sports_ai.pipeline.football_watchlist import FootballWatchlistConfig, generate_football_watchlist
from sports_ai.storage.football_research_store import connect as connect_research, ensure_schema
from sports_ai.storage.paper_shadow_ledger import (
    connect as connect_ledger,
    fetch_ledger_rows,
    fetch_lock_rows,
    fetch_market_snapshots,
)
from sports_ai.data_sources.football_upcoming import UpcomingFixture
import json


CURRENT_SEASON_CSV = """Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,B365H,B365D,B365A,B365CH,B365CD,B365CA
E0,08/08/2026,15:00,Alpha,Beta,2,0,H,1.90,3.30,4.20,1.88,3.25,4.10
E0,15/08/2026,15:00,Alpha,Beta,,,,2.60,3.60,4.80,,,
E0,15/08/2026,17:30,Gamma,Delta,,,,2.05,3.10,4.10,,,
"""


class DummyClient:
    def get(self, url: str):
        import httpx

        request = httpx.Request("GET", url)
        return httpx.Response(200, request=request, text=CURRENT_SEASON_CSV)


API_FIXTURE_NO_PRICE = UpcomingFixture(
    event_id="evt_football_20260815_alpha_beta",
    match_date="2026-08-15",
    kickoff_time="15:00",
    home_team="Alpha",
    away_team="Beta",
    division_code="E0",
    competition_id="ENG1",
    competition_name="Premier League",
    source_kind="football-data-org-api",
    source_ref="https://api.football-data.org/v4/matches",
)


def _seed_research_db(db_path):
    with connect_research(db_path) as conn:
        ensure_schema(conn)
        base_rows = [
            ("Alpha", "Beta", 2, 0, "H"),
            ("Alpha", "Gamma", 2, 1, "H"),
            ("Delta", "Beta", 1, 0, "H"),
            ("Gamma", "Delta", 1, 1, "D"),
            ("Alpha", "Delta", 3, 1, "H"),
            ("Gamma", "Beta", 1, 0, "H"),
            ("Alpha", "Beta", 2, 1, "H"),
            ("Delta", "Gamma", 0, 1, "A"),
        ]
        rows = []
        for idx in range(24):
            home_team, away_team, hg, ag, result_code = base_rows[idx % len(base_rows)]
            rows.append(
                (
                    f"evt_{idx+1}",
                    f"2025-08-{(idx % 28) + 1:02d}",
                    home_team,
                    away_team,
                    hg,
                    ag,
                    result_code,
                )
            )
        for event_id, match_date, home_team, away_team, hg, ag, result_code in rows:
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
                    match_date,
                    "15:00",
                    home_team,
                    away_team,
                    hg,
                    ag,
                    result_code,
                ),
            )


def test_generate_football_watchlist_builds_candidates_and_logs(tmp_path, monkeypatch):
    research_db = tmp_path / "football_research.sqlite"
    ledger_db = tmp_path / "paper_shadow.sqlite"
    _seed_research_db(research_db)
    monkeypatch.setattr("sports_ai.pipeline.football_watchlist._utc_now", lambda: "2026-08-15T10:00:00+00:00")

    result = generate_football_watchlist(
        FootballWatchlistConfig(
            research_db_path=research_db,
            season_code="2627",
            division_codes=("E0",),
            target_date="2026-08-15",
            model_name="poisson_1x2_v1",
            min_team_matches=2,
            prior_weight=3.0,
            edge_threshold=0.03,
            min_data_quality=0.0,
            max_uncertainty=2.0,
            min_uncertainty_adjusted_edge=-1.0,
            require_model_convergence=False,
            allowed_selections=("home",),
            max_fair_odds=3.0,
            ledger_db_path=ledger_db,
        ),
        client=DummyClient(),
    )

    assert result["card_count"] == 2
    assert result["bet_candidate_count"] >= 1
    assert result["funnel"]["fixtures_discovered"] == 2
    assert result["funnel"]["blind_prediction_possible"] == 2
    assert result["funnel"]["market_prices_available"] == 2
    assert result["funnel"]["final_paper_shadow_candidates"] == result["bet_candidate_count"]
    assert len(result["logged_decision_ids"]) == result["bet_candidate_count"]
    assert any(card["status"] == "PAPER_SHADOW_CANDIDATE" for card in result["cards"])
    assert all(card.get("prediction_lock_hash") for card in result["cards"])
    assert all(card.get("market_lock_hash") for card in result["cards"])

    with connect_ledger(ledger_db) as conn:
        rows = fetch_ledger_rows(conn)
        lock_rows = fetch_lock_rows(conn, run_id=result["run_id"])
        market_snapshots = fetch_market_snapshots(conn, run_id=result["run_id"])
    assert len(rows) == result["bet_candidate_count"]
    assert rows[0]["sport"] == "football"
    assert rows[0]["decision"] == "PAPER_SHADOW_CANDIDATE"
    assert rows[0]["model_version"] == "poisson_1x2_v1"
    assert len(lock_rows) == result["card_count"] * 2
    assert len(market_snapshots) >= 3


def test_generate_football_watchlist_preserves_lock_and_snapshot_sequence(tmp_path, monkeypatch):
    research_db = tmp_path / "football_research.sqlite"
    ledger_db = tmp_path / "paper_shadow.sqlite"
    _seed_research_db(research_db)
    monkeypatch.setattr("sports_ai.pipeline.football_watchlist._utc_now", lambda: "2026-08-15T10:00:00+00:00")

    result = generate_football_watchlist(
        FootballWatchlistConfig(
            research_db_path=research_db,
            season_code="2627",
            division_codes=("E0",),
            target_date="2026-08-15",
            model_name="poisson_1x2_v1",
            min_team_matches=2,
            prior_weight=3.0,
            edge_threshold=0.03,
            min_data_quality=0.0,
            max_uncertainty=2.0,
            min_uncertainty_adjusted_edge=-1.0,
            require_model_convergence=False,
            allowed_selections=("home",),
            max_fair_odds=3.0,
            ledger_db_path=ledger_db,
        ),
        client=DummyClient(),
    )

    candidate_card = next(card for card in result["cards"] if card["status"] == "PAPER_SHADOW_CANDIDATE")
    with connect_ledger(ledger_db) as conn:
        lock_rows = fetch_lock_rows(conn, event_id=candidate_card["event_id"], run_id=result["run_id"])
        market_snapshots = fetch_market_snapshots(conn, event_id=candidate_card["event_id"], run_id=result["run_id"])
        ledger_rows = fetch_ledger_rows(conn)

    assert len(lock_rows) == 2
    assert lock_rows[0]["stage"] == "prediction"
    assert lock_rows[1]["stage"] == "market"
    assert lock_rows[1]["prev_hash"] == lock_rows[0]["payload_hash"]
    assert len(market_snapshots) >= 1

    prediction_payload = json.loads(lock_rows[0]["payload_json"])
    market_payload = json.loads(lock_rows[1]["payload_json"])
    assert "bookmaker" not in json.dumps(prediction_payload).lower()
    assert "available_price" not in json.dumps(prediction_payload).lower()
    assert "prices" not in json.dumps(prediction_payload).lower()
    assert "bookmaker" in json.dumps(market_payload).lower()
    assert "prices" in json.dumps(market_payload).lower()

    decision_row = next(row for row in ledger_rows if row["event_id"] == candidate_card["event_id"])
    assert lock_rows[0]["payload_hash"] in (decision_row["notes"] or "")
    assert lock_rows[1]["payload_hash"] in (decision_row["notes"] or "")


def test_generate_football_watchlist_handles_no_upcoming_rows(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    _seed_research_db(research_db)

    result = generate_football_watchlist(
        FootballWatchlistConfig(
            research_db_path=research_db,
            season_code="2627",
            division_codes=("E0",),
            target_date="2026-08-16",
            model_name="poisson_1x2_v1",
            min_team_matches=2,
            prior_weight=3.0,
            edge_threshold=0.03,
            allowed_selections=("home",),
            max_fair_odds=3.0,
        ),
        client=DummyClient(),
    )

    assert result["card_count"] == 0
    assert result["bet_candidate_count"] == 0
    assert result["funnel"]["fixtures_discovered"] == 0


def test_generate_football_watchlist_marks_watch_when_fixture_has_no_price(tmp_path, monkeypatch):
    research_db = tmp_path / "football_research.sqlite"
    _seed_research_db(research_db)

    monkeypatch.setattr(
        "sports_ai.pipeline.football_watchlist._fetch_csv_rows",
        lambda *args, **kwargs: ("https://example.test/no_prices.csv", []),
    )
    monkeypatch.setattr(
        "sports_ai.pipeline.football_watchlist.discover_upcoming_fixtures",
        lambda **kwargs: (
            [API_FIXTURE_NO_PRICE],
            [
                {
                    "provider": "football-data-org",
                    "division_code": "E0",
                    "requested": True,
                    "status": "OK",
                    "fixtures_returned": 1,
                    "competitions_returned": 1,
                    "failure_reason": None,
                    "source_ref": "https://api.football-data.org/v4/matches",
                }
            ],
        ),
    )

    result = generate_football_watchlist(
        FootballWatchlistConfig(
            research_db_path=research_db,
            season_code="2627",
            division_codes=("E0",),
            target_date="2026-08-15",
            model_name="poisson_1x2_v1",
            min_team_matches=2,
            prior_weight=3.0,
            edge_threshold=0.03,
            allowed_selections=("home",),
            max_fair_odds=3.0,
            fixture_source="football-data-org",
            football_data_token="dummy",
        ),
        client=DummyClient(),
    )

    assert result["card_count"] == 1
    assert result["bet_candidate_count"] == 0
    assert result["cards"][0]["status"] == "WATCH"
    assert result["funnel"]["market_prices_missing"] == 1
    assert result["provider_diagnostics"][0]["provider"] == "football-data-org"


def test_generate_football_watchlist_tolerates_current_csv_fetch_failure(tmp_path, monkeypatch):
    research_db = tmp_path / "football_research.sqlite"
    _seed_research_db(research_db)

    monkeypatch.setattr(
        "sports_ai.pipeline.football_watchlist.discover_upcoming_fixtures",
        lambda **kwargs: (
            [API_FIXTURE_NO_PRICE],
            [
                {
                    "provider": "football-data-org",
                    "division_code": "E0",
                    "requested": True,
                    "status": "OK",
                    "fixtures_returned": 1,
                    "competitions_returned": 1,
                    "failure_reason": None,
                    "source_ref": "https://api.football-data.org/v4/matches",
                },
                {
                    "provider": "football-data-co-uk",
                    "division_code": "E0",
                    "requested": True,
                    "status": "FAILED",
                    "fixtures_returned": 0,
                    "competitions_returned": 0,
                    "failure_reason": "RuntimeError: source unavailable",
                    "source_ref": "https://www.football-data.co.uk/mmz4281/2627/E0.csv",
                },
            ],
        ),
    )
    monkeypatch.setattr(
        "sports_ai.pipeline.football_watchlist._fetch_csv_rows",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("source unavailable")),
    )

    result = generate_football_watchlist(
        FootballWatchlistConfig(
            research_db_path=research_db,
            season_code="2627",
            division_codes=("E0",),
            target_date="2026-08-15",
            model_name="poisson_1x2_v1",
            min_team_matches=2,
            prior_weight=3.0,
            edge_threshold=0.03,
            fixture_source="football-data-org",
            football_data_token="dummy",
        ),
        client=DummyClient(),
    )

    assert result["card_count"] == 1
    assert result["cards"][0]["status"] == "WATCH"
    assert result["funnel"]["fixtures_discovered"] == 1
    assert result["funnel"]["provider_failures"] == 1


def test_generate_football_watchlist_uses_manual_fixture_and_market_inputs(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    ledger_db = tmp_path / "paper_shadow.sqlite"
    manual_fixtures = tmp_path / "manual_fixtures.json"
    manual_market = tmp_path / "manual_market.json"
    _seed_research_db(research_db)

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
                    "captured_at": "2026-08-14T18:00:00+00:00",
                    "source_ref": "manual_capture",
                    "source_kind": "manual-market-import",
                }
            ]
        ),
        encoding="utf-8",
    )

    result = generate_football_watchlist(
        FootballWatchlistConfig(
            research_db_path=research_db,
            season_code="2627",
            division_codes=("E0",),
            target_date="2026-08-15",
            model_name="poisson_1x2_v1",
            min_team_matches=2,
            prior_weight=3.0,
            edge_threshold=0.03,
            allowed_selections=("home",),
            max_fair_odds=3.0,
            ledger_db_path=ledger_db,
            fixture_source="manual",
            manual_fixtures_path=manual_fixtures,
            manual_market_path=manual_market,
        ),
        client=DummyClient(),
    )

    assert result["funnel"]["fixtures_discovered"] == 1
    assert result["funnel"]["market_prices_available"] == 1
    assert result["cards"][0]["market_source_kind"] == "manual-market-import"


def test_generate_football_watchlist_supports_promoted_v3_model(tmp_path, monkeypatch):
    research_db = tmp_path / "football_research.sqlite"
    ledger_db = tmp_path / "paper_shadow.sqlite"
    _seed_research_db(research_db)
    monkeypatch.setattr("sports_ai.pipeline.football_watchlist._utc_now", lambda: "2026-08-15T10:00:00+00:00")

    result = generate_football_watchlist(
        FootballWatchlistConfig(
            research_db_path=research_db,
            season_code="2627",
            division_codes=("E0",),
            target_date="2026-08-15",
            model_name="poisson_1x2_v3",
            min_team_matches=2,
            prior_weight=3.0,
            strength_regularization=0.03,
            edge_threshold=0.0,
            min_data_quality=0.0,
            max_uncertainty=2.0,
            min_uncertainty_adjusted_edge=-1.0,
            require_model_convergence=False,
            allowed_selections=("home", "draw", "away"),
            max_fair_odds=5.0,
            ledger_db_path=ledger_db,
        ),
        client=DummyClient(),
    )

    assert result["model_name"] == "poisson_1x2_v3"
    assert result["card_count"] == 2
    assert result["funnel"]["blind_prediction_possible"] == 2
    with connect_ledger(ledger_db) as conn:
        rows = fetch_ledger_rows(conn)
    assert rows
    assert all(row["model_version"] == "poisson_1x2_v3" for row in rows)


def test_generate_football_watchlist_default_gate_blocks_unstable_v3_candidate(tmp_path, monkeypatch):
    research_db = tmp_path / "football_research.sqlite"
    ledger_db = tmp_path / "paper_shadow.sqlite"
    _seed_research_db(research_db)
    monkeypatch.setattr("sports_ai.pipeline.football_watchlist._utc_now", lambda: "2026-08-15T10:00:00+00:00")

    result = generate_football_watchlist(
        FootballWatchlistConfig(
            research_db_path=research_db,
            season_code="2627",
            division_codes=("E0",),
            target_date="2026-08-15",
            model_name="poisson_1x2_v3",
            min_team_matches=2,
            prior_weight=3.0,
            strength_regularization=0.03,
            edge_threshold=0.0,
            allowed_selections=("home", "draw", "away"),
            max_fair_odds=5.0,
            ledger_db_path=ledger_db,
        ),
        client=DummyClient(),
    )

    assert result["card_count"] == 2
    assert result["funnel"]["final_paper_shadow_candidates"] == 0
    assert all(card["status"] != "PAPER_SHADOW_CANDIDATE" for card in result["cards"])
    assert any(card["reason_code"] in {"PASS_MODEL_UNSTABLE", "PASS_LOW_DATA_QUALITY"} for card in result["cards"])
    assert all("market_report" in card for card in result["cards"] if card.get("prediction"))
    with connect_ledger(ledger_db) as conn:
        rows = fetch_ledger_rows(conn)
    assert rows == []


def test_generate_football_watchlist_rejects_post_kickoff_market_snapshot(tmp_path):
    research_db = tmp_path / "football_research.sqlite"
    ledger_db = tmp_path / "paper_shadow.sqlite"
    manual_fixtures = tmp_path / "manual_fixtures.json"
    manual_market = tmp_path / "manual_market.json"
    _seed_research_db(research_db)

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
                    "captured_at": "2026-08-15T14:00:00+00:00",
                    "source_ref": "manual_capture",
                    "source_kind": "manual-market-import",
                    "price_source_tier": "SECONDARY_BOOKMAKER_LISTING",
                }
            ]
        ),
        encoding="utf-8",
    )

    result = generate_football_watchlist(
        FootballWatchlistConfig(
            research_db_path=research_db,
            season_code="2627",
            division_codes=("E0",),
            target_date="2026-08-15",
            model_name="poisson_1x2_v1",
            min_team_matches=2,
            prior_weight=3.0,
            edge_threshold=0.0,
            allowed_selections=("home",),
            max_fair_odds=3.0,
            ledger_db_path=ledger_db,
            fixture_source="manual",
            manual_fixtures_path=manual_fixtures,
            manual_market_path=manual_market,
        ),
        client=DummyClient(),
    )

    assert result["funnel"]["fixtures_discovered"] == 1
    assert result["funnel"]["market_prices_available"] == 1
    assert result["funnel"]["market_snapshots_rejected_late"] == 1
    assert result["funnel"]["final_paper_shadow_candidates"] == 0
    assert result["cards"][0]["status"] == "PASS"
    assert "invalid for pre-match decision" in result["cards"][0]["reason"]
    assert result["cards"][0]["late_market_snapshot"] is True
    assert result["cards"][0]["kickoff_at_utc"] == "2026-08-15T14:00:00+00:00"
    with connect_ledger(ledger_db) as conn:
        rows = fetch_ledger_rows(conn)
        market_snapshots = fetch_market_snapshots(conn, run_id=result["run_id"])
    assert rows == []
    assert len(market_snapshots) == 1
