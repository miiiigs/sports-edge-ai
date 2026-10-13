"""High-confidence AH benchmark: market inversion, lane rules and paired comparison."""

from __future__ import annotations

import sports_ai.dashboard.services  # noqa: F401  (pre-import breaks the market_data/dashboard cycle)
import pytest
from sports_ai.experiments import football_high_confidence_benchmark as hc
from sports_ai.experiments import football_model_pick_confidence_replay as rep


@pytest.mark.parametrize("ph,pa", [(0.45, 0.28), (0.70, 0.12), (0.20, 0.55), (0.38, 0.38)])
def test_poisson_inversion_reproduces_market_1x2(ph, pa):
    lh, la = hc.solve_poisson_means(ph, pa)
    h, a = hc._outcome_probs(lh, la)
    assert h == pytest.approx(ph, abs=1e-6) and a == pytest.approx(pa, abs=1e-6)


def test_market_ah_rows_are_coherent():
    rows = hc.market_ah_rows({"HOME": 1.80, "DRAW": 3.70, "AWAY": 4.60})
    by = {(r["line"], r["selection"]): r for r in rows}
    for line in hc.AH_LINES:
        assert by[(line, "HOME")]["q"] + by[(line, "AWAY")]["q"] == pytest.approx(1.0)
        assert by[(line, "HOME")]["fair"] == pytest.approx(1.0 / by[(line, "HOME")]["q"])
    home = [by[(line, "HOME")]["q"] for line in hc.AH_LINES]
    assert home == sorted(home)  # more handicap help -> higher home q


def _row(line, sel, q):
    return {"market_family": "asian_handicap", "line": line, "selection": sel, "fair_decimal_price": 1 / q,
            "effective_win_stake_probability": q * 0.9}


def test_lanes_pick_one_line_per_fixture_and_respect_history_gate():
    rows = {
        "e1": [_row(-0.5, "HOME", 0.55), _row(-0.5, "AWAY", 0.45), _row(0.5, "HOME", 0.74), _row(0.5, "AWAY", 0.26),
               _row(1.0, "HOME", 0.82), _row(1.0, "AWAY", 0.18)],
        "e2": [_row(0.5, "HOME", 0.74), _row(0.5, "AWAY", 0.26)],
    }
    pin = {"HOME": 2.0, "DRAW": 3.5, "AWAY": 3.9}
    fixtures = {e: rep.Fixture(e, "ENG1", "2425", 1, 1, 10, pin) for e in rows}
    lanes = hc.build_lanes(rows, fixtures, {"e1": 5, "e2": 1}, seasons={"2425"}, match_dates={"e1": "d1", "e2": "d1"})
    model = lanes["MODEL_HC"]
    assert [p["event_id"] for p in model] == ["e1"]  # e2 lacks venue history
    assert (model[0]["line"], model[0]["selection"]) == (0.5, "HOME")  # longest fair price with q >= 0.70
    assert model[0]["state"] == "WON"  # 1-1 with +0.5
    assert len(lanes["FAVOURITE_DNB"]) == 2 and len(lanes["MARKET_HC"]) == 2
    assert len(lanes["_ELIGIBLE"]) == 2


def test_paired_comparison_sign_and_identity():
    picks = [{"date": f"d{i // 4}", "q": 0.7, "market_q": 0.7, "state": "WON" if i % 10 < 7 else "LOST"} for i in range(200)]
    same = hc.paired_model_vs_market(picks)
    assert same["log_loss_difference_model_minus_market"] == pytest.approx(0.0)
    worse = hc.paired_model_vs_market([{**p, "q": 0.9} for p in picks])
    assert worse["log_loss_difference_model_minus_market"] > 0
    assert worse["difference_ci95_cluster_bootstrap_by_date"][0] > 0
