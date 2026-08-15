"""Walk-forward backtest for the blind football Poisson 1X2 scaffold."""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Sequence

from ..agents.football.poisson_1x2 import fit_poisson_1x2_from_history
from ..agents.football.poisson_1x2_v2 import fit_poisson_1x2_v2_from_history
from ..agents.football.poisson_1x2_v3 import fit_poisson_1x2_v3_from_history
from ..agents.football.poisson_1x2_v4 import fit_poisson_1x2_v4_from_history
from ..agents.market.devig import devig_shin

RESULT_TO_SELECTION = {
    "H": "home",
    "D": "draw",
    "A": "away",
}
SELECTION_ORDER = ("home", "draw", "away")
MODEL_NAMES = ("poisson_1x2_v1", "poisson_1x2_v2", "poisson_1x2_v3", "poisson_1x2_v4")
EDGE_BUCKETS = (
    (0.03, 0.05, "3-5%"),
    (0.05, 0.08, "5-8%"),
    (0.08, 0.10, "8-10%"),
    (0.10, 0.15, "10-15%"),
    (0.15, 0.20, "15-20%"),
    (0.20, None, "20%+"),
)
CALIBRATION_BUCKET_EDGES = tuple(step / 10 for step in range(11))


@dataclass(frozen=True)
class FootballPoissonBacktestConfig:
    research_db_path: str | Path
    season_code: str
    competition_ids: tuple[str, ...]
    bookmaker: str = "Bet365"
    model_name: str = "poisson_1x2_v1"
    min_team_matches: int = 6
    prior_weight: float = 5.0
    half_life_matches: float = 18.0
    strength_regularization: float = 0.1
    optimizer_learning_rate: float = 0.6
    optimizer_max_iterations: int = 250
    dixon_coles_rho: float = -0.1
    edge_threshold: float = 0.03
    max_fair_odds: float | None = None
    allowed_selections: tuple[str, ...] = SELECTION_ORDER
    history_season_codes: tuple[str, ...] | None = None
    include_candidate_rows: bool = False


def _connect_research_store(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_events(conn: sqlite3.Connection, *, competition_ids: Sequence[str]) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in competition_ids)
    sql = f"""
        SELECT
            event_id,
            competition_id,
            competition_name,
            season_code,
            match_date,
            home_team,
            away_team,
            home_goals,
            away_goals,
            result_code
        FROM football_events
        WHERE competition_id IN ({placeholders})
        ORDER BY competition_id, match_date, event_id
    """
    return list(conn.execute(sql, [*competition_ids]).fetchall())


def _fetch_market_map(conn: sqlite3.Connection, *, season_code: str, bookmaker: str) -> dict[str, dict[str, float]]:
    rows = conn.execute(
        """
        SELECT event_id, selection_prices_json
        FROM football_market_quotes
        WHERE season_code = ?
          AND market_type = '1X2'
          AND bookmaker = ?
        """,
        (season_code, bookmaker),
    ).fetchall()
    return {row["event_id"]: json.loads(row["selection_prices_json"]) for row in rows}


def _multiclass_log_loss(records: list[tuple[dict[str, float], str]]) -> float | None:
    if not records:
        return None
    total = 0.0
    for probs, actual in records:
        p = max(1e-12, min(1 - 1e-12, probs[actual]))
        total += -math.log(p)
    return total / len(records)


def _multiclass_brier(records: list[tuple[dict[str, float], str]]) -> float | None:
    if not records:
        return None
    total = 0.0
    for probs, actual in records:
        total += sum((probs[key] - (1.0 if key == actual else 0.0)) ** 2 for key in SELECTION_ORDER)
    return total / len(records)


