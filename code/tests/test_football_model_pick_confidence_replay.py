"""Leakage-safety and semantics of the Model Pick confidence replay."""

from __future__ import annotations

import sports_ai.dashboard.services  # noqa: F401  (pre-import breaks the market_data/dashboard cycle)
import pytest
from sports_ai.experiments import football_model_pick_confidence_replay as rep


def _fx(event_id, season, hg=1, ag=0, corners=10, comp="ENG1", pin=None):
    return rep.Fixture(event_id, comp, season, hg, ag, corners, pin)


def _row(family, line, selection, fw, hw=0.0, pu=0.0, hl=0.0, fl=None):
    fl = 1.0 - fw - hw - pu - hl if fl is None else fl
    w, l = fw + 0.5 * hw, fl + 0.5 * hl
    return {
        "market_family": family,
        "line": line,
        "selection": selection,
        "fair_decimal_price": 1.0 + l / w,
        "effective_win_stake_probability": w,
    }


def _pick(season, q, state, market="asian_handicap"):
    return {"event_id": f"{season}-{q}-{state}", "competition_id": "ENG1", "season_code": season,
            "market": market, "selection": "HOME", "line": 0.0, "q": q, "fair": 1 / q, "state": state}


def test_conditional_probability_excludes_push_mass():
    row = _row("asian_handicap", 0.0, "HOME", fw=0.3973, pu=0.2641, fl=0.3386)
    assert row["effective_win_stake_probability"] == pytest.approx(0.3973)
    assert rep.conditional_probability(row) == pytest.approx(0.3973 / (0.3973 + 0.3386))


def test_cohort_stake_share_counts_half_results_and_ignores_pushes():
    c = rep.Cohort()
    for state in ("WON", "HALF_WIN", "PUSH", "HALF_LOSS", "LOST"):
        c.add(state, 0.5, 2.0)
    # won = 1 + 0.5, lost = 0.5 + 1
    assert c.win_share == pytest.approx(0.5)
    assert c.pushes == 1 and c.n == 5


def test_balanced_policy_uses_the_production_policy(monkeypatch):
    calls = []
    real = rep.select_model_picks

    def spy(rows):
        calls.append(len(rows))
        return real(rows)

    monkeypatch.setattr(rep, "select_model_picks", spy)
    rows = {"e1": [_row("asian_handicap", 0.0, "HOME", 0.45, pu=0.1), _row("asian_handicap", 0.0, "AWAY", 0.45, pu=0.1),
                   _row("corner_total", 9.5, "OVER", 0.52), _row("corner_total", 9.5, "UNDER", 0.48)]}
    picks = rep.balanced_policy_picks(rows, {"e1": _fx("e1", "2223", hg=1, ag=0, corners=11)})
    assert calls == [4]
    by_market = {p["market"]: p for p in picks}
    assert by_market["asian_handicap"]["state"] == "WON"  # HOME 0.0 at 1-0
    assert by_market["corner_total"]["selection"] == "OVER" and by_market["corner_total"]["state"] == "WON"


def test_threshold_choice_never_reads_holdout_outcomes():
    selection = [_pick("2223", 0.60, "WON") for _ in range(150)] + [_pick("2223", 0.60, "LOST") for _ in range(50)]
    selection += [_pick("2223", 0.52, "LOST") for _ in range(300)]
    holdout_good = [_pick("2425", 0.60, "WON") for _ in range(300)]
    holdout_bad = [_pick("2425", 0.60, "LOST") for _ in range(300)]
    kw = dict(market="asian_handicap", selection={"2223"}, target=0.70, min_selected=200)
    chosen = rep.choose_threshold(selection + holdout_good, **kw)
    assert chosen == 0.54  # lowest catalog threshold that isolates the 75%-win selection cohort
    assert rep.choose_threshold(selection + holdout_bad, **kw) == chosen


def test_threshold_is_none_when_selection_seasons_never_reach_the_target():
    picks = [_pick("2223", 0.60, "WON") for _ in range(120)] + [_pick("2223", 0.60, "LOST") for _ in range(120)]
    assert rep.choose_threshold(picks, market="asian_handicap", selection={"2223"}, target=0.65) is None


def test_temperature_is_fitted_on_selection_samples_only():
    # Overconfident selection data: q=0.8 but only 60% win -> temperature > 1.
    sel = [(0.8, 1.0, 0.0)] * 60 + [(0.8, 0.0, 1.0)] * 40
    t = rep.fit_temperature(sel)
    assert t > 1.0
    before = rep.calibration(sel)["log_loss"]
    after = rep.calibration(sel, t)["log_loss"]
    assert after < before


def test_overlapping_selection_and_holdout_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        rep.run_replay(day10_prices=tmp_path / "x", research_db=tmp_path / "y",
                       selection_seasons=("2223",), holdout_seasons=("2223",), rows_by_fixture={}, fixtures={})


def test_run_replay_end_to_end_on_synthetic_data(tmp_path):
    rows, fixtures = {}, {}
    pin = {"HOME": 2.0, "DRAW": 3.4, "AWAY": 4.0}
    for i, season in enumerate(["2223"] * 6 + ["2425"] * 6):
        e = f"e{i}"
        rows[e] = [
            _row("asian_handicap", -0.25, "HOME", 0.40, hw=0.12, hl=0.10, fl=0.38),
            _row("asian_handicap", -0.25, "AWAY", 0.38, hw=0.10, hl=0.12, fl=0.40),
            _row("asian_handicap", 1.0, "HOME", 0.70, pu=0.15, fl=0.15),
            _row("asian_handicap", 1.0, "AWAY", 0.15, pu=0.15, fl=0.70),
            _row("corner_total", 9.5, "OVER", 0.55), _row("corner_total", 9.5, "UNDER", 0.45),
            _row("1x2", None, "HOME", 0.48), _row("1x2", None, "DRAW", 0.27), _row("1x2", None, "AWAY", 0.25),
        ]
        fixtures[e] = _fx(e, season, hg=i % 3, ag=1, corners=8 + i % 4, pin=pin)
    out = rep.run_replay(day10_prices=tmp_path / "unused", research_db=tmp_path / "unused",
                         selection_seasons=("2223",), holdout_seasons=("2425",), rows_by_fixture=rows, fixtures=fixtures)
    assert out["balanced_policy"]["asian_handicap"]["all"]["n"] == 12
    assert out["balanced_policy"]["corner_total"]["all"]["n"] == 12
    assert set(out["threshold_sweep"]) == {"asian_handicap", "corner_total"}
    assert out["model_vs_market_1x2"]["available"] is True
    assert out["probability_semantics"].startswith("q = 1 / fair")
    assert out["corner_temperature"]["temperature_chosen_on_selection"] in rep.TEMPERATURE_GRID
