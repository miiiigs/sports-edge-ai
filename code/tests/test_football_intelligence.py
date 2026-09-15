from __future__ import annotations

import pytest

from sports_ai.dashboard.football_intelligence import (
    _calibration,
    _clv,
    _edge_bucket,
    _health,
    _metrics,
    _odds_bucket,
)


def _row(**updates):
    row = {
        "paper_trade_id": "paper-1",
        "fixture_id": "fixture-1",
        "_canonical_fixture_id": "canonical-1",
        "market": "1x2",
        "trade_state": "WON",
        "net_pnl": 1.0,
        "entry_odds": 2.0,
        "available_market_price": 2.0,
        "edge": 0.06,
        "model_probability": 0.60,
        "closing_odds": 1.80,
        "closing_snapshot_id": 10,
        "closing_margin": 0.04,
        "clv_pct": (2.0 / 1.80 / 1.04) - 1.0,
    }
    row.update(updates)
    return row


def test_metrics_use_normalized_settled_rows_only():
    rows = [
        _row(paper_trade_id="w", trade_state="WON", net_pnl=1.0, entry_odds=2.0),
        _row(paper_trade_id="l", fixture_id="fixture-2", _canonical_fixture_id="canonical-2", trade_state="LOST", net_pnl=-1.0, entry_odds=3.0),
        _row(paper_trade_id="p", fixture_id="fixture-3", _canonical_fixture_id="canonical-3", trade_state="PUSH", net_pnl=0.0, entry_odds=2.5),
        _row(paper_trade_id="o", fixture_id="fixture-4", _canonical_fixture_id="canonical-4", trade_state="OPEN", net_pnl=None),
    ]

    result = _metrics(rows, minimum_sample=3)

    assert result["bets"] == 3
    assert result["matches"] == 3
    assert result["wins"] == 1
    assert result["losses"] == 1
    assert result["pushes"] == 1
    assert result["average_odds"] == pytest.approx(2.5)
    assert result["strike_rate"] == pytest.approx(0.5)
    assert result["sample_state"] == "SUFFICIENT"


def test_calibration_excludes_push_half_and_void_states():
    rows = [
        _row(paper_trade_id="w", trade_state="WON", model_probability=0.70),
        _row(paper_trade_id="l", trade_state="LOST", model_probability=0.40),
        _row(paper_trade_id="p", trade_state="PUSH", model_probability=0.60),
        _row(paper_trade_id="hw", trade_state="HALF_WIN", model_probability=0.60),
    ]

    result = _calibration(rows)

    assert result["sample"] == 2
    assert result["brier_score"] == pytest.approx(((0.70 - 1.0) ** 2 + (0.40 - 0.0) ** 2) / 2)
    assert result["status"] == "INSUFFICIENT_SAMPLE"


def test_clv_requires_margin_adjusted_persisted_clv_and_complete_evidence():
    positive_clv = (2.50 / 2.00 / 1.05) - 1.0
    negative_clv = (1.80 / 2.00 / 1.05) - 1.0
    rows = [
        _row(
            paper_trade_id="positive",
            entry_odds=2.50,
            closing_odds=2.00,
            closing_snapshot_id=1,
            closing_margin=0.05,
            clv_pct=positive_clv,
        ),
        _row(
            paper_trade_id="negative",
            fixture_id="fixture-2",
            entry_odds=1.80,
            closing_odds=2.00,
            closing_snapshot_id=2,
            closing_margin=0.05,
            clv_pct=negative_clv,
        ),
        _row(
            paper_trade_id="missing-margin",
            fixture_id="fixture-3",
            entry_odds=3.00,
            closing_odds=2.00,
            closing_snapshot_id=3,
            closing_margin=None,
            clv_pct=None,
        ),
        _row(
            paper_trade_id="missing-snapshot",
            fixture_id="fixture-4",
            closing_snapshot_id=None,
            closing_margin=0.05,
            clv_pct=0.10,
        ),
    ]

    result = _clv(rows)

    assert result["sample"] == 2
    assert result["average_clv"] == pytest.approx((positive_clv + negative_clv) / 2)
    assert result["positive_rate"] == pytest.approx(0.5)
    assert result["status"] == "INSUFFICIENT_DATA"
    assert "closing_margin" in result["method_note"]


def test_buckets_are_deterministic():
    assert _edge_bucket(_row(edge=0.019)) == "0-2%"
    assert _edge_bucket(_row(edge=0.02)) == "2-5%"
    assert _edge_bucket(_row(edge=0.10)) == "10%+"
    assert _odds_bucket(_row(entry_odds=1.49)) == "<1.50"
    assert _odds_bucket(_row(entry_odds=1.50)) == "1.50-2.00"
    assert _odds_bucket(_row(entry_odds=3.00)) == "3.00+"


def test_health_fails_closed_when_samples_are_small():
    summary = {"bets": 33, "roi": 0.18}
    calibration = {"sample": 29, "expected_calibration_error": 0.02}
    clv = {"sample": 5, "average_clv": 0.03}

    result = _health(summary, calibration, clv)

    assert result["state"] == "INSUFFICIENT_SAMPLE"
    assert "More normalized settled bets" in result["reason"]