def _binary_log_loss(pairs: list[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    total = 0.0
    for prob, actual in pairs:
        p = max(1e-12, min(1 - 1e-12, prob))
        total += -(actual * math.log(p) + (1 - actual) * math.log(1 - p))
    return total / len(pairs)


def _selection_summary(
    records: list[tuple[float, int]],
    *,
    candidate_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if not records:
        return {
            "sample_count": 0,
            "mean_predicted_probability": None,
            "realized_rate": None,
            "brier": None,
            "binary_log_loss": None,
            "candidate_count": len(candidate_rows),
            "roi_per_bet": None,
            "average_offered_odds": None,
        }

    unit_returns = [row["unit_return"] for row in candidate_rows]
    offered_odds = [row["available_price"] for row in candidate_rows]
    mean_predicted_probability = mean(prob for prob, _ in records)
    realized_rate = mean(actual for _, actual in records)
    return {
        "sample_count": len(records),
        "mean_predicted_probability": mean_predicted_probability,
        "realized_rate": realized_rate,
        "calibration_gap": mean_predicted_probability - realized_rate,
        "brier": mean((prob - actual) ** 2 for prob, actual in records),
        "binary_log_loss": _binary_log_loss(records),
        "candidate_count": len(candidate_rows),
        "roi_per_bet": (sum(unit_returns) / len(unit_returns)) if unit_returns else None,
        "average_offered_odds": mean(offered_odds) if offered_odds else None,
    }


def _calibration_bucket_summary(records: list[tuple[float, int]]) -> list[dict[str, Any]]:
    buckets: list[dict[str, Any]] = []
    for lower, upper in zip(CALIBRATION_BUCKET_EDGES[:-1], CALIBRATION_BUCKET_EDGES[1:], strict=False):
        bucket_pairs = [
            (prob, actual)
            for prob, actual in records
            if (lower <= prob < upper) or (upper == 1.0 and lower <= prob <= upper)
        ]
        if bucket_pairs:
            mean_pred = mean(prob for prob, _ in bucket_pairs)
            realized = mean(actual for _, actual in bucket_pairs)
        else:
            mean_pred = None
            realized = None
        buckets.append(
            {
                "bucket": f"{int(lower * 100)}-{int(upper * 100)}%",
                "sample_count": len(bucket_pairs),
                "mean_predicted_probability": mean_pred,
                "realized_rate": realized,
                "calibration_gap": (None if mean_pred is None or realized is None else mean_pred - realized),
            }
        )
    return buckets


def _competition_summary(
    *,
    model_records: list[tuple[dict[str, float], str]],
    market_records: list[tuple[dict[str, float], str]],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    unit_returns = [candidate["unit_return"] for candidate in candidates]
    odds = [candidate["available_price"] for candidate in candidates]
    return {
        "sample_count": len(model_records),
        "model_log_loss": _multiclass_log_loss(model_records),
        "model_brier": _multiclass_brier(model_records),
        "market_log_loss": _multiclass_log_loss(market_records),
        "market_brier": _multiclass_brier(market_records),
        "candidate_count": len(candidates),
        "roi_per_bet": (sum(unit_returns) / len(unit_returns)) if unit_returns else None,
        "average_offered_odds": mean(odds) if odds else None,
    }


def _edge_bucket(edge: float) -> str | None:
    for lower, upper, label in EDGE_BUCKETS:
        if edge < lower:
            continue
        if upper is None or edge < upper:
            return label
    return None


def _eligible_history_rows(
    history_rows: list[dict[str, Any]],
    *,
    history_season_codes: tuple[str, ...] | None,
) -> list[dict[str, Any]]:
    if history_season_codes is None:
        return history_rows
    allowed = set(history_season_codes)
    return [row for row in history_rows if row.get("season_code") in allowed]


def _fit_prediction(
    config: FootballPoissonBacktestConfig,
    *,
    history_rows: list[dict[str, Any]],
    event: dict[str, Any],
) -> dict[str, Any] | None:
    common_kwargs = {
        "history_rows": history_rows,
        "event_id": event["event_id"],
        "home_team": event["home_team"],
        "away_team": event["away_team"],
        "min_team_matches": config.min_team_matches,
        "prior_weight": config.prior_weight,
    }
    if config.model_name == "poisson_1x2_v1":
        return fit_poisson_1x2_from_history(**common_kwargs)
    if config.model_name == "poisson_1x2_v2":
        return fit_poisson_1x2_v2_from_history(
            **common_kwargs,
            half_life_matches=config.half_life_matches,
        )
    if config.model_name == "poisson_1x2_v3":
        return fit_poisson_1x2_v3_from_history(
            **common_kwargs,
            strength_regularization=config.strength_regularization,
            learning_rate=config.optimizer_learning_rate,
            max_iterations=config.optimizer_max_iterations,
        )
    if config.model_name == "poisson_1x2_v4":
        return fit_poisson_1x2_v4_from_history(
            **common_kwargs,
            strength_regularization=config.strength_regularization,
            learning_rate=config.optimizer_learning_rate,
            max_iterations=config.optimizer_max_iterations,
            dixon_coles_rho=config.dixon_coles_rho,
        )
    raise ValueError(f"unknown model_name: {config.model_name}")


def run_football_poisson_backtest(config: FootballPoissonBacktestConfig) -> dict[str, Any]:
    """Run a walk-forward blind backtest using stored prior football results."""

    if config.model_name not in MODEL_NAMES:
        raise ValueError(f"model_name must be one of {MODEL_NAMES}")

    with _connect_research_store(config.research_db_path) as conn:
        all_events = _fetch_events(conn, competition_ids=config.competition_ids)
        market_map = _fetch_market_map(conn, season_code=config.season_code, bookmaker=config.bookmaker)

    by_competition_history: dict[str, list[dict[str, Any]]] = defaultdict(list)
    prediction_records: list[tuple[dict[str, float], str]] = []
    market_records: list[tuple[dict[str, float], str]] = []
    candidate_rows: list[dict[str, Any]] = []
    sample_predictions: list[dict[str, Any]] = []
    selection_probability_records: dict[tuple[str, str], list[tuple[float, int]]] = defaultdict(list)
    competition_model_records: dict[str, list[tuple[dict[str, float], str]]] = defaultdict(list)
    competition_market_records: dict[str, list[tuple[dict[str, float], str]]] = defaultdict(list)
    competition_candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    target_events = 0
    predicted_count = 0
    prior_history_rows_available = 0
    prior_season_rows_used = 0

    for row in all_events:
        event = dict(row)
        competition_id = event["competition_id"]
        history = by_competition_history[competition_id]

        if event["season_code"] != config.season_code:
            history.append(event)
            continue

        target_events += 1
        eligible_history = _eligible_history_rows(
            history,
            history_season_codes=config.history_season_codes,
        )
        prior_history_rows_available += len(eligible_history)
        prior_season_rows_used += sum(1 for item in eligible_history if item.get("season_code") != config.season_code)
        prediction = _fit_prediction(
            config,
            history_rows=eligible_history,
            event=event,
        )

        actual_selection = RESULT_TO_SELECTION.get(event["result_code"])
        if prediction is not None and actual_selection is not None:
            predicted_count += 1
            probs = prediction["probabilities"]
            prediction_records.append((probs, actual_selection))
            competition_model_records[competition_id].append((probs, actual_selection))

            for selection in SELECTION_ORDER:
                selection_probability_records[(competition_id, selection)].append(
                    (probs[selection], 1 if selection == actual_selection else 0)
                )

            quote = market_map.get(event["event_id"])
            best_candidate: dict[str, Any] | None = None
            if quote and all(quote.get(selection) not in (None, 0) for selection in SELECTION_ORDER):
                market_probs = devig_shin([float(quote[selection]) for selection in SELECTION_ORDER])
                market_prob_map = dict(zip(SELECTION_ORDER, market_probs, strict=False))
                market_records.append((market_prob_map, actual_selection))
                competition_market_records[competition_id].append((market_prob_map, actual_selection))

                for selection in config.allowed_selections:
                    available_price = quote.get(selection)
                    fair_prob = probs[selection]
                    fair_odds = prediction["fair_odds"][selection]
                    if available_price in (None, 0):
                        continue
                    if config.max_fair_odds is not None and fair_odds > config.max_fair_odds:
                        continue
                    edge = fair_prob - (1.0 / float(available_price))
                    if edge < config.edge_threshold:
                        continue
                    candidate = {
                        "event_id": event["event_id"],
                        "competition_id": competition_id,
                        "match_date": event["match_date"],
                        "home_team": event["home_team"],
                        "away_team": event["away_team"],
                        "selection": selection,
                        "available_price": float(available_price),
                        "fair_probability": fair_prob,
                        "fair_odds": fair_odds,
                        "market_probability": market_prob_map.get(selection),
                        "model_vs_market": fair_prob - market_prob_map[selection],
                        "edge": edge,
                        "unit_return": (float(available_price) - 1.0) if selection == actual_selection else -1.0,
                        "settlement": "WIN" if selection == actual_selection else "LOSS",
                        "edge_bucket": _edge_bucket(edge),
                    }
                    if best_candidate is None or candidate["edge"] > best_candidate["edge"]:
                        best_candidate = candidate

            if best_candidate is not None:
                candidate_rows.append(best_candidate)
                competition_candidates[competition_id].append(best_candidate)

            if len(sample_predictions) < 5:
                sample_predictions.append(
                    {
                        "event_id": event["event_id"],
                        "competition_id": competition_id,
                        "match_date": event["match_date"],
                        "home_team": event["home_team"],
                        "away_team": event["away_team"],
                        "probabilities": probs,
                        "fair_odds": prediction["fair_odds"],
                        "actual_result": actual_selection,
                        "candidate": best_candidate,
                    }
                )

        history.append(event)

    total_units = sum(candidate["unit_return"] for candidate in candidate_rows)
    edge_bucket_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    selection_candidate_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidate_rows:
        selection_candidate_rows[candidate["selection"]].append(candidate)
        if candidate["edge_bucket"] is not None:
            edge_bucket_rows[candidate["edge_bucket"]].append(candidate)

    competition_report = {
        competition_id: _competition_summary(
            model_records=competition_model_records[competition_id],
            market_records=competition_market_records[competition_id],
            candidates=competition_candidates[competition_id],
        )
        for competition_id in config.competition_ids
    }
    selection_report = {
        competition_id: {
            selection: _selection_summary(
                selection_probability_records[(competition_id, selection)],
                candidate_rows=[row for row in competition_candidates[competition_id] if row["selection"] == selection],
            )
            for selection in SELECTION_ORDER
        }
        for competition_id in config.competition_ids
    }
    edge_bucket_report = {}
    for _, _, label in EDGE_BUCKETS:
        rows = edge_bucket_rows[label]
        edge_bucket_report[label] = {
            "candidate_count": len(rows),
            "roi_per_bet": (sum(row["unit_return"] for row in rows) / len(rows)) if rows else None,
            "average_offered_odds": mean(row["available_price"] for row in rows) if rows else None,
        }

    all_selection_report = {
        selection: _selection_summary(
            [pair for (competition_id, selection_key), records in selection_probability_records.items() if selection_key == selection for pair in records],
            candidate_rows=selection_candidate_rows[selection],
        )
        for selection in SELECTION_ORDER
    }
    calibration_bucket_report = {
        selection: _calibration_bucket_summary(
            [
                pair
                for (competition_id, selection_key), records in selection_probability_records.items()
                if selection_key == selection
                for pair in records
            ]
        )
        for selection in SELECTION_ORDER
    }
    model_evaluation = {
        "event_count": target_events,
        "predicted_count": predicted_count,
        "history_semantics": {
            "mode": (
                "all_stored_prior_matches_before_each_target_event"
                if config.history_season_codes is None
                else "all_stored_prior_matches_before_each_target_event_filtered_to_history_seasons"
            ),
            "prior_history_rows_available": prior_history_rows_available,
            "prior_season_rows_used": prior_season_rows_used,
        },
        "prediction_metrics": {
            "multiclass_log_loss": _multiclass_log_loss(prediction_records),
            "multiclass_brier": _multiclass_brier(prediction_records),
        },
        "market_benchmark": {
            "matched_market_count": len(market_records),
            "multiclass_log_loss": _multiclass_log_loss(market_records),
            "multiclass_brier": _multiclass_brier(market_records),
        },
        "reports": {
            "by_competition": competition_report,
            "by_selection": all_selection_report,
            "by_competition_selection": selection_report,
            "calibration_buckets_by_selection": calibration_bucket_report,
        },
    }
    policy_evaluation = {
        "policy": {
            "edge_threshold": config.edge_threshold,
            "max_fair_odds": config.max_fair_odds,
            "allowed_selections": list(config.allowed_selections),
        },
        "candidate_metrics": {
            "candidate_count": len(candidate_rows),
            "total_units": total_units,
            "roi_per_bet": (total_units / len(candidate_rows)) if candidate_rows else None,
            "win_rate": (
                sum(candidate["unit_return"] > 0 for candidate in candidate_rows) / len(candidate_rows)
                if candidate_rows
                else None
            ),
        },
        "reports": {
            "by_edge_bucket": edge_bucket_report,
            "candidate_by_selection": {
                selection: {
                    "candidate_count": len(selection_candidate_rows[selection]),
                    "roi_per_bet": (
                        sum(row["unit_return"] for row in selection_candidate_rows[selection])
                        / len(selection_candidate_rows[selection])
                    )
                    if selection_candidate_rows[selection]
                    else None,
                    "average_offered_odds": (
                        mean(row["available_price"] for row in selection_candidate_rows[selection])
                        if selection_candidate_rows[selection]
                        else None
                    ),
                }
                for selection in SELECTION_ORDER
            },
        },
    }

    result = {
        "model_name": config.model_name,
        "season_code": config.season_code,
        "competition_ids": list(config.competition_ids),
        "bookmaker": config.bookmaker,
        "min_team_matches": config.min_team_matches,
        "prior_weight": config.prior_weight,
        "half_life_matches": config.half_life_matches,
        "strength_regularization": config.strength_regularization,
        "optimizer_learning_rate": config.optimizer_learning_rate,
        "optimizer_max_iterations": config.optimizer_max_iterations,
        "dixon_coles_rho": config.dixon_coles_rho,
        "edge_threshold": config.edge_threshold,
        "max_fair_odds": config.max_fair_odds,
        "allowed_selections": list(config.allowed_selections),
        "history_season_codes": list(config.history_season_codes) if config.history_season_codes else None,
        "event_count": model_evaluation["event_count"],
        "predicted_count": model_evaluation["predicted_count"],
        "history_semantics": model_evaluation["history_semantics"],
        "prediction_metrics": model_evaluation["prediction_metrics"],
        "market_benchmark": model_evaluation["market_benchmark"],
        "candidate_metrics": policy_evaluation["candidate_metrics"],
        "reports": {
            **model_evaluation["reports"],
            **policy_evaluation["reports"],
        },
        "model_evaluation": model_evaluation,
        "policy_evaluation": policy_evaluation,
        "sample_predictions": sample_predictions,
    }
    if config.include_candidate_rows:
        result["candidate_rows"] = candidate_rows
    return result


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Backtest the blind football Poisson 1X2 scaffold.")
    parser.add_argument("--research-db", required=True, help="Path to football research SQLite store.")
    parser.add_argument("--season", required=True, help="Season code like 2526.")
    parser.add_argument(
        "--competition",
        action="append",
        dest="competitions",
        required=True,
        help="Competition id like ENG1 or ESP1. Repeat for multiple leagues.",
    )
    parser.add_argument("--bookmaker", default="Bet365", help="Bookmaker used for historical comparison.")
    parser.add_argument(
        "--model",
        default="poisson_1x2_v1",
        choices=MODEL_NAMES,
        help="Model version to backtest.",
    )
    parser.add_argument("--min-team-matches", type=int, default=6, help="Minimum prior matches per team.")
    parser.add_argument("--prior-weight", type=float, default=5.0, help="Shrinkage weight toward league average.")
    parser.add_argument(
        "--half-life-matches",
        type=float,
        default=18.0,
        help="Recency half-life in matches for v2.",
    )
    parser.add_argument(
        "--strength-regularization",
        type=float,
        default=0.1,
        help="L2 shrinkage strength for v3 opponent-adjusted team parameters.",
    )
    parser.add_argument(
        "--optimizer-learning-rate",
        type=float,
        default=0.6,
        help="Optimizer learning rate for v3 team-strength estimation.",
    )
    parser.add_argument(
        "--optimizer-max-iterations",
        type=int,
        default=250,
        help="Optimizer iteration cap for v3 team-strength estimation.",
    )
    parser.add_argument(
        "--dixon-coles-rho",
        type=float,
        default=-0.1,
        help="Low-score Dixon-Coles correction parameter for v4.",
    )
    parser.add_argument("--edge-threshold", type=float, default=0.03, help="Minimum edge for a candidate pick.")
    parser.add_argument("--max-fair-odds", type=float, help="Optional cap on fair odds to avoid longshots.")
    parser.add_argument(
        "--history-season",
        action="append",
        dest="history_seasons",
        help="Optional history season filter. Repeat to freeze training seasons.",
    )
    parser.add_argument(
        "--selection",
        action="append",
        dest="selections",
        help="Allowed selection key like home, draw, away. Repeat to narrow picks.",
    )
    args = parser.parse_args(argv)

    result = run_football_poisson_backtest(
        FootballPoissonBacktestConfig(
            research_db_path=args.research_db,
            season_code=args.season,
            competition_ids=tuple(args.competitions),
            bookmaker=args.bookmaker,
            model_name=args.model,
            min_team_matches=args.min_team_matches,
            prior_weight=args.prior_weight,
            half_life_matches=args.half_life_matches,
            strength_regularization=args.strength_regularization,
            optimizer_learning_rate=args.optimizer_learning_rate,
            optimizer_max_iterations=args.optimizer_max_iterations,
            dixon_coles_rho=args.dixon_coles_rho,
            edge_threshold=args.edge_threshold,
            max_fair_odds=args.max_fair_odds,
            allowed_selections=tuple(args.selections) if args.selections else SELECTION_ORDER,
            history_season_codes=tuple(args.history_seasons) if args.history_seasons else None,
        )
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
