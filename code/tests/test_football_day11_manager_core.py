import json
import sqlite3

import pytest

from sports_ai.experiments.football_day11_manager_core import (
    DECISION_MISSING_REQUIRED_MARKET_DATA,
    DECISION_NO_BET,
    DECISION_PRELIMINARY_CANDIDATE,
    DECISION_REJECTED_RULE,
    DECISION_STALE_MARKET,
    REASON_INCOMPLETE_REFERENCE_MARKET,
    REASON_INVALID_TIMESTAMP,
    REASON_MISSING_AVAILABLE_PRICE,
    REASON_MISSING_TEAM_LEVEL_METADATA,
    REASON_NO_BET_EDGE_BELOW_THRESHOLD,
    REASON_NO_BET_PRICE_BELOW_MINIMUM,
    REASON_PASS_EDGE_THRESHOLD,
    REASON_PASS_MINIMUM_PRICE,
    REASON_REJECT_CUP_MARKET_NOT_ALLOWED,
    REASON_STALE_MARKET,
    REASON_UNKNOWN_COMPETITION_THRESHOLD,
    apply_day11_manager_rules,
    build_reference_market_booksum,
    canonical_market_identity,
    minimum_acceptable_price,
    normalize_market_snapshot,
    resolve_competition_threshold,
    run_day11_manager_experiment,
    settlement_aware_edge,
    validate_reference_market_rows,
    Day11ManagerConfig,
)


def _model_row(*, event_id="evt_001", competition_id="ENG1", selection="HOME", w=0.40, l=0.60, as_of="2026-08-18T15:00:00"):
    return {
        "fixture_id": event_id,
        "event_id": event_id,
        "competition_id": competition_id,
        "competition_name": "Premier League",
        "season_code": "2526",
        "division_code": "E0",
        "match_date": "2026-08-18",
        "kickoff_at": "2026-08-18T15:00:00",
        "as_of": as_of,
        "market_family": "1x2",
        "line": None,
        "selection": selection,
        "fair_decimal_price": 1.0 + (l / w),
        "effective_win_stake_probability": w,
        "effective_loss_stake_probability": l,
        "settlement_probabilities": {
            "full_win_probability": w,
            "half_win_probability": 0.0,
            "push_probability": 0.0,
            "half_loss_probability": 0.0,
            "full_loss_probability": l,
        },
        "source_scoreline_object_id": "scoreline_obj_001",
        "source_corner_object_id": None,
        "model_versions": {
            "scoreline_model_version": "football_day9_dixon_coles_scoreline_v1",
        },
        "pricing_model_version": "football_day10_market_pricing_v1",
        "settlement_engine_version": "football_day10_settlement_engine_v1",
        "settlement_pricing_policy_id": "zero_ev_with_partial_and_push_settlement_v1",
    }


def _snapshot(
    *,
    selection,
    odds,
    source="pinnacle_closing_1x2",
    bookmaker="Pinnacle",
    reference_designation="reference",
    event_id="evt_001",
    competition_id="ENG1",
    market_as_of="2026-08-18T15:00:00",
    freshness_state="FRESHNESS_POLICY_UNCONFIGURED",
):
    return normalize_market_snapshot(
        {
            "fixture_id": event_id,
            "event_id": event_id,
            "competition_id": competition_id,
            "competition_name": "Premier League",
            "season_code": "2526",
            "division_code": "E0",
            "home_team": "Alpha",
            "away_team": "Beta",
            "kickoff_at": "2026-08-18T15:00:00",
            "captured_at": market_as_of,
            "market_as_of": market_as_of,
            "source": source,
            "bookmaker": bookmaker,
            "reference_designation": reference_designation,
            "market_family": "1x2",
            "line": None,
            "selection": selection,
            "decimal_odds": odds,
            "provider_market_id": f"{source}_{bookmaker}",
            "freshness_state": freshness_state,
            "input_label": "HISTORICAL / DEMONSTRATION MARKET INPUT",
            "source_snapshot_version_id": f"src_{source}_{bookmaker}",
        }
    )


