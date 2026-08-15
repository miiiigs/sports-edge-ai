"""Historical validation for football decision policies.

This module compares the original raw-edge policy with the stricter live
decision gate without retuning the frozen probability model.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Sequence

from ..pipeline.football_watchlist import (
    CANDIDATE_STATUS,
    FootballWatchlistConfig,
    _decision_as_candidate,
    _evaluate_selection_decision,
    _market_report,
)
from .football_poisson_backtest import (
    SELECTION_ORDER,
    FootballPoissonBacktestConfig,
    _connect_research_store,
    _edge_bucket,
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
class FootballDecisionPolicyValidationConfig:
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
    min_data_quality: float = 0.82
    max_uncertainty: float = 0.98
    min_uncertainty_adjusted_edge: float = 0.015
    require_model_convergence: bool = True
    output_path: str | Path | None = None


def _unit_return(selection: str, actual_selection: str, price: float) -> float:
    return price - 1.0 if selection == actual_selection else -1.0


def _safe_mean(values: list[float]) -> float | None:
    return mean(values) if values else None


def _max_drawdown(unit_returns: list[float]) -> float | None:
    if not unit_returns:
        return None
    peak = 0.0
    running = 0.0
    max_drawdown = 0.0
    for value in unit_returns:
        running += value
        peak = max(peak, running)
        max_drawdown = min(max_drawdown, running - peak)
    return max_drawdown


def _summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    unit_returns = [row["unit_return"] for row in rows]
    wins = [1.0 if row["settlement"] == "WIN" else 0.0 for row in rows]
    probabilities = [row["fair_probability"] for row in rows]
    odds = [row["available_price"] for row in rows]
    edges = [row["edge"] for row in rows]
    robust_edges = [
        row["uncertainty_adjusted_edge"]
        for row in rows
        if row.get("uncertainty_adjusted_edge") is not None
    ]
    return {
        "candidate_count": len(rows),
        "total_units": sum(unit_returns) if unit_returns else 0.0,
        "roi_per_bet": (sum(unit_returns) / len(unit_returns)) if unit_returns else None,
        "win_rate": _safe_mean(wins),
        "average_odds": _safe_mean(odds),
        "average_model_probability": _safe_mean(probabilities),
        "realized_rate": _safe_mean(wins),
        "calibration_gap": (
            _safe_mean(probabilities) - _safe_mean(wins)
            if probabilities and wins
            else None
        ),
        "average_edge": _safe_mean(edges),
        "average_uncertainty_adjusted_edge": _safe_mean(robust_edges),
        "max_drawdown_units": _max_drawdown(unit_returns),
        "clv": None,
        "clv_note": "CLV is unavailable unless trusted historical entry and close timestamps are present.",
    }


def _group_summary(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key) or "UNKNOWN")].append(row)
    return {name: _summarize_rows(group_rows) for name, group_rows in sorted(grouped.items())}


def _edge_bucket_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("edge_bucket") or "UNBUCKETED")].append(row)
    return {name: _summarize_rows(group_rows) for name, group_rows in sorted(grouped.items())}


def _candidate_row(
    *,
    event: dict[str, Any],
    actual_selection: str,
    decision: dict[str, Any],
    policy_name: str,
    reason_code: str,
) -> dict[str, Any]:
    price = float(decision["available_price"])
    selection = str(decision["selection"])
    unit_return = _unit_return(selection, actual_selection, price)
    row = {
        "policy_name": policy_name,
        "event_id": event["event_id"],
        "competition_id": event["competition_id"],
        "match_date": event["match_date"],
        "home_team": event["home_team"],
        "away_team": event["away_team"],
        "selection": selection,
        "available_price": price,
        "fair_probability": decision["fair_probability"],
        "fair_odds": decision["fair_odds"],
        "edge": decision["edge"],
        "raw_break_even_probability": decision["raw_break_even_probability"],
        "devig_market_probability": decision.get("devig_market_probability"),
        "model_vs_devig_market": decision.get("model_vs_devig_market"),
        "expected_value": decision.get("expected_value"),
        "conservative_probability": decision.get("conservative_probability"),
        "uncertainty_adjusted_edge": decision.get("uncertainty_adjusted_edge"),
        "league_reliability_modifier": decision.get("league_reliability_modifier"),
        "reason_code": reason_code,
        "settlement": "WIN" if unit_return > 0 else "LOSS",
        "unit_return": unit_return,
        "edge_bucket": _edge_bucket(float(decision["edge"])),
    }
    return row


def _watchlist_config(config: FootballDecisionPolicyValidationConfig) -> FootballWatchlistConfig:
    return FootballWatchlistConfig(
        research_db_path=config.research_db_path,
        season_code=config.season_code,
        division_codes=tuple(config.competition_ids),
        target_date="historical-validation",
        bookmaker=config.bookmaker,
        model_name=config.model_name if config.model_name in {"poisson_1x2_v1", "poisson_1x2_v3"} else "poisson_1x2_v3",
        min_team_matches=config.min_team_matches,
        prior_weight=config.prior_weight,
        strength_regularization=config.strength_regularization,
        edge_threshold=config.edge_threshold,
        min_data_quality=config.min_data_quality,
        max_uncertainty=config.max_uncertainty,
        min_uncertainty_adjusted_edge=config.min_uncertainty_adjusted_edge,
        require_model_convergence=config.require_model_convergence,
        allowed_selections=config.allowed_selections,
        max_fair_odds=config.max_fair_odds,
    )


def _policy_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "summary": _summarize_rows(rows),
        "by_competition": _group_summary(rows, "competition_id"),
        "by_selection": _group_summary(rows, "selection"),
        "by_edge_bucket": _edge_bucket_summary(rows),
    }


def run_football_decision_policy_validation(
    config: FootballDecisionPolicyValidationConfig,
) -> dict[str, Any]:
    """Compare old raw-edge candidates against the stricter robust gate."""

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
    robust_config = _watchlist_config(config)

    with _connect_research_store(config.research_db_path) as conn:
        all_events = _fetch_events(conn, competition_ids=config.competition_ids)
        market_map = _fetch_market_map(conn, season_code=config.season_code, bookmaker=config.bookmaker)

    by_competition_history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    old_rows: list[dict[str, Any]] = []
    robust_rows: list[dict[str, Any]] = []
    old_candidate_ids: set[str] = set()
    robust_candidate_ids: set[str] = set()
    reason_counts: Counter[str] = Counter()
    robust_selection_reason_counts: Counter[str] = Counter()
    evaluated_events = 0
    predicted_events = 0
    market_matched_events = 0
    evaluated_selection_count = 0

    for row in all_events:
        event = dict(row)
        competition_id = event["competition_id"]
        history = by_competition_history[competition_id]

        if event["season_code"] != config.season_code:
            history.append(event)
            continue

        evaluated_events += 1
        actual_selection = RESULT_TO_SELECTION.get(event.get("result_code"))
        eligible_history = _eligible_history_rows(
            history,
            history_season_codes=config.history_season_codes,
        )
        prediction = _fit_prediction(
            model_config,
            history_rows=eligible_history,
            event=event,
        )
        if prediction is None or actual_selection is None:
            history.append(event)
            continue

        predicted_events += 1
        quote = market_map.get(event["event_id"])
        if not quote or any(quote.get(selection) in (None, 0) for selection in SELECTION_ORDER):
            history.append(event)
            continue

        market_matched_events += 1
        market = _market_report({selection: float(quote[selection]) for selection in SELECTION_ORDER})
        old_best: dict[str, Any] | None = None
        robust_best: dict[str, Any] | None = None
        event_reason_counts: Counter[str] = Counter()

        for selection in config.allowed_selections:
            price = quote.get(selection)
            if price in (None, 0):
                continue
            decision = _evaluate_selection_decision(
                config=robust_config,
                competition_id=competition_id,
                selection=selection,
                price=float(price),
                prediction=prediction,
                market_report=market,
            )
            evaluated_selection_count += 1
            robust_selection_reason_counts[decision.reason_code] += 1
            event_reason_counts[decision.reason_code] += 1
            decision_payload = _decision_as_candidate(decision)

            if (
                (config.max_fair_odds is None or decision.fair_odds <= config.max_fair_odds)
                and decision.raw_edge >= config.edge_threshold
            ):
                if old_best is None or decision.raw_edge > old_best["edge"]:
                    old_best = decision_payload

            if decision.status == CANDIDATE_STATUS:
                if robust_best is None or decision.raw_edge > robust_best["edge"]:
                    robust_best = decision_payload

        if old_best is not None:
            old_candidate_ids.add(event["event_id"])
            robust_reason_for_old_candidate = str(old_best.get("reason_code") or "UNKNOWN")
            old_rows.append(
                _candidate_row(
                    event=event,
                    actual_selection=actual_selection,
                    decision=old_best,
                    policy_name="old_raw_edge_gate",
                    reason_code="OLD_GATE_RAW_EDGE",
                )
            )
            old_rows[-1]["robust_gate_reason_code"] = robust_reason_for_old_candidate

        if robust_best is not None:
            robust_candidate_ids.add(event["event_id"])
            robust_rows.append(
                _candidate_row(
                    event=event,
                    actual_selection=actual_selection,
                    decision=robust_best,
                    policy_name="new_robust_gate",
                    reason_code=robust_best["reason_code"],
                )
            )
        elif old_best is not None:
            matching_reason = str(old_best.get("reason_code") or "ROBUST_GATE_REJECTED")
            reason_counts[matching_reason] += 1

        history.append(event)

    overlap_ids = old_candidate_ids & robust_candidate_ids
    rejected_old_ids = old_candidate_ids - robust_candidate_ids
    new_only_ids = robust_candidate_ids - old_candidate_ids
    report = {
        "evaluation_mode": "historical_decision_policy_validation",
        "model_name": config.model_name,
        "model_status": "frozen_promoted_probability_model" if config.model_name == "poisson_1x2_v3" else "research_model",
        "season_code": config.season_code,
        "history_season_codes": list(config.history_season_codes) if config.history_season_codes else None,
        "competition_ids": list(config.competition_ids),
        "bookmaker": config.bookmaker,
        "policy_parameters": {
            "old_policy": {
                "edge_threshold": config.edge_threshold,
                "max_fair_odds": config.max_fair_odds,
                "allowed_selections": list(config.allowed_selections),
            },
            "new_robust_policy": {
                "edge_threshold": config.edge_threshold,
                "max_fair_odds": config.max_fair_odds,
                "min_data_quality": config.min_data_quality,
                "max_uncertainty": config.max_uncertainty,
                "min_uncertainty_adjusted_edge": config.min_uncertainty_adjusted_edge,
                "require_model_convergence": config.require_model_convergence,
                "allowed_selections": list(config.allowed_selections),
            },
        },
        "coverage": {
            "target_events": evaluated_events,
            "predicted_events": predicted_events,
            "market_matched_events": market_matched_events,
            "evaluated_selection_count": evaluated_selection_count,
        },
        "old_policy": _policy_report(old_rows),
        "new_robust_policy": _policy_report(robust_rows),
        "comparison": {
            "old_candidate_count": len(old_rows),
            "new_candidate_count": len(robust_rows),
            "candidate_count_delta": len(robust_rows) - len(old_rows),
            "overlap_candidate_count": len(overlap_ids),
            "old_candidates_rejected_by_new_gate": len(rejected_old_ids),
            "new_candidates_not_in_old_gate": len(new_only_ids),
            "selection_reason_counts": dict(sorted(robust_selection_reason_counts.items())),
            "old_candidate_rejection_reason_counts": dict(sorted(reason_counts.items())),
            "interpretation": (
                "Use this as a pre-specified policy evaluation. Do not retune thresholds from this single report."
            ),
        },
        "sample_rows": {
            "old_policy": old_rows[:20],
            "new_robust_policy": robust_rows[:20],
        },
    }

    if config.output_path is not None:
        target = Path(config.output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Validate old vs robust football decision policies.")
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
    parser.add_argument("--min-data-quality", type=float, default=0.82)
    parser.add_argument("--max-uncertainty", type=float, default=0.98)
    parser.add_argument("--min-uncertainty-adjusted-edge", type=float, default=0.015)
    parser.add_argument("--allow-nonconverged-model", action="store_true")
    parser.add_argument("--selection", action="append", dest="selections")
    parser.add_argument("--output")
    args = parser.parse_args(argv)

    report = run_football_decision_policy_validation(
        FootballDecisionPolicyValidationConfig(
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
            min_data_quality=args.min_data_quality,
            max_uncertainty=args.max_uncertainty,
            min_uncertainty_adjusted_edge=args.min_uncertainty_adjusted_edge,
            require_model_convergence=not args.allow_nonconverged_model,
            output_path=args.output,
        )
    )
    print(
        json.dumps(
            {
                "output": args.output,
                "coverage": report["coverage"],
                "old_policy": report["old_policy"]["summary"],
                "new_robust_policy": report["new_robust_policy"]["summary"],
                "comparison": report["comparison"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
