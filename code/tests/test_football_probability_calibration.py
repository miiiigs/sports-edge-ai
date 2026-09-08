from __future__ import annotations

from sports_ai.dashboard.football_calibration import (
    MIN_TRAIN_SAMPLE,
    _fit_platt,
    _samples_from_rows,
    _walk_forward_shadow,
    apply_shadow_platt,
    build_shadow_calibration_payload,
)


def _row(index: int, *, probability: float, won: bool, fixture: str | None = None, market: str = "1x2"):
    return {
        "paper_trade_id": f"paper-{index}",
        "fixture_id": fixture or f"fixture-{index}",
        "_canonical_fixture_id": fixture or f"fixture-{index}",
        "market": market,
        "trade_state": "WON" if won else "LOST",
        "model_probability": probability,
        "entry_odds": 2.0,
        "edge": 0.06,
        "kickoff": f"2026-08-{1 + (index // 20):02d}T{index % 20:02d}:00:00+00:00",
    }


def test_platt_candidate_is_finite_and_bounded():
    rows = [
        _row(index, probability=0.30 if index % 2 == 0 else 0.70, won=index % 3 != 0)
        for index in range(100)
    ]
    samples = _samples_from_rows(rows)
    calibrator = _fit_platt(samples)

    assert calibrator is not None
    assert 0.0 < apply_shadow_platt(0.30, calibrator) < 1.0
    assert 0.0 < apply_shadow_platt(0.70, calibrator) < 1.0


def test_walk_forward_keeps_fixture_outcomes_out_of_own_fit():
    rows = []
    for index in range(MIN_TRAIN_SAMPLE):
        rows.append(_row(index, probability=0.40 + (0.20 if index % 2 else 0.0), won=index % 3 != 0))
    held_out_fixture = "held-out-fixture"
    for offset in range(5):
        rows.append(
            _row(
                MIN_TRAIN_SAMPLE + offset,
                probability=0.55,
                won=offset % 2 == 0,
                fixture=held_out_fixture,
            )
        )

    result = _walk_forward_shadow(_samples_from_rows(rows))

    assert result["sample"] == 5
    assert result["evaluated_fixtures"] == 1
    assert "before that fixture's outcomes" in result["leakage_guard"]


def test_shadow_payload_never_promotes_itself_to_runtime():
    rows = [
        _row(
            index,
            probability=0.25 if index % 4 == 0 else 0.45 if index % 4 == 1 else 0.60 if index % 4 == 2 else 0.75,
            won=(index * 7) % 10 < (2 if index % 4 == 0 else 5 if index % 4 == 1 else 6 if index % 4 == 2 else 7),
            market=("1x2", "btts", "goals_total", "corners_total", "asian_handicap")[index % 5],
        )
        for index in range(180)
    ]

    payload = build_shadow_calibration_payload(rows)

    assert payload["available"] is True
    assert payload["state"] == "SHADOW_ONLY"
    assert payload["runtime_applied"] is False
    assert payload["affects_bet_decisions"] is False
    assert payload["affects_thresholds"] is False
    assert payload["promotion"]["eligible"] is False
    assert payload["candidate"] is not None
    assert payload["candidate"]["effective_from"] is None
    assert payload["walk_forward"]["sample"] > 0


def test_non_decisive_rows_are_excluded_from_calibration():
    rows = [
        _row(1, probability=0.60, won=True),
        _row(2, probability=0.40, won=False),
        {**_row(3, probability=0.55, won=True), "trade_state": "PUSH"},
        {**_row(4, probability=0.55, won=True), "trade_state": "HALF_WIN"},
        {**_row(5, probability=0.55, won=False), "trade_state": "VOID"},
    ]

    samples = _samples_from_rows(rows)

    assert len(samples) == 2
    assert {sample["outcome"] for sample in samples} == {0, 1}