def _create_research_store(db_path):
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
        conn.execute(
            """
            CREATE TABLE football_market_quotes (
                quote_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                competition_id TEXT NOT NULL,
                season_code TEXT NOT NULL,
                division_code TEXT NOT NULL,
                source TEXT NOT NULL,
                market_type TEXT NOT NULL,
                bookmaker TEXT NOT NULL,
                selection_prices_json TEXT NOT NULL
            )
            """
        )
    conn.close()


def test_normalize_market_snapshot_preserves_sign_and_rejects_invalid_lines():
    normalized = normalize_market_snapshot(
        {
            "fixture_id": "evt_ah",
            "event_id": "evt_ah",
            "competition_id": "ENG1",
            "competition_name": "Premier League",
            "season_code": "2526",
            "division_code": "E0",
            "home_team": "Alpha",
            "away_team": "Beta",
            "kickoff_at": "2026-08-18T15:00:00",
            "captured_at": "2026-08-18T15:00:00",
            "market_as_of": "2026-08-18T15:00:00",
            "source": "manual",
            "bookmaker": "Book",
            "reference_designation": "available",
            "market_family": "asian_handicap",
            "line": "-0.25",
            "selection": "HOME",
            "decimal_odds": 1.95,
            "provider_market_id": "ah_1",
            "freshness_state": "FRESH",
            "input_label": "demo",
            "source_snapshot_version_id": "src_1",
        }
    )
    assert normalized["normalized_line"] == pytest.approx(-0.25)

    with pytest.raises(ValueError):
        normalize_market_snapshot(
            {
                **normalized,
                "line": "0.3",
                "market_family": "goals_total",
                "selection": "OVER",
            }
        )


def test_validate_reference_market_requires_complete_market():
    rows = [
        _snapshot(selection="HOME", odds=2.10),
        _snapshot(selection="DRAW", odds=3.40),
    ]
    validation = validate_reference_market_rows(rows)
    assert validation["is_complete"] is False
    assert validation["reason"] == REASON_INCOMPLETE_REFERENCE_MARKET


def test_build_reference_market_booksum_persists_power_and_shin():
    rows = [
        _snapshot(selection="HOME", odds=2.10),
        _snapshot(selection="DRAW", odds=3.40),
        _snapshot(selection="AWAY", odds=3.80),
    ]
    analysis = build_reference_market_booksum(rows, final_method="shin")
    assert analysis["reference_bookmaker"] == "Pinnacle"
    assert analysis["final_devig_method"] == "shin"
    assert analysis["booksum"] > 1.0
    assert set(analysis["devigged_probabilities_by_selection"]) == {"HOME", "DRAW", "AWAY"}
    assert analysis["devig_methods"]["power"]["parameter_name"] == "k"
    assert analysis["devig_methods"]["shin"]["parameter_name"] == "z"


def test_settlement_edge_and_minimum_price_reductions_hold_for_binary_and_push_cases():
    binary_edge = settlement_aware_edge(
        best_available_price=2.70,
        effective_win_stake_probability=0.40,
        effective_loss_stake_probability=0.60,
    )
    assert binary_edge == pytest.approx((0.40 * 2.70) - 1.0)
    assert minimum_acceptable_price(
        effective_win_stake_probability=0.40,
        effective_loss_stake_probability=0.60,
        threshold=0.05,
    ) == pytest.approx((1.0 + 0.05) / 0.40)

    quarter_edge = settlement_aware_edge(
        best_available_price=2.20,
        effective_win_stake_probability=0.55,
        effective_loss_stake_probability=0.35,
    )
    assert quarter_edge == pytest.approx((0.55 * 1.20) - 0.35)


