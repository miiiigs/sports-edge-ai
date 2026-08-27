from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from sports_ai.agents.football import route_a_classifier
from sports_ai.experiments.football_day8_goal_conversion import Day8JoinedRow


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _route_a_artifact() -> dict[str, object]:
    path = _repo_root() / "research" / "football_research" / "football_route_a_direct_classifier_2026-08-27_artifact.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_route_a_training_rows_reject_as_of_after_kickoff(monkeypatch) -> None:
    row = Day8JoinedRow(
        event_id="event-1",
        competition_id="ENG1",
        competition_name="Premier League",
        season_code="2526",
        division_code="E0",
        match_date="2026-08-27",
        kickoff_time="20:00",
        as_of="2026-08-27T20:00:01+00:00",
        home_team_id="home",
        away_team_id="away",
        gap_model_version_shots="shots_v1",
        gap_model_version_shots_on_target="sot_v1",
        gap_model_version_corners="corners_v1",
        predicted_home_shots=14.0,
        predicted_away_shots=10.0,
        predicted_home_shots_on_target=5.0,
        predicted_away_shots_on_target=3.0,
        predicted_home_corners=6.0,
        predicted_away_corners=4.0,
        home_team_pre_match_competition_matches=10,
        away_team_pre_match_competition_matches=10,
        actual_home_goals=2,
        actual_away_goals=1,
        actual_home_corners=6,
        actual_away_corners=4,
    )

    monkeypatch.setattr(route_a_classifier, "_load_day7_selected_gap_results", lambda **kwargs: ({}, {}))
    monkeypatch.setattr(route_a_classifier, "_join_day7_predictions", lambda **kwargs: [row])

    try:
        route_a_classifier.build_route_a_training_rows(research_db_path="unused.sqlite")
    except ValueError as exc:
        assert "chronology violation" in str(exc)
    else:  # pragma: no cover - explicit failure branch
        raise AssertionError("expected chronology violation")


def test_route_a_logistic_training_is_deterministic() -> None:
    x_matrix = np.asarray(
        [
            [1.0, -1.0, 0.5],
            [1.0, 0.0, -0.5],
            [1.0, 1.0, 0.75],
            [1.0, 2.0, 1.25],
        ],
        dtype=np.float64,
    )
    y_vector = np.asarray([0.0, 0.0, 1.0, 1.0], dtype=np.float64)

    first = route_a_classifier._fit_logistic_regression(x_matrix, y_vector)
    second = route_a_classifier._fit_logistic_regression(x_matrix, y_vector)

    assert np.allclose(first, second)


def test_route_a_artifact_is_active_for_btts_and_ou25() -> None:
    artifact = _route_a_artifact()

    assert artifact["overall_route_a_status"] == "ACTIVE"
    assert artifact["validation"]["market_results"]["btts"]["accepted"] is True
    assert artifact["validation"]["market_results"]["goals_total_2.5"]["accepted"] is True


def test_route_a_calibration_windows_do_not_look_forward() -> None:
    artifact = _route_a_artifact()
    season_order = {season: index for index, season in enumerate(artifact["validation"]["seasons"])}

    for market_result in artifact["validation"]["market_results"].values():
        for fold in market_result["folds"]:
            test_index = season_order[fold["test_season"]]
            for train_season in fold["train_seasons"]:
                assert season_order[train_season] < test_index
            for calibration_season in fold["calibration_seasons"]:
                assert season_order[calibration_season] < test_index
                assert calibration_season in fold["train_seasons"]


def test_route_a_predicts_probabilities_within_unit_interval() -> None:
    artifact = _route_a_artifact()

    btts = route_a_classifier.predict_route_a_market(
        artifact,
        market_key=route_a_classifier.MARKET_KEY_BTTS,
        competition_id="ENG1",
        predicted_home_shots=13.2,
        predicted_away_shots=10.9,
        predicted_home_corners=5.6,
        predicted_away_corners=4.3,
        as_of="2026-08-27T00:00:00+00:00",
    )
    ou25 = route_a_classifier.predict_route_a_market(
        artifact,
        market_key=route_a_classifier.MARKET_KEY_OU25,
        competition_id="ENG1",
        predicted_home_shots=13.2,
        predicted_away_shots=10.9,
        predicted_home_corners=5.6,
        predicted_away_corners=4.3,
        as_of="2026-08-27T00:00:00+00:00",
    )

    for payload in (btts, ou25):
        assert payload["prediction_status"] == "PREDICTED"
        assert 0.0 <= payload["positive_probability"] <= 1.0
        assert 0.0 <= payload["negative_probability"] <= 1.0


def test_route_a_unsupported_market_is_not_applicable() -> None:
    artifact = _route_a_artifact()

    payload = route_a_classifier.predict_route_a_market(
        artifact,
        market_key="1x2",
        competition_id="ENG1",
        predicted_home_shots=13.2,
        predicted_away_shots=10.9,
        predicted_home_corners=5.6,
        predicted_away_corners=4.3,
        as_of="2026-08-27T00:00:00+00:00",
    )

    assert payload["prediction_status"] == "NOT_APPLICABLE"
    assert payload["block_reason"] == "MARKET_NOT_SUPPORTED"


def test_route_a_missing_gap_inputs_block_prediction() -> None:
    artifact = _route_a_artifact()

    payload = route_a_classifier.predict_route_a_market(
        artifact,
        market_key=route_a_classifier.MARKET_KEY_BTTS,
        competition_id="ENG1",
        predicted_home_shots=None,
        predicted_away_shots=10.9,
        predicted_home_corners=5.6,
        predicted_away_corners=4.3,
        as_of="2026-08-27T00:00:00+00:00",
    )

    assert payload["prediction_status"] == "BLOCKED"
    assert payload["block_reason"] == "MISSING_GAP_FEATURES"


def test_route_a_market_capability_truth_matches_active_artifact() -> None:
    artifact = _route_a_artifact()

    capabilities = route_a_classifier.summarize_market_capabilities(artifact)

    assert capabilities == {
        "1x2": "NOT_SUPPORTED",
        "btts": "SUPPORTED",
        "goals_total": "SUPPORTED",
        "asian_handicap": "NOT_SUPPORTED",
        "corner_total": "NOT_SUPPORTED",
    }
