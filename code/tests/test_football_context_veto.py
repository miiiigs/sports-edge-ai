from __future__ import annotations

import pytest

from sports_ai.dashboard.football_context_veto import (
    CONTEXT_BLOCKED,
    CONTEXT_FLAG,
    CONTEXT_PASS,
    CONTEXT_VETO,
    REASON_ADVERSE_ODDS_MOVE,
    REASON_DEAD_RUBBER,
    REASON_EUROPE_CONGESTION,
    REASON_KEY_ATTACKER_RUled_OUT,
    REASON_NEWLY_PROMOTED_RELEGATED,
    REASON_RECENT_MANAGER_CHANGE,
    REASON_REQUIRED_EVIDENCE_MISSING,
    REASON_STALE_SOURCE,
    REASON_TWO_EXPECTED_STARTERS_MISSING,
    REASON_UNRATED_CUP_OPPONENT,
    REASON_XG_DIVERGENCE,
    adverse_odds_move,
    evaluate_football_context_veto,
)


def test_empty_context_is_pass_and_never_creates_or_boosts_bet():
    result = evaluate_football_context_veto({})

    assert result["status"] == CONTEXT_PASS
    assert result["kill_only"] is True
    assert result["bet_creation_allowed"] is False
    assert result["probability_adjustment"] == 0.0
    assert result["threshold_adjustment"] == 0.0


def test_two_missing_expected_starters_vetoes():
    result = evaluate_football_context_veto({"missing_expected_starters": 2})

    assert result["status"] == CONTEXT_VETO
    assert REASON_TWO_EXPECTED_STARTERS_MISSING in result["veto_reasons"]


def test_key_attacker_ruled_out_within_24h_vetoes():
    result = evaluate_football_context_veto({"key_attacker_ruled_out_within_24h": True})

    assert result["status"] == CONTEXT_VETO
    assert REASON_KEY_ATTACKER_RUled_OUT in result["veto_reasons"]


def test_promoted_or_relegated_team_under_eight_matches_vetoes():
    result = evaluate_football_context_veto(
        {"promoted_or_relegated": True, "season_matches_played": 7}
    )

    assert result["status"] == CONTEXT_VETO
    assert REASON_NEWLY_PROMOTED_RELEGATED in result["veto_reasons"]


def test_recent_manager_change_vetoes_through_three_matches():
    result = evaluate_football_context_veto({"manager_change_matches_ago": 3})

    assert result["status"] == CONTEXT_VETO
    assert REASON_RECENT_MANAGER_CHANGE in result["veto_reasons"]


def test_european_fixture_within_four_days_flags_and_widens_threshold_only():
    result = evaluate_football_context_veto({"hours_since_european_fixture": 72})

    assert result["status"] == CONTEXT_FLAG
    assert REASON_EUROPE_CONGESTION in result["flag_reasons"]
    assert result["threshold_adjustment"] == pytest.approx(0.01)
    assert result["probability_adjustment"] == 0.0


def test_unrated_cup_opponent_vetoes():
    result = evaluate_football_context_veto({"cup_tie": True, "opponent_rated": False})

    assert result["status"] == CONTEXT_VETO
    assert REASON_UNRATED_CUP_OPPONENT in result["veto_reasons"]


def test_xg_divergence_can_be_represented_as_context_veto_without_repricing():
    result = evaluate_football_context_veto({"xg_divergence_failed": True})

    assert result["status"] == CONTEXT_VETO
    assert REASON_XG_DIVERGENCE in result["veto_reasons"]
    assert result["probability_adjustment"] == 0.0


def test_adverse_decimal_odds_move_over_fifteen_percent_vetoes():
    move = adverse_odds_move(baseline_odds=2.00, current_odds=2.31)
    result = evaluate_football_context_veto({"baseline_odds": 2.00, "current_odds": 2.31})

    assert move["available"] is True
    assert move["price_move_fraction"] == pytest.approx(0.155)
    assert move["implied_probability_change_fraction"] < 0.0
    assert move["veto"] is True
    assert result["status"] == CONTEXT_VETO
    assert REASON_ADVERSE_ODDS_MOVE in result["veto_reasons"]


def test_shortening_price_is_not_an_adverse_move():
    move = adverse_odds_move(baseline_odds=2.00, current_odds=1.80)

    assert move["available"] is True
    assert move["price_move_fraction"] < 0.0
    assert move["veto"] is False


def test_any_stale_source_vetoes_and_records_source_names():
    result = evaluate_football_context_veto(
        {"source_states": {"odds": "FRESH", "lineups": "STALE", "injuries": "FRESH"}}
    )

    assert result["status"] == CONTEXT_VETO
    assert REASON_STALE_SOURCE in result["veto_reasons"]
    assert result["diagnostics"]["stale_sources"] == ["lineups"]


def test_missing_required_evidence_blocks_when_strict_mode_is_enabled():
    result = evaluate_football_context_veto(
        {
            "required_evidence": ["lineups", "injuries"],
            "available_evidence": {"lineups": True, "injuries": False},
        },
        strict_required_evidence=True,
    )

    assert result["status"] == CONTEXT_BLOCKED
    assert REASON_REQUIRED_EVIDENCE_MISSING in result["block_reasons"]
    assert result["diagnostics"]["missing_required_evidence"] == ["injuries"]


def test_dead_rubber_vetoes():
    result = evaluate_football_context_veto({"dead_rubber_both_sides": True})

    assert result["status"] == CONTEXT_VETO
    assert REASON_DEAD_RUBBER in result["veto_reasons"]


def test_veto_has_priority_over_flag_and_preserves_both_reasons():
    result = evaluate_football_context_veto(
        {
            "hours_to_european_fixture": 48,
            "dead_rubber_both_sides": True,
        }
    )

    assert result["status"] == CONTEXT_VETO
    assert REASON_DEAD_RUBBER in result["veto_reasons"]
    assert REASON_EUROPE_CONGESTION in result["flag_reasons"]
    assert result["threshold_adjustment"] == pytest.approx(0.01)