def test_apply_day11_manager_rules_returns_candidate_and_no_bet():
    reference_market = build_reference_market_booksum(
        [
            _snapshot(selection="HOME", odds=2.00),
            _snapshot(selection="DRAW", odds=3.60),
            _snapshot(selection="AWAY", odds=4.20),
        ],
        final_method="shin",
    )

    candidate = apply_day11_manager_rules(
        model_row=_model_row(),
        reference_market=reference_market,
        available_offer_rows=[
            _snapshot(
                selection="HOME",
                odds=2.70,
                source="bet365_closing_1x2",
                bookmaker="Bet365",
                reference_designation="available",
            )
        ],
        decision_as_of="2026-08-18T15:00:00",
    )
    assert candidate["decision"] == DECISION_PRELIMINARY_CANDIDATE
    assert REASON_PASS_EDGE_THRESHOLD in candidate["reason_codes"]
    assert REASON_PASS_MINIMUM_PRICE in candidate["reason_codes"]

    no_bet = apply_day11_manager_rules(
        model_row=_model_row(),
        reference_market=reference_market,
        available_offer_rows=[
            _snapshot(
                selection="HOME",
                odds=2.55,
                source="bet365_closing_1x2",
                bookmaker="Bet365",
                reference_designation="available",
            )
        ],
        decision_as_of="2026-08-18T15:00:00",
    )
    assert no_bet["decision"] == DECISION_NO_BET
    assert REASON_NO_BET_EDGE_BELOW_THRESHOLD in no_bet["reason_codes"]
    assert REASON_NO_BET_PRICE_BELOW_MINIMUM in no_bet["reason_codes"]


def test_apply_day11_manager_rules_handles_rejections_and_missing_states():
    reference_market = build_reference_market_booksum(
        [
            _snapshot(selection="HOME", odds=2.00),
            _snapshot(selection="DRAW", odds=3.60),
            _snapshot(selection="AWAY", odds=4.20),
        ],
        final_method="shin",
    )

    stale = apply_day11_manager_rules(
        model_row=_model_row(),
        reference_market=reference_market,
        available_offer_rows=[
            _snapshot(
                selection="HOME",
                odds=2.70,
                source="bet365_closing_1x2",
                bookmaker="Bet365",
                reference_designation="available",
                freshness_state="STALE",
            )
        ],
        decision_as_of="2026-08-18T15:00:00",
    )
    assert stale["decision"] == DECISION_STALE_MARKET
    assert stale["reason_codes"] == [REASON_STALE_MARKET]

    missing = apply_day11_manager_rules(
        model_row=_model_row(),
        reference_market=reference_market,
        available_offer_rows=[],
        decision_as_of="2026-08-18T15:00:00",
    )
    assert missing["decision"] == DECISION_MISSING_REQUIRED_MARKET_DATA
    assert missing["reason_codes"] == [REASON_MISSING_AVAILABLE_PRICE]

    invalid_timestamp = apply_day11_manager_rules(
        model_row=_model_row(as_of="2026-08-18T16:00:00"),
        reference_market=reference_market,
        available_offer_rows=[],
        decision_as_of="2026-08-18T15:00:00",
    )
    assert invalid_timestamp["decision"] == DECISION_REJECTED_RULE
    assert invalid_timestamp["reason_codes"] == [REASON_INVALID_TIMESTAMP]

    unknown_comp = apply_day11_manager_rules(
        model_row=_model_row(competition_id="UNKNOWN"),
        reference_market=reference_market,
        available_offer_rows=[],
        decision_as_of="2026-08-18T15:00:00",
    )
    assert unknown_comp["decision"] == DECISION_REJECTED_RULE
    assert unknown_comp["reason_codes"] == [REASON_UNKNOWN_COMPETITION_THRESHOLD]

    cup_reject = apply_day11_manager_rules(
        model_row={**_model_row(competition_id="ENG_FA_CUP"), "market_family": "1x2"},
        reference_market=reference_market,
        available_offer_rows=[],
        decision_as_of="2026-08-18T15:00:00",
        team_level_metadata={"Alpha": 3, "Beta": 4},
    )
    assert cup_reject["decision"] == DECISION_REJECTED_RULE
    assert cup_reject["reason_codes"] == [REASON_REJECT_CUP_MARKET_NOT_ALLOWED]

    cup_missing_team_level = apply_day11_manager_rules(
        model_row={**_model_row(competition_id="ENG_FA_CUP"), "market_family": "btts"},
        reference_market=reference_market,
        available_offer_rows=[],
        decision_as_of="2026-08-18T15:00:00",
    )
    assert cup_missing_team_level["decision"] == DECISION_REJECTED_RULE
    assert cup_missing_team_level["reason_codes"] == [REASON_MISSING_TEAM_LEVEL_METADATA]


def test_resolve_competition_threshold_uses_exact_mapping():
    assert resolve_competition_threshold("ENG1") == pytest.approx(0.05)
    assert resolve_competition_threshold("ENG2") == pytest.approx(0.06)
    assert resolve_competition_threshold("ENG_FA_CUP") == pytest.approx(0.08)
    with pytest.raises(KeyError):
        resolve_competition_threshold("XYZ")


