from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np

from sports_ai.agents.football import route_a_classifier


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _artifact_path() -> Path:
    return _repo_root() / "research" / "football_research" / "football_route_a_direct_classifier_2026-08-27_artifact.json"


def _manifest_path() -> Path:
    return _repo_root() / "research" / "football_research" / "football_route_a_direct_classifier_2026-08-27_manifest.json"


def _route_a_artifact() -> dict[str, object]:
    return json.loads(_artifact_path().read_text(encoding="utf-8"))


def _route_a_manifest() -> dict[str, object]:
    return json.loads(_manifest_path().read_text(encoding="utf-8"))


def _runtime_artifact(
    *,
    validation_status: str = route_a_classifier.ROUTE_A_VALIDATION_STATUS_PASSED,
    review_status: str = route_a_classifier.ROUTE_A_REVIEW_STATUS_APPROVED,
    btts_status: str = route_a_classifier.ROUTE_A_MARKET_STATUS_VALIDATED,
    ou25_status: str = route_a_classifier.ROUTE_A_MARKET_STATUS_VALIDATED,
) -> dict[str, object]:
    artifact = _route_a_artifact()
    artifact["validation"]["validation_status"] = validation_status
    artifact["validation"]["review_status"] = review_status
    artifact["validation"]["market_validation_status"] = {
        route_a_classifier.MARKET_KEY_BTTS: btts_status,
        route_a_classifier.MARKET_KEY_OU25: ou25_status,
    }
    artifact["markets"][route_a_classifier.MARKET_KEY_BTTS]["market_validation_status"] = btts_status
    artifact["markets"][route_a_classifier.MARKET_KEY_OU25]["market_validation_status"] = ou25_status
    artifact["overall_route_a_status"] = (
        route_a_classifier.ROUTE_A_REVIEW_STATUS_PENDING
        if review_status != route_a_classifier.ROUTE_A_REVIEW_STATUS_APPROVED
        else "ACTIVE"
    )
    return artifact


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


def test_committed_route_a_artifact_is_rejected_after_clean_remediation() -> None:
    artifact = _route_a_artifact()

    assert artifact["schema_version"] == route_a_classifier.ROUTE_A_ARTIFACT_SCHEMA_VERSION
    assert artifact["validation"]["validation_status"] == route_a_classifier.ROUTE_A_VALIDATION_STATUS_FAILED
    assert artifact["validation"]["market_validation_status"] == {
        route_a_classifier.MARKET_KEY_BTTS: route_a_classifier.ROUTE_A_MARKET_STATUS_REJECTED,
        route_a_classifier.MARKET_KEY_OU25: route_a_classifier.ROUTE_A_MARKET_STATUS_REJECTED,
    }
    assert artifact["overall_route_a_status"] == route_a_classifier.ROUTE_A_RUNTIME_STATUS_REJECTED


def test_route_a_clean_windows_do_not_look_forward() -> None:
    artifact = _route_a_artifact()
    season_order = {season: index for index, season in enumerate(artifact["validation"]["seasons"])}

    for market_result in artifact["validation"]["market_results"].values():
        for fold in market_result["folds"]:
            test_index = season_order[fold["test_season"]]
            for train_season in fold["train_seasons"]:
                assert season_order[train_season] < test_index
            for calibration_season in fold["calibration_seasons"]:
                assert season_order[calibration_season] < test_index


def test_route_a_manifest_and_artifact_contract_validate() -> None:
    artifact = _route_a_artifact()
    manifest = _route_a_manifest()

    artifact_valid, artifact_error = route_a_classifier.validate_route_a_artifact_payload(artifact)
    manifest_valid, manifest_error = route_a_classifier.validate_route_a_manifest_payload(manifest)
    bundle_valid, bundle_error = route_a_classifier.verify_route_a_manifest_against_artifact(
        manifest=manifest,
        artifact=artifact,
        artifact_path=_artifact_path(),
        artifact_sha256=manifest["artifact_sha256"],
    )

    assert artifact_valid is True, artifact_error
    assert manifest_valid is True, manifest_error
    assert bundle_valid is True, bundle_error


def test_checksum_mismatch_blocks_manifest_verification() -> None:
    bundle_valid, bundle_error = route_a_classifier.verify_route_a_manifest_against_artifact(
        manifest=_route_a_manifest(),
        artifact=_route_a_artifact(),
        artifact_path=_artifact_path(),
        artifact_sha256="0" * 64,
    )

    assert bundle_valid is False
    assert bundle_error == "artifact checksum mismatch"


def test_fake_active_json_cannot_validate_as_runtime_artifact() -> None:
    active_only = {
        "overall_route_a_status": "ACTIVE",
        "validation": {"overall_route_a_status": "ACTIVE"},
    }
    artifact_valid, artifact_error = route_a_classifier.validate_route_a_artifact_payload(active_only)

    assert artifact_valid is False
    assert artifact_error == "artifact schema_version mismatch"


def test_rejected_markets_block_route_a_predictions() -> None:
    artifact = _route_a_artifact()

    for market_key in (route_a_classifier.MARKET_KEY_BTTS, route_a_classifier.MARKET_KEY_OU25):
        payload = route_a_classifier.predict_route_a_market(
            artifact,
            market_key=market_key,
            competition_id="ENG1",
            predicted_home_shots=13.2,
            predicted_away_shots=10.9,
            predicted_home_corners=5.6,
            predicted_away_corners=4.3,
            as_of="2026-08-27T00:00:00+00:00",
        )
        assert payload["prediction_status"] == "BLOCKED"
        assert payload["block_reason"] == "MARKET_REJECTED"


def test_route_a_predicts_with_approved_runtime_contract() -> None:
    artifact = _runtime_artifact()

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


def test_route_a_missing_gap_inputs_block_prediction_when_runtime_is_approved() -> None:
    artifact = _runtime_artifact()

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


def test_route_a_market_capability_truth_matches_pending_and_rejected_states() -> None:
    pending_artifact = _runtime_artifact(
        review_status=route_a_classifier.ROUTE_A_REVIEW_STATUS_PENDING,
    )
    rejected_artifact = _route_a_artifact()

    assert route_a_classifier.summarize_market_capabilities(pending_artifact) == {
        "1x2": "NOT_SUPPORTED",
        "btts": route_a_classifier.ROUTE_A_RUNTIME_STATUS_PENDING,
        "goals_total": route_a_classifier.ROUTE_A_RUNTIME_STATUS_PENDING,
        "asian_handicap": "NOT_SUPPORTED",
        "corner_total": "NOT_SUPPORTED",
    }
    assert route_a_classifier.summarize_market_capabilities(rejected_artifact) == {
        "1x2": "NOT_SUPPORTED",
        "btts": route_a_classifier.ROUTE_A_RUNTIME_STATUS_REJECTED,
        "goals_total": route_a_classifier.ROUTE_A_RUNTIME_STATUS_REJECTED,
        "asian_handicap": "NOT_SUPPORTED",
        "corner_total": "NOT_SUPPORTED",
    }


def test_route_a_unsupported_market_is_not_applicable() -> None:
    payload = route_a_classifier.predict_route_a_market(
        _runtime_artifact(),
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
