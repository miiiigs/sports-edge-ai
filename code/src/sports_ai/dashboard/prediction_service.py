"""Server-side frozen pre-match football prediction generation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..agents.football.route_a_classifier import (
    ROUTE_A_AGENT_ID,
    ROUTE_A_AGENT_VERSION_TAG,
    build_route_a_market_rows,
)
from ..experiments.football_day10_market_pricing import (
    DAY10_INPUT_CONTRACT_VERSION,
    DAY7_BASELINE_EXPERIMENT_VERSION,
    DAY8_EXPERIMENT_VERSION,
    DAY8_TO_DAY10_CORNER_INTERFACE_POLICY_ID,
    DAY9_EXPERIMENT_VERSION,
    DAY9_TO_DAY10_INTERFACE_POLICY_ID,
    SANITIZED_INPUT_POLICY_ID,
    derive_corner_markets_for_fixture,
    derive_goals_markets_for_fixture,
)
from ..experiments.football_day9_scoreline_model import (
    MAX_GOALS,
    SCORELINE_MODEL_VERSION,
    build_dixon_coles_scoreline_distribution,
)
from ..locking import canonical_hash
from .models import (
    FIXTURE_TERMINAL_STATUSES,
    PREDICTION_STATUS_BLOCKED,
    PREDICTION_STATUS_FAILED,
    PREDICTION_STATUS_READY,
    NormalizedFixture,
    utc_now_iso,
)
from .snapshot_service import CurrentFootballModelSnapshot

try:
    from ..experiments.football_day8_goal_conversion import CONVERSION_MODEL_VERSION, _predict_expected_goals
except ImportError:  # pragma: no cover - defensive only
    CONVERSION_MODEL_VERSION = "football_day8_goal_conversion_v1"


@dataclass(frozen=True)
class PredictionContext:
    fixture: NormalizedFixture
    snapshot: CurrentFootballModelSnapshot


# How the live snapshot fits its parameters. The replay (#116) uses prior-fold fits
# and out-of-sample rho; the live snapshot refits on the expanding window of every
# completed research match, all of which predate any fixture it prices.
MODEL_PARAMETER_FIT_PROTOCOL = "expanding_window_all_completed_research_matches_v1"


def _model_parameters(snapshot: CurrentFootballModelSnapshot, competition_id: str, *, rho: float, xi: float) -> dict[str, Any]:
    """Reproducibility record of the exact fitted parameters used for this prediction."""
    return {
        "fit_protocol": MODEL_PARAMETER_FIT_PROTOCOL,
        "research_cutoff": snapshot.model_as_of,
        "model_snapshot_id": snapshot.snapshot_id,
        "conversion_coefficients": snapshot.conversion_coefficients[competition_id],
        "rho": rho,
        "rho_source": "in_sample_mle_expanding_window",
        "xi": xi,
        "xi_source": "day9_manifest",
        "corner_model": dict(snapshot.corner_models.get(competition_id) or {}),
    }


def _top_scorelines(matrix: list[list[float]], *, limit: int = 5) -> list[dict[str, Any]]:
    scorelines = [
        {
            "home_goals": home_goals,
            "away_goals": away_goals,
            "probability": float(probability),
        }
        for home_goals, row in enumerate(matrix)
        for away_goals, probability in enumerate(row)
    ]
    return sorted(
        scorelines,
        key=lambda row: (-row["probability"], row["home_goals"], row["away_goals"]),
    )[:limit]


def _market_index(rows: list[dict[str, Any]]) -> dict[tuple[str, float | None, str], dict[str, Any]]:
    return {
        (
            str(row["market_family"]),
            None if row.get("line") is None else float(row["line"]),
            str(row["selection"]),
        ): row
        for row in rows
    }


def _representative_fair_prices(market_rows: list[dict[str, Any]]) -> dict[str, Any]:
    index = _market_index(market_rows)

    def fetch(market_family: str, line: float | None, selection: str) -> dict[str, Any] | None:
        return index.get((market_family, line, selection))

    return {
        "1x2": {
            "HOME": fetch("1x2", None, "HOME"),
            "DRAW": fetch("1x2", None, "DRAW"),
            "AWAY": fetch("1x2", None, "AWAY"),
        },
        "btts": {
            "YES": fetch("btts", None, "YES"),
            "NO": fetch("btts", None, "NO"),
        },
        "goals_total_2_5": {
            "OVER": fetch("goals_total", 2.5, "OVER"),
            "UNDER": fetch("goals_total", 2.5, "UNDER"),
        },
        "asian_handicap_home_minus_0_5": fetch("asian_handicap", -0.5, "HOME"),
        "corner_total_9_5": {
            "OVER": fetch("corner_total", 9.5, "OVER"),
            "UNDER": fetch("corner_total", 9.5, "UNDER"),
        },
    }


def _blocked_prediction(
    *,
    fixture: NormalizedFixture,
    generated_at: str,
    blockers: list[str] | None = None,
    blocker_reason: str | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    resolved_blockers = list(blockers or ([] if blocker_reason is None else [blocker_reason]))
    version_key = canonical_hash(
        {
            "fixture_id": fixture.fixture_id,
            "status": PREDICTION_STATUS_BLOCKED,
            "reason": resolved_blockers,
        }
    )
    return {
        "prediction_id": f"prediction_{version_key}",
        "fixture_id": fixture.fixture_id,
        "generated_at": generated_at,
        "model_as_of": None,
        "model_snapshot_id": None,
        "prediction_version": version_key,
        "prediction_locked_at": fixture.kickoff_utc,
        "version_key": version_key,
        "prediction_status": PREDICTION_STATUS_BLOCKED,
        "blockers": resolved_blockers,
        "warnings": warnings or [],
        "manager_decision_status": "MARKET_DATA_NOT_CONNECTED",
        "safeguard_status": "BLOCKED",
        "safeguard_reason": "POINT-IN-TIME XG NOT AVAILABLE",
    }


def _route_b_shadow_agent(prediction: dict[str, Any]) -> dict[str, Any]:
    prediction_status = str(prediction.get("prediction_status") or PREDICTION_STATUS_BLOCKED)
    statuses = {
        PREDICTION_STATUS_READY: "PREDICTED",
        PREDICTION_STATUS_BLOCKED: "BLOCKED",
        PREDICTION_STATUS_FAILED: "BLOCKED",
    }
    if statuses.get(prediction_status, "NOT_APPLICABLE") == "PREDICTED":
        return {
            "agent_id": "ROUTE_B_COMPOSITE",
            "agent_version_tag": "football_edge_desk_foundation_v1",
            "prediction_status": "PREDICTED",
            "generated_at": prediction.get("generated_at"),
            "model_snapshot_id": prediction.get("model_snapshot_id"),
            "source_prediction_version": prediction.get("version_key") or prediction.get("prediction_id"),
            "market_rows": list(prediction.get("market_rows") or []),
            "metadata": {},
        }
    return {
        "agent_id": "ROUTE_B_COMPOSITE",
        "agent_version_tag": "football_edge_desk_foundation_v1",
        "prediction_status": statuses.get(prediction_status, "NOT_APPLICABLE"),
        "generated_at": prediction.get("generated_at"),
        "model_snapshot_id": prediction.get("model_snapshot_id"),
        "source_prediction_version": prediction.get("version_key") or prediction.get("prediction_id"),
        "model_version": (prediction.get("model_versions") or {}).get("current_snapshot_model_version"),
        "model_as_of": prediction.get("model_as_of"),
        "blockers": list(prediction.get("blockers") or []),
        "warnings": list(prediction.get("warnings") or []),
        "market_contexts": [
            {"market_family": "1x2", "line": None},
            {"market_family": "asian_handicap", "line": None},
            {"market_family": "btts", "line": None},
            {"market_family": "corner_total", "line": None},
            {"market_family": "goals_total", "line": None},
        ],
        "metadata": {},
    }


def _route_a_shadow_agent(
    *,
    fixture: NormalizedFixture,
    prediction: dict[str, Any],
    snapshot: CurrentFootballModelSnapshot | None,
) -> dict[str, Any]:
    if snapshot is None:
        return {
            "agent_id": ROUTE_A_AGENT_ID,
            "agent_version_tag": ROUTE_A_AGENT_VERSION_TAG,
            "prediction_status": "BLOCKED",
            "generated_at": prediction.get("generated_at"),
            "source_prediction_version": prediction.get("version_key") or prediction.get("prediction_id"),
            "blockers": ["MODEL_SNAPSHOT_UNAVAILABLE"],
            "market_contexts": [
                {"market_family": "btts", "line": None},
                {"market_family": "goals_total", "line": 2.5},
            ],
            "metadata": {},
        }

    if str(prediction.get("prediction_status")) != PREDICTION_STATUS_READY:
        return {
            "agent_id": ROUTE_A_AGENT_ID,
            "agent_version_tag": ROUTE_A_AGENT_VERSION_TAG,
            "prediction_status": "BLOCKED",
            "generated_at": prediction.get("generated_at"),
            "model_snapshot_id": prediction.get("model_snapshot_id"),
            "source_prediction_version": prediction.get("version_key") or prediction.get("prediction_id"),
            "model_version": None if snapshot.route_a_artifact is None else snapshot.route_a_artifact.get("agent_version_tag"),
            "model_as_of": prediction.get("model_as_of") or snapshot.model_as_of,
            "blockers": list(prediction.get("blockers") or ["ROUTE_A_UPSTREAM_BLOCKED"]),
            "warnings": list(prediction.get("warnings") or []),
            "market_contexts": [
                {"market_family": "btts", "line": None},
                {"market_family": "goals_total", "line": 2.5},
            ],
            "metadata": {
                "artifact_id": None if snapshot.route_a_artifact is None else snapshot.route_a_artifact.get("artifact_id"),
            },
        }

    route_a_prediction = build_route_a_market_rows(
        snapshot.route_a_artifact,
        competition_id=fixture.competition_id,
        predicted_home_shots=prediction.get("expected_home_shots"),
        predicted_away_shots=prediction.get("expected_away_shots"),
        predicted_home_corners=prediction.get("expected_home_corners"),
        predicted_away_corners=prediction.get("expected_away_corners"),
        as_of=str(prediction.get("model_as_of") or snapshot.model_as_of),
    )
    return {
        "agent_id": ROUTE_A_AGENT_ID,
        "agent_version_tag": ROUTE_A_AGENT_VERSION_TAG,
        "prediction_status": str(route_a_prediction.get("prediction_status") or "BLOCKED"),
        "generated_at": prediction.get("generated_at"),
        "model_snapshot_id": prediction.get("model_snapshot_id"),
        "source_prediction_version": prediction.get("version_key") or prediction.get("prediction_id"),
        "model_version": None if snapshot.route_a_artifact is None else snapshot.route_a_artifact.get("agent_version_tag"),
        "model_as_of": prediction.get("model_as_of") or snapshot.model_as_of,
        "blockers": list(route_a_prediction.get("blockers") or []),
        "warnings": [],
        "market_contexts": list(route_a_prediction.get("market_contexts") or []),
        "market_rows": list(route_a_prediction.get("market_rows") or []),
        "metadata": {
            "artifact_id": None if snapshot.route_a_artifact is None else snapshot.route_a_artifact.get("artifact_id"),
            "overall_route_a_status": None
            if snapshot.route_a_artifact is None
            else snapshot.route_a_artifact.get("overall_route_a_status"),
        },
    }


def attach_edge_desk_shadow_bundle(
    prediction: dict[str, Any],
    *,
    fixture: NormalizedFixture,
    snapshot: CurrentFootballModelSnapshot | None,
) -> dict[str, Any]:
    agents = [_route_b_shadow_agent(prediction)]
    if snapshot is not None and snapshot.route_a_artifact is not None:
        agents.append(_route_a_shadow_agent(fixture=fixture, prediction=prediction, snapshot=snapshot))
    payload = dict(prediction)
    payload["edge_desk_shadow_bundle"] = {
        "agents": agents
    }
    return payload


def _season_code_from_fixture(fixture: NormalizedFixture) -> str:
    season_text = str(fixture.season or "").strip()
    if season_text.isdigit() and len(season_text) == 4:
        return season_text
    return season_text or fixture.kickoff_utc[:4]


def build_prediction_for_fixture(context: PredictionContext) -> dict[str, Any]:
    fixture = context.fixture
    snapshot = context.snapshot
    generated_at = utc_now_iso()

    if fixture.competition_id not in snapshot.supported_competitions:
        return attach_edge_desk_shadow_bundle(
            _blocked_prediction(
                fixture=fixture,
                generated_at=generated_at,
                blocker_reason="COMPETITION_UNSUPPORTED",
            ),
            fixture=fixture,
            snapshot=snapshot,
        )

    home_resolution = snapshot.resolve_team(
        provider=fixture.provider,
        competition_id=fixture.competition_id,
        provider_team_id=fixture.home_team_id,
        provider_team_name=fixture.home_team_name,
    )
    away_resolution = snapshot.resolve_team(
        provider=fixture.provider,
        competition_id=fixture.competition_id,
        provider_team_id=fixture.away_team_id,
        provider_team_name=fixture.away_team_name,
    )
    if not home_resolution.is_resolved or not away_resolution.is_resolved:
        reasons = []
        if not home_resolution.is_resolved:
            reasons.append("HOME_TEAM_IDENTITY_UNRESOLVED")
        if not away_resolution.is_resolved:
            reasons.append("AWAY_TEAM_IDENTITY_UNRESOLVED")
        return attach_edge_desk_shadow_bundle(
            {
                **_blocked_prediction(
                    fixture=fixture,
                    generated_at=generated_at,
                    blockers=reasons,
                ),
                "team_resolution": {
                    "home": home_resolution.to_dict(),
                    "away": away_resolution.to_dict(),
                },
            },
            fixture=fixture,
            snapshot=snapshot,
        )

    home_history = snapshot.team_model_history_status(
        competition_id=fixture.competition_id,
        canonical_team_id=str(home_resolution.canonical_team_id),
        venue="home",
    )
    away_history = snapshot.team_model_history_status(
        competition_id=fixture.competition_id,
        canonical_team_id=str(away_resolution.canonical_team_id),
        venue="away",
    )
    if not home_history["available"] or not away_history["available"]:
        blockers = []
        if not home_history["available"]:
            blockers.append("HOME_MODEL_HISTORY_MISSING")
        if not away_history["available"]:
            blockers.append("AWAY_MODEL_HISTORY_MISSING")
        return attach_edge_desk_shadow_bundle(
            {
                **_blocked_prediction(
                    fixture=fixture,
                    generated_at=generated_at,
                    blockers=blockers,
                ),
                "team_resolution": {
                    "home": home_resolution.to_dict(),
                    "away": away_resolution.to_dict(),
                },
                "model_history": {
                    "home": home_history,
                    "away": away_history,
                },
            },
            fixture=fixture,
            snapshot=snapshot,
        )

    try:
        shots_state = context.snapshot.gap_engine.state_record(
            competition_id=fixture.competition_id,
            statistic="shots",
            team_id=str(home_resolution.canonical_team_id),
            venue="home",
        )
        shots_on_target_state = context.snapshot.gap_engine.state_record(
            competition_id=fixture.competition_id,
            statistic="shots_on_target",
            team_id=str(home_resolution.canonical_team_id),
            venue="home",
        )
        corners_state = context.snapshot.gap_engine.state_record(
            competition_id=fixture.competition_id,
            statistic="corners",
            team_id=str(home_resolution.canonical_team_id),
            venue="home",
        )
        shots = snapshot.gap_engine.predict_fixture(
            competition_id=fixture.competition_id,
            statistic="shots",
            home_team_id=str(home_resolution.canonical_team_id),
            away_team_id=str(away_resolution.canonical_team_id),
        )
        shots_on_target = snapshot.gap_engine.predict_fixture(
            competition_id=fixture.competition_id,
            statistic="shots_on_target",
            home_team_id=str(home_resolution.canonical_team_id),
            away_team_id=str(away_resolution.canonical_team_id),
        )
        corners = snapshot.gap_engine.predict_fixture(
            competition_id=fixture.competition_id,
            statistic="corners",
            home_team_id=str(home_resolution.canonical_team_id),
            away_team_id=str(away_resolution.canonical_team_id),
        )
        coefficients = snapshot.conversion_coefficients[fixture.competition_id]
        expected_goals_home = _predict_expected_goals(
            coefficients=coefficients["home"],
            predicted_shots=float(shots.expected_home),
            predicted_shots_on_target=float(shots_on_target.expected_home),
        )
        expected_goals_away = _predict_expected_goals(
            coefficients=coefficients["away"],
            predicted_shots=float(shots.expected_away),
            predicted_shots_on_target=float(shots_on_target.expected_away),
        )
        expected_total_goals = expected_goals_home + expected_goals_away
        rho = float(snapshot.rho_by_competition[fixture.competition_id])
        xi = float(snapshot.xi_by_competition.get(fixture.competition_id, 0.0))
        scoreline_distribution = build_dixon_coles_scoreline_distribution(
            expected_goals_home,
            expected_goals_away,
            rho=rho,
            max_goals=MAX_GOALS,
        )
    except Exception as exc:
        return attach_edge_desk_shadow_bundle(
            {
                **_blocked_prediction(
                    fixture=fixture,
                    generated_at=generated_at,
                    blocker_reason="MODEL_STATE_UNAVAILABLE",
                    warnings=[f"{type(exc).__name__}: {exc}"],
                ),
                "prediction_status": PREDICTION_STATUS_FAILED,
            },
            fixture=fixture,
            snapshot=snapshot,
        )

    scoreline_object_content = {
        "event_id": fixture.fixture_id,
        "competition_id": fixture.competition_id,
        "kickoff_at": fixture.kickoff_utc,
        "as_of": snapshot.model_as_of,
        "scoreline_model_version": SCORELINE_MODEL_VERSION,
        "conversion_model_version": CONVERSION_MODEL_VERSION,
        "gap_model_version_shots": shots.statistic,
        "gap_model_version_shots_on_target": shots_on_target.statistic,
        "expected_goals_home": expected_goals_home,
        "expected_goals_away": expected_goals_away,
        "expected_total_goals": expected_total_goals,
        "rho": rho,
        "xi": xi,
        "matrix_max_goals": int(scoreline_distribution["max_goals"]),
        "matrix": scoreline_distribution["matrix"],
        "tail_mass_beyond_max_goals": float(scoreline_distribution["tail_mass_beyond_max_goals"]),
        "raw_matrix_mass": float(scoreline_distribution["raw_matrix_mass"]),
        "normalization_factor": float(scoreline_distribution["normalization_factor"]),
        "normalization_error": float(scoreline_distribution["normalization_error"]),
        "full_distribution_mass_estimate": float(
            scoreline_distribution["raw_matrix_mass"] + scoreline_distribution["tail_mass_beyond_max_goals"]
        ),
        "full_distribution_mass_estimate_error": float(
            (scoreline_distribution["raw_matrix_mass"] + scoreline_distribution["tail_mass_beyond_max_goals"]) - 1.0
        ),
        "marginal_home_mean": float(scoreline_distribution["marginal_home_mean"]),
        "marginal_away_mean": float(scoreline_distribution["marginal_away_mean"]),
        "interface_policy_id": DAY9_TO_DAY10_INTERFACE_POLICY_ID,
    }
    scoreline_object_id = f"scoreline_{canonical_hash(scoreline_object_content)}"

    corner_model = snapshot.corner_models[fixture.competition_id]
    corner_object_content = {
        "event_id": fixture.fixture_id,
        "competition_id": fixture.competition_id,
        "kickoff_at": fixture.kickoff_utc,
        "as_of": snapshot.model_as_of,
        "gap_model_version_corners": corners.statistic,
        "conversion_model_version": CONVERSION_MODEL_VERSION,
        "selected_corner_distribution": str(corner_model["selected_distribution"]),
        "predicted_home_corners": float(corners.expected_home),
        "predicted_away_corners": float(corners.expected_away),
        "predicted_total_corners": float(corners.expected_home + corners.expected_away),
        "dispersion": None if corner_model.get("dispersion") is None else float(corner_model["dispersion"]),
        "interface_policy_id": DAY8_TO_DAY10_CORNER_INTERFACE_POLICY_ID,
    }
    corner_object_id = f"corner_{canonical_hash(corner_object_content)}"

    pricing_input = {
        "fixture_id": fixture.fixture_id,
        "event_id": fixture.fixture_id,
        "competition_id": fixture.competition_id,
        "competition_name": fixture.competition_name,
        "season_code": _season_code_from_fixture(fixture),
        "division_code": fixture.division_code,
        "match_date": fixture.kickoff_utc[:10],
        "kickoff_time": fixture.kickoff_utc[11:16],
        "kickoff_at": fixture.kickoff_utc,
        "as_of": snapshot.model_as_of,
        "day10_input_contract_version": DAY10_INPUT_CONTRACT_VERSION,
        "sanitized_input_policy_id": SANITIZED_INPUT_POLICY_ID,
        "model_versions": {
            "day7_gap_baseline_experiment_version": DAY7_BASELINE_EXPERIMENT_VERSION,
            "day8_experiment_version": DAY8_EXPERIMENT_VERSION,
            "day9_experiment_version": DAY9_EXPERIMENT_VERSION,
            "conversion_model_version": CONVERSION_MODEL_VERSION,
            "scoreline_model_version": SCORELINE_MODEL_VERSION,
            "gap_model_version_shots": shots_state.model_version,
            "gap_model_version_shots_on_target": shots_on_target_state.model_version,
            "gap_model_version_corners": corners_state.model_version,
            "current_snapshot_model_version": snapshot.model_version,
        },
        "scoreline_object": {
            "object_id": scoreline_object_id,
            **scoreline_object_content,
        },
        "corner_object": {
            "object_id": corner_object_id,
            **corner_object_content,
        },
    }

    market_rows = derive_goals_markets_for_fixture(pricing_input) + derive_corner_markets_for_fixture(pricing_input)
    representative_prices = _representative_fair_prices(market_rows)
    top_scoreline_rows = _top_scorelines(scoreline_distribution["matrix"], limit=5)
    prediction_version_key = canonical_hash(
        {
            "fixture_id": fixture.fixture_id,
            "model_snapshot_id": snapshot.snapshot_id,
            "kickoff_utc": fixture.kickoff_utc,
        }
    )
    prediction_id = f"prediction_{prediction_version_key}"

    current_time_utc = datetime.now(timezone.utc)
    kickoff_utc = datetime.fromisoformat(fixture.kickoff_utc)
    lock_state = "LOCKED_PRE_MATCH_PREDICTION" if current_time_utc >= kickoff_utc else "PRE_MATCH_OPEN"

    return attach_edge_desk_shadow_bundle(
        {
        "prediction_id": prediction_id,
        "fixture_id": fixture.fixture_id,
        "generated_at": generated_at,
        "model_as_of": snapshot.model_as_of,
        "model_snapshot_id": snapshot.snapshot_id,
        "prediction_version": prediction_version_key,
        "prediction_locked_at": fixture.kickoff_utc,
        "version_key": prediction_version_key,
        "prediction_status": PREDICTION_STATUS_READY,
        "competition_id": fixture.competition_id,
        "competition_name": fixture.competition_name,
        "home_team_name": fixture.home_team_name,
        "away_team_name": fixture.away_team_name,
        "kickoff_utc": fixture.kickoff_utc,
        "lock_state": lock_state,
        "model_versions": pricing_input["model_versions"],
        "model_parameters": _model_parameters(snapshot, fixture.competition_id, rho=rho, xi=xi),
        "team_resolution": {
            "home": home_resolution.to_dict(),
            "away": away_resolution.to_dict(),
        },
        "model_history": {
            "home": home_history,
            "away": away_history,
        },
        "expected_home_shots": float(shots.expected_home),
        "expected_away_shots": float(shots.expected_away),
        "expected_home_sot": float(shots_on_target.expected_home),
        "expected_away_sot": float(shots_on_target.expected_away),
        "expected_home_corners": float(corners.expected_home),
        "expected_away_corners": float(corners.expected_away),
        "expected_home_goals": float(expected_goals_home),
        "expected_away_goals": float(expected_goals_away),
        "expected_total_goals": float(expected_total_goals),
        "top_scorelines": top_scoreline_rows,
        "scoreline_matrix_object_id": scoreline_object_id,
        "corner_object_id": corner_object_id,
        "scoreline_object": pricing_input["scoreline_object"],
        "corner_object": pricing_input["corner_object"],
        "fair_prices": representative_prices,
        "market_rows": market_rows,
        "market_row_count": len(market_rows),
        "blockers": [],
        "warnings": []
        if not math.isinf(representative_prices["1x2"]["HOME"]["fair_decimal_price"])
        else ["home 1x2 fair price is infinite because model home-win probability is zero"],
        "manager_decision_status": "MARKET_DATA_NOT_CONNECTED",
        "safeguard_status": "BLOCKED",
        "safeguard_reason": "POINT-IN-TIME XG NOT AVAILABLE",
        },
        fixture=fixture,
        snapshot=snapshot,
    )