def test_run_day11_manager_experiment_is_deterministic_and_avoids_staking_fields(tmp_path):
    research_db = tmp_path / "football.sqlite"
    _create_research_store(research_db)
    conn = sqlite3.connect(research_db)
    with conn:
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
            [
                ("evt_001", "ENG1", "Premier League", "2526", "E0", "2026-08-18", "15:00", "Alpha", "Beta", None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None),
                ("evt_002", "ENG1", "Premier League", "2526", "E0", "2026-08-18", "17:00", "Gamma", "Delta", None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None),
            ],
        )
        conn.executemany(
            """
            INSERT INTO football_market_quotes (
                event_id, competition_id, season_code, division_code, source, market_type, bookmaker, selection_prices_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("evt_001", "ENG1", "2526", "E0", "pinnacle_closing_1x2", "1X2", "Pinnacle", json.dumps({"home": 2.05, "draw": 3.55, "away": 4.00})),
                ("evt_001", "ENG1", "2526", "E0", "bet365_closing_1x2", "1X2", "Bet365", json.dumps({"home": 2.80, "draw": 3.20, "away": 3.70})),
                ("evt_001", "ENG1", "2526", "E0", "max_closing_1x2", "1X2", "MarketMax", json.dumps({"home": 2.85, "draw": 3.25, "away": 3.80})),
                ("evt_001", "ENG1", "2526", "E0", "avg_closing_1x2", "1X2", "MarketAvg", json.dumps({"home": 2.60, "draw": 3.30, "away": 3.90})),
                ("evt_002", "ENG1", "2526", "E0", "avg_closing_1x2", "1X2", "MarketAvg", json.dumps({"home": 2.30, "draw": 3.30, "away": 3.10})),
            ],
        )
    conn.close()

    model_rows = [
        _model_row(event_id="evt_001", selection="HOME", w=0.40, l=0.60),
        _model_row(event_id="evt_001", selection="DRAW", w=0.27, l=0.73),
        _model_row(event_id="evt_001", selection="AWAY", w=0.33, l=0.67),
        {
            **_model_row(event_id="evt_002", selection="HOME", w=0.40, l=0.60, as_of="2026-08-18T17:00:00"),
            "kickoff_at": "2026-08-18T17:00:00",
        },
        {
            **_model_row(event_id="evt_002", selection="DRAW", w=0.28, l=0.72, as_of="2026-08-18T17:00:00"),
            "kickoff_at": "2026-08-18T17:00:00",
        },
        {
            **_model_row(event_id="evt_002", selection="AWAY", w=0.32, l=0.68, as_of="2026-08-18T17:00:00"),
            "kickoff_at": "2026-08-18T17:00:00",
        },
    ]
    day10_path = tmp_path / "day10.jsonl"
    day10_path.write_text("".join(json.dumps(row) + "\n" for row in model_rows), encoding="utf-8")

    config = Day11ManagerConfig(
        day10_market_prices_path=day10_path,
        research_db_path=research_db,
        technical_output_dir=tmp_path / "tech",
        product_output_dir=tmp_path / "product",
        latest_output_dir=tmp_path / "latest",
    )
    first = run_day11_manager_experiment(config)
    second = run_day11_manager_experiment(config)

    assert first["decision_records"] == second["decision_records"]
    assert first["devig_outputs"] == second["devig_outputs"]
    assert len(first["decision_records"]) == 6
    assert {row["decision"] for row in first["decision_records"]} >= {
        DECISION_PRELIMINARY_CANDIDATE,
        DECISION_MISSING_REQUIRED_MARKET_DATA,
    }
    for row in first["decision_records"]:
        serialized = json.dumps(row, sort_keys=True)
        assert "suggested_stake" not in serialized.lower()
        assert "stake_fraction" not in serialized.lower()
        assert "kelly" not in serialized.lower()
        assert row["model_fair_decimal_price"] is not None
        assert row["decision_id"]
        assert row["day11_manager_core_ready"] == "YES"
        assert row["live_paper_pipeline_ready"] == "NO"
