"""Focused tests for read-only Value evidence-integrity diagnostics + issuance flags."""

from __future__ import annotations

from sports_ai.dashboard import value_evidence_integrity as vei
from sports_ai.dashboard.services import DashboardConfig


def _row(market, selection, line, odds, bookmaker, *, captured="2026-10-09T01:00:00+00:00"):
    return {
        "market": market, "selection": selection, "line": line, "odds": odds,
        "bookmaker": bookmaker, "captured_at": captured,
    }


def test_implied_probability_sum_and_classification():
    assert vei.implied_probability_sum([2.0, 2.0]) == 1.0
    assert vei.classify_pair_integrity([1.9, 1.95]) == vei.INTEGRITY_VALID
    assert vei.classify_pair_integrity([2.0, 1.73]) == vei.INTEGRITY_VALID  # corners
    assert vei.classify_pair_integrity([2.07, 2.45]) == vei.INTEGRITY_BOOK_PAIRING_INCONSISTENT  # sum < 1
    assert vei.classify_pair_integrity([1.42, 4.58]) == vei.INTEGRITY_BOOK_PAIRING_INCONSISTENT
    assert vei.classify_pair_integrity([1.0]) == vei.INTEGRITY_MISSING_PAIR


def test_pair_odds_at_line_uses_latest_and_exact_line():
    rows = [
        _row("asian_handicap", "HOME", -0.25, 2.10, "Pinnacle", captured="2026-10-08T23:00:00+00:00"),
        _row("asian_handicap", "HOME", -0.25, 2.07, "Pinnacle", captured="2026-10-09T00:16:00+00:00"),
        _row("asian_handicap", "AWAY", -0.25, 1.90, "Pinnacle", captured="2026-10-09T00:16:00+00:00"),
        _row("asian_handicap", "AWAY", -0.5, 3.90, "Pinnacle"),  # different line ignored
        _row("asian_handicap", "HOME", -0.25, 2.99, "Bet365"),  # different bookmaker
    ]
    odds = vei.pair_odds_at_line(rows, bookmaker="Pinnacle", market="asian_handicap", line=-0.25, selections=("HOME", "AWAY"))
    assert odds == {"HOME": 2.07, "AWAY": 1.90}


def test_audit_book_pair_and_candidate_overall():
    rows = [
        _row("asian_handicap", "HOME", -0.5, 1.93, "Pinnacle"),
        _row("asian_handicap", "AWAY", -0.5, 3.79, "Pinnacle"),
        _row("asian_handicap", "HOME", -0.5, 3.90, "Bet365"),
        _row("asian_handicap", "AWAY", -0.5, 1.90, "Bet365"),
    ]
    book = vei.audit_book_pair(rows, bookmaker="Pinnacle", market="asian_handicap", line=-0.5)
    assert book["state"] == vei.INTEGRITY_BOOK_PAIRING_INCONSISTENT  # 1/1.93 + 1/3.79 < 1
    result = vei.audit_candidate_integrity(rows, market="asian_handicap", line=-0.5, best_bookmaker="Bet365", reference_bookmaker="Pinnacle")
    assert result["state"] == vei.INTEGRITY_BOOK_PAIRING_INCONSISTENT
    assert result["reference_pair"]["implied_probability_sum"] < 1.0


def test_flags_default_off():
    config = DashboardConfig.default()
    assert config.enable_value_bet_issuance is False
    assert config.enable_value_bet_lifecycle_scheduler is False
    assert config.value_bet_lifecycle_interval_seconds == 600
