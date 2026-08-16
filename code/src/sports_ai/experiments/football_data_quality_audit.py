"""Audit the historical football data-quality signal used in policy research."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..data_quality import (
    explain_v3_data_quality,
    historical_football_data_quality_components,
    historical_football_data_quality_components_v2,
    summarize_float_distribution,
)
from .football_poisson_backtest import (
    SELECTION_ORDER,
    FootballPoissonBacktestConfig,
    _connect_research_store,
    _eligible_history_rows,
    _fetch_events,
    _fetch_market_map,
    _fit_prediction,
)


RESULT_TO_SELECTION = {
    "H": "home",
    "D": "draw",
    "A": "away",
}


@dataclass(frozen=True)
class FootballDataQualityAuditConfig:
    research_db_path: str | Path
    season_code: str
    competition_ids: tuple[str, ...]
    bookmaker: str = "Bet365"
    model_name: str = "poisson_1x2_v3"
    min_team_matches: int = 6
    prior_weight: float = 5.0
    strength_regularization: float = 0.03
    optimizer_learning_rate: float = 0.6
    optimizer_max_iterations: int = 250
    dixon_coles_rho: float = -0.1
    history_season_codes: tuple[str, ...] | None = None
    edge_threshold: float = 0.03
    max_fair_odds: float | None = 2.8
    allowed_selections: tuple[str, ...] = SELECTION_ORDER
    dq_threshold: float = 0.82
    output_path: str | Path | None = None


def _distribution_by_key(rows: list[dict[str, Any]], *, value_key: str, group_key: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        value = row.get(value_key)
        if value is None:
            continue
        grouped[str(row.get(group_key) or "UNKNOWN")].append(float(value))
    return {name: summarize_float_distribution(values) for name, values in sorted(grouped.items())}


def _distribution_by_convergence(rows: list[dict[str, Any]], *, value_key: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        value = row.get(value_key)
        if value is None:
            continue
        label = "converged" if row.get("converged") is True else "non_converged"
        grouped[label].append(float(value))
    return {name: summarize_float_distribution(values) for name, values in sorted(grouped.items())}


def _distribution_by_bucket(rows: list[dict[str, Any]], *, value_key: str, bucket_key: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        value = row.get(value_key)
        if value is None:
            continue
        grouped[str(row.get(bucket_key) or "UNKNOWN")].append(float(value))
    return {name: summarize_float_distribution(values) for name, values in sorted(grouped.items())}


def _old_candidate_for_event(
    *,
    prediction: dict[str, Any],
    quote: dict[str, float],
    competition_id: str,
    edge_threshold: float,
    max_fair_odds: float | None,
    allowed_selections: Sequence[str],
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for selection in allowed_selections:
        price = quote.get(selection)
        if price in (None, 0):
            continue
        fair_prob = prediction["probabilities"][selection]
        fair_odds = prediction["fair_odds"][selection]
        if max_fair_odds is not None and fair_odds > max_fair_odds:
            continue
        edge = fair_prob - (1.0 / float(price))
        if edge < edge_threshold:
            continue
        candidate = {
            "competition_id": competition_id,
            "selection": selection,
            "available_price": float(price),
            "fair_probability": fair_prob,
            "fair_odds": fair_odds,
            "edge": edge,
        }
        if best is None or candidate["edge"] > best["edge"]:
            best = candidate
    return best


def _sample_records(rows: list[dict[str, Any]], *, limit: int = 20) -> list[dict[str, Any]]:
    return [
        {
            "event_id": row["event_id"],
            "competition_id": row["competition_id"],
            "match_date": row["match_date"],
            "home_team": row["home_team"],
            "away_team": row["away_team"],
            "selection": row.get("selection"),
            "current_v3_data_quality": row["current_v3_data_quality"],
            "historical_data_quality": row["historical_data_quality"],
            "sports_data_quality_v2": row["sports_data_quality_v2"],
            "model_fit_quality_v2": row["model_fit_quality_v2"],
            "market_data_quality_v2": row["market_data_quality_v2"],
            "decision_data_quality_v2": row["decision_data_quality_v2"],
            "converged": row["converged"],
            "weakest_history_n": row["weakest_history_n"],
            "weakest_recent_n": row["weakest_recent_n"],
            "season_phase": row["season_phase"],
            "newly_observed_team": row["newly_observed_team"],
            "continuity_flag": row["continuity_flag"],
            "last_max_update": row["last_max_update"],
            "reasons": row["v3_dq_reasons"],
            "historical_reasons": row["historical_dq_reasons"],
            "historical_v2_reasons": row["historical_v2_reasons"],
        }
        for row in rows[:limit]
    ]


def _season_phase(match_date: str | None) -> str:
    month = (match_date or "")[5:7]
    if month in {"08", "09"}:
        return "early_season"
    if month in {"10", "11", "12", "01"}:
        return "mid_season"
    if month in {"02", "03", "04", "05"}:
        return "late_season"
    return "other_season_window"


def _weakest_history_bucket(value: int) -> str:
    if value < 8:
        return "<8"
    if value < 12:
        return "8-11"
    if value < 16:
        return "12-15"
    if value < 24:
        return "16-23"
    return "24+"


def build_football_data_quality_audit(
    config: FootballDataQualityAuditConfig,
) -> dict[str, Any]:
    """Audit the current v3 DQ proxy and proposed historical contract."""

    model_config = FootballPoissonBacktestConfig(
        research_db_path=config.research_db_path,
        season_code=config.season_code,
        competition_ids=config.competition_ids,
        bookmaker=config.bookmaker,
        model_name=config.model_name,
        min_team_matches=config.min_team_matches,
        prior_weight=config.prior_weight,
        strength_regularization=config.strength_regularization,
        optimizer_learning_rate=config.optimizer_learning_rate,
        optimizer_max_iterations=config.optimizer_max_iterations,
        dixon_coles_rho=config.dixon_coles_rho,
        edge_threshold=config.edge_threshold,
        max_fair_odds=config.max_fair_odds,
        allowed_selections=config.allowed_selections,
        history_season_codes=config.history_season_codes,
    )

    with _connect_research_store(config.research_db_path) as conn:
        all_events = _fetch_events(conn, competition_ids=config.competition_ids)
        market_map = _fetch_market_map(conn, season_code=config.season_code, bookmaker=config.bookmaker)

    by_competition_history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    predicted_rows: list[dict[str, Any]] = []
    old_candidate_rows: list[dict[str, Any]] = []
    predicted_events = 0
    market_matched_events = 0
    v3_reason_counts: Counter[str] = Counter()
    historical_reason_counts: Counter[str] = Counter()
    historical_v2_reason_counts: Counter[str] = Counter()
    old_candidate_rejection_reasons: Counter[str] = Counter()

    for row in all_events:
        event = dict(row)
        competition_id = event["competition_id"]
        history = by_competition_history[competition_id]

        if event["season_code"] != config.season_code:
            history.append(event)
            continue

        eligible_history = _eligible_history_rows(
            history,
            history_season_codes=config.history_season_codes,
        )
        prediction = _fit_prediction(
            model_config,
            history_rows=eligible_history,
            event=event,
        )
        if prediction is None or RESULT_TO_SELECTION.get(event.get("result_code")) is None:
            history.append(event)
            continue

        predicted_events += 1
        quote = market_map.get(event["event_id"])
        if quote and all(quote.get(selection) not in (None, 0) for selection in SELECTION_ORDER):
            market_matched_events += 1

        v3_explanation = explain_v3_data_quality(prediction, threshold=config.dq_threshold)
        historical_dq = historical_football_data_quality_components(
            prediction=prediction,
            history_rows=eligible_history,
            event=event,
            market_quote={selection: float(quote[selection]) for selection in SELECTION_ORDER} if quote else None,
        )
        historical_dq_v2 = historical_football_data_quality_components_v2(
            prediction=prediction,
            history_rows=eligible_history,
            event=event,
            market_quote={selection: float(quote[selection]) for selection in SELECTION_ORDER} if quote else None,
        )

        for reason in v3_explanation["reasons"]:
            v3_reason_counts[reason] += 1
        for reason in historical_dq["reasons"]:
            historical_reason_counts[reason] += 1
        for reason in historical_dq_v2["reasons"]:
            historical_v2_reason_counts[reason] += 1

        audit_row = {
            "event_id": event["event_id"],
            "competition_id": competition_id,
            "match_date": event["match_date"],
            "home_team": event["home_team"],
            "away_team": event["away_team"],
            "current_v3_data_quality": float(prediction["data_quality"]),
            "historical_data_quality": float(historical_dq["historical_data_quality"]),
            "sports_data_quality_v2": float(historical_dq_v2["sports_data_quality"]),
            "model_fit_quality_v2": float(historical_dq_v2["model_fit_quality"]),
            "market_data_quality_v2": float(historical_dq_v2["market_data_quality"]),
            "decision_data_quality_v2": float(historical_dq_v2["decision_data_quality"]),
            "converged": v3_explanation["converged"],
            "weakest_history_n": v3_explanation["weakest_history_n"],
            "weakest_recent_n": int(historical_dq_v2["counts"]["weakest_recent_n"]),
            "season_phase": _season_phase(event.get("match_date")),
            "newly_observed_team": bool(historical_dq_v2["flags"]["newly_observed_team"]),
            "continuity_flag": str(historical_dq_v2["flags"]["continuity_flag"]),
            "last_max_update": v3_explanation["last_max_update"],
            "v3_dq_reasons": list(v3_explanation["reasons"]),
            "historical_dq_reasons": list(historical_dq["reasons"]),
            "historical_v2_reasons": list(historical_dq_v2["reasons"]),
            "historical_dq_components": historical_dq["components"],
            "historical_dq_v2_components": historical_dq_v2["components"],
        }
        predicted_rows.append(audit_row)

        if quote:
            old_candidate = _old_candidate_for_event(
                prediction=prediction,
                quote={selection: float(quote[selection]) for selection in SELECTION_ORDER},
                competition_id=competition_id,
                edge_threshold=config.edge_threshold,
                max_fair_odds=config.max_fair_odds,
                allowed_selections=config.allowed_selections,
            )
            if old_candidate is not None:
                candidate_row = dict(audit_row)
                candidate_row.update(old_candidate)
                old_candidate_rows.append(candidate_row)
                for reason in v3_explanation["reasons"]:
                    old_candidate_rejection_reasons[reason] += 1

        history.append(event)

    report = {
        "evaluation_mode": "historical_data_quality_audit",
        "model_name": config.model_name,
        "season_code": config.season_code,
        "history_season_codes": list(config.history_season_codes) if config.history_season_codes else None,
        "competition_ids": list(config.competition_ids),
        "coverage": {
            "predicted_events": predicted_events,
            "market_matched_events": market_matched_events,
            "old_policy_candidate_count": len(old_candidate_rows),
            "dq_threshold": config.dq_threshold,
        },
        "current_v3_proxy": {
            "all_predicted": summarize_float_distribution(
                [row["current_v3_data_quality"] for row in predicted_rows]
            ),
            "old_policy_candidates": summarize_float_distribution(
                [row["current_v3_data_quality"] for row in old_candidate_rows]
            ),
            "by_competition_all_predicted": _distribution_by_key(
                predicted_rows,
                value_key="current_v3_data_quality",
                group_key="competition_id",
            ),
            "by_competition_old_candidates": _distribution_by_key(
                old_candidate_rows,
                value_key="current_v3_data_quality",
                group_key="competition_id",
            ),
            "by_convergence_all_predicted": _distribution_by_convergence(
                predicted_rows,
                value_key="current_v3_data_quality",
            ),
            "by_convergence_old_candidates": _distribution_by_convergence(
                old_candidate_rows,
                value_key="current_v3_data_quality",
            ),
            "reason_counts_all_predicted": dict(sorted(v3_reason_counts.items())),
            "reason_counts_old_candidates": dict(sorted(old_candidate_rejection_reasons.items())),
        },
        "historical_point_in_time_contract_v1": {
            "all_predicted": summarize_float_distribution(
                [row["historical_data_quality"] for row in predicted_rows]
            ),
            "old_policy_candidates": summarize_float_distribution(
                [row["historical_data_quality"] for row in old_candidate_rows]
            ),
            "by_competition_all_predicted": _distribution_by_key(
                predicted_rows,
                value_key="historical_data_quality",
                group_key="competition_id",
            ),
            "by_competition_old_candidates": _distribution_by_key(
                old_candidate_rows,
                value_key="historical_data_quality",
                group_key="competition_id",
            ),
            "by_convergence_all_predicted": _distribution_by_convergence(
                predicted_rows,
                value_key="historical_data_quality",
            ),
            "by_convergence_old_candidates": _distribution_by_convergence(
                old_candidate_rows,
                value_key="historical_data_quality",
            ),
            "reason_counts_all_predicted": dict(sorted(historical_reason_counts.items())),
            "component_means_all_predicted": {
                component: summarize_float_distribution(
                    [
                        row["historical_dq_components"][component]
                        for row in predicted_rows
                    ]
                )
                for component in (
                    "history_coverage",
                    "model_convergence",
                    "fixture_context_completeness",
                    "team_entity_continuity",
                )
            },
        },
        "historical_point_in_time_contract_v2": {
            "sports_data_quality_all_predicted": summarize_float_distribution(
                [row["sports_data_quality_v2"] for row in predicted_rows]
            ),
            "sports_data_quality_old_candidates": summarize_float_distribution(
                [row["sports_data_quality_v2"] for row in old_candidate_rows]
            ),
            "model_fit_quality_all_predicted": summarize_float_distribution(
                [row["model_fit_quality_v2"] for row in predicted_rows]
            ),
            "market_data_quality_all_predicted": summarize_float_distribution(
                [row["market_data_quality_v2"] for row in predicted_rows]
            ),
            "decision_data_quality_all_predicted": summarize_float_distribution(
                [row["decision_data_quality_v2"] for row in predicted_rows]
            ),
            "decision_data_quality_old_candidates": summarize_float_distribution(
                [row["decision_data_quality_v2"] for row in old_candidate_rows]
            ),
            "by_competition_decision_quality_all_predicted": _distribution_by_key(
                predicted_rows,
                value_key="decision_data_quality_v2",
                group_key="competition_id",
            ),
            "by_competition_decision_quality_old_candidates": _distribution_by_key(
                old_candidate_rows,
                value_key="decision_data_quality_v2",
                group_key="competition_id",
            ),
            "by_season_phase_decision_quality_all_predicted": _distribution_by_bucket(
                predicted_rows,
                value_key="decision_data_quality_v2",
                bucket_key="season_phase",
            ),
            "by_season_phase_decision_quality_old_candidates": _distribution_by_bucket(
                old_candidate_rows,
                value_key="decision_data_quality_v2",
                bucket_key="season_phase",
            ),
            "by_newly_observed_team_decision_quality_all_predicted": _distribution_by_bucket(
                predicted_rows,
                value_key="decision_data_quality_v2",
                bucket_key="newly_observed_team",
            ),
            "by_continuity_flag_decision_quality_all_predicted": _distribution_by_bucket(
                predicted_rows,
                value_key="decision_data_quality_v2",
                bucket_key="continuity_flag",
            ),
            "by_weakest_history_bucket_decision_quality_all_predicted": _distribution_by_bucket(
                [
                    {
                        **row,
                        "weakest_history_bucket": _weakest_history_bucket(int(row["weakest_history_n"])),
                    }
                    for row in predicted_rows
                ],
                value_key="decision_data_quality_v2",
                bucket_key="weakest_history_bucket",
            ),
            "by_convergence_decision_quality_all_predicted": _distribution_by_convergence(
                predicted_rows,
                value_key="decision_data_quality_v2",
            ),
            "reason_counts_all_predicted": dict(sorted(historical_v2_reason_counts.items())),
            "component_distributions_all_predicted": {
                component: summarize_float_distribution(
                    [
                        row["historical_dq_v2_components"][component]
                        for row in predicted_rows
                    ]
                )
                for component in (
                    "sample_depth_score",
                    "recent_coverage_score",
                    "role_depth_score",
                    "continuity_score",
                    "previous_season_score",
                    "convergence_score",
                    "stability_score",
                    "uncertainty_score",
                    "bookmaker_available_score",
                    "all_selections_present_score",
                    "timestamp_quality_score",
                    "entry_close_availability_score",
                )
            },
            "below_threshold_diagnostics": {
                "all_predicted_count": sum(
                    1 for row in predicted_rows if row["decision_data_quality_v2"] < config.dq_threshold
                ),
                "old_policy_candidate_count": sum(
                    1 for row in old_candidate_rows if row["decision_data_quality_v2"] < config.dq_threshold
                ),
                "reason_counts_all_predicted": dict(
                    sorted(
                        Counter(
                            reason
                            for row in predicted_rows
                            if row["decision_data_quality_v2"] < config.dq_threshold
                            for reason in row["historical_v2_reasons"]
                        ).items()
                    )
                ),
                "reason_counts_old_policy_candidates": dict(
                    sorted(
                        Counter(
                            reason
                            for row in old_candidate_rows
                            if row["decision_data_quality_v2"] < config.dq_threshold
                            for reason in row["historical_v2_reasons"]
                        ).items()
                    )
                ),
                "by_league": _distribution_by_key(
                    [row for row in predicted_rows if row["decision_data_quality_v2"] < config.dq_threshold],
                    value_key="decision_data_quality_v2",
                    group_key="competition_id",
                ),
                "by_season_phase": _distribution_by_bucket(
                    [row for row in predicted_rows if row["decision_data_quality_v2"] < config.dq_threshold],
                    value_key="decision_data_quality_v2",
                    bucket_key="season_phase",
                ),
                "by_continuity_flag": _distribution_by_bucket(
                    [row for row in predicted_rows if row["decision_data_quality_v2"] < config.dq_threshold],
                    value_key="decision_data_quality_v2",
                    bucket_key="continuity_flag",
                ),
                "by_newly_observed_team": _distribution_by_bucket(
                    [row for row in predicted_rows if row["decision_data_quality_v2"] < config.dq_threshold],
                    value_key="decision_data_quality_v2",
                    bucket_key="newly_observed_team",
                ),
            },
        },
        "diagnosis": {
            "old_candidates_below_current_threshold": sum(
                1 for row in old_candidate_rows if row["current_v3_data_quality"] < config.dq_threshold
            ),
            "old_candidates_below_historical_contract_threshold": sum(
                1 for row in old_candidate_rows if row["historical_data_quality"] < config.dq_threshold
            ),
            "old_candidates_below_historical_v2_decision_threshold": sum(
                1 for row in old_candidate_rows if row["decision_data_quality_v2"] < config.dq_threshold
            ),
            "current_interpretation": (
                "The current v3 data_quality is a convergence/sample proxy, not a complete historical decision-quality contract."
            ),
            "market_data_quality_note": (
                "historical market_data_quality is structurally capped because timestamp_quality_score is a fixed partial credit when a quote exists and entry_close_availability_score is currently 0.0 without trusted historical timing data."
            ),
            "next_step": (
                "Audit the refined historical contract first, then decide whether the policy validator should switch from the live proxy to the approved historical decision-quality contract."
            ),
        },
        "sample_rows": {
            "all_predicted": _sample_records(predicted_rows),
            "old_policy_candidates": _sample_records(old_candidate_rows),
        },
    }

    if config.output_path is not None:
        target = Path(config.output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Audit historical football data-quality behavior.")
    parser.add_argument("--research-db", required=True)
    parser.add_argument("--season", required=True)
    parser.add_argument("--competition", action="append", dest="competitions", required=True)
    parser.add_argument("--bookmaker", default="Bet365")
    parser.add_argument("--model", default="poisson_1x2_v3")
    parser.add_argument("--history-season", action="append", dest="history_seasons")
    parser.add_argument("--min-team-matches", type=int, default=6)
    parser.add_argument("--prior-weight", type=float, default=5.0)
    parser.add_argument("--strength-regularization", type=float, default=0.03)
    parser.add_argument("--optimizer-learning-rate", type=float, default=0.6)
    parser.add_argument("--optimizer-max-iterations", type=int, default=250)
    parser.add_argument("--dixon-coles-rho", type=float, default=-0.1)
    parser.add_argument("--edge-threshold", type=float, default=0.03)
    parser.add_argument("--max-fair-odds", type=float, default=2.8)
    parser.add_argument("--selection", action="append", dest="selections")
    parser.add_argument("--dq-threshold", type=float, default=0.82)
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    report = build_football_data_quality_audit(
        FootballDataQualityAuditConfig(
            research_db_path=args.research_db,
            season_code=args.season,
            competition_ids=tuple(args.competitions),
            bookmaker=args.bookmaker,
            model_name=args.model,
            min_team_matches=args.min_team_matches,
            prior_weight=args.prior_weight,
            strength_regularization=args.strength_regularization,
            optimizer_learning_rate=args.optimizer_learning_rate,
            optimizer_max_iterations=args.optimizer_max_iterations,
            dixon_coles_rho=args.dixon_coles_rho,
            history_season_codes=tuple(args.history_seasons) if args.history_seasons else None,
            edge_threshold=args.edge_threshold,
            max_fair_odds=args.max_fair_odds,
            allowed_selections=tuple(args.selections) if args.selections else SELECTION_ORDER,
            dq_threshold=args.dq_threshold,
            output_path=args.output,
        )
    )
    print(
        json.dumps(
            {
                "output": args.output,
                "coverage": report["coverage"],
                "current_v3_proxy": {
                    "all_predicted": report["current_v3_proxy"]["all_predicted"],
                    "old_policy_candidates": report["current_v3_proxy"]["old_policy_candidates"],
                    "reason_counts_old_candidates": report["current_v3_proxy"]["reason_counts_old_candidates"],
                },
                "historical_point_in_time_contract_v1": {
                    "all_predicted": report["historical_point_in_time_contract_v1"]["all_predicted"],
                    "old_policy_candidates": report["historical_point_in_time_contract_v1"]["old_policy_candidates"],
                },
                "historical_point_in_time_contract_v2": {
                    "sports_data_quality_all_predicted": report["historical_point_in_time_contract_v2"]["sports_data_quality_all_predicted"],
                    "model_fit_quality_all_predicted": report["historical_point_in_time_contract_v2"]["model_fit_quality_all_predicted"],
                    "decision_data_quality_all_predicted": report["historical_point_in_time_contract_v2"]["decision_data_quality_all_predicted"],
                    "decision_data_quality_old_candidates": report["historical_point_in_time_contract_v2"]["decision_data_quality_old_candidates"],
                    "reason_counts_all_predicted": report["historical_point_in_time_contract_v2"]["reason_counts_all_predicted"],
                },
                "diagnosis": report["diagnosis"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
