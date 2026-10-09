"""Focused tests for frozen Model Pick value evaluation (pure, DB-only contract)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sports_ai.dashboard import frozen_value_evaluation as fv

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
KICKOFF = "2026-10-08T15:00:00+00:00"


def _fixture(**overrides):
    base = {
        "fixture_id": "api-football:1",
        "provider": "api-football",
        "provider_fixture_id": "1",
        "competition_id": "ENG1",
        "competition_name": "Premier League",
        "division_code": "E0",
        "season": "2026",
        "home_team_name": "Home",
        "away_team_name": "Away",
        "kickoff_utc": KICKOFF,
    }
    base.update(overrides)
    return base


def _pick(market, selection, line, *, w=0.55, fair=1.82):
    return {
        "model_pick_id": "mp1",
        "fixture_id": "api-football:1",
        "market": market,
        "selection": selection,
        "line": line,
        "model_probability": w,
        "model_fair_price": fair,
        "model_as_of": "2026-10-08T10:00:00+00:00",
        "prediction_generated_at": "2026-10-08T10:00:00+00:00",
    }


def _row(market, selection, line, odds, bookmaker, *, captured="2026-10-08T11:50:00+00:00", source="2026-10-08T11:45:00+00:00", is_pre_match=True, snapshot_id=1, provider="api-football"):
    return {
        "fixture_id": "api-football:1", "market": market, "selection": selection, "line": line,
        "odds": odds, "bookmaker": bookmaker, "provider": provider, "captured_at": captured,
        "provider_timestamp": source, "is_pre_match": is_pre_match, "snapshot_id": snapshot_id,
        "freshness_state": "FRESH",
    }


def _complete_ah(*, ref_home=2.10, ref_away=1.80, other=None, line=-0.5):
    rows = [_row("asian_handicap", "HOME", line, ref_home, "Pinnacle"), _row("asian_handicap", "AWAY", line, ref_away, "Pinnacle")]
    if other is not None:
        rows.append(_row("asian_handicap", "HOME", line, other, "Betfair", snapshot_id=2))
    return rows


def _ev(pick, rows, *, fixture=None, now=NOW, safeguard=None, existing=False, lineage_ok=True):
    return fv.evaluate_frozen_pick(
        pick=pick, fixture=_fixture() if fixture is None else fixture, market_rows=rows,
        now_utc=now, safeguard_state=safeguard, existing_position=existing, existing_position_lineage_ok=lineage_ok,
    )


def test_value_bet_qualifies():
    result = _ev(_pick("asian_handicap", "HOME", -0.5), _complete_ah(other=3.00))
    assert result.state == fv.VALUE_BET
    assert result.selection == "HOME"  # frozen side
    assert result.best_price == 3.00
    assert result.reference_bookmaker == "Pinnacle"
    assert result.effective_win == 0.55
    assert result.effective_loss == pytest.approx(0.55 * 0.82)


def test_no_comparable_line_when_only_other_line():
    rows = [_row("asian_handicap", "HOME", -0.25, 3.0, "Pinnacle"), _row("asian_handicap", "AWAY", -0.25, 1.5, "Pinnacle")]
    result = _ev(_pick("asian_handicap", "HOME", -0.5), rows)
    assert result.state == fv.NO_COMPARABLE_MARKET_LINE


def test_better_price_on_other_line_cannot_redirect():
    # Frozen line -0.5 has no evidence; a huge price exists only at -0.25.
    rows = [_row("asian_handicap", "HOME", -0.25, 10.0, "Pinnacle"), _row("asian_handicap", "AWAY", -0.25, 1.05, "Pinnacle")]
    result = _ev(_pick("asian_handicap", "HOME", -0.5), rows)
    assert result.state == fv.NO_COMPARABLE_MARKET_LINE


def test_opposite_side_better_price_cannot_redirect():
    rows = _complete_ah()
    rows.append(_row("asian_handicap", "AWAY", -0.5, 5.0, "Betfair", snapshot_id=3))
    result = _ev(_pick("asian_handicap", "HOME", -0.5), rows)
    assert result.selection == "HOME"  # never switches to AWAY


def test_no_market_evidence():
    assert _ev(_pick("asian_handicap", "HOME", -0.5), []).state == fv.NO_MARKET_EVIDENCE


def test_stale_offer_is_market_stale():
    stale = _complete_ah(other=3.0)
    # make every row 300 minutes old
    for row in stale:
        row["captured_at"] = "2026-10-08T07:00:00+00:00"
        row["provider_timestamp"] = "2026-10-08T07:00:00+00:00"
    assert _ev(_pick("asian_handicap", "HOME", -0.5), stale).state == fv.MARKET_STALE


def test_stale_reference_book_is_market_stale():
    rows = [
        _row("asian_handicap", "HOME", -0.5, 2.10, "Pinnacle", captured="2026-10-08T07:00:00+00:00", source="2026-10-08T07:00:00+00:00"),
        _row("asian_handicap", "AWAY", -0.5, 1.80, "Pinnacle", captured="2026-10-08T07:00:00+00:00", source="2026-10-08T07:00:00+00:00"),
        _row("asian_handicap", "HOME", -0.5, 3.0, "Marathonbet", snapshot_id=2),
    ]
    assert _ev(_pick("asian_handicap", "HOME", -0.5), rows).state == fv.MARKET_STALE


def test_reference_absent_is_incomplete():
    rows = [_row("asian_handicap", "HOME", -0.5, 3.0, "Betfair", snapshot_id=2)]
    assert _ev(_pick("asian_handicap", "HOME", -0.5), rows).state == fv.REFERENCE_MARKET_INCOMPLETE


def test_fresh_offer_incomplete_reference():
    rows = [_row("asian_handicap", "HOME", -0.5, 2.10, "Pinnacle"), _row("asian_handicap", "HOME", -0.5, 3.0, "Betfair", snapshot_id=2)]
    assert _ev(_pick("asian_handicap", "HOME", -0.5), rows).state == fv.REFERENCE_MARKET_INCOMPLETE


def test_post_kickoff_blocked():
    result = _ev(_pick("asian_handicap", "HOME", -0.5), _complete_ah(other=3.0), now=datetime(2026, 10, 8, 16, 0, tzinfo=timezone.utc))
    assert result.state == fv.FIXTURE_ALREADY_STARTED


def test_in_play_rows_not_used():
    rows = _complete_ah(other=3.0)
    for row in rows:
        row["is_pre_match"] = False
    assert _ev(_pick("asian_handicap", "HOME", -0.5), rows).state == fv.MARKET_STALE


def test_source_timestamp_after_kickoff_blocked():
    rows = _complete_ah(other=3.0)
    for row in rows:
        row["provider_timestamp"] = "2026-10-08T15:30:00+00:00"
    assert _ev(_pick("asian_handicap", "HOME", -0.5), rows).state == fv.MARKET_STALE


def test_invalid_odds_blocked():
    rows = _complete_ah(other=1.0)
    assert _ev(_pick("asian_handicap", "HOME", -0.5), rows).state == fv.NO_MARKET_EVIDENCE


def test_identity_unresolved_missing_fixture():
    result = fv.evaluate_frozen_pick(pick=_pick("asian_handicap", "HOME", -0.5), fixture=None, market_rows=_complete_ah(), now_utc=NOW)
    assert result.state == fv.MARKET_IDENTITY_UNRESOLVED
    result2 = fv.evaluate_frozen_pick(pick=_pick("asian_handicap", "HOME", -0.5), fixture=_fixture(provider_fixture_id=""), market_rows=_complete_ah(), now_utc=NOW)
    assert result2.state == fv.MARKET_IDENTITY_UNRESOLVED


def test_240m_boundary_enforced():
    fresh = _complete_ah(other=3.0)
    # age 239 -> fresh
    for row in fresh:
        row["captured_at"] = "2026-10-08T08:01:00+00:00"
        row["provider_timestamp"] = "2026-10-08T08:01:00+00:00"
    assert _ev(_pick("asian_handicap", "HOME", -0.5), fresh).state == fv.VALUE_BET
    # age 241 -> stale
    for row in fresh:
        row["captured_at"] = "2026-10-08T07:59:00+00:00"
        row["provider_timestamp"] = "2026-10-08T07:59:00+00:00"
    assert _ev(_pick("asian_handicap", "HOME", -0.5), fresh).state == fv.MARKET_STALE


def test_no_bet_low_price():
    result = _ev(_pick("asian_handicap", "HOME", -0.5), _complete_ah(other=1.30))
    assert result.state == fv.NO_BET
    assert result.best_price == 1.30


def test_safeguard_blocked():
    result = _ev(_pick("asian_handicap", "HOME", -0.5), _complete_ah(other=3.0), safeguard="BLOCKED")
    assert result.state == fv.SAFEGUARD_BLOCKED


def test_existing_position():
    result = _ev(_pick("asian_handicap", "HOME", -0.5), _complete_ah(other=3.0), existing=True)
    assert result.state == fv.EXISTING_POSITION
    unresolved = _ev(_pick("asian_handicap", "HOME", -0.5), _complete_ah(other=3.0), existing=True, lineage_ok=False)
    assert unresolved.state == fv.EXISTING_POSITION_LINEAGE_UNRESOLVED


def test_missing_frozen_probability():
    pick = _pick("asian_handicap", "HOME", -0.5)
    pick["model_probability"] = None
    assert _ev(pick, _complete_ah(other=3.0)).state == fv.MODEL_PICK_UNAVAILABLE


def test_derived_loss_parity_helper():
    assert fv.derive_effective_loss_probability(model_probability=0.5, model_fair_price=3.0) == pytest.approx(1.0)
    assert fv.derive_effective_loss_probability(model_probability=0.55, model_fair_price=1.82) == pytest.approx(0.55 * 0.82)


def test_corners_value_bet():
    rows = [
        _row("corner_total", "OVER", 9.5, 1.95, "Pinnacle"),
        _row("corner_total", "UNDER", 9.5, 1.90, "Pinnacle"),
        _row("corner_total", "OVER", 9.5, 3.20, "Betfair", snapshot_id=2),
    ]
    result = _ev(_pick("corner_total", "OVER", 9.5), rows)
    assert result.state == fv.VALUE_BET
    assert result.selection == "OVER"


def test_action_hash_only_value_bets_and_deterministic():
    value = _ev(_pick("asian_handicap", "HOME", -0.5), _complete_ah(other=3.0))
    no_bet = _ev(_pick("asian_handicap", "HOME", -0.5), _complete_ah(other=1.30))
    h1 = fv.value_action_hash([value])
    h2 = fv.value_action_hash([value])
    assert h1 == h2
    assert fv.value_action_hash([no_bet]) != h1  # no-bet excluded -> empty hash
