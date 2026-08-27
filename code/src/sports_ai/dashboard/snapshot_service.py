"""Current forward football model snapshot builder for dashboard predictions."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from ..agents.football.gap_ratings import GapConfigEntry, GapObservation, GapRatingsEngine
from ..agents.football.route_a_classifier import (
    validate_route_a_artifact_payload,
    validate_route_a_manifest_payload,
    verify_route_a_manifest_against_artifact,
)
from ..locking import canonical_hash
from .models import TeamResolution, normalize_text_key, slugify, utc_now_iso
from .team_aliases import classify_unresolved_team, resolve_provider_team_alias, suggest_canonical_candidates

try:
    from ..experiments.football_day8_goal_conversion import (
        CONVERSION_MODEL_VERSION,
        _nonnegative_linear_fit,
        _predict_expected_goals,
    )
except ImportError:  # pragma: no cover - defensive only
    CONVERSION_MODEL_VERSION = "football_day8_goal_conversion_v1"

from ..experiments.football_day10_market_pricing import PRICING_MODEL_VERSION
from ..experiments.football_day9_scoreline_model import (
    DEFAULT_XI,
    RHO_GRID,
    SCORELINE_MODEL_VERSION,
    dixon_coles_tau,
)

SUPPORTED_STATISTICS = ("shots", "shots_on_target", "corners")
GAP_MANIFEST_FILENAMES = {
    "shots": "football_gap_shots_walkforward_fitted_*_manifest.json",
    "shots_on_target": "football_gap_shots_on_target_walkforward_fitted_*_manifest.json",
    "corners": "football_gap_corners_walkforward_fitted_*_manifest.json",
}
DAY9_MANIFEST_PATTERN = "football_day9_scoreline_model_*_manifest.json"
ROUTE_A_ARTIFACT_PATTERN = "football_route_a_direct_classifier_*_artifact.json"
ROUTE_A_MANIFEST_PATTERN = "football_route_a_direct_classifier_*_manifest.json"
TEAM_ALIAS_FILENAME = "football_team_aliases.json"
SNAPSHOT_MODEL_VERSION = "football_dashboard_current_snapshot_v1"


class CurrentSnapshotError(RuntimeError):
    """Raised when the current model snapshot cannot be built."""


@dataclass(frozen=True)
class CurrentFootballModelSnapshot:
    snapshot_id: str
    built_at: str
    model_as_of: str
    model_version: str
    gap_engine: GapRatingsEngine
    supported_competitions: tuple[str, ...]
    competition_names: dict[str, str]
    team_catalog: dict[str, dict[str, dict[str, str]]]
    alias_catalog: dict[str, dict[str, str]]
    conversion_coefficients: dict[str, dict[str, dict[str, float]]]
    corner_models: dict[str, dict[str, Any]]
    rho_by_competition: dict[str, float]
    xi_by_competition: dict[str, float]
    team_model_history_index: dict[str, dict[str, dict[str, dict[str, int]]]]
    route_a_artifact: dict[str, Any] | None
    manifest_paths: dict[str, str]
    diagnostics: dict[str, Any]

    def _resolved_team_resolution(
        self,
        *,
        provider: str,
        competition_id: str,
        provider_team_id: str | None,
        provider_team_name: str,
        normalized_team_name: str,
        canonical_team_name: str,
        resolution_kind: str,
        confidence: float,
        reason: str | None = None,
        diagnostic_category: str | None = None,
    ) -> TeamResolution:
        candidates = self.team_catalog.get(competition_id, {})
        target_name_key = normalize_text_key(canonical_team_name)
        exact = candidates.get(target_name_key)
        resolved_canonical_name = canonical_team_name if exact is None else str(exact["canonical_team_name"])
        resolved_canonical_id = (
            _canonical_team_id(competition_id, canonical_team_name)
            if exact is None
            else str(exact["canonical_team_id"])
        )
        model_history_available = exact is not None
        model_history_reason = None if model_history_available else "resolved identity has no historical state in current competition"
        return TeamResolution(
            provider=provider,
            competition_id=competition_id,
            provider_team_id=provider_team_id,
            provider_team_name=provider_team_name,
            normalized_team_name=normalized_team_name,
            canonical_team_id=resolved_canonical_id,
            canonical_team_name=resolved_canonical_name,
            resolution_kind=resolution_kind,
            confidence=confidence,
            reason=reason,
            diagnostic_category=diagnostic_category,
            suggested_canonical_name=resolved_canonical_name,
            canonical_candidates=(resolved_canonical_name,),
            model_history_available=model_history_available,
            model_history_reason=model_history_reason,
        )

    def resolve_team(
        self,
        *,
        provider: str,
        competition_id: str,
        provider_team_id: str | None,
        provider_team_name: str,
    ) -> TeamResolution:
        normalized_team_name = normalize_text_key(provider_team_name)
        candidates = self.team_catalog.get(competition_id, {})
        alias_match = resolve_provider_team_alias(
            provider=provider,
            competition_id=competition_id,
            provider_team_id=provider_team_id,
            provider_team_name=provider_team_name,
        )
        if alias_match is not None:
            return self._resolved_team_resolution(
                provider=provider,
                competition_id=competition_id,
                provider_team_id=provider_team_id,
                provider_team_name=provider_team_name,
                normalized_team_name=normalized_team_name,
                canonical_team_name=str(alias_match["canonical_team_name"]),
                resolution_kind=str(alias_match["resolution_kind"]),
                confidence=1.0,
                reason=str(alias_match["reason"]),
                diagnostic_category=str(alias_match["diagnostic_category"]),
            )

        competition_aliases = self.alias_catalog.get(competition_id, {})
        target_name = competition_aliases.get(normalized_team_name, normalized_team_name)
        exact = candidates.get(target_name)
        if exact is not None:
            return self._resolved_team_resolution(
                provider=provider,
                competition_id=competition_id,
                provider_team_id=provider_team_id,
                provider_team_name=provider_team_name,
                normalized_team_name=normalized_team_name,
                canonical_team_name=str(exact["canonical_team_name"]),
                resolution_kind="exact_normalized_name",
                confidence=1.0,
            )

        target_candidates = tuple(str(row["canonical_team_name"]) for row in candidates.values())
        canonical_candidates = suggest_canonical_candidates(target_candidates, provider_team_name)
        diagnostic_category, unresolved_reason = classify_unresolved_team(
            provider_team_name=provider_team_name,
            canonical_candidates=canonical_candidates,
        )
        return TeamResolution(
            provider=provider,
            competition_id=competition_id,
            provider_team_id=provider_team_id,
            provider_team_name=provider_team_name,
            normalized_team_name=normalized_team_name,
            canonical_team_id=None,
            canonical_team_name=None,
            resolution_kind="unresolved",
            confidence=0.0,
            reason=unresolved_reason,
            diagnostic_category=diagnostic_category,
            suggested_canonical_name=None if not canonical_candidates else canonical_candidates[0],
            canonical_candidates=canonical_candidates,
            model_history_available=None,
            model_history_reason=None,
        )

    def team_model_history_status(
        self,
        *,
        competition_id: str,
        canonical_team_id: str,
        venue: str,
    ) -> dict[str, Any]:
        competition_index = self.team_model_history_index.get(competition_id, {})
        team_index = competition_index.get(canonical_team_id, {})
        matches_observed_by_statistic = {
            statistic: int((team_index.get(statistic) or {}).get(venue, 0))
            for statistic in SUPPORTED_STATISTICS
        }
        missing_statistics = [
            statistic
            for statistic, matches_observed in matches_observed_by_statistic.items()
            if matches_observed <= 0
        ]
        return {
            "competition_id": competition_id,
            "canonical_team_id": canonical_team_id,
            "venue": venue,
            "available": not missing_statistics,
            "missing_statistics": missing_statistics,
            "matches_observed_by_statistic": matches_observed_by_statistic,
        }


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _research_root(repo_root: Path) -> Path:
    return repo_root / "research" / "football_research"


def _manual_inputs_root(repo_root: Path) -> Path:
    return repo_root / "research" / "manual_inputs"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _latest_matching_file(base_dir: Path, pattern: str) -> Path:
    matches = sorted(base_dir.glob(pattern))
    if not matches:
        raise CurrentSnapshotError(f"missing required artifact matching {pattern!r}")
    return matches[-1]


def _latest_optional_json(base_dir: Path, pattern: str) -> tuple[dict[str, Any] | None, str | None, str | None]:
    matches = sorted(base_dir.glob(pattern))
    if not matches:
        return None, None, None
    path = matches[-1]
    try:
        payload = _load_json(path)
    except Exception as exc:  # pragma: no cover - defensive only
        return None, None, f"{type(exc).__name__}: {exc}"
    if not isinstance(payload, dict):
        return None, None, "route a artifact must deserialize to a JSON object"
    return payload, str(path), None


def _load_route_a_verified_bundle(
    base_dir: Path,
) -> tuple[dict[str, Any] | None, str | None, str | None, str | None]:
    manifest, manifest_path, manifest_error = _latest_optional_json(base_dir, ROUTE_A_MANIFEST_PATTERN)
    if manifest_path is None:
        return None, None, None, None
    if manifest_error is not None:
        return None, None, manifest_path, manifest_error
    assert manifest is not None
    manifest_valid, manifest_validation_error = validate_route_a_manifest_payload(manifest)
    if not manifest_valid:
        return None, None, manifest_path, manifest_validation_error
    artifact_reference = Path(str(manifest["artifact_reference"]))
    if not artifact_reference.exists():
        return None, None, manifest_path, "manifest-pinned route a artifact is missing"
    try:
        artifact = _load_json(artifact_reference)
    except Exception as exc:  # pragma: no cover - defensive only
        return None, None, manifest_path, f"{type(exc).__name__}: {exc}"
    if not isinstance(artifact, dict):
        return None, None, manifest_path, "route a artifact must deserialize to a JSON object"
    artifact_valid, artifact_validation_error = validate_route_a_artifact_payload(artifact)
    if not artifact_valid:
        return None, None, manifest_path, artifact_validation_error
    artifact_sha256 = hashlib.sha256(artifact_reference.read_bytes()).hexdigest()
    bundle_valid, bundle_validation_error = verify_route_a_manifest_against_artifact(
        manifest=manifest,
        artifact=artifact,
        artifact_path=artifact_reference,
        artifact_sha256=artifact_sha256,
    )
    if not bundle_valid:
        return None, None, manifest_path, bundle_validation_error
    verified_artifact = dict(artifact)
    verified_artifact["runtime_manifest"] = manifest
    return verified_artifact, str(artifact_reference), manifest_path, None


def _load_gap_manifests(research_root: Path) -> tuple[dict[str, Any], dict[str, str]]:
    manifests: dict[str, Any] = {}
    paths: dict[str, str] = {}
    for statistic, pattern in GAP_MANIFEST_FILENAMES.items():
        path = _latest_matching_file(research_root, pattern)
        manifests[statistic] = _load_json(path)
        paths[f"gap_{statistic}"] = str(path)
    return manifests, paths


def _load_day9_manifest(research_root: Path) -> tuple[dict[str, Any], str]:
    path = _latest_matching_file(research_root, DAY9_MANIFEST_PATTERN)
    return _load_json(path), str(path)


def _load_alias_catalog(repo_root: Path) -> dict[str, dict[str, str]]:
    path = _manual_inputs_root(repo_root) / TEAM_ALIAS_FILENAME
    built_in = {
        "ENG1": {
            normalize_text_key("Manchester United"): normalize_text_key("Man United"),
            normalize_text_key("Manchester City"): normalize_text_key("Man City"),
            normalize_text_key("Tottenham Hotspur"): normalize_text_key("Tottenham"),
            normalize_text_key("Wolverhampton Wanderers"): normalize_text_key("Wolves"),
            normalize_text_key("Newcastle United"): normalize_text_key("Newcastle"),
            normalize_text_key("West Bromwich Albion"): normalize_text_key("West Brom"),
        },
        "ENG2": {
            normalize_text_key("West Bromwich Albion"): normalize_text_key("West Brom"),
        },
        "ESP1": {
            normalize_text_key("Athletic Club"): normalize_text_key("Ath Bilbao"),
            normalize_text_key("Athletic Bilbao"): normalize_text_key("Ath Bilbao"),
            normalize_text_key("Atletico Madrid"): normalize_text_key("Ath Madrid"),
            normalize_text_key("Real Sociedad"): normalize_text_key("Sociedad"),
            normalize_text_key("Rayo Vallecano"): normalize_text_key("Vallecano"),
            normalize_text_key("Deportivo Alaves"): normalize_text_key("Alaves"),
            normalize_text_key("Real Betis"): normalize_text_key("Betis"),
            normalize_text_key("Celta Vigo"): normalize_text_key("Celta"),
            normalize_text_key("RCD Espanyol"): normalize_text_key("Espanol"),
        },
    }
    if not path.exists():
        return built_in
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise CurrentSnapshotError("football_team_aliases.json must be a JSON object")
    merged = {competition_id: dict(mapping) for competition_id, mapping in built_in.items()}
    for competition_id, aliases in payload.items():
        if not isinstance(aliases, dict):
            continue
        competition_map = merged.setdefault(str(competition_id), {})
        for raw_key, raw_value in aliases.items():
            competition_map[normalize_text_key(str(raw_key))] = normalize_text_key(str(raw_value))
    return merged


def _connect_research_store(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_completed_events(
    *,
    research_db_path: str | Path,
    supported_competitions: set[str],
) -> list[sqlite3.Row]:
    query = """
        SELECT
            event_id,
            competition_id,
            competition_name,
            season_code,
            division_code,
            match_date,
            kickoff_time,
            home_team,
            away_team,
            home_goals,
            away_goals,
            home_shots,
            away_shots,
            home_shots_on_target,
            away_shots_on_target,
            home_corners,
            away_corners
        FROM football_events
        WHERE competition_id IN ({placeholders})
          AND home_goals IS NOT NULL
          AND away_goals IS NOT NULL
          AND home_shots IS NOT NULL
          AND away_shots IS NOT NULL
          AND home_shots_on_target IS NOT NULL
          AND away_shots_on_target IS NOT NULL
          AND home_corners IS NOT NULL
          AND away_corners IS NOT NULL
        ORDER BY competition_id, match_date, COALESCE(kickoff_time, ''), event_id
    """
    placeholders = ",".join("?" for _ in supported_competitions)
    with _connect_research_store(research_db_path) as conn:
        rows = conn.execute(query.format(placeholders=placeholders), tuple(sorted(supported_competitions))).fetchall()
    return rows


def _competition_name_map(rows: list[sqlite3.Row]) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in rows:
        result.setdefault(str(row["competition_id"]), str(row["competition_name"]))
    return result


def _build_gap_engine(manifests: dict[str, Any]) -> GapRatingsEngine:
    entries: list[GapConfigEntry] = []
    for statistic, manifest in manifests.items():
        baseline_details = manifest.get("baseline_details") or []
        learning_rates = manifest.get("learning_rates_by_competition") or {}
        model_version = manifest.get("model_version") or f"football_gap_{statistic}_fitted_v1"
        for detail in baseline_details:
            competition_id = str(detail["competition_id"])
            learning_rate = float(learning_rates.get(competition_id, detail["learning_rate"]))
            entries.append(
                GapConfigEntry(
                    competition_id=competition_id,
                    statistic=statistic,
                    learning_rate=learning_rate,
                    initial_home_baseline=float(detail["initial_home_baseline"]),
                    initial_away_baseline=float(detail["initial_away_baseline"]),
                    model_version=str(model_version),
                )
            )
    return GapRatingsEngine(entries)


def _canonical_team_id(competition_id: str, team_name: str) -> str:
    return f"{competition_id.lower()}_{slugify(team_name)}"


def _build_team_catalog(rows: list[sqlite3.Row]) -> dict[str, dict[str, dict[str, str]]]:
    catalog: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    seen: set[tuple[str, str]] = set()
    for row in rows:
        competition_id = str(row["competition_id"])
        for field_name in ("home_team", "away_team"):
            team_name = str(row[field_name])
            key = (competition_id, team_name)
            if key in seen:
                continue
            seen.add(key)
            normalized_name = normalize_text_key(team_name)
            catalog[competition_id][normalized_name] = {
                "canonical_team_id": _canonical_team_id(competition_id, team_name),
                "canonical_team_name": team_name,
            }
    return {competition_id: dict(mapping) for competition_id, mapping in sorted(catalog.items())}


def _build_team_model_history_index(gap_engine: GapRatingsEngine) -> dict[str, dict[str, dict[str, dict[str, int]]]]:
    history_index: dict[str, dict[str, dict[str, dict[str, int]]]] = defaultdict(lambda: defaultdict(dict))
    for row in gap_engine.export_state_records():
        competition_id = str(row["competition_id"])
        team_id = str(row["team_id"])
        statistic = str(row["statistic"])
        venue = str(row["venue"])
        history_index[competition_id].setdefault(team_id, {}).setdefault(statistic, {})[venue] = int(
            row["matches_observed"]
        )
    return {
        competition_id: {
            team_id: {
                statistic: {venue: int(count) for venue, count in venue_counts.items()}
                for statistic, venue_counts in statistics.items()
            }
            for team_id, statistics in teams.items()
        }
        for competition_id, teams in history_index.items()
    }


def _log_poisson_pmf(k: int, mean_value: float) -> float:
    if mean_value <= 0:
        return 0.0 if k == 0 else float("-inf")
    return (k * math.log(mean_value)) - mean_value - math.lgamma(k + 1.0)


def _fit_corner_model(rows: list[dict[str, Any]]) -> dict[str, Any]:
    means = np.asarray([row["predicted_total_corners"] for row in rows], dtype=np.float64)
    observed = np.asarray([row["actual_total_corners"] for row in rows], dtype=np.float64)
    numerator = float(np.mean(means ** 2))
    delta = float(np.mean(((observed - means) ** 2) - observed))
    dispersion = None
    if numerator > 0.0 and delta > 1e-12:
        candidate = numerator / delta
        if math.isfinite(candidate) and candidate > 0.0:
            dispersion = float(candidate)

    poisson_ll = float(sum(_log_poisson_pmf(int(y), float(mu)) for y, mu in zip(observed, means)))
    if dispersion is None:
        negative_binomial_ll = float("-inf")
    else:
        negative_binomial_ll = 0.0
        for y, mu in zip(observed, means):
            if mu <= 0.0:
                negative_binomial_ll += 0.0 if int(y) == 0 else float("-inf")
                continue
            r = float(dispersion)
            p = r / (r + float(mu))
            negative_binomial_ll += (
                math.lgamma(int(y) + r)
                - math.lgamma(r)
                - math.lgamma(int(y) + 1.0)
                + (r * math.log(p))
                + (int(y) * math.log(1.0 - p))
            )
    selection = "negative_binomial" if negative_binomial_ll > poisson_ll else "poisson"
    return {
        "selected_distribution": selection,
        "dispersion": dispersion,
        "poisson_log_likelihood": poisson_ll,
        "negative_binomial_log_likelihood": negative_binomial_ll,
        "sample_size": len(rows),
    }


def _fit_rho(rows: list[dict[str, Any]]) -> float:
    best_rho = 0.0
    best_log_likelihood = float("-inf")
    for rho in RHO_GRID:
        log_likelihood = 0.0
        valid = True
        for row in rows:
            home_mean = max(0.0, float(row["expected_goals_home"]))
            away_mean = max(0.0, float(row["expected_goals_away"]))
            tau = dixon_coles_tau(
                int(row["actual_home_goals"]),
                int(row["actual_away_goals"]),
                home_goals_mean=home_mean,
                away_goals_mean=away_mean,
                rho=float(rho),
            )
            if tau <= 0.0:
                valid = False
                break
            log_likelihood += _log_poisson_pmf(int(row["actual_home_goals"]), home_mean)
            log_likelihood += _log_poisson_pmf(int(row["actual_away_goals"]), away_mean)
            log_likelihood += math.log(tau)
        if valid and log_likelihood > best_log_likelihood:
            best_log_likelihood = log_likelihood
            best_rho = float(rho)
    return best_rho


def build_current_football_model_snapshot(
    *,
    research_db_path: str | Path | None = None,
) -> CurrentFootballModelSnapshot:
    repo_root = _repo_root()
    research_root = _research_root(repo_root)
    manifests, manifest_paths = _load_gap_manifests(research_root)
    day9_manifest, day9_manifest_path = _load_day9_manifest(research_root)
    manifest_paths["day9"] = day9_manifest_path
    route_a_artifact, route_a_artifact_path, route_a_manifest_path, route_a_artifact_error = _load_route_a_verified_bundle(
        research_root
    )
    if route_a_artifact_path is not None:
        manifest_paths["route_a_artifact"] = route_a_artifact_path
    if route_a_manifest_path is not None:
        manifest_paths["route_a_manifest"] = route_a_manifest_path

    supported_competitions = {
        str(detail["competition_id"])
        for manifest in manifests.values()
        for detail in manifest.get("baseline_details") or []
    }
    if not supported_competitions:
        raise CurrentSnapshotError("no supported competitions discovered from Day 7 manifests")

    resolved_research_db_path = (
        Path(research_db_path)
        if research_db_path is not None
        else research_root / "store" / "football_research.sqlite"
    )
    rows = _fetch_completed_events(
        research_db_path=resolved_research_db_path,
        supported_competitions=supported_competitions,
    )
    if not rows:
        raise CurrentSnapshotError("research store does not contain completed supported football events")

    team_catalog = _build_team_catalog(rows)
    alias_catalog = _load_alias_catalog(repo_root)
    gap_engine = _build_gap_engine(manifests)

    replay_rows_by_competition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        competition_id = str(row["competition_id"])
        home_team_name = str(row["home_team"])
        away_team_name = str(row["away_team"])
        home_team_id = team_catalog[competition_id][normalize_text_key(home_team_name)]["canonical_team_id"]
        away_team_id = team_catalog[competition_id][normalize_text_key(away_team_name)]["canonical_team_id"]

        gap_predictions: dict[str, dict[str, float]] = {}
        for statistic, home_field, away_field in (
            ("shots", "home_shots", "away_shots"),
            ("shots_on_target", "home_shots_on_target", "away_shots_on_target"),
            ("corners", "home_corners", "away_corners"),
        ):
            prediction = gap_engine.predict_fixture(
                competition_id=competition_id,
                statistic=statistic,
                home_team_id=home_team_id,
                away_team_id=away_team_id,
            )
            gap_predictions[statistic] = {
                "expected_home": float(prediction.expected_home),
                "expected_away": float(prediction.expected_away),
            }
            gap_engine.apply_observation(
                GapObservation(
                    event_id=str(row["event_id"]),
                    competition_id=competition_id,
                    match_date=str(row["match_date"]),
                    home_team_id=home_team_id,
                    away_team_id=away_team_id,
                    statistic=statistic,
                    home_value=float(row[home_field]),
                    away_value=float(row[away_field]),
                )
            )

        replay_rows_by_competition[competition_id].append(
            {
                "event_id": str(row["event_id"]),
                "competition_id": competition_id,
                "season_code": str(row["season_code"]),
                "division_code": str(row["division_code"]),
                "match_date": str(row["match_date"]),
                "kickoff_time": None if row["kickoff_time"] is None else str(row["kickoff_time"]),
                "home_team_id": home_team_id,
                "away_team_id": away_team_id,
                "home_team_name": home_team_name,
                "away_team_name": away_team_name,
                "predicted_home_shots": gap_predictions["shots"]["expected_home"],
                "predicted_away_shots": gap_predictions["shots"]["expected_away"],
                "predicted_home_shots_on_target": gap_predictions["shots_on_target"]["expected_home"],
                "predicted_away_shots_on_target": gap_predictions["shots_on_target"]["expected_away"],
                "predicted_home_corners": gap_predictions["corners"]["expected_home"],
                "predicted_away_corners": gap_predictions["corners"]["expected_away"],
                "predicted_total_corners": gap_predictions["corners"]["expected_home"]
                + gap_predictions["corners"]["expected_away"],
                "actual_home_goals": int(row["home_goals"]),
                "actual_away_goals": int(row["away_goals"]),
                "actual_total_corners": int(row["home_corners"]) + int(row["away_corners"]),
            }
        )

    conversion_coefficients: dict[str, dict[str, dict[str, float]]] = {}
    corner_models: dict[str, dict[str, Any]] = {}
    rho_by_competition: dict[str, float] = {}
    xi_by_competition: dict[str, float] = {}
    replay_diagnostics: dict[str, Any] = {}

    xi_from_manifest = {
        str(row["competition_id"]): float(row.get("selected_xi", DEFAULT_XI))
        for row in day9_manifest.get("final_rho_by_competition") or []
    }

    for competition_id, competition_rows in sorted(replay_rows_by_competition.items()):
        home_coefficients = _nonnegative_linear_fit(
            feature_rows=[
                (row["predicted_home_shots"], row["predicted_home_shots_on_target"])
                for row in competition_rows
            ],
            targets=[float(row["actual_home_goals"]) for row in competition_rows],
        )
        away_coefficients = _nonnegative_linear_fit(
            feature_rows=[
                (row["predicted_away_shots"], row["predicted_away_shots_on_target"])
                for row in competition_rows
            ],
            targets=[float(row["actual_away_goals"]) for row in competition_rows],
        )
        conversion_coefficients[competition_id] = {
            "home": {
                "beta_shots": float(home_coefficients["beta_shots"]),
                "beta_shots_on_target": float(home_coefficients["beta_shots_on_target"]),
            },
            "away": {
                "beta_shots": float(away_coefficients["beta_shots"]),
                "beta_shots_on_target": float(away_coefficients["beta_shots_on_target"]),
            },
        }

        enriched_rows = []
        for row in competition_rows:
            expected_goals_home = _predict_expected_goals(
                coefficients=conversion_coefficients[competition_id]["home"],
                predicted_shots=float(row["predicted_home_shots"]),
                predicted_shots_on_target=float(row["predicted_home_shots_on_target"]),
            )
            expected_goals_away = _predict_expected_goals(
                coefficients=conversion_coefficients[competition_id]["away"],
                predicted_shots=float(row["predicted_away_shots"]),
                predicted_shots_on_target=float(row["predicted_away_shots_on_target"]),
            )
            enriched_rows.append(
                {
                    **row,
                    "expected_goals_home": expected_goals_home,
                    "expected_goals_away": expected_goals_away,
                }
            )

        corner_models[competition_id] = _fit_corner_model(competition_rows)
        rho_by_competition[competition_id] = _fit_rho(enriched_rows)
        xi_by_competition[competition_id] = xi_from_manifest.get(competition_id, DEFAULT_XI)
        replay_diagnostics[competition_id] = {
            "replay_row_count": len(competition_rows),
            "conversion_coefficients": conversion_coefficients[competition_id],
            "corner_model": corner_models[competition_id],
            "rho": rho_by_competition[competition_id],
            "xi": xi_by_competition[competition_id],
        }

    competition_names = _competition_name_map(rows)
    latest_match_date = max(str(row["match_date"]) for row in rows)
    model_as_of = f"{latest_match_date}T23:59:59+00:00"
    built_at = utc_now_iso()
    team_model_history_index = _build_team_model_history_index(gap_engine)
    snapshot_content = {
        "model_version": SNAPSHOT_MODEL_VERSION,
        "model_as_of": model_as_of,
        "gap_manifests": manifest_paths,
        "supported_competitions": sorted(supported_competitions),
        "conversion_coefficients": conversion_coefficients,
        "corner_models": corner_models,
        "rho_by_competition": rho_by_competition,
        "xi_by_competition": xi_by_competition,
        "route_a_artifact_id": None if route_a_artifact is None else route_a_artifact.get("artifact_id"),
    }
    snapshot_id = f"current_snapshot_{canonical_hash(snapshot_content)}"

    return CurrentFootballModelSnapshot(
        snapshot_id=snapshot_id,
        built_at=built_at,
        model_as_of=model_as_of,
        model_version=SNAPSHOT_MODEL_VERSION,
        gap_engine=gap_engine,
        supported_competitions=tuple(sorted(supported_competitions)),
        competition_names=competition_names,
        team_catalog=team_catalog,
        alias_catalog=alias_catalog,
        conversion_coefficients=conversion_coefficients,
        corner_models=corner_models,
        rho_by_competition=rho_by_competition,
        xi_by_competition=xi_by_competition,
        team_model_history_index=team_model_history_index,
        route_a_artifact=route_a_artifact,
        manifest_paths=manifest_paths,
        diagnostics={
            "built_at": built_at,
            "model_as_of": model_as_of,
            "pricing_model_version": PRICING_MODEL_VERSION,
            "conversion_model_version": CONVERSION_MODEL_VERSION,
            "scoreline_model_version": SCORELINE_MODEL_VERSION,
            "supported_competitions": list(sorted(supported_competitions)),
            "route_a": {
                "artifact_loaded": route_a_artifact is not None,
                "artifact_path": route_a_artifact_path,
                "manifest_path": route_a_manifest_path,
                "artifact_error": route_a_artifact_error,
                "overall_status": None if route_a_artifact is None else route_a_artifact.get("overall_route_a_status"),
                "artifact_id": None if route_a_artifact is None else route_a_artifact.get("artifact_id"),
            },
            "replay": replay_diagnostics,
        },
    )
