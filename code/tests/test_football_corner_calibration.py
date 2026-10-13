"""Opt-in corner probability temperature: default no-op, coherent when enabled."""

from __future__ import annotations

import copy
import inspect
import math

import sports_ai.dashboard.services  # noqa: F401  (pre-import breaks the market_data/dashboard cycle)
import pytest
from sports_ai.dashboard import football_corner_calibration as cc
from sports_ai.dashboard import prediction_service
from sports_ai.experiments.football_day10_market_pricing import CORNER_TOTAL_LINES, price_corner_total_selection

_KEYS = ("fixture_id", "event_id", "competition_id", "competition_name", "season_code", "division_code",
         "match_date", "kickoff_time", "kickoff_at", "as_of", "day10_input_contract_version")


def _pricing_input(mean=10.4, distribution="negative_binomial", dispersion=40.0):
    base = {k: "x" for k in _KEYS}
    base["model_versions"] = {}
    base["corner_object"] = {
        "object_id": "corner_obj",
        "predicted_total_corners": mean,
        "selected_corner_distribution": distribution,
        "dispersion": dispersion,
    }
    return base


def _rows(**kw):
    pi = _pricing_input(**kw)
    return [price_corner_total_selection(pi, line=line, selection=sel) for line in CORNER_TOTAL_LINES for sel in ("OVER", "UNDER")]


def _q(row):
    return 1.0 / row["fair_decimal_price"]


@pytest.mark.parametrize("raw,expected", [(None, 1.0), ("", 1.0), ("1.2", 1.2), ("2.0", 2.0), ("0.8", 1.0), ("2.5", 1.0), ("nan", 1.0), ("abc", 1.0)])
def test_resolve_accepts_only_softening_temperatures(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(cc.CORNER_TEMPERATURE_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(cc.CORNER_TEMPERATURE_ENV_VAR, raw)
    assert cc.resolve_corner_temperature() == expected


def test_default_temperature_leaves_rows_identical():
    rows = _rows()
    before = copy.deepcopy(rows)
    assert [cc.apply_corner_temperature(r, 1.0) for r in rows] == before


def test_temperature_pulls_probabilities_toward_half_and_keeps_rows_coherent():
    rows = _rows()
    tempered = [cc.apply_corner_temperature(r, 1.2) for r in rows]
    by_key = {(r["line"], r["selection"]): r for r in tempered}
    for raw, new in zip(rows, tempered):
        q0, q1 = _q(raw), _q(new)
        assert abs(q1 - 0.5) <= abs(q0 - 0.5) + 1e-12
        sp = new["settlement_probabilities"]
        assert math.isclose(sum(sp.values()), 1.0, abs_tol=1e-12)
        assert sp["push_probability"] == raw["settlement_probabilities"]["push_probability"]
        old = raw["settlement_probabilities"]
        if old["half_win_probability"] > 0:
            assert sp["half_win_probability"] / sp["full_win_probability"] == pytest.approx(
                old["half_win_probability"] / old["full_win_probability"]
            )
        assert new["effective_win_stake_probability"] == pytest.approx(sp["full_win_probability"] + 0.5 * sp["half_win_probability"])
        assert new["numerical_metadata"]["corner_probability_temperature"] == 1.2
        assert new["numerical_metadata"]["corner_untempered_fair_decimal_price"] == raw["fair_decimal_price"]
    for line in CORNER_TOTAL_LINES:
        assert _q(by_key[(line, "OVER")]) + _q(by_key[(line, "UNDER")]) == pytest.approx(1.0)


def test_tempered_q_matches_the_validated_logit_formula():
    row = _rows()[0]
    q = _q(row)
    expected = 1 / (1 + math.exp(-math.log(q / (1 - q)) / 1.2))
    assert _q(cc.apply_corner_temperature(row, 1.2)) == pytest.approx(expected)


def test_monotonicity_across_lines_is_preserved():
    tempered = [cc.apply_corner_temperature(r, 1.2) for r in _rows() if r["selection"] == "OVER"]
    qs = [_q(r) for r in tempered]
    assert qs == sorted(qs, reverse=True)


def test_non_corner_rows_are_untouched():
    row = {"market_family": "asian_handicap", "fair_decimal_price": 1.9}
    assert cc.apply_corner_temperature(row, 1.2) == row


def test_live_prediction_path_applies_the_resolved_temperature():
    source = inspect.getsource(prediction_service.build_prediction_for_fixture)
    assert "resolve_corner_temperature()" in source
    assert "apply_corner_temperature(row, corner_temperature)" in source


@pytest.mark.parametrize("mean", [7.5, 9.1, 10.4, 12.8])
def test_balanced_line_model_pick_selection_is_unchanged(mean):
    from sports_ai.dashboard.model_pick_policy import select_model_picks

    def strip(rows):
        keep = ("market_family", "line", "selection", "fair_decimal_price", "effective_win_stake_probability")
        return [{k: r[k] for k in keep} for r in rows]

    rows = _rows(mean=mean)
    base = select_model_picks(strip(rows))["corner_total"]
    tempered = select_model_picks(strip([cc.apply_corner_temperature(r, 1.2) for r in rows]))["corner_total"]
    assert (tempered["line"], tempered["selection"]) == (base["line"], base["selection"])
    assert abs(1 / tempered["model_fair_price"] - 0.5) <= abs(1 / base["model_fair_price"] - 0.5)
